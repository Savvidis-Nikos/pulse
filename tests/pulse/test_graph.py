"""Graph API + Graph Handler (mock mode only, never calls the API)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("PULSE_LLM", "mock")

from pulse import contracts as C  # noqa: E402
from pulse import graph_api as ga  # noqa: E402
from pulse.contracts import Edge  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[2] / "pulse" / "inbox_examples"


@pytest.fixture(scope="module")
def w(tmp_path_factory):
    assert os.environ.get("PULSE_LLM", "mock") == "mock"
    from pulse.world import world
    old = C.STATE_DIR
    C.STATE_DIR = tmp_path_factory.mktemp("pulse_state")   # graph_changes.jsonl goes here, not into .pulse/
    yield world()
    C.STATE_DIR = old


@pytest.mark.parametrize("kpi", ["gmv", "orders"])
def test_snapshot_annotated(w, kpi):
    snap = ga.build_snapshot(w, kpi)
    ids = [n.id for n in snap.nodes]
    assert ids[0] == kpi and len(ids) == len(set(ids)) and 3 <= len(ids) <= ga.MAX_NODES
    kpi_node = snap.nodes[0]
    assert kpi_node.kind == "kpi" and kpi_node.value is not None and kpi_node.expected is not None
    assert kpi_node.formula_status in ("validated", "incomplete", "unvalidated", "broken")
    assert all(n.status in ("ok", "anomalous", "missing_data", "stale", "unknown") for n in snap.nodes)
    kinds = {e.kind for e in snap.edges}
    assert "formula" in kinds
    for e in snap.edges:
        assert e.source in ids and e.target in ids
    contrib = [e for e in snap.edges if e.kind == "formula" and e.target == kpi]
    assert contrib and all(e.contribution_pct is not None for e in contrib)
    assert abs(sum(e.contribution_pct for e in contrib) - 100) < 1.0   # LMDI shares add up
    assert kpi_node.inherited_pct is not None


def test_breakdown_and_detail(w):
    rows = ga.breakdown(w, "conversion_rate", "country")
    assert rows and {"segment", "value", "expected", "share_of_change", "rate_effect", "mix_effect"} <= set(rows[0])
    assert abs(sum(r["share_of_change"] for r in rows) - 100) < 1.0
    d = ga.node_detail(w, "orders")
    assert d["series_48h"] and "country" in d["dims"] and d["inputs"]


def test_validate_edge(w):
    ok = ga.validate_edge(w, Edge(source="orders", target="gmv", kind="formula", label="gmv = orders * aov"))
    assert ok.action == "add_edge" and ok.edge.status == "validated"
    assert ok.evidence["max_rel_residual"] < 1e-6
    bad_formula = ga.validate_edge(w, Edge(source="sessions", target="gmv", kind="formula", label="gmv = sessions * 2"))
    assert bad_formula.action == "reject_edge"
    false_cause = ga.validate_edge(w, Edge(source="crash_rate", target="take_rate", kind="cause", sign=1))
    assert false_cause.action == "reject_edge" and "corr" in false_cause.evidence
    true_cause = ga.validate_edge(w, Edge(source="auth_failure_rate", target="payment_success_rate", kind="cause",
                                          sign=-1))
    assert true_cause.action == "add_edge" and true_cause.edge.status == "validated"


def test_search_finds_unmodelled_data(w):
    hits = ga.search_data(w, "checkout service errors")
    assert any(h["kind"] == "column" and h["table"] == "services" for h in hits)


def test_ingest_prose_adds_validated_node_and_survives_restart(w):
    from pulse.graph_handler import ingest_metric
    changes = ingest_metric(w, (EXAMPLES / "checkout_errors_prose.txt").read_text(encoding="utf-8"))
    nodes = [c.node.id for c in changes if c.action == "add_node"]
    assert "checkout_error_rate" in nodes
    formula = [c for c in changes if c.edge and c.edge.kind == "formula" and c.edge.target == "checkout_error_rate"]
    assert formula and all(c.edge.status == "validated" for c in formula)
    assert len(ga.load_changes()) == len(changes)
    snap = ga.build_snapshot(w, "checkout_error_rate")
    assert snap.nodes[0].added_by == "graph_handler" and snap.nodes[0].value is not None
    # restart: drop the in-memory additions, then the change log re-applies them
    for n in nodes:
        w.reg.remove(n)
        w.graph.G.remove_node(n)
    w.engine.refresh()
    w.__dict__.pop("_pulse_applied", None)
    assert ga.ensure_applied(w) == len(changes)
    assert "checkout_error_rate" in w.reg and "checkout_error_rate" in w.graph.G
    assert ga.build_snapshot(w, "checkout_error_rate").nodes[0].formula_status == "validated"


def test_ingest_ambiguous_example(w):
    from pulse.graph_handler import ingest_metric
    changes = ingest_metric(w, (EXAMPLES / "seller_gmv.yaml").read_text(encoding="utf-8"))
    assert any(c.action == "add_node" for c in changes)
    assert any(c.action == "mark_ambiguous" for c in changes)
