// Pulse web UI: briefing, investigations, metric graph. Reads the Pulse state directory through the server.
import { api } from "./api.js";
import { esc, fmtValue, fmtPct, fmtTime, fmtClock, tone, prettyId, numText, sevRank, SEVERITY, CONFIDENCE, MODE } from "./format.js";
import { sparkline, bindSparks } from "./spark.js";
import { createOrrery, legendHTML } from "./orrery.js";

const $ = (sel, root = document) => root.querySelector(sel);
const icon = name => `<svg class="icon" aria-hidden="true"><use href="icons.svg#i-${name}"/></svg>`;

const S = {
  ov: null, version: -1, error: null,
  view: "briefing", param: null,
  legendCollapsed: false,
  open: new Set(), refreshing: false, refreshMsg: "",
  inv: { incidentId: null, interp: null, snap: null, frame: 0, playing: false, timer: null, orrery: null, livePoll: null, key: "" },
  graph: { snapshotId: null, depth: Infinity, selected: null, orrery: null, snap: null },
  con: { data: null, timer: null, key: "", error: "", scenario: "payment_failure", runFrom: null, followed: new Set(), follow: null },
};
try { S.legendCollapsed = localStorage.getItem("pulse-legend") === "collapsed"; } catch (e) { /* storage blocked */ }

// ---------- data ----------
const kpiLabel = name => S.ov?.tick?.kpis?.find(k => k.name === name)?.label || prettyId(name);
const kpiOf = name => S.ov?.tick?.kpis?.find(k => k.name === name);
const incidents = () => (S.ov?.incidents || []).slice().sort((a, b) =>
  (a.state === "resolved") - (b.state === "resolved") || sevRank(a.severity) - sevRank(b.severity) || String(b.last_seen).localeCompare(String(a.last_seen)));
const interpIndex = id => (S.ov?.interpretations || []).find(i => i.incident_id === id || i.interpretation_id === id);
const hasInvestigation = inc => Boolean(interpIndex(inc.id) || inc.interpretation_id);

async function refresh() {
  try {
    const ov = await api.overview();
    S.error = null;
    const changed = ov.version !== S.version;
    S.ov = ov; S.version = ov.version;
    S.skew = (ov.server_time || Date.now() / 1000) - Date.now() / 1000;
    renderStatus();
    if (changed) renderView(false);
    if (S.view === "investigation") await loadConsole();
  } catch (e) {
    S.error = e;
    renderStatus();
  }
}

// ---------- routing ----------
function parseHash() {
  const [, view = "briefing", ...rest] = (location.hash || "#/briefing").split("/");
  return { view: ["briefing", "investigation", "graph"].includes(view) ? view : "briefing", param: rest.length ? decodeURIComponent(rest.join("/")) : null };
}
window.addEventListener("hashchange", () => { const r = parseHash(); S.view = r.view; S.param = r.param; renderView(true); });

function renderView(navigated) {
  for (const v of ["briefing", "investigation", "graph"]) $(`#view-${v}`).hidden = v !== S.view;
  document.querySelectorAll(".nav a").forEach(a => { if (a.dataset.view === S.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  if (!S.ov) return;
  if (S.view === "briefing") renderBriefing();
  if (S.view === "investigation") renderInvestigation(navigated);
  if (S.view === "graph") renderGraph(navigated);
}

// ---------- top bar ----------
function renderStatus() {
  const el = $("#status");
  const theme = `<button class="icon-btn" type="button" id="theme-btn" aria-label="Switch between dark and light">${icon(isDark() ? "sun" : "moon")}</button>`;
  if (S.error && !S.ov) { el.innerHTML = `<span>Can't reach the Pulse server. Start it with <b>python -m pulse.web</b>.</span>${theme}`; bindTheme(); return; }
  const t = S.ov?.tick, mode = t?.llm_mode || "mock";
  const spent = S.ov?.spend?.usd ?? t?.spend_usd ?? 0, cap = S.ov?.cap_usd || 12;
  el.innerHTML = `
    <div class="status-item"><b>${t ? fmtTime(t.ts) : "No data yet"}</b><span>Data up to</span></div>
    <div class="status-item hide-narrow" id="loop">${loopHTML()}</div>
    <div class="status-item"><b class="mode" data-mode="${mode}"><i class="mode-dot"></i>${esc(MODE[mode] || mode)}</b><span>Agents</span></div>
    <div class="status-item hide-narrow"><b class="num">$${spent.toFixed(2)} of $${cap.toFixed(0)}</b><span class="spend-bar" aria-hidden="true"><i style="width:${Math.min(100, spent / cap * 100).toFixed(1)}%"></i></span></div>
    ${S.error ? `<span class="faint">Connection lost, retrying</span>` : ""}${theme}`;
  bindTheme();
}
// The always-on loop (`pulse watch`) writes a heartbeat; the top bar shows from it whether it runs and what it does.
function loopState() {
  const w = S.ov?.watch, t = S.ov?.tick;
  if (!w) return { kind: "manual", text: t ? `Check ${t.tick}` : "—" };
  const now = Date.now() / 1000 + (S.skew || 0);
  const failed = w.last_error ? ", last check failed, trying again" : "";
  if (w.stopped === "end of data") return { kind: "off", text: "Stopped at the end of the data" };
  if (w.stopped) return { kind: "off", text: "Not watching" };
  if (w.loading && now - w.beat < 300) return { kind: "busy", text: "Loading the data" };
  if (w.checking && now - w.beat < 900) return { kind: "busy", text: `Checking ${fmtClock(w.checking)} now` };
  if (w.next_check && now < w.next_check + 120) {
    const s = Math.max(0, Math.ceil(w.next_check - now));
    return { kind: "on", text: `Next check in ${s < 90 ? `${s} s` : `${Math.round(s / 60)} min`}${failed}` };
  }
  return { kind: "off", text: "Not watching" };
}
function loopHTML() {
  const l = loopState(), t = S.ov?.tick;
  return `<b class="loop num" data-loop="${l.kind}">${l.kind === "manual" ? "" : `<i class="loop-dot"></i>`}${esc(l.text)}</b>
    <span>${l.kind === "manual" || !t ? "Monitoring loop" : `Monitoring loop, check ${t.tick}`}</span>`;
}
setInterval(() => { const el = $("#loop"); if (el && S.ov?.watch) el.innerHTML = loopHTML(); }, 1000);

function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
}
function bindTheme() {
  $("#theme-btn")?.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("pulse-theme", next); } catch (e) { /* storage blocked */ }
    renderStatus();
  });
}

// ---------- briefing ----------
// CEO wording: "product_views" -> "product views", "FR" -> "France", "PO" -> "purchase order", "KPIs" -> "metrics"
const COUNTRY = { DE: "Germany", FR: "France", NL: "Netherlands", PL: "Poland" };
const ACRONYM = { PO: "purchase order", PSP: "payment provider", AOV: "average order value", CTR: "click-through rate",
  GMV: "revenue", ROAS: "return on ad spend", OOS: "out of stock", SKU: "product", SKUs: "products" };
const plain = s => String(s ?? "")
  .replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, m => m.replace(/_/g, " "))
  .replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, m => m.toLowerCase().replace(/_/g, " "))
  .replace(/\b(DE|FR|NL|PL)\b/g, c => COUNTRY[c])
  .replace(/\b(PO|PSP|AOV|CTR|GMV|ROAS|OOS|SKUs?)\b/g, a => ACRONYM[a])
  .replace(/\bKPIs\b/g, "metrics").replace(/\bKPI\b/g, "metric");
const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const tidy = s => plain(s).replace(/path data >= ?100% complete/g, "data on the path is complete")
  .replace(/>= ?100% complete/g, "complete").replace(/\bBAD\b/g, "bad")
  .replace(/\b(localised|timing|cause metric): /g, "").replace(/ -> /g, " → ")
  .replace(/\b(\d\d)-(\d\d) (\d\d:\d\d)\b/g, (m, mo, d, t) => `${+d} ${MONTH[+mo - 1]} ${t}`);
const plainLabels = snap => { (snap?.nodes || []).forEach(n => { if (n.label) n.label = plain(n.label); }); return snap; };
const lc1 = s => s.charAt(0).toLowerCase() + s.slice(1);

// The loop's log is written for engineers ("conversion_rate -13.3% vs expected (z -2.5) -> watch");
// the briefing says the same thing in plain words. Unknown lines fall back to a generic clean-up.
const segText = key => {
  const [name, ...seg] = String(key).split("|");
  const where = seg.map(s => s.split("=")[1] || s).map(v => COUNTRY[v] || cap(v));
  return kpiLabel(name) + (where.length ? ` (${where.join(", ")})` : "");
};
const issueText = id => {
  const inc = (S.ov?.incidents || []).find(i => i.id === id), m = /^INC-(.+)-\d{10}$/.exec(id);
  return `the ${lc1(segText(inc?.kpi || (m ? m[1] : id)))} issue`;
};
const move = (key, pct) => `${segText(key)} is ${Math.round(Math.abs(+pct))}% ${+pct < 0 ? "below" : "above"} normal`;
const expectedBy = s => plain(s.replace(/^(expected|planned): /, "").split(/ \(| is limited/)[0]);   // the event's name, not its numbers
const briefReasons = w => w.split(", ").map(part => {
  const m = /^(\S+) (opened|escalated|resolved|data issue|innocent change \((.*)\))$/.exec(part);
  if (m) {
    const l = lc1(segText(m[1]));
    return { opened: `new issue on ${l}`, escalated: `${l} got worse`, resolved: `${l} recovered`, "data issue": `${l} data incomplete` }[m[2]]
      || `${l} moved because of the ${plain(m[3])}`;
  }
  const n = /^investigated (\d+)$/.exec(part);
  return n ? `${n[1]} investigation${n[1] === "1" ? "" : "s"} finished` : plain(part);
}).join(", ");
const STATUS = String.raw`(\S+) ([+-]?\d+(?:\.\d+)?)% vs expected \(z [^)]*\)`, INC = String.raw`(INC-[^\s:]+)`;
const ACTIVITY = [
  [/^nothing material changed -> brief numbers refreshed \(no LLM call\)$/, () => "Nothing important changed. The numbers were updated without calling the agents."],
  [/^all (\d+) KPIs ok -> template brief \(no LLM call\)$/, n => `All ${n} headline metrics are normal. No agent was needed.`],
  [RegExp(`^${STATUS}(?:, out of band since (\\d\\d:\\d\\d))? -> opened ${INC}$`), (k, p, since) => `${move(k, p)}${since ? ` since ${since}` : ""}. Opened a new issue.`],
  [RegExp(`^${STATUS} -> ${INC} ESCALATED \\((.*)\\)$`), (k, p, id, t) => `${move(k, p)}. The issue got worse: ${plain(t)}.`],
  [RegExp(`^${STATUS} -> ${INC} persisting \\((\\d+) ticks\\), not re-reported$`), (k, p, id, n) => `${move(k, p)}, ${n} checks in a row. Already reported, so not repeated.`],
  [RegExp(`^(\\S+) back in band -> ${INC} resolved \\(back to normal ?(.*)\\)$`), (k, id, t) => `${segText(k)} is back to normal${t ? " " + plain(t) : ""}. Issue resolved.`],
  [RegExp(`^(\\S+) back in band -> ${INC} resolved`), k => `${segText(k)} is back to normal. Issue resolved.`],
  [RegExp(`^(\\S+) in band -> ${INC} recovering \\((\\d+)/(\\d+)\\)$`), (k, id, a, b) => `${segText(k)} is back in its normal range: ${a} of ${b} quiet hours needed to close the issue.`],
  [RegExp(`^(\\S+) data incomplete -> ${INC} held, no judgement this hour$`), k => `${segText(k)}: data incomplete this hour, the issue is left as it is.`],
  [/^(\S+): (.*) -> data_issue, no alert while the data is incomplete$/, (k, t) => `Data for ${lc1(segText(k))} is incomplete: ${plain(t.replace(": ", ", "))}. No alert until it is complete.`],
  [RegExp(`^${STATUS} -> watch( - downgraded from alert)?(?: \\((.*)\\))?$`), (k, p, dg, w) => `${move(k, p)}. Watching${!w ? "" : /not dismissed/.test(w) ? `: the ${expectedBy(w)} does not explain all of it` : `, expected during the ${expectedBy(w)}`}.`],
  [/^(\S+) back to ok$/, k => `${segText(k)} is back to normal.`],
  [RegExp(`^${INC}: (.*) -> asking the graph interpreter$`), (id, r) => `Asked the investigator agent to look into ${issueText(id)} (${plain(r)}).`],
  [RegExp(`^${INC}: follow-up held, investigated (\\d+) check\\(s\\) ago$`), (id, n) => `The briefing asked for a fresh look at ${issueText(id)}. It was investigated ${n === "1" ? "an hour" : `${n} hours`} ago, so the new investigation waits a little.`],
  [RegExp(`^interpreter on ${INC} \\(([^,]+), [\\d.]+s, \\w+\\): "(.*)" - (\\w+), path .*$`), (id, r, h, c) => `The investigator agent finished: ${plain(h)} (${(CONFIDENCE[c] || c).toLowerCase()}).`],
  [/^brief by orchestrator agent \([^)]*\) because: (.*)$/, w => `The orchestrator agent wrote a new briefing: ${briefReasons(w)}.`],
  [/^brief asked to re-investigate next tick: (.*)$/, l => `The briefing asked for a fresh investigation of: ${l.split(", ").map(segText).join(", ")}.`],
  [/^orchestrator agent failed /, () => "The orchestrator agent could not run, so a standard briefing was written."],
  [RegExp(`^interpreter (?:unavailable|failed).*?${INC}`), id => `The investigator agent could not run on ${issueText(id)}. It will try again.`],
  [/^Graph Handler on inbox\/(\S+): (.*)$/, (f, t) => {
    const added = [...t.matchAll(/added (\S+) = /g)].map(m => plain(m[1])), causes = +(/(\d+) cause edges/.exec(t)?.[1] || 0);
    return added.length ? `The graph agent added ${added.join(", ")} to the metric graph from ${f}${causes ? `, with ${causes} cause link${causes === 1 ? "" : "s"}` : ""}.`
      : `The graph agent read ${f} but added no new metric.`;
  }],
  [/^(\S+) pinned: now monitored like the headline metrics$/, k => `${metricName(k)} was pinned to the briefing and is now monitored like the headline metrics.`],
  [/^(\S+) restored: monitored again as a headline metric$/, k => `${metricName(k)} is back on the briefing.`],
  [/^(\S+) untracked: watched in the background only(, .* closed as no longer tracked)?$/, (k, closed) => `${metricName(k)} was untracked: it is watched in the background only${closed ? " and its open issue was closed" : ""}.`],
  [/^LLM budget above 80% of cap/, () => "The agent budget is above 80% of its cap, so routine re-checks were skipped."],
  [/^(\d+) investigations wanted, running the top (\d+)/, (n, m) => `${n} issues needed investigating: the top ${m} were investigated, the rest wait for the next check.`],
];
function activity(line) {
  for (const [re, fn] of ACTIVITY) {
    const m = re.exec(line);
    if (m) return fn(...m.slice(1));
  }
  return cap(plain(String(line).replace(/INC-[^\s:]+/g, issueText).replace(/ \(z [^)]*\)/g, "")
    .replace(/\s*->\s*/g, ": ").replace(/\bLLM\b/g, "agent")));
}
const longTime = iso => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-GB", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
};
// a metric's name, also for metrics that have no row (untracked ones)
const metricName = id => cap(plain(kpiOf(id)?.label || S.ov?.choices?.labels?.[id] || prettyId(id)));

// The CEO's choices (pulse/choices.py): untrack a row, restore it, pin a metric from a graph.
const CHOICE = { busy: null, msg: "" };
async function choose(action, metric) {
  if (CHOICE.busy) return;
  CHOICE.busy = metric; CHOICE.msg = "";
  if (S.view === "briefing") renderBriefing();
  try {
    const r = await api.choose(action, metric);
    if (r.message && r.message !== "Saved") CHOICE.msg = `${r.message}.`;
    if (action === "untrack") S.open.delete(metric);
  } catch (e) {
    CHOICE.msg = `Could not save: ${e.message}`;
  }
  CHOICE.busy = null;
  await refresh();
  if (S.view === "briefing") renderBriefing();
}

const COUNT_WORD = { alert: n => n === 1 ? "needs attention" : "need attention", data_issue: () => "with incomplete data", watch: () => "being watched", ok: () => "normal" };

async function refreshData() {
  if (S.refreshing) return;
  S.refreshing = true; S.refreshMsg = "";
  renderBriefing();
  try {
    const r = await api.refresh();
  } catch (e) {
    S.refreshMsg = `Refresh failed: ${e.message}`;
  }
  S.refreshing = false;
  await refresh();
  if (S.view === "briefing") renderBriefing();
}

// What happened, in the investigator's own first sentences: magnitude and where, not the path through the graph
// or the verdict (the conclusion line says that). At most two sentences, so a CEO reads it in seconds.
// An investigation summary without the sentences shown elsewhere on the page (first cause, confidence).
const fresh = summary => String(summary || "").split(/(?<=[.!?])\s+(?=[A-Z'"(])/)
  .filter(s => !/->|first cause|Confidence:|I rate|hypothesis|supported/i.test(s));
// In a briefing row: also without the size of the move, which the row already shows.
function gist(summary) {
  const said = fresh(summary).filter(s => !/\d% (below|above) normal|% vs normal/.test(s));
  return cap(plain(said.slice(0, 2).join(" ").replace(/^It is /, "")));
}
// "event:payments-sdk android 7.4.0" -> "payments-sdk android 7.4.0"; a metric id -> its name
const causeName = node => !node ? "" : node.startsWith("event:") ? node.slice(6) : metricName(node.split("|")[0]);

// The latest issue on a metric (the open one if there is one) and the one-line conclusion about it.
function kpiConclusion(k) {
  const inc = incidents().find(i => i.id === k.incident_id) || incidents().find(i => i.kpi === k.name);
  if (!inc) return { inc: null, html: "" };
  const it = interpIndex(inc.id), done = inc.state === "resolved";
  const cause = causeName(it?.cause);
  let html;
  if (it?.partial) html = `<p class="kpi-verdict"><i class="live-dot"></i><span>The investigator agent is looking into it now.</span></p>`;
  else if (!it) html = `<p class="kpi-verdict"><span>Not investigated yet.</span></p>`;
  else html = `<p class="kpi-verdict">${done ? "" : `<span class="chip" data-conf="${esc(it.confidence)}">${esc(CONFIDENCE[it.confidence] || it.confidence)}</span>`}
    <span>${done ? `Resolved ${fmtTime(inc.resolved_at || inc.last_seen)}. ` : ""}${cause ? `Likely cause: <b>${esc(cause)}</b>` : esc(plain(it.headline || ""))}</span></p>`;
  return { inc, it, html };
}

// "expected: FR Black Week teaser campaign (sessions up 19% while orders flat, so ...)" ->
// "Sessions up 19% while orders flat, so .... Expected during the France Black Week teaser campaign."
const noteText = note => {
  const m = /^expected: (.+?)(?: \((.+)\))?$/.exec(String(note || "").trim());
  if (!m) return note ? cap(note.trim()) + "." : "";
  return `${m[2] ? cap(m[2]) + ". " : ""}Expected during the ${m[1]}.`;
};

// A row's subtitle only for what its columns don't already show (value, change, since when, status).
function rowNote(sentence) {
  const m = /as expected during (?:the )?(.+?)\.?$/.exec(sentence);
  return m ? `Expected during the ${m[1]}.` : /a good sign/.test(sentence) ? "A good sign." : "";
}

function kpiDetail(k, item) {
  const text = plain(item?.detail || noteText(k.note));
  const untrack = S.ov.choices ? `<button class="link-btn untrack-btn" type="button" data-untrack="${esc(k.name)}" ${CHOICE.busy ? "disabled" : ""}
    title="Move it to the background: no alerts of its own, still used in investigations">${CHOICE.busy === k.name ? "Untracking…" : "Untrack"}</button>` : "";
  const c = kpiConclusion(k), open = c.inc && c.inc.state !== "resolved";
  // the problem: the investigator's first sentences for an open issue, else the brief's own words
  const problem = open && c.it?.summary && !c.it.partial ? gist(c.it.summary) : text;
  const partial = k.completeness != null && k.completeness < 0.995 ? `Only ${Math.round(k.completeness * 100)}% of this hour's data has arrived.` : "";
  const link = c.inc ? `<a class="concl-link" href="#/investigation/${encodeURIComponent(c.inc.id)}">Open the investigation ${icon("arrow-right")}</a>` : "";
  return `<div class="kpi-more">
    ${problem || partial ? `<p class="kpi-why">${esc([problem, partial].filter(Boolean).join(" "))}</p>` : ""}
    ${c.html}
    ${link || untrack ? `<div class="kpi-actions">${link}${untrack}</div>` : ""}
  </div>`;
}

// What the latest check changed, read from its log (only transitions are logged): metric -> plain words.
function changesAt(t) {
  const out = new Map();
  for (const l of t?.log || []) {
    const name = /^([a-z][\w|=]*?):? /.exec(l)?.[1];
    if (!name) continue;
    if (/ -> opened INC-/.test(l)) out.set(name, "New alert");
    else if (/ ESCALATED /.test(l)) out.set(name, "Worse");
    else if (/ back in band -> | back to ok$/.test(l)) out.set(name, "Back to normal");
    else if (/ -> data_issue/.test(l)) out.set(name, "Data incomplete");
    else if (/ -> watch/.test(l) && !out.has(name)) out.set(name, "Now on watch");
  }
  return out;
}

function renderBriefing() {
  const root = $("#view-briefing");
  const t = S.ov.tick, b = S.ov.brief;
  if (!S.ov.found || !t) {
    root.innerHTML = `<div class="empty">No monitoring data yet. Start the loop with <code>python -m pulse replay</code> or <code>python -m pulse watch</code>, or open the demo with <code>python -m pulse.web --demo</code>.</div>`;
    return;
  }
  const items = new Map((b?.items || []).map(i => [i.kpi, i]));
  const changes = changesAt(t);
  const counts = ["alert", "data_issue", "watch", "ok"].map(s => [s, t.kpis.filter(k => k.severity === s).length]).filter(([, n]) => n);
  const incs = incidents();
  // a bare clock time only for today's events; older ones carry their date
  const at = ts => String(ts || "").slice(0, 10) === String(t.ts).slice(0, 10) ? fmtClock(ts) : fmtTime(ts);
  const issueRows = incs.map(i => {
    const when = i.state === "resolved" ? `Started ${at(i.onset_ts || i.opened_at)}, ${i.closed_reason ? "closed" : "resolved"} ${at(i.resolved_at || i.last_seen)}${i.closed_reason ? ", no longer tracked" : ""}` :
      `Open since ${at(i.onset_ts || i.opened_at)}`;
    const live = interpIndex(i.id)?.partial;
    // "monitor headline - interpreter conclusion": lead with the conclusion
    const [what, ...why] = (i.headline || `${kpiLabel(i.kpi)} moved the wrong way`).split(" - ");
    const fresh = i.opened_at === t.ts && i.state !== "resolved";
    return `<li><a class="issue" href="#/investigation/${encodeURIComponent(i.id)}" data-sev="${i.severity}" data-state="${i.state}"${fresh ? " data-new" : ""}>
      <span class="issue-bar"></span><span>
        <span class="issue-headline">${esc(cap(plain(why.length ? why.join(" - ") : what)))}</span>
        <span class="issue-meta">${fresh ? `<span class="change-tag">New at ${fmtClock(t.ts)}</span>` : ""}<span>${esc(segText(i.kpi))}</span><span>${when}</span>${live ? `<span>Being investigated now</span>` : hasInvestigation(i) ? "" : `<span>Not investigated yet</span>`}</span>
      </span></a></li>`;
  }).join("");
  const kpis = t.kpis.slice().sort((a, b2) => sevRank(a.severity) - sevRank(b2.severity));
  const pinned = new Set(S.ov.choices?.pinned || []);
  const rows = kpis.map(k => {
    // colour only what the monitor flagged: an in-band wobble stays neutral even if it points down
    const tn = k.severity === "alert" || k.severity === "watch" ? tone(k.deviation_pct, k.polarity, true) : "flat";
    const sentence = rowNote(plain(items.get(k.name)?.sentence || ""));
    const open = S.open.has(k.name);
    const comp = k.completeness != null && k.completeness < 0.95 ? ` <span class="faint">(${Math.round(k.completeness * 100)}% of data)</span>` : "";
    const change = changes.get(k.name);
    return `<tr class="kpi-row${open ? " open" : ""}" data-kpi="${esc(k.name)}"${change ? ` data-change` : ""}>
      <td><div class="kpi-label">${esc(cap(plain(k.label)))}${pinned.has(k.name) ? ` <span class="pin-tag">Pinned</span>` : ""}</div>${sentence ? `<div class="kpi-sentence">${esc(sentence)}</div>` : ""}</td>
      <td><span class="kpi-value num">${fmtValue(k.value, k.unit)}</span></td>
      <td class="col-expected"><span class="kpi-expected num">${fmtValue(k.expected, k.unit)}</span></td>
      <td><span class="delta num" data-tone="${tn}">${fmtPct(k.deviation_pct)}${k.onset_ts && k.severity !== "ok" ? `<span class="delta-since">since ${fmtClock(k.onset_ts)}</span>` : ""}</span></td>
      <td class="col-spark">${sparkline(k.series, { tone: tn, unit: k.unit, label: `${k.label}, last 48 hours against its normal range` })}</td>
      <td><span class="sev" data-sev="${k.severity}"><i></i>${SEVERITY[k.severity] || k.severity}</span>${comp}${change ? `<span class="change-tag" title="Changed at the ${fmtClock(t.ts)} check">${change}</span>` : ""}</td>
      <td class="col-toggle"><button class="expand" type="button" aria-expanded="${open}" aria-label="More about ${esc(k.label)}">${icon("chevron-down")}</button></td>
    </tr>
    <tr class="kpi-detail" ${open ? "" : "hidden"}><td colspan="7">${kpiDetail(k, items.get(k.name))}</td></tr>`;
  }).join("");
  // only metrics that have no row of their own: the table already shows the others
  const rowNames = new Set(t.kpis.map(k => cap(plain(k.label))));
  const followUp = [...new Set((b?.follow_up || []).map(x => cap(plain(kpiLabel(x)))))].filter(x => !rowNames.has(x)).map(esc);
  const background = (S.ov.choices?.untracked || []).map(m => `<span class="bg-metric">${esc(metricName(m))}
    <button class="link-btn" type="button" data-restore="${esc(m)}" ${CHOICE.busy ? "disabled" : ""}>${CHOICE.busy === m ? "Restoring…" : "Restore"}</button></span>`);
  root.innerHTML = `
    <div class="brief">
      <div class="brief-main">
        <div class="brief-meta">
          <span>${longTime(b?.ts || t.ts)}</span>
          <button class="refresh-btn" type="button" id="refresh-btn" ${S.refreshing ? `disabled aria-busy="true"` : ""} title="Load the next hour of simulated marketplace data and run the check on it">${icon("refresh-cw")}<span>${S.refreshing ? "Loading new data…" : "Refresh data"}</span></button>
          ${S.refreshMsg ? `<span class="refresh-msg">${esc(S.refreshMsg)}</span>` : ""}
        </div>
        <p class="brief-counts">${counts.map(([s, n]) => `<span class="sev" data-sev="${s}"><i></i>${n} ${COUNT_WORD[s](n)}</span>`).join("")}</p>
        ${followUp.length ? `<p class="follow-up"><b>Watching closely:</b> ${followUp.join(", ")}</p>` : ""}
      </div>
      <aside class="brief-side">
        <div>
          <h2 class="section-title">Issues</h2>
          ${incs.length ? `<ul class="issues">${issueRows}</ul>` : `<p class="muted serif" style="font-size:16px">Nothing is moving the wrong way.</p>`}
        </div>
      </aside>
    <section class="ledger" aria-label="Headline metrics">
      <h2 class="section-title">Headline metrics</h2>
      <table>
        <thead><tr><th>Metric</th><th>Now</th><th class="col-expected">Normal</th><th>Change</th><th class="col-spark">Last 48 hours</th><th>Status</th><th class="col-toggle"></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${background.length ? `<p class="background-line"><span class="faint">Background metrics</span>${background.join("")}</p>` : ""}
      ${CHOICE.msg ? `<p class="refresh-msg choice-msg">${esc(CHOICE.msg)}</p>` : ""}
    </section>
    </div>`;
  $("#refresh-btn", root).addEventListener("click", refreshData);
  root.querySelectorAll("[data-untrack]").forEach(b => b.addEventListener("click", () => choose("untrack", b.dataset.untrack)));
  root.querySelectorAll("[data-restore]").forEach(b => b.addEventListener("click", () => choose("restore", b.dataset.restore)));
  root.querySelectorAll("tr.kpi-row").forEach(tr => tr.addEventListener("click", e => {
    if (e.target.closest("a")) return;
    const name = tr.dataset.kpi, open = !S.open.has(name);
    if (open) S.open.add(name); else S.open.delete(name);
    tr.classList.toggle("open", open);
    tr.nextElementSibling.hidden = !open;
    tr.querySelector(".expand").setAttribute("aria-expanded", String(open));
  }));
  bindSparks(root);
}

// ---------- investigation ----------
async function renderInvestigation(navigated) {
  const view = $("#view-investigation");
  if (!$("#inv-main")) view.innerHTML = `<div id="console-slot"></div><div id="inv-main"></div>`;
  if (S.con.data) renderConsole(); else loadConsole();
  const root = $("#inv-main");
  const incs = incidents();
  if (!incs.length) {
    S.inv.incidentId = null;
    root.innerHTML = `<div class="empty">No investigations yet.</div>`;
    return;
  }
  let id = S.param && incs.some(i => i.id === S.param) ? S.param : (S.inv.incidentId && incs.some(i => i.id === S.inv.incidentId) ? S.inv.incidentId : (incs.find(hasInvestigation) || incs[0]).id);
  const incidentChanged = id !== S.inv.incidentId;
  S.inv.incidentId = id;
  const inc = incs.find(i => i.id === id);

  // tabs
  const tabs = incs.map(i => `<button class="inv-tab" type="button" role="tab" data-id="${esc(i.id)}" data-sev="${i.severity}" aria-selected="${i.id === id}">
      <span class="issue-bar"></span><span><b>${esc(kpiLabel(i.kpi))}</b><span>${i.state === "resolved" ? "Resolved" : i.state === "persisting" ? `Ongoing, ${i.ticks_seen} checks` : `Opened ${fmtClock(i.opened_at)}`}${interpIndex(i.id)?.partial ? ", investigating now" : ""}</span></span></button>`).join("");

  if (incidentChanged || navigated || !$("#inv-shell")) {
    S.inv.key = "";
    root.innerHTML = `<div class="inv-tabs" role="tablist" aria-label="Issues">${tabs}</div>
      <div id="inv-shell" class="inv-body">
        <div class="orrery-panel">
          <div id="orrery-host"></div>
          <div class="orrery-tools">
            <button class="icon-btn" type="button" data-zoom="in" aria-label="Zoom in">${icon("zoom-in")}</button>
            <button class="icon-btn" type="button" data-zoom="out" aria-label="Zoom out">${icon("zoom-out")}</button>
            <button class="icon-btn" type="button" data-zoom="fit" aria-label="Fit the graph">${icon("scan")}</button>
          </div>
        </div>
        <article class="reading" id="reading" aria-live="polite"></article>
      </div>`;
    root.querySelectorAll(".inv-tab").forEach(b => b.addEventListener("click", () => { location.hash = `#/investigation/${encodeURIComponent(b.dataset.id)}`; }));
    S.inv.orrery = createOrrery($("#orrery-host"), { onSelect: nodeId => jumpToNode(nodeId) });
    root.querySelectorAll("[data-zoom]").forEach(b => b.addEventListener("click", () => {
      const o = S.inv.orrery; if (!o) return;
      b.dataset.zoom === "in" ? o.zoomIn() : b.dataset.zoom === "out" ? o.zoomOut() : o.fit();
    }));
  } else {
    $(".inv-tabs").innerHTML = tabs;
    root.querySelectorAll(".inv-tab").forEach(b => b.addEventListener("click", () => { location.hash = `#/investigation/${encodeURIComponent(b.dataset.id)}`; }));
  }
  await loadInvestigation(inc);
}

async function loadInvestigation(inc) {
  const reading = $("#reading");
  let interp = null;
  for (const iid of [inc.id, inc.interpretation_id].filter(Boolean)) {
    try { interp = await api.interpretation(iid); break; } catch (e) { /* try next id */ }
  }
  if (!interp) {
    $("#orrery-host").innerHTML = "";
    reading.innerHTML = `<div class="empty">${esc(tidy(inc.headline || kpiLabel(inc.kpi)))}<br>Not investigated yet.</div>`;
    S.inv.orrery = createOrrery($("#orrery-host"), { onSelect: jumpToNode });
    return;
  }
  const snapId = interp.snapshot_id || inc.snapshot_id;
  let snap = S.inv.snap && S.inv.snap.snapshot_id === snapId ? S.inv.snap : null;
  if (!snap && snapId) { try { snap = plainLabels(await api.snapshot(snapId)); } catch (e) { snap = null; } }
  const key = `${interp.interpretation_id}|${(interp.trace || []).length}|${interp.partial}`;
  if (key === S.inv.key) return;   // nothing new
  const firstLoad = !S.inv.interp || S.inv.interp.interpretation_id !== interp.interpretation_id;
  S.inv.key = key; S.inv.interp = interp; S.inv.snap = snap;

  if (snap) {
    // always the whole picture so far: the relevant metrics highlighted (a running investigation grows step by step)
    S.inv.frame = S.inv.orrery.setData(snap, interp, { partial: interp.partial }).length - 1;
  } else {
    $("#orrery-host").innerHTML = `<div class="empty" style="margin:24px">Graph not available.</div>`;
  }
  // the console followed the agent here: bring the graph into view
  const followed = S.con.follow === inc.id && (firstLoad || interp.partial);
  if (followed) S.con.follow = null;
  renderReading();
  setFrame(S.inv.frame);
  const tabs = $(".inv-tabs");   // bring the graph into view below the console, once it is drawn
  if (followed && tabs) requestAnimationFrame(() => {
    if (tabs.getBoundingClientRect().top > innerHeight * 0.5) tabs.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  clearTimeout(S.inv.livePoll);
  if (interp.partial) S.inv.livePoll = setTimeout(() => { if (S.view === "investigation" && S.inv.incidentId === inc.id) loadInvestigation(inc); }, 1500);
}

function mountLegend(slot, present = null) {
  if (!slot) return;
  slot.innerHTML = legendHTML(S.legendCollapsed, S.ov?.legend, present);
  slot.querySelector("[data-legend-toggle]").addEventListener("click", () => {
    S.legendCollapsed = !S.legendCollapsed;
    try { localStorage.setItem("pulse-legend", S.legendCollapsed ? "collapsed" : "open"); } catch (e) { /* storage blocked */ }
    mountLegend(slot, present);
  });
}

// Legend tokens the current graph actually draws (null when the server sent no styles: show the full legend).
function legendTokens(snap, interp) {
  const nodes = snap?.nodes || [];
  if (!nodes.some(n => n.style)) return null;
  const t = new Set();
  for (const n of nodes) {
    const st = n.style || {};
    if (st.fill) t.add(st.fill);
    if (st.shape === "sun") t.add("kpi");
    if (st.shape === "diamond") t.add("diamond");
    if (st.faded && st.fill !== "stale") t.add("faded");
    if (st.badge) t.add("unusual");
    (st.rings || []).forEach(r => t.add(r));
  }
  for (const e of snap?.edges || []) if (e.style?.line) t.add(e.style.line);
  if (interp) {
    if (interp.first_cause) t.add("first_cause");
    if ((interp.path || []).length > 1) t.add("path");
    if (interp.symptoms?.length) t.add("symptom");
    if (interp.unusual?.length) t.add("unusual");
    if (interp.ruled_out?.length) t.add("faded");
  }
  return t;
}

function setFrame(i) {
  const frames = S.inv.orrery?.frames || [];
  if (!frames.length) return;
  S.inv.frame = Math.max(0, Math.min(i, frames.length - 1));
  S.inv.orrery.setFrame(S.inv.frame);
}

// a click on a metric (in the graph or the text) points it out; the picture stays the whole investigation
function jumpToNode(nodeId) {
  S.inv.orrery?.focusNode?.(nodeId);
}

// Prefer the snapshot's human label for a node id; fall back to a cleaned-up id.
function labelOf(id) {
  const n = (S.inv.snap?.nodes || []).find(x => x.id === id) || (S.graph.snap?.nodes || []).find(x => x.id === id);
  if (/(^|:)data_issue$/.test(id)) return "Data quality";
  return plain(n?.label ? prettyId(n.label) : prettyId(id));
}

function nodeRef(id) {
  return `<button class="node-ref" type="button" data-node="${esc(id)}">${esc(labelOf(id))}</button>`;
}

function findingList(list) {
  if (!list || !list.length) return "";
  return `<ul>${list.map(f => `<li>${nodeRef(f.node)} <span class="serif">${esc(tidy(f.why || ""))}</span></li>`).join("")}</ul>`;
}

function renderReading() {
  const it = S.inv.interp, el = $("#reading");
  if (!it) return;
  const steps = it.trace || [];
  if (it.partial) {   // no conclusions until the interpreter has finished: only what it has found so far
    el.innerHTML = `<div class="live-banner"><i class="live-dot"></i>Investigating now, ${steps.length} ${steps.length === 1 ? "step" : "steps"} so far</div>
      <h2 class="conclusion-headline">${esc(labelOf(it.kpi))}</h2>`;
  } else {
    const facts = [];
    if (it.first_cause) facts.push(["First cause", `${nodeRef(it.first_cause.node)}<div class="serif">${esc(tidy(it.first_cause.why || ""))}</div>`]);
    if (it.symptoms?.length) facts.push(["Knock-on effects", findingList(it.symptoms)]);
    if (it.unusual?.length) facts.push(["Also unusual", findingList(it.unusual)]);
    if (it.ruled_out?.length) facts.push(["Ruled out", findingList(it.ruled_out)]);
    if (it.missing_data?.length) facts.push(["Missing data", findingList(it.missing_data)]);
    if (it.new_connections?.length) facts.push(["New links", `<ul>${it.new_connections.map(e => `<li>${nodeRef(e.source)} to ${nodeRef(e.target)} <span class="serif">${e.status === "ambiguous" ? "weak link" : "added"}</span></li>`).join("")}</ul>`]);
    if (it.uncertainty) facts.push(["Still uncertain", `<span class="serif">${esc(tidy(it.uncertainty))}</span>`]);
    if (it.watch_next?.length) facts.push(["Keep watching", it.watch_next.map(nodeRef).join(", ")]);
    const recalled = incidents().find(i => i.id === S.inv.incidentId)?.recalled || [];
    if (recalled.length) facts.push(["Seen before", `<ul>${recalled.map(r => `<li class="serif">${esc(tidy(r))}</li>`).join("")}</ul>`]);
    el.innerHTML = `
      <div class="verdict"><span class="chip" data-conf="${it.confidence}">${CONFIDENCE[it.confidence] || it.confidence}</span>
        <span>${fmtTime(it.ts)}${it.cost_usd ? ` · $${it.cost_usd.toFixed(2)}` : ""}</span></div>
      <h2 class="conclusion-headline">${esc(cap(tidy(it.headline || "")))}</h2>
      ${fresh(it.summary).length ? `<p class="conclusion-summary">${esc(tidy(fresh(it.summary).join(" ")))}</p>` : ""}
      ${facts.length ? `<dl class="facts">${facts.map(([dt, dd]) => `<div class="fact"><dt>${dt}</dt><dd>${dd}</dd></div>`).join("")}</dl>` : ""}`;
  }
  el.querySelectorAll(".node-ref").forEach(b => b.addEventListener("click", () => jumpToNode(b.dataset.node)));
}

// ---------- event console (top of the investigations page) ----------
// A live simulated market (server: pulse/web/console.py): inject a scenario, run hours, watch Pulse's agents react.
// The log streams while Pulse works, grouped by market hour: one line for what the monitor saw change, one per
// investigation (live while it runs, then its conclusion; the steps are drawn in the graph below), the briefing; the pipeline shows which agent works now; each injected scenario is scored against what was injected.
const CON_GROUP = { fault: "Problems", innocent: "Planned changes", data: "Data problems" };
const CON_KIND = { fault: "Problem", innocent: "Planned change", data: "Data problem" };
const CON_QUIET = /^(nothing material changed|all \d+ KPIs ok) /;
const AGENT_MODE = { live: "Live", replay: "Recorded", mock: "Scripted" };
const AGENT_MODEL = { live: ["Claude Sonnet 5.5", "Claude Opus 5.5"], replay: ["Recorded answers", "Recorded answers"],
  mock: ["Scripted, no AI", "Scripted, no AI"] };
const ANSWERED_BY = { live: "answered by Claude", replay: "recorded Claude answer", mock: "scripted" };
const RE_ASK = /^(INC-[^\s:]+): (.*) -> asking the graph interpreter$/;
const RE_INV = /^(INC-[^\s:]+): investigator agent started$/;
const RE_STEP = /^(INC-[^\s:]+) step (\d+): (.*)$/;
const RE_CONCL = /^interpreter on (INC-[^\s:]+) \((.*?), ([\d.]+)s, (\w+)\): "(.*)" - (\w+), path (.*)$/;
const RE_BRIEF = /^brief by orchestrator agent \(([\w ]+?), (.*?), \$([\d.]+)\) because: (.*)$/;
const RE_TICK = /^\[[^\]]+\] tick\s+\d+ \| (.*?) \| (.*)$/;
const clockText = ts => {
  const d = new Date(String(ts).replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};
const hm = ts => String(ts || "").slice(11, 16);
const hoursBetween = (a, b) => Math.round((new Date(String(b).replace(" ", "T")) - new Date(String(a).replace(" ", "T"))) / 3.6e6);
const segWords = seg => Object.values(seg || {}).map(v => COUNTRY[v] || cap(String(v))).join(", ");
const issueOf = id => lc1(segText((S.ov?.incidents || []).find(i => i.id === id)?.kpi || /^INC-(.+)-\d{10}$/.exec(id)?.[1] || id));

// The log as market hours: each hour holds the monitor's decisions (summed up in one line), the investigations and
// the briefing, closed by the hour's verdict. Market, injection, cost and error lines stand on their own.
function conGroups(log) {
  const out = [];
  let g = null;
  const open = t => { g = { t, items: [], inv: {}, mon: null, check: null }; out.push(g); };
  for (const e of log) {
    if (["market", "inject", "error", "cost"].includes(e.kind)) { out.push({ solo: e }); g = null; continue; }
    if (e.kind === "checking") { open(e.t); continue; }
    if (!g || g.t !== e.t) open(e.t);
    if (e.kind === "check") { g.check = e; continue; }
    const id = (RE_ASK.exec(e.text) || RE_INV.exec(e.text) || RE_STEP.exec(e.text) || RE_CONCL.exec(e.text))?.[1];
    if (id) {
      if (!g.inv[id]) { g.inv[id] = { id, reason: "", steps: [], concl: null }; g.items.push({ inv: g.inv[id] }); }
      const b = g.inv[id], ask = RE_ASK.exec(e.text), st = RE_STEP.exec(e.text), c = RE_CONCL.exec(e.text);
      if (ask) b.reason = ask[2];
      if (st) b.steps.push(st[3]);
      if (c) b.concl = { reason: c[2], seconds: c[3], mode: c[4], headline: c[5], confidence: c[6] };
      continue;
    }
    if (e.kind !== "note" && !RE_BRIEF.test(e.text) && !/^Graph Handler on |^brief asked to re-investigate/.test(e.text)
        && !CON_QUIET.test(e.text)) {
      if (!g.mon) { g.mon = []; g.items.push({ mon: g.mon }); }
      g.mon.push(e.text);
      continue;
    }
    g.items.push({ e });
  }
  return out;
}

// The investigator's walk as a few logical steps (the graph shows every move): the path it followed down the graph,
// where the change sits, the change it matched, what it ruled out. Loading the graph and its neighbours is left out.
function logicalSteps(steps) {
  const path = [], out = [], ruled = [];
  for (const x of steps) {
    let m;
    if ((m = /^Ruled out ([^:]+):/.exec(x))) ruled.push(m[1]);
    else if ((m = /^(.+?) is [\d.]+% (?:below|above) normal .*carries \d+% of the change/.exec(x))) path.push(m[1]);
    else if ((m = /^(.+?) carries (\d+)% of the change in /.exec(x))) {
      if (!out.some(o => o.startsWith("Found where"))) out.push(`Found where it is: ${m[1]}, ${m[2]}% of the change`);
    }
    else if (/recent change/.test(x)) {
      m = /best match: '([^']+)' \((\w+)/.exec(x);
      out.push(m ? `Matched a recent ${m[2].replace(/_/g, " ").replace(/^tv /, "TV ")}: ${m[1]}` : "Found no recent change nearby");
    }
  }
  return [path.length ? `Traced it through the graph${path.length === 1 ? " to" : ":"} ${path.join(" → ")}` : "", ...out,
    ruled.length ? `Ruled out: ${ruled.join(", ")}` : ""].filter(Boolean).map(tidy);
}

// The monitor's hour in one line: what changed. Issues that simply continue and recovery counts are left out.
function monitorSummary(lines) {
  const b = { opened: [], worse: [], back: [], watch: [], data: [] }, other = [];
  for (const x of lines) {
    const name = metricName(/^[\w.]+/.exec(x)?.[0] || "");
    if (/ persisting \(| in band -> INC-\S+ recovering|follow-up held|investigations wanted|back to ok$/.test(x)) continue;
    if (/ -> opened INC-/.test(x)) b.opened.push(name);
    else if (/ ESCALATED /.test(x)) b.worse.push(name);
    else if (/ back in band -> /.test(x)) b.back.push(name);
    else if (/ data incomplete -> | -> data_issue/.test(x)) b.data.push(name);
    else if (/ -> watch/.test(x)) {
      const why = /expected: ([^;(]+)/.exec(x)?.[1]?.trim();
      b.watch.push(why ? `${name}, expected during the ${plain(why)}` : name);
    } else other.push(activity(x));
  }
  const part = (label, xs) => xs.length ? `${label}: ${[...new Set(xs)].join(", ")}` : "";
  return [part(b.opened.length === 1 ? "New issue" : "New issues", b.opened), part("Worse", b.worse),
    part("Back to normal", b.back), part("Watching", b.watch), part("Incomplete data", b.data), ...other]
    .filter(Boolean).join(" · ");
}

function conItem(it, busyHere) {
  if (it.mon) {
    const text = monitorSummary(it.mon);
    return text ? `<li class="con-item" data-agent="monitor"><span class="con-agent">Monitor</span><span>${esc(text)}</span></li>` : "";
  }
  if (it.inv) {
    // the conclusion (or the agent working), then its logical steps
    const b = it.inv, c = b.concl, n = b.steps.filter(x => !/^Conclusion \(/.test(x)).length, logic = logicalSteps(b.steps);
    const head = c ? `<span class="chip" data-conf="${c.confidence}">${CONFIDENCE[c.confidence] || c.confidence}</span>
        <span class="serif">${esc(cap(tidy(c.headline)))}</span>
        <a class="con-open" href="#/investigation/${encodeURIComponent(b.id)}">Open</a>`
      : `<span>Investigating ${esc(issueOf(b.id))}</span>${busyHere ? `<span class="con-working"><i class="live-dot"></i>${n ? `step ${n}` : "starting"}</span>` : ""}`;
    return `<li class="con-item" data-agent="investigator"><span class="con-agent">Investigator</span><div>
      <div class="con-concl">${head}</div>
      ${logic.length ? `<ol class="con-logic">${logic.map(x => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}</div></li>`;
  }
  const e = it.e, br = RE_BRIEF.exec(e.text);
  if (br) return `<li class="con-item" data-agent="briefing"><span class="con-agent">Briefing</span>
    <span>Updated the CEO briefing.${+br[3] > 0 ? ` <span class="faint num">$${(+br[3]).toFixed(3)}</span>` : ""}</span></li>`;
  if (e.kind === "note") {
    if (!busyHere) return "";
    if (/^briefing agent started/.test(e.text)) return `<li class="con-item" data-agent="briefing"><span class="con-agent">Briefing</span><span class="con-working"><i class="live-dot"></i>Writing the CEO briefing</span></li>`;
    if (/^graph handler agent started/.test(e.text)) return `<li class="con-item" data-agent="graph"><span class="con-agent">Graph</span><span class="con-working"><i class="live-dot"></i>Placing a new metric in the graph</span></li>`;
    return "";
  }
  if (/^Graph Handler on /.test(e.text)) return `<li class="con-item" data-agent="graph"><span class="con-agent">Graph</span><span>${esc(activity(e.text))}</span></li>`;
  if (CON_QUIET.test(e.text) || /^brief asked to re-investigate/.test(e.text)) return "";
  return `<li class="con-item" data-agent="monitor"><span class="con-agent">Monitor</span><span>${esc(activity(e.text))}</span></li>`;
}

function conGroupHTML(g, last, busy) {
  if (g.solo) {
    if (/^Market ran /.test(g.solo.text)) return "";
    const e = g.solo, tag = { market: "Market", inject: "Injected", error: "Error", cost: "Cost" }[e.kind];
    return `<li class="con-solo" data-kind="${e.kind}"><time>${esc(hm(e.t))}</time><span class="con-agent">${tag}</span><span>${esc(e.text)}</span></li>`;
  }
  const running = busy && last && !g.check;
  // drop a finished note only when something later in the hour replaced it (the briefing line, the conclusion)
  const items = g.items.filter((it, i) => !(it.e?.kind === "note" && (!running || g.items.slice(i + 1).some(x => x.e && RE_BRIEF.test(x.e.text)))));
  const m = g.check && RE_TICK.exec(g.check.text);
  const verdict = running ? `<span class="con-working"><i class="live-dot"></i>Checking ${esc(hm(g.t))}</span>`
    : m ? esc(m[1] === "all ok" ? "All headline metrics normal" : tidy(m[2].replace(/^NEW: /, ""))) : "";
  const rows = items.map(it => conItem(it, running)).join("");
  const said = rows && Object.values(g.inv).some(b => b.concl && m && m[2].includes(b.concl.headline));
  return `<li class="con-hour"${running ? ` data-running="true"` : ""}><time>${esc(hm(g.t))}</time><div>
    ${verdict && !said ? `<div class="con-verdict">${verdict}</div>` : ""}
    ${rows ? `<ol class="con-items">${rows}</ol>` : ""}</div></li>`;
}

// Which part of the pipeline works now, and what each part did last.
function conPipeline(c, groups) {
  const model = AGENT_MODEL[c.agents] || AGENT_MODEL.mock;
  const hours = groups.filter(g => !g.solo), lastHour = hours.at(-1), lastEntry = c.log.at(-1);
  let active = null;
  if (c.busy && lastEntry) {
    if (["market", "inject"].includes(lastEntry.kind) || /^Starting/.test(c.job || "")) active = "market";
    else if (lastEntry.kind === "note" && /^briefing agent/.test(lastEntry.text)) active = "briefing";
    else if (lastEntry.kind === "note") active = "investigator";
    else active = "monitor";
  }
  // each card says what its stage is doing or when it last ran; what it found is in the log below, once
  const invs = hours.flatMap(g => Object.values(g.inv)), lastInv = invs.at(-1), done = invs.filter(b => b.concl).length;
  const injected = c.session?.injected?.length || 0;
  const briefHour = hours.filter(g => g.items.some(it => it.e && RE_BRIEF.test(it.e.text))).at(-1);
  const stage = (key, name, sub, text, doneText, idleText) => {
    const on = active === key;
    return `<div class="con-stage" data-stage="${key}" data-state="${on ? "active" : doneText ? "done" : "idle"}">
      <div class="con-stage-head"><i class="con-stage-dot"></i><b>${name}</b><span>${esc(sub)}</span></div>
      <div class="con-stage-text">${esc(on ? text : doneText || idleText)}</div></div>`;
  };
  const steps = lastInv ? lastInv.steps.filter(x => !/^Conclusion \(/.test(x)).length : 0;
  return `<div class="con-pipeline" aria-label="What the agents are doing">
    ${stage("market", "Market", "Simulator", c.job || "Running",
      c.session ? (injected ? `${injected} ${injected === 1 ? "scenario" : "scenarios"} injected` : "Quiet market") : "", "")}
    ${stage("monitor", "Monitor", "Statistics, no AI", `Checking ${hm(lastHour?.t)}`,
      lastHour?.check ? `Last check ${hm(lastHour.t)}` : "", "Checks every market hour")}
    ${stage("investigator", "Investigator agent", model[0], steps ? `Working, step ${steps}` : "Starting the investigation",
      done ? `${done} ${done === 1 ? "investigation" : "investigations"} finished` : "", "Starts when an alert opens")}
    ${stage("briefing", "Briefing agent", model[1], "Writing the CEO briefing",
      briefHour ? `Updated at ${hm(briefHour.t)}` : "", "Writes the CEO briefing")}
  </div>`;
}

// Each injected scenario next to what Pulse made of it (scored like the offline scorecard).
function conScenario(s, r) {
  // what really changed (Pulse is not told) in one line, and the verdict as a few marks
  const mark = (v, text, title = "") => `<span class="scn-mark" data-ok="${v === true ? "yes" : v === false ? "no" : "wait"}"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`;
  let marks;
  if (s.kind === "fault") {
    const after = r?.alert_at && s.start ? hoursBetween(s.start, r.alert_at) : null;
    marks = !r?.detected ? mark(null, "Waiting for an alert")
      : mark(true, `Alert ${hm(r.alert_at)}${after ? `, ${after} h after it started` : ""}`)
        + (s.event ? mark(!!r.cause_named, "Cause", `It was ${plain(s.event)}`) : "")
        + (Object.keys(s.segment).length ? mark(!!r.segment_named, "Place", `It was ${segWords(s.segment)}`) : "");
  } else if (s.kind === "innocent") {
    marks = r?.false_alert ? mark(false, "Alert raised") : r?.explained ? mark(true, "Matched, no alert") : mark(null, r?.seen ? "Being watched" : "No effect yet");
  } else {
    marks = r?.false_alert ? mark(false, "Alert raised") : r?.data_issue_seen ? mark(true, "Seen as incomplete data") : mark(null, "Not seen yet");
  }
  const inc = r?.incidents?.[0], truth = cap(plain(String(s.cause || "").split(": ")[0]));
  return `<div class="con-scn" data-kind="${s.kind}">
    <div class="con-scn-head"><i></i><span>${CON_KIND[s.kind] || "Scenario"} · ${esc(hm(s.at))}</span>
      ${inc ? `<a href="#/investigation/${encodeURIComponent(inc)}">Investigation ${icon("arrow-right")}</a>` : ""}</div>
    <div class="con-scn-title">${esc(s.title)}</div>
    ${truth ? `<p class="con-scn-truth" title="What really changed (Pulse is not told): ${esc(plain(s.cause))}">${esc(truth)}</p>` : ""}
    <div class="con-scn-marks">${marks}</div>
  </div>`;
}

function renderConsole() {
  const slot = $("#console-slot"), c = S.con.data;
  if (!slot) return;
  if (!c?.enabled) { slot.innerHTML = ""; S.con.key = ""; return; }
  const live = c.source === "live", sess = live ? c.session : null, busy = c.busy;
  const key = JSON.stringify([c.source, busy, c.job, c.agents, c.spend, sess?.clock, c.log.at(-1)?.n, S.con.error,
    (S.ov?.incidents || []).length, S.ov?.brief?.headline, c.results]);
  if (key === S.con.key && $("#console")) return;
  S.con.key = key;
  const dis = busy ? "disabled" : "";
  const options = ["fault", "innocent", "data"].map(k => `<optgroup label="${CON_GROUP[k]}">${c.catalog.filter(x => x.kind === k)
    .map(x => `<option value="${x.id}" ${x.id === S.con.scenario ? "selected" : ""}>${esc(x.title)}</option>`).join("")}</optgroup>`).join("");
  const agents = !live ? "" : `<div class="con-agents"><span class="faint">Agents</span><div class="segmented" role="group" aria-label="Agents">${
    ["live", "replay", "mock"].map(m => `<button type="button" data-agents="${m}" aria-pressed="${c.agents === m}" ${busy || (m === "live" && !c.has_key) ? "disabled" : ""}>${AGENT_MODE[m]}</button>`).join("")}</div>
    ${c.spend > 0 ? `<span class="faint num">$${c.spend.toFixed(2)} spent</span>` : ""}</div>`;
  const controls = !live ? "" : !c.session
    ? `<div class="con-controls"><button class="btn btn-primary" type="button" data-con="start" ${dis}>Start live market</button></div>`
    : `<div class="con-controls">
        <select id="con-scenario" aria-label="Scenario" ${dis}>${options}</select>
        <button class="btn btn-primary" type="button" data-con="inject" ${dis}>Inject</button>
        <div class="segmented" role="group" aria-label="Run the market">${[1, 3, 6].map(h => `<button type="button" data-con="run" data-hours="${h}" ${dis}>+${h} h</button>`).join("")}</div>
        <button class="btn" type="button" data-con="restart" ${dis}>Restart</button>
      </div>`;
  const status = busy ? `<span class="con-status"><i class="live-dot"></i>${esc(c.job)}</span>`
    : sess ? `<span class="con-status">Market at <b>${esc(clockText(sess.last_hour))}</b></span>` : "";
  const groups = sess ? conGroups(c.log) : [];
  const lastHourIdx = groups.map(g => !g.solo).lastIndexOf(true);
  const rows = groups.map((g, i) => conGroupHTML(g, i === lastHourIdx, busy)).join("");
  const results = new Map((c.results || []).map(r => [`${r.id}|${r.at}`, r]));
  const scenarios = (sess?.injected || []).slice().reverse().map(s => conScenario(s, results.get(`${s.id}|${s.at}`))).join("");
  slot.innerHTML = `<section class="console" id="console" aria-label="Event console">
    <div class="con-head"><h2 class="section-title">Event console</h2>
      <div class="segmented" role="group" aria-label="Data shown">
        <button type="button" data-source="recorded" aria-pressed="${!live}">Recorded run</button>
        <button type="button" data-source="live" aria-pressed="${live}">Live market</button></div>
      ${agents}${status}</div>
    ${controls}
    ${S.con.error ? `<p class="con-error">${esc(S.con.error)}</p>` : ""}
    ${sess ? conPipeline(c, groups) : ""}
    ${rows || scenarios ? `<div class="con-body">
      <ol class="con-log" id="con-log">${rows}</ol>
      ${scenarios ? `<div class="con-scns">${scenarios}</div>` : ""}</div>` : ""}
  </section>`;
  slot.querySelectorAll("[data-source]").forEach(b => b.addEventListener("click", () => {
    if (b.getAttribute("aria-pressed") !== "true") consoleAct({ action: "source", source: b.dataset.source });
  }));
  slot.querySelectorAll("[data-agents]").forEach(b => b.addEventListener("click", () => {
    if (b.getAttribute("aria-pressed") !== "true") consoleAct({ action: "agents", mode: b.dataset.agents });
  }));
  slot.querySelectorAll("[data-con]").forEach(b => b.addEventListener("click", () => {
    const a = b.dataset.con;
    if (a === "inject") consoleAct({ action: "inject", id: $("#con-scenario").value });
    else if (a === "run") {
      S.con.followed = new Set(); S.con.runFrom = c.log.at(-1)?.n || 0;
      consoleAct({ action: "run", hours: Number(b.dataset.hours) });
    }
    else if (a === "start" || confirm("Restart the live market from a quiet history?")) consoleAct({ action: "start" });
  }));
  $("#con-scenario")?.addEventListener("change", e => { S.con.scenario = e.target.value; });
  const log = $("#con-log");
  if (log) log.scrollTop = log.scrollHeight;
}

// After +N h the investigation on screen follows the agent: each investigation it starts opens by itself (a running
// one streams into the graph, a finished one plays from the start).
function followAgents() {
  const c = S.con.data;
  if (!c || c.source !== "live" || S.view !== "investigation" || S.con.runFrom == null) return;
  const m = c.log.filter(e => e.n > S.con.runFrom && e.kind === "note")
    .map(e => RE_INV.exec(e.text) || RE_STEP.exec(e.text)).filter(Boolean).at(-1);
  const id = m?.[1];
  if (!id || S.con.followed.has(id) || !(S.ov?.incidents || []).some(i => i.id === id)) return;
  S.con.followed.add(id);
  S.con.follow = id;
  if (S.inv.incidentId !== id) location.hash = `#/investigation/${encodeURIComponent(id)}`;
  else { S.inv.key = ""; renderInvestigation(false); }
}

async function loadConsole() {
  try { S.con.data = await api.console(); } catch (e) { return; }
  renderConsole();
  followAgents();
  clearTimeout(S.con.timer);
  if (S.con.data.busy) S.con.timer = setTimeout(refresh, 1000);   // follow a running job closely
}

async function consoleAct(body) {
  try {
    S.con.data = await api.consoleAct(body);
    S.con.error = "";
  } catch (e) {
    S.con.error = e.message;
  }
  renderConsole();
  await refresh();
}


// ---------- metric graph ----------
async function renderGraph(navigated) {
  const root = $("#view-graph");
  const snaps = S.ov.snapshots || [];
  if (!snaps.length) {
    root.innerHTML = `<div class="empty">No graph snapshots yet. The graph handler writes one each time the interpreter investigates a metric.</div>`;
    return;
  }
  const id = S.param && snaps.some(s => s.snapshot_id === S.param) ? S.param : (S.graph.snapshotId && snaps.some(s => s.snapshot_id === S.graph.snapshotId) ? S.graph.snapshotId : snaps[0].snapshot_id);
  const changedSnap = id !== S.graph.snapshotId;
  S.graph.snapshotId = id;
  if (changedSnap || navigated || !$("#graph-shell")) {
    root.innerHTML = `
      <div class="graph-controls">
        <div class="field"><label for="snap-select">Metric</label>
          <select id="snap-select">${snaps.map(s => `<option value="${esc(s.snapshot_id)}" ${s.snapshot_id === id ? "selected" : ""}>${esc(kpiLabel(s.kpi))} (${esc(prettyId(s.center))}), ${fmtTime(s.ts)}</option>`).join("")}</select></div>
        <div class="field"><span class="label" id="depth-label">Show</span><div class="segmented" role="group" aria-labelledby="depth-label" id="depth-seg"></div></div>
      </div>
      <div id="graph-shell" class="inv-body">
        <div class="orrery-panel">
          <div id="graph-host"></div>
          <div class="orrery-tools">
            <button class="icon-btn" type="button" data-gzoom="in" aria-label="Zoom in">${icon("zoom-in")}</button>
            <button class="icon-btn" type="button" data-gzoom="out" aria-label="Zoom out">${icon("zoom-out")}</button>
            <button class="icon-btn" type="button" data-gzoom="fit" aria-label="Fit the graph">${icon("scan")}</button>
          </div>
          <div id="graph-legend"></div>
        </div>
        <aside class="reading">
          <div id="node-card"></div>
          <div>
            <h2 class="section-title">Changes made by the graph handler</h2>
            <p class="section-note">New metrics and links are added only after a check against the data.</p>
            <ul class="changes" id="changes"></ul>
          </div>
        </aside>
      </div>`;
    $("#snap-select").addEventListener("change", e => { location.hash = `#/graph/${encodeURIComponent(e.target.value)}`; });
    S.graph.orrery = createOrrery($("#graph-host"), { onSelect: nid => { S.graph.selected = nid; renderNodeCard(); } });
    root.querySelectorAll("[data-gzoom]").forEach(b => b.addEventListener("click", () => {
      const o = S.graph.orrery; b.dataset.gzoom === "in" ? o.zoomIn() : b.dataset.gzoom === "out" ? o.zoomOut() : o.fit();
    }));
    try { S.graph.snap = plainLabels(await api.snapshot(id)); } catch (e) { S.graph.snap = null; }
    mountLegend($("#graph-legend"), legendTokens(S.graph.snap, null));
    if (S.graph.snap) {
      S.graph.orrery.setData(S.graph.snap, null);
      S.graph.orrery.setMaxDepth(S.graph.depth);
      S.graph.orrery.setFrame(0);
    }
    S.graph.selected = S.graph.snap?.center || null;
    renderDepth();
    renderNodeCard();
  }
  renderChanges();
}

function renderDepth() {
  const seg = $("#depth-seg");
  if (!seg || !S.graph.snap) return;
  const max = Math.max(1, ...Object.keys(S.graph.snap.layout?.orbits || {}).map(Number));
  const opts = [...Array(max).keys()].map(i => i + 1);
  seg.innerHTML = opts.map(d => `<button type="button" data-d="${d}" aria-pressed="${(S.graph.depth === Infinity ? max : S.graph.depth) === d}">${d === max ? "All links" : d === 1 ? "1 link" : `${d} links`}</button>`).join("");
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    const d = Number(b.dataset.d);
    S.graph.depth = d >= max ? Infinity : d;
    S.graph.orrery.setMaxDepth(S.graph.depth);
    renderDepth();
  }));
}

function renderNodeCard() {
  const el = $("#node-card");
  const n = S.graph.snap?.nodes.find(x => x.id === S.graph.selected);
  if (!el) return;
  if (!n) { el.innerHTML = `<p class="muted">Click a metric to see its numbers and links.</p>`; return; }
  const edges = S.graph.snap.edges;
  const inputs = edges.filter(e => e.target === n.id && e.kind === "formula").map(e => e.source);
  const causes = edges.filter(e => e.target === n.id && e.kind !== "formula").map(e => e.source);
  const feeds = edges.filter(e => e.source === n.id).map(e => e.target);
  const tn = tone(n.deviation_pct, n.polarity, n.status === "anomalous");
  const line = (label, list) => list.length ? `<div><span class="faint">${label}</span> ${list.map(id => `<button class="node-ref" type="button" data-node="${esc(id)}">${esc(prettyId(id))}</button>`).join(", ")}</div>` : "";
  el.innerHTML = `<div class="node-card">
    <h3>${esc(prettyId(n.label || n.id))}</h3>
    ${n.description ? `<p class="serif muted" style="font-size:15.5px">${esc(n.description)}</p>` : ""}
    <div><span class="kpi-value num">${fmtValue(n.value, n.unit)}</span> <span class="delta num" data-tone="${tn}">${fmtPct(n.deviation_pct)}</span> <span class="faint">against ${fmtValue(n.expected, n.unit)} normally</span></div>
    ${n.definition ? `<div><span class="faint">Formula</span> ${esc(n.definition)}</div>` : ""}
    ${line("Built from", inputs)}${line("Possible causes", causes)}${line("Feeds into", feeds)}
    ${n.completeness != null && n.completeness < 1 ? `<div class="faint">${Math.round(n.completeness * 100)}% of the expected data arrived.</div>` : ""}
    ${n.added_by && n.added_by !== "seed" ? `<div class="faint">Added by the graph handler.</div>` : ""}
  </div>`;
  el.querySelectorAll(".node-ref").forEach(b => b.addEventListener("click", () => { S.graph.selected = b.dataset.node; S.graph.orrery.focusNode(b.dataset.node); renderNodeCard(); }));
}

const CHANGE_GLYPH = {
  add_node: `<svg class="change-glyph" viewBox="-10 -10 20 20"><path d="M0,-7 L7,0 L0,7 L-7,0 Z" fill="var(--brass)"/></svg>`,
  add_edge: `<svg class="change-glyph" viewBox="0 0 20 20"><path d="M2,10 L18,10" stroke="var(--brass)" stroke-width="2" stroke-dasharray="4 3"/></svg>`,
  mark_ambiguous: `<svg class="change-glyph" viewBox="0 0 20 20"><path d="M2,10 L18,10" stroke="var(--text-3)" stroke-width="2" stroke-dasharray="1.5 3.5" stroke-linecap="round"/></svg>`,
  reject_edge: `<svg class="change-glyph" viewBox="0 0 20 20"><path d="M5,5 L15,15 M15,5 L5,15" stroke="var(--text-3)" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  update_formula: `<svg class="change-glyph" viewBox="-10 -10 20 20"><circle r="6" fill="none" stroke="var(--watch)" stroke-width="1.8" stroke-dasharray="3 2.5"/></svg>`,
};

function changeTitle(c) {
  const e = c.edge, n = c.node;
  const s = e ? prettyId(e.source) : "", t = e ? prettyId(e.target) : "";
  switch (c.action) {
    case "add_node": return `Added the metric ${prettyId(n?.label || n?.id)}`;
    case "add_edge": return `Linked ${s} to ${t}`;
    case "mark_ambiguous": return `Kept a weak link from ${s} to ${t}`;
    case "reject_edge": return `Rejected a link from ${s} to ${t}`;
    case "update_formula": return `Updated the formula of ${prettyId(n?.id)}`;
    default: return c.action;
  }
}

function renderChanges() {
  const el = $("#changes");
  if (!el) return;
  const list = (S.ov.changes || []).slice().reverse();
  if (!list.length) { el.innerHTML = `<li class="muted">No changes yet. When a new metric arrives, the graph handler places it here after checking it against the data.</li>`; return; }
  el.innerHTML = list.map(c => {
    const ev = Object.entries(c.evidence || {}).slice(0, 4).map(([k, v]) => `${prettyId(k)} ${numText(v)}`).join(", ");
    return `<li class="change" data-action="${c.action}">${CHANGE_GLYPH[c.action] || ""}<div>
      <div class="change-title">${esc(changeTitle(c))}</div>
      ${c.reason ? `<div class="change-reason">${esc(c.reason)}</div>` : ""}
      <div class="change-evidence">${fmtTime(c.ts)}${ev ? `. Evidence: ${esc(ev)}` : ""}</div></div></li>`;
  }).join("");
}

// ---------- pin a metric from a graph ----------
// Clicking a metric on a graph (Investigations or Metric graph page) shows one bar in the graph's corner: pin it to
// the briefing, or restore it when it was untracked. Releases, segments and data-quality nodes get no bar.
function enablePinning() {
  const isMetric = n => n && ["kpi", "metric"].includes(n.kind) && /^[a-z][a-z0-9_]*$/.test(n.id);
  const bar = (panel, html) => {
    let el = panel.querySelector(".pin-bar");
    if (!html) return el?.remove();
    if (!el) { el = document.createElement("div"); el.className = "pin-bar"; panel.appendChild(el); }
    el.innerHTML = html;
    return el;
  };
  const show = (panel, id) => {
    const node = [S.graph.snap, S.inv.snap].flatMap(s => s?.nodes || []).find(n => n.id === id);
    if (!S.ov?.choices || !isMetric(node)) return bar(panel, "");
    const name = esc(cap(node.label || metricName(id)));
    if ((S.ov.tick?.kpis || []).some(k => k.name === id)) return bar(panel, `<span><b>${name}</b> is on the briefing</span>`);
    const action = S.ov.choices.untracked.includes(id) ? "restore" : "pin";
    const el = bar(panel, `<span><b>${name}</b></span><button class="btn btn-primary" type="button">${action === "pin" ? "Pin to the briefing" : "Restore to the briefing"}</button>`);
    el.querySelector("button").addEventListener("click", async e => {
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = action === "pin" ? "Pinning…" : "Restoring…";
      try {
        const r = await api.choose(action, id);
        bar(panel, `<span><b>${name}</b> ${r.message && r.message !== "Saved" ? esc(lc1(r.message)) : "is on the briefing"}</span><a href="#/briefing">Open the briefing</a>`);
        refresh();
      } catch (err) {
        bar(panel, `<span>Could not pin ${name}: ${esc(err.message)}</span>`);
      }
    });
  };
  const pick = e => {
    const panel = e.target.closest?.(".orrery-panel");
    if (!panel || e.target.closest(".pin-bar")) return;
    const g = e.target.closest("g.n[data-id]");
    if (g) show(panel, g.dataset.id); else if (e.type === "click" && e.target.closest("svg")) bar(panel, "");
  };
  document.addEventListener("click", pick);
  document.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") pick(e); });
}

// ---------- start ----------
const r = parseHash();
S.view = r.view; S.param = r.param;
renderView(true);
enablePinning();
refresh();
setInterval(refresh, 3000);
