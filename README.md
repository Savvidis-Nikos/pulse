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
.\.venv\Scripts\python.exe -m pytest tests/pulse -q    # 53 tests, incl. a check that this README matches the code
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

## Event console
The Investigations page has an event console for live demos. **Live market** runs a separate simulated market, with
its data in `data/live/` and Pulse's state in `.pulse_live/`, so the recorded run and `data/eye/` are never touched.
- **Start live market**: a quiet history from 1 Nov up to Sunday 15 Nov, 09:00 (about 30 s).
- **Inject**: a problem, a planned change or a data problem, live from the next market hour.
- **+1 h / +3 h / +6 h**: the market runs, then Pulse checks each new hour and investigates what moved (about 15 s for
  3 hours). Every step appears in the console, with a link to each finished investigation.

**Recorded run** switches the whole UI back to the state the server was started with. While the live market is shown,
the briefing's **Refresh data** button runs the market one hour.

## License
MIT, see [LICENSE](LICENSE).
