"""DuckDB warehouse over the simulator's data contract, with a clock and a read-only SQL guard.

- Every CSV in data/eye/ (except injected.json, the ground truth) is loaded once into `raw.<table>`.
- `set_now(ts)` (re)creates `v.<table>` views that only show rows whose time column is < now + 1h, so one
  process can replay many hours and nothing can look ahead.
- Pandas copies of the tables (`frame()`) feed the metric engine; `view()` returns the clocked pandas slice.
- `safe_sql()` is the agents' free-form query tool: SELECT/WITH only, v.* only, no file access, row cap,
  timeout. External file access is disabled after loading.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from argus.settings import CONFIG_DIR, DATA_DIR

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|copy|export|import|install|load|pragma|set|reset|"
    r"call|checkpoint|vacuum|begin|commit|rollback|grant|truncate|read_csv\w*|read_parquet|read_json\w*|read_text|"
    r"read_blob|glob|parquet_scan|csv_scan|getenv|duckdb_\w+|sniff_csv|httpfs|query_table|query)\b|\braw\s*\.",
    re.IGNORECASE)


class SqlRejected(ValueError):
    pass


@dataclass
class TableSpec:
    name: str
    file: str
    time: str | None
    kind: str
    dims: list
    source: str | None
    column_sources: dict
    panel: bool = False


def load_table_specs() -> tuple[dict[str, TableSpec], dict[str, list[str]]]:
    raw = yaml.safe_load((CONFIG_DIR / "tables.yaml").read_text(encoding="utf-8"))
    ts_cols = raw.pop("timestamp_columns", {})
    specs = {}
    for name, s in raw.items():
        specs[name] = TableSpec(name=name, file=s["file"], time=s.get("time"), kind=s.get("kind", "hourly"),
                                dims=list(s.get("dims", [])), source=s.get("source"),
                                column_sources=s.get("column_sources", {}) or {}, panel=bool(s.get("panel")))
    return specs, ts_cols


class Warehouse:
    def __init__(self, data_dir: Path | str = DATA_DIR, load_panel: bool = True):
        self.data_dir = Path(data_dir)
        self.specs, self.ts_cols = load_table_specs()
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE SCHEMA raw; CREATE SCHEMA v;")
        self.frames: dict[str, pd.DataFrame] = {}
        self.loaded: list[str] = []
        self.now: pd.Timestamp | None = None
        self._lock = threading.Lock()
        for name, spec in self.specs.items():
            if spec.panel and not load_panel:
                continue
            path = self.data_dir / spec.file
            if not path.exists():
                continue
            self._load(name, spec, path)
        if (self.data_dir / "order_items.csv").exists() and load_panel:
            self.con.execute(f"CREATE TABLE raw.order_items AS SELECT * FROM read_csv_auto('{self._p('order_items.csv')}')")
            self.loaded.append("order_items")
        # unclocked views for trusted internal use (SQL-defined metrics are cached once, then clipped to now)
        self.con.execute("CREATE SCHEMA full_data")
        for name in self.loaded:
            self.con.execute(f"CREATE VIEW full_data.{name} AS SELECT * FROM raw.{name}")
        self.con.execute("SET search_path = 'v'")
        self.con.execute("SET enable_external_access = false")
        hourly = self.frames.get("facts")
        self.max_ts = pd.Timestamp(hourly["ts"].max()) if hourly is not None else None
        self.min_ts = pd.Timestamp(hourly["ts"].min()) if hourly is not None else None
        self.set_now(self.max_ts)

    # ------------------------------------------------------------------ loading
    def _p(self, file: str) -> str:
        return str((self.data_dir / file).resolve()).replace("\\", "/").replace("'", "''")

    def _load(self, name: str, spec: TableSpec, path: Path):
        cols = self.ts_cols.get(name, [spec.time] if spec.time else [])
        self.con.execute(f"CREATE TABLE raw.{name} AS SELECT * FROM read_csv_auto('{self._p(spec.file)}', header=true)")
        types = dict(self.con.execute(f"SELECT column_name, data_type FROM information_schema.columns "
                                      f"WHERE table_schema='raw' AND table_name='{name}'").fetchall())
        for c in cols:
            if c in types and not types[c].startswith("TIMESTAMP"):
                self.con.execute(f'ALTER TABLE raw.{name} ALTER "{c}" TYPE TIMESTAMP USING TRY_CAST("{c}" AS TIMESTAMP)')
        df = self.con.execute(f"SELECT * FROM raw.{name}").df()
        if spec.time and spec.time in df.columns:
            df = df.sort_values(spec.time).reset_index(drop=True)
        self.frames[name] = df
        self.loaded.append(name)

    # ------------------------------------------------------------------ clock
    def set_now(self, now) -> None:
        now = pd.Timestamp(now).floor("h")
        with self._lock:
            self.now = now
            cutoff = (now + pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            for name in self.loaded:
                spec = self.specs.get(name)
                if name == "order_items":
                    if "orders" in self.loaded:
                        self.con.execute("CREATE OR REPLACE VIEW v.order_items AS SELECT oi.* FROM raw.order_items oi "
                                         "JOIN v.orders o USING (order_id)")
                    continue
                if spec is None or not spec.time:
                    self.con.execute(f"CREATE OR REPLACE VIEW v.{name} AS SELECT * FROM raw.{name}")
                else:
                    self.con.execute(f'CREATE OR REPLACE VIEW v.{name} AS SELECT * FROM raw.{name} '
                                     f'WHERE "{spec.time}" < TIMESTAMP \'{cutoff}\'')

    # ------------------------------------------------------------------ pandas access
    def frame(self, name: str) -> pd.DataFrame:
        """Full table (no clock!). Only for the metric engine, which clips every query to `now` itself."""
        return self.frames[name]

    def view(self, name: str, since=None) -> pd.DataFrame:
        """Clocked pandas slice: rows with time < now + 1h (and >= since if given)."""
        df = self.frames.get(name)
        if df is None:
            return pd.DataFrame()
        spec = self.specs.get(name)
        if spec is None or not spec.time:
            return df
        t = df[spec.time]
        m = t < self.now + pd.Timedelta(hours=1)
        if since is not None:
            m &= t >= pd.Timestamp(since)
        return df[m]

    def has(self, name: str) -> bool:
        return name in self.frames

    def columns(self, name: str) -> list[str]:
        return list(self.frames[name].columns) if name in self.frames else []

    def panel_covers(self, start, end) -> bool:
        """Is the 1 % user panel available for [start, end]? (In live mode it is not rewritten.)"""
        s = self.frames.get("sessions")
        if s is None or s.empty:
            return False
        lo, hi = s["start_ts"].min(), s["start_ts"].max()
        return lo <= pd.Timestamp(start) and hi >= pd.Timestamp(end) - pd.Timedelta(hours=1)

    def source_of(self, table: str, column: str | None = None) -> str | None:
        spec = self.specs.get(table)
        if spec is None:
            return None
        if column:
            for src, cols in spec.column_sources.items():
                if column in cols:
                    return src
        return spec.source

    # ------------------------------------------------------------------ SQL
    def sql(self, query: str) -> pd.DataFrame:
        """Trusted internal query (views in schema v are clocked)."""
        cur = self.con.cursor()
        try:
            cur.execute("SET search_path = 'v'")
            return cur.execute(query).df()
        finally:
            cur.close()

    def sql_full(self, query: str) -> pd.DataFrame:
        """Trusted query over the full (unclocked) data. Callers must clip results to `now` themselves."""
        cur = self.con.cursor()
        try:
            cur.execute("SET search_path = 'full_data'")
            return cur.execute(query).df()
        finally:
            cur.close()

    def schema(self) -> dict[str, list[str]]:
        return {n: self.columns(n) for n in self.loaded if n in self.frames}

    def safe_sql(self, query: str, max_rows: int = 200, timeout_s: float = 15.0) -> dict:
        """The agents' read-only query tool. Returns {columns, rows, n_rows, truncated} or raises SqlRejected."""
        q = re.sub(r"--[^\n]*", " ", query)
        q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S).strip().rstrip(";").strip()
        if not re.match(r"^(select|with)\b", q, re.I):
            raise SqlRejected("only SELECT / WITH queries are allowed")
        if ";" in q:
            raise SqlRejected("one statement only")
        m = FORBIDDEN.search(q)
        if m:
            raise SqlRejected(f"forbidden construct: {m.group(0)!r}")
        cur = self.con.cursor()
        timer = threading.Timer(timeout_s, cur.interrupt)
        try:
            cur.execute("SET search_path = 'v'")
            timer.start()
            df = cur.execute(f"SELECT * FROM ({q}) AS _q LIMIT {int(max_rows) + 1}").df()
        except duckdb.Error as exc:
            raise SqlRejected(f"query failed: {str(exc).splitlines()[0][:300]}") from None
        finally:
            timer.cancel()
            cur.close()
        truncated = len(df) > max_rows
        df = df.head(max_rows)
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M")
        rows = df.round(6).astype(object).where(df.notna(), None).values.tolist()
        return {"columns": list(df.columns), "rows": rows, "n_rows": len(rows), "truncated": truncated}


_WH: Warehouse | None = None


def get_warehouse(reload: bool = False, **kw) -> Warehouse:
    """Process-wide warehouse (loading all tables takes a few seconds)."""
    global _WH
    if _WH is None or reload:
        _WH = Warehouse(**kw)
    return _WH
