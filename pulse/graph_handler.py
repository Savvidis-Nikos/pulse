"""Graph Handler agent (role "graph_handler", Sonnet): extends the metric graph from messy contributor input.

    from pulse.graph_handler import ingest_metric, discover
    changes = ingest_metric(w, "Share of checkout requests that fail on the checkout service")
    changes = ingest_metric(w, {"name": "...", "sql": "SELECT ts, ... FROM ..."})
    changes = discover(w, "checkout_rate")

The contributor never says where the metric belongs. The agent (or its deterministic mock policy) uses tools
(search_data, try_definition, validate_edge, node_detail) to turn the input into a definition; then CODE
decides: the definition must compute on the data, duplicates are detected (symbolic -> alias, numeric ->
mark_ambiguous), formula edges must reconcile, and every cause edge goes through graph_api.validate_edge.
The LLM never decides acceptance. Accepted changes are appended to <PULSE_STATE>/graph_changes.jsonl and
applied to the in-memory world (graph_api.ensure_applied re-applies them after a restart).

CLI:  python -m pulse.graph_handler ingest pulse/inbox_examples/checkout_errors_prose.txt
      python -m pulse.graph_handler discover checkout_rate
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pulse import graph_api as ga
from pulse.contracts import Edge, GraphChange, Node
from pulse.llm import Tool, obj, run_agent

ROLE = "graph_handler"
MAX_CAUSES = 4
NON_ADDITIVE = re.compile(r"(rate|ratio|ctr|share|index|rank|cpc|rpm|roas|util|_ms$|_h$|_min$|sentiment|price$|avg_)")
DOWN_WORDS = re.compile(r"\b(error|errors|fail|failure|failures|crash|crashes|oos|out of stock|refund|refunds|"
                        r"unavailable|complaint|complaints|latency|slow|invalid|chargeback|abandon|timeout)", re.I)
UP_WORDS = re.compile(r"\b(revenue|gmv|orders|conversion|sales|profit|margin|availability|success|clicks)", re.I)
RATIO_SPLIT = re.compile(r"\s+(?:as a share of|as a fraction of|as a percentage of|out of all|out of|divided by|"
                         r"relative to|per|over|of all|of)\s+", re.I)
RATIO_WORDS = re.compile(r"\b(share|fraction|rate|ratio|percentage|percent|proportion|per)\b", re.I)
DENOM_WORDS = ("requests", "attempts", "sessions", "impressions", "views", "searches", "orders", "visits",
               "due_units", "auth_attempts", "listed_skus", "app_sessions")

SYSTEM = """You are the Graph Handler of Pulse, an agentic monitoring system for an online marketplace.
You maintain the company's metric graph: metrics connected by formula edges (input -> metric), breakdowns by
dimension (country, platform, category, ...) and possible-cause edges (cause -> effect, with sign and lag).
A contributor hands you a new metric in whatever form they have: free prose, partial YAML, a SQL query, or a
raw column name with a description. They never say where it belongs in the graph. Your job:
1. search_data to find the raw columns and existing metrics the input refers to (raw columns marked
   'unmodelled' are company data the graph does not use yet);
2. try_definition until you have a definition that computes on the data (simple = sum of a column on a table;
   ratio = numerator / denominator metric names; derived = arithmetic over metric names; sql = SELECT ts,
   [dims...], value | num, den FROM <table> GROUP BY ...). If you need a raw column as a building block,
   declare it in base_metrics (name, table, column) and refer to it by that name;
3. propose cause edges between the new metric and existing metrics only when a business mechanism is
   plausible, and check them with validate_edge.
Code re-validates everything numerically; your proposals are hypotheses. Use snake_case names. Be concise."""

FINAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "description", "type", "table", "column", "numerator", "denominator", "expr", "sql",
                 "unit", "polarity", "base_metrics", "proposed_edges", "notes"],
    "properties": {
        "name": {"type": "string"}, "description": {"type": "string"},
        "type": {"type": "string", "enum": ["simple", "ratio", "derived", "sql"]},
        "table": {"type": ["string", "null"]}, "column": {"type": ["string", "null"]},
        "numerator": {"type": ["string", "null"]}, "denominator": {"type": ["string", "null"]},
        "expr": {"type": ["string", "null"]}, "sql": {"type": ["string", "null"]},
        "unit": {"type": "string"},
        "polarity": {"type": "string", "enum": ["up_is_good", "down_is_good", "neutral"]},
        "base_metrics": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "table", "column", "description"],
            "properties": {"name": {"type": "string"}, "table": {"type": "string"}, "column": {"type": "string"},
                           "description": {"type": "string"}}}},
        "proposed_edges": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["source", "target", "sign", "lag_h", "mechanism"],
            "properties": {"source": {"type": "string"}, "target": {"type": "string"},
                           "sign": {"type": "integer", "enum": [1, -1]}, "lag_h": {"type": ["integer", "null"]},
                           "mechanism": {"type": "string"}}}},
        "notes": {"type": "string"},
    },
}


# ====================================================================== helpers
def _ts(w) -> str:
    return pd.Timestamp(w.now).isoformat()


def _snake(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s or not s[0].isalpha():
        s = "m_" + s
    return s[:48]


def _forget(w, name: str) -> None:
    """Drop a probe metric's engine cache entries without flushing everything."""
    eng = w.engine
    for k in [k for k in eng._cache if k[0] == name]:
        eng._cache.pop(k, None)
    for d in (eng._shape, eng._dims, eng._sql_cache):
        d.pop(name, None)
    w.reg.__dict__.pop("_expand_cache", None)


def _base_name(w, table: str, column: str) -> str:
    """Name for a new base metric on a raw column (avoid clashing with existing metrics)."""
    for m in w.reg:
        if m.type == "simple" and m.table == table and m.column == column:
            return m.name
    name = _snake(column)
    if name in w.reg:
        name = _snake(f"{table}_{column}")
    return name


def _base_spec(w, b: dict) -> dict:
    col = b["column"]
    return {"name": b["name"], "type": "simple", "table": b["table"], "column": col,
            "agg": "mean" if NON_ADDITIVE.search(col) else "sum",
            "time_agg": "mean" if NON_ADDITIVE.search(col) else "sum",
            "description": b.get("description") or f"{col} from {b['table']}",
            "unit": "count", "domain": _domain_of_table(w, b["table"])}


def _domain_of_table(w, table: str) -> str:
    from collections import Counter
    doms = Counter(m.domain for m in w.reg if m.type == "simple" and m.table == table)
    return doms.most_common(1)[0][0] if doms else "other"


def _spec_dict(d: dict) -> dict:
    keys = ("name", "type", "description", "table", "column", "agg", "numerator", "denominator", "expr", "sql",
            "unit", "polarity", "domain", "tier", "time_agg")
    return {k: d[k] for k in keys if d.get(k) not in (None, "")}


# ====================================================================== tools
def try_definition(w, definition: dict, base_metrics: list | None = None) -> dict:
    """Compute a candidate definition on the data without committing it. Returns hours, last value, dims,
    shape and numeric duplicates among existing metrics."""
    from argus.semantic.registry import MetricSpec
    from argus.semantic.extend import _numeric_duplicates
    ga.ensure_applied(w)
    added = []
    probe = "probe_candidate"
    try:
        for b in base_metrics or []:
            if b["name"] not in w.reg:
                if b.get("column") not in w.wh.columns(b.get("table") or ""):
                    return {"ok": False, "error": f"unknown column {b.get('table')}.{b.get('column')}"}
                w.reg.add(MetricSpec(**_base_spec(w, b)), persist=False)
                added.append(b["name"])
        d = dict(definition)
        d["name"] = probe
        d.setdefault("type", "sql" if d.get("sql") else "ratio" if d.get("numerator") else
                     "derived" if d.get("expr") else "simple")
        spec = MetricSpec(**_spec_dict(d))
        unknown = [r for r in spec.references() if r not in w.reg]
        if unknown:
            return {"ok": False, "error": f"unknown metrics referenced: {unknown}"}
        if spec.type == "simple" and spec.column not in w.wh.columns(spec.table or ""):
            return {"ok": False, "error": f"unknown column {spec.table}.{spec.column}"}
        w.reg.add(spec, persist=False)
        s = w.engine.total_series(probe).astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        dims = w.engine.dims(probe)
        sh = w.engine.shape(probe)
        dups = _numeric_duplicates(w, probe, s) if len(s) >= 48 else []
        dups = [x for x in dups if x not in added]
        near = _near_duplicates(w, probe, s, exclude=set(added) | set(dups))
        return {"ok": len(s) >= 48, "hours": int(len(s)), "last": ga._r(s.iloc[-1]) if len(s) else None,
                "mean": ga._r(s.mean()) if len(s) else None, "dims": dims, "additive": sh.additive,
                "ratio_like": sh.ratio_like, "numeric_duplicates": dups, "near_duplicates": near,
                **({} if len(s) >= 48 else {"error": f"only {len(s)} hourly values"})}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if probe in w.reg:
            w.reg.remove(probe)
        _forget(w, probe)
        for b in added:
            w.reg.remove(b)
            _forget(w, b)


def _near_duplicates(w, name: str, s: pd.Series, exclude: set) -> list[dict]:
    """Existing metrics that track the candidate almost perfectly (corr >= 0.98, same scale within 25%)."""
    out = []
    unit_like = s.tail(24 * 7)
    for m in w.reg:
        if m.name in (name, *exclude) or not w.engine.shape(m.name).additive == w.engine.shape(name).additive:
            continue
        try:
            o = w.engine.total_series(m.name).astype(float).tail(24 * 7)
        except Exception:
            continue
        df = pd.concat([unit_like, o], axis=1, join="inner").replace([np.inf, -np.inf], np.nan).dropna()
        if len(df) < 48 or df.iloc[:, 0].std() == 0 or df.iloc[:, 1].std() == 0:
            continue
        corr = float(df.corr().iloc[0, 1])
        ratio = float((df.iloc[:, 0] / df.iloc[:, 1].replace(0, np.nan)).median())
        if corr >= 0.98 and 0.75 <= ratio <= 1.33:
            out.append({"metric": m.name, "corr": round(corr, 4), "ratio_median": round(ratio, 4)})
    out.sort(key=lambda d: -d["corr"])
    return out[:3]


def _tools(w) -> list[Tool]:
    def t_validate(source, target, kind="cause", sign=None, lag_h=None, label=""):
        ch = ga.validate_edge(w, Edge(source=source, target=target, kind=kind, sign=sign, lag_h=lag_h, label=label))
        return {"action": ch.action, "status": ch.edge.status if ch.edge else None, "reason": ch.reason,
                "evidence": ch.evidence}

    def t_detail(node):
        d = ga.node_detail(w, node)
        return {k: v for k, v in d.items() if k != "series_48h"}

    return [
        Tool("search_data", "Search raw warehouse columns and existing metrics related to a text query. Hits marked "
             "'unmodelled' are raw data not yet in the metric graph.", obj({"query": {"type": "string"}}),
             lambda query: ga.search_data(w, query)),
        Tool("try_definition", "Compute a candidate metric definition on the data (not committed). definition has "
             "type simple|ratio|derived|sql and the matching fields (table+column | numerator+denominator | expr | "
             "sql). base_metrics declares raw columns used as building blocks: [{name, table, column, description}].",
             obj({"definition": {"type": "object"}, "base_metrics": {"type": "array", "items": {"type": "object"}}},
                 ["definition"]),
             lambda definition, base_metrics=None: try_definition(w, definition, base_metrics)),
        Tool("validate_edge", "Numerically test an edge between two existing metrics. kind cause: lagged correlation "
             "of de-seasonalised residuals + sign check. kind formula: label may carry 'target = expression'.",
             obj({"source": {"type": "string"}, "target": {"type": "string"},
                  "kind": {"type": "string", "enum": ["cause", "formula"]},
                  "sign": {"type": ["integer", "null"]}, "lag_h": {"type": ["integer", "null"]},
                  "label": {"type": "string"}}, ["source", "target", "kind"]), t_validate),
        Tool("node_detail", "Definition, current value vs expected, dims and neighbours of an existing metric.",
             obj({"node": {"type": "string"}}), t_detail),
    ]


# ====================================================================== input normalisation
def _normalise(raw) -> dict:
    """Any input -> {text, item}: `item` holds whatever structured fields could be parsed."""
    if isinstance(raw, dict):
        item = dict(raw)
        text = " ".join(str(item.get(k, "")) for k in ("name", "description", "column", "notes") if item.get(k))
        return {"text": text, "item": item}
    s = str(raw).strip()
    item: dict = {}
    if not re.match(r"^\s*(select|with)\b", s, re.I):
        try:
            y = yaml.safe_load(s)
            if isinstance(y, list) and y and isinstance(y[0], dict):
                y = y[0]
            if isinstance(y, dict):
                item = {str(k).lower(): v for k, v in y.items()}
        except yaml.YAMLError:
            pass
    if not item:
        m = re.search(r"\b(select|with)\b.*", s, re.I | re.S)
        if m:
            item["sql"] = m.group(0).strip().rstrip(";")
            head = s[: m.start()].strip()
            comments = " ".join(re.findall(r"--\s*(.*)", s))
            item["description"] = (head + " " + comments).strip(" -#:\n")
        else:
            item["description"] = s
    # sql comments inside YAML / sql items are descriptions too
    if item.get("sql"):
        comments = " ".join(re.findall(r"--\s*(.*)", str(item["sql"])))
        if comments and not item.get("description"):
            item["description"] = comments
        item["sql"] = re.sub(r"--[^\n]*", " ", str(item["sql"])).strip().rstrip(";")
    text = " ".join(str(item.get(k, "")) for k in ("name", "description", "column", "notes") if item.get(k))
    return {"text": text, "item": item}


# ====================================================================== deterministic mock policy
def _hits(calls: list[dict], query: str | None = None) -> list[dict]:
    for c in calls:
        if c["tool"] == "search_data" and not c["error"] and (query is None or c["args"].get("query") == query):
            try:
                return json.loads(c["result"])
            except Exception:
                return []
    return []


def _pick(hits: list[dict], prefer_table: str | None = None, avoid: set | None = None) -> dict | None:
    avoid = avoid or set()
    hs = [h for h in hits if h.get("name") not in avoid and h.get("metric") not in avoid]
    if prefer_table:
        same = [h for h in hs if h.get("table") == prefer_table]
        if same:
            return same[0]
    return hs[0] if hs else None


def _as_metric(w, h: dict, base: list) -> str:
    """A search hit as a metric name; unmodelled columns become new base metrics."""
    if h.get("metric"):
        return h["metric"]
    name = _base_name(w, h["table"], h["column"])
    if name not in w.reg and all(b["name"] != name for b in base):
        base.append({"name": name, "table": h["table"], "column": h["column"],
                     "description": f"{h['column'].replace('_', ' ')} ({h['table']})"})
    return name


def _mock_definition(w, norm: dict, calls: list[dict]) -> tuple[dict, list]:
    item, text = norm["item"], norm["text"]
    base: list = []
    if item.get("sql"):
        return {"type": "sql", "sql": item["sql"]}, base
    for k in ("expr", "numerator"):
        if item.get(k):
            return ({"type": "derived", "expr": item["expr"]} if k == "expr" else
                    {"type": "ratio", "numerator": item["numerator"], "denominator": item["denominator"]}), base
    m = re.search(r"\b([a-z_]+)\.([a-z_0-9]+)\b", text)
    if item.get("table") and item.get("column"):
        m = None
        tab, col = item["table"], item["column"]
    elif m and w.wh.has(m.group(1)) and m.group(2) in w.wh.columns(m.group(1)):
        tab, col = m.group(1), m.group(2)
    else:
        tab = col = None
    if tab and col:
        return {"type": "simple", "table": tab, "column": col,
                "agg": "mean" if NON_ADDITIVE.search(col) else "sum"}, base
    # prose: ratio if it reads like "X per/of/as a share of Y"
    desc = item.get("description") or text
    first = re.split(r"[.;\n]", desc)[0]
    parts = RATIO_SPLIT.split(first, maxsplit=1)
    if RATIO_WORDS.search(first) and len(parts) == 2:
        left, right = parts
        left = re.sub(r"^\s*(the\s+)?(share|fraction|rate|ratio|percentage|proportion|number)\s*(of)?\s*", "", left,
                      flags=re.I)
        hl, hr = _hits(calls, left), _hits(calls, right)
        num = _pick([h for h in hl if h["kind"] == "column"] + hl)
        den = _pick([h for h in hr if any(x in (h.get("column") or h["name"]) for x in DENOM_WORDS)] + hr,
                    prefer_table=num.get("table") if num else None, avoid={num["name"]} if num else set())
        if num and den:
            return {"type": "ratio", "numerator": _as_metric(w, num, base),
                    "denominator": _as_metric(w, den, base)}, base
    h = _pick([x for x in _hits(calls) if x["kind"] == "column" and x["status"] == "unmodelled"] + _hits(calls))
    if h:
        if h["kind"] == "metric":
            return {"type": "derived", "expr": h["metric"]}, base
        return {"type": "simple", "table": h["table"], "column": h["column"],
                "agg": "mean" if NON_ADDITIVE.search(h["column"]) else "sum"}, base
    return {}, base


def _mock_name(w, norm: dict, d: dict) -> str:
    if norm["item"].get("name"):
        return _snake(str(norm["item"]["name"]))
    m = re.match(r"^\s*([a-z][a-z0-9_]*_[a-z0-9_]+)\s*[:=-]", norm["item"].get("description") or norm["text"])
    if m:
        return m.group(1)
    text = norm["text"].lower()
    if d.get("type") == "ratio":
        num = d["numerator"]
        stem = num[:-1] if num.endswith("s") else num
        suffix = "share" if re.search(r"\bshare|fraction|proportion|percentage\b", text) else "rate"
        return _snake(f"{stem}_{suffix}")
    if d.get("type") == "simple":
        return _snake(f"{d['table']}_{d['column']}" if d["column"] in w.reg else d["column"])
    toks = [t for t in ga._tokens(text) if t.isalpha()][:4]
    return _snake("_".join(toks) or "new_metric")


def _mock_polarity(text: str) -> str:
    if DOWN_WORDS.search(text):
        return "down_is_good"
    if UP_WORDS.search(text):
        return "up_is_good"
    return "neutral"


def _mock_policy(w, norm: dict):
    """Scripted Graph Handler: search -> define -> test -> propose. Deterministic, uses the real data."""
    state: dict = {}

    def policy(calls: list[dict]) -> dict:
        n = len(calls)
        text = norm["text"]
        desc = norm["item"].get("description") or text
        if n == 0:
            qs = [text]
            first = re.split(r"[.;\n]", desc)[0]
            parts = RATIO_SPLIT.split(first, maxsplit=1)
            if len(parts) == 2:
                left = re.sub(r"^\s*(the\s+)?(share|fraction|rate|ratio|percentage|proportion|number)\s*(of)?\s*", "",
                              parts[0], flags=re.I)
                qs += [left, parts[1]]
            return {"tools": [("search_data", {"query": q}) for q in qs if q.strip()]}
        if "def" not in state:
            d, base = _mock_definition(w, norm, calls)
            state["def"], state["base"] = d, base
            if not d:
                return {"final": _final(w, norm, {"type": "simple"}, [], [], "nothing in the data matches")}
            return {"tools": [("try_definition", {"definition": d, "base_metrics": base})]}
        if "edges" not in state:
            # mechanisms the contributor hinted at ("should explain checkout drops"): mentioned metrics outside
            # the definition become proposed cause edges
            state["edges"] = []
            m = re.search(r"(explain|affect|drive|impact|cause|hurt|lower|raise|predict)\w*\s+(.*)", text, re.I)
            if m:
                refs = set(re.findall(r"[a-z][a-z0-9_]*", json.dumps(state["def"]))) | {b["name"] for b in state["base"]}
                hits = [h for h in ga.search_data(w, m.group(2)) if h["kind"] == "metric" and h["metric"] not in refs]
                pol = _mock_polarity(text)
                for h in hits[:2]:
                    tpol = w.reg[h["metric"]].polarity
                    sign = -1 if (pol == "down_is_good") != (tpol == "down_is_good") else 1
                    state["edges"].append({"source": "__new__", "target": h["metric"], "sign": sign, "lag_h": None,
                                           "mechanism": f"contributor hint: '{m.group(0)[:80]}'"})
            return {"final": _final(w, norm, state["def"], state["base"], state["edges"], "mock policy")}
        return {"final": _final(w, norm, state["def"], state["base"], state["edges"], "mock policy")}
    return policy


def _final(w, norm: dict, d: dict, base: list, edges: list, notes: str) -> dict:
    name = _mock_name(w, norm, d) if d else "new_metric"
    for e in edges:
        e["source"] = name if e["source"] == "__new__" else e["source"]
    item = norm["item"]
    return {"name": name, "description": str(item.get("description") or norm["text"])[:300],
            "type": d.get("type", "simple"), "table": d.get("table"), "column": d.get("column"),
            "numerator": d.get("numerator"), "denominator": d.get("denominator"), "expr": d.get("expr"),
            "sql": d.get("sql"), "unit": str(item.get("unit") or ("ratio" if d.get("type") == "ratio" else "count")),
            "polarity": item.get("polarity") or _mock_polarity(norm["text"]),
            "base_metrics": base, "proposed_edges": edges, "notes": notes}


# ====================================================================== code-side validation + commit
def _node(w, name: str, spec: dict, fstatus: str | None) -> Node:
    s = ga.stats(w, name) if name in w.reg else {}
    return Node(id=name, label=ga._label(name), kind="metric", domain=spec.get("domain"), tier=spec.get("tier"),
                unit=spec.get("unit"), polarity=spec.get("polarity"), description=spec.get("description", ""),
                definition=ga._definition(w, name) if name in w.reg else None, value=s.get("value"),
                expected=s.get("expected"), deviation_pct=s.get("deviation_pct"), z=s.get("z"),
                completeness=s.get("completeness"), status=s.get("status", "unknown"), formula_status=fstatus,
                added_by="graph_handler")


def _commit(w, ch: GraphChange, out: list) -> None:
    ga.record_change(ch)
    ga.apply_change(w, ch)
    st = w.__dict__.get("_pulse_applied")
    if st is not None:   # this process already applied it; don't re-apply on the next ensure_applied
        p = ga._changes_path()
        st.update(path=str(p), size=p.stat().st_size, n=st.get("n", 0) + 1)
    out.append(ch)


def ingest_metric(w, raw: dict | str) -> list[GraphChange]:
    """Turn a messy metric definition into graph changes: add_node (+ new base metrics), formula edges,
    validated / ambiguous cause edges, rejections. Persists every change via graph_api.record_change."""
    from argus.semantic import expr as X
    from argus.semantic.extend import cause_candidates, find_identities, rules_meta
    from argus.semantic.registry import MetricSpec
    ga.ensure_applied(w)
    norm = _normalise(raw)
    ts = _ts(w)
    prompt = (f"New metric from a contributor (raw input below). Find what it measures in the data, give it a "
              f"definition that computes, and propose cause edges you can justify.\n\n---\n"
              f"{raw if isinstance(raw, str) else json.dumps(raw, default=str)}\n---")
    res = run_agent(ROLE, SYSTEM, prompt, _tools(w), FINAL_SCHEMA, mock=_mock_policy(w, norm), max_turns=10)
    out_def = res.output
    name = _snake(out_def.get("name") or "new_metric")
    out: list[GraphChange] = []

    # already handled earlier (idempotent re-ingest)
    if name in w.reg:
        prev = [c for c in ga.load_changes() if (c.node and c.node.id == name) or
                (c.edge and name in (c.edge.source, c.edge.target))]
        if prev:
            return prev
        return [GraphChange(ts=ts, action="mark_ambiguous", node=Node(id=name, label=ga._label(name)),
                            reason=f"a metric named {name} already exists in the catalog; not re-added")]

    # new base metrics first (raw columns becoming metrics)
    base_specs = []
    for b in out_def.get("base_metrics") or []:
        b = {**b, "name": _snake(b["name"])}
        if b["name"] in w.reg or b.get("column") not in w.wh.columns(b.get("table") or ""):
            continue
        base_specs.append(_base_spec(w, b))
    d = {k: out_def.get(k) for k in ("type", "table", "column", "numerator", "denominator", "expr", "sql")}
    probe = try_definition(w, {k: v for k, v in d.items() if v}, out_def.get("base_metrics") or [])
    if not probe.get("ok"):
        return [GraphChange(ts=ts, action="reject_edge", node=Node(id=name, label=ga._label(name), status="unknown"),
                            reason=f"definition does not compute on the data: {probe.get('error')}",
                            evidence={"definition": d, "probe": probe})]
    # symbolic duplicate -> alias
    try:
        if d["type"] in ("ratio", "derived") and not base_specs:
            tmp = MetricSpec(**_spec_dict({**d, "name": "probe_alias"}))
            w.reg.add(tmp, persist=False)
            e_new = X.expand("probe_alias", w.reg)
            twin = next((m.name for m in w.reg if m.name != "probe_alias" and m.type in ("ratio", "derived")
                         and X.equivalent(e_new, X.expand(m.name, w.reg))), None)
            w.reg.remove("probe_alias")
            _forget(w, "probe_alias")
            if twin:
                ch = GraphChange(ts=ts, action="update_formula", node=_node(w, twin, {}, "validated"),
                                 reason=f"{name} is symbolically identical to {twin} ({X.pretty(e_new)}): "
                                        f"registered as an alias, no new node", evidence={"alias": name, "of": twin})
                _commit(w, ch, out)
                return out
    except Exception:
        if "probe_alias" in w.reg:
            w.reg.remove("probe_alias")

    # commit base metrics and the metric itself (in memory + change log)
    for bs in base_specs:
        bspec = MetricSpec(**bs, added_by="graph_handler", added_at=ts)
        w.reg.add(bspec, persist=False)
        _forget(w, bs["name"])
        ch = GraphChange(ts=ts, action="add_node", node=_node(w, bs["name"], bs, None),
                         reason=f"raw column {bs['table']}.{bs['column']} was unmodelled; added as a base metric "
                                f"({bs['agg']} per hour)",
                         evidence={"spec": bspec.model_dump(exclude_defaults=True) | {"name": bspec.name,
                                                                                      "type": bspec.type}})
        _commit(w, ch, out)
    full = {**d, "name": name, "description": out_def.get("description", ""), "unit": out_def.get("unit", "count"),
            "polarity": out_def.get("polarity", "neutral")}
    spec = MetricSpec(**_spec_dict(full), added_by="graph_handler", added_at=ts)
    w.reg.add(spec, persist=False)
    _forget(w, name)
    meta = rules_meta(w, spec)
    spec.domain, spec.tier = meta.get("domain", "other"), meta.get("tier", "driver")
    spec.components = [c for c in meta.get("components", [])][:3]
    dups, near = probe.get("numeric_duplicates") or [], probe.get("near_duplicates") or []
    fstatus = "validated" if spec.type in ("ratio", "derived") else None
    ev = {"spec": spec.model_dump(exclude_defaults=True) | {"name": name, "type": spec.type}, "probe": probe,
          "placement": {"domain": spec.domain, "tier": spec.tier, "components": spec.components},
          "agent": {"mode": res.mode, "turns": res.turns, "tools": [c["tool"] for c in res.calls],
                    "notes": out_def.get("notes", "")}}
    reason = (f"new metric {name} = {ga._definition(w, name)}; computes on {probe.get('hours')} hours, "
              f"dims {probe.get('dims')}; placed in {spec.domain}/{spec.tier}"
              + (f", components {spec.components}" if spec.components else ""))
    _commit(w, GraphChange(ts=ts, action="add_node", node=_node(w, name, spec.model_dump(), fstatus), reason=reason,
                           evidence=ev), out)

    # duplicates on the data (not symbolic) -> ambiguous
    if dups or near:
        twin = (dups or [near[0]["metric"]])[0]
        how = ("numerically identical on every hour" if dups else
               f"tracks it at corr {near[0]['corr']} with ratio {near[0]['ratio_median']}")
        e = Edge(source=twin, target=name, kind="formula", status="ambiguous", op="=",
                 label=f"{name} may be the same quantity as {twin} ({how})", added_by="graph_handler")
        _commit(w, GraphChange(ts=ts, action="mark_ambiguous", edge=e,
                               reason=f"{name} {how} to {twin}, but the definitions differ: same measure from "
                                      f"another source, or a subset? Kept and flagged for a human.",
                               evidence={"numeric_duplicates": dups, "near_duplicates": near}), out)

    # formula edges of its own definition: reconcile numerically
    if spec.type in ("ratio", "derived"):
        expr = (f"{spec.numerator} / {spec.denominator}" if spec.type == "ratio" else spec.expr)
        fit = ga.try_formula(w, name, expr)
        for ref in sorted(spec.references()):
            e = Edge(source=ref, target=name, kind="formula",
                     status="validated" if fit.get("verdict") == "exact" else "ambiguous",
                     op="/" if spec.type == "ratio" and ref == spec.denominator else "*",
                     strength=1.0 if fit.get("verdict") == "exact" else None,
                     label=f"{name} = {fit.get('expr', expr)}", added_by="graph_handler")
            _commit(w, GraphChange(ts=ts, action="add_edge" if e.status == "validated" else "mark_ambiguous",
                                   edge=e, reason=f"definition reconciles on {fit.get('n_hours')} hours "
                                                  f"(max residual {(fit.get('fit') or {}).get('max_rel_residual')})",
                                   evidence=fit.get("fit") or {}), out)
        # identities that now hold (A = new x B), validated numerically
        try:
            s = w.engine.total_series(name).astype(float).dropna()
            for idt in find_identities(w, name, s)[:3]:
                rhs = " * ".join(p for p, _ in idt["parts"]) if idt["kind"] == "product" else \
                    " + ".join(f"{c:g}*{p}" if p != "1" else f"{c:g}" for p, c in idt["parts"])
                chk = ga.validate_edge(w, Edge(source=name, target=idt["lhs"], kind="formula",
                                               label=f"{idt['lhs']} = {rhs}", added_by="graph_handler"))
                if chk.action != "reject_edge" and idt["lhs"] != name:
                    chk.edge.added_by = "graph_handler"
                    chk.reason = f"identity found: {idt['text']} ({idt['how']}); " + chk.reason
                    _commit(w, chk, out)
        except Exception:
            pass

    # cause edges: agent proposals + correlation candidates, all validated by code
    proposals = []
    for pe in out_def.get("proposed_edges") or []:
        if pe["source"] in w.reg and pe["target"] in w.reg and name in (pe["source"], pe["target"]):
            proposals.append((pe["source"], pe["target"], pe.get("sign"), pe.get("lag_h"), pe.get("mechanism", "")))
    try:
        for c in cause_candidates(w, name, top=6):
            if c["cause"] in w.reg and c["effect"] in w.reg:
                proposals.append((c["cause"], c["effect"], c["sign"], c["lag_h"],
                                  f"learned: residuals co-move (corr {c['corr']:+.2f}, lag {c['lag_h']} h)"))
    except Exception:
        pass
    seen = set()
    n_ok = 0
    twins = set(dups) | {x["metric"] for x in near}
    for src, tgt, sign, lag, mech in proposals:
        if (src, tgt) in seen or (tgt, src) in seen or n_ok >= MAX_CAUSES:
            continue
        seen.add((src, tgt))
        if src in spec.references() or tgt in spec.references():
            continue   # formula relatives correlate mechanically
        other = tgt if src == name else src
        if twins and any(other == t or ga._formula_relatives(w, other, t)
                         or abs(ga._residual_corr(w, other, t, 0)[0] or 0) >= 0.7 for t in twins):
            continue   # correlates of its own (near-)duplicate say nothing new
        chk = ga.validate_edge(w, Edge(source=src, target=tgt, kind="cause", sign=sign, lag_h=lag, label=mech,
                                       added_by="graph_handler"))
        chk.edge.added_by = "graph_handler"
        if chk.action == "add_edge":
            chk.edge.status = "learned" if mech.startswith("learned") else "validated"
            if mech.startswith("learned") and not chk.edge.lag_h:
                chk.action, chk.edge.status = "mark_ambiguous", "ambiguous"
                chk.reason += "; same-hour co-movement gives no direction (common driver?): kept as ambiguous"
        if chk.action == "reject_edge" and mech.startswith("learned"):
            continue   # don't log rejections of our own weak candidates; log rejected agent proposals
        _commit(w, chk, out)
        n_ok += chk.action != "reject_edge"
    return out


def discover(w, around: str, max_new: int = 2) -> list[GraphChange]:
    """Find raw columns related to `around` that the graph does not model yet, propose metrics on them and
    keep only those whose relation to `around` holds on the data (validated or ambiguous cause edge)."""
    ga.ensure_applied(w)
    if around not in w.reg:
        raise KeyError(f"unknown metric {around!r}")
    spec = w.reg[around]
    query = f"{around} {spec.description}"
    out: list[GraphChange] = []

    def policy(calls):
        if not calls:
            return {"tools": [("search_data", {"query": query})]}
        hits = [h for h in _hits(calls) if h["kind"] == "column" and h["status"] == "unmodelled"
                and h.get("table_kind") == "hourly"]
        tried = [c for c in calls if c["tool"] == "try_definition"]
        if not tried and hits:
            tools = []
            for h in hits[:4]:
                b = {"name": _base_name(w, h["table"], h["column"]), "table": h["table"], "column": h["column"],
                     "description": f"{h['column'].replace('_', ' ')} ({h['table']})"}
                tools.append(("try_definition", {"definition": {"type": "derived", "expr": b["name"]},
                                                 "base_metrics": [b]}))
            return {"tools": tools}
        cands = []
        for c in tried:
            try:
                r = json.loads(c["result"])
            except Exception:
                continue
            b = c["args"]["base_metrics"][0]
            if r.get("ok") and not r.get("numeric_duplicates"):
                cands.append(b)
        return {"final": {"candidates": cands[:max_new + 2]}}

    item = obj({k: {"type": "string"} for k in ("name", "table", "column", "description")})
    schema = obj({"candidates": {"type": "array", "items": item}})   # structured outputs: every object closed
    res = run_agent(ROLE, SYSTEM, f"Find unmodelled raw data related to the metric {around} ({spec.description}) "
                    "and propose base metrics on it (try_definition each). Return candidates "
                    "[{name, table, column, description}].", _tools(w), schema, mock=policy, max_turns=6)
    for b in res.output.get("candidates", []):
        if len([c for c in out if c.action == "add_node"]) >= max_new:
            break
        b = {**b, "name": _snake(b.get("name") or b["column"])}
        if b["name"] in w.reg or b.get("column") not in w.wh.columns(b.get("table") or ""):
            continue
        from argus.semantic.registry import MetricSpec
        bs = _base_spec(w, b)
        mspec = MetricSpec(**bs, added_by="graph_handler", added_at=_ts(w))
        w.reg.add(mspec, persist=False)
        _forget(w, b["name"])
        shared = set(w.engine.dims(b["name"])) & set(w.engine.dims(around))
        chk = ga.validate_edge(w, Edge(source=b["name"], target=around, kind="cause", sign=None, lag_h=None,
                                       label=f"discovered: {b['description']} co-moves with {around}",
                                       added_by="graph_handler")) if shared else None
        w.reg.remove(b["name"])
        _forget(w, b["name"])
        if chk is None or chk.action == "reject_edge":
            continue
        w.reg.add(mspec, persist=False)
        _forget(w, b["name"])
        ch = GraphChange(ts=_ts(w), action="add_node", node=_node(w, b["name"], bs, None),
                         reason=f"discovered unmodelled column {b['table']}.{b['column']} related to {around}",
                         evidence={"spec": mspec.model_dump(exclude_defaults=True) | {"name": mspec.name,
                                                                                      "type": "simple"},
                                   "relation": chk.evidence})
        _commit(w, ch, out)
        chk.edge.added_by = "graph_handler"
        if chk.action == "add_edge":
            chk.edge.status = "learned"
            if not chk.edge.lag_h:
                chk.action, chk.edge.status = "mark_ambiguous", "ambiguous"
                chk.reason += "; same-hour co-movement gives no direction: kept as ambiguous"
        _commit(w, chk, out)
    return out


# ====================================================================== CLI
def _main(argv: list[str]) -> int:
    import os
    if len(argv) < 2 or argv[0] not in ("ingest", "discover"):
        print("usage: python -m pulse.graph_handler ingest <file> | discover <metric>")
        return 2
    from pulse.llm import load_dotenv
    load_dotenv()   # ANTHROPIC_API_KEY from .env for PULSE_LLM=live
    from pulse.world import world
    w = world()
    if os.environ.get("PULSE_NOW"):
        w.set_now(os.environ["PULSE_NOW"])
    if argv[0] == "ingest":
        raw = Path(argv[1]).read_text(encoding="utf-8")
        changes = ingest_metric(w, raw)
    else:
        changes = discover(w, argv[1])
    for c in changes:
        tgt = c.node.id if c.node else (f"{c.edge.source} -> {c.edge.target} [{c.edge.kind}/{c.edge.status}]"
                                        if c.edge else "")
        print(f"{c.action:15s} {tgt}\n    {c.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
