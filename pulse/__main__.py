"""Pulse CLI.

    python -m pulse tick [--at "2026-11-17 15:00"]          one tick (default: latest data hour)
    python -m pulse tick --next                             re-read the dataset and tick the hour after the last tick
    python -m pulse replay --start TS --end TS [--step-h 1]  tick over a historical range (demo)
    python -m pulse watch [--interval-s 5] [--start TS]     follow data/eye/live.json (marketplace.live), or step
                                                             the clock one hour per interval from the last tick
                                                             (or from --start); survives failing checks, one watcher
                                                             per state dir, heartbeat in <state>/watch.json
    python -m pulse recheck                                 apply the CEO's pinned / untracked metrics to the last
                                                             checked hour (no new data, no LLM call)
    python -m pulse status | spend | reset

State goes to PULSE_STATE (default .pulse/). LLM mode from PULSE_LLM (default mock = free).
"""
from __future__ import annotations

import argparse
import json
import os
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
    stream = os.environ.get("PULSE_STREAM_LOG") == "1"    # the event console: the tick prints its lines as it goes
    if stream and ts is not None:
        print(f"[{pd.Timestamp(ts):%Y-%m-%d %H:%M}] checking", flush=True)
    st = tick(ts)
    print(_line(st), flush=True)
    if verbose and not stream:
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


def _beat(hb: dict) -> None:
    """The watcher's heartbeat, <state>/watch.json: the UI shows from it whether the loop runs and what it does."""
    hb["beat"] = time.time()
    p = C.STATE_DIR / "watch.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hb, default=str), encoding="utf-8")
    C.replace(tmp, p)


def _other_watcher(interval_s: float) -> dict | None:
    """The heartbeat of another watcher still running on this state dir (one watcher per state dir)."""
    try:
        old = json.loads((C.STATE_DIR / "watch.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    fresh = time.time() - old.get("beat", 0) < 2 * max(old.get("interval_s", 0), interval_s) + 300
    return old if fresh and not old.get("stopped") and old.get("pid") != os.getpid() else None


def cmd_watch(a):
    from pulse.world import world
    other = _other_watcher(a.interval_s)
    if other:
        print(f"another watcher (process {other.get('pid')}) is already running on {C.STATE_DIR}", flush=True)
        return 1
    mode = "live.json" if LIVE.exists() else "stepping"
    hb = {"pid": os.getpid(), "started": time.time(), "interval_s": a.interval_s, "mode": mode, "loading": True,
          "checking": None, "next_check": None, "errors": 0, "last_error": None, "stopped": None, "recent": []}
    _beat(hb)
    w = world()
    data_end = w.now          # a fresh world sits at the last hour of data
    last = None
    hb["loading"] = False
    print(f"watching ({mode}) every {a.interval_s:g} s; Ctrl+C to stop", flush=True)
    stop, fails = "stopped", 0
    try:
        while True:
            nxt, reload = None, False
            if LIVE.exists():
                now = _live_now()   # live.json "now" = next hour to be simulated; latest data = now - 1h
                if now is not None and now != last:
                    nxt, reload = now, True       # the simulator rewrote data/eye
            else:
                st = C.load(TickState, "state.json")
                first = pd.Timestamp(a.start) if a.start else data_end - pd.Timedelta(hours=23)
                nxt = pd.Timestamp(st.ts) + pd.Timedelta(hours=1) if st else first
                if nxt > data_end:
                    stop = "end of data"
                    print("reached the end of the data", flush=True)
                    return
            if nxt is not None:
                hb["checking"] = str(nxt)
                _beat(hb)
                t0 = time.time()
                try:   # a failing check must not stop the loop: log it, back off, try again
                    if reload:
                        world(reload=True)
                    st = _run(nxt, verbose=True)
                    last, fails = nxt, 0
                    br = C.load(Brief, "brief.json")
                    hb["recent"] = ([{"hour": str(st.ts), "at": time.time(), "seconds": round(time.time() - t0, 1),
                                      "headline": br.headline if br else ""}] + hb["recent"])[:8]
                    hb["last_error"] = None
                except Exception as e:
                    fails += 1
                    hb["errors"] += 1
                    hb["last_error"] = f"{nxt}: {type(e).__name__}: {str(e)[:240]}"
                    print(f"check of {nxt} failed ({type(e).__name__}: {e}); trying again", file=sys.stderr, flush=True)
                hb["checking"] = None
            delay = a.interval_s * min(fails + 1, 6)
            hb["next_check"] = time.time() + delay
            _beat(hb)
            time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        hb.update(checking=None, next_check=None, stopped=stop)
        _beat(hb)


def cmd_recheck(a):
    from pulse.orchestrator import recheck
    st = recheck()
    print(_line(st) if st else f"no state in {STATE_DIR} yet - run `python -m pulse tick` first", flush=True)


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
    p = sub.add_parser("watch"); p.add_argument("--interval-s", type=float, default=5.0); p.add_argument("--start")
    p.set_defaults(fn=cmd_watch)
    sub.add_parser("recheck").set_defaults(fn=cmd_recheck)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("spend").set_defaults(fn=cmd_spend)
    p = sub.add_parser("reset"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_reset)
    a = ap.parse_args(argv)
    r = a.fn(a)
    return r if isinstance(r, int) else None


if __name__ == "__main__":
    sys.exit(main())
