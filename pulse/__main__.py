"""Pulse CLI.

    python -m pulse tick [--at "2026-11-17 15:00"]          one tick (default: latest data hour)
    python -m pulse tick --next                             re-read the dataset and tick the hour after the last tick
    python -m pulse replay --start TS --end TS [--step-h 1]  tick over a historical range (demo)
    python -m pulse watch [--interval-s 5]                  follow data/eye/live.json (marketplace.live), or step
                                                             the clock one hour per interval from the last tick
    python -m pulse status | spend | reset

State goes to PULSE_STATE (default .pulse/). LLM mode from PULSE_LLM (default mock = free).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time

import pandas as pd

from pulse import llm
from pulse.contracts import ROOT, STATE_DIR, Brief, TickState
from pulse import contracts as C

LIVE = ROOT / "data" / "eye" / "live.json"
SHORT = {"alert": "ALERT", "watch": "watch", "data_issue": "DATA", "ok": "ok"}


def _line(st: TickState) -> str:
    bad = [f"{k.name} {SHORT[k.severity]} {k.deviation_pct:+.0f}%" if k.deviation_pct is not None
           else f"{k.name} {SHORT[k.severity]}" for k in st.kpis if k.severity != "ok"]
    br = C.load(Brief, "brief.json")
    head = br.headline if br else ""
    return f"[{st.ts}] tick {st.tick:>3} | {', '.join(bad) or 'all ok'} | {head}"


def _run(ts, verbose: bool) -> TickState:
    from pulse.orchestrator import tick
    st = tick(ts)
    print(_line(st), flush=True)
    if verbose:
        for l in st.log:
            print(f"      - {l}")
    return st


def cmd_tick(a):
    if not a.next:
        return _run(a.at, verbose=True)
    from pulse.world import world
    data_end = world(reload=True).now          # re-read data/eye from disk
    st = C.load(TickState, "state.json")
    nxt = pd.Timestamp(st.ts) + pd.Timedelta(hours=1) if st else data_end
    if nxt > data_end:
        print(f"no newer data: the dataset ends at {data_end:%d %b %H:%M}", flush=True)
        return
    _run(nxt, verbose=True)


def cmd_replay(a):
    t, end = pd.Timestamp(a.start), pd.Timestamp(a.end)
    while t <= end:
        _run(t, verbose=not a.quiet)
        t += pd.Timedelta(hours=a.step_h)


def _live_now():
    try:
        return pd.Timestamp(json.loads(LIVE.read_text(encoding="utf-8"))["now"]) - pd.Timedelta(hours=1)
    except Exception:
        return None


def cmd_watch(a):
    from pulse.world import world
    w = world()
    data_end = w.now          # a fresh world sits at the last hour of data
    last = None
    mode = "live.json" if LIVE.exists() else "stepping"
    print(f"watching ({mode}); Ctrl+C to stop", flush=True)
    try:
        while True:
            if LIVE.exists():
                now = _live_now()   # live.json "now" = next hour to be simulated; latest data = now - 1h
                if now is not None and now != last:
                    w = world(reload=True)        # the simulator rewrote data/eye
                    _run(now, verbose=True)
                    last = now
            else:
                st = C.load(TickState, "state.json")
                nxt = pd.Timestamp(st.ts) + pd.Timedelta(hours=1) if st else data_end - pd.Timedelta(hours=23)
                if nxt > data_end:
                    print("reached the end of the data", flush=True)
                    return
                _run(nxt, verbose=True)
            time.sleep(a.interval_s)
    except KeyboardInterrupt:
        pass


def cmd_status(a):
    from pulse.orchestrator import status_text
    print(status_text())


def cmd_spend(a):
    print(f"LLM mode {llm.mode()} | cumulative spend ${llm.total_spend():.4f} | cap "
          f"${float(__import__('os').environ.get('PULSE_TOTAL_CAP_USD', '12.0')):.2f} | state {STATE_DIR}")


def cmd_reset(a):
    if STATE_DIR.exists():
        keep = STATE_DIR / "cassettes"
        for p in STATE_DIR.iterdir():
            if p == keep and not a.all:
                continue          # recorded LLM responses are money: keep them unless --all
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    print(f"cleared {STATE_DIR}" + ("" if a.all else " (kept cassettes/)"))


def main(argv=None):
    llm.load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m pulse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick"); p.add_argument("--at"); p.add_argument("--next", action="store_true")
    p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("replay"); p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.add_argument("--step-h", type=int, default=1); p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("watch"); p.add_argument("--interval-s", type=float, default=5.0); p.set_defaults(fn=cmd_watch)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("spend").set_defaults(fn=cmd_spend)
    p = sub.add_parser("reset"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_reset)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
