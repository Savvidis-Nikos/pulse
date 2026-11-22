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
