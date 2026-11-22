"""WHAT moved? Exact decomposition of a metric's change along its graph formula.

product chains   log-change contributions: dln M = sum dln f_i  (share_i = dln f_i / dln M)
sums             shift-share: dM = sum c_i dX_i
ratios           dln (N/D) = dln N - dln D
shares           dR = sum c_i d(share_i)   (e.g. margin_rate = 1 - discount_share - cogs_rate - ad_cost_share)
All terms use the same pooled baseline, so the parts add up to the total (residual reported).
"""
from __future__ import annotations

import math

from argus.analytics.windows import totals
from argus.semantic.graph import MetricGraph


def decompose(graph: MetricGraph, metric: str, segment: dict | None, start, end, exclude_days=None,
              decomposition: str | None = None) -> dict:
    eng = graph.engine
    d = None
    if decomposition:
        d = next((x for x in graph.decomps.get(metric, []) if x.id == decomposition), None)
    d = d or graph.canonical(metric)
    tot = totals(eng, metric, segment, start, end, exclude_days)
    out = {"metric": metric, "segment": segment or {}, "actual": tot["actual"], "baseline": tot["baseline"],
           "rel": tot["rel"], "formula": d.formula() if d else None, "kind": d.kind if d else "base", "parts": []}
    if d is None or d.kind == "ratio_expr":
        return out
    parts = []
    if d.kind in ("product", "ratio"):
        lnM = math.log(tot["actual"] / tot["baseline"]) if tot["actual"] > 0 and tot["baseline"] > 0 else float("nan")
        acc = 0.0
        for p, c in d.parts:
            if not eng.supports(p, list((segment or {}).keys())):
                return out | {"note": f"{p} cannot be broken down by the segment"}
            t = totals(eng, p, segment, start, end, exclude_days)
            ln = math.log(t["actual"] / t["baseline"]) if t["actual"] > 0 and t["baseline"] > 0 else float("nan")
            term = (1 if c > 0 else -1) * ln if d.kind == "ratio" else ln
            acc += term if math.isfinite(term) else 0.0
            parts.append({"metric": p, "role": ("numerator" if c > 0 else "denominator") if d.kind == "ratio" else "factor",
                          "actual": t["actual"], "baseline": t["baseline"], "rel": t["rel"], "log_change": term,
                          "share": term / lnM if math.isfinite(lnM) and abs(lnM) > 1e-12 else None})
        out["log_change"] = lnM
        out["residual"] = (lnM - acc) if math.isfinite(lnM) else None
    elif d.kind in ("sum", "identity", "share"):
        delta = tot["actual"] - tot["baseline"]
        acc = 0.0
        for p, c in d.parts:
            if p == "1":
                continue
            if not eng.supports(p, list((segment or {}).keys())):
                return out | {"note": f"{p} cannot be broken down by the segment"}
            t = totals(eng, p, segment, start, end, exclude_days)
            term = c * (t["actual"] - t["baseline"])
            acc += term if math.isfinite(term) else 0.0
            parts.append({"metric": p, "coef": c, "actual": t["actual"], "baseline": t["baseline"], "rel": t["rel"],
                          "change": term, "share": term / delta if abs(delta) > 1e-12 else None})
        out["delta"] = delta
        out["residual"] = delta - acc
    parts.sort(key=lambda x: -abs(x.get("share") or 0))
    out["parts"] = parts
    return out


def best(graph: MetricGraph, metric: str, segment: dict | None, start, end, exclude_days=None) -> dict:
    """The canonical decomposition, unless its parts cancel out (e.g. margin = gmv - costs when everything
    grows together): then the valid alternative whose contributions are the least offsetting."""
    first = decompose(graph, metric, segment, start, end, exclude_days)
    spread = sum(abs(p.get("share") or 0) for p in first["parts"])
    if spread <= 2.5:
        return first
    best_dec, best_spread = first, spread
    for d in graph.decomps.get(metric, []):
        if d.valid is False or d.kind not in ("product", "share", "sum") or d.id == (graph.canonical(metric) or d).id:
            continue
        alt = decompose(graph, metric, segment, start, end, exclude_days, decomposition=d.id)
        sp = sum(abs(p.get("share") or 0) for p in alt["parts"])
        if alt["parts"] and sp < best_spread:
            best_dec, best_spread = alt, sp
    return best_dec


def walk(graph: MetricGraph, metric: str, segment: dict | None, start, end, exclude_days=None,
         max_depth: int = 5) -> dict:
    """Follow the largest contributor down the formula tree until an atomic metric: the proximate metric."""
    path = []
    cur = metric
    seen = set()
    while cur not in seen and len(path) < max_depth:
        seen.add(cur)
        if not graph.expandable(cur):
            break
        dec = best(graph, cur, segment, start, end, exclude_days)
        path.append(dec)
        movers = [p for p in dec["parts"] if p.get("share") is not None and p["share"] > 0.3
                  and p["metric"] in graph.reg and abs(p.get("rel") or 0) > 0.02]
        if not movers or dec.get("note"):
            break
        movers.sort(key=lambda p: -p["share"])
        cur = movers[0]["metric"]
    return {"path": path, "proximate": cur}
