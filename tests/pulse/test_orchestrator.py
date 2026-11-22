"""A short mock-mode replay through the real orchestrator: files written, incident opened once, briefs gated."""
import os

import pytest

os.environ["PULSE_LLM"] = "mock"

from pulse import contracts as C  # noqa: E402
from pulse import llm  # noqa: E402
from pulse.contracts import Brief, Incident, TickState  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    return tmp_path


def test_replay_writes_state_incidents_brief(state_dir):
    from pulse.orchestrator import tick
    states = [tick(ts) for ts in ("2026-11-17 12:00", "2026-11-17 13:00", "2026-11-17 14:00",
                                  "2026-11-17 15:00", "2026-11-17 16:00")]
    for f in ("state.json", "incidents.json", "brief.json", "orchestrator.json"):
        assert (state_dir / f).exists(), f
    st = C.load(TickState, "state.json")
    assert st.ts == "2026-11-17 16:00" and st.tick == 5 and st.llm_mode == "mock" and len(st.kpis) >= 6
    # all-ok tick: template brief, zero LLM
    assert any("template brief (no LLM call)" in l for l in states[0].log)
    # the fault opens exactly one incident, at 15:00, and is not re-reported at 16:00
    incs = C.load_list(Incident, "incidents.json")
    assert [i.id for i in incs] == ["INC-payment_success_rate-2026111714"]
    assert incs[0].state == "persisting" and incs[0].ticks_seen == 2
    assert any("opened INC-payment_success_rate-2026111714" in l for l in states[3].log)
    assert any("persisting" in l and "not re-reported" in l for l in states[4].log)
    assert not any("orchestrator agent" in l for l in states[4].log)        # nothing changed -> no LLM brief
    card = next(k for k in st.kpis if k.name == "payment_success_rate")
    assert card.severity == "alert" and card.incident_id == incs[0].id
    br = C.load(Brief, "brief.json")
    assert br.ts == "2026-11-17 16:00" and br.headline and br.items
    assert any(it.kpi == "payment_success_rate" and it.severity == "alert" for it in br.items)


def test_same_hour_does_not_double_count(state_dir):
    from pulse.orchestrator import tick
    tick("2026-11-17 15:00")
    st = tick("2026-11-17 15:00")
    assert st.tick == 1
    assert C.load_list(Incident, "incidents.json")[0].ticks_seen == 1


def test_inbox_metric_is_ingested(state_dir, monkeypatch):
    import json
    import shutil

    from pulse.contracts import ROOT
    from pulse.orchestrator import tick
    from pulse.world import world
    world(reload=True)   # other tests may already have ingested this metric into the shared in-memory world
    monkeypatch.delenv("PULSE_INBOX", raising=False)
    inbox = state_dir / "inbox"
    inbox.mkdir()
    shutil.copy(ROOT / "pulse" / "inbox_examples" / "checkout_errors_prose.txt", inbox)
    st = tick("2026-11-17 13:00")
    changes = [json.loads(l) for l in (state_dir / "graph_changes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(c["action"] == "add_node" for c in changes)
    assert (inbox / "processed" / "checkout_errors_prose.txt").exists()
    assert not (inbox / "checkout_errors_prose.txt").exists()
    assert any(l.startswith("Graph Handler on inbox/checkout_errors_prose.txt: added") for l in st.log)
    assert any("orchestrator agent" in l for l in st.log)          # an ingest is a material change
