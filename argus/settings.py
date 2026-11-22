"""Paths of the metric library: data, config and the learned graph base. Overridable through environment variables."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "argus"
DATA_DIR = Path(os.environ.get("ARGUS_DATA", ROOT / "data" / "eye"))
STATE_DIR = Path(os.environ.get("ARGUS_STATE", ROOT / ".argus"))
CONFIG_DIR = PKG / "config"
METRICS_DIR = CONFIG_DIR / "metrics"
# metrics and aliases learned on top of config/metrics (read on every load, part of the metric graph)
ADDED_METRICS = STATE_DIR / "added_metrics.yaml"
ADDED_ALIASES = STATE_DIR / "aliases.json"


def write_atomic(path: Path, text: str, retries: int = 8) -> None:
    """Write via a temp file + rename, retrying briefly: the dashboard reads these files while ticks write them,
    and synced folders (OneDrive) or scanners can hold a file for a moment on Windows."""
    import time
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    for i in range(retries):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError:
            if i == retries - 1:
                raise
            time.sleep(0.15 * (i + 1))


def state(*parts: str) -> Path:
    p = STATE_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
