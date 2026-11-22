// Number, time and wording helpers. One vocabulary for the whole UI.

const MINUS = "−";

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function compact(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "k";
  if (a >= 1000) return Math.round(v).toLocaleString("en-GB");
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

export function fmtValue(v, unit) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v < 0 ? MINUS : "";
  const a = Math.abs(v);
  switch (unit) {
    case "ratio": return sign + (a * 100).toFixed(a < 0.1 ? 2 : 1) + "%";
    case "EUR": return sign + "€" + compact(a);
    case "ms": return sign + Math.round(a) + " ms";
    case "h": return sign + a.toFixed(1) + " h";
    default: return sign + compact(a);
  }
}

export function fmtPct(d, digits = 1) {
  if (d === null || d === undefined || Number.isNaN(d)) return "—";
  const a = Math.abs(d);
  const s = a >= 100 ? a.toFixed(0) : a.toFixed(digits);
  return (d > 0 ? "+" : d < 0 ? MINUS : "") + s + "%";
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function fmtClock(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// Is this move good or bad for the business?
export function tone(dev, polarity, anomalous = true) {
  if (dev === null || dev === undefined || !anomalous) return "flat";
  if (polarity === "neutral" || !polarity) return Math.abs(dev) < 0.5 ? "flat" : "shift";
  if (Math.abs(dev) < 0.5) return "flat";
  const bad = (dev < 0 && polarity === "up_is_good") || (dev > 0 && polarity === "down_is_good");
  return bad ? "harm" : "gain";
}

export const SEVERITY = {
  alert: "Needs attention",
  watch: "Watching",
  data_issue: "Data incomplete",
  ok: "Normal",
};

export const CONFIDENCE = {
  supported: "Supported by evidence",
  hypothesis: "Hypothesis",
  data_issue: "Data issue",
  unexplained: "Not explained yet",
};

export const MODE = {
  mock: "Offline run, scripted agents",
  replay: "Replay of a recorded run",
  live: "Live agents",
};

export const ACTION = {
  look: "Read the metric",
  expand: "Followed its links",
  detail: "Looked closer at",
  node_detail: "Looked closer at",
  localize: "Located the change in",
  recent_changes: "Checked recent changes near",
  breakdown: "Split by segment",
  propose_edge: "Proposed a new link",
  propose_connection: "Proposed a new link",
  search_data: "Searched raw data for",
  rule_out: "Ruled out",
  conclude: "Concluded",
};

// "event:payments-sdk android 7.4.0" -> "release payments-sdk android 7.4.0"; "rate|DE-android" -> "rate, DE-android"
export function prettyId(id) {
  return String(id ?? "")
    .replace(/^event:/, "release ")
    .replace(/^(comp|component|hypothesis|seg|segment|src|metric):/, "")
    .replace(/\|/g, ", ")
    .replace(/_/g, " ");
}

// Numbers attached to trace steps and evidence can be numbers, strings, lists or small objects.
export function numText(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Math.abs(v) < 10 ? String(+v.toFixed(3)) : Math.round(v).toLocaleString("en-GB");
  if (Array.isArray(v)) return v.map(numText).join(", ");
  if (typeof v === "object") return Object.values(v).map(numText).join(" ");
  return String(v);
}

export function sevRank(s) {
  return { alert: 0, data_issue: 1, watch: 2, ok: 3 }[s] ?? 4;
}
