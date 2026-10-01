// Pieces the Trends charts share: long ranges folded into weeks or months, goals, events from health records
// (medications started, diagnoses) marked on charts, and comparing one measure with another day by day.

import { api, get } from "../api.js";
import { barChart, lineChart, responsive, scatterChart } from "../charts.js";
import { hoursText, metricUnit, metricValue } from "./overview.js";
import { convert, displayUnit } from "../units.js";
import { fmtDate, fmtNum, html, icon, modal, mount, toast } from "../ui.js";

// Periods, the same words as Workouts and the Journal. "All" asks for ten years; the chart starts at the first day.
export const RANGES = [[7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "1 year"], [3650, "All"]];
const DAYS_KEY = "syntropy-trends-days";
export function savedDays() {
  try { const d = Number(localStorage.getItem(DAYS_KEY)); return RANGES.some(([k]) => k === d) ? d : 30; } catch { return 30; }
}
export function saveDays(d) { try { localStorage.setItem(DAYS_KEY, String(d)); } catch {} }

export const rangeButtons = (days) => html`<div class="segmented" role="group" aria-label="Period">${RANGES.map(([d, label]) =>
  html`<button class="${d === days ? "active" : ""}" data-action="range" data-days="${d}" aria-pressed="${d === days}">${label}</button>`)}</div>`;

// ------------------------------------------------------------------ weeks and months
const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function weekStart(day) {
  const d = new Date(`${day}T12:00:00`);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return iso(d);
}

/**
 * Daily points folded into weekly or monthly averages when there are too many days to draw as bars (more than about
 * four months). Returns { points, size: "day" | "week" | "month" }. Parts (sleep stages) are averaged the same way.
 */
export function bucket(points, days) {
  const complete = points.filter((p) => !p.partial);
  if (points.length <= 120) return { points, size: "day" };
  const size = days <= 800 ? "week" : "month";
  const groups = new Map();
  for (const p of complete) {
    const k = size === "week" ? weekStart(p.day) : `${p.day.slice(0, 7)}-01`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(p);
  }
  const out = [...groups.entries()].map(([k, list]) => {
    const avg = (f) => list.reduce((a, p) => a + f(p), 0) / list.length;
    const d = new Date(`${k}T12:00:00`);
    const pt = {
      day: k, value: avg((p) => p.value), count: list.length,
      tipLabel: size === "week" ? `Week of ${d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`
        : d.toLocaleDateString(undefined, { month: "long", year: "numeric" }),
      tickLabel: size === "week" ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
        : d.toLocaleDateString(undefined, { month: "short", year: "2-digit" }),
    };
    if (list[0].parts) pt.parts = Object.fromEntries(Object.keys(list[0].parts).map((key) => [key, avg((p) => p.parts[key] || 0)]));
    return pt;
  });
  return { points: out, size };
}

export const bucketWord = (size) => (size === "week" ? "Weekly average" : size === "month" ? "Monthly average" : null);

// ------------------------------------------------------------------ units for any measure
/**
 * How to show one measure: `conv` turns a stored value into the number drawn, `text` into words with the unit, and
 * `unit` is the axis unit. Works for wearable metrics, the Journal's check-in scales and workout totals.
 */
export function measureFormat(meta) {
  const key = meta.key || meta.metric;
  if (key === "journal:mood") return { conv: (v) => (v + 1) * 2 + 1, text: (v) => `${fmtNum((v + 1) * 2 + 1, 1)} of 5`, unit: "of 5" };
  if (key?.startsWith("journal:")) return { conv: (v) => v, text: (v) => `${fmtNum(v, 1)} of 5`, unit: "of 5" };
  if (key === "workouts:minutes") return { conv: (v) => v, text: (v) => `${fmtNum(v, 0)} min`, unit: "min" };
  if (key === "workouts:load") return { conv: (v) => v, text: (v) => fmtNum(v, 0), unit: "load" };
  const m = { metric: key, unit: meta.unit };
  const raw = meta.unit === "h" || meta.unit === "score";
  const unit = raw ? meta.unit : displayUnit(meta.unit, key);
  return {
    conv: (v) => (raw || v == null ? v : convert(v, meta.unit, key).value),
    text: (v) => `${metricValue(m, v)}${metricUnit(m) ? ` ${metricUnit(m)}` : ""}`,
    unit,
  };
}

// ------------------------------------------------------------------ goals
let goalCache = null;
export async function loadGoals(profileId, fresh = false) {
  if (!fresh && goalCache?.profileId === profileId) return goalCache.data;
  const data = await get("/api/goals", { profile: profileId }, { fresh: true });
  goalCache = { profileId, data };
  return data;
}

/** "at least 7h 30m", "at most 60 bpm". */
export function goalText(goal, meta) {
  return `${goal.direction === "min" ? "at least" : "at most"} ${measureFormat(meta).text(goal.target)}`;
}

/** Whether a value meets a goal. */
export const meets = (goal, v) => (goal.direction === "min" ? v >= goal.target : v <= goal.target);

/** The dialog to set, change or remove the goal for one metric. Calls `onSaved()` after a change. */
export function openGoal(profile, meta, current, suggested, onSaved) {
  const f = measureFormat(meta);
  const linear = (v) => f.conv(v);
  const back = (shown) => { const a = linear(0) ?? 0, b = linear(1) - a; return (shown - a) / (b || 1); };
  const start = current?.target ?? suggested?.target ?? meta.suggest ?? null;
  const hours = meta.unit === "h";
  const shown = start == null ? "" : hours ? start : Number(fmtNum(linear(start), 2).replace(/,/g, ""));
  const dir = current?.direction || suggested?.direction || "min";
  const m = modal({
    title: `${current ? "Change" : "Set"} a goal: ${meta.label}`,
    body: html`<form id="goal-form" class="stack">
      <div class="field"><span class="label">Each day</span>
        <div class="seg" role="radiogroup" aria-label="Direction">
          <label><input type="radio" name="dir" value="min" ${dir === "min" ? "checked" : ""}><span>At least</span></label>
          <label><input type="radio" name="dir" value="max" ${dir === "max" ? "checked" : ""}><span>At most</span></label></div></div>
      <label class="field"><span class="label">${hours ? "Hours" : `Target${f.unit ? ` (${f.unit})` : ""}`}</span>
        <input class="input" name="target" type="number" inputmode="decimal" step="${hours ? 0.25 : "any"}" min="0" required value="${shown}"
          style="max-width:200px"></label>
      <p class="small muted" style="margin:0">Shown as a line on the chart, and on the Overview with how many recent days met it.</p>
    </form>`,
    footer: html`${current ? html`<button class="btn btn-ghost" data-remove style="margin-right:auto">Remove goal</button>` : ""}
      <button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save goal</button>`,
  });
  const save = async (target) => {
    const form = m.el.querySelector("#goal-form");
    try {
      await api("/api/goals", { method: "PUT", query: { profile: profile.id },
        body: { metric: meta.key || meta.metric, target, direction: new FormData(form).get("dir") || "min" } });
      await loadGoals(profile.id, true);
      m.close();
      toast(target == null ? "Goal removed" : "Goal saved");
      onSaved?.();
    } catch (err) { toast(err.message, "bad"); }
  };
  m.el.querySelector("[data-cancel]").addEventListener("click", () => m.close());
  m.el.querySelector("[data-remove]")?.addEventListener("click", () => save(null));
  m.el.querySelector("[data-save]").addEventListener("click", () => {
    const v = Number(m.el.querySelector("[name=target]").value);
    if (!(v > 0)) { toast("Enter a number above zero.", "bad"); return; }
    save(hours ? v : back(v));
  });
  m.el.querySelector("#goal-form").addEventListener("submit", (e) => { e.preventDefault(); m.el.querySelector("[data-save]").click(); });
}

// ------------------------------------------------------------------ events from health records
let markerCache = null;
export async function loadMarkers(profileId) {
  if (markerCache?.profileId === profileId) return markerCache.list;
  const list = (await get("/api/insights/markers", { profile: profileId }).catch(() => ({ markers: [] }))).markers;
  markerCache = { profileId, list };
  return list;
}
const MARKERS_KEY = "syntropy-trends-markers";
export function markersOn() { try { return localStorage.getItem(MARKERS_KEY) !== "0"; } catch { return true; } }
export function setMarkersOn(on) { try { localStorage.setItem(MARKERS_KEY, on ? "1" : "0"); } catch {} }
/** The markers inside a window (YYYY-MM-DD bounds). */
export const markersIn = (list, start, end) => list.filter((m) => m.t >= start && m.t <= end);
export const markerToggle = (count, on) => (count ? html`<label class="check small" title="Mark when medications started and conditions were diagnosed">
  <input type="checkbox" data-markers ${on ? "checked" : ""}> <span>Medications & diagnoses <span class="faint">(${count})</span></span></label>` : "");
export const markerList = (list) => (list.length ? html`<div class="marker-list small">${list.slice(0, 6).map((m) =>
  html`<span><span class="marker-dot"></span>${fmtDate(m.t)} · ${m.label}</span>`)}${list.length > 6 ? html`<span class="faint">and ${list.length - 6} more</span>` : ""}</div>` : "");

// ------------------------------------------------------------------ compare
let comparableCache = null;
async function comparable(profileId) {
  if (comparableCache?.profileId === profileId) return comparableCache.list;
  const list = (await get("/api/insights/comparable", { profile: profileId })).measures;
  comparableCache = { profileId, list };
  return list;
}
const GROUP_NAMES = { journal: "How you felt (Journal)", workouts: "Workouts", sleep: "Sleep", readiness: "Recovery", heart: "Heart",
  activity: "Activity", vitals: "Vitals", body: "Body", mobility: "Mobility", nutrition: "Nutrition", hearing: "Hearing" };

/** A label inside a sentence: "Steps" → "steps", but "HRV (RMSSD)" and "VO₂ max" stay as they are. */
const soft = (label) => (/^[A-Z][a-z]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label);
const lagWords = (lag) => (lag === 1 ? "the next day" : lag === -1 ? "the day before" : lag > 1 ? `${lag} days later` : lag < -1 ? `${-lag} days before` : "on the same day");

function describe(res) {
  const fa = measureFormat(res.a), fb = measureFormat(res.b);
  const when = lagWords(res.lag);
  if (res.n < 7) return `Only ${res.n} days have both, not enough to compare yet.`;
  const a = soft(res.a.label), b = soft(res.b.label);
  const lead = res.strength === "no clear link" ? `No clear link between ${a} and ${b} ${when}`
    : `A ${res.strength} ${res.r > 0 ? "positive" : "negative"} link (r = ${fmtNum(res.r, 2)}): ${res.r > 0 ? "higher" : "lower"} ${b} ${res.lag ? when : ""} tended to go with higher ${a}`;
  const split = res.split ? ` On days with ${a} above ${fa.text(res.split.cut)}, ${b} ${res.lag ? when : ""} averaged ${fb.text(res.split.above)}, against ${fb.text(res.split.below)} otherwise.` : "";
  return `${lead}, over ${res.n} days.${split}`.replace(/\s+/g, " ");
}

/**
 * The Compare card under a chart: pick another measure, see it over the same days, the two against each other, and a
 * sentence on how they line up. `state` = { b, lag }; `onChange(state)` keeps it in the address.
 */
export async function compareCard(host, profile, a, days, state, onChange) {
  const list = await comparable(profile.id);
  const options = list.filter((m) => m.key !== a.key);
  if (!options.length) { mount(host, ""); return () => {}; }
  const groups = new Map();
  for (const m of options) {
    const g = GROUP_NAMES[m.group] || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(m);
  }
  let stops = [];
  let gen = 0;     // bumped by each redraw and by stop(), so a late answer never draws over a newer one
  const stop = () => { gen++; stops.forEach((f) => f()); stops = []; };
  const draw = async () => {
    stop();
    const mine = gen;
    mount(host, html`<div class="card card-pad compare-card">
      <div class="row wrap between" style="gap:10px">
        <div><h3 style="margin:0">${icon("compare")} Compare</h3><div class="small muted">See how ${soft(a.label)} lines up with anything else you track.</div></div>
        <div class="row wrap" style="gap:8px">
          <select class="input input-sm" data-compare aria-label="Compare with" style="width:auto;max-width:240px">
            <option value="">Compare with…</option>
            ${[...groups.entries()].map(([g, ms]) => html`<optgroup label="${g}">${ms.map((m) =>
              html`<option value="${m.key}" ${m.key === state.b ? "selected" : ""}>${m.label}</option>`)}</optgroup>`)}
          </select>
          ${state.b ? html`<select class="input input-sm" data-lag aria-label="Which day" style="width:auto">
            <option value="0" ${!state.lag ? "selected" : ""}>Same day</option>
            <option value="-1" ${state.lag === -1 ? "selected" : ""}>The day before</option>
            <option value="1" ${state.lag === 1 ? "selected" : ""}>The next day</option></select>
            <button class="btn btn-ghost btn-sm btn-icon" data-clear aria-label="Stop comparing">${icon("x")}</button>` : ""}
        </div></div>
      <div data-compare-body></div></div>`);
    host.querySelector("[data-compare]").addEventListener("change", (e) => { state.b = e.target.value || null; onChange(state); draw(); });
    host.querySelector("[data-lag]")?.addEventListener("change", (e) => { state.lag = Number(e.target.value); onChange(state); draw(); });
    host.querySelector("[data-clear]")?.addEventListener("click", () => { state.b = null; state.lag = 0; onChange(state); draw(); });
    if (!state.b) return;
    const body = host.querySelector("[data-compare-body]");
    mount(body, html`<div class="empty small">Comparing…</div>`);
    let res;
    try {
      res = await get("/api/insights/compare", { profile: profile.id, a: a.key, b: state.b, days: Math.max(7, Math.min(days, 3650)), lag: state.lag || 0 });
    } catch (err) { if (mine === gen) mount(body, html`<div class="banner bad small">${err.message}</div>`); return; }
    if (mine !== gen) return;
    const fa = measureFormat(res.a), fb = measureFormat(res.b);
    mount(body, html`<p class="compare-summary">${describe(res)}</p>
      <div class="compare-grid">
        <div><div class="small muted compare-label">${res.b.label}${res.lag ? ` (${lagWords(res.lag)})` : ""} over the same days</div><div data-chart-b></div></div>
        <div><div class="small muted compare-label">Each day, ${soft(res.a.label)} against ${soft(res.b.label)}</div><div data-chart-xy></div></div>
      </div>
      <p class="tiny faint" style="margin:8px 0 0">A link here is something that lined up in your own data, not proof that one causes the other.</p>`);
    const bEl = body.querySelector("[data-chart-b]"), xyEl = body.querySelector("[data-chart-xy]");
    const bPoints = res.pairs.map((p) => ({ t: p.day, v: fb.conv(p.y) }));
    const bars = res.b.key.startsWith("workouts:");
    stops.push(responsive(bEl, () => (bars
      ? barChart(bEl, res.pairs.map((p) => ({ day: p.day, value: fb.conv(p.y) })), { unit: fb.unit, label: res.b.label, height: 200, format: fb.text })
      : lineChart(bEl, [{ name: res.b.label, points: bPoints }], { unit: fb.unit, label: res.b.label, height: 200 }))));
    stops.push(responsive(xyEl, () => scatterChart(xyEl, res.pairs.map((p) => ({ x: fa.conv(p.x), y: fb.conv(p.y), day: p.day })),
      { xLabel: res.a.label, yLabel: res.b.label, fx: (v) => fmtNum(v), fy: (v) => fmtNum(v), height: 200 })));
  };
  await draw();
  return stop;
}

// ------------------------------------------------------------------ small shared bits
export const clock = (minutesAfterNoon) => {
  if (minutesAfterNoon == null) return "—";
  const m = Math.round(minutesAfterNoon + 720) % 1440;
  const d = new Date(2000, 0, 1, Math.floor(m / 60), m % 60);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};
export const clockFromText = (hhmm) => {
  if (!hhmm) return null;
  const [h, m] = hhmm.split(":").map(Number);
  return ((h * 60 + m - 720) % 1440 + 1440) % 1440;
};
export { hoursText };
