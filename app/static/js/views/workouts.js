// Workouts from the iPhone app (Apple Watch and iPhone, with routes and heart rate) and from Oura and WHOOP: how much
// you train week by week, whether this week's load is a jump from your usual, and for one kind of workout how pace
// and heart rate are changing and what your best efforts are. Daily activity charts are in Trends.

import { get } from "../api.js";
import { barChart, lineChart, responsive } from "../charts.js";
import { emptyState, esc, fmtDate, fmtDateTime, fmtNum, html, icon, loading, modal, mount, onAction, plural, raw } from "../ui.js";
import { convert, displayUnit, fmtMeasure, unitSystem } from "../units.js";
import { rangeHash, rangeLabel, rangePicker, rangeQuery, readRange, saveRange, wireRange } from "../range.js";

const RANGE_KEY = "syntropy-workouts-range";
const PAGE = 100;

export function duration(seconds) {
  if (seconds == null) return "—";
  const m = Math.round(seconds / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m} min`;
}

const distance = (m) => fmtMeasure(m, "m", "distance_walking_running");
const energy = (w) => w.active_energy_kcal ?? w.total_energy_kcal;
const monthKey = (iso) => { const d = new Date(iso); return `${d.getFullYear()}-${d.getMonth()}`; };
const monthLabel = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "long", year: "numeric" });

// ------------------------------------------------------------------ kinds of workout
const KIND_ICONS = [
  [/run|jog/i, "run"], [/cycl|bik|spin/i, "bike"], [/walk/i, "walk"], [/hik|climb/i, "mountain"], [/swim|water/i, "swim"],
  [/strength|weight|lift|functional|core|cross/i, "dumbbell"], [/yoga|pilates|stretch|flexib|mind|barre/i, "yoga"],
  [/hiit|interval|boxing|kickbox|martial|cardio|dance|jump/i, "flame"],
];
export const workoutIcon = (name) => KIND_ICONS.find(([re]) => re.test(name || ""))?.[1] || "activity";
const isRide = (name) => /cycl|bik|spin/i.test(name || "");
const isSwim = (name) => /swim/i.test(name || "");

/** Pace or speed the way each sport talks about it: min/km or min/mi for running and walking, km/h or mph for
 * cycling, per 100 m (or 100 yd) for swimming. */
export function paceText(name, secPerKm) {
  if (!secPerKm) return null;
  const us = unitSystem() === "us";
  if (isRide(name)) {
    const kmh = 3600 / secPerKm;
    return us ? `${fmtNum(kmh / 1.609344, 1)} mph` : `${fmtNum(kmh, 1)} km/h`;
  }
  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
  if (isSwim(name)) return us ? `${mmss(secPerKm / 10 * 0.9144)} /100 yd` : `${mmss(secPerKm / 10)} /100 m`;
  return us ? `${mmss(secPerKm * 1.609344)} /mi` : `${mmss(secPerKm)} /km`;
}
/** Pace as a number for charts (minutes per km/mi; km/h or mph for rides; lower is faster except for rides). */
function paceValue(name, secPerKm) {
  const us = unitSystem() === "us";
  if (isRide(name)) return us ? 3600 / secPerKm / 1.609344 : 3600 / secPerKm;
  return (us ? secPerKm * 1.609344 : secPerKm) / 60;
}
const paceUnit = (name) => (isRide(name) ? (unitSystem() === "us" ? "mph" : "km/h") : unitSystem() === "us" ? "min/mi" : "min/km");

const LOAD_WORDS = {
  spike: ["Big jump", "attention", "This week's load is well above your usual. Jumps like this raise the risk of injury: build up over a few weeks."],
  building: ["Building", "info", "A bit more than your usual. A steady build like this is how fitness grows."],
  steady: ["Steady", "good", "In line with your last four weeks."],
  easing: ["Easing off", "info", "Lighter than your usual: good for recovery, or a sign you've been busy."],
  new: ["Getting started", "info", "Not enough recent weeks to compare with yet."],
  none: ["No training yet", "info", "Workouts from the last five weeks will show here."],
};

export async function render({ el, params, state }) {
  const profile = state.profile;
  let range = readRange(params, RANGE_KEY, "90");
  let type = params.get("type") || "";
  let limit = PAGE;
  let res = null, tr = null;
  let progressBy = "";
  let stops = [];

  const load = () => Promise.all([
    get("/api/biometrics/workouts", { profile: profile.id, ...rangeQuery(range), type: type || undefined, limit }),
    get("/api/biometrics/training", { profile: profile.id, weeks: 12, type: type || undefined }),
  ]);

  const syncAddress = () => {
    const q = [rangeHash(range), type ? `type=${encodeURIComponent(type)}` : ""].filter(Boolean).join("&");
    history.replaceState(null, "", `#/workouts${q ? `?${q}` : ""}`);
  };

  const head = html`<div class="page-head"><div><h1>Workouts</h1>
    <p>How much you train, how hard, and how it's going. From the iPhone app, your Apple Watch, Oura and WHOOP; daily activity is in <a href="#/trends/activity">Trends</a>.</p></div></div>`;

  const draw = () => {
    stops.forEach((f) => f()); stops = [];
    const { workouts, summary: s, types } = res;
    const byMonth = [];
    for (const w of workouts) {
      const k = monthKey(w.start_date);
      if (byMonth.at(-1)?.key !== k) byMonth.push({ key: k, label: monthLabel(w.start_date), items: [] });
      byMonth.at(-1).items.push(w);
    }
    const maxCount = Math.max(1, ...s.by_type.map((t) => t.count));
    const anyInRange = types.length > 0;
    const anyEver = anyInRange || tr.weeks.some((wk) => wk.count) || (tr.progress || []).length;
    const [loadWord, loadTone, loadText] = LOAD_WORDS[tr.load.status] || LOAD_WORDS.none;
    const thisWeek = tr.weeks[tr.weeks.length - 1];
    const zoneTotal = thisWeek.zones.reduce((a, b) => a + b, 0);
    const progress = tr.progress || [];
    const hasPace = progress.some((p) => p.pace_s_per_km);
    const hasDist = progress.some((p) => p.distance_m);
    const hasHr = progress.some((p) => p.avg_hr);
    const choices = [hasPace && ["pace", isRide(type) ? "Speed" : "Pace"], hasDist && ["distance", "Distance"], ["duration", "Time"], hasHr && ["hr", "Heart rate"]].filter(Boolean);
    if (!choices.some(([k]) => k === progressBy)) progressBy = choices[0][0];

    mount(el, html`${head}
      <div class="toolbar">
        ${rangePicker(range)}
        ${anyInRange || type ? html`<select class="input toolbar-select" data-type-filter aria-label="Kind of workout">
          <option value="">All workouts</option>
          ${types.map((t) => html`<option value="${t.name}" ${t.name === type ? "selected" : ""}>${t.name} (${t.count})</option>`)}
          ${type && !types.some((t) => t.name === type) ? html`<option value="${type}" selected>${type} (0)</option>` : ""}
        </select>` : ""}
      </div>
      ${anyEver ? html`
      <div class="tiles wk-tiles" style="margin-bottom:16px">
        ${[["Workouts", fmtNum(s.count, 0), ""], ["Time", duration(s.duration_s), ""],
           ["Distance", fmtMeasure(s.distance_m || 0, "m", "distance_walking_running", { withUnit: false, digits: 0 }), displayUnit("m", "distance_walking_running")],
           ["Active energy", fmtNum(s.energy_kcal, 0), "kcal"]].map(([k, v, u]) => html`<div class="card tile"><div class="k"><span>${k}</span></div>
             <div class="v">${v}<small>${u}</small></div><div class="s">${rangeLabel(range)}${type ? ` · ${type}` : ""}</div></div>`)}
      </div>
      <div class="wk-top">
        <div class="card card-pad wk-load">
          <div class="card-head flush"><h3>${icon("flame")} Training load</h3><span class="small muted">last 7 days</span></div>
          <div class="wk-load-head"><span class="badge ${loadTone === "attention" ? "warn" : loadTone === "good" ? "good" : ""}">${loadWord}</span>
            ${tr.load.ratio ? html`<span class="small muted">${fmtNum(tr.load.ratio, 2)}× your usual week</span>` : ""}</div>
          <div class="wk-load-nums">
            <div><span>This week</span><b>${fmtNum(tr.load.acute, 0)}</b></div>
            <div><span>Usual week</span><b>${tr.load.chronic ? fmtNum(tr.load.chronic, 0) : "—"}</b></div>
          </div>
          ${tr.load.chronic ? html`<div class="wk-meter" role="img" aria-label="This week's load against your usual">
            <span class="wk-meter-fill ${loadTone}" style="width:${Math.min(100, (tr.load.acute / (tr.load.chronic * 2)) * 100)}%"></span>
            <span class="wk-meter-usual" style="left:50%" title="Your usual week"></span></div>` : ""}
          <p class="small muted" style="margin:10px 0 0">${loadText}</p>
          ${zoneTotal ? html`<div class="wk-zones-title small">Heart-rate zones this week</div>
            <div class="zone-bar" role="img" aria-label="Minutes in each heart-rate zone this week">${thisWeek.zones.map((m, i) => (m ? html`<span class="z${i + 1}" style="flex:${m}" title="${tr.zones[i].name}: ${Math.round(m)} min"></span>` : ""))}</div>
            <div class="zone-legend tiny">${tr.zones.map((z, i) => html`<span><i class="z${i + 1}"></i>${z.name} ${Math.round(thisWeek.zones[i])}m</span>`)}</div>` : ""}
          <p class="tiny faint" style="margin:8px 0 0">Load is workout minutes weighted by heart-rate zone (max ${tr.max_hr.bpm} bpm, ${tr.max_hr.basis}).</p>
        </div>
        <div class="card card-pad">
          <div class="card-head flush"><h3>${icon("activity")} Weekly volume${type ? ` · ${type}` : ""}</h3><span class="small muted">last 12 weeks</span></div>
          <div data-chart="weeks"></div>
        </div>
      </div>
      ${type && progress.length ? html`<div class="wk-top flip">
        <div class="card card-pad">
          <div class="row wrap between" style="gap:10px;margin-bottom:8px"><h3 style="margin:0">${icon("trends")} ${type} over time</h3>
            ${choices.length > 1 ? html`<div class="segmented" role="group" aria-label="Measure">${choices.map(([k, label]) =>
              html`<button class="${k === progressBy ? "active" : ""}" data-action="progress" data-by="${k}" aria-pressed="${k === progressBy}">${label}</button>`)}</div>` : ""}</div>
          <div data-chart="progress"></div>
          ${progressBy === "pace" && !isRide(type) ? html`<p class="tiny faint" style="margin:6px 0 0">Lower is faster.</p>` : ""}
        </div>
        ${tr.bests?.length ? html`<div class="card card-pad">
          <div class="card-head flush"><h3>${icon("trophy")} Best efforts</h3><span class="small muted">all time</span></div>
          <div class="wk-bests">${tr.bests.map((b) => html`<button class="wk-best" data-action="workout" data-id="${b.id}">
            <span class="small muted">${b.label}</span>
            <b>${b.kind === "distance" ? distance(b.distance_m) : b.kind === "duration" ? duration(b.duration_s)
              : b.kind === "speed" ? paceText(type, b.pace_s_per_km) : `${fmtNum(b.energy_kcal, 0)} kcal`}</b>
            <span class="tiny faint">${fmtDate(b.t)}</span></button>`)}</div>
        </div>` : ""}
      </div>` : ""}` : ""}
      ${workouts.length ? html`<div class="wk-layout">
        <div class="card">${byMonth.map((m) => html`
          <div class="list-head"><b>${m.label}</b><span>${plural(m.items.length, "workout")} · ${duration(m.items.reduce((t, w) => t + (w.duration_s || 0), 0))}</span></div>
          <div class="list">${m.items.map(workoutRow)}</div>`)}
          ${workouts.length >= limit ? html`<div class="card-body row between small muted"><span>Showing the latest ${fmtNum(workouts.length, 0)}</span>
            <button class="btn btn-sm" data-action="more">Show more</button></div>` : ""}
        </div>
        ${!type && s.by_type.length > 1 ? html`<div class="card wk-types">
          <div class="card-head"><h3>By kind</h3><span class="small muted">${rangeLabel(range)}</span></div>
          <div class="list">${s.by_type.map((t) => html`
            <button class="list-item clickable wk-type" data-action="type" data-name="${t.name}">
              <span class="ev-icon">${icon(workoutIcon(t.name))}</span>
              <div class="grow" style="min-width:0"><div class="row between"><span class="title truncate">${t.name}</span><b class="small">${t.count}</b></div>
                <div class="wk-bar"><span style="width:${(t.count / maxCount) * 100}%"></span></div>
                <div class="meta">${duration(t.duration_s)}${t.distance_m ? ` · ${distance(t.distance_m)}` : ""}</div></div>
            </button>`)}</div></div>` : ""}
      </div>`
      : html`<div class="card">${anyInRange || type
        ? emptyState("No workouts in this range", "Try a longer range, or all kinds of workouts.")
        : emptyState("No workouts yet", "Workouts arrive from the Syntropy phone apps (Apple Health on iPhone, Health Connect on Android) and from Oura or WHOOP. A first sync of a phone's full history can take a while; workouts are sent early.",
          html`<a class="btn btn-primary" href="#/sources">${icon("plus")} Add a source</a>`)}</div>`}`);

    const weeksEl = el.querySelector("[data-chart=weeks]");
    if (weeksEl) {
      const pts = tr.weeks.map((wk) => {
        const d = new Date(`${wk.week}T12:00:00`);
        return { day: wk.week, value: wk.minutes, tickLabel: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
          tipLabel: `Week of ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`,
          source: [plural(wk.count, "workout"), wk.distance_m ? distance(wk.distance_m) : null, `load ${fmtNum(wk.load, 0)}`].filter(Boolean).join(" · "),
          partial: wk === tr.weeks[tr.weeks.length - 1] };
      });
      const avg = pts.slice(0, -1).filter((p) => p.value).reduce((a, p, _, arr) => a + p.value / arr.length, 0);
      stops.push(responsive(weeksEl, () => barChart(weeksEl, pts, { unit: "min", label: "Workout minutes each week", format: (v) => duration(v * 60), avg: avg || null, height: 200 })));
    }
    const progEl = el.querySelector("[data-chart=progress]");
    if (progEl) {
      const pick = {
        pace: (p) => p.pace_s_per_km && paceValue(type, p.pace_s_per_km),
        distance: (p) => p.distance_m && convert(p.distance_m, "m", "distance_walking_running").value,
        duration: (p) => p.duration_s / 60,
        hr: (p) => p.avg_hr,
      }[progressBy];
      const unit = { pace: paceUnit(type), distance: displayUnit("m", "distance_walking_running"), duration: "min", hr: "bpm" }[progressBy];
      const pts = progress.map((p) => ({ t: p.t, v: pick(p) })).filter((p) => p.v);
      stops.push(responsive(progEl, () => lineChart(progEl, [{ name: type, points: pts }], { unit, label: `${type} ${progressBy}`, height: 220, digits: 2 })));
    }
  };

  mount(el, html`${head}${loading(3)}`);
  [res, tr] = await load();
  draw();

  let seq = 0;
  const reload = async () => {
    syncAddress();
    const n = ++seq;
    const next = await load();
    if (n !== seq) return;   // a newer choice is already loading
    [res, tr] = next;
    draw();
  };
  const onRange = wireRange(el, () => range, (r) => { range = r; limit = PAGE; saveRange(range, RANGE_KEY); return reload(); });
  el.addEventListener("change", (e) => {
    if (!e.target.matches("[data-type-filter]")) return;
    type = e.target.value;
    limit = PAGE;
    reload();
  });

  const off = onAction(el, {
    range: onRange,
    type: ({ name }) => { type = name; limit = PAGE; return reload(); },
    more: () => { limit += PAGE; return reload(); },
    workout: ({ id }) => openWorkout(profile, id),
    progress: ({ by }) => { progressBy = by; draw(); },
  });
  return () => { off(); stops.forEach((f) => f()); };
}

function workoutRow(w) {
  const pace = w.distance_m >= 200 && w.duration_s ? paceText(w.name, w.duration_s / (w.distance_m / 1000)) : null;
  const stats = [w.distance_m ? distance(w.distance_m) : null, pace, energy(w) ? `${fmtNum(energy(w), 0)} kcal` : null,
    w.avg_hr ? `${fmtNum(w.avg_hr, 0)} bpm` : null].filter(Boolean).join(" · ");
  const also = w.also_recorded_by?.length ? ` · also ${w.also_recorded_by.map((d) => d.source_name).join(", ")}` : "";
  return html`<button class="list-item clickable wk-row" data-action="workout" data-id="${w.id}">
    <span class="ev-icon">${icon(workoutIcon(w.name))}</span>
    <div class="grow" style="min-width:0"><div class="title truncate">${w.name}</div>
      <div class="meta truncate">${fmtDateTime(w.start_date)} · ${w.source_name}${also}</div></div>
    ${stats ? html`<span class="small muted hide-narrow">${stats}</span>` : ""}
    <b class="small wk-duration">${duration(w.duration_s)}</b>
    ${w.has_route ? html`<span class="badge hide-narrow">Route</span>` : ""}</button>`;
}

/** A workout's details (route map, heart rate, zones, splits) in a side panel. */
export async function openWorkout(profile, id) {
  const m = modal({ title: "Workout", body: loading(3), drawer: true });
  const w = await get(`/api/biometrics/workouts/${id}`, { profile: profile.id, split: unitSystem() === "us" ? "mi" : "km" });
  const pace = paceText(w.name, w.pace_s_per_km);
  const stats = [["Duration", duration(w.duration_s)], ["Distance", w.distance_m ? distance(w.distance_m) : null],
    [isRide(w.name) ? "Average speed" : "Average pace", pace],
    ["Active energy", w.active_energy_kcal ? `${fmtNum(w.active_energy_kcal, 0)} kcal` : null],
    ["Total energy", w.total_energy_kcal ? `${fmtNum(w.total_energy_kcal, 0)} kcal` : null],
    ["Avg heart rate", w.avg_hr ? `${fmtNum(w.avg_hr, 0)} bpm` : null], ["Max heart rate", w.max_hr ? `${fmtNum(w.max_hr, 0)} bpm` : null],
    ["Training load", w.load ? fmtNum(w.load, 0) : null],
    ["Elevation gain", w.elevation_ascent_m ? fmtMeasure(w.elevation_ascent_m, "m", "elevation") : null],
    ["Steps", w.step_count ? fmtNum(w.step_count, 0) : null],
    ["Temperature", w.temperature_c != null ? fmtMeasure(w.temperature_c, "°C", "", { digits: 0 }) : null],
    ["Location", w.indoor == null ? null : w.indoor ? "Indoor" : "Outdoor"], ["Source", w.source_name]].filter(([, v]) => v);
  const zoneMax = Math.max(1, ...(w.zones || []).map((z) => z.minutes));
  const zoneTotal = (w.zones || []).reduce((a, z) => a + z.minutes, 0);
  const splitUnit = unitSystem() === "us" ? "mi" : "km";
  const fastest = Math.min(...(w.splits || []).filter((s) => !s.partial).map((s) => s.seconds));
  m.setBody(html`<div class="stack">
    <div class="row" style="gap:12px"><span class="ev-icon lg">${icon(workoutIcon(w.name))}</span>
      <div><h2 style="margin:0">${w.name}</h2><div class="small muted">${fmtDateTime(w.start_date)}</div></div></div>
    ${w.route.length > 1 ? routeSvg(w.route) : ""}
    <div class="grid grid-2">${stats.map(([k, v]) => html`<div><div class="small muted">${k}</div><b>${v}</b></div>`)}</div>
    ${w.heart_rate.length ? html`<div><h3>Heart rate</h3><div id="wk-hr"></div></div>` : ""}
    ${zoneTotal ? html`<div><h3>Heart-rate zones</h3>
      <div class="zone-rows">${w.zones.map((z, i) => html`<div class="zone-row">
        <span class="small">${z.name} <span class="faint">${z.from}–${z.to} bpm</span></span>
        <span class="zone-track"><span class="z${i + 1}" style="width:${(z.minutes / zoneMax) * 100}%"></span></span>
        <b class="small">${Math.round(z.minutes)} min</b></div>`)}</div>
      <p class="tiny faint" style="margin:6px 0 0">Zones are shares of a maximum heart rate of ${w.max_hr_basis.bpm} bpm (${w.max_hr_basis.basis}).
        ${w.heart_rate.length ? "" : "This workout only has an average heart rate, so all of it counts in one zone."}</p></div>` : ""}
    ${w.splits?.length ? html`<div><h3>Splits</h3><table class="table"><thead><tr><th>${splitUnit}</th><th class="num">Time</th><th>${isRide(w.name) ? "Speed" : "Pace"}</th></tr></thead>
      <tbody>${w.splits.map((s) => html`<tr><td>${s.partial ? fmtMeasure(s.distance_m, "m", "distance_walking_running") : s.n}</td>
        <td class="num">${duration(s.seconds).replace(" min", "m")}${s.seconds === fastest ? html` <span class="badge good">Fastest</span>` : ""}</td>
        <td>${paceText(w.name, s.seconds / (s.distance_m / 1000))}</td></tr>`)}</tbody></table></div>` : ""}
  </div>`);
  if (w.heart_rate.length) {
    lineChart(m.el.querySelector("#wk-hr"), [{ name: "Heart rate", points: w.heart_rate.map((p) => ({ t: p.t, v: p.avg })) }],
      { unit: "bpm", label: "Heart rate", height: 180 });
  }
}

/** Route as an SVG polyline (equirectangular projection; plenty for a workout-sized area). */
function routeSvg(route) {
  const lats = route.map((p) => p.lat), lons = route.map((p) => p.lon);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats), minLon = Math.min(...lons), maxLon = Math.max(...lons);
  const k = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
  const w = 480, h = 260, pad = 12;
  const spanX = Math.max((maxLon - minLon) * k, 1e-6), spanY = Math.max(maxLat - minLat, 1e-6);
  const scale = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanY);
  const pts = route.map((p) => `${(pad + (p.lon - minLon) * k * scale).toFixed(1)},${(h - pad - (p.lat - minLat) * scale).toFixed(1)}`).join(" ");
  const [sx, sy] = pts.split(" ")[0].split(","), [ex, ey] = pts.split(" ").slice(-1)[0].split(",");
  return raw(`<svg viewBox="0 0 ${w} ${h}" style="width:100%;background:var(--surface-2, var(--surface));border-radius:12px" role="img" aria-label="Workout route">
    <polyline points="${esc(pts)}" fill="none" stroke="var(--accent)" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${sx}" cy="${sy}" r="5" fill="#16a34a"/><circle cx="${ex}" cy="${ey}" r="5" fill="#dc2626"/></svg>`);
}
