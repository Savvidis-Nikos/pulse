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
2. `monitor.evaluate_ex` -> KpiStatus per KPI in `pulse/kpis.yaml` (9 KPIs; seasonal baseline, robust z,
   polarity-aware severity, completeness -> data_issue, onset; watch for 6 h in a row -> alert (slow drift);
   planned event overlapping the window -> watch if: KPI listed + inside its expected range, or healthy net outcome,
   or the event moves an input upstream of the KPI in the graph and started with the move; a segment-scoped event
   (FR campaign) is NOT accepted when another segment is just as far off -> competing cause stays an alert)
3. `incidents.IncidentBook.update` -> open / persisting / escalated / resolved (no re-reporting)
4. investigate (new, escalated, every 6 ticks, follow_up; max 3/tick) -> `interpreter.interpret(w, kpi, incident_id=)`
5. on resolve -> `memory.remember` (case written to memory.jsonl); on investigate -> `memory.recall` (hypotheses only)
6. brief: ONE orchestrator LLM call only on material change, else `brief.template_brief` (no LLM)

## Modules (lines) -> public API
| file | role | API |
|---|---|---|
| contracts.py (253) | pydantic models + state layout + io | Node Edge GraphSnapshot GraphChange KpiStatus TickState Incident TraceStep Finding Interpretation Brief; save/load/save_list/load_list/append_jsonl |
| llm.py (306) | ONLY Claude gateway, cost caps, cassettes; any live failure -> logged + caller's mock policy | `run_agent(role, system, prompt, tools, schema, mock=, max_turns=, on_call=)`, Tool, obj, mode, total_spend, load_dotenv |
| world.py (22) | cached argus World | `world(reload=False)` -> w.wh w.reg w.engine w.graph w.now w.set_now |
| monitor.py (420) | statistics | load_kpis, classify, find_onset, planned_events, evaluate_ex, evaluate |
| incidents.py (157) | incident lifecycle | IncidentBook(load/save/update/active/get), IncidentEvent |
| memory.py (77) | persistent case memory | cases, remember, recall, describe |
| brief.py (147) | template brief + markdown | template_brief, kpi_sentence, render_markdown |
| orchestrator.py (501) | the harness | tick, status_text, OrchMemory (orchestrator.json) |
| graph_api.py (1235) | read-only annotated graph | build_snapshot, node_detail, breakdown, neighbors, search_data, validate_edge, record_change, load_changes, try_formula, stats, attribution, localize, changes_near, ensure_applied, apply_change |
| graph_handler.py (731) | Graph Handler agent (Sonnet) | ingest_metric(w, raw), discover(w, around), try_definition |
| interpreter.py (1318) | Graph Interpreter agent (Sonnet) | interpret(w, kpi, incident_id=, depth=2, window_h=3, on_step=, onset=) |
| evaluate.py (106) | scorecard vs simulator ground truth (throwaway state) | `python -m pulse.evaluate [--start --end --json]` |
| dashboard/layout.py, encoding.py | pure helpers used by the web UI | solar_layout; node_style, edge_style, LEGEND, trace_frames, style_snapshot |
| dashboard/make_fixtures.py | ONLY fixture source -> pulse/fixtures/state_demo | write_all, simulate |
| web/server.py + web/static/js/{app,orrery,format,spark,api}.js | the ONLY demo UI (stdlib server; reads state, one write: POST /api/refresh runs `pulse tick --next`, preceded by `marketplace.live tick --hours 1` when data/eye/live.json exists and Pulse is at its last hour; off with --demo; briefing shows the tick log through app.js `activity()`, a plain-English rewrite of the orchestrator's log lines) | serve; pages Briefing / Investigations / Metric graph |

Design rules (keep): statistics decide *whether*, the graph decides *where*, the LLM chooses next step and
narrates; code (not the LLM) accepts/rejects graph edges; every LLM agent has a deterministic mock policy;
UI reads only state files.

## State dir (`$PULSE_STATE`, default `.pulse/`, git-ignored)
state.json, incidents.json, incidents_meta.json, orchestrator.json, brief.json, memory.jsonl,
snapshots/<kpi>-<center>-<YYYYmmddHH>.json, interpretations/<incident_id>.json (+ .partial.json while running),
graph_changes.jsonl (re-applied on every load = graph survives restarts), inbox/, spend.json, llm_log.jsonl, cassettes/.

## Run
- loop: `.venv/Scripts/python.exe -m pulse tick [--at TS | --next] | replay --start TS --end TS | watch | status | spend | reset [--all]`
- demo window: `replay --start "2026-11-17 12:00" --end "2026-11-19 03:00"` (payment fault DE/android 11-17 15:00,
  resolves 11-18 05:00; inventory data_issue 11-18 00-07; FR campaign = innocent from 11-18 18:00).
  Data-issue window: 11-21 20:00-23:00. Drop `pulse/inbox_examples/*` into the inbox for the new-metric moment.
- UI: `.venv/Scripts/python.exe -m pulse.web --state .pulse | --demo | --demo-live` -> http://127.0.0.1:8765
- tests: `.venv/Scripts/python.exe -m pytest tests/pulse -q` (47 pass, ~30 s, mostly world load; the live LLM path is
  covered offline by tests/pulse/test_llm_live_stub.py with a fake client returning real SDK message objects)
- scorecard: `.venv/Scripts/python.exe -m pulse.evaluate` (~90 s, 11-08..11-21, mock): faults detected 8/14, root-cause
  change named 6/11, segment named 6/12, innocent changes alerted 0/6, data issues seen 2/2, false alarms 0
- env: PULSE_LLM=mock(default)|replay|live, PULSE_RUN_CAP_USD=0.60, PULSE_TOTAL_CAP_USD=12, PULSE_MODEL_<ROLE>,
  PULSE_INBOX, PULSE_DATA, PULSE_REINVESTIGATE_EVERY, PULSE_MAX_INVESTIGATIONS. Key in `.env` (ANTHROPIC_API_KEY).

## Claims: proven vs not (do not oversell, do not build beyond this without asking the user)
Proven offline (mock, real data): autonomous loop; seasonality/completeness/planned events; no re-reporting;
first cause vs symptoms with supported/hypothesis; new metric placed + validated (rejects, ambiguous);
graph changes, incidents and orchestrator follow-ups persist across restarts; cost caps; replay cassettes.
Also proven (scorecard): DE search fault during the FR campaign is caught (competing segment) and attributed to
search-ranker 2.3; stockout escalates by slow-drift rule (12 h) and is attributed to the delayed Voltix delivery.
NOT yet proven: any LIVE LLM run (request shape verified offline against SDK 2.12 types only); 15-min cadence is
pitch (data is hourly); faults that only move one segment and never the total KPI are missed (6/14: margin squeeze
NL, PSP outage NL 2 h, grocery feed drift, price increase, competitor promo, Safari checkout) - would need
segment-level detection; click fraud is pinned on a decoy release (hypothesis, not supported); "self-improvement"
= graph extension + case memory + offline scorecard loop (no learned thresholds).
