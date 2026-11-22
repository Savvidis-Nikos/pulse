"""Scorecard: replay the whole simulated range and score Pulse against the simulator's ground truth
(data/eye/injected.json). Runs in a throwaway state dir; mock LLM unless PULSE_LLM says otherwise.

    python -m pulse.evaluate [--start "2026-11-08 00:00"] [--end "2026-11-21 23:00"] [--json out.json]

Per injected scenario:
  fault     detected (an incident opened in its window), time to detect, root cause named (the interpreter's
            headline / path / first cause mentions the injected change event), segment named
  innocent  no incident opened (planned events must be explained, not alerted)
  data      seen as data_issue, no incident
plus incidents opened outside every scenario window (false alarms). Only meaningful on simulated data.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pandas as pd

from pulse import contracts as C

H = pd.Timedelta(hours=1)


def _tokens(s) -> set[str]:
    return {t for t in str(s).lower().replace("(", " ").replace(")", " ").replace(",", " ").split() if len(t) > 2}


def _window(sc, post_h=3):
    return pd.Timestamp(sc["start"]) - H, pd.Timestamp(sc["end"]) + post_h * H


def score(incs: list[C.Incident], interps: dict[str, dict], nonok: dict[str, set], truth: list[dict]) -> dict:
    owner = {}   # each incident belongs to the latest-starting scenario whose window holds its opening hour
    for i in incs:
        t = pd.Timestamp(i.opened_at)
        cands = [sc for sc in truth if _window(sc)[0] <= t <= _window(sc)[1]]
        if cands:
            owner[i.id] = max(cands, key=lambda sc: pd.Timestamp(sc["start"]))["id"]
    rows = []
    for sc in truth:
        mine = [i for i in incs if owner.get(i.id) == sc["id"]]
        r = {"id": sc["id"], "kind": sc["kind"], "incidents": [i.id for i in mine]}
        if sc["kind"] == "fault":
            fce = sc.get("first_cause_event")
            fce = " ".join(fce) if isinstance(fce, list) else (fce or "")
            it = interps.get(mine[0].id, {}) if mine else {}
            text = json.dumps({k: it.get(k) for k in ("headline", "summary", "path", "first_cause")}).lower()
            r.update(detected=bool(mine), headline=it.get("headline"), confidence=it.get("confidence"),
                     hours_to_detect=(pd.Timestamp(mine[0].opened_at) - pd.Timestamp(sc["start"])) / H if mine else None,
                     cause_named=(len(_tokens(fce) & _tokens(text)) >= max(1, len(_tokens(fce)) // 2)) if fce and mine else None,
                     segment_named=all(str(v).lower() in text for v in sc["segment"].values()) if sc["segment"] and mine else None)
        else:
            a, b = _window(sc)
            r["false_alert"] = bool(mine)
            if sc["kind"] == "data":
                r["data_issue_seen"] = any(a <= pd.Timestamp(ts) <= b for ts in nonok.get("data_issue", ()))
        rows.append(r)
    faults = [r for r in rows if r["kind"] == "fault"]
    by = {sc["id"]: sc for sc in truth}
    return {"faults_detected": f"{sum(r['detected'] for r in faults)}/{len(faults)}",
            "root_cause_named": f"{sum(bool(r['cause_named']) for r in faults)}/"
                                f"{sum(1 for r in faults if by[r['id']].get('first_cause_event'))}",
            "segment_named": f"{sum(bool(r['segment_named']) for r in faults)}/{sum(1 for r in faults if by[r['id']]['segment'])}",
            "innocent_alerted": [r["id"] for r in rows if r["kind"] == "innocent" and r["false_alert"]],
            "data_issues": {r["id"]: ("seen" if r.get("data_issue_seen") else "missed") + (", alerted" if r["false_alert"] else "")
                            for r in rows if r["kind"] == "data"},
            "false_alarms": [i.id for i in incs if i.id not in owner],
            "incidents": len(incs), "scenarios": rows}


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default="2026-11-08 00:00")
    ap.add_argument("--end", default="2026-11-21 23:00")
    ap.add_argument("--json", help="write the full scorecard here")
    a = ap.parse_args(argv)
    truth = json.loads((C.ROOT / "data" / "eye" / "injected.json").read_text())["scenarios"]
    C.STATE_DIR = Path(tempfile.mkdtemp(prefix="pulse_eval_"))   # never touch the demo state
    from pulse.orchestrator import tick
    from pulse.world import world
    w, nonok = world(), {}
    for now in pd.date_range(a.start, a.end, freq="h"):
        for k in tick(now, w=w).kpis:
            nonok.setdefault(k.severity, set()).add(str(now))
    incs = C.load_list(C.Incident, "incidents.json")
    interps = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (C.STATE_DIR / "interpretations").glob("*.json")
               if not p.name.endswith(".partial.json")}
    card = score(incs, interps, nonok, truth)
    for k, v in card.items():
        if k != "scenarios":
            print(f"{k:18s} {v}")
    for r in card["scenarios"]:
        if r["kind"] == "fault":
            print(f"  {r['id']:20s} detected={r['detected']!s:5} ttd={r['hours_to_detect']} cause={r['cause_named']} "
                  f"segment={r['segment_named']} | {r['headline']}")
    if a.json:
        Path(a.json).write_text(json.dumps(card, indent=1, default=str), encoding="utf-8")
    print(f"state: {C.STATE_DIR}")
    return card


if __name__ == "__main__":
    main()
