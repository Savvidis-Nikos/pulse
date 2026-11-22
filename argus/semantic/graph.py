"""The metric graph: a typed NetworkX MultiDiGraph built from the registry and validated on the data.

Nodes: metrics (by name), components (comp:<name>), data sources (src:<name>), dimensions (dim:<name>).
Edges (u -> v):
  formula    metric -> input metric. op: num | den | term | factor | share | identity | input.
             `decomp` names the decomposition the edge belongs to, `canonical` marks the one shown in the tree.
  breakdown  metric -> dim:<d>. additive (sums exactly) or mix_rate (ratio: Kitagawa mix/rate split).
  cause      cause metric -> effect metric. Possible mechanism, with sign, lag, join dims and validation status.
  component  comp:<c> -> metric. The component can move the metric directly (owners attached).
  source     metric -> src:<s>. Freshness of s gates the metric.
Formula identities are checked numerically on the total and on every single-dimension slice; invalid edges
are kept (marked valid=False) but never used for reasoning.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd
import sympy as sp
import yaml

from argus.semantic import expr as X
from argus.semantic.engine import MetricEngine
from argus.semantic.registry import Registry
from argus.settings import CONFIG_DIR, state, write_atomic

TOL = 1e-6
CHECK_HOURS = 72
DIMS_TO_CHECK = ("country", "platform", "category")


@dataclass
class Decomposition:
    metric: str
    kind: str                     # product | sum | share | ratio | identity
    parts: list[tuple[str, float]]  # (metric, exponent or coefficient)
    base: str | None = None       # product chains of additive metrics start from a base count
    canonical: bool = False
    valid: bool | None = None
    residual: float | None = None
    checked: int = 0
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.metric}:{self.kind}:" + ",".join(p for p, _ in self.parts)

    def formula(self) -> str:
        if self.kind == "ratio_expr" and self.note:
            return self.note
        if self.kind in ("product",):
            return " x ".join(p for p, _ in self.parts)
        if self.kind == "ratio":
            return f"{self.parts[0][0]} / {self.parts[1][0]}"
        terms = []
        for p, c in self.parts:
            name = "1" if p == "1" else p
            coef = "" if abs(abs(c) - 1) < 1e-12 or p == "1" else f"{abs(c):g}*"
            val = f"{abs(c):g}" if p == "1" else f"{coef}{name}"
            terms.append(("- " if c < 0 else "+ ") + val)
        s = " ".join(terms)
        return s[2:] if s.startswith("+ ") else "-" + s[2:]


@dataclass
class GraphReport:
    nodes: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)
    decompositions: list = field(default_factory=list)
    identities: list = field(default_factory=list)
    breakdowns: dict = field(default_factory=dict)
    alt_tables: list = field(default_factory=list)
    causes: list = field(default_factory=list)
    components: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class MetricGraph:
    def __init__(self, reg: Registry, engine: MetricEngine):
        self.reg = reg
        self.engine = engine
        self.G = nx.MultiDiGraph()
        self.decomps: dict[str, list[Decomposition]] = {}
        self.report = GraphReport()
        self.rate_graph = nx.DiGraph()

    # ================================================================== build
    @classmethod
    def build(cls, reg: Registry, engine: MetricEngine, validate: bool = True) -> "MetricGraph":
        g = cls(reg, engine)
        g._metric_nodes()
        g._definition_edges()
        g._rate_chains()
        g._share_decompositions()
        g._identities()
        g._breakdowns(validate)
        g._sources()
        g._components()
        g._causes(validate)
        if validate:
            g._validate_decompositions()
        g._formula_edges()
        g._summarize()
        return g

    def _metric_nodes(self):
        for m in self.reg:
            sh = self.engine.shape(m.name)
            self.G.add_node(m.name, kind="metric", type=m.type, domain=m.domain, tier=m.tier, unit=m.unit,
                            polarity=m.polarity, description=m.description, additive=sh.additive,
                            ratio_like=sh.ratio_like, dims=self.engine.dims(m.name), watch=m.watch,
                            added_by=m.added_by, time_agg=m.time_agg)

    def _add_decomp(self, d: Decomposition):
        self.decomps.setdefault(d.metric, []).append(d)

    def _definition_edges(self):
        names = set(self.reg.names())
        for m in self.reg:
            if m.type == "ratio":
                d = Decomposition(m.name, "ratio", [(m.numerator, 1.0), (m.denominator, -1.0)], canonical=True)
                self._add_decomp(d)
            elif m.type == "derived":
                e = X.definition(m, names)
                if X.is_linear(e):
                    parts = [(k, v) for k, v in X.linear_terms(e).items()]
                    self._add_decomp(Decomposition(m.name, "sum", parts, canonical=True))
                else:
                    num, den = X.fraction_parts(e)
                    parts = [(str(s), float(X.sign_of(e, s))) for s in sorted(e.free_symbols, key=str)]
                    self._add_decomp(Decomposition(m.name, "ratio_expr", parts, canonical=True,
                                                   note=f"{X.pretty(num)} / {X.pretty(den)}"))

    # ---------------------------------------------------------------- product chains (telescoping rates)
    def _rate_chains(self):
        RG = self.rate_graph
        for m in self.reg:
            if m.type != "ratio" or m.numerator == m.denominator:
                continue
            if not (self.engine.shape(m.numerator).additive and self.engine.shape(m.denominator).additive):
                continue
            if RG.has_edge(m.denominator, m.numerator):
                self.report.warnings.append(f"two rates over the same pair: {m.name} and "
                                            f"{RG[m.denominator][m.numerator]['rate']}")
                continue
            RG.add_edge(m.denominator, m.numerator, rate=m.name)
        roots = [n for n in RG if RG.in_degree(n) == 0]
        depth: dict[str, int] = {}
        for r in roots:
            for n, d in nx.single_source_shortest_path_length(RG, r).items():
                depth[n] = min(depth.get(n, 10**6), d)

        def watched(path):
            return sum(1 for a, b in zip(path, path[1:]) if self.reg[RG[a][b]["rate"]].watch)

        # ratio R = N / D  ->  R = r1 x r2 x ... along paths D -> ... -> N (length >= 2)
        for m in self.reg:
            if m.type != "ratio" or not RG.has_edge(m.denominator, m.numerator):
                continue
            paths = [p for p in nx.all_simple_paths(RG, m.denominator, m.numerator, cutoff=8) if len(p) >= 3]
            if not paths:
                continue
            paths.sort(key=lambda p: (len(p), -watched(p), p))
            for i, p in enumerate(paths[:4]):
                parts = [(RG[a][b]["rate"], 1.0) for a, b in zip(p, p[1:])]
                self._add_decomp(Decomposition(m.name, "product", parts, canonical=(i == 0)))
        # additive X with an incoming rate: X = B x rate, choosing B closest to a traffic root
        for x in RG:
            if RG.in_degree(x) == 0:
                continue
            preds = sorted(RG.predecessors(x), key=lambda b: (depth.get(b, 99), -self.reg[RG[b][x]["rate"]].watch, b))
            for i, b in enumerate(preds):
                self._add_decomp(Decomposition(x, "product", [(b, 1.0), (RG[b][x]["rate"], 1.0)], base=b,
                                               canonical=(i == 0)))

    def _share_decompositions(self):
        """R = N / D with linear N = sum c_i N_i  ->  R = sum c_i (N_i / D) when every N_i / D is a metric."""
        by_pair = {(m.numerator, m.denominator): m.name for m in self.reg if m.type == "ratio"}
        names = set(self.reg.names())
        for m in self.reg:
            if m.type != "ratio":
                continue
            nspec = self.reg[m.numerator]
            if nspec.type != "derived":
                continue
            e = X.definition(nspec, names)
            if not X.is_linear(e):
                continue
            parts = []
            for term, coef in X.linear_terms(e).items():
                if term == m.denominator:
                    parts.append(("1", coef))
                elif (term, m.denominator) in by_pair:
                    parts.append((by_pair[(term, m.denominator)], coef))
                else:
                    parts = None
                    break
            if parts:
                self._add_decomp(Decomposition(m.name, "share", parts, canonical=False,
                                               note=f"shares of {m.denominator}"))

    def _identities(self):
        path = CONFIG_DIR / "identities.yaml"
        items = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
        names = set(self.reg.names())
        for it in items or []:
            try:
                rhs = X.parse(it["rhs"], names)
            except ValueError as exc:
                self.report.warnings.append(f"identity {it['name']}: {exc}")
                continue
            if it["lhs"] not in names:
                self.report.warnings.append(f"identity {it['name']}: unknown metric {it['lhs']}")
                continue
            parts = list(X.linear_terms(rhs).items())
            d = Decomposition(it["lhs"], "identity", parts, canonical=True, note=it.get("note", ""))
            self._add_decomp(d)
        # identities discovered by the cartographer (symbolically derived, numerically validated on load)
        learned = state("learned_identities.json")
        if learned.exists():
            for it in json.loads(learned.read_text(encoding="utf-8")):
                if it["lhs"] in names and all(p in names or p == "1" for p, _ in it["parts"]):
                    self._add_decomp(Decomposition(it["lhs"], it["kind"], [tuple(p) for p in it["parts"]],
                                                   canonical=False, note=it.get("note", "learned")))

    # ---------------------------------------------------------------- breakdowns, sources, components, causes
    def _breakdowns(self, validate: bool):
        for m in self.reg:
            sh = self.engine.shape(m.name)
            for dim in self.engine.dims(m.name):
                self.G.add_node(f"dim:{dim}", kind="dimension", label=dim)
                method = "additive" if sh.additive else ("mix_rate" if sh.ratio_like else "non_additive")
                self.G.add_edge(m.name, f"dim:{dim}", key="breakdown", kind="breakdown", method=method, valid=True)
        if not validate:
            return
        # additivity check on a sample of watched additive metrics, plus alt-table consistency
        end = self.engine.wh.now
        start = end - pd.Timedelta(hours=CHECK_HOURS - 1)
        for m in self.reg:
            sh = self.engine.shape(m.name)
            if not sh.additive or m.type != "simple":
                continue
            total = self.engine.hourly(m.name, (), None, start, end).set_index("ts")["value"]
            for dim in self.engine.dims(m.name):
                parts = self.engine.hourly(m.name, (dim,), None, start, end).groupby("ts")["value"].sum()
                res = _rel_residual(total, parts)
                ok = res is None or res < TOL
                self.G.edges[m.name, f"dim:{dim}", "breakdown"]["valid"] = ok
                self.G.edges[m.name, f"dim:{dim}", "breakdown"]["residual"] = res
                if not ok:
                    self.report.warnings.append(f"breakdown {m.name} by {dim} does not add up (residual {res:.2e})")
            for alt in m.alt_tables:
                df = self.engine.wh.frame(alt)
                df = df[(df["ts"] >= start) & (df["ts"] <= end)]
                if m.column not in df.columns:
                    continue
                res = _rel_residual(total, df.groupby("ts")[m.column].sum())
                self.report.alt_tables.append({"metric": m.name, "table": alt, "residual": res,
                                               "valid": res is None or res < TOL})
        self.report.breakdowns = {"checked_metrics": sum(1 for m in self.reg if self.engine.shape(m.name).additive
                                                         and m.type == "simple")}

    def _sources(self):
        for m in self.reg:
            for s in sorted(self.engine.sources(m.name)):
                self.G.add_node(f"src:{s}", kind="source", label=s)
                self.G.add_edge(m.name, f"src:{s}", key="source", kind="source")

    def _components(self):
        comps = yaml.safe_load((CONFIG_DIR / "components.yaml").read_text(encoding="utf-8")) or {}
        for name, c in comps.items():
            node = f"comp:{name}"
            self.G.add_node(node, kind="component", label=name, description=c.get("description", ""),
                            owners=c.get("owners", []), aliases=c.get("aliases", []), services=c.get("services", []),
                            signals=[m for m in c.get("signals", []) if m in self.reg], max_lag_h=float(c.get("max_lag_h", 0) or 0))
            for metric in c.get("metrics", []):
                if metric not in self.reg:
                    self.report.warnings.append(f"component {name}: unknown metric {metric}")
                    continue
                self.G.add_edge(node, metric, key="component", kind="component")
        for m in self.reg:  # hints declared on metrics (e.g. by the cartographer)
            for c in m.components:
                if f"comp:{c}" in self.G:
                    self.G.add_edge(f"comp:{c}", m.name, key="component", kind="component")

    def _causes(self, validate: bool):
        items = yaml.safe_load((CONFIG_DIR / "causes.yaml").read_text(encoding="utf-8")) or []
        learned = state("learned_causes.json")
        if learned.exists():
            items += json.loads(learned.read_text(encoding="utf-8"))
        for it in items:
            c, e = it["cause"], it["effect"]
            if c not in self.reg or e not in self.reg:
                self.report.warnings.append(f"cause {c} -> {e}: unknown metric")
                continue
            shared = [d for d in self.engine.dims(c) if d in self.engine.dims(e)]
            join = [d for d in it.get("join", []) if d in shared]
            status = it.get("status", "asserted")
            note = ""
            if not shared:
                status, note = "rejected", "no shared dimension: the two metrics cannot be aligned on any segment"
            elif len(join) < len(it.get("join", [])):
                note = f"join reduced to {join or shared}"
            attrs = dict(kind="cause", sign=int(it.get("sign", 1)), lag_h=int(it.get("lag_h", 0)),
                         join=join or shared, mechanism=it.get("mechanism", ""), status=status, note=note,
                         valid=status != "rejected", origin=it.get("origin", "seed"))
            if validate and attrs["valid"]:
                corr = self._residual_corr(c, e, attrs["lag_h"])
                attrs["data_corr"] = corr
                attrs["data_support"] = ("untested" if corr is None else "weak" if abs(corr) < 0.2 else
                                         "consistent" if corr * attrs["sign"] > 0 else "contradicted")
            self.G.add_edge(c, e, key=f"cause:{c}->{e}", **attrs)

    def _residual_corr(self, a: str, b: str, lag: int) -> float | None:
        """Correlation of de-seasonalised hourly residuals (b lagged behind a). Weak evidence only."""
        try:
            ra = _seasonal_residual(self.engine.total_series(a))
            rb = _seasonal_residual(self.engine.total_series(b))
        except Exception:
            return None
        df = pd.concat([ra, rb.shift(-lag)], axis=1).dropna()
        if len(df) < 48 or df.iloc[:, 0].std() == 0 or df.iloc[:, 1].std() == 0:
            return None
        return round(float(df.corr().iloc[0, 1]), 3)

    # ---------------------------------------------------------------- numeric validation of formulas
    def _validate_decompositions(self):
        end = self.engine.wh.now
        start = end - pd.Timedelta(hours=CHECK_HOURS - 1)
        for metric, ds in self.decomps.items():
            for d in ds:
                if d.kind in ("ratio", "ratio_expr", "sum") and d.canonical and self.reg[metric].type != "simple":
                    d.valid, d.residual, d.note = True, 0.0, d.note or "definition"
                    continue
                worst, checked = 0.0, 0
                segments = [None] + [{dim: v} for dim in DIMS_TO_CHECK for v in self._values(metric, dim)]
                for seg in segments:
                    try:
                        res = self._residual(d, seg, start, end)
                    except KeyError:
                        continue
                    if res is None:
                        continue
                    worst = max(worst, res)
                    checked += 1
                d.checked = checked
                d.residual = worst if checked else None
                d.valid = bool(checked) and worst < (TOL if d.kind != "identity" else 1e-4)
        # a canonical decomposition that failed validation hands over to the next valid one
        for metric, ds in self.decomps.items():
            prod = [d for d in ds if d.kind == "product"]
            if prod and not any(d.canonical and d.valid for d in prod):
                for d in prod:
                    d.canonical = False
                for d in prod:
                    if d.valid:
                        d.canonical = True
                        break

    def _formula_edges(self):
        for metric, ds in self.decomps.items():
            for d in ds:
                if d.valid is None and d.kind in ("ratio", "ratio_expr", "sum") and d.canonical:
                    d.valid = True  # definitions hold by construction
                for part, coef in d.parts:
                    if part == "1" or part not in self.reg:
                        continue
                    op = {"ratio": "num" if coef > 0 else "den", "product": "factor", "sum": "term",
                          "share": "share", "identity": "identity", "ratio_expr": "input"}[d.kind]
                    self.G.add_edge(metric, part, key=f"formula:{d.id}:{part}", kind="formula", op=op,
                                    sign=1 if coef > 0 else -1, coef=coef, decomp=d.id,
                                    canonical=d.canonical, valid=d.valid is not False, residual=d.residual)

    def _values(self, metric: str, dim: str) -> list:
        if dim not in self.engine.dims(metric):
            return []
        df = self.engine.hourly(metric, (dim,))
        return sorted(df[dim].dropna().astype(str).unique().tolist())[:12]

    def _residual(self, d: Decomposition, seg, start, end) -> float | None:
        target = self.engine.hourly(d.metric, (), seg, start, end).set_index("ts")["value"]
        if d.kind == "product":
            acc = None
            for p, _ in d.parts:
                s = self.engine.hourly(p, (), seg, start, end).set_index("ts")["value"]
                acc = s if acc is None else acc * s
            pred = acc
        else:
            pred = None
            for p, c in d.parts:
                s = (pd.Series(1.0, index=target.index) if p == "1"
                     else self.engine.hourly(p, (), seg, start, end).set_index("ts")["value"])
                pred = c * s if pred is None else pred.add(c * s, fill_value=0)
        return _rel_residual(target, pred)

    def _summarize(self):
        kinds: dict[str, int] = {}
        for _, data in self.G.nodes(data=True):
            kinds[data["kind"]] = kinds.get(data["kind"], 0) + 1
        ek: dict[str, int] = {}
        for _, _, data in self.G.edges(data=True):
            ek[data["kind"]] = ek.get(data["kind"], 0) + 1
        self.report.nodes, self.report.edges = kinds, ek
        self.report.decompositions = [
            {"metric": d.metric, "kind": d.kind, "formula": d.formula(), "canonical": d.canonical, "valid": d.valid,
             "residual": d.residual, "checked_slices": d.checked, "note": d.note}
            for ds in self.decomps.values() for d in ds if d.kind in ("product", "share", "identity")]
        self.report.causes = [{"cause": u, "effect": v, "status": a["status"], "join": a["join"], "note": a["note"],
                               "data_corr": a.get("data_corr"), "data_support": a.get("data_support"), "sign": a["sign"]}
                              for u, v, a in self.G.edges(data=True) if a["kind"] == "cause"]

    # ================================================================== queries
    def metrics(self) -> list[str]:
        return [n for n, a in self.G.nodes(data=True) if a["kind"] == "metric"]

    def edges(self, kind: str, node: str | None = None, out: bool = True, valid_only: bool = True):
        it = self.G.out_edges(node, data=True) if (node and out) else (
            self.G.in_edges(node, data=True) if node else self.G.edges(data=True))
        return [(u, v, a) for u, v, a in it if a["kind"] == kind and (a.get("valid", True) or not valid_only)]

    def canonical(self, metric: str) -> Decomposition | None:
        """The decomposition shown in the tree.
        derived -> its definition; stored measures -> a validated identity, else a product chain;
        ratios -> a product chain of finer rates, else shares of the denominator, else the definition."""
        ds = [d for d in self.decomps.get(metric, []) if d.valid is not False]
        spec = self.reg[metric]
        if spec.type == "derived":
            order = ("sum", "ratio_expr", "product")
        elif spec.type == "ratio":
            order = ("product", "share", "ratio")
        else:
            order = ("identity", "product")
        for kind in order:
            for d in ds:
                if d.kind == kind and (d.canonical or kind == "share"):
                    return d
        return None

    def expandable(self, metric: str) -> bool:
        """Does the tree open this node? Atomic ratios (plain num/den) are leaves."""
        d = self.canonical(metric)
        return d is not None and d.kind != "ratio"

    def factorization(self, metric: str, _seen=None) -> list[str]:
        """Flatten canonical product chains to atomic factors, e.g. gmv -> [sessions, search_rate, ..., aov]."""
        seen = set(_seen or ())
        if metric in seen:
            return [metric]
        seen.add(metric)
        d = next((d for d in self.decomps.get(metric, []) if d.kind == "product" and d.canonical and d.valid), None)
        if d is None:
            return [metric]
        out: list[str] = []
        for p, _ in d.parts:
            out += self.factorization(p, seen)
        return out

    def formula_children(self, metric: str) -> list[tuple[str, dict]]:
        d = self.canonical(metric)
        if d is None:
            return []
        return [(p, {"op": d.kind, "coef": c}) for p, c in d.parts if p != "1" and p in self.reg]

    def causes_of(self, metric: str) -> list[tuple[str, dict]]:
        return [(u, a) for u, _, a in self.edges("cause", metric, out=False)]

    def effects_of(self, metric: str) -> list[tuple[str, dict]]:
        return [(v, a) for _, v, a in self.edges("cause", metric, out=True)]

    def components_of(self, metric: str) -> list[str]:
        return [u.split(":", 1)[1] for u, _, _ in self.edges("component", metric, out=False)]

    def component(self, comp: str) -> dict:
        """Attributes of a system component (description, owners, aliases, services, signals)."""
        return dict(self.G.nodes[f"comp:{comp}"]) if f"comp:{comp}" in self.G else {}

    def signals_of(self, comp: str) -> list[str]:
        """The component's own telemetry: metrics that move when the component itself acts."""
        return list(self.component(comp).get("signals", []))

    def metrics_of_component(self, comp: str) -> list[str]:
        return [v for _, v, _ in self.edges("component", f"comp:{comp}", out=True)]

    def impact_graph(self) -> nx.DiGraph:
        """Direction of propagation: input -> output (formula reversed) and cause -> effect."""
        D = nx.DiGraph()
        D.add_nodes_from(self.metrics())
        for u, v, a in self.G.edges(data=True):
            if not a.get("valid", True):
                continue
            if a["kind"] == "formula" and a["op"] in ("num", "den", "factor", "term", "input", "identity"):
                D.add_edge(v, u, kind="formula")
            elif a["kind"] == "cause":
                D.add_edge(u, v, kind="cause")
        return D

    def downstream(self, metric: str) -> set[str]:
        D = self.impact_graph()
        return nx.descendants(D, metric) if metric in D else set()

    def upstream(self, metric: str) -> set[str]:
        D = self.impact_graph()
        return nx.ancestors(D, metric) if metric in D else set()

    def neighbors(self, metric: str) -> dict:
        return {
            "metric": metric,
            "definition": (self.canonical(metric).formula() if self.canonical(metric) else "base measure"),
            "inputs": [p for p, _ in self.formula_children(metric)],
            "used_by": sorted({u for u, _, a in self.edges("formula", metric, out=False)}),
            "causes": [{"metric": u, "sign": a["sign"], "lag_h": a["lag_h"], "mechanism": a["mechanism"],
                        "join": a["join"], "status": a["status"]} for u, a in self.causes_of(metric)],
            "effects": [{"metric": v, "sign": a["sign"], "mechanism": a["mechanism"]} for v, a in self.effects_of(metric)],
            "components": self.components_of(metric),
            "dims": self.G.nodes[metric]["dims"],
            "sources": [v.split(":", 1)[1] for _, v, _ in self.edges("source", metric)],
        }

    def path(self, a: str, b: str) -> list[str] | None:
        D = self.impact_graph().to_undirected()
        try:
            return nx.shortest_path(D, a, b)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    # ================================================================== tree view
    def roots(self) -> list[str]:
        """North-star metrics, then outcome metrics that no north-star tree reaches."""
        roots = [m.name for m in self.reg.by_tier("north_star")]
        reached: set[str] = set()
        for r in roots:
            reached |= _tree_members(self.tree(r))
        for m in self.reg.by_tier("outcome"):
            if m.name not in reached:
                roots.append(m.name)
                reached |= _tree_members(self.tree(m.name))
        return roots

    def tree(self, root: str, max_depth: int = 7, expanded: set | None = None) -> dict:
        """Nested decomposition tree. Each metric is opened once (later occurrences are references)."""
        expanded = set() if expanded is None else expanded

        def node(m: str, depth: int, path: frozenset) -> dict:
            d = self.canonical(m)
            out = {"metric": m, "formula": d.formula() if d else None, "kind": d.kind if d else "base",
                   "children": [], "causes": [u for u, _ in self.causes_of(m)], "ref": False}
            if not self.expandable(m) or depth >= max_depth or m in path:
                return out
            if m in expanded:
                out["ref"] = True
                return out
            expanded.add(m)
            for p, _ in d.parts:
                if p != "1" and p in self.reg:
                    out["children"].append(node(p, depth + 1, path | {m}))
            return out
        return node(root, 0, frozenset())

    def render_tree(self, root: str, max_depth: int = 7, show_causes: bool = True, expanded: set | None = None) -> str:
        lines: list[str] = []

        def walk(n: dict, prefix: str, last: bool, top: bool):
            m = n["metric"]
            head = "" if top else (prefix + ("└─ " if last else "├─ "))
            f = f" = {n['formula']}" if n["formula"] else ""
            if n["kind"] == "share":
                f += "   (shares)"
            if n["ref"]:
                f += "   (expanded above)"
            cause = f"   <- {', '.join(n['causes'][:4])}" if show_causes and n["causes"] else ""
            lines.append(f"{head}{m} [{self.reg[m].tier}]{f}{cause}")
            kids = n["children"]
            child_prefix = "" if top else prefix + ("   " if last else "│  ")
            for i, k in enumerate(kids):
                walk(k, child_prefix, i == len(kids) - 1, False)

        walk(self.tree(root, max_depth, expanded), "", True, True)
        return "\n".join(lines)

    # ================================================================== export
    def to_json(self) -> dict:
        nodes = [{"id": n, **{k: v for k, v in a.items() if _jsonable(v)}} for n, a in self.G.nodes(data=True)]
        edges = [{"source": u, "target": v, **{k: val for k, val in a.items() if _jsonable(val)}}
                 for u, v, a in self.G.edges(data=True)]
        return {"nodes": nodes, "edges": edges,
                "decompositions": self.report.decompositions,
                "trees": {r: self.tree(r) for r in self.roots()},
                "factorizations": {m: self.factorization(m) for m in self.metrics()
                                   if len(self.factorization(m)) > 1}}

    def save(self) -> None:
        write_atomic(state("graph.json"), json.dumps(self.to_json(), indent=1, default=str))

    def validation_text(self) -> str:
        r = self.report
        out = [f"nodes: {r.nodes}", f"edges: {r.edges}", ""]
        out.append("formula decompositions (validated on total + every country/platform/category slice, last 72 h):")
        alts: dict[str, int] = {}
        for d in r.decompositions:
            if not d["canonical"] and d["kind"] == "product":
                alts[d["metric"]] = alts.get(d["metric"], 0) + 1
        for d in r.decompositions:
            if not d["canonical"] and d["kind"] == "product":
                continue
            if d["metric"] in alts and d["kind"] == "product":
                d = dict(d, note=(d["note"] + "; " if d["note"] else "") + f"+{alts[d['metric']]} alternative chain(s)")
            flag = "OK " if d["valid"] else ("-- " if d["valid"] is None else "BAD")
            res = "n/a" if d["residual"] is None else f"{d['residual']:.1e}"
            canon = "*" if d["canonical"] else " "
            out.append(f"  {flag}{canon} {d['metric']:22s} = {d['formula']}   (residual {res}, {d['checked_slices']} slices)"
                       + (f"  [{d['note']}]" if d["note"] else ""))
        out.append("")
        out.append("alternative-table consistency (audience vs facts):")
        for a in r.alt_tables:
            res = "n/a" if a["residual"] is None else f"{a['residual']:.1e}"
            out.append(f"  {'OK ' if a['valid'] else 'BAD'} {a['metric']:22s} {a['table']}  residual {res}")
        bad_bd = [(u, v, a) for u, v, a in self.G.edges(data=True) if a["kind"] == "breakdown" and not a["valid"]]
        out.append(f"\nbreakdowns: {r.edges.get('breakdown', 0)} edges, {len(bad_bd)} not additive as declared")
        out.append("\ncause edges (asserted mechanisms; data_corr = residual correlation over visible history):")
        for c in r.causes:
            corr = "" if c["data_corr"] is None else f" corr {c['data_corr']:+.2f} ({c['data_support']})"
            out.append(f"  {c['status']:9s} {c['cause']} -> {c['effect']} (sign {c['sign']:+d}, join {c['join']}){corr}"
                       + (f"  [{c['note']}]" if c["note"] else ""))
        if r.warnings:
            out.append("\nwarnings:")
            out += [f"  - {w}" for w in r.warnings]
        return "\n".join(out)


# ---------------------------------------------------------------------- helpers
def _tree_members(t: dict) -> set[str]:
    out = {t["metric"]}
    for c in t["children"]:
        out |= _tree_members(c)
    return out


def _rel_residual(a: pd.Series, b: pd.Series) -> float | None:
    df = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    df = df[np.isfinite(df["a"]) & np.isfinite(df["b"])]
    if df.empty:
        return None
    scale = df["a"].abs().where(df["a"].abs() > 1e-9, 1.0)
    return float(((df["a"] - df["b"]).abs() / scale).max())


def _seasonal_residual(s: pd.Series) -> pd.Series:
    """Value minus the median of the same hour over the previous 7 days, scaled by that median."""
    s = s.astype(float)
    lags = pd.concat([s.shift(24 * k) for k in range(1, 8)], axis=1)
    med = lags.median(axis=1)
    return ((s - med) / med.abs().replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)


def _jsonable(v) -> bool:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return False
    return isinstance(v, (str, int, float, bool, list, type(None))) or isinstance(v, dict)
