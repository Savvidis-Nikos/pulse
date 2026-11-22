"""Data trust gate: is each source's latest data complete, fresh and alive? Looks backwards only.

Verdicts per source: ok | late (partial rows) | stale (lag / last update too old) | frozen (values stopped
moving) | silent (no rows this hour) | down (heartbeat failed). Metrics that read an untrusted source are held
back from anomaly scoring, the source gets a DATA notice, and the affected days leave the baselines.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from argus.data.warehouse import Warehouse
from argus.settings_policy import policy


@dataclass
class Verdict:
    source: str
    status: str = "ok"
    reason: str = ""
    since: str | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return asdict(self)


def source_tables(wh: Warehouse) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, spec in wh.specs.items():
        if spec.kind != "hourly" or not wh.has(name):
            continue
        for src in {spec.source, *spec.column_sources.keys()} - {None, "meta"}:
            out.setdefault(src, []).append(name)
    return out


def _hour_status(wh: Warehouse, src: str, tables: list[str], t: pd.Timestamp) -> Verdict:
    pol = policy()["trust"]
    v = Verdict(src)
    ph = wh.frames.get("pipeline_health")
    if ph is not None and src in set(ph["source"]):
        rows = ph[(ph["source"] == src) & (ph["ts"] == t)]
        if rows.empty:
            v.status, v.reason = "silent", "no pipeline heartbeat row this hour"
            return v
        r = rows.iloc[0]
        v.evidence = {"completeness": float(r["completeness"]), "lag_minutes": float(r["lag_minutes"]),
                      "last_update": str(r["last_update"])[:16], "heartbeat_ok": bool(r["heartbeat_ok"])}
        if not bool(r["heartbeat_ok"]):
            v.status, v.reason = "down", "pipeline heartbeat failed"
            return v
        if float(r["completeness"]) < pol["completeness_min"]:
            v.status = "late"
            v.reason = f"only {float(r['completeness']):.0%} of expected rows have arrived"
            return v
        if float(r["lag_minutes"]) > pol["lag_max_minutes"]:
            v.status = "stale"
            v.reason = f"data is {float(r['lag_minutes']):.0f} min behind (last update {str(r['last_update'])[:16]})"
            return v
    for tname in tables:
        df = wh.frames[tname]
        if not (df["ts"] == t).any():
            if (df["ts"] < t).any():
                v.status, v.reason = "silent", f"no {tname} rows for {t:%H:%M}"
                return v
    return v


def _frozen(wh: Warehouse, src: str, tables: list[str], t: pd.Timestamp) -> Verdict | None:
    """Values that normally move every hour stopped moving (same snapshot re-sent)."""
    k = policy()["trust"]["frozen_hours"]
    for tname in tables:
        spec = wh.specs[tname]
        if spec.source != src:
            continue
        df = wh.frames[tname]
        lo = t - pd.Timedelta(hours=72)
        sub = df[(df["ts"] > lo) & (df["ts"] <= t)]
        num = sub.select_dtypes("number")
        if num.empty:
            continue
        tot = num.groupby(sub["ts"]).sum()
        if len(tot) < 24 or tot.index.max() != t:
            continue
        moving = (tot.diff().abs() > 0)
        cols = [c for c in tot.columns if moving[c].iloc[1:-k].mean() > 0.9]
        if not cols:
            continue
        recent = moving[cols].iloc[-(k - 1):]
        if len(recent) == k - 1 and not recent.to_numpy().any():
            return Verdict(src, "frozen", f"{tname} values identical for {k} hours ({', '.join(cols[:3])})",
                           evidence={"table": tname, "columns": cols[:5]})
    return None


def assess(wh: Warehouse, now: pd.Timestamp | None = None) -> dict[str, Verdict]:
    now = pd.Timestamp(now or wh.now)
    out = {}
    for src, tables in source_tables(wh).items():
        v = _hour_status(wh, src, tables, now)
        if v.ok:
            v = _frozen(wh, src, tables, now) or v
        if not v.ok:
            since = now
            for h in range(1, 48):
                t = now - pd.Timedelta(hours=h)
                pv = _hour_status(wh, src, tables, t)
                if pv.ok and v.status == "frozen":
                    pv = _frozen(wh, src, tables, t) or pv
                if pv.ok:
                    break
                since = t
            if v.status == "frozen":
                since = since - pd.Timedelta(hours=policy()["trust"]["frozen_hours"] - 1)
            v.since = f"{since:%Y-%m-%d %H:%M}"
        out[src] = v
    return out


def bad_days(wh: Warehouse, now: pd.Timestamp, sources: set[str] | None = None) -> dict[str, set]:
    """Days (before today) on which each source had partial / late data, to keep them out of baselines."""
    cache = wh.__dict__.setdefault("_bad_days", {})
    key = pd.Timestamp(now).normalize()
    if key not in cache:
        res: dict[str, set] = {}
        ph = wh.frames.get("pipeline_health")
        if ph is not None:
            pol = policy()["trust"]
            sub = ph[ph["ts"] < key]
            bad = sub[(sub["completeness"] < pol["completeness_min"]) | (~sub["heartbeat_ok"].astype(bool))]
            for src, g in bad.groupby("source"):
                res[str(src)] = set(pd.to_datetime(g["ts"]).dt.normalize())
        cache[key] = res
    res = cache[key]
    return {s: d for s, d in res.items() if sources is None or s in sources}


def summary(verdicts: dict[str, Verdict]) -> list[dict]:
    return [v.to_dict() for v in verdicts.values() if not v.ok]


def np_float(x) -> float:
    return float(x) if x is not None and np.isfinite(x) else float("nan")
