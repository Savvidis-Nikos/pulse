"""WHY / TEST: onset detection (ruptures), timing alignment with changes, difference-in-differences against
untouched control segments, and a counterfactual on treated vs untreated items."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from argus.analytics.baseline import band
from argus.semantic.engine import MetricEngine


def onset(engine: MetricEngine, metric: str, segment: dict | None, end, hours: int = 48) -> dict:
    """Most likely onset of the shift in the last `hours` (relative residual vs expected, 1 breakpoint)."""
    b = band(engine, metric, segment, end, hours)
    if b.empty:
        return {"onset": None}
    scale = b["expected"].abs()
    pos = scale[scale > 1e-9]
    floor = 0.1 * float(pos.median()) if len(pos) else float(b["actual"].abs().max() or 1.0)
    r = ((b["actual"] - b["expected"]) / scale.clip(lower=max(floor, 1e-9))).fillna(0.0)
    x = r.to_numpy().reshape(-1, 1)
    if len(x) < 8:
        return {"onset": None}
    try:
        import ruptures as rpt
        bk = rpt.Dynp(model="l2", min_size=2, jump=1).fit(x).predict(n_bkps=1)[0]
    except Exception:
        # fall back: maximal difference of means
        best, bk = -1, len(x) // 2
        for i in range(2, len(x) - 2):
            d = abs(x[i:].mean() - x[:i].mean())
            if d > best:
                best, bk = d, i
    if bk >= len(x):
        return {"onset": None}
    # min_size=2 pulls a shift that lives only in the last hour one hour early: move the break forward while the
    # boundary point still looks like the "before" regime
    v = r.to_numpy()
    while bk < len(v) - 1:
        before, after = v[:bk].mean(), v[bk + 1:].mean()
        if abs(v[bk] - before) < abs(v[bk] - after):
            bk += 1
        else:
            break
    before, after = float(r.iloc[:bk].mean()), float(r.iloc[bk:].mean())
    # gradual drifts: the change point marks the steepest part; walk back to the first sustained departure
    brk = bk
    head = v[: max(6, bk // 2)]
    noise = 1.4826 * float(np.median(np.abs(head - np.median(head)))) if len(head) else 0.0
    thr = max(3 * noise, 0.05)
    sgn = 1.0 if after >= before else -1.0
    dep = bk
    while dep - 1 >= 0 and sgn * v[dep - 1] > thr:
        dep -= 1
    return {"onset": f"{r.index[dep]:%Y-%m-%d %H:%M}", "break": f"{r.index[brk]:%Y-%m-%d %H:%M}",
            "shift_before": round(before, 4), "shift_after": round(after, 4), "jump": round(after - before, 4),
            "gradual": dep < brk - 1}


def align(onset_ts: str | None, event_ts: str | None, tol_h: float = 2.5) -> dict:
    if not onset_ts or not event_ts:
        return {"aligned": None}
    lag = (pd.Timestamp(onset_ts) - pd.Timestamp(event_ts)) / pd.Timedelta(hours=1)
    return {"aligned": -1.5 <= lag <= tol_h, "lag_h": round(lag, 1)}


def did(engine: MetricEngine, metric: str, treated: dict, control: dict | list[dict], event, end,
        pre_h: int = 24, n_boot: int = 400, seed: int = 7) -> dict:
    """Difference-in-differences of the hourly metric: treated vs control segment around `event`."""
    event, end = pd.Timestamp(event).floor("h"), pd.Timestamp(end)
    start = event - pd.Timedelta(hours=pre_h)

    def series(seg):
        df = engine.hourly(metric, (), seg, start, end).set_index("ts")
        return df["value"]
    t = series(treated)
    controls = control if isinstance(control, list) else [control]
    c = pd.concat([series(s) for s in controls], axis=1).mean(axis=1)
    d = pd.concat([t.rename("t"), c.rename("c")], axis=1).dropna()
    if len(d) < 6:
        return {"did": None, "note": "not enough hours"}
    # compare like with like: same hours of day before and after (use residual vs control)
    d["gap"] = d["t"] - d["c"]
    pre, post = d[d.index < event]["gap"], d[d.index >= event]["gap"]
    if len(pre) < 3 or len(post) < 2:
        return {"did": None, "note": "not enough pre/post hours"}
    est = float(post.mean() - pre.mean())
    rng = np.random.default_rng(seed)
    boots = [rng.choice(post.to_numpy(), len(post)).mean() - rng.choice(pre.to_numpy(), len(pre)).mean()
             for _ in range(n_boot)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    base = float(d[d.index < event]["t"].mean())
    return {"did": round(est, 6), "ci95": [round(float(lo), 6), round(float(hi), 6)],
            "significant": bool(lo > 0 or hi < 0), "relative": round(est / abs(base), 4) if base else None,
            "treated": treated, "control": controls, "pre_hours": len(pre), "post_hours": len(post)}


def complement_segments(engine: MetricEngine, metric: str, treated: dict) -> list[dict]:
    """Control segments: siblings that differ from the treated segment in exactly one dimension."""
    out = []
    for dim, val in treated.items():
        try:
            vals = engine.hourly(metric, (dim,)).iloc[-500:][dim].astype(str).unique()
        except KeyError:
            continue
        for v in vals:
            if v != str(val):
                out.append({**treated, dim: v})
    return out[:8]


def finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
