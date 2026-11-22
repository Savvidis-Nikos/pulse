"""Monitor: checks every CEO KPI against its seasonal baseline once per tick (pure statistics, no LLM).

For each KPI (pulse/kpis.yaml):
  value     the last `window_h` hours (flow metrics reported per hour, ratios as ratio of sums)
  expected  median of the same window on the previous 7 days, weekday-adjusted (argus.analytics.baseline);
            days on which a feeding source had partial data are left out of the baseline
  z         robust z-score (MAD, floored by sampling noise and the metric's own recent residual noise)
  severity  polarity-aware: only the UNWANTED direction can alert; a mild move is "watch";
            a source that is late / stale / frozen / silent -> "data_issue" (the number cannot be trusted);
            a move explained by a planned event (calendar / planned campaign) with a healthy net business
            outcome (orders and revenue not down), or by an announced event that moves a metric upstream of
            the KPI in the graph and started with the move -> downgraded to "watch" ("expected: <event>").
            An event limited to one segment (country / platform / category) explains nothing if another segment
            is just as far off (competing cause) or the KPI cannot be split that way.
            a "watch" that stays off in the unwanted direction for SUSTAIN_H hours becomes an "alert" (slow drift).
  onset     first hour of the current excursion (within 24 h): "since 14:00".

Segments (evaluate_segments): each tick a budget of single-dimension cells (country / platform / category, only dims
the KPI has) is checked with the same baseline, ranked by volume share x (1 + trouble) x hours since last checked.
Stricter than the totals: alert only at z >= SEG_ALERT_Z two hours in a row (or the SUSTAIN_H slow-drift rule),
a move >= SEG_DEV_FACTOR x min_dev_pct, enough volume, concentrated in the cell (no sibling segment as far off) and
not explained by a planned event in that segment.

    from pulse.monitor import evaluate, evaluate_ex, load_kpis
    statuses = evaluate(world(), load_kpis())          # list[KpiStatus]
    kpis = headline_kpis(world())                      # kpis.yaml adjusted by the CEO's choices (pulse.choices)
    evals = evaluate_ex(world(), load_kpis())          # list[Evaluation]: status + notes/onset/context
    cells = evaluate_segments(world(), load_kpis(), evals, state={})   # list[Evaluation] with .segment
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pulse.contracts import KpiStatus, SeriesPoint

KPIS_PATH = Path(__file__).with_name("kpis.yaml")
INNOCENT_EVENT_TYPES = {"campaign", "maintenance", "catalog", "sales_event"}
ONSET_LOOKBACK_H = 24
SUSTAIN_H = 6            # consecutive hours past watch_z in the unwanted direction -> alert (slow drift, e.g. stockout)
SEGMENT_Z = 3.0          # another segment this far off (bad direction) -> a segment-scoped plan does not explain it
SEGMENT_Z_SAME = 2.0     # ... or this far off and nearly as far as the event's own segment (not concentrated there)
SEG_DIMS = ("country", "platform", "category")
SEG_BUDGET = int(os.environ.get("PULSE_SEGMENT_BUDGET", "30"))   # segment cells checked per tick
SEG_ALERT_Z = 6.0        # a cell alerts at this z two hours in a row (or by the SUSTAIN_H slow-drift rule) ...
SEG_DEV_FACTOR = 2.0     # ... with a move of at least this x the KPI's min_dev_pct ...
SEG_MIN_SHARE = 0.03     # ... and at least this share of the KPI's volume in the window
SEG_NEVER_H = 24         # "hours since last checked" of a cell never checked


@dataclass
class KpiDef:
    name: str
    label: str
    window_h: int = 3
    watch_z: float = 2.5
    alert_z: float = 4.0
    min_dev_pct: float = 3.0
    polarity: str | None = None
    series_h: int = 48
    pinned: bool = False         # added by the CEO (pulse.choices), not listed in kpis.yaml


@dataclass
class Evaluation:
    """A KpiStatus plus the context the orchestrator / brief need (not part of the shared contract)."""
    status: KpiStatus
    kpi: KpiDef
    bad_z: float = 0.0                       # z in the unwanted direction (>0 = bad)
    raw_severity: str = "ok"                 # before planned-event / data-quality adjustments
    onset: str | None = None                 # "YYYY-MM-DD HH:MM" first hour of the current excursion
    notes: list[str] = field(default_factory=list)
    expected_by: str | None = None           # planned event that explains the move (innocent change)
    data_problems: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    segment: dict | None = None              # {"country": "NL"} for a segment cell, None for the KPI total

    @property
    def name(self) -> str:
        return self.status.name


# ---------------------------------------------------------------- config

def _settings(cfg: dict) -> dict:
    d = cfg.get("defaults", {})
    return {x: d[x] for x in ("window_h", "watch_z", "alert_z", "min_dev_pct", "series_h") if x in d}


def load_kpis(path: str | Path | None = None, reg=None) -> list[KpiDef]:
    cfg = yaml.safe_load(Path(path or KPIS_PATH).read_text(encoding="utf-8"))
    out = []
    for k in cfg["kpis"]:
        kd = KpiDef(**{**_settings(cfg), **k})
        if reg is not None and kd.name not in reg:
            raise KeyError(f"kpis.yaml: {kd.name!r} is not a metric in the registry")
        out.append(kd)
    return out


def headline_kpis(w, choices: dict | None = None) -> list[KpiDef]:
    """The KPIs of kpis.yaml adjusted by the CEO's choices (pulse.choices): untracked ones leave, pinned metrics join
    with the default settings (a kpis.yaml KPI pinned again gets its own back). Pinned names that are not a metric in
    this world are skipped."""
    from pulse import choices as CH
    ch = CH.load() if choices is None else choices
    out = [k for k in load_kpis(reg=w.reg) if k.name not in ch["untracked"]]
    extra = [m for m in ch["pinned"] if m not in ch["untracked"] and m not in {k.name for k in out}]
    if any(m not in w.reg for m in extra):
        from pulse.graph_api import ensure_applied
        ensure_applied(w)   # a metric the graph handler added in this state dir
    return out + [pinned_kpi(w, m) for m in extra if m in w.reg]


def pinned_kpi(w, metric: str) -> KpiDef:
    """A CEO-pinned metric with the kpis.yaml defaults; flow metrics are reported per hour like the KPIs."""
    from pulse.graph_api import _label
    flow = w.engine.shape(metric).additive and w.reg[metric].time_agg == "sum"
    cfg = yaml.safe_load(KPIS_PATH.read_text(encoding="utf-8"))
    return KpiDef(**_settings(cfg), name=metric, label=_label(metric) + (" / hour" if flow else ""), pinned=True)


# ---------------------------------------------------------------- pure logic

def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))


def bad_direction_z(z: float | None, polarity: str | None) -> float:
    """z expressed so that positive = moving in the unwanted direction."""
    if not _finite(z):
        return 0.0
    if polarity == "down_is_good":
        return float(z)
    if polarity == "neutral":
        return abs(float(z))
    return -float(z)                          # up_is_good (default)


def classify(z: float | None, dev_pct: float | None, polarity: str | None, watch_z: float = 2.5,
             alert_z: float = 4.0, min_dev_pct: float = 3.0) -> tuple[str, str]:
    """(severity, direction). Only the unwanted direction alerts; neutral metrics never exceed 'watch'."""
    if not _finite(z):
        return "ok", "flat"
    dev = float(dev_pct) if _finite(dev_pct) else 0.0
    direction = "flat" if abs(z) < 1.0 or abs(dev) < 0.5 else ("up" if dev > 0 else "down")
    b = bad_direction_z(z, polarity)
    if b >= alert_z and abs(dev) >= min_dev_pct:
        sev = "watch" if polarity == "neutral" else "alert"
    elif b >= watch_z:
        sev = "watch"
    else:
        sev = "ok"
    return sev, direction


def find_onset(bad_z: pd.Series, now: pd.Timestamp, threshold: float, lookback_h: int = ONSET_LOOKBACK_H):
    """First hour of the run of consecutive 'bad' hours that ends at `now` (one-hour gaps tolerated)."""
    s = bad_z[(bad_z.index <= now) & (bad_z.index > now - pd.Timedelta(hours=lookback_h))]
    if s.empty or not (s.iloc[-1] >= threshold):
        return None
    onset, gap = s.index[-1], 0
    for ts in reversed(s.index[:-1]):
        if s[ts] >= threshold:
            onset, gap = ts, 0
        else:
            gap += 1
            if gap > 1:
                break
    return onset


# ---------------------------------------------------------------- data helpers (cached per world+hour)

def _cache(w) -> dict:
    return w.wh.__dict__.setdefault("_pulse_monitor_cache", {})


def _verdicts(w, now):
    c = _cache(w)
    key = ("trust", now)
    if key not in c:
        from argus.analytics import trust
        try:
            c[key] = trust.assess(w.wh, now)
        except Exception:
            c[key] = {}
    return c[key]


def _completeness(w, now, sources: list[str]) -> float | None:
    df = w.wh.frames.get("completeness")
    if df is None or not sources:
        return None
    ts = pd.to_datetime(df["ts"])
    sub = df[(ts == now) & df["source"].isin(sources)]
    if sub.empty:
        return None
    return float(sub["completeness"].min())


def _exclude_days(w, now, sources) -> set:
    from argus.analytics import trust
    try:
        bd = trust.bad_days(w.wh, now, set(sources))
    except Exception:
        return set()
    return set().union(*bd.values()) if bd else set()


def _frame(w, metric: str, now, window_h: int, hours: int, exclude: set):
    from argus.analytics.baseline import expected_frame, window_frame
    W, V, idx = window_frame(w.engine, metric, (), now, window_h, hours)
    if W.empty or "_total" not in W.columns:
        return None
    E = expected_frame(w.engine, metric, W, V, exclude or None)
    if not E:
        return None
    return {k: E[k]["_total"] for k in ("actual", "expected", "sigma", "z", "rel")}


def _point(w, metric: str, now, window_h: int) -> tuple[float, float]:
    """(z, rel) of a metric at now (used for numerator / denominator / net-outcome checks)."""
    c = _cache(w)
    key = ("pt", metric, now, window_h)
    if key not in c:
        try:
            f = _frame(w, metric, now, window_h, 24 * 7 + window_h + 2, set())
            c[key] = (float(f["z"].get(now, np.nan)), float(f["rel"].get(now, np.nan))) if f else (np.nan, np.nan)
        except Exception:
            c[key] = (np.nan, np.nan)
    return c[key]


def _scope(row: dict, dims: tuple) -> dict:
    return {d: str(row[d]) for d in dims if str(row.get(d) or "*") not in ("*", "nan", "None", "")}


def planned_events(w, now: pd.Timestamp, onset: pd.Timestamp | None, kpi: str, window_h: int = 1) -> list[dict]:
    """Announced calendar entries and planned non-release events that overlap the KPI window ending at `now`."""
    out = []
    t0 = (onset or now) - pd.Timedelta(hours=3)
    w_start = now - pd.Timedelta(hours=window_h - 1)   # an event that ended inside the window still shapes it
    try:
        ups = w.graph.upstream(kpi)
    except Exception:
        ups = set()
    cal = w.wh.frames.get("calendar")
    if cal is not None:
        for r in cal.to_dict("records"):
            try:
                s, e = pd.Timestamp(r["start"]), pd.Timestamp(r["end"])
            except Exception:
                continue
            if not (s <= now and e > w_start):
                continue
            metrics = [m.strip() for m in str(r.get("metrics") or "").split(";") if m.strip() and m != "nan"]
            if not metrics:
                continue   # "no customer impact" entries explain nothing
            out.append({"name": r["name"], "kind": r.get("kind"), "source": "calendar", "scope": _scope(r, ("country",)),
                        "lists_kpi": kpi in metrics, "upstream": [m for m in metrics if m in ups],
                        "near_onset": onset is None or s - pd.Timedelta(hours=1) <= onset <= s + pd.Timedelta(hours=3),
                        "low": r.get("expected_low"), "high": r.get("expected_high")})
    ev = w.wh.view("events") if w.wh.has("events") else None
    if ev is not None and not ev.empty:
        for r in ev.to_dict("records"):
            if str(r.get("type")) not in INNOCENT_EVENT_TYPES or str(r.get("planned")).lower() != "true":
                continue
            try:
                s = pd.Timestamp(r["ts"])
            except Exception:
                continue
            end = r.get("end")   # parsed to a Timestamp by the warehouse; empty = open-ended (assume 24 h)
            e = pd.Timestamp(end) if end is not None and not pd.isna(end) and str(end).strip() else s + pd.Timedelta(hours=24)
            if s <= now and e > w_start and s >= t0 - pd.Timedelta(hours=48):
                out.append({"name": r["name"], "kind": r.get("type"), "source": "events",
                            "scope": _scope(r, ("country", "platform", "category")),
                            "lists_kpi": False, "low": None, "high": None, "near_onset": s >= t0})
    return out


# ---------------------------------------------------------------- per KPI

def _scale(w, metric: str, window_h: int) -> float:
    spec = w.reg[metric]
    sh = w.engine.shape(metric)
    return 1.0 / window_h if (sh.additive and spec.time_agg == "sum") else 1.0


def _r(x, nd=6):
    return round(float(x), nd) if _finite(x) else None


def evaluate_one(w, kd: KpiDef, now: pd.Timestamp, window_h: int | None = None) -> Evaluation:
    spec = w.reg[kd.name]
    wh_ = int(window_h or kd.window_h or spec.window_h)
    polarity = kd.polarity or spec.polarity
    sources = sorted(w.engine.sources(kd.name))
    st = KpiStatus(name=kd.name, label=kd.label, unit=spec.unit, polarity=polarity)
    ev = Evaluation(status=st, kpi=kd, sources=sources)
    st.completeness = _completeness(w, now, sources)

    exclude = _exclude_days(w, now, sources)
    f = _frame(w, kd.name, now, wh_, kd.series_h + 24 * 7 + wh_, exclude)
    scale = _scale(w, kd.name, wh_)
    bz = None
    if f is not None:
        a, e, s, z, rel = (f[k] for k in ("actual", "expected", "sigma", "z", "rel"))
        tail = a.index[a.index > now - pd.Timedelta(hours=kd.series_h)]
        st.series = [SeriesPoint(ts=f"{t:%Y-%m-%d %H:%M}", value=_r(a[t] * scale), expected=_r(e[t] * scale),
                                 lo=_r((e[t] - 3 * s[t]) * scale), hi=_r((e[t] + 3 * s[t]) * scale)) for t in tail]
        if now in a.index:
            st.value, st.expected = _r(a[now] * scale), _r(e[now] * scale)
            st.z = _r(z[now], 2)
            st.deviation_pct = _r(rel[now] * 100, 1)
        bz = z.map(lambda v: bad_direction_z(v, polarity))
        onset = find_onset(bz, now, kd.watch_z)
        ev.onset = f"{onset:%Y-%m-%d %H:%M}" if onset is not None else None
    sev, st.direction = classify(st.z, st.deviation_pct, polarity, kd.watch_z, kd.alert_z, kd.min_dev_pct)
    recent = bz[bz.index <= now].tail(SUSTAIN_H) if bz is not None else []
    if (sev == "watch" and polarity != "neutral" and len(recent) == SUSTAIN_H and (recent >= kd.watch_z).all()
            and abs(st.deviation_pct or 0) >= kd.min_dev_pct):   # same guards as classify: never neutral, a real move
        sev = "alert"
        ev.notes.append(f"sustained: off in the unwanted direction for {SUSTAIN_H}+ hours")
    ev.bad_z = bad_direction_z(st.z, polarity)
    ev.raw_severity = sev

    # ---- data quality: a late / stale / frozen source makes the number untrustworthy
    verdicts = _verdicts(w, now)
    for src in sources:
        v = verdicts.get(src)
        if v is not None and not v.ok:
            ev.data_problems.append(f"{src} {v.status}: {v.reason}")
    if st.completeness is not None and st.completeness < 0.9 and not ev.data_problems:
        ev.data_problems.append(f"only {st.completeness:.0%} of expected rows arrived")
    if st.value is None:
        ev.data_problems.append("no data for this hour")
    if ev.data_problems:
        sev = "data_issue"
        ev.notes.append("data issue: " + "; ".join(ev.data_problems))

    st.severity = _apply_plans(w, kd, now, wh_, polarity, sev, st, ev)
    st.onset_ts = ev.onset
    st.note = "; ".join(ev.notes)
    return ev


def _apply_plans(w, kd: KpiDef, now, wh_: int, polarity, sev: str, st: KpiStatus, ev: Evaluation,
                 segment: dict | None = None) -> str:
    """Innocent changes: a planned event explains the move and the business outcome is healthy -> alert becomes
    watch. For a segment cell an event scoped to another value of the same dim explains nothing; an event scoped on
    another dim must have its move concentrated where the event is (same check as for the totals)."""
    if sev not in ("alert", "watch") or st.direction == "flat":
        return sev
    onset_ts = pd.Timestamp(ev.onset) if ev.onset else None
    plans = planned_events(w, now, onset_ts, kd.name, wh_)
    explained = None
    if plans:
        net_ok = _net_outcome_ok(w, now, wh_, kd.name)
        rel = (st.deviation_pct or 0) / 100
        for p in plans:
            if p["lists_kpi"] and _finite(p["low"]) and _finite(p["high"]):
                ok = float(p["low"]) <= rel <= float(p["high"])
                if not ok:
                    ev.notes.append(f"{p['name']} is planned but {rel:+.0%} is outside its expected "
                                    f"{float(p['low']):+.0%}..{float(p['high']):+.0%}")
            else:   # healthy net outcome, or an announced change to an input of this KPI that started with it
                ok = net_ok or bool(p.get("upstream") and p.get("near_onset"))
            for dim, val in p["scope"].items() if ok else ():
                if segment and dim in segment:
                    if str(segment[dim]) != str(val):
                        ev.notes.append(f"{p['name']} is limited to {val}, not {segment[dim]}: not dismissed")
                        ok = False
                        break
                    continue   # the event is in exactly this segment
                other = _scope_conflict(w, kd.name, now, wh_, polarity, dim, val)
                if other:
                    ev.notes.append(f"{p['name']} is limited to {val}, but {other}: not dismissed")
                    ok = False
                    break
            if ok:
                explained = p
                break
        rest = [p["name"] for p in plans if not any(p["name"] in n for n in ev.notes)]
        if not explained and rest:
            ev.notes.append("planned: " + ", ".join(rest) +
                            ("" if net_ok else " (but orders / revenue are down too, so not dismissed)"))
    dn = "" if segment else _dilution_note(w, now, wh_, kd.name)
    if explained:
        ev.expected_by = explained["name"]
        if sev == "alert":
            sev = "watch"
        via = (f" (it moves {', '.join(explained['upstream'])}, upstream of {kd.name}; alert if it persists "
               "after the event)") if explained.get("upstream") and not explained.get("lists_kpi") else ""
        ev.notes.append(f"expected: {explained['name']}{via}" + dn)
    elif sev == "alert" and dn:
        ev.notes.append("possible mix shift" + dn + " - no planned event explains it")
    return sev


def _worst_other(bz: dict, rel: dict, val: str) -> str | None:
    """Another segment clearly off in the unwanted direction too (bad z >= SEGMENT_Z) or as far off as `val`."""
    worst = None
    for name, b in bz.items():
        if name != val and (b >= SEGMENT_Z or b >= max(SEGMENT_Z_SAME, 0.75 * bz[val])) and (worst is None or b > worst[1]):
            worst = (name, b, rel[name])
    return f"{worst[0]} is also {worst[2]:+.0%} (z {worst[1]:.1f})" if worst else None


def _scope_conflict(w, kpi, now, window_h, polarity, dim, val) -> str | None:
    """Why an event limited to `dim`=`val` cannot explain the KPI move: another segment is clearly off in the
    unwanted direction too (a competing cause) or as far off as the event's segment (the move is not concentrated
    where the event is), or the KPI cannot be split by `dim` at all. None = no conflict."""
    from argus.analytics.baseline import expected_frame, window_frame
    try:
        W, V, _ = window_frame(w.engine, kpi, (dim,), now, window_h, 24 * 7 + window_h + 2)
        E = expected_frame(w.engine, kpi, W, V)
    except Exception:
        E = {}
    segs = {str(c[0] if isinstance(c, tuple) else c): c for c in W.columns} if E else {}
    if val not in segs:
        return f"{kpi} cannot be split by {dim}"
    bz = {name: bad_direction_z(E["z"][col].get(now, np.nan), polarity) for name, col in segs.items()}
    return _worst_other(bz, {name: float(E["rel"][col].get(now, np.nan)) for name, col in segs.items()}, val)


def _net_outcome_ok(w, now, window_h, kpi) -> bool:
    """Net business outcome not hurt: orders and revenue are not significantly down (and the KPI is not one of them)."""
    if kpi in ("orders", "gmv"):
        return False
    for m in ("orders", "gmv"):
        if m not in w.reg:
            continue
        z, _ = _point(w, m, now, window_h)
        if _finite(z) and z <= -2.0:
            return False
    return True


def _dilution_note(w, now, window_h, kpi) -> str:
    spec = w.reg[kpi]
    if spec.type != "ratio" or not spec.numerator or not spec.denominator:
        return ""
    zn, rn = _point(w, spec.numerator, now, window_h)
    zd, rd = _point(w, spec.denominator, now, window_h)
    if _finite(zd) and zd >= 2.0 and (not _finite(zn) or zn > -2.0):
        move = lambda r: "flat" if abs(r) < 0.01 else f"{'up' if r > 0 else 'down'} {abs(r):.0%}"
        return (f" ({spec.denominator.replace('_', ' ')} {move(rd)} while {spec.numerator.replace('_', ' ')} "
                f"{move(rn)}, so more low-intent traffic dilutes the rate)")
    return ""


# ---------------------------------------------------------------- segment cells (budgeted)

def segment_key(kpi: str, segment: dict | None) -> str:
    """'payment_success_rate|country=NL' (same id as the interpreter's segment nodes); the KPI name for a total."""
    return kpi + "".join(f"|{k}={segment[k]}" for k in sorted(segment)) if segment else kpi


def segment_label(label: str, segment: dict) -> str:
    return f"{label} in {', '.join(str(v) for v in segment.values())}"


def _shares(w, kpi: str, now) -> list[tuple[str, str, float]]:
    """(dim, value, share of the KPI's volume over the last 7 days) for its single-dim cells (cached per day)."""
    c = _cache(w)
    key = ("shares", kpi, now.normalize())
    if key not in c:
        out = []
        col = "den" if w.engine.shape(kpi).ratio_like else "value"
        for dim in SEG_DIMS:
            if dim not in w.engine.dims(kpi):
                continue
            df = w.engine.hourly(kpi, (dim,), None, now - pd.Timedelta(days=7), now)
            vol = df.groupby(dim, observed=True)[col].sum().abs() if not df.empty else pd.Series(dtype=float)
            tot = float(vol.sum())
            out += [(dim, str(v), float(x) / tot) for v, x in vol.items() if tot > 0]
        c[key] = out
    return c[key]


def _seg_frame(w, metric: str, dim: str, now, window_h: int, hours: int, exclude: set) -> dict:
    from argus.analytics.baseline import expected_frame, window_frame
    W, V, _ = window_frame(w.engine, metric, (dim,), now, window_h, hours)
    return (expected_frame(w.engine, metric, W, V, exclude or None) or {}) if not W.empty else {}


def evaluate_cell(w, kd: KpiDef, dim: str, val: str, now, total: Evaluation, frames: dict | None = None) -> Evaluation:
    """One segment cell (kd.name where dim = val) with the totals' baseline and stricter alert rules."""
    frames = {} if frames is None else frames
    spec = w.reg[kd.name]
    wh_ = int(kd.window_h or spec.window_h)
    polarity = total.status.polarity
    seg = {dim: val}
    st = KpiStatus(name=kd.name, label=segment_label(kd.label, seg), unit=spec.unit, polarity=polarity)
    ev = Evaluation(status=st, kpi=kd, sources=total.sources, segment=seg)
    if (kd.name, dim) not in frames:
        frames[(kd.name, dim)] = _seg_frame(w, kd.name, dim, now, wh_, kd.series_h + 24 * 7 + wh_,
                                            _exclude_days(w, now, total.sources))
    E, col = frames[(kd.name, dim)], (val,)
    if not E or col not in E["z"].columns or now not in E["z"].index or not _finite(E["actual"][col].get(now)):
        st.note = "no data for this segment"
        return ev
    scale = _scale(w, kd.name, wh_)
    a, e, z, rel = (E[k][col] for k in ("actual", "expected", "z", "rel"))
    st.value, st.expected = _r(a[now] * scale), _r(e[now] * scale)
    st.z, st.deviation_pct = _r(z[now], 2), _r(rel[now] * 100, 1)
    bz = z.map(lambda v: bad_direction_z(v, polarity))
    onset = find_onset(bz, now, kd.watch_z)
    ev.onset = f"{onset:%Y-%m-%d %H:%M}" if onset is not None else None
    min_dev = SEG_DEV_FACTOR * kd.min_dev_pct
    sev, st.direction = classify(st.z, st.deviation_pct, polarity, kd.watch_z, SEG_ALERT_Z, min_dev)
    if sev == "alert" and not bz.get(now - pd.Timedelta(hours=1), 0.0) >= SEG_ALERT_Z:
        sev = "watch"                                   # one bad hour is not enough for a segment
    recent = bz[bz.index <= now].tail(SUSTAIN_H)
    if (sev == "watch" and polarity != "neutral" and len(recent) == SUSTAIN_H and (recent >= kd.watch_z).all()
            and abs(st.deviation_pct or 0) >= min_dev):
        sev = "alert"
        ev.notes.append(f"sustained: off in the unwanted direction for {SUSTAIN_H}+ hours")
    ev.bad_z = bad_direction_z(st.z, polarity)
    vol_all = float(np.nansum(E["volume"].loc[now].to_numpy(dtype=float)))
    if sev == "alert" and vol_all > 0 and float(E["volume"][col].get(now, 0.0)) / vol_all < SEG_MIN_SHARE:
        sev = "watch"
        ev.notes.append(f"too little volume in {val} to alert")
    if sev == "alert":   # the move must sit in this cell: no sibling segment of the same dim as far off
        sibs = {str(c[0]): c for c in E["z"].columns}
        other = _worst_other({n: bad_direction_z(E["z"][c].get(now, np.nan), polarity) for n, c in sibs.items()},
                             {n: float(E["rel"][c].get(now, np.nan)) for n, c in sibs.items()}, val)
        if other:
            sev = "watch"
            ev.notes.append(f"not concentrated in {val}: {other}")
    ev.raw_severity = sev
    st.severity = _apply_plans(w, kd, now, wh_, polarity, sev, st, ev, seg)
    st.onset_ts = ev.onset
    st.note = "; ".join(ev.notes)
    return ev


def evaluate_segments(w, kpis: list[KpiDef], evals: list[Evaluation], state: dict, budget: int | None = None,
                      force=()) -> list[Evaluation]:
    """Check a budget of single-dimension cells of the KPIs at w.now. Rank = volume share x (1 + trouble) x hours
    since the cell was last checked; trouble = the cell's bad z at its last check or its KPI total's bad z now.
    Cells in `force` (open segment incidents) are always checked. Cells of a KPI whose total is a data issue are not
    judged (a forced one is returned as data_issue so its incident is held). `state` {cell key: {"checked", "trouble"}}
    is updated in place (the orchestrator persists it)."""
    budget = SEG_BUDGET if budget is None else budget
    now = w.now
    by = {e.name: e for e in evals}
    cands, out = [], []
    for kd in kpis:
        tot = by.get(kd.name)
        if tot is None or kd.name not in w.reg:
            continue
        try:
            cells = _shares(w, kd.name, now)
        except Exception:
            continue
        for dim, val, share in cells:
            key = segment_key(kd.name, {dim: val})
            if tot.status.severity == "data_issue":
                if key in force:
                    st = KpiStatus(name=kd.name, label=segment_label(kd.label, {dim: val}), severity="data_issue")
                    out.append(Evaluation(status=st, kpi=kd, raw_severity="data_issue", segment={dim: val}))
                continue
            s = state.get(key) or {}
            hours = (now - pd.Timestamp(s["checked"])) / pd.Timedelta(hours=1) if s.get("checked") else SEG_NEVER_H
            trouble = max(float(s.get("trouble") or 0.0), tot.bad_z, 0.0)
            cands.append((key not in force, -share * (1 + trouble) * hours, key, kd, dim, val, tot))
    cands.sort(key=lambda c: c[:2])
    frames: dict = {}
    for forced_not, _, key, kd, dim, val, tot in cands[:max(budget, sum(not c[0] for c in cands))]:
        try:
            ev = evaluate_cell(w, kd, dim, val, now, tot, frames)
        except Exception as e:   # one broken cell never stops the tick
            st = KpiStatus(name=kd.name, label=segment_label(kd.label, {dim: val}),
                           note=f"segment check failed: {type(e).__name__}: {e}")
            ev = Evaluation(status=st, kpi=kd, segment={dim: val})
        out.append(ev)
        state[key] = {"checked": f"{now:%Y-%m-%d %H:%M}", "trouble": round(max(ev.bad_z, 0.0), 2)}
    return out


# ---------------------------------------------------------------- public API

def evaluate_ex(w, kpis: list[KpiDef] | None = None, now=None, window_h: int | None = None) -> list[Evaluation]:
    """Evaluate every KPI at `now` (moves the world clock if given). window_h=None -> per-KPI setting (default 3 h)."""
    kpis = kpis or load_kpis(reg=w.reg)
    if now is not None and pd.Timestamp(now).floor("h") != w.now:
        w.set_now(now)
    now = w.now
    out = []
    for kd in kpis:
        try:
            out.append(evaluate_one(w, kd, now, window_h))
        except Exception as e:   # one broken KPI never stops the tick (the note keeps the error visible)
            import traceback
            where = traceback.extract_tb(e.__traceback__)[-1]
            msg = f"evaluation failed: {type(e).__name__}: {e} [at {Path(where.filename).name}:{where.lineno}]"
            st = KpiStatus(name=kd.name, label=kd.label, severity="data_issue")
            out.append(Evaluation(status=st, kpi=kd, raw_severity="data_issue", notes=[msg], data_problems=[msg]))
    return out


def evaluate(w, kpis: list[KpiDef] | None = None, now=None, window_h: int | None = None) -> list[KpiStatus]:
    return [e.status for e in evaluate_ex(w, kpis, now, window_h)]
