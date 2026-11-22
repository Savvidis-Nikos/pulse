"""Actual window vs pooled baseline (same hours on the previous 7 days), at any breakdown.

Pooled baselines (ratio of sums over the 7 baseline windows) compose exactly: telescoping products of pooled
rates equal the pooled ratio, so decompositions on these numbers add up to the total change.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from argus.analytics.baseline import weekday_factors
from argus.semantic.engine import MetricEngine

DAYS = 7


def _agg(df: pd.DataFrame, by: list, ratio_like: bool, time_agg: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=by + ["num", "den", "value"])
    if time_agg == "last":
        df = df[df["ts"] == df["ts"].max()]
    g = df.groupby(by, observed=True) if by else df.assign(_k=0).groupby("_k")
    if ratio_like:
        out = g[["num", "den"]].sum()
    else:
        out = g[["value"]].sum() if time_agg in ("sum", "last") else g.apply(
            lambda x: pd.Series({"value": x.groupby("ts")["value"].sum().mean()}), include_groups=False)
        out["num"] = out["value"]
        out["den"] = np.nan
    if ratio_like:
        out["value"] = out["num"] / out["den"].replace(0, np.nan)
    out = out.reset_index()
    return out.drop(columns=["_k"], errors="ignore")


def pair(engine: MetricEngine, metric: str, segment: dict | None, start, end, by=(),
         exclude_days: set | None = None) -> pd.DataFrame:
    """Rows per breakdown value: actual num/den/value (a_*) and pooled baseline (b_*)."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    spec = engine.reg[metric]
    sh = engine.shape(metric)
    by = list(by or [])
    act = _agg(engine.hourly(metric, by, segment, start, end), by, sh.ratio_like, spec.time_agg)
    wf = weekday_factors(engine, metric)
    parts = []
    used = 0
    for k in range(1, DAYS + 1):
        s, e = start - pd.Timedelta(days=k), end - pd.Timedelta(days=k)
        if exclude_days and s.normalize() in exclude_days:
            continue
        b = _agg(engine.hourly(metric, by, segment, s, e), by, sh.ratio_like, spec.time_agg)
        if b.empty:
            continue
        f = wf.get(start.day_of_week, 1.0) / wf.get(s.day_of_week, 1.0) if wf else 1.0
        b = b.copy()
        for c in ("num", "den", "value"):
            if c in b and not (c == "value" and sh.ratio_like):
                b[c] = b[c] * f
        parts.append(b)
        used += 1
    if parts:
        base = pd.concat(parts)
        if by:
            base = base.groupby(by, observed=True)[["num", "den", "value"]].sum(min_count=1).reset_index()
        else:
            base = base[["num", "den", "value"]].sum(min_count=1).to_frame().T
        base[["num", "den"]] = base[["num", "den"]] / used
        if sh.ratio_like:
            base["value"] = base["num"] / base["den"].replace(0, np.nan)
        else:
            base["value"] = base["value"] / used
    else:
        base = pd.DataFrame(columns=by + ["num", "den", "value"])
    act = act.rename(columns={"num": "a_num", "den": "a_den", "value": "a_value"})
    base = base.rename(columns={"num": "b_num", "den": "b_den", "value": "b_value"})
    if by:
        out = act.merge(base, on=by, how="outer")
    else:
        out = pd.concat([act.reset_index(drop=True), base.reset_index(drop=True)], axis=1)
    out.attrs["baseline_days"] = used
    return out


def totals(engine: MetricEngine, metric: str, segment: dict | None, start, end, exclude_days=None) -> dict:
    p = pair(engine, metric, segment, start, end, (), exclude_days)
    if p.empty:
        return {"actual": float("nan"), "baseline": float("nan"), "delta": float("nan"), "rel": float("nan")}
    a, b = float(p["a_value"].iloc[0]), float(p["b_value"].iloc[0])
    return {"actual": a, "baseline": b, "delta": a - b, "rel": (a - b) / abs(b) if b else float("nan"),
            "a_num": float(p["a_num"].iloc[0]), "b_num": float(p["b_num"].iloc[0]),
            "a_den": float(p["a_den"].iloc[0]) if "a_den" in p else float("nan"),
            "b_den": float(p["b_den"].iloc[0]) if "b_den" in p else float("nan")}
