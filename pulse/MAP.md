# Pulse: component map (read this first, do not re-read the codebase)

Hackathon "Eye of the System: Fully Agentic Monitoring". Judging: 40% metric graph + automatic extension,
40% autonomous monitoring & investigation, 20% demo (fault + innocent change + new metric, live).
Pulse = new package. `argus/` is used ONLY as a library (warehouse, registry, metric engine, MetricGraph,
baseline/trust); since the move to its own folder (2026-11-22) it holds only the modules Pulse imports
(analytics, semantic, data, config, core, settings). `argus.semantic.extend.claude_adjudicate` (old agents) is
not shipped and must not be called. `argus` reads 4 learned-graph files from `.argus/` (added_metrics.yaml,
aliases.json, learned_identities.json, learned_causes.json): the graph and the scorecard below depend on them.
Simulator: `marketplace/` writes `data/eye/*.csv` (ground truth of injected faults: `data/eye/injected.json`).
Never run `python -m marketplace.*` while others work: it rewrites `data/eye/`.

## One tick (`pulse/orchestrator.py: tick(now=None, *, w=None, kpis=None) -> TickState`)
1. inbox: new files in `$PULSE_INBOX` (default `<state>/inbox`) -> `graph_handler.ingest_metric` -> moved to `processed/`
2. `monitor.evaluate_ex` -> KpiStatus per headline KPI (`monitor.headline_kpis`: the 9 KPIs in `pulse/kpis.yaml` plus
   the CEO's pinned metrics minus the untracked ones, `pulse/choices.py`; seasonal baseline, robust z,
   polarity-aware severity, completeness -> data_issue, onset; watch for 6 h in a row -> alert (slow drift);
   planned event overlapping the window -> watch if: KPI listed + inside its expected range, or healthy net outcome,
   or the event moves an input upstream of the KPI in the graph and started with the move; a segment-scoped event
   (FR campaign) is NOT accepted when another segment is just as far off -> competing cause stays an alert)
3. `incidents.IncidentBook.update` -> open / persisting / escalated / resolved (no re-reporting); a metric the CEO
   untracked -> its open incidents closed as "no longer tracked" (Incident.closed_reason, nothing written to memory)
4. investigate (new, escalated, every 6 ticks, follow_up held until the last investigation is PULSE_FOLLOW_UP_GAP=3
   ticks old; max 3/tick) -> `interpreter.interpret(w, kpi, incident_id=)`
5. on resolve -> `memory.remember` (case written to memory.jsonl); on investigate -> `memory.recall` (hypotheses only)
6. brief: ONE orchestrator LLM call only on material change, else `brief.template_brief` (no LLM); the agent's
   headline is kept on later ticks only while every KPI keeps its severity

## Modules (lines) -> public API
| file | role | API |
|---|---|---|
| choices.py (71) | the CEO's choices, one store for every run (`choices.json` at the repo root or `$PULSE_CHOICES`, git-ignored, survives `reset`); ignored by evaluate | load, save, apply(pin/untrack/restore, metric), kpi_labels, METRIC_ID |
| contracts.py (256) | pydantic models + state layout + io | Node Edge GraphSnapshot GraphChange KpiStatus TickState Incident TraceStep Finding Interpretation Brief; save/load/save_list/load_list/append_jsonl |
| llm.py (306) | ONLY Claude gateway, cost caps, cassettes; any live failure -> logged + caller's mock policy | `run_agent(role, system, prompt, tools, schema, mock=, max_turns=, on_call=)`, Tool, obj, mode, total_spend, load_dotenv |
| world.py (22) | cached argus World | `world(reload=False)` -> w.wh w.reg w.engine w.graph w.now w.set_now |
| monitor.py (610) | statistics | load_kpis, headline_kpis (kpis.yaml + choices), pinned_kpi, classify, find_onset, planned_events, evaluate_ex, evaluate |
| incidents.py (244) | incident lifecycle | IncidentBook(load/save/update/active/get/close_untracked), IncidentEvent |
| memory.py (77) | persistent case memory | cases, remember, recall, describe |
| brief.py (147) | template brief + markdown | template_brief, kpi_sentence, render_markdown |
| orchestrator.py (641) | the harness; the brief prompt lists pinned / untracked metrics | tick (kpis=None -> headline_kpis; explicit kpis -> choices ignored), recheck (apply new choices to the last checked hour: no clock move, no LLM; an alerting pinned metric opens its incident, investigated next tick), status_text, OrchMemory (orchestrator.json) |
| graph_api.py (1235) | read-only annotated graph | build_snapshot, node_detail, breakdown, neighbors, search_data, validate_edge, record_change, load_changes, try_formula, stats, attribution, localize, changes_near, ensure_applied, apply_change |
| graph_handler.py (731) | Graph Handler agent (Sonnet) | ingest_metric(w, raw), discover(w, around), try_definition |
| interpreter.py (1318) | Graph Interpreter agent (Sonnet) | interpret(w, kpi, incident_id=, depth=2, window_h=3, on_step=, onset=) |
| evaluate.py (108) | scorecard vs simulator ground truth (throwaway state) | `python -m pulse.evaluate [--start --end --json]` |
| dashboard/layout.py, encoding.py | pure helpers used by the web UI | solar_layout; node_style, edge_style, LEGEND, trace_frames, style_snapshot |
| dashboard/make_fixtures.py | ONLY fixture source -> pulse/fixtures/state_demo | write_all, simulate |
| web/server.py + web/static/js/{app,orrery,format,spark,api}.js | the ONLY demo UI (stdlib server; reads state, writes: POST /api/refresh runs `pulse tick --next`, preceded by `marketplace.live tick --hours 1` when data/eye/live.json exists and Pulse is at its last hour; POST /api/choices {action: pin/untrack/restore, metric} saves the choice and runs `pulse recheck` on the state on screen (GET /api/overview `choices`: pinned, untracked, labels; `watch` = watch.json heartbeat, `server_time`); both off (403) with --demo; pin bar on graph pages = app.js `enablePinning()`, Untrack in a briefing row's detail, Restore in the background metrics line; the event console shows the check log through app.js `activity()`, a plain-English rewrite of the orchestrator's log lines; the Metric graph page's legend lists only the tokens the current snapshot draws) | serve; pages Briefing / Investigations / Metric graph |
| web/console.py | event console on the Investigations page: a live simulated market in data/live/ with its own state .pulse_live/ (never data/eye/ or the recorded run); one job at a time in a worker thread; simulator kept in memory, Pulse via `pulse replay` subprocess with PULSE_LLM = the console's agents mode (live/replay/mock, starts as the server's) and PULSE_STREAM_LOG=1 (orchestrator `_Log` prints each decision "      - " and agent progress "      ~ " as it happens, incl. every investigator step via interpret on_step; parsed into kinds checking/step/note/check); log in .pulse_live/console.jsonl; CATALOG = offered scenarios; after each inject/run every injected scenario is scored with evaluate.score against marketplace.live.build's truth (status `results`, `session.injected[]` carries the truth). UI (app.js): pipeline strip (`conPipeline`), log grouped per market hour (`conGroups`: the monitor's decisions in one line, `monitorSummary`; one line per investigation + its logical steps, `logicalSteps`), scenario cards (`conScenario`), `followAgents` opens the investigation the agent works on after +N h (running = grows live; the investigation graph has no step player or legend, it shows the last frame with only the highlighted metrics labelled); briefing marks what the last check changed (`changesAt`) | Console(root): status, act({action: source/agents/start/inject/run}), run_sync, state_dir; GET/POST /api/console |

Design rules (keep): statistics decide *whether*, the graph decides *where*, the LLM chooses next step and
narrates; code (not the LLM) accepts/rejects graph edges; every LLM agent has a deterministic mock policy;
UI reads only state files.

## State dir (`$PULSE_STATE`, default `.pulse/`, git-ignored)
state.json, incidents.json, incidents_meta.json, orchestrator.json, brief.json, memory.jsonl, watch.json (heartbeat of
`pulse watch`: pid, checking, next_check, errors, last_error, stopped, recent checks),
snapshots/<kpi>-<center>-<YYYYmmddHH>.json, interpretations/<incident_id>.json (+ .partial.json while running),
graph_changes.jsonl (re-applied on every load = graph survives restarts), inbox/, spend.json, llm_log.jsonl, cassettes/.

## Run
- loop: `.venv/Scripts/python.exe -m pulse tick [--at TS | --next] | replay --start TS --end TS | watch [--interval-s S]
  [--start TS] | recheck | status | spend | reset [--all]`; watch = the always-on loop (a failing check is logged and
  retried with back-off, one watcher per state dir, stops at the end of the data); the UI top bar shows it live
  (app.js `loopState`). Briefing = status counts, the agent's short summary, the metrics table and
  an Issues card; a row's detail = the problem in 1-2 sentences (`gist`) + one conclusion line (`kpiConclusion`,
  overview interpretations[].cause = first_cause.node)
- demo window: `replay --start "2026-11-17 12:00" --end "2026-11-19 03:00"` (payment fault DE/android 11-17 15:00,
  resolves 11-18 05:00; inventory data_issue 11-18 00-07; FR campaign = innocent from 11-18 18:00).
  Data-issue window: 11-21 20:00-23:00. Drop `pulse/inbox_examples/*` into the inbox for the new-metric moment.
- UI: `.venv/Scripts/python.exe -m pulse.web --state .pulse | --demo | --demo-live` -> http://127.0.0.1:8765
- tests: `.venv/Scripts/python.exe -m pytest tests/pulse -q` (63 pass, ~40 s, mostly world load; the live LLM path is
  covered offline by tests/pulse/test_llm_live_stub.py with a fake client returning real SDK message objects)
- scorecard: `.venv/Scripts/python.exe -m pulse.evaluate` (~90 s, 11-08..11-21, mock): faults detected 8/14, root-cause
  change named 6/11, segment named 6/12, innocent changes alerted 0/6, data issues seen 2/2, false alarms 0
- env: PULSE_LLM=mock(default)|replay|live, PULSE_RUN_CAP_USD=0.60, PULSE_TOTAL_CAP_USD=12, PULSE_MODEL_<ROLE>,
  PULSE_INBOX, PULSE_DATA, PULSE_CHOICES, PULSE_REINVESTIGATE_EVERY, PULSE_MAX_INVESTIGATIONS, PULSE_FOLLOW_UP_GAP, PULSE_STREAM_LOG (set by the console). Key in `.env` (ANTHROPIC_API_KEY).

## Claims: proven vs not (do not oversell, do not build beyond this without asking the user)
Proven offline (mock, real data): autonomous loop; seasonality/completeness/planned events; no re-reporting;
first cause vs symptoms with supported/hypothesis; new metric placed + validated (rejects, ambiguous);
graph changes, incidents and orchestrator follow-ups persist across restarts; cost caps; replay cassettes.
Also proven (scorecard): DE search fault during the FR campaign is caught (competing segment) and attributed to
search-ranker 2.3; stockout escalates by slow-drift rule (12 h) and is attributed to the delayed Voltix delivery.
Proven LIVE (22 Nov 2026, real API): all three agents; demo window 40 ticks ~$0.40 (Opus briefs are the main cost),
same story as mock, live interpreter names GiroLokal / Android / DE and rates it hypothesis; replay of the recorded
cassettes in a fresh state dir reproduces it for $0.
NOT yet proven: 15-min cadence is pitch (data is hourly); faults that only move one segment and never the total KPI
are missed (6/14: margin squeeze NL, PSP outage NL 2 h, grocery feed drift, price increase, competitor promo, Safari checkout) - would need
segment-level detection; click fraud is pinned on a decoy release (hypothesis, not supported); "self-improvement"
= graph extension + case memory + offline scorecard loop (no learned thresholds).
