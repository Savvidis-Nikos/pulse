"""python -m pulse.web [--state DIR] [--port N] [--host H] [--demo | --demo-live]

  --demo       serve the shared demo fixtures (pulse/fixtures/state_demo, written by pulse.dashboard.make_fixtures)
  --demo-live  replay a live investigation into a temp dir (steps appear every few seconds) and serve it
"""
from __future__ import annotations

import argparse
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "pulse" / "fixtures" / "state_demo"


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m pulse.web", description="Serve the Pulse web UI.")
    ap.add_argument("--state", help="Pulse state directory (default: PULSE_STATE or .pulse/)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--demo", action="store_true", help="serve the shared demo fixtures")
    g.add_argument("--demo-live", action="store_true", help="animate a live investigation from the fixtures")
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between steps with --demo-live")
    a = ap.parse_args()

    if a.demo:
        state = FIXTURES
        if not (state / "state.json").exists():
            from pulse.dashboard.make_fixtures import write_all
            write_all(state)
    elif a.demo_live:
        state = Path(a.state) if a.state else Path(tempfile.gettempdir()) / "pulse_web_live_demo"
        from pulse.dashboard.make_fixtures import simulate
        threading.Thread(target=simulate, args=(state, a.delay), daemon=True).start()
    else:
        from pulse.contracts import STATE_DIR
        state = Path(a.state) if a.state else STATE_DIR

    from pulse.web.server import serve
    serve(state, a.host, a.port, allow_refresh=not a.demo)


if __name__ == "__main__":
    main()
