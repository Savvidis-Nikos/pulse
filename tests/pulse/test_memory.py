from pulse import contracts as C
from pulse import memory
from pulse.contracts import Finding, Incident, Interpretation


def _case(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    inc = Incident(id="INC-a", kpi="payment_success_rate", direction="down", opened_at="2026-11-10 15:00",
                   last_seen="2026-11-10 20:00", onset_ts="2026-11-10 14:00", ticks_seen=6)
    it = Interpretation(interpretation_id="INC-a", kpi="payment_success_rate", snapshot_id="s", ts="t",
                        headline="Payment success down on Android in DE", summary="", confidence="supported",
                        path=["payment_success_rate", "payment_success_rate|country=DE|platform=android",
                              "auth_failure_rate"], first_cause=Finding(node="auth_failure_rate", why=""))
    return memory.remember(inc, it, "2026-11-10 21:00")


def test_remember_is_idempotent_and_keeps_segment(monkeypatch, tmp_path):
    c = _case(monkeypatch, tmp_path)
    assert c["segment"] == {"country": "DE", "platform": "android"}
    memory.remember(Incident(id="INC-a", kpi="payment_success_rate", opened_at="x", last_seen="x"), None, "x")
    assert len(memory.cases()) == 1


def test_recall_similar_but_never_from_the_future(monkeypatch, tmp_path):
    _case(monkeypatch, tmp_path)
    hit = memory.recall("payment_success_rate", "down", ["payment_success_rate|country=DE|platform=android"],
                        before="2026-11-17 15:00", exclude="INC-b")
    assert [h["id"] for h in hit] == ["INC-a"]
    assert memory.recall("payment_success_rate", "down", before="2026-11-10 18:00") == []   # resolved later
    assert memory.recall("sessions", "up", before="2026-11-17 15:00") == []                 # not similar
