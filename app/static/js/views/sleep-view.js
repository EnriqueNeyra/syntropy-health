// Sleep in Trends: each night split into stages, when you went to bed and got up and how regular that was, the latest
// night stage by stage, and how your nights usually divide up. Figures come from the source the daily sleep numbers
// use, so they match the Overview and everywhere else.

import { get } from "../api.js";
import { barChart, hypnogram, rangeChart, responsive } from "../charts.js";
import { bucket, bucketWord, clock, clockFromText, goalText, hoursText, markerList, markerToggle, meets } from "./trend-tools.js";
import { fmtDate, html, icon, mount, plural } from "../ui.js";

// Bottom to top in each bar: deep, core, REM, then sleep a device didn't break into stages, then time awake.
export const STAGES = [
  { key: "deep", name: "Deep", color: "var(--stage-deep)" },
  { key: "core", name: "Core / light", color: "var(--stage-core)" },
  { key: "rem", name: "REM", color: "var(--stage-rem)" },
  { key: "unstaged", name: "Asleep (no stages)", color: "var(--stage-unstaged)" },
];
const AWAKE = { key: "awake", name: "Awake", color: "var(--stage-awake)" };
// Hypnogram lanes, top to bottom.
const LANES = [AWAKE, STAGES[2], STAGES[1], STAGES[0], { key: "asleep", name: "Asleep", color: "var(--stage-unstaged)" }];
// Typical shares of a night for adults, for context beside the person's own.
const TYPICAL = { deep: "13–23%", rem: "20–25%", core: "about half" };

const pct = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : "—");
const legend = (items) => html`<div class="legend stage-legend">${items.map((s) =>
  html`<span class="key-swatch"><i style="background:${s.color}"></i>${s.name}</span>`)}</div>`;

/**
 * Draws the sleep view into `detail`. ctx: { profile, days, source, goal, markers, markersOn, back, controls }.
 * Returns a function that stops its charts.
 */
export async function drawSleep(detail, ctx, seq, current) {
  const res = await get("/api/biometrics/sleep", { profile: ctx.profile.id, days: ctx.days, source: ctx.source || undefined });
  if (seq !== current()) return () => {};
  const nights = res.nights;
  const st = res.stats;
  const last = nights[nights.length - 1];
  const staged = nights.some((n) => n.stages.deep + n.stages.core + n.stages.rem > 0);
  const stages = staged ? STAGES.filter((s) => s.key !== "unstaged" || nights.some((n) => n.stages.unstaged > 0.05)) : [];
  const daily = nights.map((n) => ({ day: n.day, value: n.asleep_h, source: n.source,
    parts: staged ? Object.fromEntries(stages.map((s) => [s.key, n.stages[s.key] || 0])) : undefined }));
  const { points, size } = bucket(daily, ctx.days);
  const met = ctx.goal ? nights.filter((n) => meets(ctx.goal, n.asleep_h)).length : null;
  const firstDay = nights[0]?.day || res.start;
  const inRangeList = ctx.markers.filter((m) => m.t >= firstDay && m.t <= res.end);
  const inRange = inRangeList.length;
  const marks = ctx.markersOn ? inRangeList : [];

  mount(detail, html`
    ${ctx.back}
    <div class="card card-pad">
      <div class="detail-head">
        <div class="grow"><div class="eyebrow">${bucketWord(size) || "Each night"}</div><h2>Sleep</h2>
          <div class="small muted">${last ? html`${fmtDate(last.day)} · ${clock(last.bed_min)} – ${clock(last.wake_min)} · ${last.source}` : "No nights in this period"}</div></div>
        ${last ? html`<div class="detail-value"><div class="v">${hoursText(last.asleep_h)}</div>
          <div class="small muted">asleep of ${hoursText(last.in_bed_h)} in bed</div></div>` : ""}
      </div>
      ${ctx.controls}
      ${inRange ? html`<div class="row wrap" style="justify-content:flex-end;margin:-4px 0 6px">${markerToggle(inRange, ctx.markersOn)}</div>` : ""}
      ${staged ? legend(stages) : ""}
      <div data-chart="nights"></div>
      <div class="stat-row">
        <div><span>Average asleep</span><b>${st.asleep_h != null ? hoursText(st.asleep_h) : "—"}</b></div>
        <div><span>Time in bed</span><b>${st.in_bed_h != null ? hoursText(st.in_bed_h) : "—"}</b></div>
        <div><span>Efficiency</span><b>${st.efficiency != null ? `${Math.round(st.efficiency * 100)}%` : "—"}</b></div>
        ${ctx.goal ? html`<div><span>Goal met</span><b>${met} of ${nights.length}</b></div>`
          : html`<div><span>Under 6 hours</span><b>${plural(st.short_nights, "night")}</b></div>`}
      </div>
      ${ctx.goal ? html`<p class="small muted" style="margin:10px 0 0">Goal: ${goalText(ctx.goal, { key: "sleep_duration", unit: "h" })} asleep.</p>` : ""}
      ${markerList(marks)}
    </div>

    ${nights.length ? html`<div class="card card-pad">
      <div class="card-head flush"><h3>${icon("moon")} Bedtime and wake time</h3></div>
      <div data-chart="schedule"></div>
      <div class="stat-row">
        <div><span>Usual bedtime</span><b>${clock(clockFromText(st.bedtime))}</b></div>
        <div><span>Bedtime varies by</span><b>${st.bedtime_spread_min != null ? `± ${st.bedtime_spread_min} min` : "—"}</b></div>
        <div><span>Usual wake time</span><b>${clock(clockFromText(st.wake_time))}</b></div>
        <div><span>Wake time varies by</span><b>${st.wake_spread_min != null ? `± ${st.wake_spread_min} min` : "—"}</b></div>
      </div>
      <p class="small muted" style="margin:10px 0 0">${consistency(st)}</p>
    </div>` : ""}

    ${last?.segments?.length ? html`<div class="card card-pad">
      <div class="card-head flush"><h3>Last night, stage by stage</h3><span class="small muted">${fmtDate(last.day)} · ${last.source}</span></div>
      <div data-chart="hypnogram"></div>
      ${stageTable(last.stages, last.asleep_h)}
    </div>` : ""}

    ${staged && st.stages.deep != null ? html`<div class="card card-pad">
      <div class="card-head flush"><h3>How your nights divide up</h3><span class="small muted">average of ${plural(nights.filter((n) => n.stages.deep + n.stages.core + n.stages.rem > 0).length, "night")}</span></div>
      <div class="stage-bar" role="img" aria-label="Average share of each sleep stage">${stages.filter((s) => st.stages[s.key] > 0).map((s) =>
        html`<span style="flex:${st.stages[s.key]};background:${s.color}" title="${s.name} ${hoursText(st.stages[s.key])}"></span>`)}</div>
      ${stageTable(st.stages, st.asleep_h, true)}
      <p class="tiny faint" style="margin:8px 0 0">Adults typically spend about 13–23% of sleep in deep sleep and 20–25% in REM. Wearables estimate stages from movement and heart rate, so treat them as a guide.</p>
    </div>` : ""}`);

  const stops = [];
  const nightsEl = detail.querySelector("[data-chart=nights]");
  stops.push(responsive(nightsEl, () => barChart(nightsEl, points, {
    unit: "", label: "Sleep each night", stack: staged ? stages : null, format: hoursText, tick: (v) => `${v}h`,
    avg: st.asleep_h, goal: ctx.goal?.target, markers: marks,
  })));
  const schedEl = detail.querySelector("[data-chart=schedule]");
  if (schedEl) {
    const rows = nights.slice(-Math.min(nights.length, 90)).map((n) => ({ day: n.day, from: n.bed_min, to: n.wake_min < n.bed_min ? n.wake_min + 1440 : n.wake_min }));
    stops.push(responsive(schedEl, () => rangeChart(schedEl, rows, {
      format: clock, label: "Bedtime and wake time each night",
      usual: { from: clockFromText(st.bedtime), to: clockFromText(st.wake_time) },
    })));
  }
  const hypEl = detail.querySelector("[data-chart=hypnogram]");
  if (hypEl) {
    const used = new Set(last.segments.map((s) => s.stage));
    stops.push(responsive(hypEl, () => hypnogram(hypEl, last.segments, LANES.filter((l) => used.has(l.key)))));
  }
  return () => stops.forEach((f) => f());
}

function stageTable(stages, asleep, typical = false) {
  const rows = [...STAGES.filter((s) => stages[s.key] > 0.01), AWAKE].filter((s) => s.key !== "awake" || stages.awake > 0);
  return html`<table class="table stage-table"><thead><tr><th>Stage</th><th class="num">Time</th><th class="num">Share of sleep</th>
    ${typical ? html`<th class="hide-narrow">Typical</th>` : ""}</tr></thead><tbody>${rows.map((s) => html`<tr>
    <td><span class="key-swatch"><i style="background:${s.color}"></i>${s.name}</span></td>
    <td class="num">${hoursText(stages[s.key])}</td><td class="num">${s.key === "awake" ? "—" : pct(stages[s.key], asleep)}</td>
    ${typical ? html`<td class="hide-narrow small muted">${TYPICAL[s.key] || ""}</td>` : ""}</tr>`)}</tbody></table>`;
}

function consistency(st) {
  if (st.bedtime_spread_min == null) return "A few more nights are needed to see how regular your schedule is.";
  const s = Math.max(st.bedtime_spread_min, st.wake_spread_min ?? 0);
  if (s <= 30) return "A steady schedule: you go to bed and get up at much the same time each day.";
  if (s <= 60) return "Fairly regular. Keeping bedtime and wake time within about half an hour, weekends included, tends to help.";
  return "Your schedule shifts a lot from night to night. Going to bed and getting up at steadier times, weekends included, often makes sleep more restful.";
}

