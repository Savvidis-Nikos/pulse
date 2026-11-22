"""Pulse graph API: read-only, deterministic queries over the metric graph, annotated with the live data.

Every query takes the argus World (`pulse.world.world()`), works at `w.now` and is cached per (clock, graph version,
args). Formula edges point input -> metric, cause edges cause -> effect (pulse.contracts). Graph-handler additions
live in <PULSE_STATE>/graph_changes.jsonl and are re-applied to the in-memory world on every call (never to .argus/).
"""
from __future__ import annotations

import functools
import inspect
import math
import re

import numpy as np
import pandas as pd

from pulse import contracts as C
from pulse.contracts import Edge, GraphChange, GraphSnapshot, Node

Z_ANOM = 3.0                 # |z| at or above this marks a node anomalous
COMPLETENESS_MIN = 0.9       # below this the node is missing_data
MAX_NODES = 40               # snapshot size cap
FORMULA_RESIDUAL_MAX = 0.01  # current-window reconciliation tolerance for formula_status=validated
CAUSE_ACCEPT, CAUSE_WEAK = 0.45, 0.2   # |residual corr| needed to validate / to not reject a cause edge
MAX_LAG = 6                  # hours searched when a proposed cause edge has no lag
CHANGES_FILE = "graph_changes.jsonl"
_STALE = {"stale", "frozen", "down"}   # source verdicts that make a node stale
_MISSING = {"late", "silent"}          # source verdicts that make a node missing_data


# ====================================================================== small helpers
def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd: int = 4) -> float | None:
    x = _f(x)
    if x is None:
        return None
    if x == 0:
        return 0.0
    return float(f"{x:.{nd}g}") if abs(x) < 1e15 else x


def _dev_pct(a, e) -> float | None:
    return _r((a - e) / abs(e) * 100, 4) if a is not None and e else None


def _label(name: str) -> str:
    s = name.replace("_", " ").strip()
    for a, b in (("gmv", "GMV"), ("aov", "AOV"), ("ctr", "CTR"), ("cpc", "CPC"), ("rpm", "RPM"), ("roas", "ROAS"),
                 ("ivt", "IVT"), ("oos", "OOS"), ("sku", "SKU"), ("p99", "p99"), ("3p", "3P"), ("po ", "PO ")):
        s = re.sub(rf"\b{a.strip()}\b", b.strip(), s)
    return s[:1].upper() + s[1:]


def _cache(w) -> dict:
    """Per-world cache, reset whenever the clock or the graph version changes."""
    key = (str(w.now), w.__dict__.get("_pulse_version", 0))
    c = w.__dict__.get("_pulse_cache")
    if c is None or c.get("_key") != key:
        c = {"_key": key}
        w.__dict__["_pulse_cache"] = c
    return c


def _memo(fn):
    """Re-apply pending graph changes, then cache fn(w, *args) per (clock, graph version, function, args)."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(w, *args, **kwargs):
        ensure_applied(w)
        b = sig.bind(w, *args, **kwargs)
        b.apply_defaults()
        key = (fn.__name__, *list(b.arguments.values())[1:])
        c = _cache(w)
        if key not in c:
            c[key] = fn(w, *args, **kwargs)
        return c[key]
    return wrapper


def _bump(w) -> None:
    """Invalidate every cached query of this world (the graph changed)."""
    w.__dict__["_pulse_version"] = w.__dict__.get("_pulse_version", 0) + 1


def _is_metric(w, n) -> bool:
    return isinstance(n, str) and n in w.reg and n in w.graph.G


def _now_iso(w) -> str:
    return pd.Timestamp(w.now).isoformat()


# ====================================================================== change log (graph-handler additions)
def _changes_path():
    return C.STATE_DIR / CHANGES_FILE


def record_change(change: GraphChange) -> None:
    """Append one GraphChange to <PULSE_STATE>/graph_changes.jsonl (applied to worlds on their next call)."""
    p = _changes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(change.model_dump_json() + "\n")


def load_changes() -> list[GraphChange]:
    """Every recorded change, oldest first (blank or corrupt lines are skipped)."""
    p = _changes_path()
    out = []
    for line in p.read_text(encoding="utf-8").splitlines() if p.exists() else []:
        try:
            out.append(GraphChange.model_validate_json(line.strip()))
        except Exception:
            continue
    return out


def ensure_applied(w) -> int:
    """Re-apply recorded graph-handler changes not yet applied to this world. Returns how many were applied."""
    p = _changes_path()
    size = p.stat().st_size if p.exists() else 0
    st = w.__dict__.setdefault("_pulse_applied", {"path": None, "size": 0, "n": 0})
    if st["path"] == str(p) and st["size"] == size:
        return 0
    changes = load_changes()
    start = st["n"] if st["path"] == str(p) and len(changes) >= st["n"] else 0
    n = 0
    for ch in changes[start:]:
        try:
            apply_change(w, ch, bump=False)
            n += 1
        except Exception as exc:   # a broken line must never stop monitoring
            w.__dict__.setdefault("_pulse_apply_errors", []).append(f"{ch.action}: {exc}")
    st.update(path=str(p), size=size, n=len(changes))
    if n:
        _bump(w)
    return n


def apply_change(w, ch: GraphChange, bump: bool = True) -> None:
    """Apply one GraphChange to the in-memory registry + MetricGraph (idempotent). Never persists to .argus/."""
    from argus.semantic.registry import MetricSpec
    from argus.semantic.graph import Decomposition
    from argus.semantic import expr as X
    G = w.graph.G
    if ch.evidence.get("alias") and ch.evidence.get("of") in w.reg:
        w.reg.add_alias(ch.evidence["alias"], ch.evidence["of"], persist=False)
    elif ch.action in ("add_node", "update_formula") and ch.evidence.get("spec"):
        spec = MetricSpec(**ch.evidence["spec"])
        name = spec.name
        if ch.action == "update_formula" or name not in w.reg:
            if name in w.reg:
                w.reg.remove(name)
            w.reg.add(spec, persist=False)
            w.engine.refresh()
        sh = w.engine.shape(name)
        try:
            dims = w.engine.dims(name)
        except Exception:
            dims = []
        G.add_node(name, kind="metric", type=spec.type, domain=spec.domain, tier=spec.tier, unit=spec.unit,
                   polarity=spec.polarity, description=spec.description, additive=sh.additive,
                   ratio_like=sh.ratio_like, dims=dims, watch=spec.watch, added_by="graph_handler",
                   time_agg=spec.time_agg, pulse_status=(ch.node.formula_status if ch.node else None))
        for d in dims:
            G.add_node(f"dim:{d}", kind="dimension", label=d)
            G.add_edge(name, f"dim:{d}", key="breakdown", kind="breakdown",
                       method="additive" if sh.additive else ("mix_rate" if sh.ratio_like else "non_additive"),
                       valid=True)
        # its own definition becomes the canonical decomposition (validated numerically before it was recorded)
        ds = [d for d in w.graph.decomps.get(name, []) if not d.note.startswith("pulse")]
        ok = dict(canonical=True, valid=True, residual=0.0)
        if spec.type == "ratio":
            ds.append(Decomposition(name, "ratio", [(spec.numerator, 1.0), (spec.denominator, -1.0)], **ok,
                                    note="pulse definition"))
        elif spec.type == "derived":
            e = X.definition(spec, set(w.reg.names()))
            if X.is_linear(e):
                ds.append(Decomposition(name, "sum", list(X.linear_terms(e).items()), **ok, note="pulse definition"))
            else:
                num, den = X.fraction_parts(e)
                parts = [(str(s), float(X.sign_of(e, s))) for s in sorted(e.free_symbols, key=str)]
                ds.append(Decomposition(name, "ratio_expr", parts, **ok, note=f"pulse {X.pretty(num)} / {X.pretty(den)}"))
        w.graph.decomps[name] = ds
        for d in ds:
            if d.note.startswith("pulse"):
                for part, coef in d.parts:
                    if part == "1" or part not in w.reg:
                        continue
                    op = {"ratio": "num" if coef > 0 else "den", "sum": "term", "ratio_expr": "input"}.get(d.kind, "input")
                    G.add_edge(name, part, key=f"formula:{d.id}:{part}", kind="formula", op=op,
                               sign=1 if coef > 0 else -1, coef=coef, decomp=d.id, canonical=True, valid=True,
                               residual=0.0, origin="graph_handler")
    elif ch.action in ("add_edge", "mark_ambiguous") and ch.edge is not None:
        e = ch.edge
        if not (_is_metric(w, e.source) and _is_metric(w, e.target)):
            raise KeyError(f"edge endpoints not in graph: {e.source} -> {e.target}")
        status = "ambiguous" if ch.action == "mark_ambiguous" else e.status
        if e.kind == "cause":
            shared = [d for d in w.engine.dims(e.source) if d in w.engine.dims(e.target)]
            G.add_edge(e.source, e.target, key=f"cause:{e.source}->{e.target}", kind="cause", sign=int(e.sign or 1),
                       lag_h=int(e.lag_h or 0), join=shared, mechanism=e.label or "", status=status, note="",
                       valid=True, origin="graph_handler",
                       data_corr=(None if e.strength is None else e.strength * (e.sign or 1)),
                       data_support="consistent" if status == "validated" else "weak")
        elif e.kind == "formula" and not any(a.get("kind") == "formula" and a.get("canonical") for a in
                                             (G.get_edge_data(e.target, e.source, default={}) or {}).values()):
            # an identity/relation in which an existing metric (target) uses `source`; never canonical
            G.add_edge(e.target, e.source, key=f"formula:pulse:{e.target}:{e.source}", kind="formula",
                       op=e.op or "input", sign=int(e.sign or 1), coef=1.0, decomp=f"pulse:{e.target}",
                       canonical=False, valid=status != "rejected", residual=ch.evidence.get("residual"),
                       origin="graph_handler", status=status, label=e.label)
    elif ch.action == "reject_edge" and ch.edge is not None:
        e = ch.edge
        if e.kind == "cause" and G.has_edge(e.source, e.target, key=f"cause:{e.source}->{e.target}"):
            a = G.edges[e.source, e.target, f"cause:{e.source}->{e.target}"]
            a["valid"], a["status"], a["note"] = False, "rejected", ch.reason[:200]
    if bump:
        _bump(w)


# ====================================================================== per-node statistics
@_memo
def _verdicts(w) -> dict:
    """Data-source trust verdicts (argus trust): fresh / stale / late / silent per source."""
    try:
        from argus.analytics import trust
        return trust.assess(w.wh)
    except Exception:
        return {}


@_memo
def _completeness_by_source(w, window_h: int) -> dict[str, float]:
    df = w.wh.frames.get("completeness")
    if df is None or df.empty:
        return {}
    sub = df[(df["ts"] >= pd.Timestamp(w.now) - pd.Timedelta(hours=window_h - 1)) & (df["ts"] <= pd.Timestamp(w.now))]
    return {str(k): float(v) for k, v in sub.groupby("source")["completeness"].mean().items()} if not sub.empty else {}


def _zframe(w, m: str, window_h: int) -> dict:
    """Seasonal baseline frame for the total over the last 8 days (argus baseline, window of window_h hours)."""
    from argus.analytics.baseline import LAG_DAYS, expected_frame, window_frame
    W, V, _ = window_frame(w.engine, m, (), pd.Timestamp(w.now), window_h, 24 * (LAG_DAYS + 1) + 1)
    if W.empty or "_total" not in W.columns:
        return {}
    return expected_frame(w.engine, m, W, V)


def _onset(z: pd.Series, now: pd.Timestamp) -> str | None:
    """First hour of the current excursion (last 24 h): walk back while |z| stays above 2/3 of the threshold."""
    z = z[(z.index > now - pd.Timedelta(hours=24)) & (z.index <= now)]
    if z.empty or not np.isfinite(z.iloc[-1]) or abs(z.iloc[-1]) < Z_ANOM:
        return None
    sign = np.sign(z.iloc[-1])
    onset = z.index[-1]
    for ts, v in z.iloc[::-1].items():
        if not np.isfinite(v) or v * sign < Z_ANOM * 2 / 3:
            break
        onset = ts
    return pd.Timestamp(onset).isoformat()


@_memo
def stats(w, m: str, window_h: int = 3) -> dict:
    """value / expected / deviation_pct / z / completeness / status for metric m over the window ending now."""
    out = {"value": None, "expected": None, "deviation_pct": None, "z": None, "completeness": None,
           "status": "unknown", "hours": 0, "issues": [], "onset_ts": None}
    try:
        now = pd.Timestamp(w.now)
        df = w.engine.hourly(m, (), None, now - pd.Timedelta(hours=window_h - 1), now)
        hours = int(df["ts"].nunique()) if not df.empty else 0
        out["hours"] = hours
        E = _zframe(w, m, window_h)
        if E and now in E["actual"].index:
            a, x, z = (E[k]["_total"].at[now] for k in ("actual", "expected", "z"))
            out["value"], out["expected"] = _r(a, 6), _r(x, 6)
            out["z"] = _r(max(-99.0, min(99.0, z)), 3) if _f(z) is not None else None
            out["deviation_pct"] = _dev_pct(out["value"], out["expected"])
            out["onset_ts"] = _onset(E["z"]["_total"], now)
        elif hours:
            out["value"] = _r(w.engine.window(m, now - pd.Timedelta(hours=window_h - 1), now), 6)
    except Exception as exc:
        out["issues"].append(f"cannot compute: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        srcs = sorted(w.engine.sources(m))
    except Exception:
        srcs = []
    comp = _completeness_by_source(w, window_h)
    vals = [comp[s] for s in srcs if s in comp]
    out["completeness"] = _r(min(vals), 3) if vals else None
    verd = _verdicts(w)
    bad = {s: verd[s] for s in srcs if s in verd and not verd[s].ok}
    status = "ok"
    if out["hours"] == 0:
        status = "missing_data"
        out["issues"].append("no rows in the current window")
    elif out["value"] is None:
        status = "unknown"
        out["issues"].append("no volume in the current window (nothing to measure, e.g. denominator is 0)")
    elif out["completeness"] is not None and out["completeness"] < COMPLETENESS_MIN:
        status = "missing_data"
        out["issues"].append(f"only {out['completeness']:.0%} of expected rows arrived ({', '.join(srcs)})")
    elif any(v.status in _MISSING for v in bad.values()):
        status = "missing_data"
    elif any(v.status in _STALE for v in bad.values()):
        status = "stale"
    elif out["hours"] < window_h:
        status = "missing_data"
        out["issues"].append(f"{out['hours']} of {window_h} hours present")
    elif out["expected"] is None or out["z"] is None:
        status = "unknown"
    elif abs(out["z"]) >= Z_ANOM:
        status = "anomalous"
    for s, v in bad.items():
        out["issues"].append(f"source {s}: {v.status} ({v.reason})")
    out["status"] = status
    out["sources"] = srcs
    return out


def _definition(w, m: str) -> str:
    spec = w.reg[m]
    d = w.graph.canonical(m) if m in w.graph.G else None
    if spec.type == "ratio":
        base = f"{spec.numerator} / {spec.denominator}"
    elif spec.type == "derived":
        base = spec.expr or ""
    elif spec.type == "sql":
        base = "SQL: " + " ".join((spec.sql or "").split())[:160]
    else:
        base = f"{spec.agg}({spec.table}.{spec.column})" + (f" where {spec.where}" if spec.where else "")
    if d is not None and d.formula().replace(" ", "") != base.replace(" ", ""):
        return f"{base}  =  {d.formula()}" if spec.type in ("simple", "sql") else f"{d.formula()}  (= {base})"
    return base


def _formula_status(w, m: str, node_stats: dict, window_h: int) -> str | None:
    """validated | incomplete | unvalidated | broken for metrics with a formula; None for raw metrics."""
    d = w.graph.canonical(m)
    if d is None:
        return "broken" if any(x.valid is False for x in w.graph.decomps.get(m, []) if x.canonical) else None
    if d.valid is False:
        return "broken"
    parts = [p for p, _ in d.parts if p != "1"]
    if any(p not in w.reg for p in parts):
        return "incomplete"
    child = {p: stats(w, p, window_h) for p in parts}
    if any(s["status"] in ("missing_data", "stale") for s in child.values()):
        return "incomplete"
    if d.valid is None:
        return "unvalidated"
    # current-window reconciliation (exact for ratio, sum and telescoping product chains)
    v = node_stats.get("value")
    vals = {p: child[p]["value"] for p in parts}
    if v is not None and all(x is not None for x in vals.values()) and d.kind in ("product", "ratio", "sum", "identity", "share"):
        try:
            if d.kind == "product":
                pred = float(np.prod([vals[p] for p in parts]))
            elif d.kind == "ratio":
                pred = vals[d.parts[0][0]] / vals[d.parts[1][0]] if vals[d.parts[1][0]] else None
            else:
                pred = sum(c * (1.0 if p == "1" else vals[p]) for p, c in d.parts)
            if pred is not None and w.reg[m].time_agg == "sum" and all(w.reg[p].time_agg == "sum" for p in parts):
                res = abs(v - pred) / max(abs(v), 1e-9)
                node_stats["_residual"] = _r(res, 3)
                if res > FORMULA_RESIDUAL_MAX:
                    return "incomplete"
        except (ZeroDivisionError, KeyError, TypeError):
            pass
    return "validated"


@_memo
def attribution(w, m: str, window_h: int = 3) -> dict:
    """Own vs inherited change: formula metrics subtract the deviation their inputs predict (LMDI / differences);
    cause-only metrics regress their seasonal residual on the causes' lagged residuals (history before the last 24 h)."""
    s = stats(w, m, window_h)
    dev = s["deviation_pct"]
    out = {"own_deviation_pct": dev, "inherited_pct": 0.0 if dev is not None else None, "method": "root", "parents": []}
    if dev is None or abs(dev) < 1e-9:
        return out
    g = w.graph
    d = g.canonical(m)
    try:
        if d is not None and all(p == "1" or p in w.reg for p, _ in d.parts):
            ps = {p: stats(w, p, window_h) for p, _ in d.parts if p != "1"}
            if all(x["value"] is not None and x["expected"] is not None for x in ps.values()):
                if d.kind in ("sum", "identity", "share"):
                    exp_m = s["expected"]
                    pred = sum(cf * (ps[p]["value"] - ps[p]["expected"]) for p, cf in d.parts if p != "1")
                    pred_dev = pred / abs(exp_m) * 100 if exp_m else None
                else:
                    if any(x["value"] <= 0 or x["expected"] <= 0 for x in ps.values()):
                        raise ValueError("non-positive input")
                    ln = sum((1 if cf >= 0 else -1) * math.log(ps[p]["value"] / ps[p]["expected"])
                             for p, cf in d.parts if p != "1")
                    pred_dev = (math.exp(ln) - 1) * 100
                if pred_dev is not None:
                    own = dev - pred_dev
                    out = {"own_deviation_pct": _r(own, 4),
                           "inherited_pct": _r(max(0.0, min(1.0, 1 - abs(own) / abs(dev))) * 100, 4),
                           "method": f"formula ({d.formula()})", "parents": list(ps)}
                    return out
        causes = [(u, a) for u, a in g.causes_of(m) if u in w.reg][:5]
        if causes:
            from argus.semantic.graph import _seasonal_residual
            y = _seasonal_residual(w.engine.total_series(m))
            X_ = pd.concat([_seasonal_residual(w.engine.total_series(u)).shift(int(a.get("lag_h", 0))).rename(u)
                            for u, a in causes], axis=1)
            now = pd.Timestamp(w.now)
            df = pd.concat([y.rename("_y"), X_], axis=1).replace([np.inf, -np.inf], np.nan)
            hist = df[df.index <= now - pd.Timedelta(hours=24)].dropna()
            cur = df[(df.index > now - pd.Timedelta(hours=window_h)) & (df.index <= now)].mean()
            if len(hist) >= 72 and cur.notna().all():
                A = np.column_stack([hist[[u for u, _ in causes]].to_numpy(), np.ones(len(hist))])
                beta, *_ = np.linalg.lstsq(A, hist["_y"].to_numpy(), rcond=None)
                pred = float(np.dot(cur[[u for u, _ in causes]].to_numpy(), beta[:-1]))
                actual = float(cur["_y"])
                share = max(0.0, min(1.0, pred / actual)) if abs(actual) > 1e-9 else 0.0
                out = {"own_deviation_pct": _r(dev * (1 - share), 4), "inherited_pct": _r(share * 100, 4),
                       "method": "regression on cause residuals", "parents": [u for u, _ in causes],
                       "betas": {u: _r(b, 3) for (u, _), b in zip(causes, beta[:-1])}}
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
    return out


def _node(w, m: str, window_h: int, depth: int, kpi: str) -> Node:
    s = dict(stats(w, m, window_h))
    a = w.graph.G.nodes[m]
    spec = w.reg[m]
    fs = _formula_status(w, m, s, window_h)
    at = attribution(w, m, window_h)
    return Node(id=m, label=_label(m), kind="kpi" if m == kpi else "metric", domain=spec.domain, tier=spec.tier,
                unit=spec.unit, polarity=spec.polarity, description=spec.description, definition=_definition(w, m),
                value=s["value"], expected=s["expected"], deviation_pct=s["deviation_pct"], z=s["z"],
                completeness=s["completeness"], status=s["status"], formula_status=fs,
                own_deviation_pct=at["own_deviation_pct"], inherited_pct=at["inherited_pct"], onset_ts=s["onset_ts"],
                added_by=str(a.get("added_by", "seed")), depth=depth)


# ====================================================================== edges
def _formula_edges_of(w, m: str) -> list[Edge]:
    """Canonical formula inputs of m as Pulse edges (input -> m)."""
    d = w.graph.canonical(m)
    if d is None:
        return []
    out = []
    origin = "graph_handler" if d.note.startswith("pulse") else "seed"
    for p, coef in d.parts:
        if p == "1" or p not in w.reg:
            continue
        if d.kind == "ratio":
            op, lab = ("*" if coef > 0 else "/"), ("numerator" if coef > 0 else "denominator")
        elif d.kind == "ratio_expr":
            op, lab = ("*" if coef >= 0 else "/"), "input"
        elif d.kind in ("sum", "identity", "share"):
            op, lab = ("+" if coef > 0 else "-"), "term"
        else:
            op, lab = "*", "factor"
        out.append(Edge(source=p, target=m, kind="formula",
                        status="validated" if d.valid else ("asserted" if d.valid is None else "rejected"),
                        op=op, sign=1 if coef >= 0 else -1, strength=1.0 if d.valid else None,
                        label=f"{lab} of {m} = {d.formula()}", added_by=origin))
    return out


def _cause_edge(u: str, v: str, a: dict) -> Edge:
    corr = a.get("data_corr")
    st = a.get("status", "asserted")
    if st not in ("validated", "asserted", "learned", "ambiguous", "proposed", "rejected"):
        st = "asserted"
    lab = a.get("mechanism", "") or ""
    if a.get("data_support") == "contradicted":
        lab += " [data contradicts the sign]"
    return Edge(source=u, target=v, kind="cause", status=st, sign=int(a.get("sign", 1)), lag_h=int(a.get("lag_h", 0)),
                strength=_r(abs(corr), 3) if corr is not None else None, label=lab,
                added_by="graph_handler" if a.get("origin") == "graph_handler" else "seed")


def _contributions(w, target: str, edges: list[Edge], nodes: dict[str, Node]) -> None:
    """Fill contribution_pct on formula edges: share of the target's change explained by each input."""
    d = w.graph.canonical(target)
    if d is None:
        return
    parts = {e.source: e for e in edges}
    raw: dict[str, float] = {}
    for p, coef in d.parts:
        if p == "1" or p not in parts:
            continue
        v, x = nodes[p].value, nodes[p].expected
        if v is None or x is None:
            return
        if d.kind in ("sum", "identity", "share"):
            raw[p] = coef * (v - x)
        else:
            if v <= 0 or x <= 0:
                return
            raw[p] = math.log(v / x) * (1 if coef >= 0 else -1)
    tot = sum(raw.values())
    if not raw or abs(tot) < 1e-12:
        return
    for p, val in raw.items():
        parts[p].contribution_pct = _r(val / tot * 100, 4)


# ====================================================================== snapshot
def build_snapshot(w, kpi: str, center: str | None = None, depth: int = 2, window_h: int = 3) -> GraphSnapshot:
    """Annotated sub-graph around `center` (default: the KPI) with typed edges, contributions and data notes."""
    return _snapshot(w, kpi, center or kpi, depth, window_h).model_copy(deep=True)


def _select_nodes(w, kpi: str, center: str, depth: int, window_h: int) -> dict[str, int]:
    """BFS from the centre, per level: formula inputs, graph-handler additions, causes (flagged first), then up to
    6 flagged parents/effects of the centre. Returns {metric: depth}, at most MAX_NODES."""
    g = w.graph
    order: dict[str, int] = {center: 0}
    if kpi in w.reg and kpi != center:
        order[kpi] = 0
    frontier = [center]
    for d in range(1, depth + 1):
        cands: dict[str, int] = {}   # name -> priority (0 formula input, 1 handler addition, 2 cause, 3 parent)
        for n in frontier:
            for p, _ in g.formula_children(n):
                cands.setdefault(p, 0)
            for u, _ in g.causes_of(n):
                if _is_metric(w, u):
                    cands.setdefault(u, 2)
            for nb in list(g.G.predecessors(n)) + list(g.G.successors(n)):
                if _is_metric(w, nb) and g.G.nodes[nb].get("added_by") == "graph_handler":
                    cands.setdefault(nb, 1)
            if n == center:
                parents = {u for u, _, _ in g.edges("formula", n, out=False)} | {v for v, _ in g.effects_of(n)}
                for nb in parents:
                    if _is_metric(w, nb):
                        cands.setdefault(nb, 3)
        ranked = []
        for nb, prio in cands.items():
            if nb in order:
                continue
            s = stats(w, nb, window_h)
            z = abs(s["z"]) if s["z"] is not None else 0.0
            flagged = s["status"] in ("anomalous", "missing_data", "stale")
            if prio == 3 and not flagged:
                continue
            ranked.append((prio, not flagged, -z, nb))
        ranked.sort()
        n_parents = 0
        nxt = []
        for prio, _, _, nb in ranked:
            if len(order) >= MAX_NODES:
                break
            if prio == 3:
                n_parents += 1
                if n_parents > 6:
                    continue
            order[nb] = d
            if prio != 3:
                nxt.append(nb)
        frontier = nxt
    return order


def _notes(w, nodes: dict[str, Node], window_h: int) -> list[str]:
    """Data-quality notes: bad sources, missing data, broken formulas, graph changes that failed to apply."""
    notes = []
    verd = _verdicts(w)
    srcs = sorted({s for m in nodes for s in stats(w, m, window_h).get("sources", [])})
    for s in srcs:
        v = verd.get(s)
        if v is not None and not v.ok:
            affected = [m for m in nodes if s in stats(w, m, window_h).get("sources", [])]
            notes.append(f"data source {s} is {v.status}: {v.reason}; affects {', '.join(affected[:8])}")
    for m, n in nodes.items():
        if n.status == "missing_data":
            iss = [i for i in stats(w, m, window_h)["issues"] if not i.startswith("source ")]
            notes.append(f"{m}: missing data" + (f" ({'; '.join(iss)})" if iss else ""))
        if n.formula_status in ("incomplete", "broken"):
            notes.append(f"{m}: formula {n.formula_status} ({n.definition})")
    for err in w.__dict__.get("_pulse_apply_errors", [])[-3:]:
        notes.append(f"graph change not applied: {err}")
    return notes


@_memo
def _snapshot(w, kpi: str, center: str, depth: int, window_h: int) -> GraphSnapshot:
    if center not in w.reg:
        raise KeyError(f"unknown metric {center!r}")
    g = w.graph
    order = _select_nodes(w, kpi, center, depth, window_h)
    nodes = {m: _node(w, m, window_h, dep, kpi) for m, dep in order.items()}
    edges: list[Edge] = []
    for m in nodes:
        fe = [e for e in _formula_edges_of(w, m) if e.source in nodes]
        _contributions(w, m, fe, nodes)
        edges += fe
    seen = set()
    for u, v, a in g.G.edges(data=True):
        if a.get("kind") == "cause" and u in nodes and v in nodes and a.get("valid", True) and (u, v) not in seen:
            seen.add((u, v))
            edges.append(_cause_edge(u, v, a))
    # non-canonical graph-handler formula relations (identities found for new metrics)
    for u, v, a in g.G.edges(data=True):
        if a.get("kind") == "formula" and a.get("origin") == "graph_handler" and not a.get("canonical") \
                and u in nodes and v in nodes and not any(x.kind == "formula" and x.source == v and x.target == u
                                                          for x in edges):
            edges.append(Edge(source=v, target=u, kind="formula", status=a.get("status", "validated"),
                              op=a.get("op"), label=a.get("label", ""), added_by="graph_handler"))
    return GraphSnapshot(snapshot_id=f"{kpi}-{center}-{pd.Timestamp(w.now):%Y%m%d%H}", kpi=kpi, center=center,
                         ts=_now_iso(w), window_h=window_h, depth=depth, nodes=list(nodes.values()), edges=edges,
                         notes=_notes(w, nodes, window_h))


# ====================================================================== node detail / neighbours
@_memo
def node_detail(w, node: str, window_h: int = 3) -> dict:
    """Everything about one metric: meaning, current numbers, last 48 h hourly series, dims and neighbours."""
    if node not in w.reg:
        raise KeyError(f"unknown metric {node!r}")
    spec = w.reg[node]
    s = dict(stats(w, node, window_h))
    fs = _formula_status(w, node, s, window_h)
    series = []
    try:
        from argus.analytics.baseline import band
        for ts, r in band(w.engine, node, None, w.now, hours=48).iterrows():
            series.append({"ts": f"{ts:%Y-%m-%d %H:%M}", "value": _r(r["actual"]), "expected": _r(r["expected"])})
    except Exception:
        pass
    if not series:
        try:
            ser = w.engine.total_series(node, start=pd.Timestamp(w.now) - pd.Timedelta(hours=47), end=w.now)
            series = [{"ts": f"{ts:%Y-%m-%d %H:%M}", "value": _r(v), "expected": None} for ts, v in ser.items()]
        except Exception:
            pass
    nb = neighbors(w, node)
    return {
        "id": node, "label": _label(node), "description": spec.description, "definition": _definition(w, node),
        "type": spec.type, "unit": spec.unit, "polarity": spec.polarity, "domain": spec.domain, "tier": spec.tier,
        "added_by": w.graph.G.nodes[node].get("added_by", "seed"),
        "window_h": window_h, "value": s["value"], "expected": s["expected"], "deviation_pct": s["deviation_pct"],
        "z": s["z"], "status": s["status"], "formula_status": fs, "completeness": s["completeness"],
        "onset_ts": s["onset_ts"], "attribution": attribution(w, node, window_h),
        "sources": s.get("sources", []), "issues": s["issues"], "formula_residual": s.get("_residual"),
        "dims": nb["dims"], "series_48h": series,
        "inputs": nb["inputs"], "used_by": nb["used_by"], "causes": nb["causes"], "effects": nb["effects"],
        "components": nb["components"],
    }


@_memo
def neighbors(w, node: str) -> dict:
    """Typed edges around a metric (formula inputs/uses, causes/effects) incl. graph-handler additions."""
    g = w.graph
    if node not in g.G:
        raise KeyError(f"unknown metric {node!r}")
    d = g.canonical(node)
    inputs = _formula_edges_of(w, node)
    edges: list[Edge] = list(inputs)
    used_by = []
    for u, _, a in g.G.in_edges(node, data=True):
        if a.get("kind") == "formula" and a.get("valid", True) and u in w.reg and u not in used_by:
            used_by.append(u)
    for u in used_by:
        dd = g.canonical(u)
        if dd is not None and any(p == node for p, _ in dd.parts):
            edges += [e for e in _formula_edges_of(w, u) if e.source == node]
        else:
            edges.append(Edge(source=node, target=u, kind="formula", status="validated", op="+",
                              label=f"{node} appears in an alternative decomposition of {u}",
                              added_by="graph_handler" if any(
                                  a.get("origin") == "graph_handler" for a in
                                  (g.G.get_edge_data(u, node, default={}) or {}).values()) else "seed"))
    causes, effects = [], []
    for rows, pairs, inbound in ((causes, g.causes_of(node), True), (effects, g.effects_of(node), False)):
        for x, a in pairs:
            e = _cause_edge(x, node, a) if inbound else _cause_edge(node, x, a)
            edges.append(e)
            rows.append({"metric": x, "sign": e.sign, "lag_h": e.lag_h, "status": e.status, "strength": e.strength,
                         "mechanism": a.get("mechanism", ""), "added_by": e.added_by})
    alts = [{"formula": x.formula(), "kind": x.kind, "valid": x.valid, "canonical": x.canonical,
             "residual": _r(x.residual, 3)} for x in g.decomps.get(node, [])]
    return {
        "metric": node,
        "definition": d.formula() if d else _definition(w, node),
        "inputs": [{"metric": e.source, "op": e.op, "status": e.status} for e in inputs],
        "used_by": used_by,
        "causes": causes,
        "effects": effects,
        "dims": list(g.G.nodes[node].get("dims", [])),
        "components": g.components_of(node),
        "decompositions": alts,
        "edges": [e.model_dump() for e in edges],
    }


# ====================================================================== breakdown
def _js(e: float, et: float, a: float, at: float) -> float:
    """Adtributor surprise of one segment: JS-divergence term between expected share p and actual share q."""
    p = e / et if et else 0.0
    q = a / at if at else 0.0
    out = 0.0
    m = (p + q) / 2
    if p > 0 and m > 0:
        out += 0.5 * p * math.log(p / m)
    if q > 0 and m > 0:
        out += 0.5 * q * math.log(q / m)
    return out


@_memo
def breakdown(w, node: str, dim: str, window_h: int = 3) -> list[dict]:
    """Per segment of `dim`: value, expected, deviation, share_of_change (sums to ~100), JS surprise; ratios add a
    linearised num/den split and rate_effect / mix_effect. ValueError if the metric has no such dim."""
    from argus.analytics.baseline import scan_rows
    eng = w.engine
    if node not in w.reg:
        raise KeyError(f"unknown metric {node!r}")
    if dim not in eng.dims(node):
        raise ValueError(f"{node} cannot be broken down by {dim}; available: {eng.dims(node)}")
    now = pd.Timestamp(w.now)
    rows = scan_rows(eng, node, (dim,), now, window_h)
    if rows.empty:
        return []
    spec = w.reg[node]
    sh = eng.shape(node)
    seg = lambda r: str(r["segment"][dim])  # noqa: E731
    out = []
    for _, r in rows.iterrows():
        a, e = _f(r["actual"]), _f(r["expected"])
        out.append({"segment": seg(r), "value": _r(a, 6), "expected": _r(e, 6),
                    "deviation_pct": _dev_pct(a, e),
                    "z": _r(r["z"], 3), "volume": _r(r["volume"], 6), "share_of_change": None})
    raw: dict[str, float] = {}
    if sh.ratio_like and spec.type == "ratio":
        N = scan_rows(eng, spec.numerator, (dim,), now, window_h)
        D = scan_rows(eng, spec.denominator, (dim,), now, window_h)
        if not N.empty and not D.empty:
            n, dd = ({seg(r): (_f(r["actual"]) or 0.0, _f(r["expected"]) or 0.0) for _, r in X.iterrows()}
                     for X in (N, D))
            Na, Ne = sum(x[0] for x in n.values()), sum(x[1] for x in n.values())
            Da, De = sum(x[0] for x in dd.values()), sum(x[1] for x in dd.values())
            if De > 0 and Da > 0:
                Re = Ne / De
                for o in out:
                    s = o["segment"]
                    na, ne = n.get(s, (0.0, 0.0))
                    da, de = dd.get(s, (0.0, 0.0))
                    raw[s] = ((na - ne) - Re * (da - de)) / De
                    ra = na / da if da else None
                    re_ = ne / de if de else None
                    if ra is not None and re_ is not None:
                        # Shapley split with averaged weights: rate + mix sums exactly to R - Re over segments
                        wa, we = da / Da, de / De
                        o["rate_effect"] = _r(0.5 * (wa + we) * (ra - re_), 4)
                        o["mix_effect"] = _r((wa - we) * 0.5 * (ra + re_), 4)
                    # surprise: JS divergence of the segment's share of numerator and of denominator
                    o["surprise"] = _r(_js(ne, Ne, na, Na) + _js(de, De, da, Da), 4)
    elif sh.additive:
        ta = sum(o["value"] or 0.0 for o in out)
        te = sum(o["expected"] or 0.0 for o in out)
        for o in out:
            if o["value"] is not None and o["expected"] is not None:
                raw[o["segment"]] = o["value"] - o["expected"]
                o["surprise"] = _r(_js(o["expected"], te, o["value"], ta), 4)
    if not raw:   # non-additive fallback: volume-weighted deviation
        for o in out:
            if o["value"] is not None and o["expected"] is not None:
                raw[o["segment"]] = (o["value"] - o["expected"]) * (o["volume"] or 1.0)
    tot = sum(raw.values())
    if abs(tot) > 1e-12:
        for o in out:
            if o["segment"] in raw:
                o["share_of_change"] = _r(raw[o["segment"]] / tot * 100, 4)
    out.sort(key=lambda o: -abs(o["share_of_change"] or 0.0))
    return out


# ====================================================================== search over raw data + registry
SYNONYMS = {
    "revenue": ["gmv", "sales", "revenue"], "sales": ["gmv", "sales", "orders"], "money": ["gmv", "eur"],
    "visits": ["sessions", "visit"], "visit": ["sessions"], "visitors": ["sessions", "users"], "traffic": ["sessions"],
    "session": ["sessions"], "paid": ["ad", "ads", "sponsored", "marketing"], "ads": ["ad", "ads", "sponsored", "auction"],
    "advertising": ["ad", "ads", "sponsored"], "ad": ["ad", "ads", "sponsored"], "buy": ["orders", "checkout"],
    "purchases": ["orders"], "purchase": ["orders"], "conversion": ["orders", "conversion"], "basket": ["cart"],
    "cart": ["cart", "carts", "add_to_carts"], "stock": ["stock", "inventory", "oos", "in_stock"],
    "out": ["oos"], "unavailable": ["oos", "in_stock", "unavailable"], "availability": ["in_stock", "availability"],
    "error": ["errors", "error", "failures"], "errors": ["errors", "error", "failures"], "failure": ["failures", "errors"],
    "failed": ["failures", "errors"], "crash": ["crashes", "crash"], "crashes": ["crashes", "crash"],
    "payment": ["payment", "payments", "auth"], "pay": ["payment", "payments"], "refund": ["refunds"],
    "returns": ["refunds"], "complaints": ["tickets", "support"], "support": ["tickets", "support"],
    "tickets": ["tickets"], "slow": ["latency", "p99", "ms"], "latency": ["latency", "p99_ms", "p99"],
    "speed": ["latency", "p99"], "price": ["price", "price_index"], "prices": ["price", "price_index"],
    "competitor": ["market", "competitor", "price_index"], "seller": ["sellers", "seller"], "sellers": ["sellers"],
    "delivery": ["received", "due", "delivery", "wholesale"], "supplier": ["supplier", "wholesale"],
    "recommendation": ["feed", "recsys"], "recommendations": ["feed", "recsys"], "feed": ["feed"],
    "search": ["search", "searches"], "views": ["views", "product_views"], "page": ["views", "page"],
    "checkout": ["checkout", "checkouts"], "requests": ["requests"], "response": ["response", "first_response"],
    "sentiment": ["sentiment"], "discount": ["discount", "discounts"], "promo": ["promo", "discount"],
    "margin": ["margin", "cogs", "profit"], "profit": ["profit", "margin"], "cost": ["cost", "cogs"],
    "budget": ["budget"], "impressions": ["impressions"], "clicks": ["clicks"], "share": ["share", "rate"],
    "fraction": ["share", "rate"], "rate": ["rate", "share"], "items": ["n_items", "items"],
}
STOP = {"the", "of", "a", "an", "and", "or", "to", "in", "on", "for", "by", "per", "that", "from", "with", "as",
        "is", "are", "how", "many", "what", "which", "we", "our", "share", "number", "total", "count", "hour",
        "came", "come", "it", "this", "be", "should", "all", "each", "every", "at", "rate", "ratio", "sum", "metric"}


def _tokens(text: str) -> list[str]:
    t = re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
    return [x for x in t if x not in STOP and len(x) > 1]


def _stem(t: str) -> str:
    for suf in ("ies", "es", "s"):
        if t.endswith(suf) and len(t) > len(suf) + 2:
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def _expand_query(query: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in _tokens(query):
        out[_stem(t)] = max(out.get(_stem(t), 0), 1.0)
        for s in SYNONYMS.get(t, []) + SYNONYMS.get(_stem(t), []):
            for p in _tokens(s):
                out[_stem(p)] = max(out.get(_stem(p), 0), 0.6)
    return out


@_memo
def _modelled_columns(w) -> dict[tuple[str, str], str]:
    """(table, column) -> the simple metric that reads it."""
    used: dict[tuple[str, str], str] = {}
    for m in w.reg:
        if m.type == "simple":
            for t in [m.table] + list(m.alt_tables):
                used.setdefault((t, m.column), m.name)
    return used


def search_data(w, query: str, limit: int = 15) -> list[dict]:
    """Registry metrics and raw warehouse columns matching the query (tokens + synonyms), best first.
    status 'unmodelled' marks raw data the metric graph does not use yet."""
    ensure_applied(w)
    q = _expand_query(query)
    if not q:
        return []
    hits = []

    def score(text: str) -> float:
        toks = {_stem(t) for t in _tokens(text)}
        return sum(wt for t, wt in q.items() if t in toks or any(len(t) > 3 and (t in x or x in t) and len(x) > 3
                                                                    for x in toks) and wt >= 0.6)
    used = _modelled_columns(w)
    sql_words: dict[str, set] = {}
    for m in w.reg:
        if m.type == "sql":
            for word in set(re.findall(r"[a-z_]+", (m.sql or "").lower())):
                sql_words.setdefault(word, set()).add(m.name)
    for m in w.reg:
        s = 1.5 * score(m.name) + 0.5 * score(m.description or "") + score(" ".join(m.aliases))
        if s > 0:
            hits.append({"kind": "metric", "name": m.name, "table": m.table, "column": m.column,
                         "description": m.description, "status": "modelled", "metric": m.name, "score": round(s, 2),
                         "definition": _definition(w, m.name), "unit": m.unit,
                         "added_by": w.graph.G.nodes.get(m.name, {}).get("added_by", "seed")})
    for t, spec in w.wh.specs.items():
        if not w.wh.has(t) or t in ("completeness", "pipeline_health") or t not in w.wh.frames:
            continue
        df = w.wh.frames[t]
        num_cols = [col for col in df.select_dtypes("number").columns if col not in spec.dims]
        tscore = 0.4 * score(t)
        for col in num_cols:
            cs = score(col)
            if cs <= 0 and tscore <= 0:
                continue
            s = cs + tscore
            m = used.get((t, col))
            if not m and (t in sql_words.get(t, set()) or (col in sql_words and t in sql_words)):
                both = sql_words[col] & sql_words[t]   # a SQL metric that mentions both table and column
                m = sorted(both)[0] if both else None
            status = "modelled" if m else "unmodelled"
            try:
                mean = _r(df[col].tail(5000).mean(), 4)
            except Exception:
                mean = None
            hits.append({"kind": "column", "name": f"{t}.{col}", "table": t, "column": col,
                         "description": f"raw column {col} in table {t} ({spec.kind}, by {', '.join(spec.dims) or '-'})",
                         "status": status, "metric": m, "score": round(s + (0.3 if status == "unmodelled" else 0), 2),
                         "dims": list(spec.dims), "table_kind": spec.kind, "sample_mean": mean,
                         "source": w.wh.source_of(t, col)})
    hits.sort(key=lambda h: (-h["score"], h["kind"] != "column", h["name"]))
    return hits[:limit]


# ====================================================================== numeric validation
def _series(w, m: str) -> pd.Series:
    end = pd.Timestamp(w.now)
    return w.engine.total_series(m, start=end - pd.Timedelta(hours=24 * 14 - 1), end=end).astype(float)


def _eval_expr(w, expr: str) -> tuple[pd.Series, object]:
    from argus.semantic import expr as X
    names = set(w.reg.names()) | {a for m in w.reg for a in m.aliases}
    e = X.parse(expr, names)
    e = e.xreplace({s: X.sym(w.reg.resolve(str(s))) for s in e.free_symbols})
    cols = {str(s): _series(w, str(s)) for s in sorted(e.free_symbols, key=str)}
    if not cols:
        raise ValueError("expression has no metric inputs")
    idx = functools.reduce(lambda i, j: i.union(j), (v.index for v in cols.values()))
    arr = {k: v.reindex(idx).to_numpy(dtype=float) for k, v in cols.items()}
    return pd.Series(X.evaluate(e, arr), index=idx), e


def _fit(target: pd.Series, pred: pd.Series) -> dict:
    df = pd.concat([target.rename("t"), pred.rename("p")], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 6:
        return {"n_hours": int(len(df)), "max_rel_residual": None, "median_rel_residual": None, "corr": None,
                "ratio_median": None}
    scale = df["t"].abs().where(df["t"].abs() > 1e-9, 1.0)
    rel = (df["t"] - df["p"]).abs() / scale
    corr = float(df.corr().iloc[0, 1]) if df["t"].std() > 0 and df["p"].std() > 0 else None
    ratio = (df["t"] / df["p"].replace(0, np.nan)).dropna()
    return {"n_hours": int(len(df)), "max_rel_residual": _r(rel.max(), 3), "median_rel_residual": _r(rel.median(), 3),
            "corr": _r(corr, 4), "ratio_median": _r(ratio.median(), 4) if len(ratio) else None,
            "ratio_cv": _r(ratio.std() / abs(ratio.mean()), 3) if len(ratio) > 2 and ratio.mean() else None}


def try_formula(w, name: str, expr: str) -> dict:
    """Evaluate a formula over existing metrics (hourly, 14 days); if `name` exists, report the fit against it."""
    ensure_applied(w)
    try:
        pred, e = _eval_expr(w, expr)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "expr": expr}
    from argus.semantic import expr as X
    p = pred.dropna()
    out = {"ok": True, "expr": X.pretty(e), "inputs": sorted(str(s) for s in e.free_symbols),
           "shape": X.classify(e), "n_hours": int(len(p)),
           "summary": ({"last": _r(p.iloc[-1]), "mean": _r(p.mean()), "min": _r(p.min()), "max": _r(p.max())}
                       if len(p) else dict.fromkeys(("last", "mean", "min", "max")))}
    if name in w.reg:
        fit = _fit(_series(w, name), pred)
        out["target"] = name
        out["fit"] = fit
        mr = fit["max_rel_residual"]
        out["verdict"] = ("exact" if mr is not None and mr < 1e-6 else
                          "close" if mr is not None and mr < 0.01 else
                          "proportional" if fit.get("ratio_cv") is not None and fit["ratio_cv"] < 0.01 else
                          "no_fit")
        try:
            out["symbolic_identity"] = bool(X.equivalent(X.expand(name, w.reg), e.xreplace(
                {s: X.expand(str(s), w.reg) for s in e.free_symbols})))
        except Exception:
            out["symbolic_identity"] = None
    return out


def _resid(w, m: str) -> pd.Series | None:
    """De-seasonalised hourly residual of m's total (cached per world; None when it cannot be computed)."""
    res = _cache(w).setdefault("resid", {})
    if m not in res:
        from argus.semantic.graph import _seasonal_residual
        try:
            res[m] = _seasonal_residual(w.engine.total_series(m))
        except Exception:
            res[m] = None
    return res[m]


def _residual_corr(w, a: str, b: str, lag: int) -> tuple[float | None, int]:
    ra, rb = _resid(w, a), _resid(w, b)
    if ra is None or rb is None:
        return None, 0
    df = pd.concat([ra, rb.shift(-lag)], axis=1).dropna()
    if len(df) < 48 or df.iloc[:, 0].std() == 0 or df.iloc[:, 1].std() == 0:
        return None, int(len(df))
    return round(float(df.corr().iloc[0, 1]), 3), int(len(df))


def _partial_corr(w, a: str, b: str, lag: int, controls: list[str]) -> float | None:
    """Correlation of a's and b's residuals after regressing both on the controls' residuals."""
    ra, rb = _resid(w, a), _resid(w, b)
    cols = [ra, rb.shift(-lag) if rb is not None else None] + [_resid(w, m) for m in controls]
    if any(x is None for x in cols):
        return None
    df = pd.concat(cols, axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 48:
        return None
    Z = np.column_stack([df.iloc[:, 2:].to_numpy(), np.ones(len(df))])
    ra = df.iloc[:, 0].to_numpy() - Z @ np.linalg.lstsq(Z, df.iloc[:, 0].to_numpy(), rcond=None)[0]
    rb = df.iloc[:, 1].to_numpy() - Z @ np.linalg.lstsq(Z, df.iloc[:, 1].to_numpy(), rcond=None)[0]
    if ra.std() == 0 or rb.std() == 0:
        return None
    return round(float(np.corrcoef(ra, rb)[0, 1]), 3)


def _formula_relatives(w, a: str, b: str) -> bool:
    from argus.semantic import expr as X
    try:
        fa = {str(s) for s in X.expand(a, w.reg).free_symbols}
        fb = {str(s) for s in X.expand(b, w.reg).free_symbols}
        return bool(fa & fb)
    except Exception:
        return False


def _decide(e: Edge, ts: str, status: str, reason: str, ev: dict | None = None) -> GraphChange:
    """Set the edge status and wrap it in the matching change: validated -> add, rejected -> reject, else ambiguous."""
    e.status = status
    action = {"validated": "add_edge", "rejected": "reject_edge"}.get(status, "mark_ambiguous")
    return GraphChange(ts=ts, action=action, edge=e, reason=reason, evidence=ev or {})


def validate_edge(w, edge: Edge) -> GraphChange:
    """Numeric check of a proposed edge (not persisted). Formula: does it reproduce the target? (label may carry
    "target = expr"). Cause: lagged residual correlation + sign, shared-dim, formula-relative and confounder checks."""
    ensure_applied(w)
    ts = _now_iso(w)
    e = edge.model_copy(deep=True)
    for m in (e.source, e.target):
        if m not in w.reg:
            return _decide(e, ts, "rejected", f"unknown metric {m}")
    if e.kind == "formula":
        return _validate_formula(w, e, ts)
    if e.kind != "cause":
        return _decide(e, ts, "proposed", f"{e.kind} edges are not validated numerically")
    shared = sorted(set(w.engine.dims(e.source)) & set(w.engine.dims(e.target)))
    ev: dict = {"shared_dims": shared}
    if e.lag_h is None:
        best, nh = (None, 0), 0
        for lag in range(0, MAX_LAG + 1):
            cval, nh = _residual_corr(w, e.source, e.target, lag)
            if cval is not None and (best[0] is None or abs(cval) > abs(best[0])):
                best = (cval, lag)
        corr, lag = best
    else:
        lag = int(e.lag_h)
        corr, nh = _residual_corr(w, e.source, e.target, lag)
    ev.update(corr=corr, lag_h=lag, n_hours=nh, test="lagged correlation of de-seasonalised hourly residuals")
    e.lag_h = lag
    rel = _formula_relatives(w, e.source, e.target)
    ev["formula_relatives"] = rel
    if e.sign is None and corr is not None:
        e.sign = 1 if corr > 0 else -1
    e.strength = _r(abs(corr), 3) if corr is not None else None
    if corr is None:
        return _decide(e, ts, "ambiguous", "not enough overlapping history to test the relationship", ev)
    sign_ok = corr * (e.sign or 1) > 0
    ev["sign_consistent"] = sign_ok
    if abs(corr) < CAUSE_WEAK:
        return _decide(e, ts, "rejected",
                       f"no co-movement in the data (|corr| {abs(corr):.2f} < {CAUSE_WEAK} at lag {lag} h)", ev)
    if not sign_ok:
        return _decide(e, ts, "rejected",
                       f"data contradicts the claimed sign (corr {corr:+.2f}, claimed {e.sign:+d})", ev)
    if not shared:
        return _decide(e, ts, "ambiguous", "correlated in total but no shared dimension to localise it", ev)
    if rel:
        return _decide(e, ts, "ambiguous",
                       f"corr {corr:+.2f} but the two metrics share formula inputs (mechanical correlation)", ev)
    # common parent (possible confounder): shared causes in the graph; partial correlation controls for them
    g = w.graph
    common = sorted({u for u, _ in g.causes_of(e.source)} & {u for u, _ in g.causes_of(e.target)})
    ev["common_parents"] = common
    if common:
        pc = _partial_corr(w, e.source, e.target, lag, common[:3])
        ev["partial_corr"] = pc
        if pc is None or abs(pc) < CAUSE_ACCEPT:
            return _decide(e, ts, "ambiguous", f"corr {corr:+.2f} but both depend on {', '.join(common[:3])} "
                           f"(possible confounder; partial corr {pc if pc is None else f'{pc:+.2f}'})", ev)
    if abs(corr) >= CAUSE_ACCEPT:
        return _decide(e, ts, "validated", f"residuals co-move (corr {corr:+.2f} at lag {lag} h), sign as claimed", ev)
    return _decide(e, ts, "ambiguous", f"weak support (corr {corr:+.2f} at lag {lag} h): plausible but unproven", ev)


def _validate_formula(w, e: Edge, ts: str) -> GraphChange:
    """An explicit "target = expr" in the label is re-computed; else look for a decomposition, else an identity."""
    m = re.search(r"([a-z][a-z0-9_]*)\s*=\s*([^=\[\]]+)$", (e.label or "").strip().split("\n")[0])
    if m and m.group(1) == e.target and e.source in m.group(2):
        r = try_formula(w, e.target, m.group(2).strip())
        ev = {"expr": r.get("expr"), **(r.get("fit") or {}), "verdict": r.get("verdict"),
              "symbolic_identity": r.get("symbolic_identity"), "error": r.get("error")}
        if r.get("verdict") in ("exact", "close"):
            e.strength = 1.0
            return _decide(e, ts, "validated", f"{e.target} = {r.get('expr')} reconciles on {ev.get('n_hours')} hours "
                           f"(max residual {ev.get('max_rel_residual')})", ev)
        if r.get("verdict") == "proportional":
            return _decide(e, ts, "ambiguous", f"{e.target} is proportional to {r.get('expr')} "
                           f"(x{ev.get('ratio_median')}) but not equal: a scale factor or a missing term", ev)
        return _decide(e, ts, "rejected", f"{e.target} = {m.group(2).strip()} does not reproduce the data "
                       f"(max residual {ev.get('max_rel_residual')})", ev)
    for d in w.graph.decomps.get(e.target, []):
        if any(p == e.source for p, _ in d.parts):
            ev = {"decomposition": d.formula(), "valid": d.valid, "residual": d.residual, "checked_slices": d.checked}
            if d.valid:
                e.strength = 1.0
                e.label = e.label or f"{e.target} = {d.formula()}"
                return _decide(e, ts, "validated",
                               f"{e.source} is an input of {e.target} = {d.formula()} (residual {d.residual})", ev)
            if d.valid is False:
                return _decide(e, ts, "rejected",
                               f"decomposition {d.formula()} fails on the data (residual {d.residual})", ev)
    r = try_formula(w, e.target, e.source)   # single-input forms: identity or constant scale
    ev = {"tested": f"{e.target} ~ {e.source}", **(r.get("fit") or {}), "verdict": r.get("verdict")}
    if r.get("verdict") == "exact":
        e.strength = 1.0
        return _decide(e, ts, "validated", f"{e.target} equals {e.source} on the data (duplicate / identity)", ev)
    if r.get("verdict") == "proportional":
        return _decide(e, ts, "ambiguous",
                       f"{e.target} is a constant multiple of {e.source} (x{ev.get('ratio_median')})", ev)
    return _decide(e, ts, "rejected",
                   f"no formula of {e.target} contains {e.source} and no simple relation fits the data", ev)


# ====================================================================== localisation + timing evidence
@_memo
def localize(w, node: str, window_h: int = 3) -> list[dict]:
    """Adtributor-style: dims ranked by surprise, then the worst cell of the top-2 dims plus control cells (same
    value on one dim, another on the other) as independent evidence. Items: {type: dim|cell|control, ...}."""
    from argus.analytics.baseline import scan_rows
    dims = [d for d in w.engine.dims(node) if d not in ("sku", "seller", "advertiser")]
    ranked = []
    for d in dims:
        try:
            rows = breakdown(w, node, d, window_h)
        except Exception:
            continue
        if not rows:
            continue
        top = rows[0]
        surprise = sum(r.get("surprise") or 0.0 for r in rows)
        ranked.append({"type": "dim", "dim": d, "surprise": _r(surprise, 4), "top_segment": top["segment"],
                       "top_share_of_change": top["share_of_change"], "top_deviation_pct": top["deviation_pct"],
                       "n_segments": len(rows),
                       "concentrated": bool(top["share_of_change"] and top["share_of_change"] >= 60)})
    ranked.sort(key=lambda r: (-(r["surprise"] or 0), -(r["top_share_of_change"] or 0), r["dim"]))
    out = list(ranked)
    if len(ranked) >= 2:
        d1, d2 = ranked[0]["dim"], ranked[1]["dim"]
        try:
            rows = scan_rows(w.engine, node, (d1, d2), pd.Timestamp(w.now), window_h)
        except Exception:
            rows = pd.DataFrame()
        if not rows.empty:
            cells = []
            ratio = w.engine.shape(node).ratio_like
            vol_tot = float(np.nansum(rows["volume"].to_numpy(dtype=float))) or 1.0
            for _, r in rows.iterrows():
                a, e, v = _f(r["actual"]), _f(r["expected"]), _f(r["volume"]) or 0.0
                if a is None or not e or v < 0.01 * vol_tot:
                    continue
                cells.append({"segment": {d1: str(r["segment"][d1]), d2: str(r["segment"][d2])},
                              "value": _r(a, 6), "expected": _r(e, 6), "deviation_pct": _dev_pct(a, e),
                              "z": _r(r["z"], 3), "impact": _r((a - e) * (v if ratio else 1.0), 4)})
            s_dev = stats(w, node, window_h)["deviation_pct"] or 0.0
            sign = -1 if s_dev < 0 else 1
            cells.sort(key=lambda x: -(x["impact"] or 0) * sign)
            if cells:
                worst = cells[0]
                out.append({"type": "cell", **worst})
                for dim_keep, dim_vary in ((d1, d2), (d2, d1)):
                    ctrl = [x for x in cells if x["segment"][dim_keep] == worst["segment"][dim_keep]
                            and x["segment"][dim_vary] != worst["segment"][dim_vary]]
                    if ctrl:
                        best = min(ctrl, key=lambda x: abs(x["deviation_pct"] or 0))
                        out.append({"type": "control", "same": dim_keep, "varies": dim_vary, **best,
                                    "reading": (f"{dim_vary}={best['segment'][dim_vary]} within "
                                                f"{dim_keep}={worst['segment'][dim_keep]} moved "
                                                f"{best['deviation_pct']:+.1f}% vs {worst['deviation_pct']:+.1f}% "
                                                f"in the worst cell")})
    return out


_CHANGE_TABLES = (  # table, time column, name column, kind column
    ("events", "first_exposure_at", "name", "type"),
    ("calendar", "start", "name", "kind"),
    ("external_events", "start", "name", "kind"),
    ("status_feed", "ts", "title", "severity"),
)


def _naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize(None) if t.tzinfo is not None else t


def changes_near(w, ts, segment: dict | None = None, hours: int = 2) -> list[dict]:
    """Deploys, planned events, competitor actions, status posts and graph changes within +-hours of ts;
    segment_overlap is False when a row names a different country/platform/category than `segment`."""
    t0 = _naive(ts)
    lo, hi = t0 - pd.Timedelta(hours=hours), t0 + pd.Timedelta(hours=hours)
    seg = {k: str(v) for k, v in (segment or {}).items() if v not in (None, "*", "all")}
    out = []
    for table, tcol, ncol, kcol in _CHANGE_TABLES:
        df = w.wh.view(table)
        if df is None or df.empty or tcol not in df.columns:
            continue
        t = pd.to_datetime(df[tcol], errors="coerce")
        sub = df[(t >= lo) & (t <= hi)]
        for i, r in sub.iterrows():
            overlap = all(not isinstance(r.get(k), str) or r.get(k) in ("", "*", v)
                          for k, v in seg.items() if k in sub.columns)
            rts = pd.Timestamp(t.loc[i])
            item = {"table": table, "ts": rts.isoformat(), "name": str(r.get(ncol, "")),
                    "kind": str(r.get(kcol, "")), "delta_h": _r((rts - t0) / pd.Timedelta(hours=1), 3),
                    "before_onset": bool(rts <= t0), "segment_overlap": overlap}
            for k in ("component", "country", "platform", "category", "planned", "expected_impact", "metrics",
                      "region", "state", "competitor", "discount"):
                if k in sub.columns and pd.notna(r.get(k)):
                    v = r.get(k)
                    item[k] = v.item() if hasattr(v, "item") else (str(v) if not isinstance(v, (bool, int, float)) else v)
            out.append(item)
    for ch in load_changes():
        try:
            cts = _naive(ch.ts)
        except Exception:
            continue
        if lo <= cts <= hi and cts <= pd.Timestamp(w.now) + pd.Timedelta(hours=1):
            out.append({"table": "graph_changes", "ts": cts.isoformat(), "name": ch.reason[:120], "kind": ch.action,
                        "delta_h": _r((cts - t0) / pd.Timedelta(hours=1), 3), "before_onset": bool(cts <= t0),
                        "segment_overlap": True})
    out.sort(key=lambda x: (abs(x["delta_h"] or 0), x["ts"]))
    return out
