"""`pulse watch`: the always-on loop keeps going when a check fails, writes a heartbeat the UI reads, stops at the end
of the data, and refuses to start a second watcher on the same state dir. No world load, no LLM."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import pulse.__main__ as cli
import pulse.world
from pulse import contracts as C
from pulse.contracts import TickState


@pytest.fixture
def watch_env(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cli, "LIVE", tmp_path / "no-live.json")
    monkeypatch.setattr(pulse.world, "world", lambda reload=False: SimpleNamespace(now=pd.Timestamp("2026-11-17 14:00")))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return tmp_path


def test_watch_survives_a_failing_check_and_stops_at_the_end_of_the_data(watch_env, monkeypatch):
    calls = []

    def fake_run(ts, verbose):
        calls.append(str(ts))
        if len(calls) == 2:
            raise RuntimeError("boom")
        st = TickState(ts=str(ts), tick=len(calls), kpis=[])
        C.save(st, "state.json")
        return st

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["watch", "--interval-s", "0", "--start", "2026-11-17 12:00"]) is None
    # 13:00 failed once and was checked again; the loop went on to the last hour of data
    assert calls == ["2026-11-17 12:00:00", "2026-11-17 13:00:00", "2026-11-17 13:00:00", "2026-11-17 14:00:00"]
    hb = json.loads((watch_env / "watch.json").read_text(encoding="utf-8"))
    assert hb["stopped"] == "end of data" and hb["errors"] == 1 and hb["last_error"] is None
    assert hb["checking"] is None and [r["hour"] for r in hb["recent"]] == [
        "2026-11-17 14:00:00", "2026-11-17 13:00:00", "2026-11-17 12:00:00"]


def test_one_watcher_per_state_dir(watch_env):
    beat = {"pid": -1, "beat": time.time(), "interval_s": 5, "stopped": None}
    (watch_env / "watch.json").write_text(json.dumps(beat), encoding="utf-8")
    assert cli.main(["watch", "--interval-s", "5"]) == 1
