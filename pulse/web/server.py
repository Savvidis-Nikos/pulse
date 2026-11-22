"""Pulse web UI server: static front-end + a read-only JSON API over the Pulse state directory.

    .venv/Scripts/python.exe -m pulse.web                  # serves http://127.0.0.1:8765, state from PULSE_STATE or .pulse/
    .venv/Scripts/python.exe -m pulse.web --state DIR --port 8765
    .venv/Scripts/python.exe -m pulse.web --demo           # writes a demo state into a temp dir and serves it

Standard library only. The server never computes analytics: it only reads files written by the agents
(see pulse/contracts.py for the layout), so the UI works in mock, replay and live mode alike.
The one write is POST /api/refresh: it runs `python -m pulse tick --next` (re-read the dataset, check the next hour).
When the live simulator owns data/eye (data/eye/live.json) and Pulse has already checked its last hour, the refresh
first asks the simulator for one more hour (`python -m marketplace.live tick`), so every click brings new data.
A static dataset (no live.json) is never rewritten.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "eye" / "live.json"
_refreshing = threading.Lock()
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl(p: Path, limit: int = 200) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _encoding():
    """Shared visual encoding (pulse.dashboard.encoding): meaning tokens the front-end maps to theme colours."""
    try:
        from pulse.dashboard import encoding
        return encoding
    except Exception:
        return None


def with_layout(snap: dict) -> dict:
    """Add fixed x/y per node, orbit radii and style tokens, using the shared deterministic solar layout
    (pulse.dashboard.layout), so the browser only renders positions and never runs a physics simulation."""
    from pulse.contracts import GraphSnapshot
    from pulse.dashboard import layout as L
    model = GraphSnapshot.model_validate(snap)
    pos = L.solar_layout(model)
    enc = _encoding()
    for n in snap.get("nodes", []):
        if enc:
            try:
                n["style"] = enc.node_style(dict(n), center=snap.get("center"))
            except Exception:
                pass   # the front-end falls back to its own rules
        x, y = pos.get(n["id"], (0.0, 0.0))
        n["x"], n["y"] = x, y
    if enc:
        for e in snap.get("edges", []):
            try:
                e["style"] = enc.edge_style(dict(e))
            except Exception:
                pass
    snap["layout"] = {"aspect": L.ASPECT, "orbits": {str(k): v for k, v in L.orbit_radii(model).items()},
                      "depths": L.node_depths(model)}
    return snap


def with_frames(interp: dict, state: Path) -> dict:
    """Add step-replay frames (pulse.dashboard.encoding.trace_frames), aligned to the snapshot's edge directions."""
    enc = _encoding()
    if not enc:
        return interp
    sid = interp.get("snapshot_id")
    snap = _read_json(state / "snapshots" / f"{sid}.json") if sid and SAFE_ID.match(sid) else None
    try:
        interp["frames"] = enc.trace_frames(dict(interp), snap=snap, partial=interp.get("partial"))
    except Exception:
        pass   # the front-end derives frames itself
    return interp


def overview(state: Path) -> dict:
    """Everything the briefing page needs in one call, plus an index of investigations."""
    snaps = []
    sdir = state / "snapshots"
    if sdir.exists():
        for f in sorted(sdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            d = _read_json(f)
            if isinstance(d, dict):
                snaps.append({"snapshot_id": d.get("snapshot_id", f.stem), "kpi": d.get("kpi"),
                              "center": d.get("center"), "ts": d.get("ts"), "nodes": len(d.get("nodes", []))})
    interps = []
    idir = state / "interpretations"
    if idir.exists():
        for f in sorted(idir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            partial_file = f.name.endswith(".partial.json")
            iid = f.name[: -len(".partial.json")] if partial_file else f.stem
            if partial_file and (idir / f"{iid}.json").exists():
                continue
            d = _read_json(f) or {}
            interps.append({"interpretation_id": iid, "partial": partial_file,
                            "incident_id": d.get("incident_id") or iid, "kpi": d.get("kpi"),
                            "snapshot_id": d.get("snapshot_id"), "headline": d.get("headline"),
                            "summary": d.get("summary"), "confidence": d.get("confidence"),
                            "steps": len(d.get("trace") or [])})
    mt = [p.stat().st_mtime for p in state.rglob("*") if p.is_file()] if state.exists() else []
    return {
        "state_dir": str(state),
        "found": state.exists(),
        "version": max(mt) if mt else 0,
        "tick": _read_json(state / "state.json"),
        "incidents": _read_json(state / "incidents.json") or [],
        "brief": _read_json(state / "brief.json"),
        "spend": _read_json(state / "spend.json") or {"usd": 0},
        "changes": _read_jsonl(state / "graph_changes.jsonl"),
        "snapshots": snaps,
        "interpretations": interps,
        "cap_usd": float(os.environ.get("PULSE_TOTAL_CAP_USD", "12.0")),
        "legend": getattr(_encoding(), "LEGEND", None),
    }


def _live_caught_up(state: Path) -> bool:
    """True when the live simulator writes data/eye and Pulse has checked its last hour (live.json `now` - 1 h)."""
    data = os.environ.get("PULSE_DATA")
    if not LIVE.exists() or (data and Path(data).resolve() != LIVE.parent.resolve()):
        return False
    try:
        live_end = datetime.fromisoformat(str(json.loads(LIVE.read_text(encoding="utf-8"))["now"])) - timedelta(hours=1)
        return datetime.fromisoformat(str(json.loads((state / "state.json").read_text(encoding="utf-8"))["ts"])) == live_end
    except Exception:
        return False


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)


def _last_error(r: subprocess.CompletedProcess, default: str) -> str:
    err = [l for l in r.stderr.splitlines() if l.strip()]
    return err[-1] if err else default


def refresh(state: Path) -> tuple[dict, int]:
    """Re-read the simulated dataset and run the monitoring loop on the next hour (same as the CLI, own process).
    In live mode, simulate the next marketplace hour first when Pulse has already seen all the data."""
    if not _refreshing.acquire(blocking=False):
        return {"error": "A refresh is already running"}, 409
    try:
        env = {**os.environ, "PULSE_STATE": str(state), "PYTHONIOENCODING": "utf-8"}
        simulated = _live_caught_up(state)
        if simulated:
            s = _run(["marketplace.live", "tick", "--hours", "1"], env)
            if s.returncode != 0:
                return {"error": f"The simulator failed: {_last_error(s, 'no output')}"}, 500
        r = _run(["pulse", "tick", "--next"], env)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if r.returncode != 0:
            return {"error": _last_error(r, "The check failed")}, 500
        return {"message": lines[0] if lines else "done", "simulated": simulated}, 200
    except subprocess.TimeoutExpired:
        return {"error": "The check took too long"}, 504
    finally:
        _refreshing.release()


class Handler(SimpleHTTPRequestHandler):
    state_dir: Path = Path(".pulse")
    allow_refresh: bool = True

    def log_message(self, format, *args):  # keep the console quiet; 4xx/5xx still print
        if len(args) > 1 and str(args[1])[:1] in ("4", "5"):
            super().log_message(format, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/"):
            if path in ("", "/"):
                self.path = "/index.html"
            return super().do_GET()
        parts = [p for p in path.split("/") if p][1:]
        st = self.state_dir
        if parts == ["overview"]:
            return self._json(overview(st))
        if len(parts) == 2 and SAFE_ID.match(parts[1]):
            kind, ident = parts
            if kind == "snapshot":
                d = _read_json(st / "snapshots" / f"{ident}.json")
                return self._json(with_layout(d)) if d else self._json({"error": f"No snapshot named {ident}"}, 404)
            if kind == "interpretation":
                d = _read_json(st / "interpretations" / f"{ident}.json")
                if d:
                    d["partial"] = False
                    return self._json(with_frames(d, st))
                d = _read_json(st / "interpretations" / f"{ident}.partial.json")
                if d:
                    d["partial"] = True
                    return self._json(with_frames(d, st))
                return self._json({"error": f"No interpretation named {ident}"}, 404)
        return self._json({"error": f"Unknown endpoint {path}"}, 404)

    def do_POST(self):
        if unquote(urlparse(self.path).path) == "/api/refresh":
            if not self.allow_refresh:
                return self._json({"error": "Refresh is off for the demo fixtures"}, 403)
            return self._json(*refresh(self.state_dir))
        return self._json({"error": "Unknown endpoint"}, 404)


def serve(state_dir: Path, host: str = "127.0.0.1", port: int = 8765, allow_refresh: bool = True) -> None:
    Handler.state_dir = Path(state_dir)
    Handler.allow_refresh = allow_refresh
    httpd = ThreadingHTTPServer((host, port), partial(Handler, directory=str(STATIC)))
    print(f"Pulse UI on http://{host}:{port}  (state: {Handler.state_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
