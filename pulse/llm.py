"""LLM gateway: the ONLY place Pulse talks to Claude. Cost control lives here.

Modes (env PULSE_LLM):
  mock    (default) no API call. The caller passes a `mock` policy that scripts tool calls and the final answer,
          so the whole loop (tools, traces, files, dashboard) runs for free. Use this for all development and tests.
  replay  no API call. Replays responses recorded by a previous live run (cassettes/), executing the real tools.
          Falls back to mock if no cassette matches. Use for rehearsing the demo for free.
  live    real API calls, recorded to cassettes/ automatically. Guarded by two caps:
            PULSE_RUN_CAP_USD   (default 0.60) per agent run
            PULSE_TOTAL_CAP_USD (default 12.0) cumulative, persisted in <state>/spend.json
          At the run cap the model is told to answer from the evidence it has; at the total cap no call is made.
  Any live / replay failure (cap reached, API error, refusal, invalid answer) is logged to llm_log.jsonl and the
  run falls back to the caller's mock policy, so the monitoring loop never stops because of the LLM.

Model routing (env overrides PULSE_MODEL_<ROLE>): orchestrator/briefer -> Opus 5.5, workers -> Sonnet 5.5.

Usage:
    from pulse.llm import Tool, run_agent
    res = run_agent("interpreter", system, prompt, tools, schema, mock=my_mock_policy)
    res.output   # dict matching `schema` (structured final answer)
    res.calls    # [{tool, args, result, error}]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pulse import contracts as C

MODELS = {
    "orchestrator": "claude-opus-5-5",
    "briefer": "claude-opus-5-5",
    "interpreter": "claude-sonnet-5-5",
    "graph_handler": "claude-sonnet-5-5",
}
EFFORT = {"orchestrator": "medium", "briefer": "medium", "interpreter": "medium", "graph_handler": "medium"}
# USD per 1M tokens: input, output, cache write (5m, 1.25x), cache read
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
MAX_TOKENS = 16000
BETAS = ["server-side-fallback-2026-07-01"]


class BudgetExceeded(RuntimeError):
    pass


class AgentError(RuntimeError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]          # called with **args; return str or JSON-serialisable

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class RunResult:
    output: dict
    calls: list[dict] = field(default_factory=list)
    turns: int = 0
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    model: str = ""
    mode: str = "mock"
    seconds: float = 0.0


# A mock policy gets the calls made so far ([{tool, args, result, error}]) and returns either
#   {"tools": [(name, args), ...]}  -> those tools are executed, then the policy is called again
#   {"final": {...}}                -> the run ends with that output (must match the schema)
MockPolicy = Callable[[list[dict]], dict]


def obj(props: dict, required: list[str] | None = None) -> dict:
    """Small helper for tool input schemas."""
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def mode() -> str:
    m = os.environ.get("PULSE_LLM", "mock").lower()
    return m if m in ("mock", "replay", "live") else "mock"


def model_for(role: str) -> str:
    return os.environ.get(f"PULSE_MODEL_{role.upper()}", MODELS.get(role, "claude-sonnet-5-5"))


def total_spend() -> float:
    p = C.STATE_DIR / "spend.json"
    try:
        return float(json.loads(p.read_text())["usd"])
    except Exception:
        return 0.0


def _add_spend(usd: float) -> None:
    p = C.STATE_DIR / "spend.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"usd": round(total_spend() + usd, 6)}))


def cost(model: str, u: dict) -> float:
    pi, po, pw, pr = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (u.get("input_tokens", 0) * pi + u.get("output_tokens", 0) * po
            + u.get("cache_creation_input_tokens", 0) * pw + u.get("cache_read_input_tokens", 0) * pr) / 1e6


def _log(rec: dict) -> None:
    p = C.STATE_DIR / "llm_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _key(role: str, system: str, prompt: str, tools: list[Tool]) -> str:
    h = hashlib.sha1(json.dumps([role, system, prompt, sorted(t.name for t in tools)]).encode()).hexdigest()[:16]
    return f"{role}-{h}"


def _cassette_path(key: str):
    return C.STATE_DIR / "cassettes" / f"{key}.json"


def _run_tool(tool: Tool | None, name: str, args: dict) -> tuple[str, bool]:
    if tool is None:
        return f"error: unknown tool {name}", True
    try:
        out = tool.fn(**args)
        s = out if isinstance(out, str) else json.dumps(out, default=str)
        return s[:20000], False
    except Exception as e:  # tool errors go back to the model, never crash the run
        return f"error: {type(e).__name__}: {e}", True


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


# ---------------------------------------------------------------- mock

def _run_mock(role, tools, mock: MockPolicy | None, max_turns: int, on_call=None) -> RunResult:
    if mock is None:
        raise AgentError(f"{role}: PULSE_LLM={mode()} but no mock policy was given")
    by = {t.name: t for t in tools}
    calls: list[dict] = []
    for turn in range(1, max_turns + 2):
        step = mock(calls)
        if "final" in step:
            return RunResult(step["final"], calls, turn, {}, 0.0, "mock", "mock")
        for name, args in step.get("tools", []):
            s, err = _run_tool(by.get(name), name, args)
            rec = {"tool": name, "args": args, "result": s, "error": err}
            calls.append(rec)
            if on_call:
                on_call(rec)
    raise AgentError(f"{role}: mock policy never produced a final answer")


# ---------------------------------------------------------------- live / replay

def run_agent(role: str, system: str, prompt: str, tools: list[Tool], schema: dict, *,
              mock: MockPolicy | None = None, max_turns: int = 12, effort: str | None = None,
              on_call: Callable[[dict], None] | None = None) -> RunResult:
    """Run one tool-using agent to a schema-constrained final answer. See module doc for modes."""
    m = mode()
    if m == "mock":
        return _run_mock(role, tools, mock, max_turns, on_call)
    try:
        return _run_model(m, role, system, prompt, tools, schema, mock, max_turns, effort, on_call)
    except Exception as e:
        if mock is None:
            raise
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        _log({"ts": time.time(), "role": role, "mode": m, "error": msg, "fallback": "mock policy"})
        print(f"[pulse.llm] {role}: {m} run failed ({msg}) - falling back to the mock policy", file=sys.stderr)
        res = _run_mock(role, tools, mock, max_turns, on_call)
        res.model = f"mock fallback ({m} failed: {type(e).__name__})"
        return res


def _run_model(m, role, system, prompt, tools, schema, mock, max_turns, effort, on_call) -> RunResult:
    key = _key(role, system, prompt, tools)
    cassette = None
    if m == "replay":
        p = _cassette_path(key)
        if not p.exists():
            return _run_mock(role, tools, mock, max_turns, on_call)
        cassette = json.loads(p.read_text(encoding="utf-8"))

    model = model_for(role)
    run_cap = float(os.environ.get("PULSE_RUN_CAP_USD", "0.60"))
    total_cap = float(os.environ.get("PULSE_TOTAL_CAP_USD", "12.0"))
    if m == "live" and total_spend() >= total_cap:
        raise BudgetExceeded(f"total LLM spend {total_spend():.2f} USD reached PULSE_TOTAL_CAP_USD={total_cap}")

    client = None
    if m == "live":
        import anthropic
        client = anthropic.Anthropic(max_retries=3, timeout=240.0)
    by = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": prompt}]
    params = dict(model=model, max_tokens=MAX_TOKENS, betas=BETAS, fallbacks="default",
                  system=[{"type": "text", "text": system}],
                  output_config={"effort": effort or EFFORT.get(role, "medium"),
                                 "format": {"type": "json_schema", "schema": schema}},
                  cache_control={"type": "ephemeral"})
    if tools:
        params["tools"] = [t.spec() for t in tools]
    usage: dict = {}
    calls: list[dict] = []
    recorded: list[dict] = []
    t0 = time.time()
    for turn in range(1, max_turns + 2):
        if m == "replay":
            assert cassette is not None
            if turn - 1 >= len(cassette):
                break
            resp = cassette[turn - 1]
            content, stop, u = resp["content"], resp["stop_reason"], {}
        else:
            assert client is not None
            r = client.beta.messages.create(messages=messages, **params)
            content = [b.to_dict(exclude_none=True) for b in r.content]   # echoed back unchanged (thinking too)
            stop = r.stop_reason or ""
            u = {k: getattr(r.usage, k, 0) or 0 for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
            recorded.append({"content": content, "stop_reason": stop})
            c = cost(model, u)
            _add_spend(c)
            for k, v in u.items():
                usage[k] = usage.get(k, 0) + v
            _log({"ts": time.time(), "role": role, "model": model, "turn": turn, "stop": stop, "usage": u,
                  "usd": round(c, 5)})
        if stop == "refusal":
            raise AgentError(f"{role}: the model declined the request")
        if stop == "max_tokens":
            raise AgentError(f"{role}: answer exceeded max_tokens")
        uses = [b for b in content if b.get("type") == "tool_use"]
        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": content})
            continue
        if stop != "tool_use" or not uses:
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            try:
                out = _parse_json(text)
            except Exception as e:
                raise AgentError(f"{role}: final answer was not valid JSON: {text[:200]!r}") from e
            if m == "live":
                p = _cassette_path(key)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(recorded), encoding="utf-8")
            return RunResult(out, calls, turn, usage, round(cost(model, usage), 4), model, m, round(time.time() - t0, 1))
        messages.append({"role": "assistant", "content": content})
        results = []
        for b in uses:
            s, err = _run_tool(by.get(b["name"]), b["name"], dict(b.get("input") or {}))
            rec = {"tool": b["name"], "args": dict(b.get("input") or {}), "result": s, "error": err}
            calls.append(rec)
            if on_call:
                on_call(rec)
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": s,
                            **({"is_error": True} if err else {})})
        over_budget = m == "live" and (cost(model, usage) >= run_cap or total_spend() >= total_cap)
        stop_now = turn >= max_turns or over_budget
        if stop_now:
            results.append({"type": "text", "text": "Budget exhausted: call no more tools. Give your final answer "
                                                    "now from the evidence already gathered."})
        messages.append({"role": "user", "content": results})
    if m == "replay":
        return _run_mock(role, tools, mock, max_turns, on_call)
    raise AgentError(f"{role}: no final answer within {max_turns} turns")


def available() -> bool:
    """True when live calls are possible (key present and mode=live)."""
    return mode() == "live" and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_dotenv() -> None:
    """Minimal .env loader (no extra dependency)."""
    from pulse.contracts import ROOT
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
