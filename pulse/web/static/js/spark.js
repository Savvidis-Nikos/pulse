// 48-hour sparkline with the expected band (lo..hi), the expected line and the latest value.
// Hovering shows the hour under the pointer, its value and the normal value (bindSparks).
import { esc, fmtValue, fmtTime } from "./format.js";

export function sparkline(series, { width = 220, height = 46, tone = "flat", label = "", unit = "" } = {}) {
  const pts = (series || []).filter(p => p.value !== null && p.value !== undefined);
  if (pts.length < 2) return `<svg class="spark" width="${width}" height="${height}" role="img" aria-label="No history yet"></svg>`;
  const vals = [];
  for (const p of series) {
    for (const k of ["value", "expected", "lo", "hi"]) if (p[k] !== null && p[k] !== undefined) vals.push(p[k]);
  }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const pad = 4, n = series.length;
  const x = i => pad + (i / (n - 1)) * (width - pad * 2);
  const y = v => height - pad - ((v - lo) / (hi - lo)) * (height - pad * 2);
  const line = key => series.map((p, i) => (p[key] === null || p[key] === undefined) ? null : [x(i), y(p[key])])
    .filter(Boolean).map((q, i) => (i ? "L" : "M") + q[0].toFixed(1) + " " + q[1].toFixed(1)).join(" ");
  const top = series.map((p, i) => p.hi != null ? `${x(i).toFixed(1)},${y(p.hi).toFixed(1)}` : null).filter(Boolean);
  const bot = series.map((p, i) => p.lo != null ? `${x(i).toFixed(1)},${y(p.lo).toFixed(1)}` : null).filter(Boolean).reverse();
  const band = top.length && bot.length ? `<polygon class="band" points="${top.concat(bot).join(" ")}"/>` : "";
  const last = series.length - 1 - [...series].reverse().findIndex(p => p.value != null);
  // [x, y, ts, value, expected] per hour with a value, for the hover readout
  const hover = series.map((p, i) => p.value == null ? null : [+x(i).toFixed(1), +y(p.value).toFixed(1), p.ts, p.value, p.expected ?? null]).filter(Boolean);
  return `<svg class="spark" data-tone="${tone}" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}"
    data-unit="${esc(unit || "")}" data-pts="${esc(JSON.stringify(hover))}">
    ${band}<path class="exp" d="${line("expected")}"/><path class="val" d="${line("value")}"/>
    <circle class="now" cx="${x(last).toFixed(1)}" cy="${y(series[last].value).toFixed(1)}" r="2.6"/>
    <g class="hover" visibility="hidden"><line y1="1" y2="${height - 1}"/><circle r="3.2"/></g></svg>`;
}

let tip = null;

export function bindSparks(root) {
  if (!tip) {
    tip = document.body.appendChild(document.createElement("div"));
    tip.className = "spark-tip";
    tip.setAttribute("role", "tooltip");
  }
  tip.hidden = true;
  root.querySelectorAll("svg.spark[data-pts]").forEach(svg => {
    const pts = JSON.parse(svg.dataset.pts), unit = svg.dataset.unit || null;
    const g = svg.querySelector(".hover"), [guide, dot] = g.children, vbw = svg.viewBox.baseVal.width;
    svg.addEventListener("pointermove", e => {
      const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) * vbw / r.width;
      const [px, py, ts, v, ex] = pts.reduce((a, p) => Math.abs(p[0] - sx) < Math.abs(a[0] - sx) ? p : a);
      guide.setAttribute("x1", px); guide.setAttribute("x2", px);
      dot.setAttribute("cx", px); dot.setAttribute("cy", py);
      g.setAttribute("visibility", "visible");
      tip.innerHTML = `<b class="num">${fmtValue(v, unit)}</b><span>${fmtTime(ts)}</span>` +
        (ex != null ? `<span class="faint">normal ${fmtValue(ex, unit)}</span>` : "");
      tip.style.left = `${r.left + scrollX + px * r.width / vbw}px`;
      tip.style.top = `${r.top + scrollY - 6}px`;
      tip.hidden = false;
    });
    svg.addEventListener("pointerleave", () => { g.setAttribute("visibility", "hidden"); tip.hidden = true; });
  });
}
