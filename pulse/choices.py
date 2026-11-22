"""The CEO's choices of headline metrics: one saved set that every run uses (the recorded run, the live market,
the CLI), kept outside the state dirs so it survives restarts and `python -m pulse reset`.

    choices.json at the repo root (git-ignored), or the file named by PULSE_CHOICES
    {"pinned": ["refund_rate"], "untracked": ["ad_revenue"]}

pinned     metrics the CEO added to the briefing (full monitoring, like the KPIs in pulse/kpis.yaml)
untracked  metrics the CEO moved to the background (no own alerts; investigations still walk through them)
Headline set = kpis.yaml + pinned, minus untracked (pulse.monitor.headline_kpis). Untracking a pinned metric moves
it from pinned to untracked; pinning or restoring an untracked metric brings it back. The scorecard (pulse.evaluate)
ignores this file.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KPIS_PATH = Path(__file__).with_name("kpis.yaml")
METRIC_ID = re.compile(r"^[a-z][a-z0-9_]*$")   # registry ids; release, segment and data-quality nodes never match
ACTIONS = ("pin", "untrack", "restore")


def path() -> Path:
    return Path(os.environ.get("PULSE_CHOICES") or ROOT / "choices.json")


def kpi_labels() -> dict[str, str]:
    """The headline KPIs of pulse/kpis.yaml: name -> label."""
    import yaml
    cfg = yaml.safe_load(KPIS_PATH.read_text(encoding="utf-8"))
    return {k["name"]: k.get("label") or k["name"] for k in cfg["kpis"]}


def load() -> dict:
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    lists = {k: d.get(k) if isinstance(d.get(k), list) else [] for k in ("pinned", "untracked")}
    return {k: list(dict.fromkeys(m for m in v if isinstance(m, str) and METRIC_ID.match(m))) for k, v in lists.items()}


def save(ch: dict) -> dict:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(ch, indent=2), encoding="utf-8")
    tmp.replace(p)
    return ch


def apply(action: str, metric: str) -> dict:
    """pin / restore: the metric is on the briefing; untrack: it moves to the background. Returns the new choices."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if not isinstance(metric, str) or not METRIC_ID.match(metric):
        raise ValueError("only metrics can be pinned or untracked")
    ch = load()
    if action == "untrack":
        ch["pinned"] = [m for m in ch["pinned"] if m != metric]
        if metric not in ch["untracked"]:
            ch["untracked"].append(metric)
    else:
        ch["untracked"] = [m for m in ch["untracked"] if m != metric]
        if metric not in kpi_labels() and metric not in ch["pinned"]:
            ch["pinned"].append(metric)
    return save(ch)
