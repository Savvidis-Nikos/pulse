// Pulse web UI: briefing, investigations, metric graph. Reads the Pulse state directory through the server.
import { api } from "./api.js";
import { esc, fmtValue, fmtPct, fmtTime, fmtClock, tone, prettyId, numText, sevRank, SEVERITY, CONFIDENCE, MODE, ACTION } from "./format.js";
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
  con: { data: null, timer: null, key: "", error: "", scenario: "payment_failure" },
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
    <div class="status-item hide-narrow"><b class="num">${t ? `Check ${t.tick}` : "—"}</b><span>Monitoring loop</span></div>
    <div class="status-item"><b class="mode" data-mode="${mode}"><i class="mode-dot"></i>${esc(MODE[mode] || mode)}</b><span>Agents</span></div>
    <div class="status-item hide-narrow"><b class="num">$${spent.toFixed(2)} of $${cap.toFixed(0)}</b><span class="spend-bar" aria-hidden="true"><i style="width:${Math.min(100, spent / cap * 100).toFixed(1)}%"></i></span></div>
    ${S.error ? `<span class="faint">Connection lost, retrying</span>` : ""}${theme}`;
  bindTheme();
}
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
  [RegExp(`^interpreter on ${INC} \\(([^,]+), [\\d.]+s, \\w+\\): "(.*)" - (\\w+), path .*$`), (id, r, h, c) => `The investigator agent finished: ${plain(h)} (${(CONFIDENCE[c] || c).toLowerCase()}).`],
  [/^brief by orchestrator agent \([^)]*\) because: (.*)$/, w => `The orchestrator agent wrote a new briefing: ${briefReasons(w)}.`],
  [/^brief asked to re-investigate next tick: (.*)$/, l => `Will re-check next hour: ${l.split(", ").map(segText).join(", ")}.`],
  [/^orchestrator agent failed /, () => "The orchestrator agent could not run, so a standard briefing was written."],
  [RegExp(`^interpreter (?:unavailable|failed).*?${INC}`), id => `The investigator agent could not run on ${issueText(id)}. It will try again.`],
  [/^Graph Handler on inbox\/(\S+): (.*)$/, (f, t) => {
    const added = [...t.matchAll(/added (\S+) = /g)].map(m => plain(m[1])), causes = +(/(\d+) cause edges/.exec(t)?.[1] || 0);
    return added.length ? `The graph agent added ${added.join(", ")} to the metric graph from ${f}${causes ? `, with ${causes} cause link${causes === 1 ? "" : "s"}` : ""}.`
      : `The graph agent read ${f} but added no new metric.`;
  }],
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
const COUNT_WORD = { alert: n => n === 1 ? "needs attention" : "need attention", data_issue: () => "with incomplete data", watch: () => "being watched", ok: () => "normal" };

async function refreshData() {
  if (S.refreshing) return;
  S.refreshing = true; S.refreshMsg = "";
  renderBriefing();
  try {
    const r = await api.refresh();
    if (String(r.message).startsWith("no newer data")) S.refreshMsg = cap(r.message.replace("no newer data: ", "No newer data. ")) + ".";
  } catch (e) {
    S.refreshMsg = `Refresh failed: ${e.message}`;
  }
  S.refreshing = false;
  await refresh();
  if (S.view === "briefing") renderBriefing();
}

// The latest issue on a metric (the open one if there is one) and what the interpreter concluded.
function kpiConclusion(k) {
  const inc = incidents().find(i => i.id === k.incident_id) || incidents().find(i => i.kpi === k.name);
  if (!inc) return "";
  const it = interpIndex(inc.id), done = inc.state === "resolved";
  const label = done ? `Last issue, resolved ${fmtTime(inc.resolved_at || inc.last_seen)}` :
    it?.partial ? "Being investigated now" : it ? `Conclusion${it.confidence ? `, ${CONFIDENCE[it.confidence].toLowerCase()}` : ""}` : "Not investigated yet";
  const head = it?.headline || inc.headline.split(" - ").pop();
  return `<div class="kpi-conclusion">
    <div class="concl-label">${esc(label)}</div>
    <p class="concl-head">${esc(plain(head))}</p>
    ${it?.summary && !done ? `<p class="concl-text">${esc(plain(it.summary))}</p>` : ""}
    <a class="concl-link" href="#/investigation/${encodeURIComponent(inc.id)}">Open the investigation ${icon("arrow-right")}</a>
  </div>`;
}

// "expected: FR Black Week teaser campaign (sessions up 19% while orders flat, so ...)" ->
// "Sessions up 19% while orders flat, so .... Expected during the France Black Week teaser campaign."
const noteText = note => {
  const m = /^expected: (.+?)(?: \((.+)\))?$/.exec(String(note || "").trim());
  if (!m) return note ? cap(note.trim()) + "." : "";
  return `${m[2] ? cap(m[2]) + ". " : ""}Expected during the ${m[1]}.`;
};

function kpiDetail(k, item) {
  const text = plain(item?.detail || noteText(k.note));
  const last = (k.series || []).at(-1), facts = [];
  if (last?.lo != null && last?.hi != null) {
    const hi = k.unit === "ratio" ? Math.min(last.hi, 1) : last.hi;
    facts.push(`Normal range at ${fmtClock(last.ts)}: ${fmtValue(Math.max(last.lo, 0), k.unit)} to ${fmtValue(hi, k.unit)}`);
  }
  if (k.onset_ts && k.severity !== "ok") facts.push(`Outside its normal range since ${fmtTime(k.onset_ts)}`);
  if (k.completeness != null) facts.push(k.completeness >= 0.995 ? "All data for the hour arrived" : `${Math.round(k.completeness * 100)}% of the hour's data arrived`);
  return `<div class="kpi-more">
    <div>${text ? `<p class="kpi-why">${esc(text)}</p>` : ""}<p class="kpi-facts">${facts.map(esc).join(`<span aria-hidden="true"> · </span>`)}</p></div>
    ${kpiConclusion(k)}
  </div>`;
}

function renderBriefing() {
  const root = $("#view-briefing");
  const t = S.ov.tick, b = S.ov.brief;
  if (!S.ov.found || !t) {
    root.innerHTML = `<div class="empty">No monitoring data yet. Start the loop with <code>python -m pulse replay</code> or <code>python -m pulse watch</code>, or open the demo with <code>python -m pulse.web --demo</code>.</div>`;
    return;
  }
  const items = new Map((b?.items || []).map(i => [i.kpi, i]));
  // Under the headline: the first plain paragraph of the brief only. Per-metric lines live in the table.
  const strip = s => s.trim().replace(/[.\s]+$/, "");
  const intro = (b?.text || "").split("\n").map(l => l.trim())
    .find(l => l && !/^[-•*]\s+/.test(l) && !/^On track:/.test(l) && !(b?.headline && strip(l) === strip(b.headline)));
  const counts = ["alert", "data_issue", "watch", "ok"].map(s => [s, t.kpis.filter(k => k.severity === s).length]).filter(([, n]) => n);
  const incs = incidents();
  // a bare clock time only for today's events; older ones carry their date
  const at = ts => String(ts || "").slice(0, 10) === String(t.ts).slice(0, 10) ? fmtClock(ts) : fmtTime(ts);
  const issueRows = incs.map(i => {
    const when = i.state === "resolved" ? `Started ${at(i.onset_ts || i.opened_at)}, resolved ${at(i.resolved_at || i.last_seen)}` :
      `Open since ${at(i.onset_ts || i.opened_at)}`;
    const live = interpIndex(i.id)?.partial;
    // "monitor headline - interpreter conclusion": lead with the conclusion
    const [what, ...why] = (i.headline || `${kpiLabel(i.kpi)} moved the wrong way`).split(" - ");
    return `<li><a class="issue" href="#/investigation/${encodeURIComponent(i.id)}" data-sev="${i.severity}" data-state="${i.state}">
      <span class="issue-bar"></span><span>
        <span class="issue-headline">${esc(plain(why.length ? why.join(" - ") : what))}</span>
        <span class="issue-meta"><span>${esc(segText(i.kpi))}</span><span>${when}</span>${live ? `<span>Being investigated now</span>` : hasInvestigation(i) ? "" : `<span>Not investigated yet</span>`}</span>
      </span></a></li>`;
  }).join("");
  const kpis = t.kpis.slice().sort((a, b2) => sevRank(a.severity) - sevRank(b2.severity));
  const rows = kpis.map(k => {
    // colour only what the monitor flagged: an in-band wobble stays neutral even if it points down
    const tn = k.severity === "alert" || k.severity === "watch" ? tone(k.deviation_pct, k.polarity, true) : "flat";
    const sentence = plain(items.get(k.name)?.sentence || "");
    const open = S.open.has(k.name);
    const comp = k.completeness != null && k.completeness < 0.95 ? ` <span class="faint">(${Math.round(k.completeness * 100)}% of data)</span>` : "";
    return `<tr class="kpi-row${open ? " open" : ""}" data-kpi="${esc(k.name)}">
      <td><div class="kpi-label">${esc(k.label)}</div>${sentence ? `<div class="kpi-sentence">${esc(sentence)}</div>` : ""}</td>
      <td><span class="kpi-value num">${fmtValue(k.value, k.unit)}</span></td>
      <td class="col-expected"><span class="kpi-expected num">${fmtValue(k.expected, k.unit)}</span></td>
      <td><span class="delta num" data-tone="${tn}">${fmtPct(k.deviation_pct)}${k.onset_ts && k.severity !== "ok" ? `<span class="delta-since">since ${fmtClock(k.onset_ts)}</span>` : ""}</span></td>
      <td class="col-spark">${sparkline(k.series, { tone: tn, unit: k.unit, label: `${k.label}, last 48 hours against its normal range` })}</td>
      <td><span class="sev" data-sev="${k.severity}"><i></i>${SEVERITY[k.severity] || k.severity}</span>${comp}</td>
      <td class="col-toggle"><button class="expand" type="button" aria-expanded="${open}" aria-label="More about ${esc(k.label)}">${icon("chevron-down")}</button></td>
    </tr>
    <tr class="kpi-detail" ${open ? "" : "hidden"}><td colspan="7">${kpiDetail(k, items.get(k.name))}</td></tr>`;
  }).join("");
  const followUp = (b?.follow_up || []).map(x => esc(plain(kpiLabel(x))));
  root.innerHTML = `
    <div class="brief">
      <div class="brief-main">
        <div class="brief-meta">
          <span>${longTime(b?.ts || t.ts)}</span>
          <button class="refresh-btn" type="button" id="refresh-btn" ${S.refreshing ? `disabled aria-busy="true"` : ""} title="Load the next hour of simulated marketplace data and run the check on it">${icon("refresh-cw")}<span>${S.refreshing ? "Loading new data…" : "Refresh data"}</span></button>
          ${S.refreshMsg ? `<span class="refresh-msg">${esc(S.refreshMsg)}</span>` : ""}
        </div>
        <h1 class="brief-headline">${esc(plain(b?.headline || "All headline metrics are within their normal range"))}</h1>
        ${intro ? `<p class="brief-text">${esc(plain(intro))}</p>` : ""}
        <p class="brief-counts">${counts.map(([s, n]) => `<span class="sev" data-sev="${s}"><i></i>${n} ${COUNT_WORD[s](n)}</span>`).join("")}</p>
        ${followUp.length ? `<p class="follow-up"><b>Will re-check next hour:</b> ${followUp.join(", ")}</p>` : ""}
      </div>
      <aside class="brief-side">
        <div>
          <h2 class="section-title">Issues</h2>
          ${incs.length ? `<ul class="issues">${issueRows}</ul>` : `<p class="muted serif" style="font-size:16px">Nothing is moving the wrong way.</p>`}
        </div>
        ${(t.log || []).length ? `<div>
          <h2 class="section-title">What the agents did in check ${t.tick}</h2>
          <ol class="ticklog">${t.log.map(l => `<li>${esc(activity(l))}</li>`).join("")}</ol>
        </div>` : ""}
      </aside>
    </div>
    <section class="ledger" aria-label="Headline metrics">
      <h2 class="section-title">Headline metrics</h2>
      <table>
        <thead><tr><th>Metric</th><th>Now</th><th class="col-expected">Normal</th><th>Change</th><th class="col-spark">Last 48 hours</th><th>Status</th><th class="col-toggle"></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
  $("#refresh-btn", root).addEventListener("click", refreshData);
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
function stopPlay() {
  S.inv.playing = false;
  clearInterval(S.inv.timer);
  S.inv.timer = null;
  const b = $("#play-btn");
  if (b) { b.innerHTML = icon("play"); b.setAttribute("aria-label", "Play the investigation"); }
}

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
    stopPlay();
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
          <div class="scrubber" id="scrubber"></div>
          <div id="legend-slot"></div>
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
    $("#scrubber").innerHTML = "";
    $("#legend-slot").innerHTML = "";
    reading.innerHTML = `<div class="empty">${esc(tidy(inc.headline || kpiLabel(inc.kpi)))}<br>Not investigated yet.</div>`;
    S.inv.orrery = createOrrery($("#orrery-host"), { onSelect: jumpToNode });
    return;
  }
  const snapId = interp.snapshot_id || inc.snapshot_id;
  let snap = S.inv.snap && S.inv.snap.snapshot_id === snapId ? S.inv.snap : null;
  if (!snap && snapId) { try { snap = plainLabels(await api.snapshot(snapId)); } catch (e) { snap = null; } }
  const key = `${interp.interpretation_id}|${(interp.trace || []).length}|${interp.partial}`;
  if (key === S.inv.key) return;   // nothing new
  const wasAtEnd = !S.inv.interp || S.inv.frame >= (S.inv.orrery?.frames?.length || 1) - 1;
  const firstLoad = !S.inv.interp || S.inv.interp.interpretation_id !== interp.interpretation_id;
  S.inv.key = key; S.inv.interp = interp; S.inv.snap = snap;

  if (snap) {
    const frames = S.inv.orrery.setData(snap, interp, { partial: interp.partial });
    if (firstLoad || (interp.partial && wasAtEnd) || (!interp.partial && wasAtEnd)) S.inv.frame = frames.length - 1;
    S.inv.frame = Math.min(S.inv.frame, frames.length - 1);
    mountLegend($("#legend-slot"), legendTokens(snap, interp));
  } else {
    $("#orrery-host").innerHTML = `<div class="empty" style="margin:24px">Graph not available.</div>`;
    $("#legend-slot").innerHTML = "";
  }
  renderScrubber();
  renderReading();
  setFrame(S.inv.frame);

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

function stepLabel(step) {
  if (!step) return "";
  const verb = ACTION[step.action] || step.action;
  const target = step.action === "breakdown" && step.args?.dim ? `${labelOf(step.node)} by ${step.args.dim}` : (step.node ? labelOf(step.node) : "");
  return `${verb} ${target}`.trim();
}

function renderScrubber() {
  const el = $("#scrubber");
  const frames = S.inv.orrery?.frames || [];
  const steps = S.inv.interp?.trace || [];
  if (!steps.length) { el.innerHTML = `<div class="step-caption muted">No steps recorded yet.</div>`; return; }
  const dots = steps.map((s, i) => `${i ? `<span class="step-link" data-i="${i + 1}"></span>` : ""}<button class="step-dot" type="button" data-i="${i + 1}" aria-label="Step ${i + 1}: ${esc(stepLabel(s))}">${i + 1}</button>`).join("");
  el.innerHTML = `
    <div class="scrubber-buttons">
      <button class="icon-btn" type="button" id="prev-btn" aria-label="Previous step">${icon("skip-back")}</button>
      <button class="icon-btn" type="button" id="play-btn" aria-label="Play the investigation">${icon("play")}</button>
      <button class="icon-btn" type="button" id="next-btn" aria-label="Next step">${icon("skip-forward")}</button>
    </div>
    <div class="steps" role="group" aria-label="Investigation steps">${dots}</div>
    <div class="step-caption" id="step-caption"></div>`;
  el.querySelectorAll(".step-dot").forEach(b => b.addEventListener("click", () => { stopPlay(); setFrame(Number(b.dataset.i)); }));
  $("#prev-btn").addEventListener("click", () => { stopPlay(); setFrame(S.inv.frame - 1); });
  $("#next-btn").addEventListener("click", () => { stopPlay(); setFrame(S.inv.frame + 1); });
  $("#play-btn").addEventListener("click", () => {
    if (S.inv.playing) return stopPlay();
    S.inv.playing = true;
    $("#play-btn").innerHTML = icon("pause");
    $("#play-btn").setAttribute("aria-label", "Pause");
    if (S.inv.frame >= frames.length - 1) setFrame(0);
    S.inv.timer = setInterval(() => {
      if (S.inv.frame >= (S.inv.orrery?.frames?.length || 1) - 1) return stopPlay();
      setFrame(S.inv.frame + 1);
    }, 1700);
  });
}

function setFrame(i) {
  const frames = S.inv.orrery?.frames || [];
  if (!frames.length) return;
  i = Math.max(0, Math.min(i, frames.length - 1));
  S.inv.frame = i;
  S.inv.orrery.setFrame(i);
  const steps = S.inv.interp?.trace || [];
  document.querySelectorAll("#scrubber .step-dot").forEach(b => {
    const n = Number(b.dataset.i);
    b.dataset.done = String(n <= i);
    if (n === i) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
  });
  document.querySelectorAll("#scrubber .step-link").forEach(l => { l.dataset.done = String(Number(l.dataset.i) <= i); });
  document.querySelectorAll(".trace-item").forEach(b => {
    const n = Number(b.dataset.i);
    b.dataset.done = String(n <= i);
    if (n === i) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
  });
  const cap = $("#step-caption");
  if (cap) {
    const s = steps[i - 1];
    cap.textContent = i === 0 ? `Start: ${labelOf(S.inv.interp?.kpi)}` : tidy(s?.finding || stepLabel(s));
  }
}

function jumpToNode(nodeId) {
  const steps = S.inv.interp?.trace || [];
  const idx = steps.findIndex(s => s.node === nodeId || (s.highlight_nodes || []).includes(nodeId));
  if (idx >= 0) { stopPlay(); setFrame(idx + 1); }
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
  const stepList = steps.length ? `<div><h3 class="section-title">Steps</h3>
    <ol class="trace-list">${steps.map((s, i) => `<li><button class="trace-item" type="button" data-i="${i + 1}" aria-label="Step ${i + 1}: ${esc(stepLabel(s))}">
      <span class="trace-n">${i + 1}</span><span class="trace-finding">${esc(tidy(s.finding || stepLabel(s)))}</span></button></li>`).join("")}</ol></div>` : "";
  if (it.partial) {   // no conclusions until the interpreter has finished: only what it has found so far
    el.innerHTML = `<div class="live-banner"><i class="live-dot"></i>Investigating now, ${steps.length} ${steps.length === 1 ? "step" : "steps"} so far</div>
      <h2 class="conclusion-headline">${esc(labelOf(it.kpi))}</h2>${stepList}`;
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
      <h2 class="conclusion-headline">${esc(tidy(it.headline || ""))}</h2>
      ${it.summary ? `<p class="conclusion-summary">${esc(tidy(it.summary))}</p>` : ""}
      ${facts.length ? `<dl class="facts">${facts.map(([dt, dd]) => `<div class="fact"><dt>${dt}</dt><dd>${dd}</dd></div>`).join("")}</dl>` : ""}
      ${stepList}`;
  }
  el.querySelectorAll(".trace-item").forEach(b => b.addEventListener("click", () => { stopPlay(); setFrame(Number(b.dataset.i)); }));
  el.querySelectorAll(".node-ref").forEach(b => b.addEventListener("click", () => { S.inv.orrery?.focusNode(b.dataset.node); jumpToNode(b.dataset.node); }));
}

// ---------- event console (top of the investigations page) ----------
// A live simulated market (server: pulse/web/console.py): inject a scenario, run hours, watch Pulse react.
const CON_GROUP = { fault: "Problems", innocent: "Planned changes", data: "Data problems" };
const CON_TAG = { market: "Market", inject: "Injected", check: "Pulse", step: "Pulse", error: "Error" };
const CON_QUIET = /^(nothing material changed|all \d+ KPIs ok) /;
const clockText = ts => {
  const d = new Date(String(ts).replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};

function consoleRow(e) {
  if (e.kind === "step" && CON_QUIET.test(e.text)) return "";
  let tag = CON_TAG[e.kind] || "", text = e.text;
  if (e.kind === "check") {
    const m = /^\[[^\]]+\] tick\s+\d+ \| (.*?) \| (.*)$/.exec(e.text);
    text = !m ? plain(e.text) : m[1] === "all ok" ? "All headline metrics normal" : tidy(m[2] || m[1]);
  } else if (e.kind === "step") {
    if (/^(interpreter|brief by orchestrator|orchestrator agent|Graph Handler)/.test(e.text)) tag = "Agent";
    text = activity(e.text);
  }
  const inc = /^interpreter on (INC-[^\s:]+)/.exec(e.text)?.[1];
  const open = inc ? ` <a class="con-open" href="#/investigation/${encodeURIComponent(inc)}">Open</a>` : "";
  return `<li class="con-row" data-tag="${tag}"><time>${e.t ? esc(e.t.slice(11, 16)) : ""}</time><span class="con-tag">${tag}</span><span>${esc(text)}${open}</span></li>`;
}

function renderConsole() {
  const slot = $("#console-slot"), c = S.con.data;
  if (!slot) return;
  if (!c?.enabled) { slot.innerHTML = ""; S.con.key = ""; return; }
  const live = c.source === "live", sess = live ? c.session : null, busy = c.busy;
  const key = JSON.stringify([c.source, busy, c.job, sess?.clock, c.log.at(-1)?.n, S.con.error, (S.ov?.incidents || []).length]);
  if (key === S.con.key && $("#console")) return;
  S.con.key = key;
  const dis = busy ? "disabled" : "";
  const options = ["fault", "innocent", "data"].map(k => `<optgroup label="${CON_GROUP[k]}">${c.catalog.filter(x => x.kind === k)
    .map(x => `<option value="${x.id}" ${x.id === S.con.scenario ? "selected" : ""}>${esc(x.title)}</option>`).join("")}</optgroup>`).join("");
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
  const rows = sess ? c.log.map(consoleRow).join("") : "";
  slot.innerHTML = `<section class="console" id="console" aria-label="Event console">
    <div class="con-head"><h2 class="section-title">Event console</h2>
      <div class="segmented" role="group" aria-label="Data shown">
        <button type="button" data-source="recorded" aria-pressed="${!live}">Recorded run</button>
        <button type="button" data-source="live" aria-pressed="${live}">Live market</button></div>
      ${status}</div>
    ${controls}
    ${S.con.error ? `<p class="con-error">${esc(S.con.error)}</p>` : ""}
    ${rows ? `<ol class="con-log" id="con-log">${rows}</ol>` : ""}
  </section>`;
  slot.querySelectorAll("[data-source]").forEach(b => b.addEventListener("click", () => {
    if (b.getAttribute("aria-pressed") !== "true") consoleAct({ action: "source", source: b.dataset.source });
  }));
  slot.querySelectorAll("[data-con]").forEach(b => b.addEventListener("click", () => {
    const a = b.dataset.con;
    if (a === "inject") consoleAct({ action: "inject", id: $("#con-scenario").value });
    else if (a === "run") consoleAct({ action: "run", hours: Number(b.dataset.hours) });
    else if (a === "start" || confirm("Restart the live market from a quiet history?")) consoleAct({ action: "start" });
  }));
  $("#con-scenario")?.addEventListener("change", e => { S.con.scenario = e.target.value; });
  const log = $("#con-log");
  if (log) log.scrollTop = log.scrollHeight;
}

async function loadConsole() {
  try { S.con.data = await api.console(); } catch (e) { return; }
  renderConsole();
  clearTimeout(S.con.timer);
  if (S.con.data.busy) S.con.timer = setTimeout(refresh, 1200);   // follow a running job closely
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

document.addEventListener("keydown", e => {
  if (S.view !== "investigation" || e.target.closest("input, select, textarea")) return;
  if (e.key === "ArrowRight") { stopPlay(); setFrame(S.inv.frame + 1); }
  if (e.key === "ArrowLeft") { stopPlay(); setFrame(S.inv.frame - 1); }
});

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

// ---------- start ----------
const r = parseHash();
S.view = r.view; S.param = r.param;
renderView(true);
refresh();
setInterval(refresh, 3000);
