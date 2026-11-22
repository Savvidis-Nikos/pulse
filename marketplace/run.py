"""Simulate the marketplace for the demo period and write the data contract.

python -m marketplace.run                       21 days (1-21 Nov 2026), all scenarios -> data/eye/
python -m marketplace.run --scenarios core      only the 8 original scenarios (reproduces the earlier data)
python -m marketplace.run --seed 11 --out data/eye_seed11
python -m marketplace.run --no-scenarios        a quiet period (false-alarm test)
python -m marketplace.run --user-sample 0.02    user-level panel of 2 % of sessions (default 1 %, 0 = off)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .scenarios import ALL_SCENARIOS, SCENARIOS, timeline
from .world import Marketplace

ROOT = Path(__file__).resolve().parents[1]
H = pd.Timedelta(hours=1)
START, DAYS = "2026-11-01 00:00", 21


def simulate(seed=7, start=START, days=DAYS, actions=None, until=None, user_sample=0.0):
    """Run hour by hour; apply each action at its hour (before that hour is simulated)."""
    mp = Marketplace(seed=seed, start=start, user_sample=user_sample, days=days)
    actions = sorted(actions if actions is not None else timeline(), key=lambda a: a[0])
    end = pd.Timestamp(until) if until is not None else pd.Timestamp(start) + days * 24 * H
    i = 0
    ts = pd.Timestamp(start)
    while ts < end:
        while i < len(actions) and actions[i][0] < ts + H:
            when, fn, kw = actions[i]
            fn(mp, when, **kw)
            i += 1
        mp.step(ts)
        ts += H
    return mp


def write(mp, out: Path, scenarios, seed):
    out.mkdir(parents=True, exist_ok=True)
    facts, inv, ev, comp = mp.tables()
    facts.to_csv(out / "facts.csv", index=False)
    inv.to_csv(out / "inventory.csv", index=False)
    ev.to_csv(out / "events.csv", index=False)
    comp.to_csv(out / "completeness.csv", index=False)
    prod, feed, ads = mp.detail_tables()                       # drill-down tables (product, feed, advertiser)
    for name, df in mp.ops_tables().items():                   # ads auctions, sellers, services, support, ...
        df.to_csv(out / f"{name}.csv", index=False)
    prod.to_csv(out / "products.csv", index=False)
    feed.to_csv(out / "feed_exposure.csv", index=False)
    ads.to_csv(out / "ads_advertisers.csv", index=False)
    ut = mp.user_tables()                                      # user-level panel (sessions, clicks, orders)
    if ut is not None:
        for name, df in zip(("users", "sessions", "clickstream", "orders", "order_items"), ut):
            df.to_csv(out / f"{name}.csv", index=False)
        (out / "users_meta.json").write_text(json.dumps(dict(
            label="DEMO DATA (simulated customers)", sample=mp.panel.sample,
            note="each session row stands for 1/sample real sessions; orders.csv misses rows while orders_db is late")),
            encoding="utf-8")
    (out / "injected.json").write_text(json.dumps(dict(label="DEMO DATA (simulated marketplace)", seed=seed,
                                                       scenarios=scenarios), indent=2), encoding="utf-8")
    return facts, inv, ev, comp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "data" / "eye"))
    ap.add_argument("--no-scenarios", action="store_true")
    ap.add_argument("--scenarios", choices=["all", "core"], default="all",
                    help="core = the 8 original scenarios only (operations-layer tables are written either way)")
    ap.add_argument("--user-sample", type=float, default=0.01, help="share of sessions in the user panel (0 = off)")
    a = ap.parse_args()
    core = a.scenarios == "core"
    acts = [] if a.no_scenarios else timeline(extended=not core)
    mp = simulate(a.seed, actions=acts, user_sample=a.user_sample)
    sc = [] if a.no_scenarios else (SCENARIOS if core else ALL_SCENARIOS)
    facts, inv, ev, comp = write(mp, Path(a.out), sc, a.seed)
    print(f"simulated marketplace -> {a.out}")
    print(f"  facts: {len(facts):,} rows ({facts.ts.nunique()} hours) | events: {len(ev)} | scenarios: {len(sc)}")
    if mp.panel is not None:
        print(f"  user panel ({a.user_sample:.0%} of sessions): {len(mp.panel.sessions):,} sessions, "
              f"{len(mp.panel.clicks):,} clickstream rows, {len(mp.panel.orders):,} orders")
    for s in sc:
        print(f"  - {s['kind']:<9} {s['id']:<20} {s['start']}  {s['segment']}")


if __name__ == "__main__":
    main()
