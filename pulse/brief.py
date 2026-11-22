"""CEO briefing helpers: number formatting, template (zero-cost) brief, markdown rendering.

The template brief is what the orchestrator's mock policy returns and what all-ok ticks get without any LLM call.
"""
from __future__ import annotations

import re

import pandas as pd

from pulse.contracts import Brief, BriefItem, Incident, Interpretation, KpiStatus

SEV_ORDER = {"alert": 0, "data_issue": 1, "watch": 2, "ok": 3}
SOURCE_WORDS = {   # data sources (argus/config/tables.yaml) in CEO words
    "orders_db": "order data", "web_analytics": "web traffic data", "inventory": "inventory data",
    "erp": "purchasing data", "ads_platform": "advertising data", "recsys": "recommendation data",
    "payment_gateway": "payment data", "observability": "app and service health data",
    "seller_feeds": "seller feed data", "support_desk": "support ticket data", "price_monitoring": "price data",
}


def fmt_value(v: float | None, unit: str | None) -> str:
    if v is None:
        return "n/a"
    if unit == "ratio":
        return f"{v * 100:.1f}%"
    if unit == "EUR":
        a = abs(v)
        s = f"{a / 1e6:.2f}M" if a >= 1e6 else f"{a / 1e3:.1f}k" if a >= 1e4 else f"{a:,.0f}"
        return ("-" if v < 0 else "") + "EUR " + s
    if abs(v) >= 1e4:
        return f"{v / 1e3:.1f}k"
    return f"{v:,.0f}" if abs(v) >= 10 else f"{v:.2f}"


def fmt_dev(st: KpiStatus) -> str:
    if st.deviation_pct is None:
        return ""
    word = "below" if st.deviation_pct < 0 else "above"
    return f"{abs(st.deviation_pct):.0f}% {word} normal"


def _hh(ts: str | None) -> str:
    return f"{pd.Timestamp(ts):%H:%M}" if ts else ""


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def data_problem_text(problems: str) -> str:
    """'orders_db late: only 38% ...; erp stale: ...' -> 'Order data late: only 38% .... Purchasing data stale: ....'"""
    out = []
    for p in (x for x in problems.split("; ") if x):
        m = re.match(r"^(\w+) (late|stale|silent|down|frozen): ", p)
        if m:
            p = SOURCE_WORDS.get(m[1], m[1].replace("_", " ") + " data") + p[len(m[1]):]
        out.append(_cap(p.rstrip(".")) + ".")
    return " ".join(out)


def _ok_sentence(st: KpiStatus) -> str:
    d = st.deviation_pct
    if d is None or abs(d) < 3:
        return "In line with normal for this hour."
    move = f"{'Up' if d > 0 else 'Down'} {abs(d):.0f}% on normal"
    band = st.series[-1] if st.series else None
    if band is None or None in (band.lo, band.hi, st.value) or band.lo <= st.value <= band.hi:
        return f"{move}, within its usual range."
    good = st.polarity in ("up_is_good", "down_is_good") and (d > 0) == (st.polarity == "up_is_good")
    return f"{move}, {'a good sign' if good else 'not a concern'}."


def kpi_text(st: KpiStatus, *, event: str | None = None, incident: Incident | None = None,
             interp: Interpretation | None = None, notes: list[str] | None = None,
             onset: str | None = None) -> tuple[str, str]:
    """(one short sentence, detail) for one KPI. The UI shows the numbers next to the sentence, so the sentence says
    what they mean; the detail holds the why and what we do about it."""
    notes = notes or []
    since = f" since {_hh(onset)}" if onset else ""
    why = f"Likely cause: {interp.headline.rstrip('.')}." if interp is not None and interp.headline else ""
    if st.severity == "data_issue":
        dq = next((n for n in notes if n.startswith("data issue")), "")
        return (f"{st.label} can't be trusted this hour, so we are holding judgement.",
                data_problem_text(dq.replace("data issue: ", "")) if dq else "")
    if event == "resolved" and incident is not None:
        return f"{st.label} is back to normal.", f"The issue that started at {_hh(incident.onset_ts or incident.opened_at)} is closed."
    if st.severity == "alert":
        word = {"opened": "New: ", "escalated": "Worse: "}.get(event or "", "")
        n = incident.ticks_seen if incident and not word else None
        detail = " ".join(x for x in (why, f"Ongoing for {n} checks." if n else "") if x)
        return f"{word}{st.label} is {'' if word else 'still '}{fmt_dev(st)}{since}.", detail
    if st.severity == "watch":
        exp = next((n for n in notes if n.startswith("expected:")), None)
        if exp:
            what = exp[len("expected: "):]
            event_name = what.split(" (")[0].removeprefix("the ")
            reasons = [_cap(r) + "." for r in re.findall(r"\(([^()]*)\)", what)]
            return (f"{st.label} is {fmt_dev(st)}, as expected during the {event_name}.",
                    " ".join(reasons + ["Watching, no action needed."]))
        if incident is not None:
            return f"{st.label} is recovering.", f"Still {fmt_dev(st)}; the issue stays open until it holds."
        return f"{st.label} is {fmt_dev(st)}, a mild move we are watching.", ""
    if incident is not None:
        return f"{st.label} is back inside its normal range.", "Confirming on the next check before closing the issue."
    return _ok_sentence(st), ""


def template_brief(ts: str, statuses: list[KpiStatus], *, events: dict[str, str] | None = None,
                   incidents: dict[str, Incident] | None = None, interps: dict[str, Interpretation] | None = None,
                   notes: dict[str, list[str]] | None = None, onsets: dict[str, str | None] | None = None,
                   model: str = "template", llm_mode: str = "mock") -> Brief:
    """Deterministic brief from the numbers. Dicts are keyed by KPI name."""
    events, incidents, interps = events or {}, incidents or {}, interps or {}
    notes, onsets = notes or {}, onsets or {}
    items = []
    for st in sorted(statuses, key=lambda s: SEV_ORDER.get(s.severity, 9)):
        inc = incidents.get(st.name)
        sentence, detail = kpi_text(st, event=events.get(st.name), incident=inc, interp=interps.get(st.name),
                                    notes=notes.get(st.name), onset=onsets.get(st.name))
        items.append(BriefItem(kpi=st.name, label=st.label, severity=st.severity, sentence=sentence, detail=detail,
                               incident_id=inc.id if inc else st.incident_id))
    alerts = [s for s in statuses if s.severity == "alert"]
    new = [s for s in alerts if events.get(s.name) in ("opened", "escalated")]
    resolved = [k for k, e in events.items() if e == "resolved"]
    data = [s for s in statuses if s.severity == "data_issue"]
    watch = [s for s in statuses if s.severity == "watch"]
    recovering = [s for s in statuses if s.incident_id and s.severity in ("watch", "ok")]   # open, not yet resolved
    hh = _hh(ts)
    if new:
        s = max(new, key=lambda x: abs(x.deviation_pct or 0))
        why = interps.get(s.name)
        word = "NEW" if events.get(s.name) == "opened" else "WORSE"
        headline = (f"{word}: {why.headline.rstrip('.')}" if why and why.headline
                    else f"{word}: {s.label} {fmt_dev(s)} - investigating")
        if len(alerts) > 1:
            headline += f" (+{len(alerts) - 1} more issue{'s' if len(alerts) > 2 else ''})"
    elif alerts:
        headline = f"{len(alerts)} ongoing issue{'s' if len(alerts) > 1 else ''}: " + \
                   ", ".join(f"{s.label} {fmt_dev(s)}" for s in alerts)
    elif resolved:
        headline = "Resolved: " + ", ".join(next((s.label for s in statuses if s.name == k), k) for k in resolved) + \
                   " back to normal"
    elif recovering:
        headline = "Recovering: " + ", ".join(f"{s.label} {fmt_dev(s)}" for s in recovering) +                    " - the incident stays open until it holds"
    elif data:
        headline = f"Data delayed: {', '.join(s.label for s in data)} can't be judged at {hh}"
    elif watch:
        exp = [(x, next(n for n in notes.get(x.name, []) if n.startswith("expected:"))) for x in watch
               if any(n.startswith("expected:") for n in notes.get(x.name, []))]
        if exp:
            x, n = exp[0]
            event = n[len("expected: "):].split(" (")[0]
            headline = f"No action needed: {x.label} dip is expected ({event})"
        else:
            headline = f"All clear at {hh}, {len(watch)} metric{'s' if len(watch) > 1 else ''} on watch"
    else:
        headline = f"All {len(statuses)} headline metrics on track at {hh}"
    lines = [f"{headline}."]
    for it in items:
        if it.severity != "ok" or it.kpi in events:
            lines.append(f"- {it.sentence}")
    ok = [s.label for s in statuses if s.severity == "ok" and s.name not in events]
    if ok and len(ok) < len(statuses):
        lines.append(f"On track: {', '.join(ok)}.")
    return Brief(ts=ts, headline=headline, items=items, text="\n".join(lines), model=model, llm_mode=llm_mode)


def render_markdown(brief: Brief) -> str:
    icon = {"alert": "[ALERT]", "data_issue": "[DATA]", "watch": "[WATCH]", "ok": "[OK]"}
    out = [f"## {brief.headline}", f"_{brief.ts} - {brief.model or 'template'} ({brief.llm_mode})_", ""]
    for it in brief.items:
        inc = f" `{it.incident_id}`" if it.incident_id else ""
        out.append(f"- **{icon.get(it.severity, it.severity)} {it.label}**{inc}: {it.sentence}")
    if brief.text:
        out += ["", brief.text]
    return "\n".join(out)
