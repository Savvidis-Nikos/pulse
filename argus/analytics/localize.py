"""WHERE did it happen? Adtributor-style localisation (explanatory power + surprise), recursive to 2 levels,
with Kitagawa mix/rate contributions for ratio metrics and an optional wise-pizza cross-check."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from argus.analytics.windows import pair
from argus.semantic.engine import MetricEngine

EP_SET = 0.67       # explanatory set must explain >= 67 % of the change
MAX_SET = 2         # ... with at most 2 values to count as concentrated


def _js(p: float, q: float) -> float:
    p, q = max(p, 1e-12), max(q, 1e-12)
    m = (p + q) / 2
    return 0.5 * (p * math.log(p / m) + q * math.log(q / m))


def contributions(engine: MetricEngine, metric: str, segment: dict | None, start, end, dim: str,
                  exclude_days=None) -> pd.DataFrame:
    """Per value of `dim`: actual, baseline, contribution to the total change, mix / rate split, surprise."""
    sh = engine.shape(metric)
    p = pair(engine, metric, segment, start, end, (dim,), exclude_days)
    if p.empty:
        return p
    p = p.fillna({"a_num": 0.0, "b_num": 0.0, "a_den": 0.0, "b_den": 0.0})
    if sh.ratio_like:
        A_den, B_den = p["a_den"].sum(), p["b_den"].sum()
        w1 = p["a_den"] / A_den if A_den else 0 * p["a_den"]
        w0 = p["b_den"] / B_den if B_den else 0 * p["b_den"]
        r1 = (p["a_num"] / p["a_den"].replace(0, np.nan)).fillna(0.0)
        r0 = (p["b_num"] / p["b_den"].replace(0, np.nan)).fillna(0.0)
        p["mix"] = (w1 - w0) * (r1 + r0) / 2
        p["rate"] = (r1 - r0) * (w1 + w0) / 2
        p["contribution"] = p["mix"] + p["rate"]
        total = p["contribution"].sum()
        pa = p["a_num"] / max(p["a_num"].sum(), 1e-12)
        pb = p["b_num"] / max(p["b_num"].sum(), 1e-12)
        p["actual"], p["baseline"], p["share_now"], p["share_before"] = r1, r0, w1, w0
    else:
        a = p["a_value"].fillna(0.0) if "a_value" in p else p["a_num"]
        b = p["b_value"].fillna(0.0) if "b_value" in p else p["b_num"]
        p["contribution"] = a - b
        p["mix"], p["rate"] = np.nan, np.nan
        total = p["contribution"].sum()
        pa = a / max(a.sum(), 1e-12)
        pb = b / max(b.sum(), 1e-12)
        p["actual"], p["baseline"] = a, b
    p["ep"] = p["contribution"] / total if abs(total) > 1e-12 else 0.0
    p["surprise"] = [_js(x, y) for x, y in zip(pb, pa)]
    p["rel"] = (p["actual"] - p["baseline"]) / p["baseline"].abs().replace(0, np.nan)
    p = p.rename(columns={dim: "value_of"})
    p.insert(0, "dim", dim)
    p.attrs["total_change"] = float(total)
    return p.sort_values("ep", ascending=False)


def localize(engine: MetricEngine, metric: str, segment: dict | None, start, end, dims: list[str] | None = None,
             depth: int = 2, exclude_days=None) -> dict:
    """Find the narrowest segment that explains the change. Returns the ranked dimensions and the focus."""
    segment = dict(segment or {})
    dims = [d for d in (dims or engine.dims(metric)) if d not in segment]
    ranked = []
    for d in dims:
        try:
            c = contributions(engine, metric, segment, start, end, d, exclude_days)
        except KeyError:
            continue
        if c.empty or len(c) > 12:
            continue
        vol = (c["b_den"] if "b_den" in c and c["b_den"].abs().sum() > 0 else c["baseline"]).abs()
        if (vol >= 0.05 * max(vol.sum(), 1e-12)).sum() < 2:
            continue
        total = c.attrs.get("total_change", 0.0)
        if abs(total) < 1e-12:
            continue
        c = c.sort_values("ep", ascending=False)
        chosen, ep = [], 0.0
        for _, r in c.iterrows():
            if r["ep"] <= 0:
                continue
            chosen.append(r)
            ep += r["ep"]
            if ep >= EP_SET:
                break
        surprise = float(sum(r["surprise"] for r in chosen))
        concentrated = ep >= EP_SET and len(chosen) <= MAX_SET and len(chosen) < len(c)
        ranked.append({"dim": d, "values": [str(r["value_of"]) for r in chosen], "explained": round(ep, 3),
                       "surprise": round(surprise, 5), "concentrated": bool(concentrated),
                       "table": [{"value": str(r["value_of"]), "actual": _r(r["actual"]), "baseline": _r(r["baseline"]),
                                  "rel": _r(r["rel"]), "share_of_change": _r(r["ep"]),
                                  **({"mix": _r(r["mix"]), "rate": _r(r["rate"])} if not pd.isna(r["mix"]) else {})}
                                 for _, r in c.sort_values("ep", ascending=False).head(8).iterrows()]})
    ranked.sort(key=lambda x: (not x["concentrated"], -x["surprise"]))
    focus = dict(segment)
    path = []
    if ranked and ranked[0]["concentrated"] and len(ranked[0]["values"]) == 1:
        top = ranked[0]
        focus[top["dim"]] = top["values"][0]
        path.append({top["dim"]: top["values"][0], "explained": top["explained"]})
        if depth > 1:
            sub = localize(engine, metric, focus, start, end, [d for d in dims if d != top["dim"]], depth - 1,
                           exclude_days)
            if sub["focus"] != focus and sub["path"]:
                focus = sub["focus"]
                path += sub["path"]
    return {"metric": metric, "segment": segment, "focus": focus, "path": path, "broad": not path,
            "dimensions": ranked[:6]}


def wise_pizza_segments(engine: MetricEngine, metric: str, segment: dict | None, start, end, dims: list[str],
                        max_segments: int = 3) -> list[dict] | None:
    """Cross-check with wise-pizza (multi-dimension segments explaining the change in totals)."""
    try:
        from wise_pizza import explain_changes_in_totals
    except Exception:
        return None
    sh = engine.shape(metric)
    try:
        a = engine.hourly(metric, dims, segment, start, end)
        frames = []
        for k in range(1, 8):
            s, e = pd.Timestamp(start) - pd.Timedelta(days=k), pd.Timestamp(end) - pd.Timedelta(days=k)
            frames.append(engine.hourly(metric, dims, segment, s, e))
        b = pd.concat(frames)
        def prep(df, n):
            g = df.groupby(dims, observed=True)[["num", "den"] if sh.ratio_like else ["value"]].sum().reset_index()
            if sh.ratio_like:
                g = g.rename(columns={"num": "total", "den": "size"})
            else:
                g = g.rename(columns={"value": "total"})
                g["size"] = 1.0
            g[["total", "size"]] = g[["total", "size"]] / n
            for d in dims:
                g[d] = g[d].astype(str)
            return g
        df1, df2 = prep(b, 7), prep(a, 1)
        sf = explain_changes_in_totals(df1, df2, dims=dims, total_name="total", size_name="size",
                                       max_segments=max_segments, min_depth=1, max_depth=2,
                                       how="split_fits" if sh.ratio_like else "totals", verbose=0)
        out = []
        for seg in getattr(sf, "segments", [])[:max_segments]:
            out.append({"segment": {k: str(v) for k, v in seg.get("segment", {}).items()},
                        "impact": _r(seg.get("impact")), "naive_avg": _r(seg.get("naive_avg"))})
        return out
    except Exception as exc:  # wise-pizza is a cross-check only
        return [{"error": str(exc)[:200]}]


def _r(x, n=5):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(x) else round(x, n)
