// Trends: everything measured over time in one place — lab results and vitals from health systems, and daily data
// from wearables and the iPhone — organized by topic, so weight from a clinic visit and weight from a smart scale sit
// side by side instead of on two different pages.

import { get } from "../api.js";
import { appBridge, nativeNav } from "../app.js";
import { barChart, lineChart, responsive } from "../charts.js";
import { metricUnit, metricValue } from "./overview.js";
import { drawSleep } from "./sleep-view.js";
import {
  bucket, bucketWord, compareCard, goalText, loadGoals, loadMarkers, markerList, markerToggle, markersOn, meets, openGoal,
  rangeButtons, saveDays, savedDays, setMarkersOn,
} from "./trend-tools.js";
import { convert, displayUnit, fmtMeasure, recordValue } from "../units.js";
import { openRecord } from "./record-detail.js";
import {
  scrollToTop,
  $, emptyState, flagBadge, fmtDate, fmtNum, html, icon, loading, mount, onAction, plural,
} from "../ui.js";

/** Decimal places a value was given with (6 → 0, 5.9 → 1). */
function decimals(v) {
  if (v == null || !Number.isFinite(Number(v))) return 0;
  const m = String(Number(v)).match(/\.(\d+)$/);
  return m ? Math.min(m[1].length, 3) : 0;
}

/** A number with exactly `places` decimals, in the reader's locale. */
function fmtFixed(v, places) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: places, maximumFractionDigits: places });
}

const TOPICS = [
  ["labs", "Labs"], ["vitals", "Vitals"], ["heart", "Heart"], ["sleep", "Sleep"],
  ["activity", "Activity"], ["body", "Body"], ["more", "More"],
];
// Wearable metric groups (see app/store/biometrics.py) → topic.
const GROUP_TOPIC = { activity: "activity", mobility: "activity", heart: "heart", readiness: "sleep", sleep: "sleep",
                      vitals: "vitals", body: "body" };
const GROUP_LABEL = { hearing: "Hearing", nutrition: "Nutrition" };
// Clinic-measured vitals that belong with body measurements or heart data (LOINC).
const BODY_CODES = new Set(["29463-7", "3141-9", "39156-5", "8302-2", "8306-3", "8287-5", "9843-4", "8280-0"]);
const HEART_CODES = new Set(["8867-4", "8889-8"]);
// What devices recorded or warned about (ECGs, watch notifications), shown with the measurements they relate to.
const SIGNAL_TOPIC = { sleep_apnea_event: "sleep", walking_steadiness_event: "activity", environmental_audio_exposure_event: "more",
                       headphone_audio_exposure_event: "more" };
const SIGNAL_SECTION = { heart: "ECGs & notifications", sleep: "Notifications", activity: "Mobility", more: "Hearing" };
const NARROW = "(max-width: 820px)";

function clinicalTopic(item, category) {
  if (category === "labs") return "labs";
  if (BODY_CODES.has(item.code) || /\b(weight|height|bmi|body mass|circumference)\b/i.test(item.title)) return "body";
  if (HEART_CODES.has(item.code) || /heart rate|pulse/i.test(item.title)) return "heart";
  return "vitals";
}

/** Sub-heading for a wearable metric within its topic. Topics that mix clinic and device data say where each came from. */
function wearableSection(m, topic) {
  if (m.group === "sleep") return "Sleep";
  if (m.group === "readiness") return "Recovery";
  if (m.group === "mobility") return "Mobility";
  if (topic === "activity") return "Daily activity";
  if (topic === "more") return GROUP_LABEL[m.group] || "Other";
  return "From wearables & iPhone";
}

function isFlagged(interp) { return interp && interp !== "normal"; }

// Body measurements from health records (weight, height, temperature) follow the chosen units; lab results don't.
const BODY_UNITS = new Set(["kg", "lb", "[lb_av]", "cm", "in", "[in_i]", "°C", "Cel", "°F", "[degF]"]);

function trendIcon(t) {
  if (t === "up") return html`<span class="faint" title="Rising">${icon("arrowUp")}</span>`;
  if (t === "down") return html`<span class="faint" title="Falling">${icon("arrowDown")}</span>`;
  return "";
}

/** One list of everything that can be charted, each item tagged with its topic and sub-heading. */
function buildItems(labs, vitals, metrics, signals) {
  const items = [];
  for (const [category, list] of [["labs", labs], ["vitals", vitals]]) {
    for (const it of list) {
      const topic = clinicalTopic(it, category);
      items.push({
        key: `c:${it.code}`, kind: "clinical", category, code: it.code, topic, title: it.title,
        section: topic === "labs" ? (it.panel || "Other tests") : "From health records",
        value: category === "vitals" ? recordValue(it.latest) : it.latest.value_text || "", interpretation: it.latest.interpretation, trend: it.trend,
        meta: `${fmtDate(it.latest.effective_at)} · ${plural(it.count, "result")}`,
      });
    }
  }
  for (const m of metrics) {
    const topic = GROUP_TOPIC[m.group] || "more";
    const unit = metricUnit(m) ? ` ${metricUnit(m)}` : "";
    items.push({
      key: `m:${m.metric}`, kind: "wearable", metric: m.metric, topic, title: m.label,
      section: wearableSection(m, topic),
      value: m.latest_value != null ? `${metricValue(m, m.latest_value)}${unit}` : "",
      meta: `${fmtDate(m.latest_day)} · ${m.sources.map((s) => s.replace(/ \(simulated\)$/i, "")).join(", ")}`,
    });
  }
  const byType = new Map();
  for (const r of signals) {
    const cur = byType.get(r.event_type);
    if (cur) { cur.count += r.count; if (r.latest > cur.latest) cur.latest = r.latest; } else byType.set(r.event_type, { ...r });
  }
  for (const r of byType.values()) {
    const topic = SIGNAL_TOPIC[r.event_type] || "heart";
    items.push({
      key: `e:${r.event_type}`, kind: "signal", eventType: r.event_type, topic, title: r.name.replace(/ Notification$/, " notifications"),
      section: SIGNAL_SECTION[topic], value: fmtNum(r.count, 0), meta: `Latest ${fmtDate(r.latest)}`,
    });
  }
  return items;
}

export async function render({ el, parts, params, state, navigate }) {
  const profile = state.profile;
  mount(el, loading(3));
  const [labs, vitals, metrics, signals] = await Promise.all([
    get("/api/observations", { profile: profile.id, category: "labs" }).then((r) => r.items),
    get("/api/observations", { profile: profile.id, category: "vitals" }).then((r) => r.items),
    get("/api/biometrics/metrics", { profile: profile.id, latest: true }).then((r) => r.metrics),
    get("/api/biometrics/events", { profile: profile.id, view: "signals", days: 0, limit: 1 }).then((r) => r.summary),
  ]);
  const items = buildItems(labs, vitals, metrics, signals);
  const METRIC_UNITS = new Map(metrics.map((m) => [m.metric, m.unit]));
  const topics = TOPICS.filter(([k]) => items.some((i) => i.topic === k));

  if (!items.length) {
    mount(el, html`
      <div class="page-head"><div><h1>Trends</h1><p>Lab results, vitals and wearable data over time.</p></div></div>
      <div class="card">${emptyState("Nothing to chart yet",
        "Lab results and vitals appear once a health system shares them; sleep, heart and activity data once a wearable or the iPhone app is connected.",
        html`<a class="btn btn-primary" href="#/sources">${icon("plus")} Add a source</a>`)}</div>`);
    return;
  }

  // What to show: an explicit test or metric wins (and picks its topic), then the topic in the address, then the
  // first out-of-range lab, then the first topic with data.
  const wanted = params.get("metric") ? `m:${params.get("metric")}` : params.get("code") ? `c:${params.get("code")}`
    : params.get("event") ? `e:${params.get("event")}` : null;
  let selected = items.find((i) => i.key === wanted) || null;
  let topic = selected?.topic || (topics.some(([k]) => k === parts[0]) ? parts[0] : topics[0][0]);
  if (!selected) {
    const inTopic = items.filter((i) => i.topic === topic);
    selected = inTopic.find((i) => isFlagged(i.interpretation)) || inTopic[0];
  }
  // On a phone the list and the chart take turns; opening a link to one test goes straight to its chart.
  let showDetail = Boolean(wanted && selected?.key === wanted);
  let filter = "";
  let days = savedDays();
  let source = "";
  let showMarkers = markersOn();
  // Comparing the chosen measure with another (kept in the address, so a link from an insight opens it).
  const cmp = { b: params.get("compare") || null, lag: Number(params.get("lag")) || 0 };
  const [goalData, markers] = await Promise.all([loadGoals(profile.id).catch(() => null), loadMarkers(profile.id)]);
  let goalsNow = goalData;

  mount(el, html`
    <div class="page-head"><div><h1>Trends</h1><p>Lab results, vitals and wearable data over time, in one place.</p></div></div>
    <div class="tabs" id="tr-tabs" role="tablist"></div>
    <div class="split" id="tr-layout">
      <div class="card split-list">
        <div class="split-filter search" id="tr-filter-wrap">${icon("search")}<input class="input" id="tr-filter" placeholder="Filter" aria-label="Filter this list"></div>
        <div id="tr-list"></div>
      </div>
      <div class="split-detail stack" id="tr-detail"></div>
    </div>`);

  const hashFor = (it) => {
    const q = !it ? "" : it.kind === "wearable" ? `?metric=${encodeURIComponent(it.metric)}`
      + (it === selected && cmp.b ? `&compare=${encodeURIComponent(cmp.b)}${cmp.lag ? `&lag=${cmp.lag}` : ""}` : "")
      : it.kind === "signal" ? `?event=${encodeURIComponent(it.eventType)}` : `?code=${encodeURIComponent(it.code)}`;
    return `#/trends/${it?.topic || topic}${q}`;
  };
  const address = () => history.replaceState(null, "", hashFor(selected));
  // In the iPhone app a chart is a screen of its own, with the app's back button: the page shows only that chart.
  const detailScreen = nativeNav && showDetail;
  if (detailScreen) {
    $("#tr-tabs", el).hidden = true;
    el.dataset.appTitle = selected.title;     // the screen's title in the app, instead of "Trends"
  }

  const drawTabs = () => mount($("#tr-tabs", el), topics.map(([k, label]) => {
    const n = items.filter((i) => i.topic === k).length;
    const flagged = k === "labs" && items.some((i) => i.topic === k && isFlagged(i.interpretation));
    return html`<a class="tab ${k === topic ? "active" : ""}" href="#/trends/${k}" data-action="topic" data-topic="${k}" role="tab"
      aria-selected="${k === topic}">${label} <span class="n">${n}</span>${flagged ? html`<span class="dot bad" title="Out-of-range results"></span>` : ""}</a>`;
  }));

  const drawList = () => {
    const inTopic = items.filter((i) => i.topic === topic);
    $("#tr-filter-wrap", el).hidden = inTopic.length < 10;
    const shown = filter ? inTopic.filter((i) => i.title.toLowerCase().includes(filter)) : inTopic;
    const sections = new Map();
    for (const it of shown) {
      if (!sections.has(it.section)) sections.set(it.section, []);
      sections.get(it.section).push(it);
    }
    mount($("#tr-list", el), shown.length ? [...sections.entries()].map(([name, list]) => html`
      <div class="split-section">${name}</div>
      <div class="list">${list.map((it) => html`
        <div class="list-item clickable ${it.key === selected?.key ? "selected" : ""}" data-action="pick" data-key="${it.key}" tabindex="0" role="button"
          aria-current="${it.key === selected?.key}">
          <div class="grow" style="min-width:0"><div class="title truncate">${it.title}</div><div class="meta truncate">${it.meta}</div></div>
          <div class="value small">${it.value}</div>${trendIcon(it.trend)}
          ${isFlagged(it.interpretation) ? html`<span class="dot ${it.interpretation.includes("low") ? "info" : "bad"}" title="Out of range"></span>` : ""}
          <span class="split-chevron">${icon("chevronRight")}</span>
        </div>`)}</div>`) : html`<div class="empty small">Nothing matches “${filter}”.</div>`);
  };

  const layout = () => $("#tr-layout", el).classList.toggle("show-detail", showDetail);

  let stopResize = null;
  let drawSeq = 0;
  const backBar = () => html`<button class="btn btn-ghost btn-sm split-back" data-action="back">${icon("chevronLeft")} ${TOPICS.find(([k]) => k === topic)[1]}</button>`;

  const drawClinical = async (detail, it, seq) => {
    const s = await get("/api/observations/series", { profile: profile.id, code: it.code, category: it.category });
    if (seq !== drawSeq) return;
    const hasComponents = s.components.length > 0 && !s.points.length;
    // Weight, height and temperature in the chosen units; everything else as recorded.
    const body = it.category === "vitals" && BODY_UNITS.has(s.unit);
    const hint = /height|length/i.test(s.title) ? "height" : "";
    const cv = (v) => (body && v != null ? convert(v, s.unit, hint).value : v);
    const unit = body ? displayUnit(s.unit, hint) : s.unit || "";
    // Results of one test are shown to the same precision (6.0 next to 5.9, not 6), up to what the lab reported.
    const places = body ? undefined : Math.min(3, Math.max(0, ...s.points.map((p) => decimals(p.v))));
    const num = (v) => (places === undefined ? fmtNum(v) : fmtFixed(v, places));
    const text = (v) => (body ? fmtMeasure(v, s.unit, hint) : `${num(v)} ${s.unit || ""}`);
    const pts = s.points.map((p) => ({ ...p, raw: p.v, v: cv(p.v) }));
    s.ref_low = cv(s.ref_low);
    s.ref_high = cv(s.ref_high);
    const latest = pts[pts.length - 1];
    const first = pts[0];
    const clinicalMarks = () => (pts.length > 1 ? markers.filter((m) => m.t >= String(first.t).slice(0, 10) && m.t <= String(latest.t).slice(0, 10)) : []);
    const change = latest && first && pts.length > 1 ? latest.v - first.v : null;
    const rows = hasComponents
      ? s.components[0].points.map((p, i) => ({ t: p.t, record_id: p.record_id, source: p.source_name,
          text: s.components.map((c) => fmtNum(c.points[i]?.v)).join(" / ") + " " + (s.components[0].unit || "") }))
      : pts.map((p) => ({ t: p.t, record_id: p.record_id, text: text(p.raw), interpretation: p.interpretation, source: p.source_name }));
    const latestText = hasComponents ? rows[rows.length - 1]?.text
      : latest ? (body && hint && unit === "in" ? text(latest.raw) : html`${body ? fmtNum(latest.v, 1) : num(latest.v)}<small>${unit}</small>`) : "";
    const refNum = (v) => (body ? fmtNum(v) : fmtFixed(v, Math.max(places, decimals(v))));
    const ref = s.ref_low != null || s.ref_high != null
      ? `Range ${s.ref_low != null ? refNum(s.ref_low) : ""}${s.ref_low != null && s.ref_high != null ? "–" : s.ref_high != null ? "≤ " : "+"}${s.ref_high != null ? refNum(s.ref_high) : ""} ${unit}` : null;
    mount(detail, html`
      ${backBar()}
      <div class="card card-pad">
        <div class="detail-head">
          <div class="grow"><div class="eyebrow">${it.category === "labs" ? "Lab result" : "Measured at visits"}</div><h2>${s.title}</h2>
            <div class="small muted">${[ref, `LOINC ${it.code}`].filter(Boolean).join(" · ")}</div></div>
          ${latestText ? html`<div class="detail-value"><div class="v">${latestText}</div>
            <div class="small muted">${fmtDate(rows[rows.length - 1]?.t)} ${flagBadge(latest?.interpretation)}</div></div>` : ""}
        </div>
        ${hasComponents ? html`<div class="legend" style="margin-bottom:6px">${s.components.map((c, i) => html`<span class="${i ? "alt" : ""}">${c.name}</span>`)}</div>`
          : ref ? html`<div class="legend" style="margin-bottom:6px"><span class="band">Reference range</span></div>` : ""}
        ${pts.length > 1 ? html`<div class="row wrap" style="justify-content:flex-end;margin-bottom:4px">${markerToggle(clinicalMarks().length, showMarkers)}</div>` : ""}
        <div id="tr-chart"></div>
        ${markerList(showMarkers ? clinicalMarks() : [])}
        ${change !== null ? html`<p class="small muted" style="margin-top:8px">${Math.abs(change) < 1e-9 ? "Unchanged" : `${change > 0 ? "Up" : "Down"} ${body ? fmtNum(Math.abs(change), 1) : num(Math.abs(change))} ${unit}`}
          since ${fmtDate(first.t)} (${plural(pts.length, "result")}).</p>` : ""}
      </div>
      <div class="card"><div class="card-head"><h3>All results</h3><span class="badge">${rows.length}</span></div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Date</th><th style="text-align:right">Value</th><th>Flag</th><th>Source</th></tr></thead>
        <tbody>${rows.slice().reverse().map((r) => html`<tr class="clickable" data-action="open" data-id="${r.record_id}">
          <td>${fmtDate(r.t)}</td><td class="num">${r.text}</td><td>${flagBadge(r.interpretation)}</td><td class="small muted">${r.source || ""}</td></tr>`)}</tbody></table></div></div>`);
    wireDetail(detail);
    const chartEl = $("#tr-chart", detail);
    const draw = () => {
      if (hasComponents) {
        lineChart(chartEl, s.components.slice(0, 2).map((c, i) => ({ name: c.name, alt: i === 1, points: c.points })), { unit: s.components[0].unit, label: s.title });
      } else {
        lineChart(chartEl, [{ name: s.title, points: pts.map((p) => ({ t: p.t, v: p.v, flag: isFlagged(p.interpretation), label: p.source_name })) }],
          { unit, band: { low: s.ref_low, high: s.ref_high }, label: s.title, markers: showMarkers ? clinicalMarks() : [],
            onPointClick: (p) => openRecord(pts.find((q) => q.t === p.t)?.record_id) });
      }
    };
    stopResize = responsive(chartEl, draw);
  };

  const controls = (it, sources, goal) => html`<div class="row wrap between trend-controls">
      ${rangeButtons(days)}
      <div class="row wrap" style="gap:8px">
        ${sources.length > 1 || source ? html`<select class="input input-sm" id="tr-source" aria-label="Source" style="width:auto">
          <option value="">Best source</option>${sources.map((src) => html`<option value="${src}" ${src === source ? "selected" : ""}>${src}</option>`)}</select>` : ""}
        <button class="btn btn-sm" data-action="goal">${icon("target")} ${goal ? "Goal" : "Set goal"}</button>
      </div>
    </div>`;

  const wireDetail = (detail) => {
    $("#tr-source", detail)?.addEventListener("change", (e) => { source = e.target.value; drawDetail(); });
    detail.querySelector("[data-markers]")?.addEventListener("change", (e) => { showMarkers = e.target.checked; setMarkersOn(showMarkers); drawDetail(); });
  };

  const drawCompare = async (detail, it, seq) => {
    const host = document.createElement("div");
    detail.appendChild(host);
    const stop = await compareCard(host, profile, { key: it.metric, label: it.title }, days, cmp, () => address());
    if (seq !== drawSeq) { stop(); return; }
    const prev = stopResize;
    stopResize = () => { prev?.(); stop(); };
  };

  const drawWearable = async (detail, it, seq) => {
    const goal = goalsNow?.goals?.[it.metric] || null;
    if (it.metric === "sleep_duration") {
      const s = await get("/api/biometrics/daily", { profile: profile.id, metric: it.metric, days: 7, source: source || undefined });
      if (seq !== drawSeq) return;
      stopResize = await drawSleep(detail, {
        profile, days, source, goal, markers, markersOn: showMarkers, back: backBar(), controls: controls(it, s.sources, goal),
      }, seq, () => drawSeq);
      if (seq !== drawSeq) return;
      wireDetail(detail);
      await drawCompare(detail, it, seq);
      return;
    }
    const s = await get("/api/biometrics/daily", { profile: profile.id, metric: it.metric, days, source: source || undefined });
    if (seq !== drawSeq) return;
    const m = { metric: it.metric, unit: s.unit };
    const unit = metricUnit(m);
    // Charts plot values in the chosen units (miles, pounds, °F…).
    const chartUnit = s.unit === "h" || s.unit === "score" ? s.unit : displayUnit(s.unit, it.metric);
    const cv = (v) => (s.unit === "h" || s.unit === "score" || v == null ? v : convert(v, s.unit, it.metric).value);
    const shown = s.points.map((p) => ({ ...p, value: cv(p.value) }));
    const { points, size } = bucket(shown, days);
    const last = s.points[s.points.length - 1];
    const firstDay = s.points[0]?.day || s.start;
    const marks = showMarkers ? markers.filter((mk) => mk.t >= firstDay && mk.t <= s.end) : [];
    const inRange = markers.filter((mk) => mk.t >= firstDay && mk.t <= s.end).length;
    const complete = s.points.filter((p) => !p.partial);
    const met = goal ? complete.filter((p) => meets(goal, p.value)).length : null;
    const eyebrow = bucketWord(size) || (s.aggregation === "sum" ? "Daily total" : s.aggregation === "last" ? "Daily value" : "Daily average");
    mount(detail, html`
      ${backBar()}
      <div class="card card-pad">
        <div class="detail-head">
          <div class="grow"><div class="eyebrow">${eyebrow}</div>
            <h2>${s.label}</h2><div class="small muted">${last ? `${fmtDate(last.day)}${last.partial ? " so far" : ""} · ${last.source}` : "No data in this period"}</div></div>
          ${last ? html`<div class="detail-value"><div class="v">${metricValue(m, last.value)}<small>${unit}</small></div></div>` : ""}
        </div>
        ${controls(it, s.sources, goal)}
        ${inRange ? html`<div class="row wrap" style="justify-content:flex-end;margin:-4px 0 6px">${markerToggle(inRange, showMarkers)}</div>` : ""}
        <div id="tr-chart"></div>
        <div class="stat-row">
          <div><span>Average</span><b>${metricValue(m, s.stats.mean)}</b></div>
          <div><span>Low</span><b>${metricValue(m, s.stats.min)}</b></div>
          <div><span>High</span><b>${metricValue(m, s.stats.max)}</b></div>
          ${goal ? html`<div><span>Goal met</span><b>${met} of ${complete.length} days</b></div>` : html`<div><span>Days</span><b>${s.stats.count}</b></div>`}
        </div>
        ${goal ? html`<p class="small muted" style="margin:10px 0 0">Goal: ${goalText(goal, { key: it.metric, unit: s.unit })} a day.</p>` : ""}
        ${markerList(marks)}
      </div>
      ${source ? "" : html`<p class="hint">When several devices measure this on the same day, one is chosen per day by your
        <a href="#/settings/wearables">device priority</a>. Pick a source above to see only its data.</p>`}`);
    wireDetail(detail);
    const chartEl = $("#tr-chart", detail);
    const goalLine = goal ? cv(goal.target) : null;
    const draw = () => {
      const digits = s.unit === "h" ? 1 : convert(1, s.unit, it.metric).digits ?? 0;
      if (s.aggregation === "sum") barChart(chartEl, points, { unit: chartUnit, avg: cv(s.stats.mean), label: s.label, digits, goal: goalLine, markers: marks,
        format: s.unit === "h" ? (v) => metricValue(m, v) : undefined });
      else lineChart(chartEl, [{ name: s.label, points: points.map((p) => ({ t: p.day, v: p.value, label: p.tipLabel || p.source })) }],
        { unit: chartUnit, label: s.label, goal: goalLine, markers: marks, digits });
    };
    stopResize = responsive(chartEl, draw);
    await drawCompare(detail, it, seq);
  };

  // An ECG or a notification: every time it happened, newest first, with what the device reported.
  const drawSignal = async (detail, it, seq) => {
    const { events } = await get("/api/biometrics/events", { profile: profile.id, view: "signals", type: it.eventType, days: 0, limit: 500 });
    if (seq !== drawSeq) return;
    const detailText = (e) => {
      const meta = e.metadata || {};
      const parts = [e.value_label, e.value != null && e.event_type === "ecg" ? `${fmtNum(e.value, 0)} bpm average` : null,
        meta.symptomsStatus === "present" ? "Symptoms noted" : null];
      return parts.filter((x) => x && x !== "Occurred").join(" · ");
    };
    mount(detail, html`
      ${backBar()}
      <div class="card card-pad"><div class="detail-head">
        <div class="grow"><div class="eyebrow">${it.eventType === "ecg" ? "Recordings" : "From your devices"}</div><h2>${it.title}</h2>
          <div class="small muted">${plural(events.length, "time")} · latest ${fmtDate(events[0]?.start_date)}</div></div></div>
        <p class="small muted" style="margin:0">${it.eventType === "ecg"
          ? "ECGs taken on your watch. The classification is the watch's own and isn't a diagnosis; share anything unexpected with your care team."
          : "Notifications your watch or iPhone sent. They're a prompt to pay attention, not a diagnosis."}</p></div>
      <div class="card"><div class="card-head"><h3>History</h3><span class="badge">${events.length}</span></div>
        <div class="table-wrap"><table class="table"><thead><tr><th>When</th><th>Details</th><th>Source</th></tr></thead>
        <tbody>${events.map((e) => html`<tr><td>${new Date(e.start_date).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</td>
          <td>${detailText(e) || "—"}</td><td class="small muted">${e.source_name || ""}</td></tr>`)}</tbody></table></div></div>`);
  };

  const drawDetail = async () => {
    const detail = $("#tr-detail", el);
    const seq = ++drawSeq;
    if (stopResize) { stopResize(); stopResize = null; }
    if (!selected) { mount(detail, ""); return; }
    mount(detail, html`${backBar()}${loading(2)}`);
    if (selected.kind === "wearable") await drawWearable(detail, selected, seq);
    else if (selected.kind === "signal") await drawSignal(detail, selected, seq);
    else await drawClinical(detail, selected, seq);
  };

  const narrow = () => window.matchMedia(NARROW).matches;

  drawTabs();
  drawList();
  layout();
  await drawDetail();
  if (wanted) $(".list-item.selected", el)?.scrollIntoView({ block: "nearest" });

  $("#tr-filter", el).addEventListener("input", (e) => { filter = e.target.value.trim().toLowerCase(); drawList(); });

  const off = onAction(el, {
    topic: ({ topic: t }) => {
      if (t === topic) { showDetail = false; layout(); return; }
      topic = t; filter = ""; $("#tr-filter", el).value = ""; source = ""; cmp.b = null; cmp.lag = 0;
      const inTopic = items.filter((i) => i.topic === topic);
      selected = inTopic.find((i) => isFlagged(i.interpretation)) || inTopic[0];
      showDetail = false;
      address(); drawTabs(); drawList(); layout();
      return drawDetail();
    },
    pick: ({ key }) => {
      const next = items.find((i) => i.key === key);
      if (!next) return;
      // The iPhone app opens the chart as a new screen.
      if (nativeNav && narrow() && !showDetail && appBridge({ type: "visit", hash: hashFor(next) })) return;
      if (next.key !== selected?.key) { source = ""; cmp.b = null; cmp.lag = 0; }
      selected = next;
      showDetail = true;
      address(); drawList(); layout();
      if (narrow()) scrollToTop();
      return drawDetail();
    },
    back: () => { showDetail = false; layout(); scrollToTop(); },
    range: ({ days: d }) => { days = Number(d); saveDays(days); return drawDetail(); },
    goal: () => {
      if (selected?.kind !== "wearable") return;
      const g = goalsNow?.goals?.[selected.metric];
      const unit = METRIC_UNITS.get(selected.metric);
      openGoal(profile, { key: selected.metric, metric: selected.metric, label: selected.title, unit }, g,
        goalsNow?.suggested?.[selected.metric], async () => { goalsNow = await loadGoals(profile.id); drawDetail(); });
    },
    open: ({ id }) => id && openRecord(id),
  });
  address();
  return () => { off(); if (stopResize) stopResize(); };
}
