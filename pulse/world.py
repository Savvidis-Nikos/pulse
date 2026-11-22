"""One cached handle on the marketplace data + metric graph, reused from the argus semantic layer (import only).

    from pulse.world import world
    w = world()            # argus.core.World: w.wh (warehouse), w.reg (metric registry), w.engine, w.graph, w.now
    w.set_now("2026-11-12 14:00")   # move the data clock (replay); no reload needed

Never edit argus/; treat it as a library.
"""
from __future__ import annotations

import os

_W = None


def world(reload: bool = False, validate: bool = True):
    global _W
    if _W is None or reload:
        from argus.core import load_world
        data_dir = os.environ.get("PULSE_DATA")   # default: data/eye (simulator output)
        _W = load_world(validate=validate, data_dir=data_dir)
    return _W
