// Trends: everything measured over time in one place — lab results and vitals from health systems, and daily data
// from wearables and the iPhone — organized by topic, so weight from a clinic visit and weight from a smart scale sit
// side by side instead of on two different pages.

import { del, get } from "../api.js";
import { appBridge, nativeNav } from "../app.js";
import { barChart, lineChart, responsive } from "../charts.js";
import { metricUnit, metricValue } from "./overview.js";
import { drawSleep } from "./sleep-view.js";
import {
  bucket, bucketWord, chartNotes, compareCard, goalText, loadGoals, loadMarkers, markerList, markerToggle, markersOn, meets, openGoal,
  rangeButtons, saveDays, savedDays, setMarkersOn,
} from "./trend-tools.js";
import { convert, displayUnit, fmtMeasure, recordValue } from "../units.js";
import { openRecord } from "./record-detail.js";
import {
  scrollToTop,
  $, $$, confirmDialog, emptyState, flagBadge, fmtDate, fmtNum, html, icon, loading, modal, mount, onAction, plural, resultStatus, toast,
} from "../ui.js";

const LEFT_OUT_REASON = { range: "Outside what's possible", unit: "In a unit Syntropy Health doesn't know", date: "The date can't be right" };

const READINGS_SHOWN = 500;
const CHART_HEIGHT = 280;
const CLINICAL_PERIODS = [[0, "All"], [1825, "5 years"], [365, "1 year"], [180, "6 months"]];
const BP_PANEL_CODE = "85354-6";
const HOME_PRESSURE = [["blood_pressure_systolic", "Home systolic"], ["blood_pressure_diastolic", "Home diastolic"]];
const readingText = (s) => {
  const m = { metric: s.metric_type, unit: s.shown_unit };
  return `${metricValue(m, s.shown_value)} ${metricUnit(m)}`.trim();
};

/** Stored readings with a way to remove a wrong one for good: those kept but not counted (one measure's, or with no measure
    everyone's), or one measure's newest, reaching older ones by date. `onChange` runs when the list closes, if one went. */
async function openReadings(profile, { metric, label, leftOut = false }, onChange) {
  let removed = false;
  const m = modal({
    title: leftOut ? (label ? `${label}: readings left out` : "Readings left out") : `${label}: readings`,
    body: html`<p class="small muted">${leftOut
      ? "These came from your devices but can't be right, so they aren't in your charts, averages or insights. They're kept in case you want to check them against the source."
      : "What your devices sent for this, newest first. If one is wrong, such as someone else on your scale, you can remove it."}</p>
      <label class="row small muted" style="gap:8px;margin-bottom:10px" ${leftOut ? "hidden" : ""}>Up to
        <input class="input input-sm" type="date" data-until style="width:auto"></label>
      <div data-rows>${loading(2)}</div>`,
    onClose: () => { if (removed) onChange(); },
  });
  // Not the date field: in the iPhone app that would open its picker over the list.
  m.el.focus({ preventScroll: true });
  const rows = $("[data-rows]", m.el);
  const until = $("[data-until]", m.el);
  const draw = async () => {
    const { samples } = await get("/api/biometrics/samples", { profile: profile.id, metric, left_out: leftOut || undefined, until: until.value,
                                                                limit: READINGS_SHOWN }, { fresh: true });
    if (samples.length >= READINGS_SHOWN) until.closest("label").hidden = false;
    mount(rows, samples.length ? html`
      <div class="list">${samples.map((s) => html`<div class="list-item">
        <div class="grow" style="min-width:0"><div class="title">${metric ? "" : `${s.label}: `}${readingText(s)}</div>
          <div class="meta">${[new Date(s.start_date).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }), s.source_name].filter(Boolean).join(" · ")}</div>
          ${leftOut ? html`<div class="meta">${LEFT_OUT_REASON[s.flag] || ""}</div>` : ""}</div>
        <button class="btn btn-sm btn-ghost btn-danger" data-delete data-remove="${encodeURIComponent(s.id)}">Remove</button></div>`)}</div>
      ${samples.length >= READINGS_SHOWN ? html`<p class="small muted" style="margin-top:8px">Showing the newest ${READINGS_SHOWN}. Choose an earlier date to see older ones.</p>` : ""}`
      : html`<div class="empty small">${until.value ? "No readings up to that date." : leftOut ? "Nothing is left out now." : "No readings are stored for this."}</div>`);
    rows.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
      if (!await confirmDialog("Remove this reading?", "It comes out of your charts and averages, and a later sync won't bring it back. This can't be undone.",
        { confirmLabel: "Remove", danger: true })) return;
      try {
        await del(`/api/biometrics/samples/${b.dataset.remove}?profile=${encodeURIComponent(profile.id)}`);
        removed = true;
        const row = b.closest(".list-item");
        if (row.parentElement.children.length > 1) row.remove(); else await draw();
      } catch (err) { toast(err.message, "bad"); }
    }));
  };
  until.addEventListener("change", () => draw().catch((err) => toast(err.message, "bad")));
  await draw();
}

/** Readings left out of every measure, including ones with nothing else to chart. */
function leftOutNote(list) {
  const n = list.reduce((sum, x) => sum + x.count, 0);
  if (!n) return "";
  return html`<p class="small muted" id="tr-left-out" style="margin:-8px 0 14px">${plural(n, "reading")} from your devices ${n === 1 ? "isn't" : "aren't"} counted because ${n === 1 ? "it can't" : "they can't"} be right.
    <button class="link-btn" data-action="left-out-all">See ${n === 1 ? "it" : "them"}</button></p>`;
}

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
function deviceName(source) { return source === "Combined" ? "your devices together" : source; }

/** A row per result of a panel (blood pressure), by the record each number came from, so a result with one number
 *  can't shift the rest of the history. Systolic first, and a dash where a number is missing. */
export function componentRows(components) {
  const ordered = [...components].sort((a, b) => /systolic/i.test(b.name) - /systolic/i.test(a.name));
  const byResult = new Map();
  ordered.forEach((c, i) => c.points.forEach((p) => {
    const key = p.record_id || p.t;
    if (!byResult.has(key)) byResult.set(key, { t: p.t, record_id: p.record_id, source: p.source_name, values: [] });
    byResult.get(key).values[i] = p.v;
  }));
  const unit = ordered[0]?.unit || "";
  return [...byResult.values()].sort((a, b) => String(a.t).localeCompare(String(b.t))).map((r) => ({
    t: r.t, record_id: r.record_id, source: r.source,
    text: `${ordered.map((_, i) => (r.values[i] != null ? fmtNum(r.values[i]) : "—")).join(" / ")} ${unit}`.trim(),
  }));
}

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
        key: it.code ? `c:${it.code}` : `t:${it.title}`, kind: "clinical", category, code: it.code, codes: it.codes || [], codeSystem: it.code_system, topic, title: it.title,
        section: topic === "labs" ? (it.panel || "Single tests") : "From health records",
        value: category === "vitals" ? recordValue(it.latest) : it.latest.value_text || "", interpretation: it.latest.interpretation, status: it.latest.status, trend: it.trend,
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

/** A result that's words, like "Reactive", has nothing to chart and isn't in the list, but its flag on the Overview and in the bell still links to it. */
async function resultWithoutNumber(profileId, code, title, category) {
  const s = await get("/api/observations/series", { profile: profileId, code: code || undefined, title: code ? undefined : title, category }).catch(() => null);
  const last = s?.text_results?.[s.text_results.length - 1];
  if (!last) return null;
  const item = { code: s.code, title: s.title };
  return {
    key: item.code ? `c:${item.code}` : `t:${item.title}`, kind: "clinical", category, code: item.code, codes: s.codes || [], codeSystem: s.code_system,
    topic: clinicalTopic(item, category), title: item.title, section: category === "labs" ? "Single tests" : "From health records",
    value: last.text, interpretation: last.interpretation, status: last.status, trend: null,
    meta: `${fmtDate(last.t)} · ${plural(s.text_results.length, "result")}`,
  };
}

export async function render({ el, parts, params, state, navigate }) {
  const profile = state.profile;
  mount(el, loading(3));
  const [labs, vitals, { metrics, left_out: leftOut }, signals] = await Promise.all([
    get("/api/observations", { profile: profile.id, category: "labs" }).then((r) => r.items),
    get("/api/observations", { profile: profile.id, category: "vitals" }).then((r) => r.items),
    get("/api/biometrics/metrics", { profile: profile.id, latest: true }),
    get("/api/biometrics/events", { profile: profile.id, view: "signals", days: 0, limit: 1 }).then((r) => r.summary),
  ]);
  const items = buildItems(labs, vitals, metrics, signals);
  const wanted = params.get("metric") ? `m:${params.get("metric")}` : params.get("code") ? `c:${params.get("code")}`
    : params.get("title") ? `t:${params.get("title")}` : params.get("event") ? `e:${params.get("event")}` : null;
  // A link can name any code of a test, or a test by its name; the list has one entry for all of them.
  const linked = () => items.find((i) => i.key === wanted)
    || (params.get("code") ? items.find((i) => i.codes?.includes(params.get("code"))) : null)
    || (params.get("title") ? items.find((i) => i.kind === "clinical" && i.title === params.get("title")) : null) || null;
  if (wanted && !linked() && (params.get("code") || params.get("title"))) {
    const item = await resultWithoutNumber(profile.id, params.get("code"), params.get("title"), parts[0] === "vitals" ? "vitals" : "labs");
    if (item) items.push(item);
  }
  const METRIC_UNITS = new Map(metrics.map((m) => [m.metric, m.unit]));
  const topics = TOPICS.filter(([k]) => items.some((i) => i.topic === k));
  // The page again, from what's stored now: a removed reading can change a day, its measure's latest value, or whether it's listed.
  const redraw = () => navigate(location.hash);
  const seeLeftOut = () => openReadings(profile, { leftOut: true }, redraw);

  if (!items.length) {
    mount(el, html`
      <div class="page-head"><div><h1>Trends</h1><p>Lab results, vitals and wearable data over time.</p></div></div>
      ${leftOutNote(leftOut)}
      <div class="card">${emptyState("Nothing to chart yet",
        "Lab results and vitals appear once a health system shares them; sleep, heart and activity data once a wearable or the iPhone app is connected.",
        html`<a class="btn btn-primary" href="#/sources">${icon("plus")} Add a source</a>`)}</div>`);
    return onAction(el, { "left-out-all": seeLeftOut });
  }

  // What to show: an explicit test or metric wins (and picks its topic), then the topic in the address, then the
  // first out-of-range lab, then the first topic with data.
  let selected = linked();
  // On a phone the list and the chart take turns; a link to one test goes straight to it. A link to something that's no longer
  // here stays on the list, rather than opening whichever chart would be chosen by default.
  let showDetail = Boolean(selected);
  let topic = selected?.topic || (topics.some(([k]) => k === parts[0]) ? parts[0] : topics[0][0]);
  if (!selected) {
    const inTopic = items.filter((i) => i.topic === topic);
    selected = inTopic.find((i) => isFlagged(i.interpretation)) || inTopic[0];
  }
  let filter = "";
  let days = savedDays();
  let source = "";
  let unitChoice = 0;
  let clinicalDays = 0;
  let showMarkers = markersOn();
  // Comparing the chosen measure with another (kept in the address, so a link from an insight opens it).
  const cmp = { b: params.get("compare") || null, lag: Number(params.get("lag")) || 0 };
  const [goalData, markers] = await Promise.all([loadGoals(profile.id).catch(() => null), loadMarkers(profile.id)]);
  let goalsNow = goalData;

  mount(el, html`
    <div class="page-head"><div><h1>Trends</h1><p>Lab results, vitals and wearable data over time, in one place.</p></div></div>
    ${leftOutNote(leftOut)}
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
      : it.kind === "signal" ? `?event=${encodeURIComponent(it.eventType)}`
      : it.code ? `?code=${encodeURIComponent(it.code)}` : `?title=${encodeURIComponent(it.title)}`;
    return `#/trends/${it?.topic || topic}${q}`;
  };
  // In the iPhone app a chart is a screen of its own, with the app's back button: the page shows only that chart.
  const detailScreen = nativeNav && showDetail;
  // The list's own screen in the app keeps only its topic in the address. With a chart in it, drawing the page again
  // (pull to refresh, a change on another screen, another person) would show that chart in the list's place, with no
  // way back to the list.
  let chartPushed = false;
  const address = () => history.replaceState(null, "", nativeNav && !detailScreen ? `#/trends/${topic}` : hashFor(selected));
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
          <div class="value small">${it.value}</div>${resultStatus(it.status)}${trendIcon(it.trend)}
          ${isFlagged(it.interpretation) ? html`<span class="dot ${it.interpretation.includes("low") ? "info" : "bad"}" title="Out of range"></span>` : ""}
          <span class="split-chevron">${icon("chevronRight")}</span>
        </div>`)}</div>`) : html`<div class="empty small">Nothing matches “${filter}”.</div>`);
  };

  const layout = () => $("#tr-layout", el).classList.toggle("show-detail", showDetail);

  let stopResize = null;
  let drawSeq = 0;
  const backBar = () => html`<button class="btn btn-ghost btn-sm split-back" data-action="back">${icon("chevronLeft")} ${TOPICS.find(([k]) => k === topic)[1]}</button>`;

  // What identifies a test, by what its code really is: an institution's own code isn't a LOINC.
  const codeLabel = (it, codes) => {
    if (!it.code) return null;
    if (it.codeSystem !== "http://loinc.org") return `Code ${it.code}`;
    return (codes?.length || 0) > 1 ? `LOINC ${codes.join(", ")}` : `LOINC ${it.code}`;
  };

  const drawClinical = async (detail, it, seq) => {
    const all = await get("/api/observations/series", { profile: profile.id, code: it.code || undefined, title: it.code ? undefined : it.title, category: it.category });
    if (seq !== drawSeq) return;
    // One chart is one unit. Results in another unit, with no known conversion, have a chart of their own to switch to.
    const unitSets = [{ unit: all.unit, ref_low: all.ref_low, ref_high: all.ref_high, points: all.points }, ...(all.other_units || [])].filter((u) => u.points.length);
    const shownSet = unitSets[Math.min(unitChoice, Math.max(0, unitSets.length - 1))] || unitSets[0] || { unit: all.unit, ref_low: null, ref_high: null, points: [] };
    const s = { ...all, ...shownSet };
    const hasComponents = s.components.length > 0 && !s.points.length;
    // Weight, height and temperature in the chosen units; everything else as recorded.
    const body = it.category === "vitals" && BODY_UNITS.has(s.unit);
    const hint = /height|length/i.test(s.title) ? "height" : "";
    const cv = (v) => (body && v != null ? convert(v, s.unit, hint).value : v);
    const unit = body ? displayUnit(s.unit, hint) : s.unit || "";
    // Results of one test are shown to the same precision (6.0 next to 5.9, not 6), up to what the lab reported.
    const places = body ? undefined : Math.min(3, Math.max(0, ...s.points.map((p) => decimals(p.v))));
    const num = (v) => (places === undefined ? fmtNum(v) : fmtFixed(v, places));
    const text = (v) => (body ? fmtMeasure(v, s.unit, hint) : `${num(v)}${/^\//.test(s.unit || "") ? "" : " "}${s.unit || ""}`.trim());
    const pts = s.points.map((p) => ({ ...p, raw: p.v, v: cv(p.v) }));
    s.ref_low = cv(s.ref_low);
    s.ref_high = cv(s.ref_high);
    const latest = pts[pts.length - 1];
    const inPeriod = (p) => !clinicalDays || new Date(p.t).getTime() >= Date.now() - clinicalDays * 86400000;
    const shownPts = pts.filter(inPeriod);
    const shownComponents = s.components.map((c) => ({ ...c, points: c.points.filter(inPeriod) }));
    const span = [...shownPts, ...shownComponents.flatMap((c) => c.points)].map((p) => String(p.t).slice(0, 10)).sort();
    const clinicalMarks = () => (span.length > 1 ? markers.filter((m) => m.t >= span[0] && m.t <= span[span.length - 1]) : []);
    // A limit like "<5" isn't a level, so the change is between actual measurements.
    const measured = shownPts.filter((p) => !p.comparator);
    const change = measured.length > 1 ? measured[measured.length - 1].v - measured[0].v : null;
    const bound = (p) => (p.comparator ? p.comparator.replace("<=", "≤").replace(">=", "≥") : "");
    const rows = hasComponents ? componentRows(s.components)
      : [...pts.map((p) => ({ t: p.t, record_id: p.record_id, text: body ? `${bound(p)}${text(p.raw)}` : p.text || `${bound(p)}${text(p.raw)}`,
            standard: p.converted ? `${bound(p)}${text(p.raw)}` : null, status: p.status, interpretation: p.interpretation, source: p.source_name })),
         ...(all.text_results || []).map((p) => ({ t: p.t, record_id: p.record_id, text: p.text, status: p.status, interpretation: p.interpretation, source: p.source_name }))]
        .sort((a, b) => String(a.t).localeCompare(String(b.t)));
    const wordsOnly = !hasComponents && !pts.length && rows.length > 0;
    const lastRow = wordsOnly ? rows[rows.length - 1] : latest && rows.find((r) => r.record_id === latest.record_id);
    const latestText = hasComponents || wordsOnly ? rows[rows.length - 1]?.text
      : latest ? (body && hint && unit === "in" ? `${bound(latest)}${text(latest.raw)}` : html`${bound(latest)}${body ? fmtNum(latest.v, 1) : num(latest.v)}<small>${unit}</small>`) : "";
    const refNum = (v) => (body ? fmtNum(v) : fmtFixed(v, Math.max(places, decimals(v))));
    const ref = s.ref_low != null || s.ref_high != null
      ? `Range ${s.ref_low != null ? refNum(s.ref_low) : ""}${s.ref_low != null && s.ref_high != null ? "–" : s.ref_high != null ? "≤ " : "+"}${s.ref_high != null ? refNum(s.ref_high) : ""} ${unit}` : null;
    mount(detail, html`
      ${backBar()}
      <div class="card card-pad">
        <div class="detail-head">
          <div class="grow"><div class="eyebrow">${it.category === "labs" ? "Lab result" : "Measured at visits"}</div><h2>${s.title}</h2>
            <div class="small muted">${[ref, codeLabel(it, all.codes)].filter(Boolean).join(" · ")}</div></div>
          ${latestText ? html`<div class="detail-value"><div class="v">${latestText}</div>
            <div class="small muted">${fmtDate(hasComponents ? rows[rows.length - 1]?.t : lastRow?.t)} ${resultStatus(wordsOnly ? lastRow.status : latest?.status)} ${flagBadge(wordsOnly ? lastRow.interpretation : latest?.interpretation)}</div></div>` : ""}
        </div>
        ${unitSets.length > 1 ? html`<div class="chips" style="margin-bottom:8px">${unitSets.map((u, i) => html`<button class="chip ${u === shownSet ? "active" : ""}" data-action="unit" data-index="${i}">
          ${u.unit || "No unit"} <span class="n">${u.points.length}</span></button>`)}</div>
          <p class="small muted" style="margin:0 0 8px">Some results are in a unit that can't be converted to the others, so they have their own chart rather than sharing an axis.</p>` : ""}
        ${pts.length > 1 || hasComponents ? html`<div class="row wrap between trend-controls">
          <div class="segmented" role="group" aria-label="Period">${CLINICAL_PERIODS.map(([d, label]) => html`<button class="${clinicalDays === d ? "active" : ""}" data-action="clinical-period" data-days="${d}" aria-pressed="${clinicalDays === d}">${label}</button>`)}</div>
          ${markerToggle(clinicalMarks().length, showMarkers)}
        </div>` : ""}
        ${hasComponents ? html`<div class="legend chart-legend">${s.components.map((c, i) => html`<span class="${i ? "alt" : ""}">${c.name}</span>`)}</div>`
          : ref || shownPts.some((p) => isFlagged(p.interpretation)) ? html`<div class="legend chart-legend">${ref ? html`<span class="band">Reference range</span>` : ""}${shownPts.some((p) => isFlagged(p.interpretation)) ? html`<span class="flagged">Out of range</span>` : ""}</div>` : ""}
        ${wordsOnly ? html`<p class="small muted" style="margin:0">No chart, because these results are words rather than numbers. Every result is listed below.</p>` : html`<div id="tr-chart"></div>`}
        ${markerList(showMarkers ? clinicalMarks() : [])}
        ${change !== null ? html`<p class="small muted" style="margin-top:8px">${Math.abs(change) < 1e-9 ? "Same as on" : `${change > 0 ? "Up" : "Down"} ${body ? fmtNum(Math.abs(change), 1) : num(Math.abs(change))} ${unit} since`}
          ${fmtDate(measured[0].t)} (${plural(measured.length, "measured result")}).</p>` : ""}
        ${pts.some((p) => p.comparator) ? html`<p class="small muted" style="margin-top:4px">Hollow points are limits, like "below 5": the true value is somewhere past them.</p>` : ""}
        ${pts.some((p) => p.converted) ? html`<p class="small muted" style="margin-top:4px">Some results were reported in another unit and are converted here to ${unit}.</p>` : ""}
      </div>
      <div class="card"><div class="card-head"><h3>All results</h3><span class="badge">${rows.length}</span></div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Date</th><th style="text-align:right">Value</th><th>Flag</th><th>Source</th></tr></thead>
        <tbody>${rows.slice().reverse().map((r) => html`<tr class="clickable" data-action="open" data-id="${r.record_id}">
          <td>${fmtDate(r.t)}</td><td class="num">${r.text}${r.standard ? html`<div class="small muted">${r.standard}</div>` : ""}</td><td>${resultStatus(r.status)}${flagBadge(r.interpretation)}</td><td class="small muted">${r.source || ""}</td></tr>`)}</tbody></table></div></div>`);
    wireDetail(detail);
    if (wordsOnly) return;
    const chartEl = $("#tr-chart", detail);
    // Home readings beside the clinic's, on the same axis: the same measurement, taken elsewhere.
    const home = hasComponents && it.code === BP_PANEL_CODE ? (await Promise.all(HOME_PRESSURE.map(([metric, name], i) =>
      get("/api/biometrics/daily", { profile: profile.id, metric, days: clinicalDays || 3650 }).then((d) => ({ name, alt: i === 1, faint: true, points: d.points.map((p) => ({ t: p.day, v: p.value, label: "Home" })) })).catch(() => null))))
      .filter((line) => line?.points.length) : [];
    if (seq !== drawSeq) return;
    if (home.length) $(".legend", detail)?.insertAdjacentHTML("beforeend", '<span class="faint">Paler lines: home readings, daily</span>');
    const draw = () => {
      if (hasComponents) {
        lineChart(chartEl, [...shownComponents.slice(0, 2).map((c, i) => ({ name: c.name, alt: i === 1, points: c.points })), ...home],
          { unit: s.components[0].unit, label: s.title, height: CHART_HEIGHT, markers: showMarkers ? clinicalMarks() : [] });
      } else {
        lineChart(chartEl, [{ name: s.title, points: shownPts.map((p) => ({ t: p.t, v: p.v, flag: isFlagged(p.interpretation), bound: Boolean(p.comparator),
            prefix: bound(p), label: p.source_name })) }],
          { unit, band: { low: s.ref_low, high: s.ref_high }, label: s.title, height: CHART_HEIGHT, markers: showMarkers ? clinicalMarks() : [],
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
        <button class="btn btn-sm" data-action="readings">Readings</button>
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
    const [s, usual] = await Promise.all([
      get("/api/biometrics/daily", { profile: profile.id, metric: it.metric, days, source: source || undefined }),
      get("/api/insights/baseline", { profile: profile.id, metric: it.metric, days: Math.max(7, Math.min(days, 3650)), source: source || undefined }).catch(() => null),
    ]);
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
    // The big number is one day's, whatever the chart's bars or points are averaged over.
    const word = bucketWord(size);
    const eyebrow = word ? "Latest day" : s.aggregation === "sum" ? "Daily total" : s.aggregation === "last" ? "Daily value" : "Daily average";
    const usualBand = usual?.enough ? { low: cv(usual.low), high: cv(usual.high) } : null;
    const outside = usual?.enough && last && !last.partial && (last.value < usual.low || last.value > usual.high);
    const switches = (usual?.device_changes || []).filter((c) => c.day >= firstDay && c.day <= s.end)
      .map((c) => ({ t: c.day, label: `Device changed from ${deviceName(c.from)} to ${deviceName(c.to)}: earlier numbers aren't comparable` }));
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
        ${word ? html`<p class="tiny faint" style="margin:4px 0 0">The chart shows each period's ${word.toLowerCase().replace(/^\w+ /, "")}.</p>` : ""}
        ${usualBand ? html`<p class="small muted" style="margin:8px 0 0">Your usual range${usual.source ? ` from ${deviceName(usual.source)}` : ""} is ${metricValue(m, usual.low)} to ${metricValue(m, usual.high)}${unit ? ` ${unit}` : ""}, from the ${usual.window_days} days before the last week.${outside ? " The latest day is outside it." : ""}</p>`
          : usual && !usual.enough && s.points.length ? html`<p class="tiny faint" style="margin:8px 0 0">Your usual range appears after two weeks of readings from one device.</p>` : ""}
        <div class="stat-row">
          <div><span>Average</span><b>${metricValue(m, s.stats.mean)}</b></div>
          <div><span>Low</span><b>${metricValue(m, s.stats.min)}</b></div>
          <div><span>High</span><b>${metricValue(m, s.stats.max)}</b></div>
          ${goal ? html`<div><span>Goal met</span><b>${met} of ${complete.length} days</b></div>` : html`<div><span>Days</span><b>${s.stats.count}</b></div>`}
        </div>
        ${goal ? html`<p class="small muted" style="margin:10px 0 0">Goal: ${goalText(goal, { key: it.metric, unit: s.unit })} a day.</p>` : ""}
        ${chartNotes(switches)}
        ${markerList(marks)}
        ${s.left_out ? html`<p class="small muted" style="margin:10px 0 0">${plural(s.left_out, "reading")} left out because ${s.left_out === 1 ? "it can't" : "they can't"} be right.
          <button class="link-btn" data-action="left-out">See ${s.left_out === 1 ? "it" : "them"}</button></p>` : ""}
      </div>
      ${source ? "" : html`<p class="hint">When several devices measure this on the same day, one is chosen per day by your
        <a href="#/settings/wearables">device priority</a>. Pick a source above to see only its data.</p>`}`);
    wireDetail(detail);
    const chartEl = $("#tr-chart", detail);
    const goalLine = goal ? cv(goal.target) : null;
    const draw = () => {
      const digits = s.unit === "h" ? 1 : convert(1, s.unit, it.metric).digits ?? 0;
      if (s.aggregation === "sum") barChart(chartEl, points, { unit: chartUnit, avg: cv(s.stats.mean), label: s.label, digits, goal: goalLine, height: CHART_HEIGHT, markers: [...switches, ...marks],
        format: s.unit === "h" ? (v) => metricValue(m, v) : undefined });
      else lineChart(chartEl, [{ name: s.label, points: points.map((p) => ({ t: p.day, v: p.value, label: p.tipLabel || p.source })) }],
        { unit: chartUnit, label: s.label, goal: goalLine, markers: [...switches, ...marks], digits, height: CHART_HEIGHT, band: usualBand || undefined });
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
          ? "ECGs taken on your watch. The classification is the watch's own and isn't a diagnosis."
          : "Notifications your watch or iPhone sent. They aren't a diagnosis."}</p></div>
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
    // What's on screen stays, dimmed, until the next chart is ready, and that chart fades over rather than drawing itself in.
    if (detail.childElementCount) {
      detail.classList.add("refreshing");
      document.documentElement.classList.add("no-intro");
    } else mount(detail, html`${backBar()}${loading(2)}`);
    // One chart that can't be read costs its own pane, not the whole page.
    try {
      if (selected.kind === "wearable") await drawWearable(detail, selected, seq);
      else if (selected.kind === "signal") await drawSignal(detail, selected, seq);
      else await drawClinical(detail, selected, seq);
    } catch (err) {
      if (seq === drawSeq) mount(detail, html`${backBar()}<div class="card card-pad" role="alert"><p class="muted">${selected.title} couldn't be shown: ${err.message}</p>
        <button class="btn" style="margin-top:10px" data-action="retry-detail">${icon("sync")} Try again</button></div>`);
    }
    if (seq === drawSeq) detail.classList.remove("refreshing");
  };

  const narrow = () => window.matchMedia(NARROW).matches;
  // A period button answers the moment it's pressed, whatever the chart is still waiting for.
  const pressed = (btn) => $$("button", btn.parentElement).forEach((b) => {
    b.classList.toggle("active", b === btn);
    b.setAttribute("aria-pressed", String(b === btn));
  });

  drawTabs();
  drawList();
  layout();
  await drawDetail();
  if (wanted) $(".list-item.selected", el)?.scrollIntoView({ block: "nearest" });

  $("#tr-filter", el).addEventListener("input", (e) => { filter = e.target.value.trim().toLowerCase(); drawList(); });

  const off = onAction(el, {
    "retry-detail": () => drawDetail(),
    "left-out": () => openReadings(profile, { metric: selected.metric, label: selected.title, leftOut: true }, redraw),
    "left-out-all": seeLeftOut,
    readings: () => openReadings(profile, { metric: selected.metric, label: selected.title }, redraw),
    topic: ({ topic: t }) => {
      if (t === topic) { showDetail = false; layout(); return; }
      topic = t; filter = ""; $("#tr-filter", el).value = ""; source = ""; unitChoice = 0; cmp.b = null; cmp.lag = 0;
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
      if (next.key !== selected?.key) { source = ""; unitChoice = 0; cmp.b = null; cmp.lag = 0; }
      selected = next;
      // On a phone the chart takes over from the list, so Back has to come back to the list.
      if (narrow() && !nativeNav && !showDetail) {
        history.replaceState(null, "", `#/trends/${topic}`);
        history.pushState(null, "", hashFor(next));
        chartPushed = true;
      } else address();
      showDetail = true;
      drawList(); layout();
      if (narrow()) scrollToTop();
      return drawDetail();
    },
    back: () => {
      if (chartPushed) { chartPushed = false; history.back(); return; }
      showDetail = false; layout(); scrollToTop();
    },
    range: ({ days: d }, btn) => { days = Number(d); saveDays(days); pressed(btn); return drawDetail(); },
    unit: ({ index }) => { unitChoice = Number(index) || 0; return drawDetail(); },
    "clinical-period": ({ days: d }, btn) => { clinicalDays = Number(d) || 0; pressed(btn); return drawDetail(); },
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
