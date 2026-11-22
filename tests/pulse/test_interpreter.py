"""Graph Interpreter: mock-mode runs on a fixture snapshot (fast, no data) and on real data at an injected fault."""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("PULSE_STATE", tempfile.mkdtemp(prefix="pulse_interp_"))
os.environ["PULSE_LLM"] = "mock"

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from pulse import contracts as C  # noqa: E402
from pulse.contracts import GraphChange, GraphSnapshot, Interpretation  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "pulse" / "fixtures" / "snapshot_example.json"


class FakeWorld:
    now = pd.Timestamp("2026-11-17 15:00")

    class graph:  # noqa: N801
        @staticmethod
        def components_of(m):
            return {"payment_success_rate": ["payments"], "auth_failure_rate": ["payments"]}.get(m, [])


class FakeApi:
    """Serves graph_api answers from the fixture snapshot (payment failure, DE / android, 2026-11-17 15:00)."""

    def __init__(self, snap: GraphSnapshot):
        self.snap = snap
        self.nodes = {n.id: n for n in snap.nodes}

    def build_snapshot(self, w, kpi, center=None, depth=2, window_h=3):
        return self.snap.model_copy(deep=True, update={"center": center or kpi})

    def node_detail(self, w, node, window_h=3):
        n = self.nodes[node]
        return {**n.model_dump(), "dims": ["country", "platform", "category"], "inputs": [], "causes": [],
                "series_48h": []}

    def localize(self, w, node, window_h=3):
        return [{"type": "dim", "dim": "country", "top_segment": "DE", "top_share_of_change": 96.0,
                 "top_deviation_pct": -19.8, "concentrated": True},
                {"type": "dim", "dim": "platform", "top_segment": "android", "top_share_of_change": 94.0,
                 "top_deviation_pct": -18.7, "concentrated": True},
                {"type": "cell", "segment": {"country": "DE", "platform": "android"}, "deviation_pct": -31.0},
                {"type": "control", "segment": {"country": "DE", "platform": "ios"}, "deviation_pct": 0.4,
                 "value": 0.95, "expected": 0.946}]

    def changes_near(self, w, ts, segment=None, hours=2):
        return [{"table": "events", "ts": "2026-11-17T13:40:00", "name": "payments-sdk android 7.4.0",
                 "kind": "release", "segment_overlap": True, "component": "payments", "platform": "android",
                 "planned": True}]

    def breakdown(self, w, node, dim, window_h=3):
        return []

    def search_data(self, w, query):
        return []

    def neighbors(self, w, node):
        return {"dims": ["country", "platform", "category"]}

    def validate_edge(self, w, edge):
        return GraphChange(ts=str(self.snap.ts), action="add_edge", edge=edge.model_copy(update={"status": "validated"}))

    def record_change(self, change):
        pass


def _check_files(it: Interpretation):
    assert (C.STATE_DIR / "interpretations" / f"{it.interpretation_id}.json").exists()
    assert not (C.STATE_DIR / "interpretations" / f"{it.interpretation_id}.partial.json").exists()
    snap = C.load(GraphSnapshot, f"snapshots/{it.snapshot_id}.json")
    roles = {n.id: n.roles for n in snap.nodes}
    assert "kpi" in roles[it.kpi]
    assert "first_cause" in roles[it.first_cause.node]
    ids = {n.id for n in snap.nodes}
    assert all(p in ids for p in it.path), "every path node must exist in the saved snapshot"


def test_fixture_payment_failure():
    from pulse.interpreter import interpret, render_snapshot
    snap = GraphSnapshot.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    text = render_snapshot(snap)
    assert "DOWN" in text and "BAD for business" in text and "-causes(" in text
    steps = []
    it = interpret(FakeWorld(), snap.kpi, incident_id="INC-test", api=FakeApi(snap), snapshot=snap,
                   on_step=steps.append)
    assert it.path[0] == "payment_success_rate"
    assert it.first_cause.node == "event:payments-sdk android 7.4.0"
    assert "auth_failure_rate" in it.path and "payment_success_rate|country=DE|platform=android" in it.path
    assert it.confidence == "supported"
    assert len(it.headline.split()) <= 15 and "Germany" in it.summary
    assert [s.action for s in it.trace][0] == "look" and it.trace[-1].action == "conclude"
    assert {"localize", "recent_changes"} <= {s.action for s in it.trace}
    assert all(s.highlight_nodes for s in it.trace) and len(steps) == len(it.trace)
    assert any(f.node == "crash_rate" for f in it.ruled_out)
    _check_files(it)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "data" / "eye" / "injected.json").exists(),
                    reason="needs data/eye")
def test_real_data_payment_failure_and_data_issue():
    from pulse.interpreter import interpret
    from pulse.world import world
    w = world()
    truth = {s["id"]: s for s in json.loads((Path(__file__).resolve().parents[2] / "data" / "eye" /
                                              "injected.json").read_text())["scenarios"]}
    w.set_now("2026-11-17 15:00")
    it = interpret(w, "payment_success_rate", incident_id="INC-payment_success_rate-2026111714")
    assert it.path[0] == "payment_success_rate"
    assert it.first_cause.node == "event:" + truth["payment_failure"]["first_cause_event"]
    assert any("country=DE" in p and "platform=android" in p for p in it.path)
    assert it.confidence == "supported"
    assert len(it.trace) >= 4 and all(s.highlight_nodes for s in it.trace)
    _check_files(it)
    w.set_now("2026-11-21 22:00")   # orders_db ingestion lag
    it2 = interpret(w, "orders")
    assert it2.confidence == "data_issue" and it2.missing_data


@pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "data" / "eye" / "injected.json").exists(),
                    reason="needs data/eye")
def test_real_data_supply_delay_and_seller_feed():
    from pulse.interpreter import interpret
    from pulse.world import world
    w = world()
    truth = {s["id"]: s for s in json.loads((Path(__file__).resolve().parents[2] / "data" / "eye" /
                                              "injected.json").read_text())["scenarios"]}
    w.set_now("2026-11-21 18:00")   # stockout re-check, 29 h after onset: the change search starts at the incident onset
    it = interpret(w, "in_stock_rate", onset="2026-11-20 13:00")
    assert it.first_cause.node == "event:" + truth["stockout"]["first_cause_event"]
    assert any("category=electronics" in p for p in it.path)
    w.set_now("2026-11-14 10:00")   # seller catalog feed fails: in stock <- offer availability <- seller feed errors
    it2 = interpret(w, "in_stock_rate")
    assert it2.first_cause.node == "seller_feed_errors" and "offer_availability" in it2.path
