import { del, get, put } from "../api.js";
import { refreshAlerts } from "../alerts.js";
import { barChart, responsive, sparkline } from "../charts.js";
import { openRecord } from "./record-detail.js";
import { moodLabel } from "./journal.js";
import { duration, openWorkout } from "./workouts.js";
import {
  pageScroller, $, $$, age, confirmDialog, emptyState, flagBadge, fmtAgo, fmtDate, fmtDateTime, fmtNum, html, icon, loading, modal, mount, onAction, raw, plural, toast,
} from "../ui.js";
import { displayUnit, fmtMeasure, recordValue, unitSystem } from "../units.js";

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
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
const HIDDEN_KEY = "syntropy-insights-hidden";
function hiddenInsights() { try { return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")); } catch { return new Set(); } }
export function hideInsight(id) {
  const ids = [...hiddenInsights(), id].slice(-200);
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(ids)); } catch {}
}

/** An insight's sentence. Ones about a measurement are written here, so the numbers are in the person's units. */
export function insightText(i) {
  const d = i.data || {};
  const m = { metric: d.metric, unit: d.unit };
  const v = (x) => `${metricValue(m, x)}${metricUnit(m) ? ` ${metricUnit(m)}` : ""}`;
  if (i.kind === "change") return `${i.title.split(" is ")[0]} averaged ${v(d.recent)} over the last ${d.days} days, ${d.recent > d.usual ? "up" : "down"} from your usual ${v(d.usual)}.`;
  if (i.kind === "medication") return `In the month after you started ${d.medication} (${fmtDate(d.started)}), this averaged ${v(d.after)}, against ${v(d.before)} the month before. Other things change too, so talk it over with whoever prescribed it rather than reading it as an effect.`;
  return i.text;
}

export function insightItem(i, { hide = true } = {}) {
  return html`<div class="insight ${i.tone}">
    <span class="insight-icon">${icon(TONE_ICON[i.tone] || "lightbulb")}</span>
    <div class="grow" style="min-width:0"><div class="insight-title">${i.title}</div>
      <div class="small muted">${insightText(i)}</div>
      ${i.link ? html`<a class="small" href="${i.link}">${i.kind === "link" ? "Compare" : "See the chart"} ${icon("arrowRight")}</a>` : ""}</div>
    ${hide ? html`<button class="btn btn-ghost btn-icon btn-sm insight-hide" data-action="hide-insight" data-id="${i.id}" aria-label="Hide this" title="Hide">${icon("x")}</button>` : ""}
  </div>`;
}

const LIST_MAX = 5;
const DEFAULT_TILES = 6;   // numbers shown until the person arranges their own

/** A summary card. Long lists show their first few items and link to the full list. */
function listCard(title, iconName, items, renderItem, emptyText, { href, more = "View all" } = {}) {
  return html`<div class="card widget-card">
    <div class="card-head"><h3>${icon(iconName)} ${title}</h3>${items.length ? html`<span class="badge">${items.length}</span>` : ""}</div>
    <div class="list">${items.length ? items.slice(0, LIST_MAX).map(renderItem) : html`<div class="list-item muted small">${emptyText}</div>`}</div>
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
  { id: "insights", title: "What's changed", desc: "Changes from your normal and patterns across sleep, heart, training, labs and how you feel", size: "xl", on: true,
    needs: ["insights"], first: true,
    render: (d) => {
      const hidden = hiddenInsights();
      const items = (d.insights?.insights || []).filter((i) => !hidden.has(i.id));
      return html`<div class="card widget-card">
        <div class="card-head"><h3>${icon("lightbulb")} What's changed</h3><span class="small muted">across all your sources</span></div>
        ${items.length ? html`<div class="insights">${items.slice(0, 6).map((i) => insightItem(i))}</div>`
          : html`<div class="list-item muted small">Nothing stands out right now. Insights appear as your devices, check-ins and records build up a picture of your normal.</div>`}
        ${items.length > 6 ? html`<div class="card-foot muted small">${items.length - 6} more: ask about them in <a href="#/ask">Ask</a></div>` : ""}
      </div>`;
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
          : html`<div class="list-item muted small">No goals yet. Open any measurement in <a href="#/trends/sleep?metric=sleep_duration">Trends</a> and choose Set goal: sleep, steps, resting heart rate…</div>`}
      </div>`;
    } },
  { id: "sleep", title: "Sleep", desc: "Hours asleep each night for the last two weeks", size: "m", on: true, needs: ["sleep"],
    render: (d) => html`<div class="card widget-card">
      <div class="card-head"><h3>${icon("bed")} Sleep</h3>${d.sleep.points.length ? html`<span class="small muted">avg ${metricValue({ unit: "h" }, d.sleep.stats.mean)}</span>` : ""}</div>
      ${d.sleep.points.length ? html`<div class="card-body"><div class="widget-chart" data-chart="sleep"></div></div>`
        : html`<div class="list-item muted small">No sleep recorded in the last two weeks.</div>`}
      <a class="card-foot" href="#/trends?metric=sleep_duration">Sleep trends ${icon("arrowRight")}</a></div>`,
    draw: (el, d) => {
      const box = el.querySelector("[data-chart=sleep]");
      return box && responsive(box, () => barChart(box, d.sleep.points, { unit: "h", avg: d.sleep.stats.mean, label: "Sleep", digits: 1, height: 150,
        format: (v) => metricValue({ unit: "h" }, v) }));
    } },
  { id: "journal", title: "How you've been feeling", desc: "Your latest check-ins, moods and symptoms from the Journal", size: "m", on: true, needs: ["events"],
    render: (d) => listCard("How you've been feeling", "book", d.events.events, (e) => html`
      <a class="list-item clickable" href="#/journal" style="color:inherit;text-decoration:none">
        <div class="grow" style="min-width:0"><div class="title truncate">${e.name === "State of Mind" ? "Mood" : e.name}
          ${e.category === "stateOfMind" && e.value != null ? html` <span class="badge">${moodLabel(e.value)}</span>` : e.value_label ? html` <span class="badge">${e.value_label}</span>` : ""}</div>
          <div class="meta">${fmtDateTime(e.start_date)}</div></div></a>`,
      html`Nothing logged in the last 30 days. <a href="#/journal">Check in</a>`, { href: "#/journal", more: "Journal" }) },
  { id: "workouts", title: "Recent workouts", desc: "Your latest workouts from the iPhone app and wearables", size: "m", on: true, needs: ["workouts"],
    render: (d) => listCard("Recent workouts", "activity", d.workouts.workouts, (w) => html`
      <div class="list-item clickable" data-action="workout" data-id="${w.id}">
        <div class="grow"><div class="title truncate">${w.name}</div><div class="meta truncate">${fmtDateTime(w.start_date)} · ${w.source_name}</div></div>
        <span class="small muted">${[w.distance_m ? fmtMeasure(w.distance_m, "m", "distance_walking_running") : null, duration(w.duration_s)].filter(Boolean).join(" · ")}</span>
      </div>`, "No workouts in the last 30 days.", { href: "#/workouts", more: "All workouts" }) },
  { id: "labs", title: "Out-of-range results", desc: "Your latest lab results that are high or low", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Out of range", "flask", d.summary.flagged_labs, (l) => html`
      <div class="list-item clickable" data-action="lab" data-code="${l.code || ""}">
        <div class="grow"><div class="title">${l.title}</div><div class="meta">${fmtDate(l.effective_at)} · range ${l.ref_text || "—"}</div></div>
        <div class="value">${l.value_text}</div>${flagBadge(l.interpretation)}
      </div>`, "All of your most recent lab results are in range.", { href: "#/trends/labs", more: "Lab trends" }) },
  { id: "medications", title: "Medications", desc: "Medications you're currently taking", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Medications", "pill", d.summary.active_medications, (m) => html`
      <div class="list-item clickable" data-action="record" data-id="${m.id}">
        <div class="grow"><div class="title truncate">${m.title}</div><div class="meta truncate">${m.value_text || ""}</div></div>
        ${m.details?.as_needed ? html`<span class="badge">As needed</span>` : ""}
      </div>`, "No active medications.", { href: "#/records/medications" }) },
  { id: "conditions", title: "Active conditions", desc: "Diagnoses from your health records", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Active conditions", "stethoscope", d.summary.active_conditions, (c) => html`
      <div class="list-item clickable" data-action="record" data-id="${c.id}">
        <div class="grow"><div class="title">${c.title}</div><div class="meta">Since ${fmtDate(c.effective_at)} · ${sourcesLabel(c)}</div></div>
        ${c.details?.icd10 ? html`<span class="badge mono">${c.details.icd10}</span>` : ""}
      </div>`, "No active conditions on record.", { href: "#/records/conditions" }) },
  { id: "allergies", title: "Allergies", desc: "Allergies and intolerances", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Allergies", "alert", d.summary.allergies, (a) => html`
      <div class="list-item clickable" data-action="record" data-id="${a.id}">
        <div class="grow"><div class="title">${a.title}</div><div class="meta">${a.narrative || ""}</div></div>
        ${a.details?.criticality === "high" ? html`<span class="badge bad">High risk</span>` : html`<span class="badge">${a.details?.category || "allergy"}</span>`}
      </div>`, d.summary.allergy_note || "No allergies on record.", { href: "#/records/allergies" }) },
  { id: "vitals", title: "Latest vitals", desc: "Blood pressure, weight and other measurements from your visits", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Latest vitals", "heart", d.summary.latest_vitals, (v) => html`
      <div class="list-item clickable" data-action="vital" data-code="${v.code}">
        <div class="grow"><div class="title">${v.title}</div><div class="meta">${fmtDate(v.effective_at)}</div></div>
        <div class="value">${recordValue(v)}</div></div>`, "No vitals yet.", { href: "#/trends/vitals", more: "Vital trends" }) },
  { id: "visits", title: "Recent visits", desc: "Appointments and hospital stays", size: "m", on: true, needs: ["summary"], records: true,
    render: (d) => listCard("Recent visits", "building", d.summary.recent_encounters, (e) => html`
      <div class="list-item clickable" data-action="record" data-id="${e.id}">
        <div class="grow"><div class="title truncate">${e.title}</div>
          <div class="meta truncate">${[fmtDate(e.effective_at), (e.details?.clinicians || [])[0], e.source_name].filter(Boolean).join(" · ")}</div></div>
      </div>`, "No visits on record.", { href: "#/timeline", more: "Full timeline" }) },
  { id: "immunizations", title: "Immunizations", desc: "Vaccines on record, newest first", size: "m", on: false, needs: ["summary"], records: true,
    render: (d) => listCard("Immunizations", "syringe", d.summary.immunizations || [], (i) => html`
      <div class="list-item clickable" data-action="record" data-id="${i.id}">
        <div class="grow"><div class="title truncate">${i.title}</div><div class="meta">${fmtDate(i.effective_at)}</div></div></div>`,
      "No immunizations on record.", { href: "#/records/immunizations" }) },
  { id: "sources", title: "Sources", desc: "Each connected source and when it last synced", size: "m", on: false, needs: ["conns"],
    render: (d) => listCard("Sources", "sources", d.connections, (c) => html`
      <a class="list-item clickable" href="#/sources" style="color:inherit;text-decoration:none">
        <span class="dot ${c.status === "active" ? "good" : c.status === "disconnected" ? "" : "warn"}"></span>
        <div class="grow"><div class="title truncate">${c.display_name}</div><div class="meta">${c.status === "needs_reauth" ? "Reconnect needed" : c.status === "error" ? "Sync failed" : `Synced ${fmtAgo(c.last_sync_at)}`}</div></div></a>`,
      "No sources yet.", { href: "#/sources", more: "Manage sources" }) },
];
const BY_ID = new Map(WIDGETS.map((w) => [w.id, w]));
// How wide an item is on the Overview's grid: quarter, half, three quarters or all of it (half or all on a phone).
const SIZES = [["s", "Small"], ["m", "Medium"], ["l", "Large"], ["xl", "Full width"]];
const TILE_SIZES = ["s", "m"];
const isMetric = (id) => id.startsWith("m:");
const MAX_ITEMS = 60;

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
    if (isMetric(s.id)) { push({ id: s.id, visible: s.visible !== false, size: TILE_SIZES.includes(s.size) ? s.size : "s" }); continue; }
    const def = BY_ID.get(s.id);
    if (def) push({ id: s.id, visible: s.visible !== false, size: SIZES.some(([k]) => k === s.size) ? s.size : def.size });
  }
  // New cards marked first (What's changed) go to the top, the rest after what's there.
  const front = [];
  for (const w of WIDGETS) {
    if (seen.has(w.id)) continue;
    if (w.first && saved) { seen.add(w.id); front.push({ id: w.id, visible: w.on, size: w.size }); } else push({ id: w.id, visible: w.on, size: w.size });
  }
  if (!saved) {
    // A new Overview starts with What's changed, then the numbers, then the cards.
    const firsts = out.filter((i) => BY_ID.get(i.id)?.first);
    return [...firsts, ...out.filter((i) => !BY_ID.get(i.id)?.first)];
  }
  return [...front, ...out].slice(0, MAX_ITEMS);
}

// ------------------------------------------------------------------ page
export async function render({ el, state, navigate }) {
  mount(el, loading(4));
  const profile = state.profile;
  // The summary, sources and the default numbers are needed whatever the layout, so they load alongside it.
  const early = { summary: get("/api/summary", { profile: profile.id }), conns: get("/api/connections", { profile: profile.id }),
    bio: get("/api/biometrics/overview", { profile: profile.id, brief: true }) };
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
      insights: () => needs.has("insights") ? get("/api/insights", { profile: profile.id }).catch(() => ({ insights: [] })) : null,
      // Goals also show on the number tiles, so they load whenever there are any.
      goals: () => get("/api/goals", { profile: profile.id }).catch(() => ({ progress: [] })),
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

  const itemControls = (w) => html`<div class="dash-controls">
    <button class="dash-handle" data-handle aria-label="Move ${title(w)}. Use the arrow keys to move it.">${icon("grip")}</button>
    <div class="dash-sizes" role="group" aria-label="Size of ${title(w)}">${SIZES.filter(([k]) => !isMetric(w.id) || TILE_SIZES.includes(k)).map(([k, label]) => html`
      <button class="${w.size === k ? "active" : ""}" data-action="resize" data-id="${w.id}" data-size="${k}" aria-pressed="${w.size === k}" title="${label}">${k.toUpperCase()}</button>`)}</div>
    <button class="dash-remove" data-action="remove" data-id="${w.id}" aria-label="Remove ${title(w)}" title="Remove">${icon("x")}</button>
  </div>`;

  const draw = () => {
    const { summary, bio, connections } = data;
    refreshAlerts(profile, state.status, { summary, connections });
    const conflict = summary.identity_conflict || [];
    const identity = conflict.length ? null : summary.identities[0];   // don't guess age/sex from someone else's chart
    const years = age(profile.birth_date || identity?.birth_date);
    const first = profile.name.split(" ")[0];
    data.hasRecords = Object.values(summary.counts || {}).some((n) => n > 0);

    if (!connections.length && !bio.has_samples) {
      mount(el, html`
        <div class="page-head"><div><h1>${greeting()}, ${first}</h1>
          <p>Let's bring your health data together. Everything is stored locally on this machine.</p></div></div>
        <div class="grid grid-auto">
          ${[
            ["watch", "Wearables and iPhone", "Pair the Syntropy iPhone app for Apple Health, or link Oura, WHOOP or Google Health for sleep, recovery, HRV and training.", "#/sources", "Connect a device"],
            ["building", "Medical records", "Bring in your labs, medications, conditions and visits from patient portals like Epic MyChart, Oracle Health, athenahealth and the VA.", "#/sources?add=ehr", "Find my health system"],
            ["upload", "Import files", "Drop in an Apple Health export or any FHIR record file you downloaded from a portal.", "#/sources?add=import", "Import a file"],
            ["shieldCheck", "Private by design", "No cloud account. Tokens are encrypted at rest and every access is recorded in your access log.", "#/settings/security", "Review security"],
          ].map(([ic, t, text, href, cta]) => html`
            <div class="card card-pad stack" style="gap:10px">
              <div class="row"><span class="src-avatar">${icon(ic)}</span><h2>${t}</h2></div>
              <p class="muted">${text}</p>
              <div><a class="btn" href="${href}">${cta} ${icon("arrowRight")}</a></div>
            </div>`)}
        </div>`);
      return;
    }

    const issues = connections.filter((c) => c.status === "needs_reauth" || c.status === "error");
    const syncing = connections.some((c) => c.syncing);
    const lastSync = connections.map((c) => c.last_sync_at).filter(Boolean).sort().pop();
    const items = layout.filter((w) => w.visible).map((w) => ({ w, out: content(w) })).filter((x) => x.out);
    mount(el, html`
      <div class="page-head">
        <div><h1>${greeting()}, ${first}</h1>
          <p>${[years != null ? `${years} years` : null, identity?.gender ? identity.gender.charAt(0).toUpperCase() + identity.gender.slice(1) : null].filter(Boolean).join(" · ")}</p></div>
        <div class="row wrap" style="gap:8px">
          ${editing ? html`<button class="btn btn-sm btn-ghost" data-action="reset">Reset</button>
              <button class="btn btn-sm" data-action="add">${icon("plus")} Add</button>
              <button class="btn btn-sm btn-primary" data-action="done">${icon("check")} Done</button>`
            : html`<a class="sync-pill" href="#/sources"><span class="dot ${syncing ? "info pulse" : issues.length ? "warn" : "good"}"></span>
              ${syncing ? "Syncing…" : issues.length ? `${plural(issues.length, "source")} need${issues.length === 1 ? "s" : ""} attention`
                : `${plural(connections.length, "source")} · synced ${fmtAgo(lastSync)}`}</a>
              <button class="btn btn-sm" data-action="edit">${icon("sliders")} Edit</button>`}
        </div>
      </div>
      ${editing ? html`<div class="banner dash-hint"><div class="grow">Drag items by ${icon("grip")} to move them, choose a size, or remove them.
        Add brings back cards and numbers.</div></div>` : ""}
      ${items.length ? html`<div class="dash ${editing ? "editing" : ""}" id="dash">${items.map(({ w, out }) => html`
          <div class="dash-item size-${w.size} ${isMetric(w.id) ? "is-tile" : ""}" data-id="${w.id}">
            <div class="dash-inner">${editing ? itemControls(w) : ""}${out}</div></div>`)}</div>`
        : html`<div class="card">${emptyState("Nothing to show", "Every card is hidden.", html`<button class="btn btn-primary" data-action="add">${icon("plus")} Add cards</button>`)}</div>`}`);
    stopCharts.forEach((stop) => stop());
    stopCharts = items.map(({ w }) => (!isMetric(w.id) && BY_ID.get(w.id).draw?.(el, data))).filter(Boolean);
    const grid = $("#dash", el);
    if (grid) {
      stopCharts.push(masonry(grid));
      if (editing) sortableGrid(grid, (ids) => reorder(ids));
    }
  };

  let saveTimer = null;
  let customized = Boolean(saved.widgets);
  const save = () => {
    customized = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => put(`/api/profiles/${profile.id}/overview`,
      { widgets: layout.map(({ id, visible, size }) => ({ id, visible, size })) })
      .catch((err) => toast(`Couldn't save the layout: ${err.message}`, "bad")), 400);
  };
  const apply = async (next, { reload = false } = {}) => {
    layout = next;
    save();
    if (reload) await load();
    draw();
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

  const off = onAction(el, {
    record: ({ id }) => openRecord(id),
    lab: ({ code }) => navigate(`#/trends/labs?code=${encodeURIComponent(code)}`),
    vital: ({ code }) => navigate(`#/trends/vitals?code=${encodeURIComponent(code)}`),
    metric: ({ metric }) => navigate(`#/trends?metric=${encodeURIComponent(metric)}`),
    workout: ({ id }) => openWorkout(profile, id),
    "hide-insight": ({ id }) => { hideInsight(id); draw(); },
    edit: () => { editing = true; draw(); },
    done: () => { editing = false; draw(); },
    resize: ({ id, size }) => apply(layout.map((w) => (w.id === id ? { ...w, size } : w))),
    remove: ({ id }) => apply(isMetric(id) ? layout.filter((w) => w.id !== id) : layout.map((w) => (w.id === id ? { ...w, visible: false } : w))),
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
      if (customized) await del(`/api/profiles/${profile.id}/overview`);
      customized = false;
      layout = resolveLayout(null, defaultMetrics());
      await load();
      draw();
    },
  });
  return () => { off(); clearTimeout(saveTimer); stopCharts.forEach((stop) => stop()); };
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
    await onAdd(b.dataset.add);
    draw();
  });
}
const GROUP_LABELS = { activity: "Activity", mobility: "Activity", heart: "Heart", readiness: "Recovery", sleep: "Sleep",
  vitals: "Vitals", body: "Body", hearing: "Hearing", nutrition: "Nutrition" };

// ------------------------------------------------------------------ layout mechanics
/**
 * Packs the items like a masonry wall: each spans as many small grid rows as its content is tall, so a number tile sits
 * next to a tall card without stretching, and the dense flow fills the gaps. Returns a function that stops watching.
 */
function masonry(grid) {
  const fit = (item) => {
    const inner = item.firstElementChild;
    item.style.gridRowEnd = `span ${Math.max(1, Math.ceil((inner.getBoundingClientRect().height + 16) / 2))}`;
  };
  const items = $$(".dash-item", grid);
  items.forEach(fit);
  const ro = new ResizeObserver((entries) => entries.forEach((e) => fit(e.target.parentElement)));
  items.forEach((item) => ro.observe(item.firstElementChild));
  return () => ro.disconnect();
}

/**
 * Drag items around the grid by their handle (mouse, touch and pen): the item moves in the page as it's dragged, and
 * a copy follows the pointer. The arrow keys on a handle move its item one place. Calls onReorder(ids) after a move.
 */
function sortableGrid(grid, onReorder) {
  const ids = () => $$(".dash-item", grid).map((x) => x.dataset.id);
  grid.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest("[data-handle]");
    if (!handle || e.button > 0) return;
    e.preventDefault();
    const item = handle.closest(".dash-item");
    const before = ids().join();
    const r = item.getBoundingClientRect();
    const ghost = item.cloneNode(true);
    ghost.className = `${item.className} dash-ghost`;
    Object.assign(ghost.style, { width: `${r.width}px`, height: `${r.height}px`, left: `${r.left}px`, top: `${r.top}px` });
    document.body.appendChild(ghost);
    const dx = e.clientX - r.left, dy = e.clientY - r.top;
    item.classList.add("dragging");
    const pointer = e.pointerId;
    let last = { x: e.clientX, y: e.clientY };
    let raf = null;
    const place = () => {
      ghost.style.left = `${last.x - dx}px`;
      ghost.style.top = `${last.y - dy}px`;
      const target = document.elementsFromPoint(last.x, last.y).map((n) => n.closest?.(".dash-item")).find((n) => n && n !== item && grid.contains(n));
      if (!target) return;
      const t = target.getBoundingClientRect();
      // Before the target when the pointer is in its first half (left half, or top half of a full-width item).
      const first = t.width > grid.clientWidth * 0.9 ? last.y < t.top + t.height / 2 : last.x < t.left + t.width / 2;
      const ref = first ? target : target.nextElementSibling;
      if (ref !== item && item.nextElementSibling !== ref) grid.insertBefore(item, ref);
    };
    // Scrolls the page while dragging near the top or bottom of the window.
    const edgeScroll = () => {
      const edge = last.y < 60 ? -10 : last.y > window.innerHeight - 60 ? 10 : 0;
      if (edge) { pageScroller().scrollBy(0, edge); place(); }
      raf = requestAnimationFrame(edgeScroll);
    };
    raf = requestAnimationFrame(edgeScroll);
    const move = (ev) => { if (ev.pointerId === pointer) { ev.preventDefault(); last = { x: ev.clientX, y: ev.clientY }; place(); } };
    const end = (ev) => {
      if (ev.pointerId !== pointer) return;
      cancelAnimationFrame(raf);
      ghost.remove();
      item.classList.remove("dragging");
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", end);
      document.removeEventListener("pointercancel", end);
      if (ids().join() !== before) onReorder(ids());
    };
    document.addEventListener("pointermove", move, { passive: false });
    document.addEventListener("pointerup", end);
    document.addEventListener("pointercancel", end);
  });
  grid.addEventListener("keydown", (e) => {
    const handle = e.target.closest("[data-handle]");
    const back = e.key === "ArrowUp" || e.key === "ArrowLeft";
    if (!handle || !(back || e.key === "ArrowDown" || e.key === "ArrowRight")) return;
    e.preventDefault();
    const item = handle.closest(".dash-item");
    const sibling = back ? item.previousElementSibling : item.nextElementSibling;
    if (!sibling) return;
    grid.insertBefore(item, back ? sibling : sibling.nextElementSibling);
    handle.focus();
    onReorder(ids());
  });
}
