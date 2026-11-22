"""World context shared by every layer: warehouse + registry + metric engine + metric graph."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from argus.data.warehouse import Warehouse
from argus.semantic.engine import MetricEngine
from argus.semantic.graph import MetricGraph
from argus.semantic.registry import Registry


@dataclass
class World:
    wh: Warehouse
    reg: Registry
    engine: MetricEngine
    graph: MetricGraph

    @property
    def now(self) -> pd.Timestamp:
        return self.wh.now

    def set_now(self, now) -> None:
        self.wh.set_now(now)

    def rebuild_graph(self, validate: bool = True) -> MetricGraph:
        """After the registry changed (cartographer) or new data arrived."""
        self.engine.refresh()
        self.graph = MetricGraph.build(self.reg, self.engine, validate=validate)
        return self.graph


def load_world(now=None, validate: bool = True, data_dir=None, load_panel: bool = True) -> World:
    wh = Warehouse(data_dir, load_panel=load_panel) if data_dir else Warehouse(load_panel=load_panel)
    if now is not None:
        wh.set_now(now)
    reg = Registry.load()
    engine = MetricEngine(wh, reg)
    graph = MetricGraph.build(reg, engine, validate=validate)
    return World(wh, reg, engine, graph)
