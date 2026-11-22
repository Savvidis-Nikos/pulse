"""Loads argus/config/policy.yaml once."""
from __future__ import annotations

from functools import lru_cache

import yaml

from argus.settings import CONFIG_DIR


@lru_cache(maxsize=1)
def policy() -> dict:
    return yaml.safe_load((CONFIG_DIR / "policy.yaml").read_text(encoding="utf-8"))
