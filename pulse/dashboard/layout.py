"""Deterministic 'solar system' layout for a GraphSnapshot.

The snapshot centre (the KPI) is the sun at (0, 0); every other node sits on the orbit of its `depth`.
Each node gets a parent one orbit further in, and the angular space is split like a radial tree: a node's
children share a slice of the circle centred on the node's own angle, so related metrics stay together.

The function is pure: the same snapshot always gives the same coordinates, and it always lays out the FULL
snapshot. Views hide orbits (depth filter) or highlight steps without ever recomputing positions, so nothing
moves on screen.
"""
from __future__ import annotations

import math

from pulse.contracts import GraphSnapshot

ORBIT_GAP = 190.0       # distance between orbits (vis-network canvas units)
ASPECT = 1.35           # orbits are ellipses (x stretched) to use widescreen space: a tilted solar system
MIN_ARC = 120.0         # minimum arc length between neighbours on the same orbit

_EDGE_PREF = {"formula": 0, "breakdown": 1, "component": 2, "cause": 3, "proposed": 4}


def node_depths(snap: GraphSnapshot) -> dict[str, int]:
    """Depth per node, normalised: centre is 0, every other node is >= 1."""
    out = {}
    for n in snap.nodes:
        out[n.id] = 0 if n.id == snap.center else max(1, int(n.depth or 0))
    if snap.center not in out and snap.nodes:   # defensive: centre missing from the node list
        out[snap.center] = 0
    return out


def orbit_radii(snap: GraphSnapshot) -> dict[int, float]:
    """Radius per depth. Orbits widen when crowded but never shrink below depth * ORBIT_GAP."""
    depths = node_depths(snap)
    counts: dict[int, int] = {}
    for d in depths.values():
        counts[d] = counts.get(d, 0) + 1
    radii: dict[int, float] = {0: 0.0}
    prev = 0.0
    for d in sorted(k for k in counts if k > 0):
        r = max(d * ORBIT_GAP, counts[d] * MIN_ARC / (2 * math.pi), prev + ORBIT_GAP * 0.8)
        radii[d] = round(r, 3)
        prev = r
    return radii


def _parents(snap: GraphSnapshot, depths: dict[str, int]) -> dict[str, str]:
    """Pick one parent per non-centre node: a neighbour on the next orbit in (formula edges preferred)."""
    nbrs: dict[str, list[tuple[int, str]]] = {nid: [] for nid in depths}
    for e in snap.edges:
        if e.source in depths and e.target in depths and e.source != e.target:
            pref = _EDGE_PREF.get(e.kind, 5)
            nbrs[e.source].append((pref, e.target))
            nbrs[e.target].append((pref, e.source))
    parents: dict[str, str] = {}
    for nid, d in depths.items():
        if nid == snap.center:
            continue
        inner = sorted((p, depths[o], o) for p, o in nbrs[nid] if depths[o] == d - 1)
        if not inner:   # no neighbour exactly one orbit in: take the closest shallower neighbour
            inner = sorted((depths[o], p, o) for p, o in nbrs[nid] if depths[o] < d)
            inner = [(0, 0, t[2]) for t in inner]
        parents[nid] = inner[0][2] if inner else snap.center
    return parents


def solar_layout(snap: GraphSnapshot) -> dict[str, tuple[float, float]]:
    """{node_id: (x, y)} with the centre at (0, 0). Deterministic, independent of any view filter."""
    depths = node_depths(snap)
    if not depths:
        return {}
    radii = orbit_radii(snap)
    parents = _parents(snap, depths)
    meta = {n.id: n for n in snap.nodes}

    children: dict[str, list[str]] = {nid: [] for nid in depths}
    for nid, p in parents.items():
        children.setdefault(p, []).append(nid)

    def sort_key(nid: str):
        n = meta.get(nid)
        return (n.domain or "~" if n else "~", n.kind if n else "", nid)

    for k in children:
        children[k].sort(key=sort_key)

    weight: dict[str, float] = {}

    def w(nid: str, seen: frozenset = frozenset()) -> float:
        if nid in weight:
            return weight[nid]
        kids = [c for c in children.get(nid, []) if c not in seen]
        val = max(1.0, sum(w(c, seen | {nid}) for c in kids) * 0.85)
        weight[nid] = val
        return val

    pos: dict[str, tuple[float, float]] = {snap.center: (0.0, 0.0)}
    angle: dict[str, float] = {}

    def place(nid: str, a0: float, a1: float) -> None:
        kids = children.get(nid, [])
        if not kids:
            return
        total = sum(w(c) for c in kids)
        a = a0
        for c in kids:
            span = (a1 - a0) * w(c) / total
            mid = a + span / 2
            angle[c] = mid
            r = radii[depths[c]]
            pos[c] = (round(ASPECT * r * math.cos(mid), 2), round(r * math.sin(mid), 2))
            # children of c get a slice centred on c's angle, at most its own span (keeps families together)
            place(c, mid - span / 2, mid + span / 2)
            a += span

    start = -math.pi / 2   # first family starts at 12 o'clock
    place(snap.center, start, start + 2 * math.pi)

    # anything unreachable (cycles / orphans): spread evenly on its orbit
    rest = sorted(n for n in depths if n not in pos)
    for i, nid in enumerate(rest):
        r = radii.get(depths[nid], ORBIT_GAP)
        a = start + 2 * math.pi * (i + 0.5) / max(1, len(rest))
        pos[nid] = (round(ASPECT * r * math.cos(a), 2), round(r * math.sin(a), 2))
    return pos
