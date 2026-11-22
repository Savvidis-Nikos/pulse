"""Helpers that place a new metric in the metric graph (used by pulse.graph_handler).

- `_numeric_duplicates`: existing metrics that are numerically identical to the new one on this data
- `find_identities`: identities that now hold (A = new x B, new = A x B, new = 1 - A), symbolic or numeric
- `cause_candidates`: lagged correlation of de-seasonalised residuals (weak evidence: a candidate, never a fact)
- `rules_meta`: rule-based domain, tier, time aggregation and components for the new metric
"""
from __future__ import annotations

from collections import Counter

import pandas as pd
import sympy as sp

from argus.semantic import expr as X
from argus.semantic.graph import _seasonal_residual
from argus.semantic.registry import MetricSpec

AMBIGUOUS_CORR, MAX_LAG = 0.3, 6


def _numeric_duplicates(world, name: str, s: pd.Series) -> list[str]:
    out = []
    for m in world.reg:
        if m.name == name:
            continue
        try:
            o = world.engine.total_series(m.name).astype(float)
        except Exception:
            continue
        df = pd.concat([s, o], axis=1, join="inner").dropna()
        if len(df) >= 48:
            a, b = df.iloc[:, 0], df.iloc[:, 1]
            if float((a - b).abs().max()) <= 1e-9 * max(1.0, float(b.abs().max())):
                out.append(m.name)
    return out


def _canon(e: sp.Expr) -> str:
    return sp.srepr(sp.cancel(sp.together(e)))


def find_identities(world, name: str, s: pd.Series) -> list[dict]:
    """Identities involving the new metric that are not its own definition.
    Symbolic: A / new == B  (A = new x B) and new / A == B (new = A x B), over expanded definitions.
    Numeric (for SQL/base metrics): new == 1 - A on the total and on slices."""
    reg, eng = world.reg, world.engine
    e_new = X.expand(name, reg)
    canon = {}
    for m in reg:
        if m.name != name:
            try:
                canon.setdefault(_canon(X.expand(m.name, reg)), m.name)
            except Exception:
                continue
    own = set(reg[name].references())
    out = []
    if reg[name].type not in ("simple", "sql"):
        for m in reg:
            if m.name == name or m.name in own:
                continue
            try:
                e_m = X.expand(m.name, reg)
                b1 = canon.get(_canon(e_m / e_new))
                b2 = canon.get(_canon(e_new / e_m))
            except Exception:
                continue
            seen = {tuple(sorted(p for p, _ in i["parts"])) + (i["lhs"],) for i in out}
            if b1 and b1 != name and b1 not in own and b1 != m.name and tuple(sorted([name, b1])) + (m.name,) not in seen:
                out.append({"lhs": m.name, "kind": "product", "parts": [[name, 1.0], [b1, 1.0]],
                            "text": f"{m.name} = {name} x {b1}", "how": "symbolic"})
            if (b2 and b2 != name and b2 != m.name and not ({m.name, b2} <= own)
                    and tuple(sorted([m.name, b2])) + (name,) not in seen):
                out.append({"lhs": name, "kind": "product", "parts": [[m.name, 1.0], [b2, 1.0]],
                            "text": f"{name} = {m.name} x {b2}", "how": "symbolic"})
            if len(out) >= 6:
                break
    if eng.shape(name).ratio_like or reg[name].unit in ("ratio", "share"):
        for m in reg:
            if m.name == name or not (eng.shape(m.name).ratio_like or m.unit in ("ratio", "share")):
                continue
            try:
                o = eng.total_series(m.name).astype(float)
            except Exception:
                continue
            df = pd.concat([s, o], axis=1, join="inner").dropna()
            if len(df) >= 48 and float((df.iloc[:, 0] + df.iloc[:, 1] - 1).abs().max()) < 1e-6:
                out.append({"lhs": name, "kind": "sum", "parts": [["1", 1.0], [m.name, -1.0]],
                            "text": f"{name} = 1 - {m.name}", "how": "numeric (total and all hours)"})
    return out[:6]


_RES: dict[str, pd.Series] = {}


def _residual(world, m: str) -> pd.Series | None:
    if m not in _RES:
        try:
            _RES[m] = _seasonal_residual(world.engine.total_series(m))
        except Exception:
            _RES[m] = None
    return _RES[m]


def cause_candidates(world, name: str, top: int = 6) -> list[dict]:
    """Lagged correlation of de-seasonalised residuals (weak evidence: a candidate, never a fact)."""
    reg, eng = world.reg, world.engine
    _RES.pop(name, None)
    rn = _residual(world, name)
    if rn is None:
        return []
    base_new = {str(s) for s in X.expand(name, reg).free_symbols}
    dims_new = set(eng.dims(name))
    out = []
    for m in reg:
        if m.name == name or not m.watch:
            continue
        try:
            if base_new & {str(s) for s in X.expand(m.name, reg).free_symbols}:
                continue   # formula relatives correlate mechanically
        except Exception:
            continue
        shared = sorted(dims_new & set(eng.dims(m.name)))
        if not shared:
            continue
        rm = _residual(world, m.name)
        if rm is None:
            continue
        best = (0.0, 0)
        for lag in range(-MAX_LAG, MAX_LAG + 1):
            df = pd.concat([rn, rm.shift(lag)], axis=1).dropna()
            if len(df) < 96 or df.iloc[:, 0].std() == 0 or df.iloc[:, 1].std() == 0:
                continue
            c = float(df.corr().iloc[0, 1])
            if abs(c) > abs(best[0]):
                best = (c, lag)
        c, lag = best
        if abs(c) >= AMBIGUOUS_CORR:
            # lag > 0: the other metric leads (candidate cause of the new one); lag < 0: the new one leads
            cause, effect = (m.name, name) if lag >= 0 else (name, m.name)
            out.append({"cause": cause, "effect": effect, "corr": round(c, 3), "lag_h": abs(lag), "join": shared,
                        "sign": 1 if c > 0 else -1})
    out.sort(key=lambda d: -abs(d["corr"]))
    return out[:top]


def rules_meta(world, spec: MetricSpec) -> dict:
    reg, g = world.reg, world.graph
    refs = [r for r in spec.references() if r in reg]
    if not refs and spec.type in ("simple", "sql"):
        tables = world.engine.tables(spec.name)
        refs = [m.name for m in reg if m.name != spec.name and world.engine.tables(m.name) & tables]
    doms = Counter(reg[r].domain for r in refs)
    comps = sorted({c for r in refs for c in g.components_of(r)})[:4]
    ratio = world.engine.shape(spec.name).ratio_like or spec.type == "ratio"
    return {"domain": doms.most_common(1)[0][0] if doms else "other", "tier": "driver" if ratio else "health",
            "time_agg": "sum", "components": comps}
