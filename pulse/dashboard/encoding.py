"""Graph visual encoding as pure data: MEANING TOKENS, not colours (the web UI maps tokens to CSS variables).

    node_style(node, center=None) -> {"fill", "shape", "size", "rings", "faded", "badge", "sub", ...}
    edge_style(edge)              -> {"line", "arrow", ...}
    LEGEND                        -> [{"group", "token", "label"}] in display order
    trace_frames(interp, snap=None, partial=None) -> frame 0 = before step 1, then one frame per trace step

Tokens
  fill   kpi | harm | gain | shift | ok | missing | stale
         harm = anomalous and moved the bad way for the business (polarity-aware), gain = the good way,
         shift = anomalous with neutral polarity, missing = missing_data / not measured, stale = stale data.
  shape  sun (snapshot centre / KPI) | circle | diamond (added by the Graph Handler)
  rings  formula_incomplete (incomplete or broken formula) | first_cause | path (on the path) | symptom | watch
  faded  stale data or ruled out by the interpreter
  badge  unusual | None
  line   formula (formula + breakdown) | cause (asserted / validated / component) | learned | ambiguous |
         proposed (new connection) | rejected
Extra keys (optional for a renderer): intensity = 0..1 share of the change that is the node's OWN
(1 - inherited_pct/100) so first causes can look hot and inherited symptoms pale; tooltip = ordered rows.
Pure functions over pulse.contracts models or plain dicts with the same fields; no I/O.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from pulse.contracts import Edge, GraphSnapshot, Interpretation, Node

Model = Union[Node, Edge, dict]

LEGEND: list[dict] = [
    {"group": "Nodes", "token": "kpi", "label": "The KPI being watched (the sun)"},
    {"group": "Nodes", "token": "harm", "label": "Moving the wrong way for the business"},
    {"group": "Nodes", "token": "gain", "label": "Moving the right way"},
    {"group": "Nodes", "token": "shift", "label": "Moving, neither good nor bad"},
    {"group": "Nodes", "token": "ok", "label": "Normal, inside its expected band"},
    {"group": "Nodes", "token": "missing", "label": "Missing data"},
    {"group": "Nodes", "token": "stale", "label": "Stale data (old values)"},
    {"group": "Nodes", "token": "faded", "label": "Ruled out by the investigation"},
    {"group": "Nodes", "token": "diamond", "label": "Metric added by the Graph Handler"},
    {"group": "Nodes", "token": "formula_incomplete", "label": "Formula incomplete or broken"},
    {"group": "Nodes", "token": "first_cause", "label": "First cause"},
    {"group": "Nodes", "token": "path", "label": "On the path from KPI to the cause"},
    {"group": "Nodes", "token": "symptom", "label": "Symptom: moved because of the cause"},
    {"group": "Nodes", "token": "unusual", "label": "Unusual, worth a look"},
    {"group": "Links", "token": "formula", "label": "Formula / breakdown: input → metric"},
    {"group": "Links", "token": "cause", "label": "Possible cause (expert knowledge)"},
    {"group": "Links", "token": "learned", "label": "Cause learned from the data"},
    {"group": "Links", "token": "ambiguous", "label": "Ambiguous cause"},
    {"group": "Links", "token": "proposed", "label": "New connection proposed"},
    {"group": "Links", "token": "rejected", "label": "Rejected connection"},
]


def _g(obj: Any, key: str, default: Any = None) -> Any:
    v = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    return default if v is None else v


def fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "–"
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return f"{sign}{abs(v):.1f}%"


def fmt_value(v: Optional[float], unit: Optional[str]) -> str:
    if v is None:
        return "–"
    a = abs(v)
    if unit == "ratio":
        return f"{v * 100:.{2 if a < 0.1 else 1}f}%" if a <= 1 else f"{v:.2f}"
    if unit == "event":
        return "happened" if v else "none"
    s = (f"{v / 1e6:.2f}M" if a >= 1e6 else f"{v / 1e3:.1f}k" if a >= 1e4 else f"{v:,.0f}" if a >= 100
         else f"{v:.2f}" if a < 10 else f"{v:.1f}")
    return "€" + s if unit == "EUR" else s


def move_token(node: Model) -> str:
    """harm / gain / shift for anomalous nodes, ok otherwise (data-quality statuses handled by node_style)."""
    if _g(node, "status") != "anomalous":
        return "ok"
    dev = _g(node, "deviation_pct", _g(node, "own_deviation_pct", 0.0))
    pol = _g(node, "polarity")
    if pol == "up_is_good":
        return "harm" if dev < 0 else "gain"
    if pol == "down_is_good":
        return "harm" if dev > 0 else "gain"
    return "shift"


def own_intensity(node: Model) -> float:
    """0.25..1: share of the change that is the node's own (from inherited_pct), fallback |z|/4."""
    inh = _g(node, "inherited_pct")
    if inh is not None:
        return round(max(0.25, min(1.0, 1 - float(inh) / 100)), 3)
    z = _g(node, "z")
    if z is not None:
        return round(max(0.35, min(1.0, abs(float(z)) / 4)), 3)
    return 0.85


def tooltip(node: Model) -> dict:
    unit, own, inh, comp = _g(node, "unit"), _g(node, "own_deviation_pct"), _g(node, "inherited_pct"), _g(node, "completeness")
    rows = {
        "value": fmt_value(_g(node, "value"), unit),
        "expected": fmt_value(_g(node, "expected"), unit),
        "Δ vs expected": fmt_pct(_g(node, "deviation_pct")),
        "own / inherited": (f"own {fmt_pct(own)} · inherited {'–' if inh is None else f'{inh:.0f}%'}"
                            if own is not None or inh is not None else ""),
        "left its band at": str(_g(node, "onset_ts", "")).replace("T", " ").replace("Z", " UTC"),
        "data completeness": "not measured" if comp is None else f"{comp * 100:.0f}%",
        "formula": _g(node, "definition", ""),
        "formula status": _g(node, "formula_status", ""),
        "added by": "" if _g(node, "added_by", "seed") == "seed" else str(_g(node, "added_by")).replace("_", " "),
        "about": _g(node, "description", ""),
    }
    return {k: v for k, v in rows.items() if v}


def node_style(node: Model, center: Optional[str] = None) -> dict:
    status = _g(node, "status", "ok")
    roles = list(_g(node, "roles", []))
    is_kpi = (_g(node, "id") == center) if center is not None else (_g(node, "kind") == "kpi" or "kpi" in roles)
    if status in ("missing_data", "unknown"):
        fill = "missing"
    elif status == "stale":
        fill = "stale"
    elif is_kpi:
        fill = "kpi"
    else:
        fill = move_token(node)
    rings = []
    if _g(node, "formula_status") in ("incomplete", "broken"):
        rings.append("formula_incomplete")
    if "first_cause" in roles:
        rings.append("first_cause")
    if "on_path" in roles:
        rings.append("path")
    if "symptom" in roles:
        rings.append("symptom")
    if "watch" in roles:
        rings.append("watch")
    shape = "sun" if is_kpi else "diamond" if _g(node, "added_by", "seed") == "graph_handler" else "circle"
    z = _g(node, "z")
    size = 34.0 if is_kpi else round(16.0 + (min(8.0, abs(float(z)) * 2) if z is not None and status == "anomalous" else 0), 1)
    dev = _g(node, "deviation_pct")
    sub = ("no data" if fill == "missing" and _g(node, "value") is None else "stale" if fill == "stale"
           else fmt_pct(dev) if dev is not None else "")
    return {"fill": fill, "shape": shape, "size": size, "rings": rings,
            "faded": status == "stale" or "ruled_out" in roles,
            "badge": "unusual" if "unusual" in roles else None, "sub": sub,
            "intensity": own_intensity(node) if fill in ("harm", "gain", "shift", "kpi") else 0.0,
            "tooltip": tooltip(node)}


def edge_style(edge: Model) -> dict:
    kind, status = _g(edge, "kind", "formula"), _g(edge, "status", "asserted")
    if kind == "proposed" or status == "proposed":
        line = "proposed"
    elif status == "rejected":
        line = "rejected"
    elif kind in ("formula", "breakdown"):
        line = "formula"
    elif status in ("learned", "ambiguous"):
        line = status
    else:
        line = "cause"
    return {"line": line, "arrow": True, "kind": kind, "label": "new" if line == "proposed" else ""}


def _edge_dir(snap: Optional[GraphSnapshot]):
    existing = {(e.source, e.target) for e in snap.edges} if snap else set()

    def norm(a: str, b: str) -> list[str]:
        return [b, a] if snap is not None and (a, b) not in existing and (b, a) in existing else [a, b]
    return norm


def trace_frames(interp: Union[Interpretation, dict], snap: Union[GraphSnapshot, dict, None] = None,
                 partial: Optional[bool] = None) -> list[dict]:
    """Replay frames. Frame 0 = before step 1; frame k = after trace step k (cumulative visited sets).

    The final frame of a finished (non-partial) interpretation has reveal_roles=True and path_edges = the
    KPI -> first cause path. Edges are [source, target]; with `snap` they are flipped to the direction that
    exists in the snapshot. `partial` defaults to interp["partial"] when a dict from the web API is passed.
    """
    if isinstance(interp, dict):
        if partial is None:
            partial = bool(interp.get("partial", False))
        interp = Interpretation.model_validate({k: v for k, v in interp.items() if k not in ("partial", "frames")})
    if isinstance(snap, dict):
        snap = GraphSnapshot.model_validate({k: v for k, v in snap.items() if k != "layout"})
    norm = _edge_dir(snap)
    path = list(interp.path)
    path_edges = [norm(path[i], path[i + 1]) for i in range(len(path) - 1)]
    frames = [{"i": 0, "action": "", "node": None, "numbers": {}, "focus_nodes": [interp.kpi] if interp.kpi else [],
               "visited_nodes": [], "focus_edges": [], "visited_edges": [], "path_edges": [], "reveal_roles": False,
               "caption": "Start: the KPI moved - the agent begins walking its graph."}]
    vn: list[str] = []
    ve: list[list[str]] = []
    n = len(interp.trace)
    for k, s in enumerate(interp.trace):
        fn = list(dict.fromkeys(list(s.highlight_nodes) + ([s.node] if s.node else [])))
        fe = [norm(a, b) for a, b in s.highlight_edges]
        vn += [x for x in fn if x not in vn]
        ve += [e for e in fe if e not in ve]
        final = (k == n - 1) and not partial
        frames.append({"i": s.i, "action": s.action, "node": s.node, "numbers": dict(s.numbers),
                       "focus_nodes": fn, "visited_nodes": list(vn), "focus_edges": fe,
                       "visited_edges": [list(e) for e in ve], "path_edges": path_edges if final else [],
                       "reveal_roles": final,
                       "caption": f"Step {s.i} · {s.action.replace('_', ' ')}: {s.finding}"})
    return frames


def style_snapshot(snap: Union[GraphSnapshot, dict]) -> dict:
    """Convenience: {"nodes": {id: node_style}, "edges": {"src->tgt": edge_style}} for a whole snapshot."""
    if isinstance(snap, dict):
        snap = GraphSnapshot.model_validate({k: v for k, v in snap.items() if k != "layout"})
    return {"nodes": {n.id: node_style(n, center=snap.center) for n in snap.nodes},
            "edges": {f"{e.source}->{e.target}": edge_style(e) for e in snap.edges}}
