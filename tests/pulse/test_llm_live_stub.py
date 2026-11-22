"""The live LLM path without a network: a fake Anthropic client returns real SDK message objects, so the request
shape, the tool loop (thinking blocks echoed back), cost/cassette bookkeeping, replay and the mock fallback run."""
import json

import anthropic
import pytest
from anthropic.types.beta import BetaMessage

from pulse import contracts as C
from pulse import llm

SCHEMA = llm.obj({"answer": {"type": "integer"}})


def _msg(content, stop):
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": content,
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 500}})


class FakeClient:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.beta = self
        self.messages = self

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        return self.replies.pop(0)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setenv("PULSE_LLM", "live")
    monkeypatch.setenv("PULSE_TOTAL_CAP_USD", "12")

    def install(replies):
        fake = FakeClient(replies)
        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
        return fake
    return install


TOOLS = [llm.Tool("double", "Double a number.", llm.obj({"x": {"type": "integer"}}), lambda x: {"y": 2 * x})]
TOOL_TURN = [{"type": "thinking", "thinking": "", "signature": "sig-abc"},
             {"type": "tool_use", "id": "toolu_1", "name": "double", "input": {"x": 2}}]


def mock(calls):
    return {"final": {"answer": -1}}


def test_live_tool_loop_records_cost_and_cassette_then_replays(live, monkeypatch, tmp_path):
    fake = live([_msg(TOOL_TURN, "tool_use"), _msg([{"type": "text", "text": '{"answer": 4}'}], "end_turn")])
    res = llm.run_agent("interpreter", "sys", "what is 2 doubled?", TOOLS, SCHEMA, mock=mock)
    assert res.output == {"answer": 4} and res.mode == "live" and res.turns == 2
    assert res.calls[0]["tool"] == "double" and json.loads(res.calls[0]["result"]) == {"y": 4}
    first, second = fake.requests
    assert first["model"] == "claude-sonnet-5-5" and first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"] and first["cache_control"] == {"type": "ephemeral"}
    assert first["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert "tool_choice" not in first and "thinking" not in first
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant" and assistant["content"][0] == TOOL_TURN[0]   # thinking echoed unchanged
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    assert llm.total_spend() > 0 and len((tmp_path / "llm_log.jsonl").read_text().splitlines()) == 2
    assert len(list((tmp_path / "cassettes").glob("*.json"))) == 1

    monkeypatch.setenv("PULSE_LLM", "replay")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: pytest.fail("replay must not build a client"))
    again = llm.run_agent("interpreter", "sys", "what is 2 doubled?", TOOLS, SCHEMA, mock=mock)
    assert again.output == {"answer": 4} and again.mode == "replay"


def test_total_cap_falls_back_to_mock_or_raises(live, tmp_path):
    fake = live([])
    (tmp_path / "spend.json").write_text(json.dumps({"usd": 50.0}))
    res = llm.run_agent("orchestrator", "sys", "brief", [], SCHEMA, mock=mock)
    assert res.output == {"answer": -1} and res.model.startswith("mock fallback") and not fake.requests
    assert "BudgetExceeded" in (tmp_path / "llm_log.jsonl").read_text()
    with pytest.raises(llm.BudgetExceeded):
        llm.run_agent("orchestrator", "sys", "brief", [], SCHEMA)


def test_invalid_or_refused_answers_fall_back_to_mock(live):
    live([_msg([{"type": "text", "text": "not json"}], "end_turn")])
    assert llm.run_agent("interpreter", "s", "p", TOOLS, SCHEMA, mock=mock).output == {"answer": -1}
    live([_msg([{"type": "text", "text": ""}], "refusal")])
    res = llm.run_agent("interpreter", "s", "p2", TOOLS, SCHEMA, mock=mock)
    assert res.output == {"answer": -1} and "AgentError" in res.model
