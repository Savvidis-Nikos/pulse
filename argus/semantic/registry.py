"""Metric registry: typed metric specs (MetricFlow-style taxonomy) loaded from argus/config/metrics/*.yaml.

Contributors describe *what* a metric is (definition, expression or query). Where it sits in the graph,
which formulas connect it, which dimensions it supports and which components can move it are derived by the
graph builder and the cartographer, not declared.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from argus.settings import ADDED_ALIASES, ADDED_METRICS, METRICS_DIR, write_atomic

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
Tier = Literal["north_star", "outcome", "driver", "health"]


class MetricSpec(BaseModel):
    name: str
    description: str = ""
    type: Literal["simple", "ratio", "derived", "sql"]
    # simple: <agg>(<column>) on <table> (alt_tables can serve finer breakdowns, e.g. audience for browser)
    table: str | None = None
    column: str | None = None
    agg: Literal["sum", "mean", "max", "min"] = "sum"
    where: str | None = None                     # optional pandas query on the table rows
    alt_tables: list[str] = Field(default_factory=list)
    # ratio: numerator / denominator (metric names) -> ratio of sums, never an average of ratios
    numerator: str | None = None
    denominator: str | None = None
    # derived: arithmetic over metric names (SymPy-parsed), e.g. "gmv - discounts - cogs - ad_cost"
    expr: str | None = None
    # sql: SELECT over v.* returning ts, [dims...], value  (or num, den for a ratio)
    sql: str | None = None
    # semantics
    unit: str = "count"
    domain: str = "other"
    tier: Tier = "driver"
    polarity: Literal["up_is_good", "down_is_good", "neutral"] = "neutral"
    time_agg: Literal["sum", "mean", "last"] = "sum"   # flows sum over hours; stocks average; daily-cumulative take last
    window_h: int = 3                                  # detection window
    min_volume: float = 50.0                           # detection volume floor (denominator / count)
    min_abs_change: float = 0.0                        # detection: smallest absolute move worth a page
    watch: bool = True
    components: list[str] = Field(default_factory=list)  # optional hints; the graph also derives them
    aliases: list[str] = Field(default_factory=list)
    added_by: str = "seed"
    added_at: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not NAME_RE.match(v):
            raise ValueError(f"metric name must be snake_case: {v!r}")
        return v

    @model_validator(mode="after")
    def _fields(self):
        need = {"simple": ("table", "column"), "ratio": ("numerator", "denominator"),
                "derived": ("expr",), "sql": ("sql",)}[self.type]
        missing = [f for f in need if not getattr(self, f)]
        if missing:
            raise ValueError(f"{self.name}: type {self.type} needs {missing}")
        return self

    @property
    def is_ratio(self) -> bool:
        return self.type == "ratio"

    def references(self) -> set[str]:
        """Metric names this metric is defined in terms of."""
        if self.type == "ratio":
            return {self.numerator, self.denominator}
        if self.type == "derived":
            return set(re.findall(r"[a-z][a-z0-9_]*", self.expr or "")) - {"sqrt", "log", "exp"}
        return set()


class Registry:
    def __init__(self, metrics: dict[str, MetricSpec]):
        self.metrics = metrics

    # ------------------------------------------------------------------ load / save
    @classmethod
    def load(cls, metrics_dir: Path = METRICS_DIR) -> "Registry":
        metrics: dict[str, MetricSpec] = {}
        files = sorted(p for p in metrics_dir.glob("*.yaml") if p.name != "added.yaml")
        if ADDED_METRICS.exists():
            files.append(ADDED_METRICS)
        for p in files:
            for item in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
                spec = MetricSpec(**item)
                if spec.name in metrics:
                    raise ValueError(f"duplicate metric {spec.name} in {p.name}")
                metrics[spec.name] = spec
        reg = cls(metrics)
        if ADDED_ALIASES.exists():
            import json
            for alias, canon in json.loads(ADDED_ALIASES.read_text(encoding="utf-8")).items():
                if canon in metrics and alias not in metrics[canon].aliases:
                    metrics[canon].aliases.append(alias)
        reg.check()
        return reg

    def check(self):
        for m in self.metrics.values():
            unknown = [r for r in m.references() if r not in self.metrics]
            if unknown:
                raise ValueError(f"{m.name} references unknown metrics {unknown}")

    def add(self, spec: MetricSpec, persist: bool = True):
        self.metrics[spec.name] = spec
        if persist:
            items = yaml.safe_load(ADDED_METRICS.read_text(encoding="utf-8")) if ADDED_METRICS.exists() else []
            items = [i for i in (items or []) if i.get("name") != spec.name]
            items.append(spec.model_dump(exclude_defaults=True, exclude_none=True) | {"name": spec.name, "type": spec.type})
            write_atomic(ADDED_METRICS, yaml.safe_dump(items, sort_keys=False, allow_unicode=True))

    def add_alias(self, alias: str, canonical: str, persist: bool = True):
        if alias not in self.metrics[canonical].aliases:
            self.metrics[canonical].aliases.append(alias)
        if persist:
            import json
            cur = json.loads(ADDED_ALIASES.read_text(encoding="utf-8")) if ADDED_ALIASES.exists() else {}
            cur[alias] = canonical
            write_atomic(ADDED_ALIASES, json.dumps(cur, indent=1))

    def remove(self, name: str):
        self.metrics.pop(name, None)
        self.__dict__.pop("_expand_cache", None)

    # ------------------------------------------------------------------ access
    def __getitem__(self, name: str) -> MetricSpec:
        return self.metrics[name]

    def __contains__(self, name: str) -> bool:
        return name in self.metrics

    def __iter__(self):
        return iter(self.metrics.values())

    def names(self) -> list[str]:
        return list(self.metrics)

    def resolve(self, name: str) -> str:
        """Metric name or alias -> canonical name."""
        if name in self.metrics:
            return name
        for m in self.metrics.values():
            if name in m.aliases:
                return m.name
        raise KeyError(name)

    def by_tier(self, *tiers: str) -> list[MetricSpec]:
        return [m for m in self.metrics.values() if m.tier in tiers]
