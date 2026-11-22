"""Shared data contracts for Pulse. Every module and the dashboard talk ONLY through these models.

Layout of the state directory (default `.pulse/`, override with PULSE_STATE):
  state.json                       TickState      latest tick: KPI cards
  incidents.json                   list[Incident]
  snapshots/<incident_id>.json     GraphSnapshot  annotated graph behind the KPI at investigation time
  interpretations/<incident_id>.json  Interpretation  the interpreter's conclusion + traversal trace
  brief.json                       Brief          CEO briefing (plain English)
  graph_changes.jsonl              GraphChange    one line per node/edge the graph handler added or rejected
  llm_log.jsonl / spend.json       written by pulse.llm
  cassettes/                       recorded LLM responses for free replay

Change these models only additively (new optional fields) - four people code against them in parallel.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("PULSE_STATE", ROOT / ".pulse"))

# ---------------------------------------------------------------- graph

NodeStatus = Literal["ok", "anomalous", "missing_data", "stale", "unknown"]
FormulaStatus = Literal["validated", "incomplete", "unvalidated", "broken"]
Role = Literal["kpi", "on_path", "first_cause", "symptom", "unusual", "ruled_out", "watch"]
EdgeKind = Literal["formula", "breakdown", "cause", "component", "proposed"]
EdgeStatus = Literal["validated", "asserted", "learned", "ambiguous", "proposed", "rejected"]


class Node(BaseModel):
    id: str
    label: str
    kind: Literal["kpi", "metric", "component", "segment"] = "metric"
    domain: Optional[str] = None
    tier: Optional[str] = None              # north_star | outcome | driver | health
    unit: Optional[str] = None
    polarity: Optional[str] = None          # up_is_good | down_is_good | neutral
    description: str = ""
    definition: Optional[str] = None        # e.g. "orders / sessions" or "sum(facts.gmv)"
    value: Optional[float] = None           # current window
    expected: Optional[float] = None        # seasonal baseline for the same window
    deviation_pct: Optional[float] = None   # (value - expected) / |expected| * 100
    z: Optional[float] = None               # robust z-score of the deviation
    completeness: Optional[float] = None    # 0..1 share of expected rows that arrived; None = not measured
    status: NodeStatus = "ok"
    formula_status: Optional[FormulaStatus] = None   # None for raw (non-formula) metrics
    added_by: str = "seed"                  # seed | graph_handler
    depth: int = 0                          # hops from the snapshot centre
    # own vs inherited change (DoWhy-GCM idea): formula nodes -> actual vs formula(parents' actuals);
    # cause nodes -> seasonal residual left after regressing on parents' residuals. First cause = deepest node
    # on the path with a high own share; a node whose change is ~all inherited is a symptom.
    own_deviation_pct: Optional[float] = None   # part of deviation_pct NOT explained by parents
    inherited_pct: Optional[float] = None       # 0..100 share of the deviation explained by parents
    onset_ts: Optional[str] = None              # first hour the node left its band (change-point)
    roles: list[Role] = Field(default_factory=list)  # filled by the interpreter


class Edge(BaseModel):
    source: str                             # for formula: input -> metric; for cause: cause -> effect
    target: str
    kind: EdgeKind
    status: EdgeStatus = "asserted"
    op: Optional[str] = None                # formula operator ("*", "/", "+", "-") or breakdown dim
    sign: Optional[int] = None              # cause sign (+1 / -1)
    lag_h: Optional[int] = None
    strength: Optional[float] = None        # 0..1 evidence strength (correlation, fit)
    contribution_pct: Optional[float] = None  # share of target's change explained by source, if computed
    label: str = ""                         # mechanism / short human explanation
    added_by: str = "seed"


class GraphSnapshot(BaseModel):
    snapshot_id: str
    kpi: str
    center: str
    ts: str                                 # ISO timestamp of the data window end
    window_h: int = 3
    depth: int = 2
    nodes: list[Node]
    edges: list[Edge]
    notes: list[str] = Field(default_factory=list)   # e.g. "payments table 40% complete"


class GraphChange(BaseModel):
    ts: str
    action: Literal["add_node", "add_edge", "reject_edge", "mark_ambiguous", "update_formula"]
    node: Optional[Node] = None
    edge: Optional[Edge] = None
    reason: str = ""
    evidence: dict = Field(default_factory=dict)      # numbers from the validation check
    by: str = "graph_handler"


# ---------------------------------------------------------------- monitoring

Severity = Literal["ok", "watch", "alert", "data_issue"]


class SeriesPoint(BaseModel):
    ts: str
    value: Optional[float] = None
    expected: Optional[float] = None
    lo: Optional[float] = None
    hi: Optional[float] = None


class KpiStatus(BaseModel):
    name: str                               # metric id in the registry, e.g. "gmv"
    label: str                              # CEO-facing, e.g. "Revenue per hour"
    unit: Optional[str] = None
    polarity: Optional[str] = None
    value: Optional[float] = None
    expected: Optional[float] = None
    deviation_pct: Optional[float] = None
    z: Optional[float] = None
    direction: Literal["up", "down", "flat"] = "flat"
    severity: Severity = "ok"
    completeness: Optional[float] = None
    incident_id: Optional[str] = None
    note: str = ""                          # e.g. "expected: FR Black Week campaign" or "orders_db 55% complete"
    onset_ts: Optional[str] = None          # first hour of the current bad run
    series: list[SeriesPoint] = Field(default_factory=list)   # last ~48 h for the sparkline


class TickState(BaseModel):
    ts: str                                 # data clock
    tick: int
    kpis: list[KpiStatus]
    spend_usd: float = 0.0                  # cumulative LLM spend
    llm_mode: str = "mock"                  # mock | replay | live
    log: list[str] = Field(default_factory=list)   # orchestrator decisions this tick, human readable


class Incident(BaseModel):
    id: str
    kpi: str
    direction: Optional[str] = None         # up | down
    onset_ts: Optional[str] = None
    state: Literal["open", "persisting", "resolved"] = "open"
    severity: Severity = "alert"
    opened_at: str
    last_seen: str
    resolved_at: Optional[str] = None
    ticks_seen: int = 1
    headline: str = ""                      # one line, plain English
    snapshot_id: Optional[str] = None
    interpretation_id: Optional[str] = None
    history: list[str] = Field(default_factory=list)
    recalled: list[str] = Field(default_factory=list)   # similar past incidents from memory (hypotheses, not evidence)
    segment: Optional[dict] = None          # {"country": "NL"}: a segment incident (the KPI total is not alerting)
    closed_reason: Optional[str] = None     # closed without resolving: "no longer tracked" (the CEO untracked the metric)


# ---------------------------------------------------------------- interpretation

class TraceStep(BaseModel):
    i: int
    action: str                             # look | expand | detail | breakdown | propose_edge | rule_out | conclude
    node: Optional[str] = None
    args: dict = Field(default_factory=dict)
    finding: str = ""                       # one sentence: what the agent learned
    numbers: dict = Field(default_factory=dict)
    highlight_nodes: list[str] = Field(default_factory=list)
    highlight_edges: list[tuple[str, str]] = Field(default_factory=list)


class Finding(BaseModel):
    node: str
    why: str
    numbers: dict = Field(default_factory=dict)


class Interpretation(BaseModel):
    interpretation_id: str
    incident_id: Optional[str] = None
    kpi: str
    snapshot_id: str
    ts: str
    headline: str                           # <= 15 words, CEO level
    summary: str                            # 2-4 plain-English sentences: what, where, why, impact
    path: list[str] = Field(default_factory=list)     # KPI -> ... -> first cause (node ids, in order)
    first_cause: Optional[Finding] = None
    confidence: Literal["supported", "hypothesis", "data_issue", "unexplained"] = "hypothesis"
    symptoms: list[Finding] = Field(default_factory=list)
    unusual: list[Finding] = Field(default_factory=list)
    missing_data: list[Finding] = Field(default_factory=list)
    ruled_out: list[Finding] = Field(default_factory=list)
    new_connections: list[Edge] = Field(default_factory=list)  # proposed by the interpreter, validated by the graph handler
    uncertainty: str = ""
    watch_next: list[str] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    model: str = ""
    llm_mode: str = "mock"
    cost_usd: float = 0.0


class BriefItem(BaseModel):
    kpi: str
    label: str
    severity: Severity
    sentence: str                           # one short sentence; the numbers sit next to it in the UI
    incident_id: Optional[str] = None
    detail: str = ""                        # the why and what we do about it, shown when the row is expanded


class Brief(BaseModel):
    ts: str
    headline: str
    items: list[BriefItem]
    text: str = ""                          # full plain-English briefing
    follow_up: list[str] = Field(default_factory=list)   # what the orchestrator will re-check next tick
    model: str = ""
    llm_mode: str = "mock"


# ---------------------------------------------------------------- io helpers

def save(model: BaseModel, rel: str) -> Path:
    p = STATE_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)   # atomic: the dashboard never reads half a file
    return p


def save_list(models: list[BaseModel], rel: str) -> Path:
    p = STATE_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps([m.model_dump(mode="json") for m in models], indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def load(cls, rel: str):
    p = STATE_DIR / rel
    return cls.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else None


def load_list(cls, rel: str) -> list:
    p = STATE_DIR / rel
    return [cls.model_validate(x) for x in json.loads(p.read_text(encoding="utf-8"))] if p.exists() else []


def append_jsonl(model: BaseModel, rel: str) -> None:
    p = STATE_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(model.model_dump_json() + "\n")
