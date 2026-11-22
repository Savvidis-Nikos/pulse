"""Incident memory: one incident per KPI + direction, tracked across ticks so a problem is reported once.

Lifecycle
  alert, no active incident for kpi+direction  -> NEW incident  INC-<kpi>-<yyyymmddhh of onset>  state "open"
                                                 (an active incident in the other direction counts it as recovery)
  alert, active incident                      -> "persisting" (ticks_seen += 1); ESCALATED when the deviation
                                                 grew >= 1.5x (and >= 3 points) since it was last reported
  ok / watch                                  -> recovery streak += 1; RESOLVED after `resolve_after` ticks
  data_issue                                  -> held (no judgement on untrustworthy data)
  metric untracked by the CEO                 -> closed as "no longer tracked" (close_untracked; not a resolution,
                                                 so nothing is written to case memory)
Segment incidents (update_segments): same lifecycle keyed by kpi + segment + direction, INC-<kpi>-<value>-<onset>.
An alerting cell that an active incident already explains (the KPI total in the same direction, or an incident whose
segment - its own or the one its interpretation localised - contains the cell) is ATTACHED to it (noted once, never
reported twice); when the KPI total starts alerting, its open segment incidents are MERGED into the total's incident.
Persisted in <state>/incidents.json (list[Incident]) + incidents_meta.json (recovery streak, peaks, last reported numbers).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from pulse import contracts as C
from pulse.contracts import Incident, KpiStatus
from pulse.monitor import segment_key

RESOLVE_AFTER = 3
ESCALATE_FACTOR = 1.5
ESCALATE_MIN_POINTS = 3.0
_ANY = object()


def key(inc: Incident) -> str:
    """'payment_success_rate' for a total incident, 'payment_success_rate|country=NL' for a segment incident."""
    return segment_key(inc.kpi, inc.segment)


@dataclass
class IncidentEvent:
    kind: str                 # opened | persisting | escalated | recovering | resolved | held | attached | merged
    incident: Incident
    status: KpiStatus | None = None
    text: str = ""


def _hour(ts) -> str:
    return f"{pd.Timestamp(ts):%H:%M}"


def _dev(st: KpiStatus) -> str:
    return f"{st.deviation_pct:+.1f}%" if st.deviation_pct is not None else "n/a"


class IncidentBook:
    def __init__(self, resolve_after: int = RESOLVE_AFTER, escalate_factor: float = ESCALATE_FACTOR):
        self.resolve_after = resolve_after
        self.escalate_factor = escalate_factor
        self.incidents: list[Incident] = []
        self.meta: dict[str, dict] = {}

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, **kw) -> "IncidentBook":
        b = cls(**kw)
        b.incidents = C.load_list(Incident, "incidents.json")
        p = C.STATE_DIR / "incidents_meta.json"
        if p.exists():
            try:
                b.meta = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                b.meta = {}
        return b

    def save(self) -> None:
        C.save_list(self.incidents, "incidents.json")
        p = C.STATE_DIR / "incidents_meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        C.replace(tmp, p)

    # ------------------------------------------------------------ queries
    def get(self, inc_id: str) -> Incident | None:
        return next((i for i in self.incidents if i.id == inc_id), None)

    def active(self, kpi: str | None = None, direction: str | None = None, segment=_ANY) -> list[Incident]:
        """Open incidents; segment=None -> KPI totals only, a dict -> that segment only, default -> both."""
        out = [i for i in self.incidents if i.state != "resolved" and (kpi is None or i.kpi == kpi)
               and (segment is _ANY or (i.segment or None) == (segment or None))]
        if direction is not None:
            out = [i for i in out if (i.direction or self.meta.get(i.id, {}).get("direction")) == direction]
        return out

    def direction(self, inc: Incident) -> str:
        return inc.direction or self.meta.get(inc.id, {}).get("direction", "down")

    # ------------------------------------------------------------ update
    def update(self, statuses: list[KpiStatus], now, onsets: dict[str, str | None] | None = None) -> list[IncidentEvent]:
        """Apply one tick of KPI statuses. Sets status.incident_id for KPIs with an active incident."""
        now = pd.Timestamp(now)
        ts = f"{now:%Y-%m-%d %H:%M}"
        onsets = onsets or {}
        events: list[IncidentEvent] = []
        for st in statuses:
            if st.severity == "alert" and st.direction in ("up", "down"):
                for inc in self.active(st.name, segment=None):   # a rebound: the other direction's incident recovers
                    if self.direction(inc) != st.direction:
                        self._recover(inc, st, ts, events)
                cur = self.active(st.name, st.direction, segment=None)
                if cur:
                    events.append(self._persist(cur[0], st, ts))
                    st.incident_id = cur[0].id
                    continue
                opened = self._open(st, ts, onsets.get(st.name))
                events.append(opened)
                st.incident_id = opened.incident.id
                for inc in self.active(st.name, st.direction):   # its open segment incidents: now one problem
                    if inc.segment:
                        events.append(self._merge(inc, opened.incident, ts))
                continue
            for inc in self.active(st.name, segment=None):
                self._recover(inc, st, ts, events)
        return events

    def close_untracked(self, tracked: set[str], now) -> list[Incident]:
        """Close the open incidents of metrics no longer in the headline set (`tracked`)."""
        ts = f"{pd.Timestamp(now):%Y-%m-%d %H:%M}"
        closed = [i for i in self.active() if i.kpi not in tracked]
        for inc in closed:
            inc.state, inc.resolved_at, inc.closed_reason = "resolved", ts, "no longer tracked"
            inc.history.append(f"{ts} closed: {inc.kpi} is no longer tracked")
        return closed

    def update_segments(self, cells: list, now, localized: dict[str, dict] | None = None) -> list[IncidentEvent]:
        """Apply this tick's checked segment cells (monitor.Evaluation with .segment); unchecked cells are not judged.
        localized: incident id -> segment its interpretation localised (used to tell whether it explains a cell)."""
        ts = f"{pd.Timestamp(now):%Y-%m-%d %H:%M}"
        events: list[IncidentEvent] = []
        for e in cells:
            st, seg = e.status, e.segment
            if st.severity == "alert" and st.direction in ("up", "down"):
                cur = self.active(st.name, st.direction, segment=seg)
                if cur:
                    events.append(self._persist(cur[0], st, ts))
                    st.incident_id = cur[0].id
                    continue
                host = self._explained_by(st.name, seg, st.direction, localized or {})
                if host is not None:
                    st.incident_id = host.id
                    ev = self._attach(host, st, seg, ts)
                    if ev is not None:
                        events.append(ev)
                    continue
                events.append(self._open(st, ts, e.onset, seg))
                st.incident_id = events[-1].incident.id
                continue
            for inc in self.active(st.name, segment=seg):
                self._recover(inc, st, ts, events)
        return events

    def _explained_by(self, kpi: str, seg: dict, direction: str, localized: dict[str, dict]) -> Incident | None:
        """An active incident that already explains this alerting cell: the KPI total in the same direction, else
        any incident whose own or localised segment contains the cell's value (e.g. the DE/android payment fault
        explains conversion in DE)."""
        act = self.active()
        tot = next((i for i in act if not i.segment and i.kpi == kpi and self.direction(i) == direction), None)
        if tot is not None:
            return tot
        return next((i for i in act if any(str((i.segment or localized.get(i.id) or {}).get(d)) == str(v)
                                           for d, v in seg.items())), None)

    def _attach(self, host: Incident, st: KpiStatus, seg: dict, ts: str) -> IncidentEvent | None:
        seen = self.meta.setdefault(host.id, {}).setdefault("segments", [])
        k = segment_key(st.name, seg)
        if k in seen:
            return None
        seen.append(k)
        host.history.append(f"{ts} also seen in {k}: {_dev(st)} (z {st.z:+.1f}) - same incident, not reported again")
        return IncidentEvent("attached", host, st, k)

    def _merge(self, inc: Incident, host: Incident, ts: str) -> IncidentEvent:
        inc.state, inc.resolved_at = "resolved", ts
        inc.history.append(f"{ts} merged: {host.kpi} as a whole is now alerting, tracked under {host.id}")
        host.history.append(f"{ts} absorbs {inc.id} (already open for {key(inc)})")
        self.meta.setdefault(host.id, {}).setdefault("segments", []).append(key(inc))
        return IncidentEvent("merged", inc, None, host.id)

    def _recover(self, inc: Incident, st: KpiStatus, ts: str, events: list[IncidentEvent]) -> None:
        st.incident_id = inc.id
        m = self.meta.setdefault(inc.id, {})
        if st.severity == "data_issue":
            inc.history.append(f"{ts} data incomplete - holding judgement")
            events.append(IncidentEvent("held", inc, st, "data incomplete, holding judgement"))
            return
        m["ok_streak"] = m.get("ok_streak", 0) + 1
        if m["ok_streak"] >= self.resolve_after:
            inc.state, inc.resolved_at = "resolved", ts
            start = pd.Timestamp(inc.onset_ts or inc.opened_at)
            dur = (pd.Timestamp(inc.last_seen) - start) / pd.Timedelta(hours=1) + 1
            m["duration_h"] = round(dur, 1)
            inc.history.append(f"{ts} resolved: back within the normal band for {m['ok_streak']} ticks "
                               f"(lasted ~{dur:.0f} h)")
            events.append(IncidentEvent("resolved", inc, st, f"back to normal after ~{dur:.0f} h"))
            st.incident_id = None
        else:
            inc.history.append(f"{ts} {st.severity} ({_dev(st)}) - recovering "
                               f"{m['ok_streak']}/{self.resolve_after}")
            events.append(IncidentEvent("recovering", inc, st, f"recovering ({m['ok_streak']}/{self.resolve_after})"))

    def _open(self, st: KpiStatus, ts: str, onset: str | None, segment: dict | None = None) -> IncidentEvent:
        since = onset or ts
        tag = "".join(f"-{v}" for v in (segment or {}).values())
        base = f"INC-{st.name}{tag}-{pd.Timestamp(since):%Y%m%d%H}"
        inc_id, n = base, 2
        while self.get(inc_id):
            inc_id, n = f"{base}-{n}", n + 1
        word = "below" if st.direction == "down" else "above"
        headline = (f"{st.label} {abs(st.deviation_pct or 0):.0f}% {word} normal since {_hour(since)}")
        inc = Incident(id=inc_id, kpi=st.name, state="open", severity="alert", opened_at=ts, last_seen=ts,
                       direction=st.direction, onset_ts=since, segment=segment or None,
                       headline=headline,
                       history=[f"{ts} opened: {st.label} {_dev(st)} vs expected (z {st.z:+.1f}), "
                                f"out of band since {since}"])
        self.incidents.append(inc)
        self.meta[inc_id] = {"ok_streak": 0,
                             "reported_dev": st.deviation_pct, "peak_dev": st.deviation_pct, "peak_z": st.z,
                             "last_investigated_tick": None, "escalations": 0}
        return IncidentEvent("opened", inc, st, headline)

    def _persist(self, inc: Incident, st: KpiStatus, ts: str) -> IncidentEvent:
        m = self.meta.setdefault(inc.id, {})
        m["ok_streak"] = 0
        inc.state = "persisting"
        inc.ticks_seen += 1
        inc.last_seen = ts
        dev = abs(st.deviation_pct or 0)
        if dev > abs(m.get("peak_dev") or 0):
            m["peak_dev"], m["peak_z"] = st.deviation_pct, st.z
        rep = abs(m.get("reported_dev") or 0)
        if dev >= self.escalate_factor * rep and dev - rep >= ESCALATE_MIN_POINTS:
            m["reported_dev"] = st.deviation_pct
            m["escalations"] = m.get("escalations", 0) + 1
            inc.history.append(f"{ts} ESCALATED: now {_dev(st)} (was {rep:.1f}% when last reported), z {st.z:+.1f}")
            return IncidentEvent("escalated", inc, st, f"worsened to {_dev(st)} (from {rep:.0f}%)")
        inc.history.append(f"{ts} persisting: {_dev(st)} (z {st.z:+.1f})")
        return IncidentEvent("persisting", inc, st, f"still {_dev(st)} after {inc.ticks_seen} ticks")
