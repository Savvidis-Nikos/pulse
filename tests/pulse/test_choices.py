"""The CEO's choices: pin a graph metric (full monitoring), untrack a headline metric (background only), restore.
One store for every run, applied at once by `recheck` without moving the data clock, ignored by the scorecard."""
import json
import os
import subprocess
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

os.environ["PULSE_LLM"] = "mock"

from pulse import choices  # noqa: E402
from pulse import contracts as C  # noqa: E402
from pulse.contracts import ROOT, Brief, Incident, KpiStatus, TickState  # noqa: E402

PINNED = "auth_failure_rate"      # moves with the payment fault of 2026-11-17 (payments-sdk android 7.4.0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_CHOICES", str(tmp_path / "choices.json"))
    return tmp_path / "choices.json"


@pytest.fixture
def state_dir(tmp_path, monkeypatch, store):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


def test_store_actions_and_bad_input(store):
    assert choices.load() == {"pinned": [], "untracked": []}
    choices.apply("pin", PINNED)
    choices.apply("untrack", "ad_revenue")
    assert json.loads(store.read_text()) == {"pinned": [PINNED], "untracked": ["ad_revenue"]}
    choices.apply("untrack", PINNED)                      # a pinned metric moves to the background, restorable
    assert choices.load() == {"pinned": [], "untracked": ["ad_revenue", PINNED]}
    choices.apply("restore", "ad_revenue")               # a kpis.yaml KPI comes back without being "pinned"
    choices.apply("pin", PINNED)
    assert choices.load() == {"pinned": [PINNED], "untracked": []}
    for action, metric in (("delete", "gmv"), ("pin", "event:payments-sdk android 7.4.0"),
                           ("pin", "payment_success_rate|country=DE"), ("pin", "orders_db:data_issue")):
        with pytest.raises(ValueError):
            choices.apply(action, metric)
    store.write_text('{"pinned": ["ok_metric", "event:x", 3], "untracked": "nope"}')
    assert choices.load() == {"pinned": ["ok_metric"], "untracked": []}


def test_choices_survive_reset(tmp_path, store):
    choices.apply("pin", PINNED)
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text("{}")
    env = {**os.environ, "PULSE_STATE": str(state), "PULSE_CHOICES": str(store)}
    subprocess.run([sys.executable, "-m", "pulse", "reset"], cwd=ROOT, env=env, check=True, capture_output=True)
    assert not (state / "state.json").exists()
    assert choices.load()["pinned"] == [PINNED]


def test_headline_kpis_follow_choices(state_dir):
    from pulse.incidents import IncidentBook
    from pulse.monitor import evaluate_ex, headline_kpis, load_kpis
    from pulse.orchestrator import _situation
    from pulse.world import world
    w = world()
    base = [k.name for k in load_kpis()]
    assert [k.name for k in headline_kpis(w)] == base
    choices.apply("pin", PINNED)
    choices.apply("pin", "no_such_metric")               # not in this world's registry: skipped, never an error
    choices.apply("untrack", "ad_revenue")
    kpis = headline_kpis(w)
    assert [k.name for k in kpis] == [m for m in base if m != "ad_revenue"] + [PINNED]
    pin = kpis[-1]
    assert pin.pinned and pin.label == "Auth failure rate" and (pin.window_h, pin.alert_z, pin.series_h) == (3, 4.0, 48)
    text = _situation("2026-11-17 15:00", evaluate_ex(w, kpis, "2026-11-17 15:00"), IncidentBook(), {}, {}, None)
    assert f"Pinned by the CEO (monitored like the others): {PINNED}." in text
    assert "Untracked by the CEO" in text and "ad_revenue" in text.split("Untracked by the CEO")[1]


def test_pin_recheck_untrack_restore(state_dir):
    from pulse.monitor import load_kpis
    from pulse.orchestrator import recheck, tick
    tick("2026-11-17 15:00")                              # the payment fault opens its incident
    choices.apply("pin", PINNED)
    st = recheck()                                        # same hour: the row appears, the clock does not move
    assert (st.ts, st.tick) == ("2026-11-17 15:00", 1)
    row = next(k for k in st.kpis if k.name == PINNED)
    assert row.severity == "alert" and row.incident_id == f"INC-{PINNED}-2026111713" and len(row.series) == 48
    assert any(l == f"{PINNED} pinned: now monitored like the headline metrics" for l in st.log)
    br = C.load(Brief, "brief.json")
    assert br.ts == st.ts and any(i.kpi == PINNED and i.severity == "alert" for i in br.items)
    assert recheck().log == st.log                        # nothing new to apply: nothing changes

    st = tick("2026-11-17 16:00")                         # full monitoring: persisting (no duplicate), investigated
    incs = {i.id: i for i in C.load_list(Incident, "incidents.json")}
    inc = incs[f"INC-{PINNED}-2026111713"]
    assert inc.state == "persisting" and inc.ticks_seen == 2 and inc.interpretation_id
    assert sum(i.kpi == PINNED for i in incs.values()) == 1
    assert any(f"INC-{PINNED}-2026111713: not investigated yet -> asking the graph interpreter" == l for l in st.log)

    memory = state_dir / "memory.jsonl"
    before = memory.read_text(encoding="utf-8") if memory.exists() else ""
    choices.apply("untrack", PINNED)
    choices.apply("untrack", "ad_revenue")
    st = recheck()
    assert PINNED not in {k.name for k in st.kpis} and "ad_revenue" not in {k.name for k in st.kpis}
    inc = next(i for i in C.load_list(Incident, "incidents.json") if i.id == f"INC-{PINNED}-2026111713")
    assert inc.state == "resolved" and inc.closed_reason == "no longer tracked"
    assert (memory.read_text(encoding="utf-8") if memory.exists() else "") == before   # not a resolved case
    assert any(l.startswith(f"{PINNED} untracked: watched in the background only, INC-") for l in st.log)

    st = tick("2026-11-17 17:00", kpis=load_kpis())       # the scorecard's way: kpis.yaml only, choices ignored
    assert [k.name for k in st.kpis] == [k.name for k in load_kpis()]
    choices.apply("restore", "ad_revenue")
    st = tick("2026-11-17 18:00")                         # the loop itself follows the choices
    assert "ad_revenue" in {k.name for k in st.kpis} and PINNED not in {k.name for k in st.kpis}
    assert not [i for i in C.load_list(Incident, "incidents.json") if i.kpi == PINNED and i.state != "resolved"]


def _call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = Request(f"http://127.0.0.1:{port}{path}", method=method,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def server(tmp_path, store, monkeypatch):
    """The real UI server on a small state: one tick's rows (all kpis.yaml KPIs) and one graph snapshot."""
    from pulse.web import server as S
    state = tmp_path / "state"
    (state / "snapshots").mkdir(parents=True)
    names = list(choices.kpi_labels().items())
    C.STATE_DIR, old = state, C.STATE_DIR
    try:
        C.save(TickState(ts="2026-11-17 15:00", tick=1, kpis=[KpiStatus(name=n, label=l) for n, l in names]), "state.json")
        C.save(Brief(ts="2026-11-17 15:00", headline="All clear", items=[]), "brief.json")
    finally:
        C.STATE_DIR = old
    snap = {"snapshot_id": "s1", "kpi": "gmv", "center": "gmv", "ts": "2026-11-17 15:00", "nodes": [
        {"id": "gmv", "label": "GMV", "kind": "kpi"}, {"id": PINNED, "label": "Auth failure rate", "kind": "metric"},
        {"id": "event:payments-sdk android 7.4.0", "label": "release", "kind": "component"}], "edges": []}
    (state / "snapshots" / "s1.json").write_text(json.dumps(snap))
    servers = []

    def start(allow: bool) -> int:
        handler = type("H", (S.Handler,), {"state_dir": state, "allow_refresh": allow, "console": None})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(S.STATIC)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]
    yield start, state
    for s in servers:
        s.shutdown()


def test_server_choices_api(server):
    start, state = server
    demo = start(False)
    assert _call(demo, "GET", "/api/overview")[1]["choices"] is None
    assert _call(demo, "POST", "/api/choices", {"action": "untrack", "metric": "sessions"})[0] == 403
    port = start(True)
    assert _call(port, "GET", "/api/overview")[1]["choices"] == {"pinned": [], "untracked": [], "labels": {}}
    for body, code in (({"action": "pin", "metric": "event:payments-sdk android 7.4.0"}, 400),
                       ({"action": "pin", "metric": "refund_rate"}, 400),        # not drawn on any graph here
                       ({"action": "restore", "metric": "sessions"}, 400),       # not untracked
                       ({"action": "hide", "metric": "sessions"}, 400)):
        assert _call(port, "POST", "/api/choices", body)[0] == code, body
    code, out = _call(port, "POST", "/api/choices", {"action": "untrack", "metric": "sessions"})
    assert code == 200 and out["choices"]["untracked"] == ["sessions"]
    ov = _call(port, "GET", "/api/overview")[1]
    assert "sessions" not in [k["name"] for k in ov["tick"]["kpis"]] and ov["tick"]["ts"] == "2026-11-17 15:00"
    assert ov["choices"]["labels"] == {"sessions": choices.kpi_labels()["sessions"]}
    assert ov["tick"]["log"][-1] == "sessions untracked: watched in the background only"
