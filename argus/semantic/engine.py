"""Metric engine: turns any metric spec into clocked hourly series, per segment and breakdown.

Every result is a frame [ts, *by, num, den, value]:
  - additive metrics (simple sums, linear derived): value = num, den = NaN
  - ratio-like metrics (ratio, non-linear derived, sql num/den): value = num / den
Window values are always ratios of sums (never averages of ratios). Nothing after `wh.now` is ever returned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd
import sympy as sp

from argus.data.warehouse import Warehouse
from argus.semantic import expr as X
from argus.semantic.registry import MetricSpec, Registry

TIME_COLS = {"ts"}
VALUE_COLS = {"value", "num", "den"}


@dataclass
class Shape:
    """How a metric aggregates."""
    additive: bool           # sums across segments and hours
    ratio_like: bool         # value = num / den
    num: sp.Expr | None = None
    den: sp.Expr | None = None


class MetricEngine:
    def __init__(self, wh: Warehouse, reg: Registry):
        self.wh = wh
        self.reg = reg
        self._sql_cache: dict[str, pd.DataFrame] = {}
        self._cache: dict[tuple, pd.DataFrame] = {}
        self._dims: dict[str, list[str]] = {}
        self._shape: dict[str, Shape] = {}

    # ------------------------------------------------------------------ structure
    def refresh(self):
        """Drop caches (after the registry or the underlying data changed)."""
        self._sql_cache.clear()
        self._cache.clear()
        self._dims.clear()
        self._shape.clear()
        self.reg.__dict__.pop("_expand_cache", None)

    def shape(self, name: str) -> Shape:
        if name in self._shape:
            return self._shape[name]
        spec = self.reg[name]
        if spec.type == "simple":
            sh = Shape(additive=spec.agg == "sum" and spec.time_agg != "last", ratio_like=False)
        elif spec.type == "sql":
            cols = self._sql_frame(spec).columns
            rl = "num" in cols and "den" in cols
            sh = Shape(additive=not rl and spec.agg == "sum", ratio_like=rl)
        else:
            e = X.definition(spec, set(self.reg.names()))
            num, den = X.fraction_parts(e)
            if den == 1:
                comps_additive = all(self.shape(str(s)).additive for s in e.free_symbols)
                sh = Shape(additive=X.is_linear(e) and comps_additive, ratio_like=False, num=num, den=None)
            else:
                sh = Shape(additive=False, ratio_like=True, num=num, den=den)
        self._shape[name] = sh
        return sh

    def table_of(self, spec: MetricSpec, dims: set[str]) -> str | None:
        for t in [spec.table] + list(spec.alt_tables):
            if t and self.wh.has(t) and dims <= set(self.wh.specs[t].dims):
                return t
        return None

    def dims(self, name: str) -> list[str]:
        """Dimensions this metric can be broken down by."""
        if name in self._dims:
            return self._dims[name]
        spec = self.reg[name]
        if spec.type == "simple":
            out: list[str] = []
            for t in [spec.table] + list(spec.alt_tables):
                if t and self.wh.has(t) and spec.column in self.wh.columns(t):
                    out += [d for d in self.wh.specs[t].dims if d not in out]
        elif spec.type == "sql":
            out = [c for c in self._sql_frame(spec).columns if c not in TIME_COLS | VALUE_COLS]
        else:
            comps = [self.dims(r) for r in spec.references()]
            out = [d for d in comps[0] if all(d in c for c in comps[1:])] if comps else []
        self._dims[name] = out
        return out

    def tables(self, name: str) -> set[str]:
        spec = self.reg[name]
        if spec.type == "simple":
            return {spec.table} | set(spec.alt_tables)
        if spec.type == "sql":
            words = set(re.findall(r"\b[a-z_]+\b", spec.sql.lower()))
            return {t for t in self.wh.specs if t in words}
        return set().union(*[self.tables(r) for r in spec.references()]) if spec.references() else set()

    def sources(self, name: str) -> set[str]:
        """Data sources whose freshness this metric depends on."""
        spec = self.reg[name]
        if spec.type == "simple":
            s = self.wh.source_of(spec.table, spec.column)
            return {s} if s else set()
        if spec.type == "sql":
            return {self.wh.specs[t].source for t in self.tables(name) if self.wh.specs[t].source}
        return set().union(*[self.sources(r) for r in spec.references()]) if spec.references() else set()

    # ------------------------------------------------------------------ hourly series
    def hourly(self, name: str, by: tuple | list = (), segment: dict | None = None,
               start=None, end=None, upto=None) -> pd.DataFrame:
        """[ts, *by, num, den, value] for hours in [start, end] (end defaults to now).
        `upto` moves the clip point; only the analytics layer uses it, to precompute per-day matrices whose
        rows depend on past data only (rows after now are never read)."""
        now = pd.Timestamp(upto) if upto is not None else self.wh.now
        full = self._full(name, by, segment)
        hi = int(full["ts"].searchsorted(now, side="right")) if not full.empty else 0
        df = full.iloc[:hi]
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] <= pd.Timestamp(end)]
        return df

    def _full(self, name: str, by=(), segment: dict | None = None) -> pd.DataFrame:
        """Unclipped result. Every row is a per-hour aggregate, so clipping afterwards (hourly()) is equivalent
        to computing on clipped data. Internal use only: callers outside the engine go through hourly()."""
        by = tuple(by or ())
        segment = {k: v for k, v in (segment or {}).items() if v not in (None, "*", "all")}
        key = (name, by, tuple(sorted((k, tuple(v) if isinstance(v, (list, tuple, set)) else v)
                                      for k, v in segment.items())))
        full = self._cache.get(key)
        if full is None:
            full = self._compute(name, by, segment).reset_index(drop=True)
            if len(self._cache) > 20000:
                self._cache.clear()
            self._cache[key] = full
        return full

    def supports(self, name: str, dims) -> bool:
        return set(dims) <= set(self.dims(name))

    def _compute(self, name: str, by: tuple, segment: dict) -> pd.DataFrame:
        spec = self.reg[name]
        need = set(by) | set(segment)
        if not self.supports(name, need):
            raise KeyError(f"{name} cannot be broken down by {sorted(need - set(self.dims(name)))}")
        if spec.type == "simple":
            return self._simple(spec, by, segment)
        if spec.type == "sql":
            return self._sql(spec, by, segment)
        return self._formula(spec, by, segment)

    def _filter(self, df: pd.DataFrame, segment: dict) -> pd.DataFrame:
        m = np.ones(len(df), dtype=bool)
        for k, v in segment.items():
            if isinstance(v, (list, tuple, set)):
                m &= df[k].astype(str).isin([str(x) for x in v]).to_numpy()
            else:
                m &= (df[k].astype(str) == str(v)).to_numpy()
        return df[m]

    def _simple(self, spec: MetricSpec, by: tuple, segment: dict) -> pd.DataFrame:
        table = self.table_of(spec, set(by) | set(segment))
        if table is None:
            raise KeyError(f"no table serves {spec.name} by {by}/{list(segment)}")
        df = self.wh.frame(table)
        if spec.where:
            df = df.query(spec.where)
        df = self._filter(df, segment)
        g = df.groupby(["ts", *by], sort=True, observed=True)[spec.column]
        out = getattr(g, spec.agg)().rename("value").reset_index()
        out["num"] = out["value"]
        out["den"] = np.nan
        return out

    def _sql_frame(self, spec: MetricSpec) -> pd.DataFrame:
        if spec.name not in self._sql_cache:
            df = self.wh.sql_full(spec.sql)
            if "ts" not in df.columns:
                raise ValueError(f"{spec.name}: SQL must return a ts column")
            df["ts"] = pd.to_datetime(df["ts"])
            if not ({"value"} <= set(df.columns) or {"num", "den"} <= set(df.columns)):
                raise ValueError(f"{spec.name}: SQL must return value, or num and den")
            self._sql_cache[spec.name] = df
        return self._sql_cache[spec.name]

    def _sql(self, spec: MetricSpec, by: tuple, segment: dict) -> pd.DataFrame:
        df = self._filter(self._sql_frame(spec), segment)
        if {"num", "den"} <= set(df.columns):
            out = df.groupby(["ts", *by], sort=True)[["num", "den"]].sum().reset_index()
            with np.errstate(divide="ignore", invalid="ignore"):
                out["value"] = np.where(out["den"] != 0, out["num"] / out["den"], np.nan)
        else:
            agg = spec.agg if spec.agg in ("sum", "mean", "max", "min") else "sum"
            out = getattr(df.groupby(["ts", *by], sort=True)["value"], agg)().reset_index()
            out["num"] = out["value"]
            out["den"] = np.nan
        return out

    def _formula(self, spec: MetricSpec, by: tuple, segment: dict) -> pd.DataFrame:
        sh = self.shape(spec.name)
        e = X.definition(spec, set(self.reg.names()))
        keys = ["ts", *by]
        merged = None
        for s in sorted(e.free_symbols, key=str):
            part = self._full(str(s), by, segment)[keys + ["value"]].rename(columns={"value": str(s)})
            merged = part if merged is None else merged.merge(part, on=keys, how="outer")
        if merged is None:
            raise ValueError(f"{spec.name} has no inputs")
        merged = merged.sort_values(keys).reset_index(drop=True)
        cols = {c: merged[c].to_numpy(dtype=float) for c in merged.columns if c not in keys}
        out = merged[keys].copy()
        if sh.ratio_like:
            out["num"] = X.evaluate(sh.num, cols)
            out["den"] = X.evaluate(sh.den, cols)
            with np.errstate(divide="ignore", invalid="ignore"):
                out["value"] = np.where(out["den"] != 0, out["num"] / out["den"], np.nan)
        else:
            out["value"] = X.evaluate(e, cols)
            out["num"] = out["value"]
            out["den"] = np.nan
        return out

    # ------------------------------------------------------------------ windows
    def reduce(self, df: pd.DataFrame, name: str, by: tuple | list = ()) -> pd.DataFrame | float:
        """Aggregate an hourly frame over its hours (ratio of sums; time_agg sum | mean | last)."""
        spec = self.reg[name]
        sh = self.shape(name)
        by = list(by or [])
        if df.empty:
            return pd.DataFrame(columns=by + ["num", "den", "value", "hours"]) if by else float("nan")
        if spec.time_agg == "last":
            last = df["ts"].max()
            df = df[df["ts"] == last]

        def agg(g: pd.DataFrame) -> pd.Series:
            hours = g["ts"].nunique()
            if sh.ratio_like:
                n, d = g["num"].sum(), g["den"].sum()
                v = n / d if d else np.nan
            else:
                n = g["value"].sum()
                d = np.nan
                v = n if spec.time_agg == "sum" or spec.time_agg == "last" else g.groupby("ts")["value"].sum().mean()
            return pd.Series({"num": n, "den": d, "value": v, "hours": hours})

        if not by:
            return float(agg(df)["value"])
        return df.groupby(by, observed=True).apply(agg, include_groups=False).reset_index()

    def window(self, name: str, start, end, segment: dict | None = None, by=()) -> pd.DataFrame | float:
        return self.reduce(self.hourly(name, by, segment, start, end), name, by)

    def total_series(self, name: str, segment: dict | None = None, start=None, end=None) -> pd.Series:
        """Hourly value series indexed by ts (complete hourly index; missing hours are NaN)."""
        df = self.hourly(name, (), segment, start, end)
        s = df.set_index("ts")["value"]
        if s.empty:
            return s
        idx = pd.date_range(pd.Timestamp(start) if start is not None else s.index.min(),
                            pd.Timestamp(end) if end is not None else s.index.max(), freq="h")
        return s.reindex(idx)
