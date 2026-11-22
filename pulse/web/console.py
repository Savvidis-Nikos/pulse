"""Event console: a simulated marketplace that runs on demand, so a demo can cause problems and watch Pulse react.

The console has its own data and state and never touches data/eye/ or the recorded run:
    data/live/      simulator output; live.json holds the market clock and the injected scenarios
    .pulse_live/    Pulse state for the live market; console.jsonl is the log the UI shows
One job runs at a time, in a worker thread:
    start           quiet history from 1 Nov up to START_AT, fresh Pulse state, first check
    inject <id>     a simulator scenario, live from the next market hour
    run <hours>     simulate the hours in memory, write the files once, then `python -m pulse replay` over them
The simulator stays in memory between runs (about 0.1 s per hour); after a server restart it is rebuilt once from
live.json (about 25 s). Pulse runs in its own process, exactly as on the command line.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

START_AT = "2026-11-15 10:00"   # Sunday morning: traffic is rising, so a fault shows within an hour or two
SEED = 7
MAX_HOURS = 24
LOG_KEEP = 300

# What the console offers, in display order: scenarios Pulse handles in the offline scorecard.
CATALOG = [
    ("payment_failure", "fault", "Payment failure on Android in Germany"),
    ("checkout_capacity", "fault", "Checkout overload after a database change"),
    ("search_relevance", "fault", "Broken search for German shoppers"),
    ("foryou_unavailable", "fault", "For You feed recommends unavailable products"),
    ("seller_outage", "fault", "Top fashion seller's catalog feed fails"),
    ("stockout", "fault", "Electronics stockout after a late supplier delivery"),
    ("ads_pacing", "fault", "Ad budgets for home products run out early"),
    ("click_fraud", "fault", "Bot clicks on sponsored ads in Poland"),
    ("fr_campaign", "innocent", "France Black Week teaser campaign"),
    ("catalog_launch", "innocent", "New fashion collection goes live"),
    ("football_de", "innocent", "Football match on TV in Germany"),
    ("search_maintenance", "innocent", "Planned search maintenance"),
    ("late_orders_data", "data", "Order data arrives late"),
    ("frozen_inventory", "data", "Inventory feed stuck on an old snapshot"),
]
TITLE = {sid: title for sid, _, title in CATALOG}
TICK_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d)\] tick")


def _hm(ts) -> str:
    return f"{ts:%d %b, %H:%M}".lstrip("0")


class Console:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.data = self.root / "data" / "live"
        self.state = self.root / ".pulse_live"
        self.live_json = self.data / "live.json"
        self.log_path = self.state / "console.jsonl"
        self.source = "recorded"          # which state the UI shows: the recorded run or the live market
        self.job: str | None = None
        self._busy = threading.Lock()     # one job at a time
        self._log_lock = threading.Lock()
        self._log: list[dict] = self._read_log()
        self._mp = None                   # simulator kept in memory between runs
        self._pending: list = []
        self._truths: list = []

    # ---------- what the UI reads ----------
    def session(self) -> dict | None:
        try:
            return json.loads(self.live_json.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _st(self) -> dict:
        st = self.session()
        if st is None:
            raise RuntimeError("no live market: start it first")
        return st

    def status(self) -> dict:
        st = self.session()
        with self._log_lock:
            log = self._log[-150:]
        return {
            "enabled": True, "source": self.source, "busy": self.job is not None, "job": self.job,
            "catalog": [{"id": sid, "kind": kind, "title": title} for sid, kind, title in CATALOG],
            "session": None if not st else {
                "clock": st["now"],
                "last_hour": (datetime.fromisoformat(st["now"]) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                "injected": [{"id": i["name"], "title": TITLE.get(i["name"], i["name"].replace("_", " ")), "at": i["at"]}
                             for i in st["injected"]]},
            "log": log,
        }

    def state_dir(self, recorded: Path) -> Path:
        return self.state if self.source == "live" else recorded

    # ---------- actions ----------
    def act(self, body: dict) -> tuple[dict, int]:
        action = body.get("action")
        if action == "source":
            if body.get("source") not in ("recorded", "live"):
                return {"error": "source must be recorded or live"}, 400
            self.source = body["source"]
            return self.status(), 200
        if action == "start":
            self.source = "live"
            return self._submit("Starting the market", self._start)
        if self.session() is None:
            return {"error": "Start the live market first"}, 400
        if action == "inject":
            sid = body.get("id")
            if sid not in TITLE:
                return {"error": f"Unknown scenario {sid!r}"}, 400
            if not self._busy.acquire(blocking=False):
                return {"error": "The market is busy, try again when the current run ends"}, 409
            try:
                self._inject(sid)
            except Exception as e:
                self._add("error", f"Injecting failed: {e}")
            finally:
                self._busy.release()
            return self.status(), 200
        if action == "run":
            try:
                hours = max(1, min(MAX_HOURS, int(body.get("hours", 1))))
            except (TypeError, ValueError):
                return {"error": "hours must be a number"}, 400
            return self._submit(f"Running {hours} hour{'s' if hours > 1 else ''}", lambda: self._run(hours))
        return {"error": f"Unknown action {action!r}"}, 400

    def run_sync(self, hours: int = 1) -> tuple[dict, int]:
        """The briefing's Refresh button on the live market: one more hour, answered when it is done."""
        if self.session() is None:
            return {"error": "Start the live market first"}, 400
        if not self._busy.acquire(blocking=False):
            return {"error": "The market is busy"}, 409
        self.job = "Running 1 hour"
        try:
            self._run(hours)
        except Exception as e:
            self._add("error", f"Run failed: {e}")
            return {"error": str(e)}, 500
        finally:
            self.job = None
            self._busy.release()
        st = self.session()
        return {"message": f"Market clock {st['now']}" if st else "done"}, 200

    def _submit(self, label: str, fn) -> tuple[dict, int]:
        if not self._busy.acquire(blocking=False):
            return {"error": "The market is busy, try again when the current run ends"}, 409
        self.job = label

        def work():
            try:
                fn()
            except Exception as e:
                traceback.print_exc()
                self._add("error", f"{label} failed: {e}")
            finally:
                self.job = None
                self._busy.release()
        threading.Thread(target=work, daemon=True).start()
        return self.status(), 202

    # ---------- jobs (worker thread, holding _busy) ----------
    def _start(self):
        import pandas as pd
        from marketplace import live as L
        from marketplace.run import simulate, write
        self._pulse(["reset"])                       # fresh Pulse state (keeps recorded LLM cassettes)
        with self._log_lock:
            self._log = []
        self.log_path.unlink(missing_ok=True)
        now = pd.Timestamp(START_AT)
        last = now - pd.Timedelta(hours=1)
        self._add("market", "Starting a quiet market with history from 1 Nov")
        self._mp = simulate(SEED, start=L.HISTORY_START, actions=[], until=now)
        self._pending, self._truths = [], []
        self.data.mkdir(parents=True, exist_ok=True)
        write(self._mp, self.data, [], SEED)
        self._save({"seed": SEED, "now": str(now)[:16], "injected": []})
        self._add("market", f"Market ready at {_hm(last)}", t=str(last)[:16])
        self._replay(last, last)

    def _inject(self, sid: str):
        import pandas as pd
        from marketplace import live as L
        st = self._st()
        at = pd.Timestamp(st["now"])
        acts, truth = L.build(sid, at)
        st["injected"].append({"name": sid, "at": str(at)[:16]})
        self._save(st)
        if self._mp is not None:                      # otherwise the next run rebuilds it from live.json
            self._pending = sorted(self._pending + acts, key=lambda x: x[0])
            self._truths.append(truth)
        start = pd.Timestamp(truth["start"])
        self._add("inject", f"{TITLE[sid]}, from {_hm(start)}", t=str(at)[:16], scenario=sid)

    def _run(self, hours: int):
        import pandas as pd
        from marketplace.run import write
        st = self._st()
        self._ensure_sim(st)
        assert self._mp is not None
        H = pd.Timedelta(hours=1)
        now = first = pd.Timestamp(st["now"])
        for _ in range(hours):
            due = [x for x in self._pending if x[0] < now + H]
            self._pending = [x for x in self._pending if x[0] >= now + H]
            for when, fn, kw in due:
                fn(self._mp, when, **kw)
            self._mp.step(now)
            now += H
        write(self._mp, self.data, self._truths, st["seed"])
        st["now"] = str(now)[:16]
        self._save(st)
        span = f"{first:%H:%M}" if hours == 1 else f"{first:%H:%M} to {now - H:%H:%M}"
        self._add("market", f"Market ran {hours} hour{'s' if hours > 1 else ''} ({span})", t=str(now - H)[:16])
        self._replay(first, now - H)

    def _ensure_sim(self, st: dict):
        if self._mp is not None:
            return
        import pandas as pd
        from marketplace import live as L
        from marketplace.run import simulate
        pending, truths = [], []
        for inj in st["injected"]:
            acts, truth = L.build(inj["name"], pd.Timestamp(inj["at"]))
            pending += acts
            truths.append(truth)
        now = pd.Timestamp(st["now"])
        self._add("market", f"Loading the market up to {_hm(now)}")
        self._mp = simulate(st["seed"], start=L.HISTORY_START, actions=pending, until=now)
        self._pending = sorted((x for x in pending if x[0] >= now), key=lambda x: x[0])
        self._truths = truths

    # ---------- Pulse ----------
    def _env(self) -> dict:
        return {**os.environ, "PULSE_STATE": str(self.state), "PULSE_DATA": str(self.data),
                "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

    def _pulse(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pulse", *args], cwd=self.root, env=self._env(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)

    def _replay(self, start, end):
        """Run the monitoring loop over the new hours, streaming its output into the console log."""
        p = subprocess.Popen([sys.executable, "-m", "pulse", "replay", "--start", str(start)[:16], "--end", str(end)[:16]],
                             cwd=self.root, env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        assert p.stdout is not None
        t, other = None, []
        for raw in p.stdout:
            line = raw.rstrip()
            m = TICK_LINE.match(line)
            if m:
                t = m.group(1)
                self._add("check", line, t=t)
            elif line.startswith("      - "):
                self._add("step", line.strip()[2:], t=t)
            elif line.strip():
                other.append(line)
        if p.wait() != 0:
            self._add("error", f"Pulse stopped: {other[-1] if other else 'exit code ' + str(p.returncode)}", t=t)

    # ---------- files ----------
    def _save(self, st: dict):
        self.data.mkdir(parents=True, exist_ok=True)
        tmp = self.live_json.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        os.replace(tmp, self.live_json)

    def _read_log(self) -> list[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []

    def _add(self, kind: str, text: str, t: str | None = None, **extra):
        with self._log_lock:
            n = (self._log[-1]["n"] + 1) if self._log else 1
            entry = {"n": n, "t": t, "kind": kind, "text": text, **extra}
            self._log = (self._log + [entry])[-LOG_KEEP:]
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass   # the in-memory log still feeds the UI
