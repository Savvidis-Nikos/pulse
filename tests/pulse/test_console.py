"""Event console (pulse/web/console.py): what it offers and how it validates actions, without running the simulator."""
from marketplace.live import NAMES
from pulse.web.console import CATALOG, Console


def test_catalog_scenarios_exist_in_the_simulator():
    assert {sid for sid, _, _ in CATALOG} <= set(NAMES)
    assert {kind for _, kind, _ in CATALOG} == {"fault", "innocent", "data"}


def test_source_switches_the_state_dir(tmp_path):
    c = Console(tmp_path)
    st = c.status()
    assert st["enabled"] and st["source"] == "recorded" and st["session"] is None and not st["busy"]
    assert c.state_dir(tmp_path / "recorded") == tmp_path / "recorded"
    assert c.act({"action": "source", "source": "live"})[1] == 200
    assert c.state_dir(tmp_path / "recorded") == tmp_path / ".pulse_live"


def test_actions_are_validated(tmp_path):
    c = Console(tmp_path)
    assert c.act({"action": "inject", "id": "payment_failure"})[1] == 400      # no market yet
    assert c.act({"action": "source", "source": "elsewhere"})[1] == 400
    c._save({"seed": 7, "now": "2026-11-15 10:00", "injected": []})
    assert c.act({"action": "inject", "id": "not_a_scenario"})[1] == 400
    assert c.act({"action": "nonsense"})[1] == 400
    body, code = c.act({"action": "inject", "id": "payment_failure"})
    assert code == 200 and body["session"]["injected"][0]["id"] == "payment_failure"
    assert body["session"]["last_hour"] == "2026-11-15 09:00"
    assert body["log"][-1]["kind"] == "inject"


def test_agents_mode_reaches_pulse_and_live_needs_no_running_job(tmp_path, monkeypatch):
    c = Console(tmp_path)
    assert c.act({"action": "agents", "mode": "elsewhere"})[1] == 400
    body, code = c.act({"action": "agents", "mode": "replay"})
    assert code == 200 and body["agents"] == "replay"
    env = c._env()
    assert env["PULSE_LLM"] == "replay" and env["PULSE_STREAM_LOG"] == "1"
    c.job = "Running 3 hours"
    assert c.act({"action": "agents", "mode": "mock"})[1] == 409      # never switch agents mid-run


def test_streamed_lines_become_hours_steps_and_agent_progress(tmp_path, monkeypatch):
    import io
    import pulse.web.console as con
    out = ("[2026-11-15 12:00] checking\n"
           "      - orders -12.3% vs expected (z -4.1) -> opened INC-orders-2026111512\n"
           "      ~ INC-orders-2026111512: investigator agent started\n"
           "      ~ INC-orders-2026111512 step 1: Conversion rate carries 94% of the change\n"
           "[2026-11-15 12:00] tick   4 | orders ALERT -12% | Orders down 12.3%\n")

    class FakeProc:
        def __init__(self, *a, **k):
            self.stdout, self.returncode = io.StringIO(out), 0

        def wait(self):
            return 0

    monkeypatch.setattr(con.subprocess, "Popen", FakeProc)
    c = Console(tmp_path)
    c._replay("2026-11-15 12:00", "2026-11-15 12:00")
    assert [e["kind"] for e in c._log] == ["checking", "step", "note", "note", "check"]
    assert {e["t"] for e in c._log} == {"2026-11-15 12:00"}


def test_injected_fault_is_scored_against_what_pulse_found(tmp_path):
    import json
    c = Console(tmp_path)
    c._save({"seed": 7, "now": "2026-11-15 13:00", "injected": [{"name": "payment_failure", "at": "2026-11-15 10:00"}]})
    (c.state / "interpretations").mkdir(parents=True)
    inc = {"id": "INC-payment_success_rate-2026111511", "kpi": "payment_success_rate", "opened_at": "2026-11-15 12:00",
           "last_seen": "2026-11-15 12:00", "onset_ts": "2026-11-15 11:00"}
    (c.state / "incidents.json").write_text(json.dumps([inc]), encoding="utf-8")
    (c.state / "interpretations" / f"{inc['id']}.json").write_text(json.dumps({
        "headline": "Payment success rate down 7.5% for Android in DE, likely caused by payments-sdk android 7.4.0",
        "confidence": "supported"}), encoding="utf-8")
    c._add("step", f"payment_success_rate -7.5% vs expected (z -5.0) -> opened {inc['id']}", t="2026-11-15 12:00")
    c._score()
    r = c.status()["results"][0]
    assert r["detected"] and r["cause_named"] and r["segment_named"] and r["alert_at"] == "2026-11-15 12:00"
    assert c.status()["session"]["injected"][0]["segment"] == {"country": "DE", "platform": "android"}
