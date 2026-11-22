"""Expected behaviour of a metric: robust seasonal baseline, vectorised over segments.

For a window of `window_h` hours ending at t, the expectation is the median of the same window on each of the
previous 7 days (ratio of sums per day), weekday-adjusted for flow metrics. The spread is 1.4826 x MAD of those
7 values, floored by sampling noise (Poisson for counts, binomial for proportions) and a relative floor, so a
quiet week cannot make a tiny wobble look significant. Days marked untrusted are left out.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from argus.semantic.engine import MetricEngine

LAG_DAYS = 7
MIN_LAGS = 4
RES_WINDOW = 168      # hours of residuals used for the empirical noise scale
RES_MIN = 36
TYPICAL_PRICE = 80.0


def _wide(df: pd.DataFrame, by: tuple, col: str, idx: pd.DatetimeIndex) -> pd.DataFrame:
    if not by:
        return df.set_index("ts")[[col]].rename(columns={col: "_total"}).reindex(idx)
    w = df.pivot_table(index="ts", columns=list(by), values=col, aggfunc="sum", observed=True)
    if len(by) == 1:
        w.columns = [(c,) for c in w.columns]
    else:
        w.columns = [tuple(c) for c in w.columns]
    return w.reindex(idx)


def weekday_factors(engine: MetricEngine, metric: str) -> dict[int, float]:
    """Relative level of each weekday for flow metrics (median daily total per weekday / overall median)."""
    spec = engine.reg[metric]
    sh = engine.shape(metric)
    if not (sh.additive and spec.time_agg == "sum"):
        return {}
    cache = engine.__dict__.setdefault("_wf_cache", {})
    key = (metric, engine.wh.now.normalize())
    if key in cache:
        return cache[key]
    s = engine.hourly(metric).set_index("ts")["value"]
    today = engine.wh.now.normalize()
    s = s[s.index < today]
    daily = s.groupby(s.index.normalize()).agg(["sum", "count"])
    daily = daily[daily["count"] == 24]["sum"]
    out: dict[int, float] = {}
    if len(daily) >= 7 and daily.median() > 0:
        med = daily.median()
        by_dow = daily.groupby(daily.index.day_of_week).median() / med
        out = {int(k): float(v) for k, v in by_dow.items() if np.isfinite(v) and v > 0}
    cache[key] = out
    return out


def window_frame(engine: MetricEngine, metric: str, by: tuple, end: pd.Timestamp, window_h: int,
                 hours: int, upto=None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    """Window value W[t, segment] and volume V[t, segment] for t in the last `hours` hours up to `end`."""
    spec = engine.reg[metric]
    sh = engine.shape(metric)
    start = end - pd.Timedelta(hours=hours + window_h)
    idx = pd.date_range(start, end, freq="h")
    df = engine.hourly(metric, by, None, start, end, upto=upto)
    if df.empty:
        empty = pd.DataFrame(index=idx)
        return empty, empty, idx
    if spec.time_agg == "last":
        if sh.ratio_like:
            W = _wide(df, by, "num", idx) / _wide(df, by, "den", idx).replace(0, np.nan)
            V = _wide(df, by, "den", idx)
        else:
            W = _wide(df, by, "value", idx)
            V = W.abs()
        return W, V, idx
    if sh.ratio_like:
        N = _wide(df, by, "num", idx).rolling(window_h, min_periods=window_h).sum()
        D = _wide(df, by, "den", idx).rolling(window_h, min_periods=window_h).sum()
        W = N / D.replace(0, np.nan)
        V = D
    elif sh.additive and spec.time_agg == "sum":
        W = _wide(df, by, "value", idx).rolling(window_h, min_periods=window_h).sum()
        V = W.abs()
    else:
        W = _wide(df, by, "value", idx).rolling(window_h, min_periods=window_h).mean()
        V = pd.DataFrame(np.inf, index=W.index, columns=W.columns)
    return W, V, idx


def expected_frame(engine: MetricEngine, metric: str, W: pd.DataFrame, V: pd.DataFrame,
                   exclude_days: set | None = None) -> dict[str, pd.DataFrame]:
    """Expected value, sigma, z and relative change for every row of W that has 7 days of history."""
    spec = engine.reg[metric]
    sh = engine.shape(metric)
    wf = weekday_factors(engine, metric)
    lags = []
    idx = W.index
    for k in range(1, LAG_DAYS + 1):
        L = W.shift(24 * k)
        if wf:
            f_now = np.array([wf.get(d, 1.0) for d in idx.day_of_week])
            f_then = np.array([wf.get(d, 1.0) for d in (idx - pd.Timedelta(days=k)).day_of_week])
            L = L.mul(f_now / f_then, axis=0)
        if exclude_days:
            bad = np.isin((idx - pd.Timedelta(days=k)).normalize(), list(exclude_days))
            L.loc[bad] = np.nan
        lags.append(L.to_numpy(dtype=float))
    if not lags or W.empty:
        return {}
    stack = np.stack(lags)                                # (7, T, S)
    n_ok = np.sum(np.isfinite(stack), axis=0)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        exp = np.nanmedian(stack, axis=0)
        mad = np.nanmedian(np.abs(stack - exp[None]), axis=0) * 1.4826
    exp[n_ok < MIN_LAGS] = np.nan
    act = W.to_numpy(dtype=float)
    vol = V.to_numpy(dtype=float)
    floor = _noise_floor(spec, sh, exp, vol, _den_unit(engine, metric))
    # empirical noise of this segment: robust scale of its relative residuals over the previous week
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        rel_res = pd.DataFrame(np.abs(act - exp) / np.abs(exp), index=W.index, columns=W.columns)
        rel_res = rel_res.replace([np.inf, -np.inf], np.nan)
        scale = rel_res.rolling(RES_WINDOW, min_periods=RES_MIN).median().shift(1).to_numpy() * 1.4826
        res_sigma = np.nan_to_num(scale * np.abs(exp), nan=0.0)
    sigma = np.fmax(np.fmax(np.nan_to_num(mad, nan=0.0), floor), res_sigma)
    with np.errstate(all="ignore"):
        z = (act - exp) / sigma
        rel = np.where(np.abs(exp) > 1e-12, (act - exp) / np.abs(exp), np.where(np.abs(act) > 1e-12, np.sign(act) * np.inf, 0.0))
    mk = lambda a: pd.DataFrame(a, index=W.index, columns=W.columns)  # noqa: E731
    return {"actual": W, "expected": mk(exp), "sigma": mk(sigma), "z": mk(z), "rel": mk(rel), "volume": V,
            "lags": stack}


def _den_unit(engine: MetricEngine, metric: str) -> str:
    spec = engine.reg[metric]
    if spec.type == "ratio":
        return engine.reg[spec.denominator].unit
    if spec.type == "derived":
        sh = engine.shape(metric)
        if sh.den is not None:
            units = {engine.reg[str(s)].unit for s in sh.den.free_symbols if str(s) in engine.reg}
            return units.pop() if len(units) == 1 else "count"
    return "count"


def _noise_floor(spec, sh, exp: np.ndarray, vol: np.ndarray, den_unit: str = "count") -> np.ndarray:
    a = np.abs(exp)
    with np.errstate(all="ignore"):
        d = np.where(vol > 0, vol, np.nan)
        if sh.ratio_like and den_unit == "EUR":
            # money-weighted ratios: the effective sample is the number of purchases, not euros
            n = d / TYPICAL_PRICE
            return np.fmax(np.nan_to_num(a / np.sqrt(n), nan=0.0), 0.02 * a)
        if sh.ratio_like and spec.unit == "ratio":
            p = np.clip(exp, 1e-6, None)
            samp = np.where(p < 1, np.sqrt(np.clip(p * (1 - p), 0, None) / d), np.sqrt(p / d))
            return np.fmax(np.nan_to_num(samp, nan=0.0), 0.015 * a)
        if sh.ratio_like:
            # per-unit values (aov, cpc, roas, sentiment): coefficient of variation ~1 per unit
            samp = a / np.sqrt(d)
            return np.fmax(np.nan_to_num(samp, nan=0.0), 0.02 * a)
        if sh.additive and spec.unit == "EUR":
            return np.fmax(np.sqrt(a * TYPICAL_PRICE), 0.03 * a)
        if sh.additive and spec.time_agg == "sum":
            return np.fmax(np.sqrt(np.fmax(a, 1.0)), 0.02 * a)
        return np.fmax(0.05 * a, 1e-9)


def scan_rows(engine: MetricEngine, metric: str, by: tuple, now: pd.Timestamp, window_h: int | None = None,
              exclude_days: set | None = None, extra_hours: int = 1) -> pd.DataFrame:
    """One row per segment with the window statistics at `now` and at the previous hour(s)."""
    spec = engine.reg[metric]
    window_h = window_h or spec.window_h
    hours = 24 * LAG_DAYS + extra_hours + 1
    W, V, idx = window_frame(engine, metric, by, now, window_h, hours)
    if W.empty or now not in W.index:
        return pd.DataFrame()
    E = expected_frame(engine, metric, W, V, exclude_days)
    if not E:
        return pd.DataFrame()
    rows = []
    prev = now - pd.Timedelta(hours=1)
    for col in W.columns:
        a = E["actual"].at[now, col]
        if not np.isfinite(a) and not np.isfinite(E["expected"].at[now, col]):
            continue
        row = {"metric": metric, "by": by, "segment": dict(zip(by, col)) if by else {},
               "actual": a, "expected": E["expected"].at[now, col], "sigma": E["sigma"].at[now, col],
               "z": E["z"].at[now, col], "rel": E["rel"].at[now, col], "volume": E["volume"].at[now, col]}
        if prev in W.index:
            row["z_prev"] = E["z"].at[prev, col]
            row["rel_prev"] = E["rel"].at[prev, col]
        rows.append(row)
    return pd.DataFrame(rows)


def compare(engine: MetricEngine, metric: str, segment: dict | None, start, end,
            exclude_days: set | None = None) -> dict:
    """Actual over [start, end] vs the same hours on the previous 7 days (weekday-adjusted median)."""
    start, end = pd.Timestamp(start).floor("h"), pd.Timestamp(end).floor("h")
    hours = int((end - start) / pd.Timedelta(hours=1)) + 1
    spec = engine.reg[metric]
    actual = engine.window(metric, start, end, segment)
    wf = weekday_factors(engine, metric)
    vals = []
    for k in range(1, LAG_DAYS + 1):
        s, e = start - pd.Timedelta(days=k), end - pd.Timedelta(days=k)
        if exclude_days and s.normalize() in exclude_days:
            continue
        v = engine.window(metric, s, e, segment)
        if wf and np.isfinite(v):
            v *= wf.get(start.day_of_week, 1.0) / wf.get(s.day_of_week, 1.0)
        if np.isfinite(v):
            vals.append(float(v))
    exp = float(np.median(vals)) if len(vals) >= 3 else float("nan")
    mad = float(np.median(np.abs(np.array(vals) - exp)) * 1.4826) if vals else float("nan")
    sh = engine.shape(metric)
    vol = np.array([[engine.window(metric, start, end, segment) if not sh.ratio_like else
                     _den(engine, metric, segment, start, end)]])
    floor = (float(_noise_floor(spec, sh, np.array([[exp]]), vol, _den_unit(engine, metric))[0, 0])
             if np.isfinite(exp) else float("nan"))
    sigma = max(mad if np.isfinite(mad) else 0.0, floor if np.isfinite(floor) else 0.0) or float("nan")
    z = (actual - exp) / sigma if np.isfinite(sigma) and sigma > 0 else float("nan")
    rel = (actual - exp) / abs(exp) if exp else float("nan")
    return {"metric": metric, "segment": segment or {}, "start": f"{start:%Y-%m-%d %H:%M}",
            "end": f"{end:%Y-%m-%d %H:%M}", "hours": hours, "actual": actual, "expected": exp, "z": z, "rel": rel,
            "history": vals, "unit": spec.unit}


def _den(engine: MetricEngine, metric: str, segment, start, end) -> float:
    df = engine.hourly(metric, (), segment, start, end)
    return float(df["den"].sum()) if "den" in df and df["den"].notna().any() else float("nan")


def band(engine: MetricEngine, metric: str, segment: dict | None, end, hours: int = 72,
         exclude_days: set | None = None) -> pd.DataFrame:
    """Hourly actual with expected value and a +/- 3 sigma band (window 1 h), for charts."""
    seg = segment or {}
    by = tuple(seg.keys())
    end = pd.Timestamp(end)
    W, V, idx = window_frame(engine, metric, by, end, 1, hours + 24 * LAG_DAYS)
    if W.empty:
        return pd.DataFrame()
    E = expected_frame(engine, metric, W, V, exclude_days)
    col = (tuple(seg.values()) if len(by) > 1 else (tuple(seg.values()) if by else "_total"))
    if col not in W.columns:
        return pd.DataFrame()
    out = pd.DataFrame({"actual": E["actual"][col], "expected": E["expected"][col], "sigma": E["sigma"][col]})
    out["lo"] = out["expected"] - 3 * out["sigma"]
    out["hi"] = out["expected"] + 3 * out["sigma"]
    return out[out.index > end - pd.Timedelta(hours=hours)]
