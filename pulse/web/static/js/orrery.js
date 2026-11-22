// The orrery: a metric graph drawn as a solar system around the KPI being explained.
// Positions come from the server (pulse.dashboard.layout.solar_layout); nothing here simulates physics,
// so filtering depth or stepping through an investigation never moves a node.
import { esc, fmtValue, fmtPct, fmtClock, prettyId, tone } from "./format.js";

const NS = "http://www.w3.org/2000/svg";
const TIER_R = { north_star: 15, outcome: 13, driver: 11, health: 9.5 };
const CURVED = new Set(["cause", "learned", "ambiguous", "proposed"]);

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
const ekey = (a, b) => `${a}|${b}`;

// ---------- style tokens (server may send node.style / edge.style; otherwise these rules apply) ----------
export function nodeStyle(n, center) {
  if (n.style) {
    // shared encoding (pulse.dashboard.encoding). Role rings are revealed per replay frame by CSS classes,
    // so only the data-quality ring is drawn statically here.
    const s = n.style;
    return {
      ...s,
      shape: n.id === center ? "sun" : s.shape === "sun" ? "circle" : s.shape,
      fill: s.fill === "kpi" && n.id !== center ? "ok" : s.fill,
      size: n.id === center ? 26 : (s.size || 16) * 0.62,
      rings: (s.rings || []).filter(r => r === "formula_incomplete"),
    };
  }
  let fill = "ok";
  if (n.status === "missing_data") fill = "missing";
  else if (n.status === "stale") fill = "stale";
  else if (n.status === "anomalous") {
    const t = tone(n.deviation_pct, n.polarity, true);
    fill = t === "flat" ? "ok" : t;
  }
  const rings = [];
  if (n.formula_status === "incomplete" || n.formula_status === "broken") rings.push("formula_incomplete");
  let sub = "";
  if (n.status === "missing_data") sub = "no data";
  else if (n.status === "stale") sub = "stale";
  else if (n.status === "anomalous" && n.deviation_pct != null) sub = fmtPct(n.deviation_pct);
  return {
    fill, rings, sub,
    shape: n.id === center ? "sun" : (n.added_by && n.added_by !== "seed") ? "diamond" : "circle",
    size: n.id === center ? 26 : (TIER_R[n.tier] || 10.5),
  };
}

export function edgeStyle(e) {
  if (e.style) return e.style;
  if (e.status === "rejected") return { line: "rejected" };
  if (e.kind === "proposed" || (e.added_by && e.added_by !== "seed" && e.status !== "ambiguous")) return { line: "proposed" };
  if (e.status === "ambiguous") return { line: "ambiguous" };
  if (e.kind === "formula" || e.kind === "breakdown" || e.kind === "component") return { line: "formula" };
  if (e.status === "learned" || e.status === "validated") return { line: "learned" };
  return { line: "cause" };
}

// ---------- trace frames (server may send interpretation.frames) ----------
export function traceFrames(interp, partial) {
  if (interp.frames && interp.frames.length) return interp.frames;
  const steps = interp.trace || [];
  const frames = [{ focus_nodes: [], visited_nodes: [], focus_edges: [], visited_edges: [], path_edges: [], reveal_roles: false, caption: "" }];
  const vn = new Set([interp.kpi]);
  let ve = [];
  const pathEdges = [];
  for (let i = 0; i + 1 < (interp.path || []).length; i++) pathEdges.push([interp.path[i], interp.path[i + 1]]);
  steps.forEach((s, i) => {
    const fn = [...(s.highlight_nodes || [])];
    if (s.node && !fn.includes(s.node)) fn.unshift(s.node);
    fn.forEach(n => vn.add(n));
    const fe = s.highlight_edges || [];
    ve = ve.concat(fe);
    const last = i === steps.length - 1 && !partial;
    frames.push({
      focus_nodes: fn, visited_nodes: [...vn], focus_edges: fe, visited_edges: [...ve],
      path_edges: last ? pathEdges : [], reveal_roles: last, caption: s.finding || "",
    });
  });
  return frames;
}

function rolesFrom(snapshot, interp) {
  const roles = new Map();
  const add = (id, r) => { if (!id) return; if (!roles.has(id)) roles.set(id, new Set()); roles.get(id).add(r); };
  for (const n of snapshot.nodes) for (const r of n.roles || []) add(n.id, r);
  if (interp) {
    (interp.path || []).forEach(id => add(id, "on_path"));
    if (interp.first_cause) add(interp.first_cause.node, "first_cause");
    (interp.ruled_out || []).forEach(f => add(f.node, "ruled_out"));
    (interp.unusual || []).forEach(f => add(f.node, "unusual"));
    (interp.watch_next || []).forEach(id => add(id, "watch"));
    (interp.symptoms || []).forEach(f => add(f.node, "symptom"));
  }
  return roles;
}

// ---------- the component ----------
export function createOrrery(host, { onSelect } = {}) {
  host.innerHTML = "";
  host.classList.add("orrery-stage");
  const svg = el("svg", { role: "img", "aria-label": "Metric graph drawn as a solar system around the selected metric" }, host);
  const tip = document.createElement("div");
  tip.className = "tooltip";
  host.appendChild(tip);

  let snap = null, interp = null, frames = [], roles = new Map();
  let vb = { x: -500, y: -400, w: 1000, h: 800 };
  let k0 = 1;
  let nodeEls = new Map(), edgeEls = [], orbitEls = [];
  let maxDepth = Infinity;
  let mode = "static";

  function screenScale() {
    const r = svg.getBoundingClientRect();
    if (!r.width || !r.height) return 1;
    return Math.max(vb.w / r.width, vb.h / r.height);
  }
  function applyViewBox() {
    svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    svg.style.setProperty("--k", screenScale().toFixed(4));
  }

  function bounds(onlyVisible = true) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of snap.nodes) {
      const d = (snap.layout?.depths || {})[n.id] ?? n.depth ?? 0;
      if (onlyVisible && d > maxDepth) continue;
      x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y);
    }
    if (!Number.isFinite(x0)) { x0 = -200; x1 = 200; y0 = -200; y1 = 200; }
    return { x0, y0, x1, y1 };
  }

  function fit() {
    if (!snap) return;
    const b = bounds(true);
    // include the outermost visible orbit, so the whole orrery rim is in view
    const orbits = Object.entries(snap.layout?.orbits || {}).map(([d, rr]) => [Number(d), rr]).filter(([d]) => d > 0 && d <= maxDepth);
    if (orbits.length) {
      const rr = Math.max(...orbits.map(o => o[1])), ax = (snap.layout?.aspect || 1) * rr;
      b.x0 = Math.min(b.x0, -ax); b.x1 = Math.max(b.x1, ax); b.y0 = Math.min(b.y0, -rr); b.y1 = Math.max(b.y1, rr);
    }
    const r = svg.getBoundingClientRect();
    const aspect = r.width && r.height ? r.width / r.height : 1.5;
    const padX = 215, padY = 50;
    let w = b.x1 - b.x0 + padX * 2, h = b.y1 - b.y0 + padY * 2;
    if (w / h < aspect) w = h * aspect; else h = w / aspect;
    vb = { x: (b.x0 + b.x1) / 2 - w / 2, y: (b.y0 + b.y1) / 2 - h / 2, w, h };
    applyViewBox();
  }

  function zoom(factor, cx, cy) {
    const nx = cx ?? vb.x + vb.w / 2, ny = cy ?? vb.y + vb.h / 2;
    const w = Math.min(Math.max(vb.w * factor, 260), 9000);
    const f = w / vb.w;
    vb = { x: nx - (nx - vb.x) * f, y: ny - (ny - vb.y) * f, w, h: vb.h * f };
    applyViewBox();
  }

  function toUser(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    const m = svg.getScreenCTM();
    return m ? pt.matrixTransform(m.inverse()) : { x: 0, y: 0 };
  }

  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const p = toUser(e);
    zoom(e.deltaY > 0 ? 1.12 : 1 / 1.12, p.x, p.y);
  }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", e => {
    if (e.target.closest(".n")) return;
    drag = { start: toUser(e), vb: { ...vb } };
    svg.setPointerCapture(e.pointerId);
    svg.classList.add("dragging");
  });
  svg.addEventListener("pointermove", e => {
    if (!drag) return;
    const m = svg.getScreenCTM();
    if (!m) return;
    const s = 1 / m.a;
    vb.x -= e.movementX * s; vb.y -= e.movementY * s;
    applyViewBox();
  });
  const endDrag = () => { drag = null; svg.classList.remove("dragging"); };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  new ResizeObserver(() => applyViewBox()).observe(host);

  // ---------- drawing ----------
  function defs(g) {
    const d = el("defs", {}, g);
    const pat = el("pattern", { id: "hatch", width: 5 * k0, height: 5 * k0, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, d);
    el("rect", { width: 5 * k0, height: 5 * k0, fill: "var(--surface-2)" }, pat);
    el("line", { x1: 0, y1: 0, x2: 0, y2: 5 * k0, stroke: "var(--void)", "stroke-width": 1.6 * k0 }, pat);
    const grad = el("radialGradient", { id: "sun-core", cx: "40%", cy: "38%", r: "70%" }, d);
    el("stop", { offset: "0%", "stop-color": "var(--brass-2)" }, grad);
    el("stop", { offset: "100%", "stop-color": "var(--brass)" }, grad);
    const s = 7 * k0;
    for (const [id, cls] of [["arr-formula", "arrow"], ["arr-cause", "arrow-cause"], ["arr-learned", "arrow-learned"], ["arr-brass", "arrow-brass"]]) {
      const m = el("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: s, markerHeight: s, markerUnits: "userSpaceOnUse", orient: "auto" }, d);
      el("path", { d: "M0,1 L10,5 L0,9 z", class: cls }, m);
    }
  }

  function drawOrbits(g) {
    orbitEls = [];
    const aspect = snap.layout?.aspect ?? 1;
    const orbits = Object.entries(snap.layout?.orbits || {}).map(([d, r]) => [Number(d), r]).filter(([d]) => d > 0).sort((a, b) => a[0] - b[0]);
    orbits.forEach(([d, r], i) => {
      const og = el("g", { "data-depth": d }, g);
      el("ellipse", { class: "orbit", cx: 0, cy: 0, rx: aspect * r, ry: r, "vector-effect": "non-scaling-stroke" }, og);
      if (i === orbits.length - 1) {   // engraved scale on the outermost orbit, like an orrery's rim
        for (let a = 0; a < 360; a += 10) {
          const t = a * Math.PI / 180, long = a % 30 === 0 ? 1.035 : 1.018;
          el("line", { class: "orbit-tick", x1: aspect * r * Math.cos(t), y1: r * Math.sin(t), x2: aspect * r * long * Math.cos(t), y2: r * long * Math.sin(t), "vector-effect": "non-scaling-stroke" }, og);
        }
      }
      // put the ring's label in the widest gap between the metrics on that ring (default: just above 3 o'clock)
      const taken = snap.nodes.filter(n => n.depth === d && n.x !== undefined).map(n => Math.atan2(n.y, n.x / aspect));
      let best = -0.12, bestGap = -1;
      for (let a = -Math.PI; a < Math.PI; a += Math.PI / 36) {
        const gap = Math.min(Math.PI, ...taken.map(t => Math.abs(Math.atan2(Math.sin(a - t), Math.cos(a - t)))));
        if (gap > bestGap + 1e-6 || (Math.abs(gap - bestGap) < 1e-6 && Math.abs(a + 0.12) < Math.abs(best + 0.12))) { best = a; bestGap = gap; }
      }
      const lbl = el("text", { class: "orbit-label", x: aspect * r * Math.cos(best) + 8 * k0, y: r * Math.sin(best), style: `font-size:${10.5 * k0}px` }, og);
      lbl.textContent = d === 1 ? "1 link away" : `${d} links away`;
      orbitEls.push({ d, g: og });
    });
  }

  function edgePath(a, b, ra, rb, curved) {
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    if (!curved) {
      const ux = dx / len, uy = dy / len;
      return `M${a.x + ux * ra},${a.y + uy * ra} L${b.x - ux * rb},${b.y - uy * rb}`;
    }
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, off = len * 0.18;
    const cx = mx - (dy / len) * off, cy = my + (dx / len) * off;
    const s = (p, q, r) => { const vx = q.x - p.x, vy = q.y - p.y, l = Math.hypot(vx, vy) || 1; return { x: p.x + vx / l * r, y: p.y + vy / l * r }; };
    const p0 = s(a, { x: cx, y: cy }, ra), p1 = s(b, { x: cx, y: cy }, rb);
    return `M${p0.x},${p0.y} Q${cx},${cy} ${p1.x},${p1.y}`;
  }

  function draw() {
    svg.innerHTML = "";
    nodeEls = new Map(); edgeEls = [];
    fit();
    k0 = screenScale();
    const root = el("g", {}, svg);
    defs(root);
    drawOrbits(el("g", { class: "orbits" }, root));
    const byId = new Map(snap.nodes.map(n => [n.id, n]));
    const radius = new Map(snap.nodes.map(n => [n.id, nodeStyle(n, snap.center).size * k0]));

    const eg = el("g", { class: "edges" }, root);
    for (const e of snap.edges) {
      const a = byId.get(e.source), b = byId.get(e.target);
      if (!a || !b || a.x === undefined || b.x === undefined) continue;
      const st = edgeStyle(e);
      if (st.line === "rejected") continue;
      const gap = 4 * k0;
      const p = el("path", {
        class: `e e-${st.line}`, d: edgePath(a, b, radius.get(a.id) + gap, radius.get(b.id) + gap + 2 * k0, CURVED.has(st.line)),
        "vector-effect": "non-scaling-stroke",
        "marker-end": `url(#${st.line === "formula" ? "arr-formula" : st.line === "learned" ? "arr-learned" : st.line === "proposed" ? "arr-brass" : "arr-cause"})`,
      }, eg);
      const t = el("title", {}, p);
      t.textContent = e.label || `${prettyId(e.source)} ${e.kind === "formula" ? "feeds the formula of" : "may affect"} ${prettyId(e.target)}`;
      edgeEls.push({ e, el: p, line: st.line });
    }

    const ng = el("g", { class: "nodes" }, root);
    const labels = [];
    for (const n of snap.nodes) {
      if (n.x === undefined) continue;
      const st = nodeStyle(n, snap.center);
      const r = radius.get(n.id);
      const g = el("g", { class: `n n-${st.fill}${st.shape === "sun" ? " n-kpi" : ""}`, transform: `translate(${n.x},${n.y})`, tabindex: 0, "data-id": n.id, role: "button", "aria-label": prettyId(n.label || n.id) }, ng);
      if (st.shape === "sun") {
        el("circle", { class: "sun-corona", r: r * 2.1, opacity: 0.07 }, g);
        el("circle", { class: "sun-corona", r: r * 1.5, opacity: 0.14 }, g);
        el("circle", { r, fill: "url(#sun-core)" }, g);
        const t = tone(n.deviation_pct, n.polarity, n.status === "anomalous");
        if (t === "harm" || t === "gain") el("circle", { r: r + 3 * k0, fill: "none", stroke: `var(--${t})`, "stroke-width": 2.6, "vector-effect": "non-scaling-stroke" }, g);
      } else {
        const q = r * 1.25;
        const body = st.shape === "diamond"
          ? el("path", { class: "n-body", d: `M0,${-q} L${q},0 L0,${q} L${-q},0 Z`, "vector-effect": "non-scaling-stroke" }, g)
          : el("circle", { class: "n-body", r, "vector-effect": "non-scaling-stroke" }, g);
        // own share of the change: a first cause glows, a node that only inherits its move stays pale
        if (["harm", "gain", "shift"].includes(st.fill) && st.intensity != null) body.style.fillOpacity = (0.35 + 0.65 * st.intensity).toFixed(2);
      }
      el("circle", { class: "n-symptom-ring", r: r + 3.5 * k0, "vector-effect": "non-scaling-stroke" }, g);
      if ((st.rings || []).includes("formula_incomplete")) el("circle", { class: "n-formula-ring", r: r + 4.5 * k0, "vector-effect": "non-scaling-stroke" }, g);
      el("circle", { class: "n-watch-ring", r: r + 4 * k0, "vector-effect": "non-scaling-stroke" }, g);
      el("circle", { class: "n-path-ring", r: r + 3 * k0, "vector-effect": "non-scaling-stroke" }, g);
      el("circle", { class: "n-first-ring", r: r + 8 * k0, "vector-effect": "non-scaling-stroke" }, g);
      el("circle", { class: "n-focus-ring", r: r + 6 * k0, "vector-effect": "non-scaling-stroke" }, g);
      el("circle", { class: "n-unusual", cx: r * 0.85, cy: -r * 0.85, r: 3.4 * k0 }, g);

      // label: pushed radially outward so neighbours on the same orbit don't collide
      const ang = Math.atan2(n.y, n.x / (snap.layout?.aspect || 1));
      const isSun = st.shape === "sun";
      const off = r + (isSun ? 14 : 9) * k0;
      let lx = isSun ? 0 : Math.cos(ang) * off, ly = isSun ? off + 6 * k0 : Math.sin(ang) * off;
      const c = Math.cos(ang), s = Math.sin(ang);
      const anchor = isSun || Math.abs(c) < 0.3 ? "middle" : c > 0 ? "start" : "end";
      if (!isSun && Math.abs(c) < 0.3) ly += s > 0 ? 8 * k0 : -4 * k0;
      const fs = (isSun ? 13.5 : 11.5) * k0;
      const tg = el("g", { class: "n-text" }, g);
      const text = el("text", { class: "n-label", x: lx, y: ly, "text-anchor": anchor, style: `font-size:${fs}px;stroke-width:${4 * k0}px` }, tg);
      text.textContent = prettyId(n.label || n.id);
      const measured = [text];
      if (st.sub) {
        const sub = el("text", { class: `n-label n-sub t-${st.fill === "missing" || st.fill === "stale" ? "none" : st.fill}`, x: lx, y: ly + fs * 1.15, "text-anchor": anchor, style: `font-size:${10.5 * k0}px;stroke-width:${4 * k0}px` }, tg);
        sub.textContent = st.sub;
        measured.push(sub);
      }
      const tag = el("text", { class: "n-label n-tag", x: lx, y: ly + fs * 1.15 * (st.sub ? 2 : 1), "text-anchor": anchor, style: `font-size:${10.5 * k0}px;stroke-width:${4 * k0}px` }, tg);
      tag.textContent = "first cause";
      labels.push({ n, tg, measured, r, isSun, dir: Math.sin(ang) >= 0 ? 1 : -1 });

      g.addEventListener("pointerenter", ev => showTip(n, ev));
      g.addEventListener("pointermove", ev => moveTip(ev));
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("focus", () => { const b = g.getBoundingClientRect(); showTip(n, { clientX: b.right, clientY: b.top }); });
      g.addEventListener("blur", hideTip);
      g.addEventListener("click", () => onSelect && onSelect(n.id));
      g.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); onSelect && onSelect(n.id); } });
      nodeEls.set(n.id, g);
    }
    declutter(labels, root);
    applyDepth();
  }

  // One greedy pass, done once per draw (so labels never move on depth or replay changes): the sun's label is
  // placed first, then the most-moved metrics; each label slides up/down in small steps until it clears the
  // labels already placed, the other metric dots and the ring labels.
  function declutter(labels, root) {
    const box = (b, dx = 0, dy = 0) => ({ x0: b.x + dx, y0: b.y + dy, x1: b.x + b.width + dx, y1: b.y + b.height + dy });
    const hit = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
    const pad = 2 * k0;
    const fixed = [...root.querySelectorAll(".orbit-label")].map(t => box(t.getBBox()));
    const dots = labels.map(L => ({ id: L.n.id, b: { x0: L.n.x - L.r, y0: L.n.y - L.r, x1: L.n.x + L.r, y1: L.n.y + L.r } }));
    const placed = [];
    const r = svg.getBoundingClientRect();                  // visible user-space x range ("meet" may widen the viewBox)
    const visW = r.width && r.height ? r.width / Math.min(r.width / vb.w, r.height / vb.h) : vb.w;
    const left = vb.x + vb.w / 2 - visW / 2 + pad, right = vb.x + vb.w / 2 + visW / 2 - pad;
    const order = [...labels].sort((a, b) => (b.isSun - a.isSun) || (Math.abs(b.n.deviation_pct || 0) - Math.abs(a.n.deviation_pct || 0)));
    for (const L of order) {
      const bs = L.measured.map(t => t.getBBox());
      if (!bs.length || !bs[0].width) continue;              // not rendered (hidden tab): leave as is
      const u = { x0: Math.min(...bs.map(b => b.x)) - pad, y0: Math.min(...bs.map(b => b.y)) - pad,
                  x1: Math.max(...bs.map(b => b.x + b.width)) + pad, y1: Math.max(...bs.map(b => b.y + b.height)) + pad };
      const h = u.y1 - u.y0;
      let bestDy = 0, bestHits = Infinity;
      for (const step of [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6]) {
        const dy = step * h * 0.55 * L.dir;
        const c = { x0: u.x0 + L.n.x, y0: u.y0 + L.n.y + dy, x1: u.x1 + L.n.x, y1: u.y1 + L.n.y + dy };
        const hits = placed.filter(p => hit(c, p)).length + fixed.filter(p => hit(c, p)).length
          + dots.filter(d => d.id !== L.n.id && hit(c, d.b)).length;
        if (hits < bestHits) { bestHits = hits; bestDy = dy; }
        if (!hits) break;
      }
      const x0 = u.x0 + L.n.x, x1 = u.x1 + L.n.x;             // keep the label inside the panel (no clipped names)
      const dx = x0 < left ? left - x0 : x1 > right ? Math.max(left - x0, right - x1) : 0;
      if (bestDy || dx) L.tg.setAttribute("transform", `translate(${dx},${bestDy})`);
      placed.push({ x0: x0 + dx, y0: u.y0 + L.n.y + bestDy, x1: x1 + dx, y1: u.y1 + L.n.y + bestDy });
    }
  }

  // ---------- tooltip ----------
  function showTip(n, ev) {
    const tt = n.style?.tooltip;
    if (tt && Object.keys(tt).length) {
      const about = tt.about || tt.About;
      const rows2 = Object.entries(tt).filter(([k]) => !/^about$/i.test(k));
      tip.innerHTML = `<h4>${esc(prettyId(n.label || n.id))}</h4>
        ${about || n.description ? `<div class="tt-desc">${esc(about || n.description)}</div>` : ""}
        <table>${rows2.map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join("")}</table>`;
      tip.dataset.show = "true";
      moveTip(ev);
      return;
    }
    const rows = [];
    const unit = n.unit;
    if (n.value != null) rows.push(["Now", fmtValue(n.value, unit)]);
    if (n.expected != null) rows.push(["Normal for this hour", fmtValue(n.expected, unit)]);
    if (n.deviation_pct != null) rows.push(["Change", fmtPct(n.deviation_pct)]);
    if (n.z != null) rows.push(["Unusualness (z)", n.z.toFixed(1)]);
    if (n.completeness != null) rows.push(["Data arrived", `${Math.round(n.completeness * 100)}%`]);
    if (n.onset_ts) rows.push(["Started", fmtClock(n.onset_ts)]);
    if (n.inherited_pct != null) rows.push(["Explained by its inputs", `${Math.round(n.inherited_pct)}%`]);
    const notes = [];
    if (n.definition) notes.push(`Formula: ${esc(n.definition)}`);
    if (n.formula_status === "incomplete") notes.push("The formula does not fully add up against the data.");
    if (n.formula_status === "broken") notes.push("The formula no longer matches the data.");
    if (n.status === "missing_data") notes.push("No data arrived for this window.");
    if (n.status === "stale") notes.push("The latest data point is old.");
    if (n.added_by && n.added_by !== "seed") notes.push("Added to the graph by the graph handler.");
    tip.innerHTML = `<h4>${esc(prettyId(n.label || n.id))}</h4>
      ${n.description ? `<div class="tt-desc">${esc(n.description)}</div>` : ""}
      ${rows.length ? `<table>${rows.map(([a, b]) => `<tr><td>${a}</td><td>${esc(b)}</td></tr>`).join("")}</table>` : ""}
      ${notes.length ? `<div class="tt-note">${notes.join("<br>")}</div>` : ""}`;
    tip.dataset.show = "true";
    moveTip(ev);
  }
  function moveTip(ev) {
    const hb = host.getBoundingClientRect();
    let x = ev.clientX - hb.left + 16, y = ev.clientY - hb.top + 12;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    if (x + tw > hb.width - 8) x = ev.clientX - hb.left - tw - 16;
    if (y + th > hb.height - 8) y = Math.max(8, hb.height - th - 8);
    tip.style.left = `${x}px`; tip.style.top = `${y}px`;
  }
  function hideTip() { tip.dataset.show = "false"; }

  // ---------- state application ----------
  function depthOf(id) {
    return (snap.layout?.depths || {})[id] ?? snap.nodes.find(n => n.id === id)?.depth ?? 0;
  }
  function applyDepth() {
    for (const [id, g] of nodeEls) g.style.display = depthOf(id) > maxDepth ? "none" : "";
    for (const { e, el: p } of edgeEls) p.style.display = (depthOf(e.source) > maxDepth || depthOf(e.target) > maxDepth) ? "none" : "";
    for (const o of orbitEls) o.g.style.display = o.d > maxDepth ? "none" : "";
  }

  const ROLE_CLASS = { first_cause: "r-first", on_path: "r-path", ruled_out: "r-ruled", unusual: "r-unusual", watch: "r-watch", symptom: "r-symptom" };
  function clearState() {
    for (const g of nodeEls.values()) g.classList.remove("r-first", "r-path", "r-ruled", "r-unusual", "r-watch", "r-symptom", "r-visited", "r-focus");
    for (const { el: p } of edgeEls) p.classList.remove("e-visited", "e-focus", "e-path");
    svg.classList.remove("dim");
  }
  function applyRoles() {
    for (const [id, set] of roles) {
      const g = nodeEls.get(id);
      if (!g) continue;
      for (const r of set) if (ROLE_CLASS[r]) g.classList.add(ROLE_CLASS[r]);
    }
  }
  function markEdges(pairs, cls) {
    const keys = new Set();
    for (const [a, b] of pairs || []) { keys.add(ekey(a, b)); keys.add(ekey(b, a)); }
    for (const { e, el: p } of edgeEls) if (keys.has(ekey(e.source, e.target))) p.classList.add(cls);
  }

  function setFrame(i) {
    if (!snap) return;
    clearState();
    if (mode === "static") { applyRoles(); return; }
    const f = frames[Math.max(0, Math.min(i, frames.length - 1))];
    if (!f) return;
    if (i > 0) svg.classList.add("dim");
    for (const id of f.visited_nodes || []) nodeEls.get(id)?.classList.add("r-visited");
    markEdges(f.visited_edges, "e-visited");
    if (f.reveal_roles) { applyRoles(); markEdges(f.path_edges, "e-path"); }
    for (const id of f.focus_nodes || []) {
      const g = nodeEls.get(id);
      if (!g) continue;
      g.classList.remove("r-focus");
      void g.getBoundingClientRect();   // restart the one-shot ping
      g.classList.add("r-focus");
    }
    markEdges(f.focus_edges, "e-focus");
  }

  return {
    setData(snapshot, interpretation = null, { partial = false } = {}) {
      const sameSnap = snap && snap.snapshot_id === snapshot.snapshot_id && snap.nodes.length === snapshot.nodes.length;
      snap = snapshot; interp = interpretation;
      roles = rolesFrom(snapshot, interpretation);
      frames = interpretation ? traceFrames(interpretation, partial) : [];
      mode = interpretation ? "trace" : "static";
      if (!sameSnap) draw();
      return frames;
    },
    setFrame,
    setMaxDepth(d) { maxDepth = d; if (snap) applyDepth(); },
    fit, zoomIn: () => zoom(1 / 1.25), zoomOut: () => zoom(1.25),
    focusNode(id) {
      const n = snap?.nodes.find(x => x.id === id);
      if (!n) return;
      vb = { ...vb, x: n.x - vb.w / 2, y: n.y - vb.h / 2 };
      applyViewBox();
    },
    get frames() { return frames; },
  };
}

// ---------- legend: only the encodings the current graph uses ----------
const SWATCH = {};   // token -> swatch svg, filled in by legendHTML
const DEFAULT_LEGEND = [
  ["Nodes", "kpi", "Metric under investigation"], ["Nodes", "harm", "Worse than normal"], ["Nodes", "gain", "Better than normal"],
  ["Nodes", "shift", "Changed"], ["Nodes", "ok", "Normal"], ["Nodes", "missing", "Missing data"], ["Nodes", "stale", "Stale data"],
  ["Nodes", "faded", "Ruled out"], ["Nodes", "diamond", "Added by the graph agent"], ["Nodes", "formula_incomplete", "Incomplete formula"],
  ["Nodes", "first_cause", "First cause"], ["Nodes", "path", "On the path to the cause"], ["Nodes", "symptom", "Knock-on effect"],
  ["Nodes", "unusual", "Unusual"], ["Links", "formula", "Part of its formula"], ["Links", "cause", "Known cause"],
  ["Links", "learned", "Cause learned from data"], ["Links", "ambiguous", "Uncertain cause"], ["Links", "proposed", "Proposed link"],
].map(([group, token, label]) => ({ group, token, label }));

// present: Set of tokens drawn in the current graph (null = show every entry)
export function legendHTML(collapsed = false, legend = null, present = null) {
  const nodeSw = (cls, shape = "circle", extra = "") => {
    const body = shape === "diamond" ? `<path class="n-body" d="M0,-8 L8,0 L0,8 L-8,0 Z"/>` : `<circle class="n-body" r="6.5"/>`;
    return `<svg width="22" height="20" viewBox="-11 -10 22 20"><g class="n ${cls}">${body}${extra}</g></svg>`;
  };
  const sun = `<svg width="22" height="20" viewBox="-11 -10 22 20"><circle r="9" fill="var(--brass)" opacity=".18"/><circle r="6" fill="var(--brass)"/></svg>`;
  const edgeSw = cls => `<svg width="34" height="12" viewBox="0 0 34 12"><path class="e ${cls}" d="M2,6 L30,6"/></svg>`;
  const arrow = cls => `<svg width="34" height="12" viewBox="0 0 34 12"><path class="e ${cls}" d="M2,6 L26,6"/><path d="M25,2 L32,6 L25,10 z" class="${cls === "e-formula" ? "arrow" : cls === "e-learned" ? "arrow-learned" : cls === "e-proposed" || cls === "e-path" ? "arrow-brass" : "arrow-cause"}"/></svg>`;
  const row = (sw, label) => `<div class="legend-row">${sw}<span>${label}</span></div>`;
  Object.assign(SWATCH, {
    kpi: sun, harm: nodeSw("n-harm"), gain: nodeSw("n-gain"), shift: nodeSw("n-shift"), ok: nodeSw("n-ok"),
    missing: nodeSw("n-missing"), stale: nodeSw("n-stale"), faded: nodeSw("n-ok r-ruled"),
    diamond: nodeSw("n-ok", "diamond"),
    formula_incomplete: nodeSw("n-ok", "circle", `<circle class="n-formula-ring" r="9.5"/>`),
    first_cause: nodeSw("n-harm r-first", "circle", `<circle class="n-path-ring" r="8.5"/><circle class="n-first-ring" r="11"/>`),
    path: nodeSw("n-ok r-path", "circle", `<circle class="n-path-ring" r="8.5"/>`),
    symptom: nodeSw("n-harm r-symptom", "circle", `<circle class="n-symptom-ring" r="9"/>`),
    unusual: nodeSw("n-ok r-unusual", "circle", `<circle class="n-unusual" cx="5.5" cy="-5.5" r="2.6"/>`),
    formula: arrow("e-formula"), cause: arrow("e-cause"), learned: arrow("e-learned"),
    ambiguous: edgeSw("e-ambiguous"), proposed: arrow("e-proposed"),
  });
  const shown = (Array.isArray(legend) && legend.length ? legend : DEFAULT_LEGEND)
    .filter(x => SWATCH[x.token] && (!present || present.has(x.token)));
  const nodes = shown.filter(x => x.group === "Nodes").map(x => row(SWATCH[x.token], esc(x.label))).join("");
  let links = shown.filter(x => x.group === "Links").map(x => row(SWATCH[x.token], esc(x.label))).join("");
  if (!present || present.has("path")) links += row(edgeSw("e-path"), "Path to the first cause");
  return `<div class="legend" data-collapsed="${collapsed}">
    <div class="legend-head"><b>Legend</b>
      <button class="legend-toggle" type="button" data-legend-toggle aria-expanded="${!collapsed}">${collapsed ? "Show" : "Hide"}</button></div>
    <div class="legend-cols"><div class="legend-list">${nodes}</div><div class="legend-list legend-links">${links}</div></div></div>`;
}
