# Pulse: agentic monitoring for a simulated marketplace

Hackathon "Eye of the System: Fully Agentic Monitoring" (Almedia track, HackLab Berlin).

- `marketplace/`: simulator of an Amazon-like marketplace. Writes hourly data to `data/eye/`, with the injected
  faults as ground truth in `data/eye/injected.json`. Data docs: `docs/DATA_OVERVIEW.md`.
- `pulse/`: the monitor. Watches 9 CEO KPIs every hour, opens incidents, walks the metric graph to the first cause
  with Claude agents, and briefs the CEO in a web UI. Component map: [`pulse/MAP.md`](pulse/MAP.md).
- `argus/`: metric library used by Pulse (warehouse, metric registry, metric graph, statistics).
- `docs/`: the hackathon brief (`HACKATHON_BRIEF.md`) and the simulator's data guides.

## Setup (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # optional: add ANTHROPIC_API_KEY for live runs
```

## Run
```powershell
# UI on http://127.0.0.1:8765
.\.venv\Scripts\python.exe -m pulse.web --state .pulse       # current state
.\.venv\Scripts\python.exe -m pulse.web --demo               # bundled fixtures (no data needed)
.\.venv\Scripts\python.exe -m pulse.web --demo-live          # animated investigation from the fixtures

# monitoring loop (mock LLM by default)
.\.venv\Scripts\python.exe -m pulse replay --start "2026-11-17 12:00" --end "2026-11-19 03:00"   # demo window
.\.venv\Scripts\python.exe -m pulse tick --next
.\.venv\Scripts\python.exe -m pulse status
.\.venv\Scripts\python.exe -m pulse reset

# checks
.\.venv\Scripts\python.exe -m pytest tests/pulse -q    # 50 tests, incl. a check that this README matches the code
.\.venv\Scripts\python.exe -m pulse.evaluate           # scorecard against the simulator's ground truth
```

## Simulator
```powershell
.\.venv\Scripts\python.exe -m marketplace.run                       # 1-21 Nov 2026, all scenarios -> data/eye/ (~1 min, seeded)
.\.venv\Scripts\python.exe -m marketplace.run --out data/eye_test   # write somewhere else
.\.venv\Scripts\python.exe -m marketplace.user_report --country DE  # funnel and top products from the user panel

# live mode: the marketplace advances one simulated hour per tick, Pulse follows with `pulse watch`
.\.venv\Scripts\python.exe -m marketplace.live start                # quiet history 1-14 Nov, clock at 15 Nov 00:00
.\.venv\Scripts\python.exe -m marketplace.live loop --seconds 4
.\.venv\Scripts\python.exe -m marketplace.live inject payment_failure
.\.venv\Scripts\python.exe -m marketplace.live list                 # all scenarios
```
`marketplace.run` and `marketplace.live` rewrite `data/eye/`. Run `marketplace.run` again to restore the full 3-week
dataset that the demo window and the scorecard use.

The UI's **Refresh data** button checks the next hour of data. In live mode, once Pulse has seen every hour, it first
asks the simulator for a new one (about 30 s), so each click brings fresh data. A static dataset is never rewritten.
