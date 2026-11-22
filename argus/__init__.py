"""Metric library used by Pulse.

Layers: data (DuckDB warehouse over data/eye/) -> semantic (metric registry, metric engine, metric graph)
-> analytics (seasonal baselines, data trust, detection, localisation). Entry point: `argus.core.load_world`.
"""
__version__ = "0.1.0"
