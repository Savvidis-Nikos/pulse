"""Business context visible at `now`: change log, calendar, status feed, external market events.

Everything respects the clock: changes are visible from deployed_at, calendar entries from announced_at,
status posts and external events from their timestamp.
"""
from __future__ import annotations

import pandas as pd

from argus.data.warehouse import Warehouse


def _scope_ok(row, segment: dict) -> bool:
    """Does an event scoped to (country, platform, category) touch the segment?"""
    for dim in ("country", "platform", "category"):
        v = str(row.get(dim, "*")) if dim in row else "*"
        if v in ("*", "nan", "None", ""):
            continue
        if dim in segment and str(segment[dim]) != v:
            return False
    return True


def _fmt(ts) -> str | None:
    return None if pd.isna(ts) else f"{pd.Timestamp(ts):%Y-%m-%d %H:%M}"


def changes(wh: Warehouse, start, end, segment: dict | None = None, component: str | None = None) -> list[dict]:
    """Change-log entries deployed in [start - 1h, end] (and visible at now) that touch the segment."""
    ev = wh.view("events")
    if ev.empty:
        return []
    start, end = pd.Timestamp(start) - pd.Timedelta(hours=1), min(pd.Timestamp(end), wh.now + pd.Timedelta(hours=1))
    sub = ev[(ev["deployed_at"] >= start) & (ev["deployed_at"] <= end)]
    out = []
    for _, r in sub.iterrows():
        if segment and not _scope_ok(r, segment):
            continue
        if component and str(r["component"]) != component:
            continue
        out.append({"name": r["name"], "type": r["type"], "component": r["component"], "owner": r["owner"],
                    "deployed_at": _fmt(r["deployed_at"]), "first_exposure_at": _fmt(r.get("first_exposure_at")),
                    "scope": {d: str(r[d]) for d in ("country", "platform", "category")},
                    "planned": bool(r["planned"]) if not pd.isna(r["planned"]) else None})
    return out


def active_changes(wh: Warehouse, at, segment: dict | None = None, lookback_h: int = 72) -> list[dict]:
    """Changes deployed in the lookback window and not rolled back before `at`."""
    at = pd.Timestamp(at)
    items = changes(wh, at - pd.Timedelta(hours=lookback_h), at, segment)
    rolled = [c for c in items if c["type"] == "rollback"]
    return [c for c in items if c["type"] != "rollback" and not any(
        r["component"] == c["component"] and r["deployed_at"] > c["deployed_at"] and _same_scope(r, c) for r in rolled)]


def _same_scope(a: dict, b: dict) -> bool:
    return all(a["scope"].get(d) in ("*", b["scope"].get(d)) or b["scope"].get(d) == "*" for d in a["scope"])


def calendar(wh: Warehouse, start, end, segment: dict | None = None) -> list[dict]:
    """Announced calendar entries overlapping [start, end] that touch the segment's country."""
    cal = wh.view("calendar")
    if cal.empty:
        return []
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    sub = cal[(cal["start"] <= end) & (cal["end"] > start)]
    out = []
    for _, r in sub.iterrows():
        c = str(r["country"])
        if segment and "country" in segment and c not in ("*", str(segment["country"])):
            continue
        metrics = [m.strip() for m in str(r["metrics"]).split(";") if m.strip() and str(r["metrics"]) != "nan"]
        out.append({"name": r["name"], "kind": r["kind"], "country": c, "start": _fmt(r["start"]),
                    "end": _fmt(r["end"]), "expected_impact": r["expected_impact"], "metrics": metrics,
                    "expected_low": None if pd.isna(r["expected_low"]) else float(r["expected_low"]),
                    "expected_high": None if pd.isna(r["expected_high"]) else float(r["expected_high"])})
    return out


def status_posts(wh: Warehouse, start, end, component: str | None = None) -> list[dict]:
    sf = wh.view("status_feed")
    if sf.empty:
        return []
    sub = sf[(sf["ts"] >= pd.Timestamp(start) - pd.Timedelta(hours=2)) & (sf["ts"] <= pd.Timestamp(end))]
    out = []
    for _, r in sub.iterrows():
        if component and component.lower() not in str(r["component"]).lower():
            continue
        out.append({"ts": _fmt(r["ts"]), "source": r["source"], "channel": r["channel"], "component": r["component"],
                    "region": r["region"], "severity": r["severity"], "state": r["state"], "title": r["title"]})
    return out


def external(wh: Warehouse, start, end, segment: dict | None = None) -> list[dict]:
    ex = wh.view("external_events")
    if ex.empty:
        return []
    sub = ex[(ex["start"] <= pd.Timestamp(end)) & (ex["end"] >= pd.Timestamp(start))]
    out = []
    for _, r in sub.iterrows():
        if segment and not _scope_ok(r, segment):
            continue
        out.append({"ts": _fmt(r["ts"]), "name": r["name"], "kind": r["kind"], "competitor": r["competitor"],
                    "country": r["country"], "category": r["category"], "discount": float(r["discount"]),
                    "start": _fmt(r["start"]), "end": _fmt(r["end"]), "source": r["source"]})
    return out


def everything(wh: Warehouse, start, end, segment: dict | None = None) -> dict:
    return {"changes": changes(wh, start, end, segment), "calendar": calendar(wh, start, end, segment),
            "status_feed": status_posts(wh, start, end), "external": external(wh, start, end, segment)}
