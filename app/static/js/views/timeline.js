// Timeline: one health history in order. Visits, results and diagnoses from every institution, with what was logged
// and measured along the way: symptoms and doses from the Journal, ECGs and alerts from devices, and (by filter)
// workouts and daily check-ins.
import { get } from "../api.js";
import { openRecord } from "./record-detail.js";
import { openWorkout, duration, workoutIcon } from "./workouts.js";
import { moodLabel } from "./journal.js";
import { fmtMeasure } from "../units.js";
import {
  $, CATEGORY_ICON, debounce, emptyState, flagBadge, html, icon, loading, mount, onAction, parseDate,
} from "../ui.js";

const FILTERS = [
  ["", "Everything"], ["encounters", "Visits"], ["labs", "Labs"], ["conditions", "Diagnoses"], ["medications", "Medications"],
  ["procedures,reports", "Procedures & imaging"], ["immunizations", "Vaccines"], ["notes", "Notes"],
  ["journal,checkins", "Journal"], ["signals", "Device alerts"], ["workouts", "Workouts"],
];
const JOURNAL_ICONS = { symptoms: "stethoscope", medication: "pill", notes: "note", cycle: "heart", stateOfMind: "sparkles", other: "list" };

// The local day (a check-in at 10:48 PM belongs to that evening, not to the next day in UTC).
function dayKey(r) {
  const d = parseDate(r.effective_at);
  return d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` : "unknown";
}

function describe(r) {
  if (r.kind === "workout") {
    const bits = [r.distance_m ? fmtMeasure(r.distance_m, "m", "distance_walking_running") : null, duration(r.duration_s)].filter(Boolean).join(" · ");
    return html`Workout · <b>${r.title}</b> ${bits}`;
  }
  if (r.kind === "journal") {
    if (r.category === "stateOfMind") return html`Checked in · <b>${r.value != null ? moodLabel(r.value) : "Mood"}</b>${r.note ? html` <span class="muted">“${r.note}”</span>` : ""}`;
    const verb = r.category === "medication" ? "Took" : r.category === "symptoms" ? "Logged" : "";
    return html`${verb ? `${verb} · ` : ""}<b>${r.title}</b>${r.value_label ? html` <span class="badge">${r.value_label}</span>` : ""}${r.note ? html` <span class="muted">${r.note}</span>` : ""}`;
  }
  if (r.kind === "signal") return html`<b>${r.title}</b>${r.value_label && r.value_label !== "Occurred" ? ` · ${r.value_label}` : ""}`;
  switch (r.category) {
    case "labs": return html`<b>${r.title}</b> ${r.value_text || ""} ${flagBadge(r.interpretation)}`;
    case "conditions": return html`Diagnosed <b>${r.title}</b>`;
    case "medications": return html`Prescribed <b>${r.title}</b>`;
    case "immunizations": return html`Vaccinated · <b>${r.title}</b>`;
    case "allergies": return html`Allergy recorded · <b>${r.title}</b>`;
    case "procedures": return html`Procedure · <b>${r.title}</b>`;
    case "reports": return html`Report · <b>${r.title}</b>`;
    case "notes": return html`Note · <b>${r.title}</b>`;
    case "encounters": return html`<b>${r.title}</b>${(r.details?.clinicians || [])[0] ? html` with ${r.details.clinicians[0]}` : ""}`;
    default: return html`<b>${r.title}</b>`;
  }
}

export async function render({ el, state, navigate }) {
  const profile = state.profile;
  let filter = "";
  let q = "";
  let items = [];
  let nextBefore = null;
  const expanded = new Set();

  mount(el, html`
    <div class="page-head"><div><h1>Timeline</h1><p>Your health history in order: every institution, with what you logged and your devices noticed along the way.</p></div>
      <div class="search" style="min-width:240px">${icon("search")}<input class="input" id="tl-q" placeholder="Filter timeline…"></div></div>
    <div class="chips" id="tl-filters" style="margin-bottom:16px"></div>
    <div class="card card-pad" id="tl-body">${loading(4)}</div>`);

  const drawFilters = () => mount($("#tl-filters", el), FILTERS.map(([k, label]) =>
    html`<button class="chip ${filter === k ? "active" : ""}" data-action="filter" data-key="${k}">${label}</button>`));

  const draw = () => {
    const body = $("#tl-body", el);
    if (!items.length) {
      mount(body, emptyState("Nothing on your timeline yet", "Connect a health system to build your history."));
      return;
    }
    const groups = new Map();
    for (const r of items) {
      const k = dayKey(r);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    // Within a day show the visit first, then labs collapsed.
    mount(body, html`${[...groups.entries()].map(([day, evs]) => {
      const d = parseDate(day);
      const labs = evs.filter((e) => e.category === "labs");
      const other = evs.filter((e) => e.category !== "labs");
      const flagged = labs.filter((l) => l.interpretation && l.interpretation !== "normal");
      return html`<div class="timeline-day">
        <div class="timeline-date">${d ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "Unknown"}
          <small>${d ? d.getFullYear() : ""}</small></div>
        <div>
          ${other.map((e) => html`<div class="ev ${e.kind ? `ev-${e.kind}` : ""}" data-action="open" data-id="${e.id}" data-kind="${e.kind || "record"}" data-type="${e.event_type || ""}">
            <span class="ev-icon">${icon(e.kind === "workout" ? workoutIcon(e.title) : e.kind === "journal" ? JOURNAL_ICONS[e.category] || "book"
              : e.kind === "signal" ? (e.event_type === "ecg" ? "heart" : "bell") : CATEGORY_ICON[e.category] || "records")}</span>
            <div class="grow"><div>${describe(e)}</div><div class="small muted">${e.kind ? `${e.kind === "journal" && e.manual ? "Journal" : e.source_name || ""}${e.kind !== "record" ? ` · ${new Date(e.effective_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}` : ""}` : e.source_name || ""}</div></div></div>`)}
          ${labs.length > 3 && !expanded.has(day) ? html`<div class="ev" data-action="labs" data-day="${day}"><span class="ev-icon">${icon("flask")}</span>
              <div class="grow"><div><b>${labs.length} lab results</b>${flagged.length ? html` · <span class="flag-high">${flagged.length} out of range</span>` : ""}</div>
              <div class="small muted">${labs.slice(0, 5).map((l) => l.title).join(", ")}${labs.length > 5 ? "…" : ""}</div></div></div>`
            : labs.map((e) => html`<div class="ev" data-action="open" data-id="${e.id}"><span class="ev-icon">${icon("flask")}</span>
              <div class="grow"><div>${describe(e)}</div><div class="small muted">${e.source_name || ""}</div></div></div>`)}
        </div></div>`;
    })}
    ${nextBefore ? html`<div style="text-align:center;padding-top:14px"><button class="btn" data-action="more">Show earlier</button></div>` : ""}`);
  };

  const load = async (append = false) => {
    const res = await get("/api/timeline", { profile: profile.id, categories: filter || undefined, q: q || undefined,
                                             before: append ? nextBefore : undefined, limit: 150 });
    items = append ? items.concat(res.items) : res.items;
    nextBefore = res.next_before;
    draw();
  };

  drawFilters();
  await load();
  $("#tl-q", el).addEventListener("input", debounce((e) => { q = e.target.value.trim(); load(); }, 300));

  return onAction(el, {
    filter: ({ key }) => { filter = key; drawFilters(); mount($("#tl-body", el), loading(3)); return load(); },
    open: ({ id, kind, type }) => (kind === "workout" ? openWorkout(profile, id) : kind === "journal" ? navigate("#/journal")
      : kind === "signal" ? navigate(`#/trends?event=${encodeURIComponent(type)}`) : openRecord(id)),
    labs: ({ day }) => { expanded.add(day); draw(); },
    more: () => load(true),
  });
}
