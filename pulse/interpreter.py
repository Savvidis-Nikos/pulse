"""Graph Interpreter agent (Sonnet 5.5): walks the annotated metric graph around one KPI with tools and explains the
move in plain English: where it sits, the first cause, which moves are symptoms, data issues or planned events, and
what remains uncertain.

    it = interpret(world(), "orders", incident_id="inc-orders-1")   # -> contracts.Interpretation

Writes (under PULSE_STATE) snapshots/<snapshot_id>.json (re-saved with node roles and the path's segment / event
nodes), interpretations/<id>.partial.json (after every trace step, for live animation) and interpretations/<id>.json.
Every tool call becomes a TraceStep whose `finding` sentence is computed here from the tool numbers. In mock mode
(default) a deterministic policy performs the same walk and writes a template answer with real numbers.

CLI:  python -m pulse.interpreter orders --at "2026-11-17 18:00"
"""
from __future__ import annotations

import argparse
import json
import math
import re
from typing import Any, Callable, Optional

import pandas as pd

from pulse import contracts as C
from pulse.contracts import Edge, Finding, GraphSnapshot, Interpretation, Node, TraceStep
from pulse.llm import Tool, mode, obj, run_agent

ROLE = "interpreter"
MAX_TURNS = 8
TOOL_CHARS = 1500
BEAM = 4                       # neighbours shown per node and hop
EXPLAINED_MIN = 70.0           # stop rule: share of the KPI change the explanation must cover
COMPLETE_MIN = 0.9
DIMS = ("country", "platform", "category")
INNOCENT_TYPES = {"campaign", "maintenance", "public_holiday", "tv_event", "holiday", "catalog", "launch"}
COUNTRY = {"DE": "Germany", "FR": "France", "NL": "the Netherlands", "PL": "Poland", "ES": "Spain", "IT": "Italy",
           "AT": "Austria", "BE": "Belgium", "UK": "the UK", "GB": "the UK"}
PLATFORM = {"android": "Android", "ios": "iOS", "web": "web", "app": "the app"}
POLARITY = {"up_is_good": 1, "down_is_good": -1}

# ---------------------------------------------------------------- prompt and output schema (stable -> cached)

SYSTEM = """You are the Graph Interpreter of Pulse, monitoring an online marketplace. A CEO KPI moved. Explain the move
by walking the typed metric graph with tools, then answer for the CEO.

Graph text
- One block per node: "id = formula | value vs expected (DOWN/UP x%, z, BAD/good for business), onset, own x% /
  inherited y%, data z% complete". own = part of the move not explained by the node's inputs/causes; a node whose
  move is mostly inherited is a symptom. Direction and business verdict are already computed: do not re-derive them.
- "a -input(op), carries N% of change-> b": formula fact; N = share of b's move carried by a.
- "a -causes(sign, lag, status)-> b": possible mechanism, a HYPOTHESIS until timing and segment match.
- Only the top neighbours per hop are shown; expand(node) shows more.

Method
1. Follow formula inputs that carry the change, downwards, while they move in the same direction.
2. localize (or breakdown) the deepest moving node: which segment carries the change, is the control (rest) normal?
3. Test cause edges into it (cause must move with the edge's sign) and recent_changes (releases, campaigns, calendar).
4. Keep a ledger of competing hypotheses: fault on the path, traffic or mix change, data issue, planned/innocent
   event. Make at least one tool call that tests a rival before concluding.
5. First cause = the deepest node on the path with a high own share (or the change event that started it).
   Everything above it on the path is a symptom.
6. Stop when the explanation covers >=70% of the KPI change AND >=2 independent evidence types agree (timing match,
   segment localisation, normal control, own-change share, moving cause metric): confidence "supported". Fewer:
   "hypothesis". Data <90% complete on the path: stop, "data_issue". Nothing found: "unexplained".
   A planned event with a positive net business outcome (e.g. conversion down but orders up) is innocent.
7. If the graph cannot explain it, search_data + propose_connection (validated on the data by the graph handler).
Budget ~8 tool calls.

Answer rules
- Numbers only from the graph text or tool results.
- path: node ids from the KPI to the first cause (may end on a segment id "metric|country=DE" or "event:<name>").
- headline <= 15 words. summary 2-4 plain sentences, no jargon: what (KPI + impact in EUR or orders per hour) ->
  where (segment) -> why (first cause + 2 pieces of evidence) -> how sure.
- hypotheses: the ledger, verdict supported | rejected | open. uncertainty: what could change the conclusion.
  watch_next: node ids.
"""

_S = {"type": "string"}
_STRS = {"type": "array", "items": _S}
_FINDING = obj({"node": _S, "why": _S})
_FLIST = {"type": "array", "items": _FINDING}
_HYP = obj({"name": _S, "verdict": {"type": "string", "enum": ["supported", "rejected", "open"]}, "evidence": _S})
SCHEMA = obj({"headline": _S, "summary": _S, "path": _STRS, "first_cause": _FINDING,
              "confidence": {"type": "string", "enum": ["supported", "hypothesis", "data_issue", "unexplained"]},
              **dict.fromkeys(("symptoms", "unusual", "missing_data", "ruled_out"), _FLIST),
              "hypotheses": {"type": "array", "items": _HYP}, "uncertainty": _S, "watch_next": _STRS})

# ---------------------------------------------------------------- small helpers


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def _f(v: Any, unit: str | None = None) -> str:
    """Short human number: '2.08%', '1,138', '12.3k', 'EUR 5.0k'."""
    v = _num(v)
    if v is None:
        return "n/a"
    if unit == "ratio" or (unit is None and 0 < abs(v) < 1):
        return f"{v * 100:.2f}%" if abs(v) < 0.1 else f"{v * 100:.1f}%"
    a = abs(v)
    s = f"{v / 1e6:.2f}M" if a >= 1e6 else f"{v / 1e3:.1f}k" if a >= 1e4 else f"{v:,.0f}" if a >= 100 else f"{v:.3g}"
    return f"EUR {s}" if unit == "EUR" else s


def _pct(v: Any, signed: bool = True) -> str:
    v = _num(v)
    return "n/a" if v is None else f"{v:+.1f}%" if signed else f"{abs(v):.1f}%"


def _below(dev: Any) -> str:
    """'14.0% below normal' / '3.2% above normal'."""
    return f"{_pct(dev, False)} {'below' if (dev or 0) < 0 else 'above'} normal"


def _r(v: Any) -> Any:
    """JSON-safe, rounded copy (drops '_' keys)."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else float(f"{v:.4g}")
    if isinstance(v, dict):
        return {str(k): _r(x) for k, x in v.items() if not str(k).startswith("_")}
    if isinstance(v, (list, tuple)):
        return [_r(x) for x in v]
    if hasattr(v, "item"):
        try:
            return _r(v.item())
        except Exception:
            return str(v)
    return v if isinstance(v, int) else str(v)


def _compact(x: Any) -> str:
    """Tool result: compact JSON, truncated to TOOL_CHARS."""
    s = json.dumps(_r(x), separators=(",", ":"), default=str)
    return s if len(s) <= TOOL_CHARS else s[: TOOL_CHARS - 16] + "...(truncated)"


def _sgn(x: Any) -> int:
    x = _num(x)
    return 0 if not x else (1 if x > 0 else -1)


def _seg_text(seg: dict) -> str:
    """{'country': 'DE', 'platform': 'android'} -> 'Android in Germany'."""
    parts = [PLATFORM.get(str(seg["platform"]).lower(), str(seg["platform"]))] if seg.get("platform") else []
    parts += [str(seg["category"])] if seg.get("category") else []
    parts += [str(v) for k, v in seg.items() if k not in ("platform", "category", "country")]
    s = " ".join(parts)
    if seg.get("country"):
        c = COUNTRY.get(str(seg["country"]).upper(), str(seg["country"]))
        s = f"{s} in {c}" if s else c
    return s


def _seg_id(metric: str, seg: dict) -> str:
    return metric + "|" + "|".join(f"{k}={seg[k]}" for k in sorted(seg))


def _parse_seg(segment: Any) -> dict:
    """'country=DE,platform=android' (or a dict) -> {'country': 'DE', 'platform': 'android'}."""
    if isinstance(segment, dict):
        return {k: str(v) for k, v in segment.items() if v not in (None, "", "*")}
    out = {}
    for part in re.split(r"[,;&|]", str(segment or "")):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip() not in ("", "*"):
                out[k.strip()] = v.strip()
    return out


def _anomalous(n: Optional[Node]) -> bool:
    return n is not None and (n.status == "anomalous" or (n.status == "ok" and abs(n.z or 0) >= 3.0))


def _data_bad(n: Optional[Node]) -> bool:
    return n is not None and (n.status in ("missing_data", "stale") or
                              (n.completeness is not None and n.completeness < COMPLETE_MIN))


def _biz(n: Node) -> int:
    """+1 a good move for the business, -1 a bad one, 0 flat or neutral polarity."""
    return POLARITY.get(n.polarity or "", 0) * _sgn(n.deviation_pct)


def _verdict(n: Node) -> str:
    if not _sgn(n.deviation_pct):
        return "flat"
    if n.status == "ok" and abs(n.z or 0) < 2:
        return "within normal range"
    if n.polarity not in POLARITY:
        return "neutral direction"
    return "BAD for business" if _biz(n) < 0 else "good for business"


def _own_share(n: Optional[Node]) -> Optional[float]:
    """Share (0-100) of the node's move not inherited from its inputs."""
    if n is None:
        return None
    if n.inherited_pct is not None:
        return max(0.0, min(100.0, 100.0 - n.inherited_pct))
    if n.own_deviation_pct is not None and n.deviation_pct:
        return max(0.0, min(100.0, 100.0 * n.own_deviation_pct / n.deviation_pct))
    return None


def _hhmm(ts: Any) -> str:
    try:
        return f"{pd.Timestamp(ts):%m-%d %H:%M}"
    except Exception:
        return str(ts)

# ---------------------------------------------------------------- graph text (encoding + beam pruning)


def node_line(n: Node) -> str:
    """'orders = sessions x conversion_rate | 1,138 vs 1,323 expected (DOWN 14.0%, z -2.3, BAD for business), ...'"""
    head = n.id
    if n.definition:
        d = n.definition.split("  ")[-1].strip("(= )") if "  =  " in n.definition else n.definition.split("  (")[0]
        head += " = " + d.replace(" x ", " × ")[:70]
    bits = []
    if n.value is not None or n.expected is not None:
        mv = ""
        if n.deviation_pct is not None:
            mv = f"{'DOWN' if n.deviation_pct < 0 else 'UP'} {abs(n.deviation_pct):.1f}%"
            if n.z is not None:
                mv += f", z {n.z:+.1f}"
            mv += f", {_verdict(n)}"
        bits.append(f"{_f(n.value, n.unit)} vs {_f(n.expected, n.unit)} expected" + (f" ({mv})" if mv else ""))
    if n.onset_ts:
        bits.append(f"onset {_hhmm(n.onset_ts)}")
    own = _own_share(n)
    if own is not None:
        bits.append(f"own {own:.0f}% / inherited {100 - own:.0f}%")
    if n.completeness is not None:
        bits.append(f"data {n.completeness * 100:.0f}% complete")
    if n.status in ("missing_data", "stale", "unknown"):
        bits.append(f"STATUS {n.status.upper()}")
    if n.formula_status in ("incomplete", "broken"):
        bits.append(f"formula {n.formula_status}")
    return head + " | " + ", ".join(bits)


def edge_line(e: Edge) -> str:
    if e.kind == "formula":
        rel = f"input({e.op or '?'})" + (f", carries {e.contribution_pct:.0f}% of change" if e.contribution_pct
                                         is not None else "")
    elif e.kind == "cause":
        rel = f"causes({'+' if (e.sign or 1) > 0 else '-'}, lag {e.lag_h or 0}h, {e.status})"
    elif e.kind == "breakdown":
        rel = f"segment of({e.op})"
    else:
        rel = f"{e.kind}({e.status})"
    return f"  {e.source} -{rel}-> {e.target}"


def _edge_score(e: Edge, nodes: dict[str, Node]) -> float:
    """Beam score of an incoming edge: |contribution| (or a kind prior) x |z| of its source."""
    n = nodes.get(e.source)
    if n is None:
        return 0.0
    if _data_bad(n):
        return 99.0
    z = abs(n.z or 0)
    own = _own_share(n)
    if own is not None and e.kind == "cause":
        z *= max(own, 20) / 100
    w = abs(e.contribution_pct) / 100 if e.contribution_pct is not None else (0.5 if e.kind == "cause" else 0.3)
    return w * max(z, 0.1)


def render_graph(nodes: dict[str, Node], edges: list[Edge], start: str, depth: int,
                 shown: set[str] | None = None) -> tuple[list[str], set[str]]:
    """Grouped, beam-pruned text: node block, then its top BEAM incoming edges (inputs and causes) per hop."""
    shown = set() if shown is None else shown
    lines: list[str] = []
    frontier = [start]
    if start not in shown and start in nodes:
        lines.append("[hop 0] " + node_line(nodes[start]))
        shown.add(start)
    for hop in range(1, depth + 1):
        nxt = []
        for t in frontier:
            inc = [e for e in edges if e.target == t and e.kind in ("formula", "cause") and e.source in nodes]
            inc.sort(key=lambda e: -_edge_score(e, nodes))
            for e in inc[:BEAM]:
                if e.source not in shown:
                    lines.append(f"[hop {hop}] " + node_line(nodes[e.source]))
                    shown.add(e.source)
                    nxt.append(e.source)
                lines.append(edge_line(e))
            rest = inc[BEAM:]
            if rest:
                unusual = [e.source for e in rest if _anomalous(nodes.get(e.source))]
                lines.append(f"  ({len(rest)} more neighbours of {t} hidden"
                             + (f"; unusual among them: {', '.join(unusual)}" if unusual else ", all normal") + ")")
        frontier = nxt
    return lines, shown


def render_snapshot(snap: GraphSnapshot) -> str:
    """Compact text of the annotated neighbourhood for the user message (token-cheap)."""
    nodes = {n.id: n for n in snap.nodes}
    lines = [f"KPI {snap.kpi} | {snap.window_h}h window ending {snap.ts}"]
    body, shown = render_graph(nodes, snap.edges, snap.kpi, max(1, snap.depth))
    lines += body
    # unusual nodes that use the KPI (parents / effects): often the real carrier of a ratio KPI
    side = [n for n in snap.nodes if n.id not in shown and (_anomalous(n) or _data_bad(n))]
    if side:
        lines.append("ALSO UNUSUAL NEARBY")
        for n in sorted(side, key=lambda n: -abs(n.z or 0))[:BEAM]:
            lines.append(node_line(n))
            lines += [edge_line(e) for e in snap.edges if n.id in (e.source, e.target)
                      and (e.source in shown or e.target in shown)][:2]
    if snap.notes:
        lines.append("NOTES: " + "; ".join(snap.notes[:5]))
    return "\n".join(lines)

# ---------------------------------------------------------------- run state + tools


class _Run:
    """State of one interpretation (graph view, trace, tool results). Each tool returns a compact string and leaves a
    pending trace step that on_call turns into a TraceStep."""

    def __init__(self, w, api, snap: GraphSnapshot, interp_id: str, incident_id: Optional[str],
                 on_step: Optional[Callable[[TraceStep], None]], window_h: int):
        self.w, self.api, self.snap, self.kpi = w, api, snap, snap.kpi
        self.id, self.incident_id, self.on_step, self.window_h = interp_id, incident_id, on_step, window_h
        self.nodes: dict[str, Node] = {n.id: n for n in snap.nodes}
        self.edges: list[Edge] = list(snap.edges)
        self.extra_nodes: dict[str, Node] = {}           # change events / segments added by the interpreter
        self.extra_edges: list[Edge] = []
        self.new_connections: list[Edge] = []            # proposals accepted by graph_api.validate_edge
        self.trace: list[TraceStep] = []
        self.pending: Optional[dict] = None              # trace step of the running tool call
        self.plan: dict = {}                             # mock walk state; "path" feeds the partial file
        # tool results by node / query, read back by the mock walk and the final snapshot
        self.ruled, self.details, self.breakdowns, self.locs, self.changes, self.searches = {}, {}, {}, {}, {}, {}
        self.proposals, self.expanded, self.onsets = [], set(), {}
        self.incident_onset: Optional[str] = None

    # ------------------------------------------------------------ graph queries
    def node(self, nid: str) -> Optional[Node]:
        return self.nodes.get(nid) or self.extra_nodes.get(nid)

    def label(self, nid: str) -> str:
        n = self.node(nid)
        return n.label if n is not None and n.label else nid.replace("_", " ")

    def inputs(self, nid: str) -> list[tuple[str, Edge]]:
        return [(e.source, e) for e in self.edges if e.kind == "formula" and e.target == nid]

    def causes(self, nid: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == "cause" and e.target == nid]

    def edge_between(self, a: str, b: str) -> Optional[Edge]:
        return next((e for e in self.edges + self.extra_edges + self.new_connections if {e.source, e.target} == {a, b}),
                    None)

    def onset(self, nid: str) -> Optional[str]:
        n = self.node(nid)
        return (n.onset_ts if n is not None and n.onset_ts else None) or self.onsets.get(nid)

    def visited(self) -> set[str]:
        return {self.kpi} | {s.node for s in self.trace if s.node}

    def merge(self, sub: GraphSnapshot, base_depth: int) -> list[str]:
        """Add a sub-snapshot's nodes / edges; returns the new node ids."""
        added = []
        for n in sub.nodes:
            if n.id not in self.nodes:
                self.nodes[n.id] = n.model_copy(update={"depth": base_depth + n.depth})
                added.append(n.id)
        idx = {(e.source, e.target, e.kind): i for i, e in enumerate(self.edges)}
        for e in sub.edges:
            k = (e.source, e.target, e.kind)
            if k not in idx:
                idx[k] = len(self.edges)
                self.edges.append(e)
            elif e.kind == "formula" and e.target == sub.center and e.contribution_pct is not None:
                # the sub-snapshot centred on the target sees all its inputs: its contributions are complete
                self.edges[idx[k]] = self.edges[idx[k]].model_copy(update={"contribution_pct": e.contribution_pct})
        return added

    # ------------------------------------------------------------ trace
    def emit(self, action: str, node: Optional[str], args: dict, finding: str, numbers: dict | None = None,
             hn: list[str] | None = None, he: list[tuple[str, str]] | None = None) -> TraceStep:
        st = TraceStep(i=len(self.trace), action=action, node=node, args=_r(args), finding=finding,
                       numbers=_r(numbers or {}), highlight_nodes=list(dict.fromkeys(x for x in (hn or []) if x)),
                       highlight_edges=[(a, b) for a, b in (he or [])])
        self.trace.append(st)
        if self.on_step:
            try:
                self.on_step(st)
            except Exception:
                pass
        self.write_partial()
        return st

    def note(self, action, node, args, finding, numbers=None, hn=None, he=None) -> None:
        self.pending = dict(action=action, node=node, args=args, finding=finding, numbers=numbers, hn=hn, he=he)

    def on_call(self, rec: dict) -> None:
        """pulse.llm callback after every tool call (mock, replay and live)."""
        p, self.pending = self.pending, None
        if p is None or rec.get("error"):
            node = (rec.get("args") or {}).get("node")
            self.emit(rec.get("tool", "tool"), node, rec.get("args") or {},
                      f"{rec.get('tool')} failed: {str(rec.get('result', ''))[:160]}", {}, [node] if node else [], [])
            return
        self.emit(**p)

    def write_partial(self) -> None:
        k = self.nodes.get(self.kpi)
        part = Interpretation(interpretation_id=self.id, incident_id=self.incident_id, kpi=self.kpi,
                              snapshot_id=self.snap.snapshot_id, ts=self.snap.ts,
                              headline=f"Investigating {self.label(self.kpi)}"
                                       + (f" ({_pct(k.deviation_pct)})" if k and k.deviation_pct is not None else ""),
                              summary="Investigation in progress.", path=self.plan.get("path", [self.kpi]),
                              trace=self.trace, new_connections=self.new_connections, llm_mode=mode())
        C.save(part, f"interpretations/{self.id}.partial.json")

    # ------------------------------------------------------------ tools (wrap pulse.graph_api)
    def t_expand(self, node: str) -> str:
        n0 = self.node(node)
        sub = self.api.build_snapshot(self.w, self.kpi, center=node, depth=1, window_h=self.window_h)
        self.expanded.add(node)
        added = self.merge(sub, n0.depth if n0 else 0)
        lines, shown = render_graph(self.nodes, self.edges, node, 1, shown={node})
        near = sorted(shown - {node})
        anom = [x for x in near if _anomalous(self.node(x))]
        miss = [x for x in near if _data_bad(self.node(x))]
        f = f"Opened {len(near)} top neighbours of {self.label(node)} ({len(added)} new)"
        f += f"; unusual: {', '.join(anom[:4])}" if anom else "; none of them is unusual"
        f += f"; data incomplete: {', '.join(miss[:3])}." if miss else "."
        self.note("expand", node, {"node": node}, f, {"shown": len(near), "new": len(added), "anomalous": anom[:6]},
                  [node] + near, [(x, node) for x in near])
        return "\n".join(lines)[:TOOL_CHARS]

    def t_node_detail(self, node: str) -> str:
        d = self.api.node_detail(self.w, node, window_h=self.window_h) or {}
        self.details[node] = d
        n = self.node(node)
        v, x = _num(d.get("value")), _num(d.get("expected"))
        dev, z, comp = _num(d.get("deviation_pct")), _num(d.get("z")), _num(d.get("completeness"))
        status, unit = d.get("status", n.status if n else "unknown"), d.get("unit", n.unit if n else None)
        on = _onset_from_series(d.get("series_48h") or [])
        if on and not (n is not None and n.onset_ts):
            self.onsets[node] = on
        if node in self.nodes:
            upd = {k: val for k, val in (("value", v), ("expected", x), ("deviation_pct", dev), ("z", z),
                                         ("completeness", comp)) if val is not None}
            if status in ("ok", "anomalous", "missing_data", "stale", "unknown"):
                upd["status"] = status
            self.nodes[node] = self.nodes[node].model_copy(update=upd)
        n = self.node(node)
        if dev is None:
            f = f"{self.label(node)}: no comparable value in this window ({status})."
        else:
            f = f"{self.label(node)} is {_below(dev)} ({_f(v, unit)} vs {_f(x, unit)} expected)"
            par = next((e for e in self.edges if e.kind == "formula" and e.source == node and
                        e.contribution_pct is not None and e.target in self.visited()), None)
            if par is not None:
                f += f" and carries {par.contribution_pct:.0f}% of the change in {self.label(par.target)}"
            if self.onset(node):
                f += f", since {_hhmm(self.onset(node))}"
            f += "."
        if comp is not None and comp < COMPLETE_MIN:
            f += f" Only {comp * 100:.0f}% of its data has arrived."
        he = [(e.source, e.target) for e in self.edges if e.source == node and e.target in self.visited()]
        self.note("detail", node, {"node": node}, f,
                  {"value": v, "expected": x, "deviation_pct": dev, "z": z, "completeness": comp, "status": status,
                   "onset": self.onset(node), "own_pct": _own_share(n)}, [node], he)
        return _compact({**{k: d.get(k) for k in ("id", "definition", "unit", "polarity", "value", "expected",
                                                  "deviation_pct", "z", "status", "completeness", "issues", "dims",
                                                  "components")},
                         "direction": None if dev is None else ("DOWN" if dev < 0 else "UP"), "onset": self.onset(node),
                         "inputs": [i.get("metric") if isinstance(i, dict) else i for i in d.get("inputs", [])],
                         "causes": [c.get("metric") for c in d.get("causes", [])][:8]})

    def t_breakdown(self, node: str, dim: str) -> str:
        rows = [dict(r) for r in (self.api.breakdown(self.w, node, dim, window_h=self.window_h) or [])]
        self.breakdowns[(node, dim)] = rows
        loc = _localise(rows)
        if loc is None:
            f = f"No {dim} data for {self.label(node)}."
        elif loc["concentrated"]:
            f = (f"{_seg_text({dim: loc['top']})} carries {loc['share']:.0f}% of the change in {self.label(node)} "
                 f"({_pct(loc['dev'])} vs normal); the other {dim} values move {_pct(loc['rest_dev'])} (control).")
        else:
            f = (f"The change in {self.label(node)} is spread across {dim} values (largest: {loc['top']}, "
                 f"{loc['share']:.0f}% of the change, {_pct(loc['dev'])}).")
        hn = [node] + ([_seg_id(node, {dim: loc["top"]})] if loc and loc["concentrated"] else [])
        self.note("breakdown", node, {"node": node, "dim": dim}, f,
                  {k: loc[k] for k in ("top", "share", "dev", "rest_dev", "concentrated")} if loc else {}, hn, [])
        return _compact([{k: r.get(k) for k in ("segment", "value", "expected", "deviation_pct", "share_of_change")}
                         for r in rows[:8]])

    def t_localize(self, node: str) -> str:
        items = list(self.api.localize(self.w, node, window_h=self.window_h) or [])
        loc = _parse_localize(items)
        self.locs[node] = loc
        seg, ctrl = loc["segment"], loc["control_dev"]
        if seg:
            f = (f"{_seg_text(seg)} carries {min(loc['share'], 100):.0f}% of the change in {self.label(node)} "
                 f"({_pct(loc['dev'])})" + (f"; control cell {loc['control_text']} moves {_pct(ctrl)}."
                                             if ctrl is not None else "."))
        else:
            f = f"The change in {self.label(node)} is not concentrated in one country, platform or category."
        self.note("localize", node, {"node": node}, f, {k: loc[k] for k in ("segment", "share", "dev", "control_dev")},
                  [node] + ([_seg_id(node, seg)] if seg else []), [])
        slim = [{k: x.get(k) for k in ("type", "dim", "top_segment", "top_share_of_change", "top_deviation_pct",
                                        "segment", "deviation_pct", "z", "reading") if x.get(k) is not None}
                for x in items[:8]]
        return _compact(slim)

    def t_recent_changes(self, node: str, segment: str = "") -> str:
        seg = _parse_seg(segment)
        onset = self.incident_onset or self.onset(self.kpi) or self.onset(node)
        fn = self.api.changes_near   # +-3 h around the onset, the 36 h before it, and anything from the last 36 h
        raw = list(fn(self.w, self.w.now, seg, 36))
        if onset:
            raw = list(fn(self.w, onset, seg, 3)) + list(fn(self.w, pd.Timestamp(onset) - pd.Timedelta(hours=18), seg, 18)) + raw
        evs = [_norm_change(e) for e in raw
               if e.get("segment_overlap", True) and e.get("table") != "graph_changes"
               and "rollback" not in str(e.get("kind", "")) and str(e.get("state", "")) != "resolved"]
        evs = _rank_changes(evs, self, node, seg, onset or str(self.w.now))
        self.changes[node] = evs
        for ev in evs[:3]:
            self.extra_nodes.setdefault(ev["id"], Node(
                id=ev["id"], label=ev["name"], kind="component", domain=ev.get("component"),
                description=f"{ev['type']} ({ev.get('when', '')})", added_by="interpreter",
                depth=(self.node(node).depth + 1) if self.node(node) else 1))
        where = f" in {_seg_text(seg)}" if seg else ""
        if evs:
            top = evs[0]
            f = (f"{len(evs)} recent change(s) near {self.label(node)}{where}; best match: '{top['name']}' "
                 f"({top['type']}, {top['when']}" + (f", {top['timing']}" if top.get("timing") else "")
                 + ("; planned/innocent" if top["innocent"] else "") + ").")
        else:
            f = f"No release, campaign or incident near {self.label(node)}{where} around the onset or in the last 36 hours."
        self.note("recent_changes", node, {"node": node, "segment": segment}, f,
                  {"matches": len(evs), "best": evs[0]["name"] if evs else None,
                   "timing": evs[0].get("timing") if evs else None},
                  [node] + [e["id"] for e in evs[:1]], [(evs[0]["id"], node)] if evs else [])
        return _compact([{k: e.get(k) for k in ("id", "type", "name", "component", "segment", "when", "timing",
                                                 "innocent", "match")} for e in evs[:5]])

    def t_search_data(self, query: str) -> str:
        res = [dict(r) for r in (self.api.search_data(self.w, query) or []) if isinstance(r, dict)][:12]
        self.searches[query] = res
        unmod = [r for r in res if r.get("status") == "unmodelled"]
        names = [str(r.get("name")) for r in res]
        f = (f"Data search for '{query}' found {len(res)} candidates"
             + (f" ({len(unmod)} not yet in the graph)" if unmod else "")
             + (f": {', '.join(names[:4])}." if names else "."))
        self.note("search", None, {"query": query}, f, {"found": len(res), "unmodelled": len(unmod)},
                  [x for x in names if x in self.nodes][:6], [])
        return _compact([{k: r.get(k) for k in ("kind", "name", "status", "metric", "description")} for r in res])

    def t_propose(self, source: str, target: str, kind: str = "cause", reason: str = "") -> str:
        k = kind if kind in ("formula", "cause") else "cause"
        ns, nt = self.node(source), self.node(target)
        sign = (_sgn((ns.deviation_pct or 0) * (nt.deviation_pct or 0)) or 1) if ns and nt else None
        e = Edge(source=source, target=target, kind=k, status="proposed", sign=sign if k == "cause" else None,
                 label=reason[:160], added_by="interpreter")
        ch = self.api.validate_edge(self.w, e)
        status = ch.edge.status if ch.edge is not None else "proposed"
        accepted = ch.action != "reject_edge" and status != "rejected"
        rec = {"source": source, "target": target, "kind": k, "status": status if accepted else "rejected",
               "reason": ch.reason, "evidence": ch.evidence}
        self.proposals.append(rec)
        if accepted:
            ch = ch.model_copy(update={"by": "interpreter"})
            try:
                self.api.record_change(ch)
            except Exception:
                pass
            self.new_connections.append((ch.edge or e).model_copy(update={"added_by": "interpreter"}))
        ev = ", ".join(f"{a}={_r(b)}" for a, b in list((ch.evidence or {}).items())[:3])
        f = (f"Proposed new link {source} -> {target}: "
             + (f"accepted as {status}" if accepted else "rejected") + f" by the numeric check ({ch.reason[:100]}).")
        self.note("propose_edge", target, {"source": source, "target": target, "kind": k, "reason": reason[:160]},
                  f, {"status": rec["status"], "evidence": ev}, [source, target], [(source, target)])
        return _compact(rec)

    def t_rule_out(self, node: str, why: str) -> str:
        self.ruled[node] = why
        n = self.node(node)
        nums = {"deviation_pct": n.deviation_pct, "z": n.z, "status": n.status} if n else {}
        f = (f"Ruled out {self.label(node)}: it is {_pct(n.deviation_pct)} vs normal ({_verdict(n)})"
             if n is not None and n.deviation_pct is not None else f"Ruled out {self.label(node)}")
        f += (f"; {why.rstrip('.')[:140]}." if why else ".")
        he = [(e.source, e.target) for e in self.edges if node in (e.source, e.target)
              and ({e.source, e.target} - {node}) & self.visited()]
        self.note("rule_out", node, {"node": node, "why": why[:200]}, f, nums, [node], he[:3])
        return _compact({"ruled_out": node, **nums})

    def tools(self) -> list[Tool]:
        s = _S
        return [
            Tool("expand", "Show the top neighbours (inputs, causes) of a node, annotated, ranked by "
                           "|contribution| x |z|.", obj({"node": s}), self.t_expand),
            Tool("node_detail", "Numbers of one node: value, expected, direction, z, completeness, onset, dims, "
                                "inputs, causes.", obj({"node": s}), self.t_node_detail),
            Tool("localize", "Where is the node's change concentrated (country, platform, category, pairs) and how "
                             "does the control (everything else) move?", obj({"node": s}), self.t_localize),
            Tool("breakdown", "Split a node by one dimension: value, expected, deviation, share of change per "
                              "segment.", obj({"node": s, "dim": s}), self.t_breakdown),
            Tool("recent_changes", "Releases, campaigns, calendar events, competitor events and incidents near the "
                                   "node's onset. segment like 'country=DE,platform=android' or ''.",
                 obj({"node": s, "segment": s}), self.t_recent_changes),
            Tool("search_data", "Search metrics and raw warehouse columns related to a query (status modelled or "
                                "unmodelled).", obj({"query": s}), self.t_search_data),
            Tool("propose_connection", "Propose a new edge source -> target (kind cause|formula) between metrics; "
                                       "validated on the data, returns verdict and numbers.",
                 obj({"source": s, "target": s, "kind": s, "reason": s}), self.t_propose),
            Tool("rule_out", "Record that a node / rival explanation is rejected, with a short reason.",
                 obj({"node": s, "why": s}), self.t_rule_out),
        ]

# ---------------------------------------------------------------- localisation / onset / change helpers


def _localise(rows: list[dict]) -> Optional[dict]:
    """Breakdown rows -> top segment by share of change; concentrated if it carries >=50% and the rest moves less."""
    rows = [r for r in rows if r.get("segment") is not None]
    if not rows:
        return None
    if all(_num(r.get("share_of_change")) is None for r in rows):
        top, share = max(rows, key=lambda r: abs(_num(r.get("deviation_pct")) or 0)), None
    else:
        top = max(rows, key=lambda r: _num(r.get("share_of_change")) or -1e9)
        share = _num(top.get("share_of_change"))
    dev = _num(top.get("deviation_pct"))
    rest = [x for x in (_num(r.get("deviation_pct")) for r in rows if r is not top) if x is not None]
    rest_dev = sum(rest) / len(rest) if rest else None
    conc = (share is not None and share >= 50 and len(rows) > 1 and dev is not None
            and (rest_dev is None or abs(rest_dev) <= max(3.0, abs(dev) / 3)))
    return {"top": str(top["segment"]), "share": share or 0.0, "dev": dev,
            "rest_dev": None if rest_dev is None else round(rest_dev, 1), "concentrated": bool(conc)}


def _parse_localize(items: list[dict]) -> dict:
    """graph_api.localize items -> {segment, share, dev, control_dev, control_text, pair_checked}. Segment = tops of
    the concentrated core dims (max 2); a matching worst cell gives the deviation, its control cells the control."""
    out = {"segment": {}, "share": 0.0, "dev": None, "control_dev": None, "control_text": "", "pair_checked": False}
    conc = sorted([x for x in items if x.get("type") == "dim" and x.get("concentrated")],
                  key=lambda x: -(_num(x.get("top_share_of_change")) or 0))
    core = [x for x in conc if x.get("dim") in DIMS][:2]   # user_type / browser mirror volume
    if not core:
        return out
    out["segment"] = {x["dim"]: str(x["top_segment"]) for x in core}
    out["share"] = min(_num(x.get("top_share_of_change")) or 0 for x in core)
    out["dev"] = _num(core[0].get("top_deviation_pct"))
    cell = next((x for x in items if x.get("type") == "cell" and isinstance(x.get("segment"), dict)), None)
    if cell:
        cseg = {k: str(v) for k, v in cell["segment"].items()}
        if all(out["segment"].get(k) == v for k, v in cseg.items() if k in DIMS) and any(k in DIMS for k in cseg):
            if set(cseg) <= set(DIMS):
                out.update(segment=cseg, dev=_num(cell.get("deviation_pct")), pair_checked=True)
            ctrls = [x for x in items if x.get("type") == "control" and _num(x.get("deviation_pct")) is not None]
            ctrls.sort(key=lambda c: (_num(c.get("value")) == _num(c.get("expected")), abs(_num(c["deviation_pct"]))))
            ctrls = [c for c in ctrls if set(c.get("segment") or {}) <= set(DIMS)]
            if ctrls:
                out.update(control_dev=_num(ctrls[0]["deviation_pct"]), control_text=_seg_text(ctrls[0]["segment"]))
    return out


def _onset_from_series(series: list[dict]) -> Optional[str]:
    """First hour of the current run of hours that sit on the same side of expected by more than 4%."""
    pts = [(p.get("ts"), _num(p.get("value")), _num(p.get("expected"))) for p in series]
    pts = [(t, v, x) for t, v, x in pts if v is not None and x]
    if len(pts) < 3:
        return None
    side = lambda v, x: 0 if abs(v - x) / abs(x) < 0.04 else (1 if v > x else -1)  # noqa: E731
    cur = side(pts[-1][1], pts[-1][2]) or side(pts[-2][1], pts[-2][2])
    start, gap = None, 0
    for t, v, x in reversed(pts):
        sd = side(v, x)
        if cur and sd == cur:
            start, gap = t, 0
        elif cur and gap == 0 and sd == 0:
            gap = 1          # tolerate one noisy hour
        else:
            break
    return start


def _norm_change(e: dict) -> dict:
    """graph_api.changes_near row -> {id, type, name, component, segment, t, innocent, calendar, planned, metrics}."""
    name, typ, cal = str(e.get("name") or "change"), str(e.get("kind") or "change"), e.get("table") == "calendar"
    return {"id": f"event:{name}", "type": typ, "name": name, "component": str(e.get("component") or ""),
            "segment": {d: str(e[d]) for d in DIMS if e.get(d) not in (None, "*", "", "nan")},
            "t": e.get("ts"), "innocent": cal or typ in INNOCENT_TYPES, "calendar": cal, "table": e.get("table"),
            "planned": e.get("planned") if isinstance(e.get("planned"), bool) else None,
            "metrics": [m for m in str(e.get("metrics") or "").split(";") if m]}


def _rank_changes(evs: list[dict], run: _Run, node: str, seg: dict, onset: str) -> list[dict]:
    """Score changes: component of the node (+2) or the walked path (+1), segment match, timing vs the onset,
    change-log rows over status posts (a post reports a symptom), unplanned first. Returns the top 6."""
    def comps(ms):
        out = set()
        for m in ms:
            try:
                out |= set(run.w.graph.components_of(m))
            except Exception:
                pass
        return out
    own_c, path_c = comps([node]), comps({run.kpi} | {s.node for s in run.trace if s.node})
    mets = {node, run.kpi} | {s.node for s in run.trace if s.node}
    try:
        on = pd.Timestamp(onset)
    except Exception:
        on = pd.Timestamp(run.w.now)
    now = pd.Timestamp(run.w.now)
    out, seen = [], set()
    for e in evs:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        e = dict(e)
        sc = -1.0 if e.get("table") == "status_feed" else 1.0
        sc += 2.0 if e.get("component") in own_c else 1.0 if e.get("component") in path_c else 0.0
        if e.get("calendar"):
            sc += 2.5 if set(e.get("metrics") or []) & mets else 0.5
        if e.get("planned") is False:
            sc += 1.5
        for d, v in (e.get("segment") or {}).items():
            sc += 1.5 if d in seg and str(v).lower() == str(seg[d]).lower() else -0.3
        e["timing"], e["timing_ok"] = None, False
        try:
            t = pd.Timestamp(e["t"]).tz_localize(None)
            lead = (on - t).total_seconds() / 3600
            e["when"] = f"live since {t:%m-%d %H:%M}, {(now - t).total_seconds() / 3600:.0f}h ago"
            if -1.0 <= lead <= 3.0:
                sc += 1.5
                e["timing_ok"] = True
                e["timing"] = (f"went live {abs(lead) * 60:.0f} min {'before' if lead >= 0 else 'after'} the onset"
                               if abs(lead) < 1 else f"went live {lead:.0f}h before the onset")
            elif 3 < lead <= 24:
                sc += 0.5
                e["timing"] = f"{lead:.0f}h before the onset"
        except Exception:
            e["when"] = "time unknown"
        e["score"] = round(sc, 2)
        e["match"] = "component" if e.get("component") in own_c | path_c else "segment/time"
        out.append(e)
    out.sort(key=lambda e: -e["score"])
    return out[:6]

# ---------------------------------------------------------------- deterministic mock policy (baseline walk)


def _mock_policy(run: _Run):
    """pulse.llm mock policy: the next tool batch of the scripted walk, then the template answer."""
    gen = _walk(run)
    run.left = MAX_TURNS

    def policy(calls: list[dict]) -> dict:
        step = next(gen, None) if run.left > 0 else None
        if step is None:
            return {"final": _template_answer(run)}
        run.left -= 1
        return step
    return policy


def _carries(run: _Run, parent: str, child: str, e: Edge, visited: set) -> bool:
    """Does `child` carry `parent`'s change? (formula share >= 30% and moving clearly, or anomalous with the sign)."""
    pn, cn = run.node(parent), run.node(child)
    if cn is None or child in visited or _sgn(cn.deviation_pct) == 0:
        return False
    if (e.label or "").startswith(("numerator", "denominator")):
        return False   # an atomic ratio is a leaf: its raw counts move because of it (orders / payment_attempts)
    if pn is not None and _verdict(pn) in ("BAD for business", "within normal range") and _biz(cn) > 0 and _biz(pn) < 0:
        return False   # a good move offsets a bad one, it cannot carry it (margin down while GMV is up)
    if e.contribution_pct is not None:
        big = pn is not None and abs(cn.deviation_pct) >= 0.4 * abs(pn.deviation_pct or 0)
        return e.contribution_pct >= 30 and (abs(cn.z or 0) >= 2 or _anomalous(cn) or big)
    return _anomalous(cn) and _sgn(cn.deviation_pct) * (e.sign or 1) == _sgn(pn.deviation_pct if pn else 0)


def _path_own(run: _Run, nid: str, path: set) -> Optional[float]:
    """Own share of a node's move as seen from the walk: inputs on the path or raw num/den counts of an atomic ratio
    do not count as parents (graph_api's inherited_pct is circular there, e.g. payment_success_rate <- orders)."""
    ins, n = run.inputs(nid), run.node(nid)
    if not ins:
        atomic = n is not None and n.definition and " / " in n.definition and " x " not in n.definition
        return 100.0 if atomic else _own_share(n)
    skip = lambda c, e: c in path or (e.label or "").startswith(("numerator", "denominator"))  # noqa: E731
    if not any(skip(c, e) for c, e in ins):
        return _own_share(run.node(nid))
    rest = [max(0.0, e.contribution_pct or 0.0) for c, e in ins if not skip(c, e)]
    return max(0.0, 100.0 - min(100.0, sum(rest)))


def _walk(run: _Run):
    """Formula descent -> localise -> cause edges -> change log -> rival / outcome check -> optional new link."""
    kpi, plan = run.kpi, run.plan
    plan.update(path=[kpi], data_issue=[], loc={}, cause=None, event=None, proposal=None, explained=100.0,
                ledger={}, innocent=False)
    if _data_bad(run.node(kpi)):   # data first: the KPI itself incomplete?
        yield {"tools": [("node_detail", {"node": kpi})]}
        plan["data_issue"].append(kpi)
        return
    visited, cur, rival_done = {kpi}, kpi, False
    while run.left > 4 and len(plan["path"]) < 6:
        # make sure all inputs of cur are visible (snapshot frontier / partial contributions)
        want = [i.get("metric") if isinstance(i, dict) else i for i in run.details.get(cur, {}).get("inputs", [])]
        have = {c for c, _ in run.inputs(cur)}
        if cur != kpi and cur not in run.expanded and (not have or (want and set(want) - have)):
            yield {"tools": [("expand", {"node": cur})]}
        kids = [(c, e) for c, e in run.inputs(cur) if c not in visited and run.node(c) is not None]
        bad = [c for c, _ in kids if _data_bad(run.node(c))]
        if bad:
            yield {"tools": [("node_detail", {"node": bad[0]})]}
            plan["data_issue"].append(bad[0])
            plan["path"].append(bad[0])
            return
        # beam score = contribution x |z| (a term that only mirrors another input moves little on its own scale)
        carriers = sorted([((e.contribution_pct if e.contribution_pct is not None else 50.0)
                            * max(abs(run.node(c).z or 0), 0.3), c, e)
                           for c, e in kids if _carries(run, cur, c, e, visited)], key=lambda x: -x[0])
        if not carriers:
            break
        _, best, be = carriers[0]
        tools = [("node_detail", {"node": best})]
        # rival test (traffic / mix): the non-carrying sibling at the first hop
        sib = [c for c, e in kids if c != best and not _anomalous(run.node(c))]
        if not rival_done and sib:
            tools.append(("rule_out", {"node": sib[0], "why": f"does not carry the change in {run.label(cur)}"}))
            rival_done = True
            plan["ledger"]["traffic_or_mix"] = ("rejected", f"{sib[0]} {_pct(run.node(sib[0]).deviation_pct)}")
        yield {"tools": tools}
        if _data_bad(run.node(best)):
            plan["data_issue"].append(best)
            plan["path"].append(best)
            return
        if be.contribution_pct is not None:
            plan["explained"] *= min(100.0, be.contribution_pct) / 100
        plan["path"].append(best)
        visited.add(best)
        cur = best
    deep = plan["deep"] = plan["path"][-1]   # deepest formula node: localised, then its causes are tested
    dn = run.node(deep)
    if run.left > 3 and dn is not None and _sgn(dn.deviation_pct):   # localise the deepest moving node
        yield {"tools": [("localize", {"node": deep})]}
        loc = run.locs.get(deep, {})
        plan["loc"] = loc if loc.get("segment") else {}
    seg = plan["loc"].get("segment", {}) if plan["loc"] else {}
    # cause edges into the deepest node
    if run.left > 3 and deep not in run.expanded and not run.causes(deep) and deep != kpi:
        yield {"tools": [("expand", {"node": deep})]}
    dsign = _sgn(dn.deviation_pct if dn else 0)
    cands, calm = [], []
    for e in run.causes(deep):
        s = run.node(e.source)
        if s is None or e.source in visited:
            continue
        if _anomalous(s) and (e.sign or 1) * _sgn(s.deviation_pct) == dsign:
            cands.append((abs(s.z or 0), e.source))
        elif not _anomalous(s) and not _data_bad(s) and s.deviation_pct is not None:
            calm.append(e.source)
    cands.sort(reverse=True)
    if cands and run.left > 2:
        c = cands[0][1]
        yield {"tools": [("node_detail", {"node": c})]}
        if _anomalous(run.node(c)):
            plan["cause"] = c
            plan["path"].append(c)
            visited.add(c)
            # one more hop: an unusual cause of that cause moving with its edge sign (seller feed errors -> offers)
            cs = _sgn(run.node(c).deviation_pct)
            deeper = sorted(((abs(run.node(e.source).z or 0), e.source) for e in run.causes(c)
                             if e.source not in visited and _anomalous(run.node(e.source))
                             and (e.sign or 1) * _sgn(run.node(e.source).deviation_pct) == cs), reverse=True)
            if deeper and run.left > 2:
                d2 = deeper[0][1]
                yield {"tools": [("node_detail", {"node": d2})]}
                plan["cause"] = d2
                plan["path"].append(d2)
                visited.add(d2)
    # change log (also tests the planned-event rival) + rule out calm causes in the same turn
    target = plan["cause"] or deep
    tools = [("recent_changes", {"node": target, "segment": ",".join(f"{a}={b}" for a, b in seg.items())})]
    tools += [("rule_out", {"node": x, "why": "possible cause, but it did not move"}) for x in calm[:2]]
    if run.left > 1:
        yield {"tools": tools}
    evs = run.changes.get(target, [])
    if evs and evs[0]["score"] >= 3.0:
        ev = evs[0]
        try:
            cc = set(run.w.graph.components_of(plan["cause"])) if plan["cause"] else set()
        except Exception:
            cc = set()
        try:   # the KPI's own components (in_stock_rate: catalog, wholesale) also own an unplanned change in its segment
            kc = set(run.w.graph.components_of(kpi))
        except Exception:
            kc = set()
        own_kpi = (ev.get("component") in kc and ev.get("planned") is False and bool(ev.get("segment"))
                   and all(str(seg.get(d, "")).lower() == str(v).lower() for d, v in ev["segment"].items()))
        if cc and ev.get("component") not in cc and not ev.get("calendar") and not own_kpi:
            plan["coincident"] = ev     # same time and segment, but not the cause metric's component
        else:
            plan["event"] = ev
    ev = plan["event"]
    # planned event: is the net business outcome positive? (innocent vs planned-but-harmful)
    if ev and ev.get("innocent"):
        tier = getattr(run.node(kpi), "tier", None)
        out_node = kpi if tier == "north_star" else ("orders" if kpi != "orders" else "gmv")
        ctry = (ev.get("segment") or {}).get("country")
        d = None
        if ctry and run.left > 1:
            yield {"tools": [("breakdown", {"node": out_node, "dim": "country"})]}
            rows = run.breakdowns.get((out_node, "country"), [])
            row = next((r for r in rows if str(r.get("segment")) == ctry), None)
            d = _num(row.get("deviation_pct")) if row else None
            out_node = f"{out_node} in {COUNTRY.get(ctry, ctry)}"
        elif out_node != kpi and out_node not in run.nodes and run.left > 1:
            yield {"tools": [("node_detail", {"node": out_node})]}
        if d is None:
            on = run.node(out_node)
            d = on.deviation_pct if on is not None else _num(run.details.get(out_node, {}).get("deviation_pct"))
        plan["outcome"] = (out_node, d)
        plan["innocent"] = bool(ev.get("calendar")) or (d is not None and d >= -1.0)
    # nothing explains it: search the data and propose one new connection
    if not plan["cause"] and not ev and dn is not None and _anomalous(dn) and run.left > 1:
        q = deep.replace("_rate", "").replace("_", " ")
        yield {"tools": [("search_data", {"query": q})]}
        pick = None
        for r in run.searches.get(q, []):
            nm = str(r.get("metric") or "")
            if r.get("kind") == "metric" and nm and nm != deep and nm not in visited and not run.edge_between(nm, deep):
                pick = nm
                break
        if pick and run.left > 0:
            yield {"tools": [("propose_connection", {"source": pick, "target": deep, "kind": "cause",
                                                     "reason": f"{pick} may drive {deep} (found by data search)"})]}
            pr = run.proposals[-1] if run.proposals else None
            if pr and pr["status"] != "rejected":
                plan["proposal"] = pr

# ---------------------------------------------------------------- template answer (mock final)


def _impact(run: _Run) -> str:
    """'about 62 fewer orders per hour (roughly EUR 5.0k)' from the KPI (3h window totals -> per hour)."""
    k, hours = run.node(run.kpi), max(run.window_h, 1)
    if k is None or k.value is None or k.expected is None:
        return ""
    gap = k.value - k.expected
    if k.unit in ("EUR", "count"):
        g = abs(gap) / hours
        if k.unit == "EUR":
            return f"about {_f(g, 'EUR')} per hour {'less' if gap < 0 else 'more'} than normal"
        s = f"about {g:,.0f} {run.label(run.kpi).lower()} per hour {'fewer' if gap < 0 else 'more'} than normal"
        aov = run.node("aov")
        if run.kpi == "orders" and aov is not None and aov.value:
            s += f" (roughly {_f(g * aov.value, 'EUR')} per hour)"
        return s
    o = run.node("orders")
    if o is not None and o.value is not None and o.expected is not None:
        og = (o.value - o.expected) / hours
        return f"orders are {abs(og):,.0f} per hour {'below' if og < 0 else 'above'} normal"
    return ""


def _template_answer(run: _Run) -> dict:
    """Mock final answer in the SCHEMA shape, written from the walk's plan with real numbers."""
    plan, kpi, k = run.plan, run.kpi, run.node(run.kpi)
    kl, dev = run.label(kpi), (k.deviation_pct if k else None)
    updown = "down" if (dev or 0) < 0 else "up"
    path = list(plan["path"])
    out: dict[str, Any] = {"symptoms": [], "unusual": [], "missing_data": [], "hypotheses": [],
                           "ruled_out": [{"node": n, "why": w} for n, w in run.ruled.items()]}
    impact = _impact(run)

    if plan.get("data_issue"):   # data issue: stop
        bad = plan["data_issue"][0]
        bn = run.node(bad)
        ctext = (f"only {bn.completeness * 100:.0f}% of the expected data has arrived"
                 if bn is not None and bn.completeness is not None else f"its data is {bn.status if bn else 'missing'}")
        out.update(
            headline=" ".join(f"{kl} looks {updown} {_pct(dev, False)}, but the data is incomplete".split()[:15]),
            summary=(f"{kl} shows {_below(dev)}, but for "
                     f"{run.label(bad)} {ctext}. This looks like a data delay rather than a real business change, so "
                     f"no cause is attributed yet. We re-check once the data has caught up."),
            path=path, first_cause={"node": bad, "why": f"data incomplete: {ctext}"}, confidence="data_issue",
            missing_data=[{"node": bad, "why": ctext}],
            hypotheses=[{"name": "data issue", "verdict": "supported", "evidence": ctext},
                        {"name": "fault on the path", "verdict": "open", "evidence": "hidden by missing rows"}],
            uncertainty="A real change could be hidden behind the missing rows; confirm after the data is complete.",
            watch_next=list(dict.fromkeys([bad, kpi])))
        return out

    deep = plan.get("deep") or plan["path"][-1]
    dn = run.node(deep)
    cause, ev, prop = plan.get("cause"), plan.get("event"), plan.get("proposal")
    loc = plan.get("loc") or {}
    seg = loc.get("segment") or {}
    where = _seg_text(seg) if seg else ""
    innocent = bool(ev and plan.get("innocent"))
    # independent evidence types
    evidence = []
    share_txt = f"{min(loc.get('share') or 0, 100):.0f}%"
    if seg:
        evidence.append(f"localised: {where} carries {share_txt} of the change")
    ctrl = loc.get("control_dev")
    if seg and ctrl is not None and abs(ctrl) <= max(3.0, abs(loc.get("dev") or 0) / 3):
        evidence.append(f"control normal: {loc.get('control_text') or 'the rest'} moves {_pct(ctrl)}")
    if ev and ev.get("timing_ok"):
        evidence.append(f"timing: '{ev['name']}' {ev['timing']}")
    elif ev and ev.get("calendar"):
        evidence.append(f"calendar: '{ev['name']}' was announced")
    if cause:
        evidence.append(f"cause metric: {run.label(cause)} {_pct(run.node(cause).deviation_pct)} with the expected "
                        f"sign")
    own = _path_own(run, cause or deep, set(plan["path"]))
    if own is not None and own >= 50 and deep != kpi:
        evidence.append(f"own change: {own:.0f}% of {run.label(cause or deep)}'s move is its own")
    if prop:
        evidence.append(f"new link {prop['source']} -> {prop['target']} {prop['status']} on the data")
    explained = plan.get("explained", 100.0) if len(path) > 1 else 0.0
    # first cause: change event > cause metric > proposed link > deepest node (events / links extend the path)
    fc = ev["id"] if ev else cause or (prop["source"] if prop else deep)
    if ev or (prop and not cause):
        path.append(fc)
    if seg:   # put the localised segment on the path, right after the deepest metric
        path.insert(path.index(deep) + 1, _seg_id(deep, seg))
    conf = ("unexplained" if len(plan["path"]) == 1 and not ev and not seg else
            "supported" if len(evidence) >= 2 and explained >= EXPLAINED_MIN else
            "hypothesis" if evidence or len(plan["path"]) > 1 else "unexplained")
    fc_why = "; ".join(evidence[:3]) or f"{run.label(deep)} {_pct(dn.deviation_pct if dn else None)}: deepest " \
                                        f"moving node on the path"
    # symptoms / unusual
    if cause and ev:
        out["symptoms"].append({"node": cause, "why": f"{_pct(run.node(cause).deviation_pct)}: the mechanism through "
                                                      f"which '{ev['name']}' hits {kl}"})
    for n in plan["path"][1:]:
        nn = run.node(n)
        if n not in (fc, cause) and nn is not None:
            out["symptoms"].append({"node": n, "why": f"{_pct(nn.deviation_pct)}, inherited from "
                                                      f"{ev['name'] if ev else run.label(fc)}"})
    on_path = set(path) | set(run.ruled)
    for n in sorted(run.nodes.values(), key=lambda n: -abs(n.z or 0)):
        if n.id in on_path or not _anomalous(n):
            continue
        if any(e.source in on_path and e.target == n.id for e in run.edges):
            out["symptoms"].append({"node": n.id, "why": f"{_pct(n.deviation_pct)}: downstream of the path"})
        elif len(out["unusual"]) < 4:
            out["unusual"].append({"node": n.id, "why": f"{_pct(n.deviation_pct)} vs normal, not explained here"})
    # hypothesis ledger
    tm = (plan.get("ledger") or {}).get("traffic_or_mix")
    sn = run.node("sessions")
    if tm is None and sn is not None and sn.deviation_pct is not None:
        tm = ("rejected" if not _anomalous(sn) else "open", f"sessions {_pct(sn.deviation_pct)}")
    comp_min = min([n.completeness for n in (run.node(p) for p in plan["path"]) if n is not None
                    and n.completeness is not None] or [1.0])
    net = f"; net {plan['outcome'][0]} {_pct(plan['outcome'][1])}" if plan.get("outcome") else ""
    hyp = lambda name, verdict, evidence: {"name": name, "verdict": verdict, "evidence": evidence}  # noqa: E731
    out["hypotheses"] = [
        hyp(f"fault: {ev['name'] if ev else run.label(fc)}", "rejected" if innocent else
            ("supported" if conf == "supported" else "open"), "; ".join(evidence) or "no evidence yet"),
        hyp("traffic or mix change", tm[0] if tm else "open", tm[1] if tm else "not tested"),
        hyp("data issue", "rejected", f"data on the path is at least {comp_min * 100:.0f}% complete"),
        hyp("planned or innocent event", "supported" if innocent else "rejected",
            f"'{ev['name']}' ({ev['type']}){net}" if ev else "no planned event near the onset")]
    if innocent:
        out["ruled_out"].append({"node": "hypothesis:fault", "why": "planned event with positive net outcome"})
    if not ev:
        out["ruled_out"].append({"node": "hypothesis:planned_event", "why": "no release, campaign or calendar entry"})
    out["ruled_out"].append({"node": "hypothesis:data_issue", "why": f"path data >= {comp_min * 100:.0f}% complete"})
    # text: what -> where -> why -> how sure
    head = f"{kl} {updown} {_pct(dev, False)}"
    hw = head + (f" for {where}" if where and len(where) < 28 else "")
    headline = (f"{hw}: expected effect of {ev['name']}" if innocent else
                f"{hw}, likely caused by {ev['name']}" if ev else
                f"{hw}, driven by {run.label(cause)}" if cause else
                f"{head}; no clear cause found yet" if conf == "unexplained" else
                f"{hw}, carried by {run.label(deep)}")
    sents = [f"{kl} is {_below(dev)} over the last {run.window_h} hours" + (f", {impact}." if impact else ".")]
    if len(plan["path"]) > 1 or seg:
        chain = " -> ".join(run.label(n) for n in plan["path"][1:4] if n != cause)
        s2 = (f"It runs through {chain}" if chain else "It")
        if seg:
            s2 += (f"{' and is' if chain else ' is'} concentrated in {where} ({share_txt} of the change"
                   + (f"; {loc.get('control_text') or 'the rest'} {_pct(ctrl)}" if ctrl is not None else "") + ")")
        sents.append(s2 + ".")
    if innocent:
        o = plan.get("outcome")
        sents.append(f"It coincides with the planned '{ev['name']}' ({ev['when']})"
                     + (f" and {run.label(o[0]) if run.node(o[0]) else o[0]} are {_pct(o[1])}, so the net business "
                        f"effect is positive" if o and o[1] is not None else "")
                     + ": an expected side effect, not a fault.")
    elif ev or cause or prop:
        bits = []
        if ev:
            bits.append(f"'{ev['name']}' ({ev.get('timing') or ev['when']})")
        if cause:
            bits.append(f"{run.label(cause)} is {_pct(run.node(cause).deviation_pct)} vs normal")
        if prop and not (ev or cause):
            bits.append(f"a new link from {prop['source']} ({prop['status']} on the data)")
        sents.append("Most likely first cause: " + "; ".join(bits) + ".")
    else:
        sents.append("Nothing in the graph or the change log explains it yet.")
    sents.append({"supported": f"Confidence: supported, {len(evidence)} independent signals agree"
                               f" and the path explains about {explained:.0f}% of the move.",
                  "hypothesis": "Confidence: hypothesis" + (
                      f" - the path explains only about {explained:.0f}% of the move." if explained < EXPLAINED_MIN
                      and len(plan["path"]) > 1 else " - only one signal points to it so far."),
                  "unexplained": "Treat this as unexplained for now."}[conf])
    unc = []
    if not seg:
        unc.append("the change is not concentrated in one segment")
    if not ev:
        unc.append("no release or campaign was found at the onset")
    if len(seg) > 1 and not loc.get("pair_checked"):  # e.g. DE and android each concentrated, cell not tested
        unc.append(f"the {' x '.join(seg)} intersection is inferred from separate breakdowns")
    if plan.get("coincident"):
        unc.append(f"'{plan['coincident']['name']}' happened at the same time in the same segment but touches a "
                   f"different system")
    if explained < EXPLAINED_MIN and len(plan["path"]) > 1:
        unc.append(f"about {100 - explained:.0f}% of the KPI move is not covered by this path")
    out.update(headline=" ".join(headline.split()[:15]), summary=" ".join(sents[:4]), path=path, confidence=conf,
               first_cause={"node": fc, "why": fc_why},
               uncertainty=("Open points: " + "; ".join(unc) + ".") if unc else
               f"Effect sizes come from {run.window_h}-hour windows and may shift as more hours arrive.",
               watch_next=list(dict.fromkeys([x for x in [cause or deep, kpi] + [u["node"] for u in
                                                                                 out["unusual"][:2]] if x])))
    return out

# ---------------------------------------------------------------- finalisation (agent output -> Interpretation)


def _finding(run: _Run, f: Any) -> Optional[Finding]:
    if not isinstance(f, dict) or not f.get("node"):
        return None
    n = run.node(str(f["node"]))
    nums = {} if n is None else {k: v for k, v in (("value", n.value), ("expected", n.expected),
                                                   ("deviation_pct", n.deviation_pct), ("z", n.z),
                                                   ("completeness", n.completeness)) if v is not None}
    return Finding(node=str(f["node"]), why=str(f.get("why", ""))[:400], numbers=_r(nums))


def _ensure_node(run: _Run, nid: str, prev: Optional[str]) -> None:
    """Make segment ids ('m|country=DE') and event ids addressable as nodes + edges in the saved snapshot."""
    if nid.startswith("event:"):
        if nid not in run.extra_nodes and nid not in run.nodes:
            run.extra_nodes[nid] = Node(id=nid, label=nid[6:], kind="component", added_by="interpreter")
        if prev and not run.edge_between(prev, nid):
            run.extra_edges.append(Edge(source=nid, target=prev, kind="cause", status="proposed",
                                        label="change event matching timing and segment", added_by="interpreter"))
        return
    if "|" in nid and run.node(nid) is None:
        metric, rest = nid.split("|", 1)
        seg = _parse_seg(rest.replace("|", ","))
        base = run.node(metric)
        val = exp = dev = None
        if len(seg) == 1:
            rows = run.breakdowns.get((metric, next(iter(seg))), [])
            row = next((r for r in rows if str(r.get("segment")) == next(iter(seg.values()))), None)
            if row:
                val, exp, dev = _num(row.get("value")), _num(row.get("expected")), _num(row.get("deviation_pct"))
        loc = run.locs.get(metric) or {}
        if dev is None and loc.get("segment") == seg:
            dev = _num(loc.get("dev"))
        run.extra_nodes[nid] = Node(id=nid, label=f"{base.label if base else metric}: {_seg_text(seg)}",
                                    kind="segment", unit=base.unit if base else None, polarity=base.polarity if base
                                    else None, value=val, expected=exp, deviation_pct=dev,
                                    status="anomalous" if dev is not None and abs(dev) >= 5 else "ok",
                                    added_by="interpreter", depth=(base.depth + 1) if base else 1)
        run.extra_edges.append(Edge(source=nid, target=metric, kind="breakdown", op=",".join(sorted(seg)),
                                    status="validated", added_by="interpreter"))


def _path_edges(run: _Run, path: list[str]) -> list[tuple[str, str]]:
    """Real edges along the path; a node after a segment links back to the nearest earlier node it touches."""
    out = []
    for j, b in enumerate(path[1:], 1):
        e = next((x for x in (run.edge_between(a, b) for a in reversed(path[:j])) if x), None)
        if e is not None:
            out.append((e.source, e.target))
    return out


def _final_snapshot(run: _Run, it: Interpretation) -> GraphSnapshot:
    """The investigated snapshot with node roles plus the interpreter's segment / event nodes and new edges."""
    roles: dict[str, list] = {}
    for role, ids in (("kpi", [run.kpi]), ("on_path", it.path[1:]),
                      ("first_cause", [it.first_cause.node] if it.first_cause else []),
                      ("symptom", [f.node for f in it.symptoms]),
                      ("unusual", [f.node for f in it.unusual + it.missing_data]),
                      ("ruled_out", [f.node for f in it.ruled_out]), ("watch", it.watch_next)):
        for n in ids:
            if n and role not in roles.setdefault(n, []):
                roles[n].append(role)
    keep = set(it.path) | {e.source for e in run.extra_edges} | {e.target for e in run.extra_edges}
    extra = [n for n in run.extra_nodes.values() if n.id in keep and n.id not in run.nodes]
    nodes = [n.model_copy(update={"roles": roles.get(n.id, [])}) for n in list(run.nodes.values()) + extra]
    have = {n.id for n in nodes}
    edges = list(run.edges)
    keys = {(e.source, e.target, e.kind) for e in edges}
    for e in run.extra_edges + run.new_connections:
        if e.source in have and e.target in have and (e.source, e.target, e.kind) not in keys:
            edges.append(e)
            keys.add((e.source, e.target, e.kind))
    notes = list(run.snap.notes) + [f"interpreted ({it.confidence}): {it.headline}"]
    return run.snap.model_copy(update={"nodes": nodes, "edges": edges, "notes": notes})


def interpret(w, kpi: str, incident_id: str | None = None, depth: int = 2, window_h: int = 3,
              on_step: Callable[[TraceStep], None] | None = None, *, api=None,
              snapshot: GraphSnapshot | None = None, onset: str | None = None) -> Interpretation:
    """Investigate one KPI by walking its annotated metric graph; returns and saves an Interpretation.
    api: object exposing the pulse.graph_api functions (default: the module); snapshot: prebuilt (tests / replays);
    onset: the incident's first bad hour (a re-check of a long incident still searches changes around its start)."""
    if api is None:
        from pulse import graph_api as api
    snap = snapshot or api.build_snapshot(w, kpi, center=None, depth=depth, window_h=window_h)
    C.save(snap, f"snapshots/{snap.snapshot_id}.json")
    interp_id = incident_id or f"{kpi}-{re.sub(r'[^0-9]', '', str(snap.ts))[:12]}"
    run = _Run(w, api, snap, interp_id, incident_id, on_step, window_h)
    run.incident_onset = onset
    k = run.node(kpi)
    run.emit("look", kpi, {"kpi": kpi},
             (f"{run.label(kpi)} is {_below(k.deviation_pct)} ({_f(k.value, k.unit)} vs {_f(k.expected, k.unit)} "
              f"expected, {_verdict(k)}); {len(snap.nodes)} connected metrics loaded, "
              f"{sum(1 for n in snap.nodes if _anomalous(n))} unusual.")
             if k is not None and k.deviation_pct is not None else f"Loaded {len(snap.nodes)} metrics around {kpi}.",
             {"value": k.value if k else None, "expected": k.expected if k else None,
              "deviation_pct": k.deviation_pct if k else None, "z": k.z if k else None},
             [kpi] + [n.id for n in snap.nodes if _anomalous(n) or _data_bad(n)][:8], [])
    res = run_agent(ROLE, SYSTEM, render_snapshot(snap), run.tools(), SCHEMA, mock=_mock_policy(run),
                    max_turns=MAX_TURNS, effort="medium", on_call=run.on_call)
    out = dict(res.output or {})
    # sanitise the answer (live mode: ids may be off): path starts at the KPI, keeps known / segment / event ids
    path = [str(p) for p in (out.get("path") or []) if p]
    path = [kpi] + [p for p in path if p != kpi]
    clean: list[str] = []
    for p in path:
        if p not in clean and (run.node(p) is not None or "|" in p or p.startswith("event:")):
            _ensure_node(run, p, clean[-1] if clean else None)
            clean.append(p)
    fc = out.get("first_cause") if isinstance(out.get("first_cause"), dict) else {}
    if fc.get("node") and fc["node"] not in clean:
        _ensure_node(run, str(fc["node"]), clean[-1])
    conf = out.get("confidence")
    conf = conf if conf in ("supported", "hypothesis", "data_issue", "unexplained") else "hypothesis"
    headline = " ".join(str(out.get("headline", "")).split()[:15]) or f"{run.label(kpi)} moved"
    fl = lambda key: [f for f in (_finding(run, x) for x in out.get(key) or []) if f]  # noqa: E731
    it = Interpretation(
        interpretation_id=interp_id, incident_id=incident_id, kpi=kpi, snapshot_id=snap.snapshot_id, ts=snap.ts,
        headline=headline, summary=str(out.get("summary", "")), path=clean, first_cause=_finding(run, fc),
        confidence=conf, symptoms=fl("symptoms"), unusual=fl("unusual"), missing_data=fl("missing_data"),
        ruled_out=fl("ruled_out"), new_connections=run.new_connections, uncertainty=str(out.get("uncertainty", "")),
        watch_next=[str(x) for x in out.get("watch_next") or []], model=res.model, llm_mode=res.mode,
        cost_usd=res.cost_usd)
    run.plan["path"] = clean
    run.emit("conclude", it.first_cause.node if it.first_cause else kpi, {"confidence": conf},
             f"Conclusion ({conf}): {headline}",
             {"path_len": len(clean), "tool_calls": len(res.calls), "hypotheses": out.get("hypotheses") or []},
             clean, _path_edges(run, clean))
    it.trace = run.trace
    C.save(_final_snapshot(run, it), f"snapshots/{snap.snapshot_id}.json")
    C.save(it, f"interpretations/{interp_id}.json")
    (C.STATE_DIR / "interpretations" / f"{interp_id}.partial.json").unlink(missing_ok=True)
    return it

# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Run the Graph Interpreter on one KPI (mock LLM by default).")
    ap.add_argument("kpi")
    ap.add_argument("--at", help="data clock, e.g. '2026-11-17 18:00'")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--window", type=int, default=3)
    ap.add_argument("--incident", default=None)
    ap.add_argument("--prompt", action="store_true", help="also print the graph text sent to the model")
    a = ap.parse_args(argv)
    from pulse.llm import load_dotenv
    load_dotenv()   # ANTHROPIC_API_KEY from .env for PULSE_LLM=live
    from pulse.world import world
    w = world()
    if a.at:
        w.set_now(a.at)
    print(f"Interpreting {a.kpi} at {w.now} (llm={mode()}, state={C.STATE_DIR})")
    if a.prompt:
        from pulse import graph_api
        print(render_snapshot(graph_api.build_snapshot(w, a.kpi, depth=a.depth, window_h=a.window)) + "\n")
    show = lambda st: print(f"  [{st.i:>2}] {st.action:<14} {(st.node or '')[:30]:<30} {st.finding}")  # noqa: E731
    it = interpret(w, a.kpi, incident_id=a.incident, depth=a.depth, window_h=a.window, on_step=show)
    print(f"\n{it.headline}\n{it.summary}\npath: {' -> '.join(it.path)}")
    if it.first_cause:
        print(f"first cause [{it.confidence}]: {it.first_cause.node} - {it.first_cause.why}")
    for name in ("symptoms", "unusual", "missing_data", "ruled_out"):
        if getattr(it, name):
            print(f"{name}: " + "; ".join(f"{f.node} ({f.why})" for f in getattr(it, name)))
    if it.new_connections:
        print("new connections: " + ", ".join(f"{e.source}->{e.target} [{e.status}]" for e in it.new_connections))
    for h in (it.trace[-1].numbers.get("hypotheses") if it.trace else None) or []:
        print(f"  hypothesis [{h['verdict']}] {h['name']}: {h['evidence']}")
    print(f"uncertainty: {it.uncertainty}\nwatch next: {', '.join(it.watch_next)}")


if __name__ == "__main__":
    main()
