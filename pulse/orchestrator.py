"""Orchestrator: the Pulse loop. One `tick()` = one new data hour (production cadence: every 15 minutes).

    evaluate KPIs (monitor, statistics)  ->  update incident memory  ->  decide what to investigate
    -> Graph Interpreter agent per chosen incident (pulse.interpreter, Sonnet)  ->  CEO brief (Opus, one call)

Cost discipline: the Opus brief runs at most once per tick and only when something changed (new / escalated /
resolved incident, a KPI changed severity, a fresh investigation). All-ok ticks get a template brief; unchanged
non-ok ticks keep the last brief. Investigations: new incident, escalation, every K ticks while persisting,
or a follow-up the previous brief asked for; at most PULSE_MAX_INVESTIGATIONS per tick, only new incidents when
the LLM budget is nearly spent. Every decision is written to TickState.log in plain English.

The KPI set is kpis.yaml adjusted by the CEO's choices (pulse.choices): pinned metrics are monitored like the KPIs,
untracked ones are background metrics and their open incidents are closed as no longer tracked. `recheck()` applies a
new choice to the last checked hour at once, without moving the clock.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

from pulse import choices
from pulse import contracts as C
from pulse import llm
from pulse.brief import fmt_value, template_brief
from pulse.contracts import Brief, BriefItem, Incident, Interpretation, TickState
from pulse.incidents import IncidentBook, key as inc_key
from pulse.monitor import (SEG_BUDGET, Evaluation, evaluate_ex, evaluate_segments, headline_kpis, load_kpis,
                           segment_key)

REINVESTIGATE_EVERY = int(os.environ.get("PULSE_REINVESTIGATE_EVERY", "6"))
MAX_INVESTIGATIONS = int(os.environ.get("PULSE_MAX_INVESTIGATIONS", "3"))
PRIORITY = {"new incident": 0, "escalated": 1, "follow-up requested by the last brief": 2, "periodic re-check": 3,
            "not investigated yet": 3}


class OrchMemory(BaseModel):
    """Orchestrator memory between ticks (<state>/orchestrator.json). Local to Pulse's harness."""
    tick: int = 0
    last_ts: str | None = None
    follow_up: list[str] = Field(default_factory=list)       # KPIs the last brief asked to re-investigate
    watch_nodes: list[str] = Field(default_factory=list)     # nodes the last brief/interpretations flagged
    last_investigated: dict[str, int] = Field(default_factory=dict)   # incident id -> tick
    last_severity: dict[str, str] = Field(default_factory=dict)
    base_headline: dict[str, str] = Field(default_factory=dict)       # incident id -> monitor headline
    announced: list[str] = Field(default_factory=list)                # planned events already briefed as innocent
    segments: dict[str, dict] = Field(default_factory=dict)           # segment cell -> {"checked": ts, "trouble": z}
    llm_briefs: int = 0
    template_briefs: int = 0
    investigations: int = 0


def _load_mem() -> OrchMemory:
    return C.load(OrchMemory, "orchestrator.json") or OrchMemory()


def _interp_for(inc: Incident) -> Interpretation | None:
    for rel in (f"interpretations/{inc.id}.json",
                f"interpretations/{inc.interpretation_id}.json" if inc.interpretation_id else None):
        if rel:
            try:
                it = C.load(Interpretation, rel)
            except Exception:
                it = None
            if it is not None:
                return it
    return None


def _fmt_status(e: Evaluation) -> str:
    st = e.status
    z = f"z {st.z:+.1f}" if st.z is not None else "z n/a"
    dev = f"{st.deviation_pct:+.1f}%" if st.deviation_pct is not None else "n/a"
    return f"{segment_key(st.name, e.segment)} {dev} vs expected ({z})"


def _localized(inc: Incident) -> dict:
    """Segment the incident's interpretation localised ('m|country=DE|platform=android' on its path), else {}."""
    it = _interp_for(inc)
    seg = {}
    for p in (it.path if it else []):
        for part in p.split("|")[1:]:
            k, _, v = part.partition("=")
            if v:
                seg[k] = v
    return seg


def _budget_tight() -> bool:
    if llm.mode() != "live":
        return False
    cap = float(os.environ.get("PULSE_TOTAL_CAP_USD", "12.0"))
    return llm.total_spend() >= 0.8 * cap


def _choice_log(before: set[str], after: list[str], closed: list[Incident]) -> list[str]:
    """Plain-English log lines for metrics the CEO pinned or untracked since the last check."""
    shut: dict[str, list[str]] = {}
    for i in closed:
        shut.setdefault(i.kpi, []).append(i.id)
    base = choices.kpi_labels()
    lines = [f"{m} restored: monitored again as a headline metric" if m in base else
             f"{m} pinned: now monitored like the headline metrics" for m in after if before and m not in before]
    lines += [f"{m} untracked: watched in the background only"
              + (f", {', '.join(shut[m])} closed as no longer tracked" if m in shut else "")
              for m in sorted((before - set(after)) | set(shut))]
    return lines


def _events_at(book: IncidentBook, ts: str) -> dict[str, tuple[str, Incident]]:
    """Incident events of data hour `ts` read back from the incident book (KPI totals only): kpi -> (event, incident)."""
    out: dict[str, tuple[str, Incident]] = {}
    for inc in book.incidents:
        if inc.segment:
            continue
        if inc.opened_at == ts and inc.state != "resolved":
            out[inc.kpi] = ("opened", inc)
        elif inc.state == "resolved" and inc.resolved_at == ts and not inc.closed_reason:
            out[inc.kpi] = ("resolved", inc)
        elif inc.state != "resolved" and any(h.startswith(f"{ts} ESCALATED") for h in inc.history):
            out[inc.kpi] = ("escalated", inc)
    return out


# ---------------------------------------------------------------- investigations

def _investigate(w, inc: Incident, reason: str, mem: OrchMemory, book: IncidentBook, ts: str,
                 log: list[str]) -> Interpretation | None:
    try:
        from pulse.interpreter import interpret
    except Exception as e:
        log.append(f"interpreter unavailable ({type(e).__name__}: {e}) - {inc.id} stays uninvestigated")
        return None
    t0 = time.time()
    try:
        it = interpret(w, inc.kpi, incident_id=inc.id, onset=inc.onset_ts,
                       **({"segment": inc.segment} if inc.segment else {}))
    except Exception as e:
        log.append(f"interpreter failed on {inc.id} ({type(e).__name__}: {str(e)[:160]}) - continuing")
        return None
    mem.last_investigated[inc.id] = mem.tick
    mem.investigations += 1
    if not isinstance(it, Interpretation):
        try:
            it = Interpretation.model_validate(it)
        except Exception:
            log.append(f"interpreter returned {type(it).__name__}, not an Interpretation - ignored")
            return None
    base = mem.base_headline.setdefault(inc.id, inc.headline)
    inc.interpretation_id = it.interpretation_id
    inc.snapshot_id = it.snapshot_id
    if it.headline:
        inc.headline = f"{base} - {it.headline.rstrip('.')}"
    path = " -> ".join(it.path) if it.path else "n/a"
    inc.history.append(f"{ts} investigated ({reason}): {it.headline} [{it.confidence}; path {path}]")
    try:   # persistent memory: similar incidents seen before (shown as hypotheses, never used as evidence)
        from pulse import memory
        seen = memory.recall(inc.kpi, inc.direction, it.path, it.first_cause.node if it.first_cause else None,
                             before=ts, exclude=inc.id)
        if seen:
            inc.recalled = [memory.describe(c) for c in seen]
            inc.history.append(f"{ts} recalled from memory: " + "; ".join(c["id"] for c in seen))
    except Exception:
        pass
    log.append(f"interpreter on {inc.id} ({reason}, {time.time() - t0:.1f}s, {it.llm_mode}): "
               f"\"{it.headline}\" - {it.confidence}, path {path}")
    return it


# ---------------------------------------------------------------- new metrics (inbox -> graph handler)

def _ingest_inbox(w, log: list[str]) -> list[str]:
    """Feed each new file in the inbox to the Graph Handler, then move it to processed/. Returns brief notes."""
    inbox = Path(os.environ.get("PULSE_INBOX") or C.STATE_DIR / "inbox")
    files = sorted(f for f in inbox.glob("*") if f.is_file()) if inbox.is_dir() else []
    notes: list[str] = []
    for f in files:
        try:
            from pulse import graph_api
            from pulse.graph_handler import ingest_metric
            changes = ingest_metric(w, f.read_text(encoding="utf-8"))
        except Exception as e:
            changes = None
            log.append(f"Graph Handler failed on inbox/{f.name} ({type(e).__name__}: {str(e)[:160]})")
        (inbox / "processed").mkdir(exist_ok=True)
        f.replace(inbox / "processed" / f.name)
        if changes is None:
            continue
        parts = []
        for ch in changes:
            if ch.action == "add_node" and ch.node is not None:
                n = ch.node
                sid = ""
                try:
                    snap = graph_api.build_snapshot(w, kpi=n.id, center=n.id)
                    C.save(snap, f"snapshots/{snap.snapshot_id}.json")
                    sid = f", snapshot {snap.snapshot_id}"
                except Exception as e:
                    sid = f", no snapshot ({type(e).__name__})"
                nf = sum(1 for c in changes if c.action == "add_edge" and c.edge is not None and c.edge.kind == "formula"
                         and c.edge.target == n.id)
                fs = f" {n.formula_status}" if n.formula_status else ""
                parts.append(f"added {n.id} = {n.definition or '?'} ({nf} formula edges{fs}{sid})")
                notes.append(f"{n.label or n.id} = {n.definition or '?'}")
            elif ch.action == "reject_edge" and ch.edge is not None:
                parts.append(f"rejected edge {ch.edge.source} -> {ch.edge.target} ({ch.reason[:60]})")
        n_cause = sum(1 for c in changes if c.action == "add_edge" and c.edge is not None and c.edge.kind != "formula")
        n_amb = sum(1 for c in changes if c.action == "mark_ambiguous")
        log.append(f"Graph Handler on inbox/{f.name}: " + ("; ".join(parts) or "no node added")
                   + f"; {n_cause} cause edges, {n_amb} ambiguous")
    return [f"New metric{'s' if len(notes) > 1 else ''} incorporated into the graph: {'; '.join(notes)}."] if notes else []


# ---------------------------------------------------------------- brief agent

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "<= 15 words, the one thing the CEO must know now"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kpi": {"type": "string"}, "label": {"type": "string"},
                "severity": {"type": "string", "enum": ["ok", "watch", "alert", "data_issue"]},
                "sentence": {"type": "string", "description": "ONE short sentence (under 20 words): what the move means"},
                "detail": {"type": "string", "description": "why, and what is being done: at most two short sentences, "
                                                            "or an empty string"},
                "incident_id": {"type": "string", "description": "incident id or empty string"},
            },
            "required": ["kpi", "label", "severity", "sentence", "detail", "incident_id"],
            "additionalProperties": False}},
        "text": {"type": "string", "description": "the full briefing, 3-8 short lines, plain English"},
        "follow_up": {"type": "object", "properties": {
            "reinvestigate": {"type": "array", "items": {"type": "string"},
                              "description": "KPI ids to send to the graph interpreter again next tick"},
            "watch_nodes": {"type": "array", "items": {"type": "string"},
                            "description": "metric ids to keep an eye on"}},
            "required": ["reinvestigate", "watch_nodes"], "additionalProperties": False},
    },
    "required": ["headline", "items", "text", "follow_up"],
    "additionalProperties": False,
}

BRIEF_SYSTEM = """You are the orchestrator of Pulse, an autonomous monitoring system for an online marketplace.
Every tick you receive the CEO KPIs (value vs seasonal expectation, robust z, severity), the incident memory and
the graph interpreter's conclusions. Write the CEO briefing:
- Lead with what changed since the last briefing: new or worse problems first, then resolutions.
  Do not re-announce ongoing incidents as new; mention them in one short line.
- Separate first causes from symptoms using the interpreter's path; say how confident it is.
- "watch" moves explained by a planned event are innocent: say so in a few words, no action needed.
- "data_issue" means the data is incomplete: say we are holding judgement, never call it a business drop.
- Use only numbers from the input or your tools. One short sentence per KPI (its numbers are shown next to it,
  so say what the move means); put the why and what is being done in that KPI's detail.
- Plain English for a CEO: metric names as words (product views, not product_views), country codes spelled out
  (France, not FR), no incident ids, no jargon or acronyms.
- follow_up.reinvestigate: KPIs whose cause is still unexplained or whose picture changed (keep it short);
  follow_up.watch_nodes: metrics worth watching next tick.
Return only the JSON object."""


def _situation(ts: str, evals: list[Evaluation], book: IncidentBook, events: dict[str, str],
               interps: dict[str, Interpretation], prev: Brief | None) -> str:
    lines = [f"Data clock: {ts} (one tick = one new hour of data).", "", "KPIs:"]
    pinned = [e.name for e in evals if e.kpi.pinned]
    untracked = [m for m in choices.load()["untracked"] if m not in {e.name for e in evals}]
    for e in evals:
        st = e.status
        lines.append(f"- {st.name} \"{st.label}\": {fmt_value(st.value, st.unit)} vs {fmt_value(st.expected, st.unit)} "
                     f"expected ({st.deviation_pct if st.deviation_pct is not None else 'n/a'}%, z {st.z}), "
                     f"severity {st.severity}" + (f", out of band since {e.onset}" if e.onset else "")
                     + (f", incident {st.incident_id}" if st.incident_id else "")
                     + (f", event this tick: {events[st.name]}" if st.name in events else "")
                     + (f". Notes: {'; '.join(e.notes)}" if e.notes else ""))
    if pinned:
        lines.append(f"Pinned by the CEO (monitored like the others): {', '.join(pinned)}.")
    if untracked:
        lines.append(f"Untracked by the CEO (background metrics, no own alerts; mention them only as a cause or "
                     f"knock-on effect): {', '.join(untracked)}.")
    act = book.active()
    lines += ["", "Active incidents:" if act else "Active incidents: none"]
    for inc in act:
        it = interps.get(inc_key(inc))
        lines.append(f"- {inc.id} ({inc.state}, {inc.ticks_seen} ticks, opened {inc.opened_at}): {inc.headline}"
                     + (f" | interpreter: {it.headline} [{it.confidence}]" if it else " | not investigated yet"))
    res = [i for i in book.incidents if i.state == "resolved" and events.get(inc_key(i)) == "resolved"]
    for inc in res:
        lines.append(f"- RESOLVED this tick: {inc.id}: {inc.headline}")
    if prev is not None:
        lines += ["", f"Previous briefing ({prev.ts}): {prev.headline}"]
    return "\n".join(lines)


def _brief_tools(evals: list[Evaluation], book: IncidentBook) -> list[llm.Tool]:
    by = {e.name: e for e in evals}

    def get_interpretation(incident_id: str):
        inc = book.get(incident_id)
        it = _interp_for(inc) if inc else None
        if it is None:
            return f"no interpretation for {incident_id}"
        d = it.model_dump(include={"headline", "summary", "path", "first_cause", "confidence", "symptoms",
                                   "missing_data", "ruled_out", "uncertainty", "watch_next"})
        return d

    def kpi_history(kpi: str, hours: int = 24):
        e = by.get(kpi)
        if e is None:
            return f"unknown KPI {kpi}; known: {sorted(by)}"
        pts = e.status.series[-max(1, min(int(hours), 48)):]
        return [{"ts": p.ts, "value": p.value, "expected": p.expected} for p in pts]

    def get_incident(incident_id: str):
        inc = book.get(incident_id)
        return inc.model_dump() if inc else f"unknown incident {incident_id}"

    return [
        llm.Tool("get_interpretation", "The graph interpreter's full conclusion for an incident (cause path, "
                 "first cause, symptoms, ruled out, uncertainty).", llm.obj({"incident_id": {"type": "string"}}),
                 get_interpretation),
        llm.Tool("kpi_history", "Hourly value vs expected for one KPI over the last N hours (max 48).",
                 llm.obj({"kpi": {"type": "string"}, "hours": {"type": "integer"}}), kpi_history),
        llm.Tool("get_incident", "Full incident record including its tick-by-tick history.",
                 llm.obj({"incident_id": {"type": "string"}}), get_incident),
    ]


def _mock_brief_policy(template: Brief, book: IncidentBook, interps: dict[str, Interpretation],
                       fresh: set[str], investigated_now: set[str]):
    """Deterministic stand-in for Opus: reads the fresh interpretations via the tool, then returns the template
    brief plus follow-ups (unexplained causes, interpreter's watch_next)."""
    def policy(calls):
        if not calls and fresh:
            return {"tools": [("get_interpretation", {"incident_id": i}) for i in sorted(fresh)]}
        re = sorted({inc.kpi for inc in book.active()
                     if inc.id not in investigated_now and inc_key(inc) in interps
                     and interps[inc_key(inc)].confidence == "unexplained"})
        watch = sorted({n for it in interps.values() for n in it.watch_next})[:8]
        out = template.model_dump(include={"headline", "text"})
        out["items"] = [{**it.model_dump(include={"kpi", "label", "severity", "sentence", "detail"}),
                         "incident_id": it.incident_id or ""} for it in template.items]
        out["follow_up"] = {"reinvestigate": re, "watch_nodes": watch}
        return {"final": out}
    return policy


# ---------------------------------------------------------------- tick

def tick(now=None, *, w=None, kpis=None) -> TickState:
    """Run one monitoring tick at data hour `now` (default: the world's current clock = latest data).
    kpis: the metrics to check; default = kpis.yaml adjusted by the CEO's choices (pinned in, untracked out)."""
    if w is None:
        from pulse.world import world
        w = world()
    chosen = not kpis
    kpis = kpis or headline_kpis(w)
    mem = _load_mem()
    t_start = time.time()
    if now is not None:
        w.set_now(now)
    now = w.now
    ts = f"{now:%Y-%m-%d %H:%M}"
    same_hour = mem.last_ts == ts
    if not same_hour:
        mem.tick += 1
    log: list[str] = []
    if mem.last_ts and pd.Timestamp(ts) < pd.Timestamp(mem.last_ts):
        log.append(f"data clock moved backwards ({mem.last_ts} -> {ts}); run `python -m pulse reset` for a clean replay")

    # 0. new metrics dropped into the inbox -> graph handler extends the graph
    ingested = _ingest_inbox(w, log)

    # 1. monitor
    evals = evaluate_ex(w, kpis, now)
    statuses = [e.status for e in evals]
    by = {e.name: e for e in evals}
    onsets = {e.name: e.onset for e in evals}

    # 2. incident memory
    book = IncidentBook.load()
    if chosen:   # metrics the CEO pinned or untracked since the last tick
        log += _choice_log(set(mem.last_severity), [k.name for k in kpis],
                           book.close_untracked({k.name for k in kpis}, now))
    if same_hour:
        log.append(f"same data hour as the last tick ({ts}): re-evaluated, incident memory unchanged")
        for st in statuses:
            act = book.active(st.name)
            st.incident_id = act[0].id if act else None
        inc_events = []
    else:
        inc_events = book.update(statuses, now, onsets)
    events: dict[str, str] = {}
    incident_of: dict[str, Incident] = {}
    for ev in inc_events:
        e = by.get(ev.incident.kpi)
        incident_of[ev.incident.kpi] = ev.incident
        if ev.kind in ("opened", "escalated", "resolved"):
            events[ev.incident.kpi] = ev.kind
        if ev.kind == "opened":
            since = f", out of band since {e.onset[11:]}" if e and e.onset else ""
            log.append(f"{_fmt_status(e)}{since} -> opened {ev.incident.id}")
        elif ev.kind == "escalated":
            log.append(f"{_fmt_status(e)} -> {ev.incident.id} ESCALATED ({ev.text})")
        elif ev.kind == "persisting":
            log.append(f"{_fmt_status(e)} -> {ev.incident.id} persisting ({ev.incident.ticks_seen} ticks), not re-reported")
        elif ev.kind == "resolved":
            log.append(f"{ev.incident.kpi} back in band -> {ev.incident.id} resolved ({ev.text})")
            try:   # write the outcome back to persistent memory so future incidents can recall it
                from pulse import memory
                iid = ev.incident.interpretation_id
                memory.remember(ev.incident, C.load(Interpretation, f"interpretations/{iid}.json") if iid else None, ts)
            except Exception:
                pass
        elif ev.kind == "recovering":
            log.append(f"{ev.incident.kpi} in band -> {ev.incident.id} {ev.text}")
        elif ev.kind == "held":
            log.append(f"{ev.incident.kpi} data incomplete -> {ev.incident.id} held, no judgement this hour")
    for inc in book.active():
        incident_of.setdefault(inc.kpi, inc)

    # severity changes of KPIs without incident events. Only material ones (a new data issue, a newly seen
    # innocent change) re-brief the CEO; ok <-> watch flicker is logged but costs nothing.
    material = []
    for e in evals:
        prev = mem.last_severity.get(e.name, "ok")
        if e.status.severity == "data_issue" and prev != "data_issue":
            material.append(f"{e.name} data issue")
        if e.expected_by and e.expected_by not in mem.announced:
            material.append(f"{e.name} innocent change ({e.expected_by})")
            mem.announced.append(e.expected_by)
        if e.name in events or any(ev.incident.kpi == e.name for ev in inc_events):
            continue
        if e.status.severity == "data_issue" and prev != "data_issue":
            log.append(f"{e.name}: {'; '.join(e.data_problems)} -> data_issue, no alert while the data is incomplete")
        elif e.status.severity == "watch" and prev != "watch":
            why = f" ({'; '.join(e.notes)})" if e.notes else ""
            dg = " - downgraded from alert" if e.raw_severity == "alert" else ""
            log.append(f"{_fmt_status(e)} -> watch{dg}{why}")
        elif e.status.severity == "ok" and prev != "ok":
            log.append(f"{e.name} back to ok")

    # 3. decide investigations
    todo: dict[str, tuple[Incident, str]] = {}
    for ev in inc_events:
        if ev.kind == "opened":
            todo[ev.incident.id] = (ev.incident, "new incident")
        elif ev.kind == "escalated":
            todo[ev.incident.id] = (ev.incident, "escalated")
        elif ev.kind == "persisting":
            last = mem.last_investigated.get(ev.incident.id)
            if last is None or mem.tick - last >= REINVESTIGATE_EVERY:
                todo.setdefault(ev.incident.id, (ev.incident, "periodic re-check" if last is not None
                                                 else "not investigated yet"))
    for k in mem.follow_up:
        for inc in book.active(k):
            todo.setdefault(inc.id, (inc, "follow-up requested by the last brief"))
    queue = sorted(todo.values(), key=lambda x: (PRIORITY.get(x[1], 9), -by[x[0].kpi].bad_z if x[0].kpi in by else 0))
    if _budget_tight():
        dropped = [i.id for i, r in queue if r != "new incident"]
        queue = [(i, r) for i, r in queue if r == "new incident"]
        if dropped:
            log.append(f"LLM budget above 80% of cap: skipped re-investigations of {', '.join(dropped)}")
    if len(queue) > MAX_INVESTIGATIONS:
        log.append(f"{len(queue)} investigations wanted, running the top {MAX_INVESTIGATIONS}: "
                   f"deferred {', '.join(i.id for i, _ in queue[MAX_INVESTIGATIONS:])}")
        queue = queue[:MAX_INVESTIGATIONS]
    investigated: set[str] = set()
    for inc, reason in queue:
        log.append(f"{inc.id}: {reason} -> asking the graph interpreter")
        it = _investigate(w, inc, reason, mem, book, ts, log)
        if it is not None:
            investigated.add(inc.id)
    interps: dict[str, Interpretation] = {}
    for inc in list(book.active()) + [i for i in book.incidents if events.get(i.kpi) == "resolved"]:
        it = _interp_for(inc)
        if it is not None:
            interps[inc.kpi] = it
            mem.watch_nodes = sorted(set(mem.watch_nodes) | set(it.watch_next))[:20]

    # 4. CEO brief
    notes = {e.name: e.notes for e in evals}
    template = template_brief(ts, statuses, events=events, incidents=incident_of, interps=interps,
                              notes=notes, onsets=onsets)
    if ingested:
        template.text += "\n" + " ".join(ingested)
    prev = C.load(Brief, "brief.json")
    nonok = [s for s in statuses if s.severity != "ok"]
    material += ingested
    changed = bool(events or investigated or material) and (not same_hour or bool(ingested))
    every_tick = os.environ.get("PULSE_BRIEF_EVERY_TICK", "0") == "1" and bool(nonok)
    follow_up: list[str] = []
    if not (changed or every_tick):
        brief = template
        mem.template_briefs += 1
        if not nonok:
            log.append(f"all {len(statuses)} KPIs ok -> template brief (no LLM call)")
        else:
            if prev is not None and prev.llm_mode in ("live", "replay"):
                # keep the agent's wording, refresh the per-KPI numbers
                brief = template.model_copy(update={"headline": prev.headline, "text": prev.text,
                                                    "model": prev.model, "llm_mode": prev.llm_mode})
            log.append("nothing material changed -> brief numbers refreshed (no LLM call)")
    else:
        why = ", ".join([f"{k} {v}" for k, v in events.items()] +
                        ([f"investigated {len(investigated)}"] if investigated else []) + material) or "non-ok KPIs"
        brief, follow_up = _llm_brief(ts, evals, book, events, interps, prev, template, investigated, log, why)
        mem.llm_briefs += 1
    mem.follow_up = [k for k in follow_up if k in by]
    if mem.follow_up:
        log.append(f"brief asked to re-investigate next tick: {', '.join(mem.follow_up)}")

    # 5. persist
    mem.last_ts = ts
    mem.last_severity = {s.name: s.severity for s in statuses}
    state = TickState(ts=ts, tick=mem.tick, kpis=statuses, spend_usd=round(llm.total_spend(), 4),
                      llm_mode=llm.mode(), log=log)
    book.save()
    C.save(state, "state.json")
    C.save(brief, "brief.json")
    C.save(mem, "orchestrator.json")
    return state


def _llm_brief(ts, evals, book, events, interps, prev, template, investigated, log, why):
    fresh = {i.id for i in book.active() if inc_key(i) in interps and i.id in investigated}
    tools = _brief_tools(evals, book)
    prompt = _situation(ts, evals, book, events, interps, prev)
    try:
        res = llm.run_agent("orchestrator", BRIEF_SYSTEM, prompt, tools, BRIEF_SCHEMA,
                            mock=_mock_brief_policy(template, book, interps, fresh, investigated), max_turns=4)
        o = res.output
        items = [BriefItem(kpi=x["kpi"], label=x.get("label", x["kpi"]), severity=x.get("severity", "ok"),
                           sentence=x.get("sentence", ""), detail=x.get("detail", ""),
                           incident_id=x.get("incident_id") or None)
                 for x in o.get("items", [])]
        brief = Brief(ts=ts, headline=o.get("headline", template.headline), items=items or template.items,
                      text=o.get("text", template.text),
                      model=res.model if res.mode != "mock" else f"mock ({llm.model_for('orchestrator')} policy)",
                      llm_mode=res.mode)
        fu = o.get("follow_up", {}) or {}
        brief.follow_up = list(fu.get("reinvestigate", [])) + list(fu.get("watch_nodes", []))
        tools_used = ", ".join(c["tool"] for c in res.calls) or "no tools"
        log.append(f"brief by orchestrator agent ({res.mode}, {tools_used}, ${res.cost_usd:.3f}) because: {why}")
        return brief, list(fu.get("reinvestigate", []))
    except Exception as e:
        log.append(f"orchestrator agent failed ({type(e).__name__}: {str(e)[:160]}) -> template brief")
        return template, []


def recheck(*, w=None) -> TickState | None:
    """Apply the CEO's latest choices to the last checked hour, without moving the data clock and without an LLM call:
    a pinned metric is checked at that hour and gets its row (and an incident if it is alerting; the interpreter
    looks at it on the next tick), an untracked one leaves and its open incident is closed as no longer tracked
    (nothing written to case memory). The other rows keep their numbers and wording. The world is loaded only when a
    metric joins."""
    prev_state = C.load(TickState, "state.json")
    if prev_state is None:
        return None
    ts, ch = prev_state.ts, choices.load()
    have = [k.name for k in prev_state.kpis]
    want = [m for m in dict.fromkeys([k.name for k in load_kpis()] + ch["pinned"]) if m not in ch["untracked"]]
    kept = [k for k in prev_state.kpis if k.name in want]
    added: list[Evaluation] = []
    if any(m not in have for m in want):
        if w is None:
            from pulse.world import world
            w = world()
        new_kpis = [k for k in headline_kpis(w, ch) if k.name not in have]
        added = evaluate_ex(w, new_kpis, ts) if new_kpis else []
    statuses = kept + [e.status for e in added]
    names = [s.name for s in statuses]
    book = IncidentBook.load()
    closed = book.close_untracked(set(names), ts)
    opened = [ev for ev in book.update([e.status for e in added], ts, {e.name: e.onset for e in added})
              if ev.kind == "opened"]
    log = _choice_log(set(have), names, closed)
    if not log:
        return prev_state
    by = {e.name: e for e in added}
    for ev in opened:
        e = by[ev.incident.kpi]
        log.append(f"{_fmt_status(e)}" + (f", out of band since {e.onset[11:]}" if e.onset else "")
                   + f" -> opened {ev.incident.id}")

    # the brief: same hour, so kept rows keep their wording; the headline is rebuilt from the numbers
    happened = _events_at(book, ts)
    events = {k: kind for k, (kind, _) in happened.items()}
    incident_of = {k: inc for k, (_, inc) in happened.items()}
    for st in statuses:
        act = book.active(st.name, segment=None)
        st.incident_id = act[0].id if act else None
        if act:
            incident_of.setdefault(st.name, act[0])
    interps = {k: it for k, inc in incident_of.items() if (it := _interp_for(inc)) is not None}
    notes = {s.name: [n for n in s.note.split("; ") if n] for s in kept} | {e.name: e.notes for e in added}
    template = template_brief(ts, statuses, events=events, incidents=incident_of, interps=interps, notes=notes,
                              onsets={s.name: s.onset_ts for s in statuses})
    prev = C.load(Brief, "brief.json")
    brief = template
    if prev is not None and prev.ts == ts:
        old = {it.kpi: it for it in prev.items}
        gone = set(have) - set(names)
        moved = [s for s in prev_state.kpis if s.name in gone] + [e.status for e in added]
        keep_words = prev.llm_mode in ("live", "replay") and all(s.severity == "ok" for s in moved)
        brief = template.model_copy(update={
            "items": [old.get(it.kpi, it) if it.kpi not in by else it for it in template.items],
            "follow_up": [f for f in prev.follow_up if f not in gone],
            **({"headline": prev.headline, "text": prev.text, "model": prev.model, "llm_mode": prev.llm_mode}
               if keep_words else {})})
    mem = _load_mem()
    mem.last_severity = {s.name: s.severity for s in statuses}
    mem.follow_up = [k for k in mem.follow_up if k in names]
    state = prev_state.model_copy(update={"kpis": statuses, "log": prev_state.log + log})
    book.save()
    C.save(state, "state.json")
    C.save(brief, "brief.json")
    C.save(mem, "orchestrator.json")
    return state


def status_text() -> str:
    """Human summary of the current state dir (for `python -m pulse status`)."""
    st = C.load(TickState, "state.json")
    if st is None:
        return f"no state in {C.STATE_DIR} yet - run `python -m pulse tick`"
    br = C.load(Brief, "brief.json")
    incs = C.load_list(Incident, "incidents.json")
    out = [f"tick {st.tick} at {st.ts}  (llm {st.llm_mode}, spend ${st.spend_usd:.3f})  state: {C.STATE_DIR}"]
    for k in st.kpis:
        dev = f"{k.deviation_pct:+.1f}%" if k.deviation_pct is not None else "  n/a"
        out.append(f"  {k.severity:<10} {k.label:<28} {fmt_value(k.value, k.unit):>12}  {dev:>7}  "
                   f"z {k.z if k.z is not None else 'n/a'}" + (f"  {k.incident_id}" if k.incident_id else ""))
    act = [i for i in incs if i.state != "resolved"]
    out.append(f"incidents: {len(act)} active, {len(incs) - len(act)} resolved")
    for i in incs[-8:]:
        out.append(f"  {i.id:<40} {i.state:<10} {i.headline}")
    if br:
        out += ["brief:", "  " + br.text.replace("\n", "\n  ")]
    return "\n".join(out)
