"""Shared graph helpers for the Pulse UI: deterministic solar layout, token encoding, replay frames, fixtures."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pulse import contracts as C  # noqa: E402
from pulse.dashboard import encoding as EN  # noqa: E402
from pulse.dashboard import make_fixtures as MF  # noqa: E402
from pulse.dashboard.layout import ASPECT, node_depths, orbit_radii, solar_layout  # noqa: E402

FIX = ROOT / "pulse" / "fixtures" / "state_demo"


def _snap() -> C.GraphSnapshot:
    return MF.snapshot_orders()


# ------------------------------------------------------------------ layout

def test_layout_is_deterministic_and_complete():
    a, b = solar_layout(_snap()), solar_layout(_snap())
    assert a == b
    assert set(a) == {n.id for n in _snap().nodes}
    assert a["orders"] == (0.0, 0.0)


def test_layout_independent_of_node_and_edge_order():
    s = _snap()
    shuffled = s.model_copy(update={"nodes": list(reversed(s.nodes)), "edges": list(reversed(s.edges))})
    assert solar_layout(s) == solar_layout(shuffled)


def test_positions_do_not_move_when_depth_filter_changes():
    """A depth filter only hides orbits: the visible nodes keep exactly the full-graph coordinates."""
    s = _snap()
    full = solar_layout(s)
    depths = node_depths(s)
    for max_d in (1, 2, 3):
        visible = {nid for nid, d in depths.items() if d <= max_d}
        again = solar_layout(s)
        assert {k: again[k] for k in visible} == {k: full[k] for k in visible}


def test_nodes_sit_on_their_orbit():
    s = _snap()
    pos, radii, depths = solar_layout(s), orbit_radii(s), node_depths(s)
    for nid, (x, y) in pos.items():
        r = math.hypot(x / ASPECT, y)
        assert abs(r - radii[depths[nid]]) < 0.05, nid


def test_children_stay_near_parent_angle():
    pos = solar_layout(_snap())
    ang = lambda n: math.atan2(pos[n][1], pos[n][0] / ASPECT)
    diff = abs(ang(MF.PSR_SEG) - ang("payment_success_rate")) % (2 * math.pi)
    assert min(diff, 2 * math.pi - diff) < math.radians(30)


def test_layout_handles_orphans_and_empty():
    s = C.GraphSnapshot(snapshot_id="x", kpi="a", center="a", ts="t",
                        nodes=[C.Node(id="a", label="A"), C.Node(id="b", label="B", depth=2)], edges=[])
    pos = solar_layout(s)
    assert set(pos) == {"a", "b"} and pos == solar_layout(s)
    assert solar_layout(C.GraphSnapshot(snapshot_id="e", kpi="a", center="a", ts="t", nodes=[], edges=[])) == {}


# ------------------------------------------------------------------ encoding

def _node(nid):
    return next(n for n in _snap().nodes if n.id == nid)


def test_node_tokens():
    s = _snap()
    st = EN.style_snapshot(s)["nodes"]
    assert st["orders"]["fill"] == "kpi" and st["orders"]["shape"] == "sun"
    assert st[MF.PSR_SEG]["fill"] == "harm" and "first_cause" in st[MF.PSR_SEG]["rings"]
    assert st["sessions|FR"]["fill"] == "gain"                  # traffic up = good
    assert st["auth_failure_rate"]["fill"] == "harm"            # down_is_good metric went up = bad
    assert st["marketing_cost"]["fill"] == "shift"              # neutral polarity
    assert st["in_stock_rate"]["fill"] == "missing" and st["in_stock_rate"]["sub"] == "no data"
    assert st["search_ctr"]["fill"] == "stale" and st["search_ctr"]["faded"]
    assert "formula_incomplete" in st["contribution_margin"]["rings"]
    assert st["auth_timeout_rate"]["shape"] == "diamond"        # added by the Graph Handler
    assert st["checkout_rate"]["faded"]                         # ruled out
    assert st[MF.RELEASE]["badge"] == "unusual"
    assert "symptom" in st["gmv"]["rings"] and "path" in st["payment_success_rate"]["rings"]


def test_own_change_intensity_and_tooltip():
    hot, pale = EN.node_style(_node(MF.PSR_SEG)), EN.node_style(_node("gmv"))
    assert hot["intensity"] > 0.9 and pale["intensity"] <= 0.3
    tt = hot["tooltip"]
    assert "own / inherited" in tt and "left its band at" in tt and tt["value"] == "58.1%"


def test_edge_tokens():
    st = EN.style_snapshot(_snap())["edges"]
    assert st["sessions->orders"]["line"] == "formula"
    assert st[f"{MF.PSR_SEG}->payment_success_rate"]["line"] == "formula"   # breakdown drawn as formula
    assert st["marketing_cost->sessions"]["line"] == "learned"
    assert st["search_ctr->product_view_rate"]["line"] == "ambiguous"
    assert st["service_error_rate->payment_success_rate"]["line"] == "cause"
    assert st[f"{MF.RELEASE}->{MF.PSR_SEG}"] == {"line": "proposed", "arrow": True, "kind": "proposed", "label": "new"}
    assert EN.edge_style({"source": "a", "target": "b", "kind": "cause", "status": "rejected"})["line"] == "rejected"


def test_legend_covers_every_token():
    tokens = {e["token"] for e in EN.LEGEND}
    st = EN.style_snapshot(_snap())
    used = set()
    for s in st["nodes"].values():
        used |= {s["fill"], *s["rings"]} | ({s["badge"]} if s["badge"] else set())
        used |= {"diamond"} if s["shape"] == "diamond" else set()
        used |= {"faded"} if s["faded"] else set()
    used |= {s["line"] for s in st["edges"].values()} | {"rejected"}
    assert used <= tokens, used - tokens
    assert all(set(e) == {"group", "token", "label"} and e["group"] in ("Nodes", "Links") for e in EN.LEGEND)


def test_encoding_accepts_plain_dicts():
    d = json.loads(_snap().model_dump_json())
    assert EN.style_snapshot(d) == EN.style_snapshot(_snap())


# ------------------------------------------------------------------ replay frames

def test_trace_frames_final():
    interp, snap = MF.interp_orders(), _snap()
    fr = EN.trace_frames(interp, snap)
    assert len(fr) == len(interp.trace) + 1
    assert fr[0]["i"] == 0 and fr[0]["visited_nodes"] == [] and not fr[0]["reveal_roles"]
    for a, b in zip(fr, fr[1:]):        # cumulative
        assert set(a["visited_nodes"]) <= set(b["visited_nodes"])
        assert all(e in b["visited_edges"] for e in a["visited_edges"])
    assert all(not f["reveal_roles"] and not f["path_edges"] for f in fr[:-1])
    last = fr[-1]
    assert last["reveal_roles"]
    assert last["path_edges"] == [["payment_success_rate", "orders"], [MF.PSR_SEG, "payment_success_rate"]]
    assert last["caption"].startswith("Step 8 · conclude:")


def test_trace_frames_partial_from_api_dict():
    d = json.loads(MF.interp_orders(MF.trace_orders()[:4]).model_dump_json())
    d["partial"] = True
    fr = EN.trace_frames(d)
    assert len(fr) == 5 and not fr[-1]["reveal_roles"] and fr[-1]["path_edges"] == []


# ------------------------------------------------------------------ fixtures

def test_fixtures_validate_against_contracts(tmp_path):
    MF.write_all(tmp_path)
    st = C.TickState.model_validate_json((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(st.kpis) == 8 and all(len(k.series) == 48 for k in st.kpis)
    assert sorted(k.severity for k in st.kpis).count("ok") == 5
    assert {k.severity for k in st.kpis} == {"ok", "alert", "watch", "data_issue"}
    incs = [C.Incident.model_validate(x) for x in json.loads((tmp_path / "incidents.json").read_text(encoding="utf-8"))]
    assert {i.state for i in incs} == {"open", "persisting", "resolved"}
    for i in incs:
        snap = C.GraphSnapshot.model_validate_json((tmp_path / "snapshots" / f"{i.snapshot_id}.json").read_text(encoding="utf-8"))
        assert snap.snapshot_id.startswith(f"{snap.kpi}-{snap.center}-")
        if i.interpretation_id:
            it = C.Interpretation.model_validate_json(
                (tmp_path / "interpretations" / f"{i.interpretation_id}.json").read_text(encoding="utf-8"))
            assert it.snapshot_id == i.snapshot_id and it.interpretation_id == i.id
            ids = {n.id for n in snap.nodes}
            assert set(it.path) <= ids and all(set(s.highlight_nodes) <= ids for s in it.trace)
    C.Interpretation.model_validate_json(
        (tmp_path / "interpretations" / f"{MF.ORDERS_ID}.partial.json").read_text(encoding="utf-8"))
    C.Brief.model_validate_json((tmp_path / "brief.json").read_text(encoding="utf-8"))
    for line in (tmp_path / "graph_changes.jsonl").read_text(encoding="utf-8").splitlines():
        C.GraphChange.model_validate_json(line)
    assert isinstance(json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))["usd"], float)


def test_checked_in_fixture_dir_is_valid():
    assert (FIX / "state.json").exists()
    C.TickState.model_validate_json((FIX / "state.json").read_text(encoding="utf-8"))
    assert len(list((FIX / "snapshots").glob("*.json"))) == 3
