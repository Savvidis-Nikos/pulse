"""Anomaly decisions on baseline statistics: level shifts with persistence, plus slow drift (CUSUM)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from argus.semantic.registry import MetricSpec
from argus.settings_policy import policy

TIER_WEIGHT = {"north_star": 1.0, "outcome": 0.9, "driver": 0.75, "health": 0.6}


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def decide(row: dict, spec: MetricSpec, additive: bool, ratio_like: bool = False) -> str | None:
    """'anomaly', 'pending' (first window only) or None."""
    pol = policy()["detection"]
    z, rel, exp, act = row.get("z"), row.get("rel"), row.get("expected"), row.get("actual")
    if not (_finite(z) and _finite(exp)):
        return None
    vol = row.get("volume")
    if additive:
        vol = max(abs(float(act or 0)), abs(float(exp)))
    if spec.min_volume and _finite(vol) and float(vol) < spec.min_volume:
        return None
    ndims = len(row.get("segment") or {})
    if ratio_like and spec.unit == "ratio" and _finite(vol) and float(exp) < 1:
        # sparse numerators (a handful of orders) are too noisy to page on
        if max(float(exp), float(act or 0)) * float(vol) < pol.get("min_events", 15):
            return None
    z_min = pol["z_min"] + (1.0 if ndims >= 2 else 0.0)
    rel_min = pol["health_rel_min"] if spec.tier == "health" else pol["rel_min"]
    if not _finite(rel):
        rel = math.copysign(10.0, float(z))
    zp, zp2 = row.get("z_prev"), row.get("z_prev2")
    sustained = (pol.get("sustained_z") and _finite(zp) and _finite(zp2) and abs(rel) >= pol["sustained_rel"]
                 and abs(rel) >= rel_min and min(abs(z), abs(zp), abs(zp2)) >= pol["sustained_z"]
                 and np.sign(z) == np.sign(zp) == np.sign(zp2) and ndims <= 1)
    if (abs(z) < z_min or abs(rel) < rel_min) and not sustained:
        return None
    if spec.min_abs_change and abs(float(act or 0) - float(exp)) < spec.min_abs_change:
        return None
    persistent = _finite(zp) and np.sign(zp) == np.sign(z) and abs(zp) >= pol["z_prev_min"]
    if persistent or abs(z) >= pol["z_strong"] or sustained:
        return "anomaly"
    return "pending"


def is_bad(spec: MetricSpec, z: float) -> bool | None:
    if spec.polarity == "up_is_good":
        return z < 0
    if spec.polarity == "down_is_good":
        return z > 0
    return None


def score(spec: MetricSpec, z: float, impact_eur_h: float | None) -> float:
    s = min(abs(z), 30.0) / 30.0 * TIER_WEIGHT.get(spec.tier, 0.7)
    if impact_eur_h:
        s += min(abs(impact_eur_h) / 5000.0, 1.0) * 0.5
    return round(s, 4)


def cusum_drift(series: pd.Series, k: float = 0.5, h: float = 5.0) -> dict | None:
    """Two-sided CUSUM on standardised daily residuals; returns the alarm with its onset, if any."""
    s = series.dropna()
    if len(s) < 6:
        return None
    ref = s.iloc[: max(3, len(s) // 2)]
    mu, sd = ref.mean(), ref.std() or 1e-9
    zs = (s - mu) / sd
    hi = lo = 0.0
    onset_hi = onset_lo = None
    for t, v in zs.items():
        hi = max(0.0, hi + v - k)
        lo = min(0.0, lo + v + k)
        onset_hi = t if hi > 0 and onset_hi is None else (None if hi == 0 else onset_hi)
        onset_lo = t if lo < 0 and onset_lo is None else (None if lo == 0 else onset_lo)
        if hi > h:
            return {"direction": "up", "onset": str(onset_hi), "stat": round(hi, 2)}
        if -lo > h:
            return {"direction": "down", "onset": str(onset_lo), "stat": round(-lo, 2)}
    return None
