"""Persistent incident memory (<state>/memory.jsonl).

Every resolved incident is written back as a case (what moved, where, first cause, confidence, how long it lasted).
When a new incident is investigated, similar past cases are recalled and attached to it. A recalled case is a
hypothesis to test, never evidence: it is shown to the CEO and the agents, but no conclusion is drawn from it.
"""
from __future__ import annotations

import json

from pulse import contracts as C
from pulse.contracts import Incident, Interpretation

FILE = "memory.jsonl"


def _segment(path) -> dict:
    """'payment_success_rate|country=DE|platform=android' -> {'country': 'DE', 'platform': 'android'}"""
    seg = {}
    for p in path or []:
        for part in str(p).split("|")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                seg[k] = v
    return seg


def _nodes(path) -> set:
    return {str(p).split("|")[0] for p in path or []}


def cases() -> list[dict]:
    p = C.STATE_DIR / FILE
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def remember(inc: Incident, it: Interpretation | None, resolved_at: str) -> dict:
    """Write a resolved incident back as a case (idempotent per incident id)."""
    case = {"id": inc.id, "kpi": inc.kpi, "direction": inc.direction, "onset": inc.onset_ts or inc.opened_at,
            "resolved_at": resolved_at, "ticks": inc.ticks_seen,
            "headline": (it.headline if it else "") or inc.headline,
            "confidence": it.confidence if it else "unexplained",
            "first_cause": it.first_cause.node if it and it.first_cause else None,
            "path": list(it.path) if it else [], "segment": _segment(it.path if it else [])}
    if any(c["id"] == inc.id for c in cases()):
        return case
    p = C.STATE_DIR / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(case) + "\n")
    return case


def recall(kpi: str, direction: str | None = None, path=None, first_cause: str | None = None,
           before: str | None = None, exclude: str | None = None, k: int = 3) -> list[dict]:
    """Past cases similar to this incident, best first. Only cases resolved before `before` (no time travel)."""
    seg, nodes = _segment(path), _nodes(path)
    scored = []
    for c in cases():
        if c["id"] == exclude or (before and c["resolved_at"] >= before):
            continue
        s = 2.0 * (c["kpi"] == kpi) + 1.0 * (direction is not None and c.get("direction") == direction)
        s += 3.0 * (first_cause is not None and c.get("first_cause") == first_cause)
        other = _nodes(c.get("path"))
        if nodes and other:
            s += 2.0 * len(nodes & other) / len(nodes | other)
        s += sum(1.0 for key, v in seg.items() if (c.get("segment") or {}).get(key) == v)
        if s >= 3:                      # at least: same KPI moving the same way, or a shared cause/segment
            scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return [dict(c, score=round(s, 2)) for s, c in scored[:k]]


def describe(c: dict) -> str:
    return (f"{c['onset'][:16]}: {c['headline']} ({c['confidence']}, resolved after {c['ticks']} checks)")
