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
.\.venv\Scripts\python.exe -m pulse watch --interval-s 15 --start "2026-11-17 12:00"   # always-on loop, 1 hour per 15 s
.\.venv\Scripts\python.exe -m pulse recheck    # apply pinned / untracked metrics to the last checked hour
.\.venv\Scripts\python.exe -m pulse status
.\.venv\Scripts\python.exe -m pulse reset

# checks
.\.venv\Scripts\python.exe -m pytest tests/pulse -q    # 63 tests, incl. a check that this README matches the code
.\.venv\Scripts\python.exe -m pulse.evaluate           # scorecard against the simulator's ground truth
```

## Always-on loop
`pulse watch` runs the monitoring by itself: every `--interval-s` seconds it checks the next hour of data (on the
recorded data it steps the clock one hour at a time, from `--start` or from the last check), investigates what moved
and writes the briefing, with nobody clicking. A failing check is logged and tried again, and only one loop runs per
state folder. The top bar of the UI shows the loop as it works (**Checking 15:00 now**, **Next check in 12 s**,
**Not watching**).

## Live agents
The agents run in mock mode (scripted, free) unless `PULSE_LLM` is set; `.env.example` lists the settings.
- `live`: real Claude calls. Opus 5.5 writes the briefing, Sonnet 5.5 investigates and adds new metrics. Each agent run
  and the total are capped, and every answer is recorded in `<state>/cassettes/`.
- `replay`: the recorded answers again with the real tools, for free. Anything not recorded falls back to mock.

While an issue stays open the briefing agent can ask for a fresh investigation; it runs at most every 3 checks.
The 40-hour demo window costs about $0.40 live (measured 22 Nov 2026) and nothing on replay. `pulse reset` keeps
the recorded answers, so a live run can be replayed in the same state folder.
```powershell
$env:PULSE_LLM = "live"; .\.venv\Scripts\python.exe -m pulse replay --start "2026-11-17 12:00" --end "2026-11-19 03:00"
$env:PULSE_LLM = "replay"; .\.venv\Scripts\python.exe -m pulse watch --interval-s 15 --start "2026-11-17 12:00"
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
The 3-week dataset in `data/eye/` is committed, so the demo window, the scorecard and the tests run right after
setup. `marketplace.run` and `marketplace.live` rewrite `data/eye/`; run `marketplace.run` again (seeded, ~30 s) to
restore the committed dataset.

The UI's **Refresh data** button checks the next hour of data. In live mode, once Pulse has seen every hour, it first
asks the simulator for a new one (about 30 s), so each click brings fresh data. A static dataset is never rewritten.

## Event console
The Investigations page has an event console for live demos. **Live market** runs a separate simulated market, with
its data in `data/live/` and Pulse's state in `.pulse_live/`, so the recorded run and `data/eye/` are never touched.
- **Start live market**: a quiet history from 1 Nov up to Sunday 15 Nov, 09:00 (about 30 s).
- **Inject**: a problem, a planned change or a data problem, live from the next market hour.
- **+1 h / +3 h / +6 h**: the market runs, then Pulse checks each new hour and investigates what moved (about 15 s for
  3 hours with scripted agents, a minute or two with live ones).
- **Agents**: **Live** (real Claude calls, capped, the cost shows next to it), **Recorded** (answers recorded in an
  earlier live run, free) or **Scripted** (no AI). It starts as the server's `PULSE_LLM`.

While Pulse works, the console shows it as it happens: a strip with the market, the monitor, the investigator agent
and the briefing agent (the one working now is lit), and a log with one block per market hour: one line for what the
monitor saw change, each investigation's conclusion with its main steps, and the briefing. The investigation below
follows the agent: its graph highlights the metrics that matter (a running investigation adds them as it goes). Each injected
scenario gets a card with what really changed (Pulse is not told) and what Pulse found: when the alert came, whether
the cause and the place were named, or, for a planned change or a data problem, that no alert was raised. On the
briefing, the metrics and issues that changed at the last check are marked.

**Recorded run** switches the whole UI back to the state the server was started with. While the live market is shown,
the briefing's **Refresh data** button runs the market one hour.

## Pin and untrack metrics
The CEO decides which metrics are on the briefing.
- **Pin**: click a metric on the Investigations graph or the Metric graph, then **Pin to the briefing**. It is then
  monitored like the 9 CEO KPIs: its own row with the 48-hour chart, seasonal normal range, alerts, issues and
  investigations. The row appears in about 15 seconds, for the hour already checked.
- **Untrack**: open a row on the briefing and click **Untrack**. The metric becomes a background metric: no alerts or
  issues of its own (an open issue is closed as no longer tracked), but it stays in the graph and investigations of
  the other metrics still look at it. **Restore** in the background metrics line under the table brings it back.

The choices are saved in `choices.json` at the repo root (not committed). The recorded run and the live market use the
same choices, and they survive restarts and `pulse reset`. The scorecard (`pulse.evaluate`) ignores them.

## License
MIT, see [LICENSE](LICENSE).
