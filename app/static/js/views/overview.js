import { api, del, get, put } from "../api.js";
import { isStale, refreshAlerts } from "../alerts.js";
import { haptic } from "../app.js";
import { barChart, responsive, sparkline } from "../charts.js";
import { openRecord } from "./record-detail.js";
import { moodLabel } from "./journal.js";
import { duration, openWorkout } from "./workouts.js";
import {
  pageScroller, $, $$, EASE_OUT, ageText, capitalize, confirmDialog, easeHeight, emptyState, flagBadge, fmtAgo, fmtDate, fmtDateTime, fmtNum, html, icon, loading, modal, mount, onAction,
  plural, prefersStill, raw, recordDate, resultStatus, toast,
} from "../ui.js";
import { displayUnit, fmtMeasure, recordValue, unitSystem } from "../units.js";

function greeting() {
  const h = new Date().getHours();
  return h >= 5 && h < 12 ? "Good morning" : h >= 12 && h < 18 ? "Good afternoon" : "Good evening";   // up late: still evening
}

/** Hours as "7h 5m" (rounded to the minute first, so 7.999 h is "8h 0m", not "7h 60m"). */
export function hoursText(value) {
  const total = Math.round(value * 60);
  return `${Math.floor(total / 60)}h ${total % 60}m`;
}

/** A wearable metric's value as text, in the chosen units (no unit symbol: see metricUnit). */
export function metricValue(m, value) {
  if (value === null || value === undefined) return "—";
  if (m.unit === "h") return hoursText(value);
  if (m.metric === "step_count" || m.unit === "kcal" || m.unit === "score") return fmtNum(value, 0);
  return fmtMeasure(value, m.unit, m.metric, { withUnit: false });
}

/** The unit symbol shown after a metric's value ("" for durations, scores and feet-and-inches heights). */
export function metricUnit(m) {
  if (m.unit === "h" || m.unit === "score") return "";
  if (m.metric === "height" && unitSystem() === "us") return "";
  return displayUnit(m.unit, m.metric);
}

export function tile(m, extra = "", goal = null) {
  // The date is only worth the space when the latest value isn't from today.
  const today = new Date().toLocaleDateString("en-CA");
  const goalLine = goal ? html`<div class="s tile-goal">${icon("target")} ${goal.met} of ${goal.days} days at goal${goal.streak >= 3 ? ` · ${goal.streak} in a row` : ""}</div>` : "";
  return html`<div class="card tile" ${raw(extra)}>
    <div class="k"><span class="truncate">${m.label}</span>${m.latest.day !== today ? html`<span class="faint">${fmtDate(m.latest.day).replace(/,? \d{4}$/, "")}</span>`
      : m.latest.partial ? html`<span class="faint">So far</span>` : ""}</div>
    <div class="v">${metricValue(m, m.latest.value)}<small>${metricUnit(m)}</small></div>
    <div class="s">7-day avg ${metricValue(m, m.avg_7d)}</div>
    ${goalLine}
    ${raw(sparkline(m.spark))}
  </div>`;
}

// ------------------------------------------------------------------ insights
const TONE_ICON = { attention: "alert", good: "check", info: "lightbulb" };
// Earlier versions kept what was hidden in each browser. The server keeps it now, so hiding something here hides it on
// the phone too. Anything still in this browser is handed over once.
const HIDDEN_KEY = "syntropy-insights-hidden";
function takeOldHidden() {
  try {
    const ids = JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]");
    localStorage.removeItem(HIDDEN_KEY);
    return Array.isArray(ids) ? ids.filter((i) => typeof i === "string").slice(-200) : [];
  } catch { return []; }
}
function hideOnServer(profileId, ids) {
  return api("/api/insights/hidden", { method: "POST", query: { profile: profileId }, body: { ids } });
}
async function loadInsights(profileId) {
  const old = takeOldHidden();
  if (old.length) await hideOnServer(profileId, old).catch(() => {});
  return get("/api/insights", { profile: profileId }, old.length ? { fresh: true } : {});
}
export function hideInsight(profileId, id) { return hideOnServer(profileId, [id]).catch(() => {}); }

/** An insight's sentence. Ones about a measurement are written here, so the numbers are in the person's units. */
export function insightText(i) {
  const d = i.data || {};
  const m = { metric: d.metric, unit: d.unit };
  const v = (x) => `${metricValue(m, x)}${metricUnit(m) ? ` ${metricUnit(m)}` : ""}`;
  if (i.kind === "change") return `${i.title.split(" is ")[0]} was typically ${v(d.recent)} over the last ${d.days} days, ${d.recent > d.usual ? "up" : "down"} from your usual ${v(d.usual)}.`;
  if (i.kind === "sleep" && d.bedtime && d.spread_min != null) {
    // The usual bedtime in the reader's clock (8:57 PM, or 20:57), not the server's.
    const [h, min] = String(d.bedtime).split(":").map(Number);
    const at = Number.isFinite(h) && Number.isFinite(min)
      ? new Date(2000, 0, 1, h, min).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : d.bedtime;
    return `Your bedtime varied by about ±${d.spread_min} minutes over the last two weeks (usually around ${at}).`;
  }
  if (i.kind === "goal" && d.unit) return `You met this goal (${d.direction === "min" ? "at least" : "at most"} ${v(d.target)}) on ${d.met} of the last ${d.days} days${d.streak >= 3 ? `, ${d.streak} in a row` : ""}.`;
  return i.text;
}

export function insightItem(i, { hide = true } = {}) {
  return html`<div class="insight ${i.tone}">
    <span class="insight-icon">${icon(TONE_ICON[i.tone] || "lightbulb")}</span>
    <div class="grow" style="min-width:0"><div class="insight-title">${i.title}</div>
      <div class="small muted">${insightText(i)}</div>
      ${i.link ? html`<a class="small" href="${i.link}">See the chart ${icon("arrowRight")}</a>` : ""}</div>
    ${hide ? html`<button class="btn btn-ghost btn-icon btn-sm insight-hide" data-action="hide-insight" data-id="${i.id}" aria-label="Hide this" title="Hide">${icon("x")}</button>` : ""}
  </div>`;
}

/** What the latest lab results come to. "All" only when every one of them was checked against a range. */
export function labStatusText(s) {
  if (!s?.total) return "No lab results on record.";
  if (s.in_range === s.total) return `All ${s.total} latest results were in range.`;
  const unchecked = s.not_assessed ? ` ${s.not_assessed} had no range to compare with.` : "";
  return `${s.in_range} of ${s.total} latest results were in range${s.flagged ? `, ${s.flagged} out of range` : ""}.${unchecked}`;
}

/** How long ago, in the words of someone reading a result: "about 3 years ago". */
function ageWords(when) {
  const years = (Date.now() - new Date(when).getTime()) / (365.25 * 86400000);
  return years >= 2 ? `about ${Math.round(years)} years ago` : `about ${Math.round(years * 12)} months ago`;
}

const LIST_MAX = 5;
const DEFAULT_TILES = 6;   // numbers shown until the person arranges their own

/** A summary card. Long lists show their first few items and link to the full list. */
function listCard(title, iconName, items, renderItem, emptyText, { href, more = "View all" } = {}) {
  return html`<div class="card widget-card">
    <div class="card-head"><h3>${icon(iconName)} ${title}</h3>${items.length ? html`<span class="badge">${items.length}</span>` : ""}</div>
    <div class="list">${items.length ? items.slice(0, LIST_MAX).map(renderItem) : html`<div class="list-item muted small"><span>${emptyText}</span></div>`}</div>
    ${href && items.length ? html`<a class="card-foot" href="${href}">${items.length > LIST_MAX && more === "View all" ? `View all ${items.length}` : more} ${icon("arrowRight")}</a>` : ""}
  </div>`;
}

function sourcesLabel(r) {
  const n = (r.sources || []).length;
  return n > 1 ? `${n} sources` : (r.source_name || "");
}

// ------------------------------------------------------------------ cards
// Each card: what the Add dialog calls it, its size until the person changes it, whether it's shown until the person
// changes that, what data it needs, and how it draws. New cards appear (per their default) for everyone. Cards marked
// records draw from medical records, and stay out of the way until there are some. Single numbers (steps, sleep, HRV…)
// are items of their own, "m:<metric>".
export const WIDGETS = [
  { id: "insights", title: "What's changed", desc: "What's changed from your normal across sleep, heart, training and labs", size: "xl", on: true,
    needs: ["insights"], first: true,
    render: (d) => {
      const items = d.insights?.insights || [];
      // What the lab marked critical is never folded away.
      const shown = Math.max(6, items.filter((i) => i.kind === "critical").length);
      return html`<div class="card widget-card">
        <div class="card-head"><h3>${icon("lightbulb")} What's changed</h3><span class="small muted">across all your sources</span></div>
        ${items.length ? html`<div class="insights">${items.slice(0, shown).map((i) => insightItem(i))}</div>`
          : html`<div class="list-item muted small">No changes to show right now. Insights appear as your devices, check-ins and records build up a picture of your normal.</div>`}
        ${items.length > shown ? html`<div class="card-foot muted small">${items.length - shown} more: ask about them in <a href="#/ask">Ask</a></div>` : ""}
      </div>`;
    } },
  { id: "start", title: "Getting started", desc: "What you can add, until two sources are connected", size: "l", on: true, needs: [],
    render: (d) => {
      if ((d.connections || []).length >= 2) return "";
      const kinds = new Set((d.connections || []).map((c) => c.kind));
      const steps = [["ehr", "building", "Connect a health system", "#/sources?add=ehr"], ["device", "phone", "Pair your phone", "#/sources?add=iphone"],
        ["wearable", "watch", "Link a wearable", "#/sources?add=wearable"], ["import", "upload", "Import a file", "#/sources?add=import"]].filter(([k]) => !kinds.has(k));
      return html`<div class="card widget-card"><div class="card-head"><h3>${icon("plus")} Add another source</h3>
          <button class="btn btn-ghost btn-icon btn-sm" data-action="remove" data-id="start" aria-label="Hide this" title="Hide">${icon("x")}</button></div>
        <div class="list">${steps.map(([, ic, label, href]) => html`<a class="list-item" href="${href}"><span class="ev-icon">${icon(ic)}</span><span class="grow">${label}</span>${icon("arrowRight")}</a>`)}</div></div>`;
    } },
  { id: "new", title: "New since you last looked", desc: "Visits, diagnoses, medications, notes and results that have arrived", size: "xl", on: true, needs: ["newRecords"], records: true, first: true,
    render: (d) => {
      const n = d.newRecords;
      if (!n || (!n.groups.length && !n.imported.length && !n.older)) return "";
      return html`<div class="card widget-card">
        <div class="card-head"><h3>${icon("bell")} New since you last looked</h3><button class="btn btn-sm btn-ghost" data-action="seen-new">Mark as seen</button></div>
        <div class="list">
          ${n.imported.map((i) => html`<div class="list-item"><span class="grow">Imported ${plural(i.count, "record")} from ${String(i.source).replace(/^Imported · /, "")}</span></div>`)}
          ${n.groups.map((g) => html`<div class="list-item" style="display:block"><div class="title">${g.label} <span class="badge">${g.count}</span></div>
            ${g.items.map((r) => html`<div class="meta clickable" data-action="record" data-id="${r.id}" role="link" tabindex="0">${r.title}${r.value_text ? ` ${r.value_text}` : ""} · ${fmtDate(r.effective_at)} · ${r.source_name || ""}</div>`)}
            ${g.count > g.items.length ? html`<div class="meta">and ${g.count - g.items.length} more</div>` : ""}</div>`)}
          ${n.older ? html`<div class="list-item small muted"><span>${plural(n.older, "older record")} also arrived.</span></div>` : ""}
        </div></div>`;
    } },
  { id: "goals", title: "Goals", desc: "Your daily goals and how many of the last 7 days met them", size: "m", on: true, needs: ["goals"],
    render: (d) => {
      const list = d.goals?.progress || [];
      return html`<div class="card widget-card">
        <div class="card-head"><h3>${icon("target")} Goals</h3><span class="small muted">last 7 days</span></div>
        ${list.length ? html`<div class="list">${list.map((g) => html`<a class="list-item clickable goal-row" href="#/trends?metric=${g.metric}" style="color:inherit;text-decoration:none">
          <div class="grow" style="min-width:0"><div class="title truncate">${g.label}</div>
            <div class="meta">${g.direction === "min" ? "At least" : "At most"} ${metricValue({ metric: g.metric, unit: g.unit }, g.target)} ${metricUnit({ metric: g.metric, unit: g.unit })}${g.streak >= 3 ? ` · ${g.streak} days in a row` : ""}</div></div>
          <span class="goal-dots" aria-label="${g.met} of ${g.days} days met">${g.history.slice(-7).map((h) => html`<i class="${h.met ? "met" : h.partial ? "open" : ""}" title="${fmtDate(h.day)}: ${metricValue({ metric: g.metric, unit: g.unit }, h.value)}"></i>`)}</span>
          <b class="small">${g.met}/${g.days}</b></a>`)}</div>`
          : html`<div class="list-item muted small"><span>No goals yet. Open any measurement in <a href="#/trends/sleep?metric=sleep_duration">Trends</a> and choose Set goal: sleep, steps, resting heart rate…</span></div>`}
      </div>`;
    } },
  { id: "sleep", title: "Sleep", desc: "Hours asleep each night for the last two weeks", size: "m", on: true, needs: ["sleep"],
    render: (d) => html`<div class="card widget-card">
      <div class="card-head"><h3>${icon("bed")} Sleep</h3>${d.sleep.points.length ? html`<span class="small muted">avg ${metricValue({ unit: "h" }, d.sleep.stats.mean)}</span>` : ""}</div>
      ${d.sleep.points.length ? html`<div class="card-body"><div class="widget-chart" data-chart="sleep"></div></div>`
        : html`<div class="list-item muted small">No sleep recorded in the last two weeks.</div>`}
      <a class="card-foot" href="#/trends?metric=sleep_duration">Sleep trends ${icon("arrowRight")}</a></div>`,
    draw: (el, d, item) => {
      const box = el.querySelector("[data-chart=sleep]");
      return box && responsive(box, () => barChart(box, d.sleep.points, { unit: "h", avg: d.sleep.stats.mean, label: "Sleep", digits: 1, noTable: true, tick: (v) => `${v}h`,
        height: item.rows ? Math.max(120, box.clientHeight) : 150, format: (v) => metricValue({ unit: "h" }, v) }), { tall: Boolean(item.rows) });
    } },
  { id: "journal", title: "How you've been feeling", desc: "Your latest check-ins, moods and symptoms from the Journal", size: "m", on: true, needs: ["events"],
    render: (d) => listCard("How you've been feeling", "book", d.events.events, (e) => html`
      <a class="list-item clickable" href="#/journal" style="color:inherit;text-decoration:none">
        <div class="grow" style="min-width:0"><div class="title truncate">${e.name === "State of Mind" ? "Mood" : e.name}
          ${e.category === "stateOfMind" && e.value != null ? html` <span class="badge">${moodLabel(e.value)}</span>` : e.value_label ? html` <span class="badge">${e.value_label}</span>` : ""}</div>
          <div class="meta">${fmtDateTime(e.start_date)}</div></div></a>`,
      html`Nothing logged in the last 30 days. <a href="#/journal">Check in</a>`, { href: "#/journal", more: "Journal" }) },
  { id: "workouts", title: "Recent workouts", desc: "Your latest workouts from the phone apps and wearables", size: "m", on: true, needs: ["workouts"],
    render: (d) => listCard("Recent workouts", "activity", d.workouts.workouts, (w) => html`
      <div class="list-item clickable" data-action="workout" data-id="${w.id}">
        <div class="grow"><div class="title truncate">${w.name}</div><div class="meta truncate">${fmtDateTime(w.start_date)} · ${w.source_name}</div></div>
        <span class="small muted">${[w.distance_m ? fmtMeasure(w.distance_m, "m", "distance_walking_running") : null, duration(w.duration_s)].filter(Boolean).join(" · ")}</span>
      </div>`, "No workouts in the last 30 days.", { href: "#/workouts", more: "All workouts" }) },
  { id: "labs", title: "Out-of-range results", desc: "Your latest lab results that are high or low", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Out of range", "flask", d.summary.flagged_labs, (l) => html`
      <div class="list-item clickable ${l.older ? "muted" : ""}" data-action="lab" data-code="${l.code || ""}" data-title="${l.title}">
        <div class="grow"><div class="title">${l.title}</div><div class="meta">${fmtDate(l.effective_at)}${l.older ? ` · older result, ${ageWords(l.effective_at)}` : ""} · range ${l.ref_text || "—"}</div></div>
        <div class="value">${l.value_text}</div>${resultStatus(l.status)}${flagBadge(l.interpretation)}
      </div>`, labStatusText(d.summary.lab_status), { href: "#/trends/labs", more: "Lab trends" }) },
  { id: "medications", title: "Medications", desc: "Medications your records list as current, and any that may be out of date", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Medications", "pill", [...d.summary.active_medications, ...(d.summary.medications_to_check || [])], (m) => html`
      <div class="list-item clickable" data-action="record" data-id="${m.id}">
        <div class="grow"><div class="title truncate">${m.title}</div><div class="meta truncate">${[m.med_reason || m.value_text, m.group_note].filter(Boolean).join(" · ")}</div></div>
        ${m.med_tier === "check" ? html`<span class="badge">May be out of date</span>` : ""}
        ${m.details?.as_needed ? html`<span class="badge">As needed</span>` : ""}
      </div>`, "No active medications.", { href: "#/records/medications" }) },
  { id: "conditions", title: "Active conditions", desc: "Diagnoses from your health records", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Active conditions", "stethoscope", [...d.summary.active_conditions, ...(d.summary.unconfirmed_conditions || []), ...(d.summary.conditions_status_unknown || [])], (c) => html`
      <div class="list-item clickable" data-action="record" data-id="${c.id}">
        <div class="grow"><div class="title">${c.title}</div><div class="meta">${c.status_note || html`Since ${c.details?.onset_text || fmtDate(c.effective_at)} · ${sourcesLabel(c)}`}</div></div>
        ${(d.summary.unconfirmed_conditions || []).includes(c) ? html`<span class="badge warn">Unconfirmed</span>` : ""}
        ${(d.summary.conditions_status_unknown || []).includes(c) ? html`<span class="badge">Status not stated</span>` : ""}
        ${c.details?.icd10 ? html`<span class="badge mono">${c.details.icd10}</span>` : ""}
      </div>`, d.summary.visit_diagnoses?.length ? `No active conditions listed. ${d.summary.visit_diagnoses.length} recorded at visits, under Records.` : "No active conditions on record.", { href: "#/records/conditions" }) },
  { id: "allergies", title: "Allergies", desc: "Allergies and intolerances", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Allergies", "alert", d.summary.allergies, (a) => html`
      <div class="list-item clickable" data-action="record" data-id="${a.id}">
        <div class="grow"><div class="title">${a.title}</div><div class="meta">${a.status_note || a.narrative || ""}</div></div>
        ${a.details?.criticality === "high" ? html`<span class="badge bad">Marked high risk</span>` : html`<span class="badge">${capitalize(a.details?.category || "allergy")}</span>`}
      </div>`, d.summary.allergy_note || "No allergies on record.", { href: "#/records/allergies" }) },
  { id: "vitals", title: "Latest vitals", desc: "Blood pressure, weight and other measurements from your visits", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Latest vitals", "heart", d.summary.latest_vitals, (v) => html`
      <div class="list-item clickable" data-action="vital" data-code="${v.code || ""}" data-title="${v.title}">
        <div class="grow"><div class="title">${v.title}</div><div class="meta">${fmtDate(v.effective_at)}</div></div>
        <div class="value">${recordValue(v)}</div></div>`, "No vitals yet.", { href: "#/trends/vitals", more: "Vital trends" }) },
  { id: "visits", title: "Recent visits", desc: "Appointments and hospital stays", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Recent visits", "building", [...(d.summary.upcoming_encounters || []), ...d.summary.recent_encounters], (e) => html`
      <div class="list-item clickable" data-action="record" data-id="${e.id}">
        <div class="grow"><div class="title truncate">${e.title}</div>
          <div class="meta truncate">${[fmtDate(e.effective_at), (e.details?.clinicians || [])[0], e.source_name].filter(Boolean).join(" · ")}</div></div>
        ${(d.summary.upcoming_encounters || []).includes(e) ? html`<span class="badge info">Upcoming</span>` : ""}
      </div>`, "No visits on record.", { href: "#/timeline", more: "Full timeline" }) },
  { id: "immunizations", title: "Immunizations", desc: "Vaccines on record, newest first", size: "m", on: false, needs: ["summary"], records: true,
    render: (d) => listCard("Immunizations", "syringe", d.summary.immunizations || [], (i) => html`
      <div class="list-item clickable" data-action="record" data-id="${i.id}">
        <div class="grow"><div class="title truncate">${i.title}</div><div class="meta">${recordDate(i)}</div></div></div>`,
      "No immunizations on record.", { href: "#/records/immunizations" }) },
  { id: "sources", title: "Sources", desc: "Each connected source and when it last synced", size: "m", on: false, needs: ["conns"],
    render: (d) => listCard("Sources", "sources", d.connections, (c) => html`
      <a class="list-item clickable" href="#/sources" style="color:inherit;text-decoration:none">
        <span class="dot ${c.access_ended || c.status === "disconnected" ? "" : c.status === "active" ? "good" : "warn"}"></span>
        <div class="grow"><div class="title truncate">${c.display_name}</div><div class="meta">${c.access_ended ? `Imported ${fmtAgo(c.last_sync_at)}`
          : c.status === "needs_reauth" ? "Reconnect needed" : c.status === "error" ? "Sync failed" : `Synced ${fmtAgo(c.last_sync_at)}`}</div></div></a>`,
      "No sources yet.", { href: "#/sources", more: "Manage sources" }) },
];
const BY_ID = new Map(WIDGETS.map((w) => [w.id, w]));
// How wide an item is on the Overview's grid: a quarter, half, three quarters or all of it (half or all on a phone). Its height
// is what its content needs, until the person drags a corner to give it a number of rows.
const SIZES = ["s", "m", "l", "xl"];
const MAX_ROWS = 6;
const isMetric = (id) => id.startsWith("m:");
const rowsOf = (v) => (Number.isInteger(v) && v >= 1 && v <= MAX_ROWS ? v : undefined);
const MAX_ITEMS = 60;
const CORE_CARDS = ["labs", "medications", "conditions", "allergies"];
const NEXT_CARDS = ["vitals", "visits"];

/** The saved layout, with cards added since it was saved placed at the end (shown if they're on by default). Layouts
 * saved before numbers could be placed one by one kept them in a "today" row; its numbers take its place. */
export function resolveLayout(saved, defaultMetrics) {
  const out = [];
  const seen = new Set();
  const push = (item) => { if (!seen.has(item.id) && out.length < MAX_ITEMS) { seen.add(item.id); out.push(item); } };
  const tiles = (metrics) => metrics.forEach((m) => push({ id: `m:${m}`, visible: true, size: "s" }));
  if (!saved) tiles(defaultMetrics);
  for (const s of saved || []) {
    if (s.id === "today") { if (s.visible !== false) tiles(s.options?.metrics?.length ? s.options.metrics : defaultMetrics); continue; }
    if (isMetric(s.id)) { push({ id: s.id, visible: s.visible !== false, size: SIZES.includes(s.size) ? s.size : "s", rows: rowsOf(s.rows) }); continue; }
    const def = BY_ID.get(s.id);
    if (def) push({ id: s.id, visible: s.visible !== false, size: SIZES.includes(s.size) ? s.size : def.size, rows: rowsOf(s.rows) });
  }
  // New cards marked first (What's changed) go to the top, the rest after what's there.
  const front = [];
  for (const w of WIDGETS) {
    if (seen.has(w.id)) continue;
    if (w.first && saved) { seen.add(w.id); front.push({ id: w.id, visible: w.on, size: w.size }); } else push({ id: w.id, visible: w.on, size: w.size });
  }
  if (!saved) {
    // A new Overview starts with What's changed, then what's on the medical record, then the numbers, then the rest.
    // Without records those cards draw nothing, so the numbers come straight after What's changed.
    const firsts = out.filter((i) => BY_ID.get(i.id)?.first);
    const picked = (ids) => ids.map((id) => out.find((i) => i.id === id));
    const early = [...firsts, ...picked(CORE_CARDS), ...out.filter((i) => isMetric(i.id)), ...picked(NEXT_CARDS)];
    return [...early, ...out.filter((i) => !early.includes(i))];
  }
  return [...front, ...out].slice(0, MAX_ITEMS);
}

// ------------------------------------------------------------------ page
export async function render({ el, state, navigate }) {
  mount(el, loading(4));
  const profile = state.profile;
  // The summary, sources, goals and the default numbers are needed whatever the layout, so they load alongside it.
  // So do the insights, the slowest to work out: most layouts show them, and waiting for the layout first held up the page.
  const early = { summary: get("/api/summary", { profile: profile.id }), conns: get("/api/connections", { profile: profile.id }),
    bio: get("/api/biometrics/overview", { profile: profile.id, brief: true }),
    goals: get("/api/goals", { profile: profile.id }).catch(() => ({ progress: [] })),
    insights: loadInsights(profile.id).catch(() => ({ insights: [] })) };
  const [saved, defaults] = await Promise.all([get(`/api/profiles/${profile.id}/overview`).catch(() => ({ widgets: null })), early.bio]);
  const defaultMetrics = () => defaults.headline.slice(0, DEFAULT_TILES).map((h) => h.metric);
  let layout = resolveLayout(saved.widgets, defaultMetrics());
  const data = {};
  let stopCharts = [];
  let editing = false;

  const metricIds = () => layout.filter((w) => w.visible && isMetric(w.id)).map((w) => w.id.slice(2));
  const load = async () => {
    const needs = new Set(layout.filter((w) => w.visible && !isMetric(w.id)).flatMap((w) => BY_ID.get(w.id).needs));
    const wanted = metricIds();
    const have = new Set(defaults.headline.map((h) => h.metric));
    const jobs = {
      // Always loaded: the empty state and the sync pill depend on them.
      summary: () => early.summary,
      bio: () => (wanted.every((m) => have.has(m)) ? defaults
        : get("/api/biometrics/overview", { profile: profile.id, brief: true, tiles: wanted.join(",") })),
      conns: () => early.conns,
      sleep: () => needs.has("sleep") ? get("/api/biometrics/daily", { profile: profile.id, metric: "sleep_duration", days: 14 }) : null,
      workouts: () => needs.has("workouts") ? get("/api/biometrics/workouts", { profile: profile.id, days: 30, limit: LIST_MAX }) : null,
      events: () => needs.has("events") ? get("/api/biometrics/events", { profile: profile.id, view: "journal", days: 30, limit: 20 }) : null,
      insights: () => needs.has("insights") ? early.insights : null,
      newRecords: () => needs.has("newRecords") ? get("/api/new-records", { profile: profile.id }) : null,
      // Goals also show on the number tiles, so they load whenever there are any.
      goals: () => early.goals,
    };
    const keys = Object.keys(jobs);
    const results = await Promise.all(keys.map((k) => (data[k] && k !== "bio" ? data[k] : jobs[k]())));
    keys.forEach((k, i) => { if (results[i]) data[k] = results[i]; });
    data.connections = data.conns.connections;
    data.metrics = new Map([...defaults.headline, ...data.bio.headline].map((h) => [h.metric, h]));
  };

  /** What an item shows, or null when it has nothing to show (records not connected, a number with no data). */
  const content = (w) => {
    if (isMetric(w.id)) {
      const m = data.metrics.get(w.id.slice(2));
      const goal = (data.goals?.progress || []).find((g) => g.metric === m?.metric && g.days);
      return m ? tile(m, editing ? "" : `data-action="metric" data-metric="${m.metric}" role="link" tabindex="0"`, goal) : null;
    }
    const def = BY_ID.get(w.id);
    if (def.records && !data.hasRecords) return null;
    const out = def.render(data);
    return out && out.value ? out : null;
  };
  const title = (w) => (isMetric(w.id) ? data.metrics.get(w.id.slice(2))?.label || w.id.slice(2) : BY_ID.get(w.id).title);

  // Always there, shown while editing, so starting and finishing an edit changes nothing else on the page.
  const itemControls = (w) => html`<div class="dash-controls">
      <button class="dash-handle" data-handle aria-label="Move ${title(w)}. Use the arrow keys to move it.">${icon("grip")}</button>
      <button class="dash-remove" data-action="remove" data-id="${w.id}" aria-label="Remove ${title(w)}" title="Remove">${icon("x")}</button>
    </div>
    ${["tl", "tr", "bl"].map((c) => html`<span class="dash-corner ${c}" data-corner="${c}" aria-hidden="true"></span>`)}
    <button class="dash-corner br" data-corner="br" aria-label="Resize ${title(w)}. Use the arrow keys to resize it."></button>`;

  const syncPill = () => {
    const connections = data.connections;
    const issues = connections.filter((c) => !c.access_ended && (c.status === "needs_reauth" || c.status === "error"));
    const syncing = connections.some((c) => c.syncing);
    const lastSync = connections.map((c) => c.last_sync_at).filter(Boolean).sort().pop();
    const oldest = connections.filter(isStale).sort((a, b) => a.last_sync_at - b.last_sync_at)[0];
    const partly = connections.filter((c) => !c.access_ended && c.last_sync_status === "partial" && c.status === "active");
    return html`<a class="sync-pill" href="#/sources"><span class="dot ${syncing ? "info pulse" : issues.length || oldest || partly.length ? "warn" : "good"}"></span>
      ${syncing ? "Syncing…" : issues.length ? `${plural(issues.length, "source")} need${issues.length === 1 ? "s" : ""} attention`
        : oldest ? `${oldest.display_name} last synced ${fmtAgo(oldest.last_sync_at)}`
        : partly.length ? `${partly[0].display_name} synced in part`
        : `${plural(connections.length, "source")}${lastSync ? ` · synced ${fmtAgo(lastSync)}` : ""}`}</a>`;
  };
  const headActions = () => (editing ? html`<button class="btn btn-sm btn-ghost" data-action="reset">Reset</button>
      <button class="btn btn-sm" data-action="add">${icon("plus")} Add</button>
      <button class="btn btn-sm btn-primary" data-action="done">${icon("check")} Done</button>`
    : html`${syncPill()}<button class="btn btn-sm" data-action="edit">${icon("sliders")} Edit</button>`);
  const EDIT_HINT = html`<div class="banner">${icon("grip")}<div class="grow">Drag a card to move it, and drag any of its corners to resize it. Add brings back cards and numbers.</div></div>`;

  const draw = () => {
    const { summary, bio, connections } = data;
    const conflict = summary.identity_conflict || [];
    const identity = conflict.length ? null : summary.identities[0];   // don't guess age/sex from someone else's chart
    const years = ageText(profile.birth_date || identity?.birth_date);
    const first = profile.name.split(" ")[0];
    // Someone else's Overview (a child, a parent you look after, someone who shares with you) is headed with their name:
    // the greeting is for whoever is signed in.
    const self = !state.status?.account || state.status.account.profile_id === profile.id;
    const heading = self ? `${greeting()}, ${first}` : profile.name;
    data.hasRecords = Object.values(summary.counts || {}).some((n) => n > 0);

    if (!connections.length && !bio.has_samples) {
      mount(el, html`
        <div class="page-head"><div><h1>${heading}</h1>
          <p>Let's bring ${self ? "your" : `${first}'s`} health data together. Everything is saved on the computer that runs Syntropy Health.</p></div></div>
        <div class="grid grid-auto">
          ${[
            ["building", "Medical records", "Bring in labs, medications, conditions and visits from patient portals like Epic MyChart, Oracle Health, athenahealth and healow.", "#/sources?add=ehr", "Find my health system", "btn-primary"],
            ["watch", "Wearables and phones", "Pair the Syntropy iPhone or Android app for Apple Health or Health Connect, or link Oura, WHOOP or Google Health for sleep, recovery, HRV and training.", "#/sources", "Connect a device", ""],
            ["upload", "Import files", "Drop in an Apple Health export, or a record file you downloaded from a patient portal.", "#/sources?add=import", "Import a file", ""],
          ].map(([ic, t, text, href, cta, kind]) => html`
            <div class="card card-pad stack" style="gap:10px">
              <div class="row"><span class="src-avatar">${icon(ic)}</span><h2>${t}</h2></div>
              <p class="muted">${text}</p>
              <div style="margin-top:auto"><a class="btn ${kind}" href="${href}">${cta} ${icon("arrowRight")}</a></div>
            </div>`)}
        </div>
        <p class="onboard-note">${icon("shieldCheck")}<span>No cloud account. Your sign-ins to other services are stored encrypted, and every access is recorded.
          <a href="#/settings/security">Review security</a></span></p>`);
      return;
    }

    const items = layout.filter((w) => w.visible).map((w) => ({ w, out: content(w) })).filter((x) => x.out);
    mount(el, html`
      <div class="page-head">
        <div><h1>${heading}</h1>
          <p>${[years, identity?.gender ? identity.gender.charAt(0).toUpperCase() + identity.gender.slice(1) : null].filter(Boolean).join(" · ")}</p></div>
        <div class="row wrap dash-actions" id="dash-actions" style="gap:8px">${headActions()}</div>
      </div>
      ${conflict.length ? html`<div class="banner warn">${icon("alert")}<div class="grow"><b>These records name different people:</b>
        ${conflict.map((p) => `${p.full_name || "unnamed patient"}${p.birth_date ? ` (born ${fmtDate(p.birth_date)})` : ""}`).join(", ")}.
        <a href="#/sources">Review sources</a></div></div>` : ""}
      <div class="dash-hint" id="dash-hint">${editing ? EDIT_HINT : ""}</div>
      ${items.length ? html`<div class="dash ${editing ? "editing" : ""}" id="dash">${items.map(({ w, out }) => html`
          <div class="dash-item size-${w.size} ${isMetric(w.id) ? "is-tile" : ""}" data-id="${w.id}" ${raw(w.rows ? `data-rows="${w.rows}" style="--rows:${w.rows}"` : "")}>
            <div class="dash-inner">${out}${itemControls(w)}</div></div>`)}</div>`
        : html`<div class="card">${emptyState("Nothing to show", "Every card is hidden.", html`<button class="btn btn-primary" data-action="add">${icon("plus")} Add cards</button>`)}</div>`}`);
    stopCharts.forEach((stop) => stop());
    stopCharts = items.map(({ w }) => (!isMetric(w.id) && BY_ID.get(w.id).draw?.(el, data, w))).filter(Boolean);
    const grid = $("#dash", el);
    dash = null;
    if (grid) {
      dash = masonry(grid);
      stopCharts.push(dash.stop);
      if (editing) quietCards(grid, true);
    }
  };
  let dash = null;
  // While editing, a card is something to pick up: its own links and buttons wait.
  const quietCards = (grid, on) => $$(".dash-inner > .card", grid).forEach((c) => { c.inert = on; });

  /** Runs `change` (a card moved or resized, or the whole grid drawn again), and everything travels from where it was to where it now belongs. */
  const glide = (change) => {
    const measure = () => new Map($$(".dash-item", el).map((i) => [i.dataset.id, i.firstElementChild.getBoundingClientRect()]));
    if (prefersStill() || !$("#dash", el)) { change(); return; }
    const before = measure();       // what's still moving carries on from where it has got to
    $$(".dash-inner", el).forEach((i) => i.getAnimations().forEach((a) => a.cancel()));
    change();
    dash?.layout();
    const running = [];
    for (const item of $$(".dash-item", el)) {
      const inner = item.firstElementChild;
      const from = before.get(item.dataset.id);
      const to = inner.getBoundingClientRect();
      if (!from) {
        running.push(inner.animate([{ opacity: 0, transform: "scale(.94)" }, { opacity: 1, transform: "none" }], { duration: 220, easing: EASE_OUT }));
        continue;
      }
      const dx = from.left - to.left, dy = from.top - to.top;
      const resized = Math.abs(from.width - to.width) > 1 || Math.abs(from.height - to.height) > 1;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && !resized) continue;
      const size = (r) => (resized ? { width: `${r.width}px`, height: `${r.height}px` } : {});
      if (resized) inner.classList.add("morphing");
      const run = inner.animate([{ transform: `translate(${dx}px, ${dy}px)`, ...size(from) }, { transform: "none", ...size(to) }], { duration: 320, easing: SPRING });
      const finish = () => inner.classList.remove("morphing");
      run.finished.then(finish, finish);
      running.push(run);
    }
    if (!running.length || !dash) return;
    // The cards' sizes are changing frame by frame: packing the grid to them meanwhile would make everything shake.
    const packing = dash;
    packing.hold(true);
    Promise.allSettled(running.map((r) => r.finished)).then(() => {
      if (!$$(".dash-inner", el).some((i) => i.getAnimations().length)) packing.hold(false);
    });
  };

  let saveTimer = null;
  let customized = Boolean(saved.widgets);
  const sendLayout = () => {
    saveTimer = null;
    return put(`/api/profiles/${profile.id}/overview`, { widgets: layout.map(({ id, visible, size, rows }) => ({ id, visible, size, rows: rows || null })) })
      .catch((err) => toast(`Couldn't save the layout: ${err.message}`, "bad"));
  };
  const save = () => {
    customized = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(sendLayout, 400);
  };
  const apply = async (next, { reload = false } = {}) => {
    layout = next;
    save();
    if (reload) await load();
    redraw();
  };
  // The same page drawn again in place: nothing moves that hasn't changed, and the charts don't draw themselves in again.
  const redraw = () => {
    document.documentElement.classList.add("no-intro");
    glide(draw);
  };
  /** The shown items in their new order; hidden cards keep their place after them. */
  const reorder = (ids) => {
    const byId = new Map(layout.map((w) => [w.id, w]));
    const moved = ids.map((id) => byId.get(id)).filter(Boolean);
    layout = [...moved, ...layout.filter((w) => !ids.includes(w.id))];
    save();
  };

  await load();
  draw();
  // The bell's alerts come from the same summary and sources (other pages ask for them themselves).
  refreshAlerts(profile, state.status, { summary: data.summary, connections: data.connections });

  const off = onAction(el, {
    record: ({ id }) => openRecord(id),
    lab: ({ code, title }) => navigate(`#/trends/labs?${code ? `code=${encodeURIComponent(code)}` : `title=${encodeURIComponent(title)}`}`),
    vital: ({ code, title }) => navigate(`#/trends/vitals?${code ? `code=${encodeURIComponent(code)}` : `title=${encodeURIComponent(title)}`}`),
    metric: ({ metric }) => navigate(`#/trends?metric=${encodeURIComponent(metric)}`),
    workout: ({ id }) => openWorkout(profile, id),
    "hide-insight": async ({ id }) => {
      await hideInsight(profile.id, id);
      if (data.insights?.insights) data.insights.insights = data.insights.insights.filter((i) => i.id !== id);
      redraw();
    },
    "seen-new": async () => {
      await api(`/api/new-records/seen?profile=${encodeURIComponent(profile.id)}`, { method: "POST" });
      data.newRecords = null;
      redraw();
    },
    edit: () => setEditing(true),
    done: async () => {
      setEditing(false);
      // Done means saved, even if the window closes straight after.
      if (saveTimer) { clearTimeout(saveTimer); await sendLayout(); }
    },
    remove: async ({ id }, btn) => {
      const inner = btn.closest(".dash-inner");
      if (!prefersStill()) await inner.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(.94)" }], { duration: 130, easing: "ease-in", fill: "forwards" }).finished;
      return apply(isMetric(id) ? layout.filter((w) => w.id !== id) : layout.map((w) => (w.id === id ? { ...w, visible: false } : w)));
    },
    add: () => openAdd({ profile, getLayout: () => layout, data,
      onAdd: (id) => {
        const next = isMetric(id) ? [...layout, { id, visible: true, size: "s" }]
          : layout.map((w) => (w.id === id ? { ...w, visible: true } : w));
        // New items go at the end of what's shown, not after hidden cards.
        const item = next.find((w) => w.id === id);
        const rest = next.filter((w) => w !== item);
        const lastShown = rest.map((w) => w.visible).lastIndexOf(true);
        rest.splice(lastShown + 1, 0, item);
        return apply(rest, { reload: true });
      } }),
    reset: async () => {
      if (!(await confirmDialog("Reset the Overview?", "Cards, numbers and sizes go back to how they started.", { confirmLabel: "Reset" }))) return;
      clearTimeout(saveTimer);
      saveTimer = null;
      if (customized) await del(`/api/profiles/${profile.id}/overview`);
      customized = false;
      layout = resolveLayout(null, defaultMetrics());
      await load();
      redraw();
    },
  });

  const setEditing = (on) => {
    editing = on;
    mount($("#dash-actions", el), headActions());
    const grid = $("#dash", el);
    grid?.classList.toggle("editing", on);
    if (grid) quietCards(grid, on);
    const hint = $("#dash-hint", el);
    easeHeight(hint, () => mount(hint, on ? EDIT_HINT : ""));
  };
  const stopArranging = arrange(el, {
    isEditing: () => editing, glide, haptic,
    item: (id) => layout.find((w) => w.id === id),
    title,
    moved: reorder,
    resized: (w, node, { size, rows }) => {
      node.classList.remove(`size-${w.size}`);
      node.classList.add(`size-${size}`);
      w.size = size;
      w.rows = rows || undefined;
      if (rows) { node.dataset.rows = rows; node.style.setProperty("--rows", rows); }
      else { delete node.dataset.rows; node.style.removeProperty("--rows"); }
    },
    saved: save,
  });
  // Leaving straight after a change still saves it.
  return () => { off(); stopArranging(); if (saveTimer) { clearTimeout(saveTimer); sendLayout(); } stopCharts.forEach((stop) => stop()); };
}

// ------------------------------------------------------------------ adding cards and numbers
function openAdd({ profile, getLayout, data, onAdd }) {
  const m = modal({ title: "Add to Overview", body: loading(3), footer: html`<button class="btn btn-primary" data-done>Done</button>` });
  m.el.querySelector("[data-done]").addEventListener("click", m.close);
  let metrics = null;
  const draw = () => {
    const layout = getLayout();
    const shown = new Set(layout.filter((w) => w.visible).map((w) => w.id));
    const cards = WIDGETS.filter((w) => !shown.has(w.id) && !(w.records && !data.hasRecords));
    const numbers = metrics.filter((x) => !shown.has(`m:${x.metric}`));
    const groups = new Map();
    for (const x of numbers) {
      const g = GROUP_LABELS[x.group] || "Other";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(x);
    }
    m.setBody(html`
      <div class="dash-add-section">Cards</div>
      ${cards.length ? html`<div class="list">${cards.map((w) => html`<button class="list-item clickable dash-add" data-add="${w.id}">
          <div class="grow"><div class="title">${w.title}</div><div class="meta">${w.desc}</div></div>${icon("plus")}</button>`)}</div>`
        : html`<p class="small muted">Every card is on your Overview.</p>`}
      <div class="dash-add-section">Numbers</div>
      ${numbers.length ? [...groups.entries()].map(([g, list]) => html`<div class="dash-add-group">${g}</div>
          <div class="dash-add-chips">${list.map((x) => html`<button class="chip" data-add="m:${x.metric}">${icon("plus")} ${x.label}</button>`)}</div>`)
        : html`<p class="small muted">${metrics.length ? "Every number you have is on your Overview." : "Numbers appear once a wearable or the iPhone app sends data."}</p>`}`);
  };
  get("/api/biometrics/metrics", { profile: profile.id }).then((r) => { metrics = r.metrics; draw(); })
    .catch((err) => m.setBody(html`<p class="muted">${err.message}</p>`));
  m.body.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-add]");
    if (!b) return;
    b.disabled = true;
    try { await onAdd(b.dataset.add); }
    catch (err) { b.disabled = false; toast(err.message, "bad"); return; }
    draw();
  });
}
const GROUP_LABELS = { activity: "Activity", mobility: "Activity", heart: "Heart", readiness: "Recovery", sleep: "Sleep",
  vitals: "Vitals", body: "Body", hearing: "Hearing", nutrition: "Nutrition" };

// ------------------------------------------------------------------ layout mechanics
const DASH_GAP = 16;
// A touch past the mark and back, the way things settle on a phone's home screen.
const SPRING = window.CSS?.supports?.("animation-timing-function", "linear(0, 1)")
  ? "linear(0, .07 4%, .27 10%, .6 19%, .88 29%, 1.02 39%, 1.05 47%, 1.04 56%, 1.01 70%, 1)" : "cubic-bezier(.2, .9, .3, 1)";

/**
 * Packs the items like a masonry wall: each spans as many small grid rows as its content is tall, so a number tile sits
 * next to a tall card without stretching, and the dense flow fills the gaps. Items given a number of rows are that tall.
 */
function masonry(grid) {
  const layout = () => {
    const items = $$(".dash-item", grid);
    // The number tiles share one height, so they line up in rows: a tile with a goal line is a little taller, and on
    // its own it pushed the next row's first tile over a column.
    const cards = items.filter((i) => i.classList.contains("is-tile") && !i.dataset.rows).map((i) => $(".tile", i)).filter(Boolean);
    cards.forEach((c) => { c.style.minHeight = ""; });
    const tallest = Math.max(0, ...cards.map((c) => c.getBoundingClientRect().height));
    cards.forEach((c) => { c.style.minHeight = `${tallest}px`; });
    for (const item of items) {
      item.style.gridRowEnd = `span ${Math.max(1, Math.ceil((item.firstElementChild.offsetHeight + DASH_GAP) / 2))}`;
    }
  };
  // The page is built before it's on screen, when nothing has a height yet: measured then, every item would span a
  // sliver and the first frame shown would be a pile of collapsed cards. So the first layout runs the moment it arrives,
  // later ones once per frame.
  if (grid.isConnected) layout();
  else grid.parentElement?.addEventListener("page-shown", layout, { once: true });
  let queued = false;
  let held = false;
  const ro = new ResizeObserver(() => {
    if (queued || held) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; if (!held) layout(); });
  });
  $$(".dash-item", grid).forEach((item) => ro.observe(item.firstElementChild));
  return { layout, stop: () => ro.disconnect(), hold: (on) => { held = on; if (!on) layout(); } };
}

const DRAG_MAX_HEIGHT = 240;     // a dragged card's copy is no taller than this (px)
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/** How many of the grid's columns a size covers, and the other way round (a phone's grid has two). */
function spanOf(size, cols) {
  if (cols >= 4) return SIZES.indexOf(size) + 1;
  return size === "s" ? 1 : cols;
}
function sizeFor(span, cols, current) {
  if (cols >= 4) return SIZES[span - 1];
  return span === 1 ? "s" : current === "s" ? "m" : current;
}

/**
 * Editing the Overview: pick a card up and the others make way for it, or drag a corner and it snaps through the sizes the
 * grid has while everything around it moves. A mouse can take a card anywhere; a finger on a card scrolls the page, so a
 * finger uses its grip. The keyboard moves a card with the arrow keys on its grip, and resizes it with them on its corner.
 */
function arrange(root, { isEditing, glide, haptic, item, title, moved, resized, saved }) {
  const gridOf = () => $("#dash", root);
  const ids = () => $$(".dash-item", gridOf()).map((x) => x.dataset.id);
  const columns = (grid) => getComputedStyle(grid).gridTemplateColumns.split(" ").length;
  const rowHeight = (grid) => parseFloat(getComputedStyle(grid).getPropertyValue("--dash-row")) || 148;

  const moveCard = (e, node) => {
    const grid = gridOf();
    const before = ids().join();
    const pointer = e.pointerId;
    const start = { x: e.clientX, y: e.clientY };
    let last = start, ghost = null, raf = null, dx = 0, dy = 0, movedAt = 0, movedFrom = start;
    const lift = () => {
      const r = node.firstElementChild.getBoundingClientRect();
      dx = start.x - r.left;
      dy = start.y - r.top;
      // The copy follows the finger from the point it was picked up by, and a long card is cut short, so it doesn't cover the page.
      const height = Math.min(r.height, Math.max(DRAG_MAX_HEIGHT, dy + 60));
      ghost = document.createElement("div");
      ghost.className = `dash-ghost${height < r.height ? " cut" : ""}`;
      ghost.append(node.firstElementChild.cloneNode(true));
      Object.assign(ghost.style, { width: `${r.width}px`, height: `${height}px`, left: `${r.left}px`, top: `${r.top}px`, transformOrigin: `${dx}px ${dy}px` });
      document.body.append(ghost);
      node.classList.add("dragging");
      haptic("medium");
      raf = requestAnimationFrame(edgeScroll);
    };
    const place = () => {
      ghost.style.left = `${last.x - dx}px`;
      ghost.style.top = `${last.y - dy}px`;
      // Having just made room, wait for the pointer to go somewhere new: cards of different sizes would otherwise swap back and forth.
      if (performance.now() - movedAt < 120 || Math.hypot(last.x - movedFrom.x, last.y - movedFrom.y) < 20) return;
      const target = document.elementsFromPoint(last.x, last.y).map((n) => n.closest?.(".dash-item")).find((n) => n && n !== node && grid.contains(n));
      if (!target) return;
      const t = target.getBoundingClientRect();
      // Before the target when the pointer is in its first half (left half, or top half of a full-width item).
      const first = t.width > grid.clientWidth * 0.9 ? last.y < t.top + t.height / 2 : last.x < t.left + t.width / 2;
      const ref = first ? target : target.nextElementSibling;
      if (ref === node || node.nextElementSibling === ref) return;
      glide(() => grid.insertBefore(node, ref));
      movedAt = performance.now();
      movedFrom = last;
      haptic("selection");
    };
    // Scrolls the page while dragging near the top or bottom of what's visible (in the iPhone app the page runs under
    // its title and tab bars, so the visible part is the visual viewport, not the window).
    const edgeScroll = () => {
      const vv = window.visualViewport;
      const top = (vv?.offsetTop || 0) + 70, bottom = (vv ? vv.offsetTop + vv.height : window.innerHeight) - 70;
      const edge = last.y < top ? -10 : last.y > bottom ? 10 : 0;
      if (edge) { pageScroller().scrollBy(0, edge); place(); }
      raf = requestAnimationFrame(edgeScroll);
    };
    const move = (ev) => {
      if (ev.pointerId !== pointer) return;
      last = { x: ev.clientX, y: ev.clientY };
      if (!ghost && Math.hypot(last.x - start.x, last.y - start.y) >= 4) lift();
      if (ghost) { ev.preventDefault(); place(); }
    };
    const end = (ev) => {
      if (ev.pointerId !== pointer) return;
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", end);
      document.removeEventListener("pointercancel", end);
      if (!ghost) return;
      cancelAnimationFrame(raf);
      // It settles into its place rather than vanishing over it.
      const to = node.firstElementChild.getBoundingClientRect();
      const landed = () => { ghost.remove(); node.classList.remove("dragging"); };
      if (prefersStill()) landed();
      else ghost.animate([{ left: ghost.style.left, top: ghost.style.top, transform: "scale(1.03)" }, { left: `${to.left}px`, top: `${to.top}px`, transform: "none" }],
        { duration: 180, easing: EASE_OUT, fill: "forwards" }).finished.then(landed, landed);
      if (ids().join() !== before) moved(ids());
    };
    document.addEventListener("pointermove", move, { passive: false });
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
  };

  /** Applies a size if it's different, and lets the grid settle around it. */
  const setSize = (node, w, span, rows) => {
    const cols = columns(gridOf());
    const size = sizeFor(span, cols, w.size);
    if (size === w.size && (rows || undefined) === w.rows) return false;
    glide(() => resized(w, node, { size, rows }));
    return true;
  };

  const resizeCard = (e, corner) => {
    const grid = gridOf();
    const node = corner.closest(".dash-item");
    const w = item(node.dataset.id);
    if (!w) return;
    const box = node.firstElementChild.getBoundingClientRect();
    const cols = columns(grid);
    const colWidth = (grid.clientWidth - DASH_GAP * (cols - 1)) / cols;
    const unit = rowHeight(grid);
    const side = corner.dataset.corner;
    const wasRows = w.rows;
    const was = { size: w.size, rows: w.rows };
    const pointer = e.pointerId;
    let span = spanOf(w.size, cols), rows = w.rows;
    const outline = document.createElement("div");
    outline.className = "dash-sizer";
    const label = document.createElement("span");
    outline.append(label);
    document.body.append(outline);
    const describe = () => { label.textContent = `${span} wide${rows ? ` · ${rows} tall` : ""}`; };
    const draw = (width, height) => {
      Object.assign(outline.style, { width: `${width}px`, height: `${height}px`,
        left: `${side[1] === "r" ? box.left : box.right - width}px`, top: `${side[0] === "b" ? box.top : box.bottom - height}px` });
    };
    draw(box.width, box.height);
    describe();
    document.documentElement.classList.add("dash-resizing");
    const move = (ev) => {
      if (ev.pointerId !== pointer) return;
      ev.preventDefault();
      const width = Math.max(colWidth * 0.6, box.width + (side[1] === "r" ? 1 : -1) * (ev.clientX - e.clientX));
      const height = Math.max(unit * 0.6, box.height + (side[0] === "b" ? 1 : -1) * (ev.clientY - e.clientY));
      draw(width, height);
      // A size is taken when the corner is well past the halfway mark, so it doesn't flicker between two at the boundary.
      const nearSpan = (width + DASH_GAP) / (colWidth + DASH_GAP);
      const nextSpan = Math.abs(nearSpan - span) > 0.6 ? clamp(Math.round(nearSpan), 1, cols) : span;
      const nearRows = (height + DASH_GAP) / (unit + DASH_GAP);
      // Sideways alone leaves a card as tall as its content needs. Its height becomes a number of rows once it's dragged to one.
      const changedHeight = wasRows != null || Math.abs(height - box.height) > unit / 2;
      const nextRows = !changedHeight ? undefined : rows && Math.abs(nearRows - rows) <= 0.6 ? rows : clamp(Math.round(nearRows), 1, MAX_ROWS);
      if (nextSpan === span && nextRows === rows) return;
      span = nextSpan;
      rows = nextRows;
      describe();
      if (setSize(node, w, span, rows)) haptic("selection");
    };
    const end = (ev) => {
      if (ev.pointerId !== pointer) return;
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", end);
      document.removeEventListener("pointercancel", end);
      document.removeEventListener("keydown", cancel, true);
      document.documentElement.classList.remove("dash-resizing");
      outline.remove();
      if (w.size !== was.size || w.rows !== was.rows) saved();
    };
    const cancel = (ev) => {
      if (ev.key !== "Escape") return;
      ev.stopPropagation();
      glide(() => resized(w, node, was));
      end({ pointerId: pointer });
    };
    document.addEventListener("pointermove", move, { passive: false });
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
    document.addEventListener("keydown", cancel, true);
  };

  const pressed = (e) => {
    if (!isEditing() || e.button > 0 || !gridOf()?.contains(e.target)) return;
    const corner = e.target.closest("[data-corner]");
    if (corner) { e.preventDefault(); resizeCard(e, corner); return; }
    if (e.target.closest(".dash-remove")) return;
    const handle = e.target.closest("[data-handle]");
    if (e.pointerType === "touch" && !handle) return;
    const node = e.target.closest(".dash-item");
    if (!node) return;
    e.preventDefault();
    moveCard(e, node);
  };
  const keys = (e) => {
    if (!isEditing() || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const grid = gridOf();
    const handle = e.target.closest?.("[data-handle]");
    const corner = e.target.closest?.("[data-corner]");
    if (handle) {
      e.preventDefault();
      const node = handle.closest(".dash-item");
      const back = e.key === "ArrowUp" || e.key === "ArrowLeft";
      const sibling = back ? node.previousElementSibling : node.nextElementSibling;
      if (!sibling) return;
      glide(() => grid.insertBefore(node, back ? sibling : sibling.nextElementSibling));
      handle.focus();
      moved(ids());
    } else if (corner) {
      e.preventDefault();
      const node = corner.closest(".dash-item");
      const w = item(node.dataset.id);
      const cols = columns(grid);
      const unit = rowHeight(grid);
      const rows = w.rows || clamp(Math.round((node.firstElementChild.offsetHeight + DASH_GAP) / (unit + DASH_GAP)), 1, MAX_ROWS);
      const span = spanOf(w.size, cols);
      const next = { ArrowLeft: [span - 1, w.rows], ArrowRight: [span + 1, w.rows], ArrowUp: [span, rows - 1], ArrowDown: [span, rows + 1] }[e.key];
      if (setSize(node, w, clamp(next[0], 1, cols), next[1] == null ? undefined : clamp(next[1], 1, MAX_ROWS))) saved();
      corner.focus();
    }
  };
  root.addEventListener("pointerdown", pressed);
  root.addEventListener("keydown", keys);
  return () => { root.removeEventListener("pointerdown", pressed); root.removeEventListener("keydown", keys); };
}
