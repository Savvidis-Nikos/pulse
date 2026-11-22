"""Live mode: the marketplace keeps running hour by hour; you break things while a monitoring system watches.

python -m marketplace.live start                      quiet history 1-14 Nov, clock at 15 Nov 00:00
python -m marketplace.live inject payment_failure     scenario starts at the next simulated hour (writes ground truth)
python -m marketplace.live inject fr_campaign         ... innocent change
python -m marketplace.live tick [--hours 3]           advance the marketplace (rewrites data/eye/)
python -m marketplace.live loop [--seconds 2]         tick forever (Ctrl+C to stop); inject from a second terminal
python -m marketplace.live list                       available scenarios

The marketplace is independent of any monitoring code: a consumer reads data/eye/ after each tick
(live.json holds the current clock "now").

Deterministic: same seed + same injections = identical data, so a demo can be repeated exactly.
Speed: `start` and `tick` re-simulate the whole history (~25 s); `loop` does that once and then simulates only
the new hour on each tick (~2 s per tick incl. writing the files), with identical results.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from . import scenarios as S
from .run import ROOT, write, simulate

H = pd.Timedelta(hours=1)
OUT = ROOT / "data" / "eye"
STATE = OUT / "live.json"
HISTORY_START = "2026-11-01 00:00"
LIVE_START = "2026-11-15 00:00"


def _seg(**kw):
    return {k: v for k, v in kw.items()}


# scenario name -> (actions relative to t0, ground truth). t0 = first hour the scenario is live.
def build(name: str, t0: pd.Timestamp):
    o = lambda h: t0 + pd.Timedelta(hours=h)   # noqa: E731
    sc = {s["id"]: dict(s) for s in S.ALL_SCENARIOS}
    if name in S.EXTRA_BUILDERS:                               # operations-layer scenarios share their builder
        acts, start, end = S.EXTRA_BUILDERS[name](t0)
        truth = sc[name]
        truth.update(start=str(start)[:16], end=str(end)[:16])
        return acts, truth
    if name == "payment_failure":
        acts = [(o(0), S.deploy_payment_sdk, dict(platform="android", version="7.4.0", breaks=("DE", "local"))),
                (o(0), S.harmless, dict(type_="release", name="web checkout button restyle", component="checkout",
                                        owner="checkout-web", spec=dict(platform="web"))),
                (o(12), S.deploy_payment_sdk, dict(platform="android", version="7.3.9", rollback_of="7.4.0"))]
        end = o(12)
    elif name == "fr_campaign":
        acts = [(o(0), S.start_campaign, dict(name="FR Black Week teaser campaign", country="FR", uplift=0.6,
                                              intent=0.25, cps=0.0, owner="crm-fr", until=o(30)))]
        end = o(30)
    elif name == "search_relevance":
        acts = [(o(0), S.deploy_ranker, dict(version="2.3", country="DE", locale_bug=True)),
                (o(1), S.recs_release, dict(name="recommendations model refresh (For You)")),
                (o(12), S.deploy_ranker, dict(version="2.2", country="DE", planned=False, rollback=True))]
        end = o(12)
    elif name == "foryou_unavailable":
        acts = [(o(0), S.recs_release, dict(name="For You model v12 (inventory filter off)", model="v12",
                                            inventory_filter=False, oos_bias=20.0)),
                (o(0), S.harmless, dict(type_="release", name="web footer links update", component="app",
                                        owner="web-platform", spec=dict(platform="web"))),
                (o(12), S.recs_release, dict(name="For You model v12 rolled back to v11", model="v11",
                                             inventory_filter=True, oos_bias=1.0, planned=False, type_="rollback"))]
        end = o(12)
    elif name == "ads_pacing":
        day = t0.normalize() + pd.Timedelta(hours=6)
        t0 = day if day >= t0 else day + pd.Timedelta(days=1)      # the pacer bug shows from the 06:00 auction
        acts = [(t0, S.ads_pacer_release, dict(version="3.1", category="home", broken=True)),
                (t0 + H, S.harmless, dict(type_="release", name="home category landing page refresh",
                                          component="app", owner="web-platform", spec=dict(category="home"))),
                (t0 + pd.Timedelta(hours=18), S.ads_pacer_release, dict(version="3.0", category="home", broken=False,
                                                                       rollback=True))]
        end = t0 + pd.Timedelta(hours=18)
    elif name == "margin_squeeze":
        acts = [(o(0), S.start_campaign, dict(name="NL paid social push", country="NL", uplift=0.35, intent=1.0,
                                              cps=0.40, until=o(21))),
                (o(0), S.recs_release, dict(name="For You: discount-affinity boost (NL)",
                                            discount_affinity={"NL": 0.13}, spec=dict(country="NL"))),
                (o(0), S.harmless, dict(type_="release", name="web header i18n strings (NL)", component="app",
                                        owner="web-platform", spec=dict(country="NL", platform="web"))),
                (o(21), S.recs_release, dict(name="For You: discount-affinity boost (NL) switched off",
                                             discount_affinity={"NL": 0.0}, spec=dict(country="NL"), planned=False,
                                             type_="rollback"))]
        end = o(21)
    elif name == "stockout":
        day6 = t0.normalize() + pd.Timedelta(hours=6)
        eff = day6 if day6 > t0 else day6 + pd.Timedelta(days=1)    # bites at the next 06:00 delivery
        acts = [(t0, S.supplier_delay, dict(category="electronics", from_ts=eff, until=eff + pd.Timedelta(days=3),
                                            name="Delivery delayed: Voltix wholesale PO #4471")),
                (eff - pd.Timedelta(minutes=30), S.harmless, dict(type_="purchasing",
                 name="Wholesale reorder run (electronics) at normal volume", component="wholesale",
                 owner="purchasing-system", spec=dict(category="electronics")))]
        t0, end = eff, eff + pd.Timedelta(days=3)
    elif name == "late_orders_data":
        acts = [(o(0), S.late_data, dict(source="orders_db", shares=[0.55, 0.42, 0.38]))]
        end = o(3)
    else:
        raise SystemExit(f"unknown scenario '{name}'. Try: python -m marketplace.live list")
    truth = sc[name]
    truth.update(start=str(t0)[:16], end=str(end)[:16])
    return acts, truth


NAMES = ["payment_failure", "fr_campaign", "search_relevance", "foryou_unavailable", "ads_pacing", "margin_squeeze",
         "stockout", "late_orders_data"] + list(S.EXTRA_BUILDERS)


def _load():
    if not STATE.exists():
        raise SystemExit("no live state: run  python -m marketplace.live start")
    return json.loads(STATE.read_text(encoding="utf-8"))


def _save(st):
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def _render(st):
    acts, truths = [], []
    for inj in st["injected"]:
        a, t = build(inj["name"], pd.Timestamp(inj["at"]))
        acts += a
        truths.append(t)
    mp = simulate(st["seed"], start=HISTORY_START, actions=acts, until=pd.Timestamp(st["now"]))
    write(mp, OUT, truths, st["seed"])


def cmd_start(a):
    OUT.mkdir(parents=True, exist_ok=True)
    st = dict(seed=a.seed, now=LIVE_START, injected=[])
    _save(st)
    _render(st)
    print(f"live marketplace ready: history {HISTORY_START} .. {st['now']} (quiet)")


def cmd_inject(a):
    st = _load()
    at = pd.Timestamp(st["now"])                      # the next hour to be simulated
    st["injected"].append(dict(name=a.name, at=str(at)))
    _save(st)
    acts, truth = build(a.name, at)
    print(f"injected '{a.name}' -> live from {truth['start']} until {truth['end']} "
          f"({len(acts)} change-log actions, incl. decoys). Ground truth written on the next tick.")


def cmd_tick(a, quiet=False):
    st = _load()
    st["now"] = str(pd.Timestamp(st["now"]) + a.hours * H)
    _save(st)
    _render(st)
    if not quiet:
        print(f"marketplace clock: {st['now']}")


def cmd_loop(a):
    """Tick forever, incrementally: the marketplace stays in memory and each tick simulates only the new hour(s)
    (~0.1 s per hour + ~1.5 s to write the files) instead of re-simulating the whole history (~25 s).
    Injections from another terminal are picked up at the next tick and applied at their hour, exactly as a
    re-simulation would apply them; hours that were already published never change."""
    st = _load()
    pending, truths = [], []
    for inj in st["injected"]:
        acts, truth = build(inj["name"], pd.Timestamp(inj["at"]))
        pending += acts
        truths.append(truth)
    seen = len(st["injected"])
    now = pd.Timestamp(st["now"])
    print(f"live loop: preparing history up to {now} (once, ~25 s) ...")
    mp = simulate(st["seed"], start=HISTORY_START, actions=pending, until=now)
    pending = [x for x in pending if x[0] >= now]          # simulate() applied everything before `now`
    write(mp, OUT, truths, st["seed"])
    print(f"live loop running: 1 tick = {a.hours} h every ~{a.seconds:g} s. Ctrl+C to stop. "
          "Inject from a second terminal: python -m marketplace.live inject <name>")
    while True:
        time.sleep(a.seconds)
        st = _load()
        for inj in st["injected"][seen:]:                  # injected from the other terminal since the last tick
            acts, truth = build(inj["name"], pd.Timestamp(inj["at"]))
            pending += acts
            truths.append(truth)
            print(f"  picked up '{inj['name']}' (live from {truth['start']})")
        seen = len(st["injected"])
        pending.sort(key=lambda x: x[0])
        for _ in range(a.hours):
            due = [x for x in pending if x[0] < now + H]
            pending = [x for x in pending if x[0] >= now + H]
            for when, fn, kw in due:
                fn(mp, when, **kw)
            mp.step(now)
            now += H
        st = _load()                                       # re-read: keep injections made during this tick
        st["now"] = str(now)                               # clock first, so a new injection lands on the next hour
        _save(st)
        write(mp, OUT, truths, st["seed"])
        print(f"marketplace clock: {now}")


def main():
    ap = argparse.ArgumentParser(prog="python -m marketplace.live")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start"); p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("inject"); p.add_argument("name", choices=NAMES)
    for name in ("tick", "loop"):
        p = sub.add_parser(name)
        p.add_argument("--hours", type=int, default=1)
        p.add_argument("--seconds", type=float, default=2.0)
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "list":
        for n in NAMES:
            s = next(x for x in S.ALL_SCENARIOS if x["id"] == n)
            print(f"  {n:<20} {s['kind']:<9} {s['cause'][:90]}")
        return
    {"start": cmd_start, "inject": cmd_inject, "tick": cmd_tick, "loop": cmd_loop}[a.cmd](a)


if __name__ == "__main__":
    main()
