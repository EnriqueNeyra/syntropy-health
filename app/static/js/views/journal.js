// The Journal: how the person feels, day by day. A quick daily check-in (mood, energy, stress) sits on top, the last
// 30 days of it beside it, and below everything logged: check-ins, symptoms, notes, doses and cycle tracking, whether
// logged here or in Apple Health on the iPhone. What devices measure or warn about (ECGs, heart notifications) is in
// Trends; workouts have their own page.

import { del, get, post, put } from "../api.js";
import { setToolbar } from "../app.js";
import { $, confirmDialog, debounce, emptyState, fmtNum, html, icon, loading, modal, mount, onAction, plural, raw, toast } from "../ui.js";
import { rangeHash, rangeLabel, rangePicker, rangeQuery, readRange, saveRange, wireRange } from "../range.js";

const RANGE_KEY = "syntropy-journal-range";
const PAGE = 200;
const STRIP_DAYS = 30;

export const CATEGORY_LABELS = { notes: "Notes", symptoms: "Symptoms", stateOfMind: "Check-ins & mood", medication: "Medication",
  cycle: "Cycle", other: "Other" };
const CATEGORY_ICONS = { notes: "note", symptoms: "stethoscope", stateOfMind: "sparkles", medication: "pill", cycle: "heart", other: "list" };

// Symptom names and ids match Apple Health's, so entries logged here and on the iPhone line up.
const SYMPTOMS = [
  ["headache", "Headache"], ["fatigue", "Fatigue"], ["nausea", "Nausea"], ["fever", "Fever"], ["coughing", "Coughing"],
  ["sore_throat", "Sore Throat"], ["sinus_congestion", "Sinus Congestion"], ["runny_nose", "Runny Nose"], ["dizziness", "Dizziness"],
  ["generalized_body_ache", "Body and Muscle Ache"], ["lower_back_pain", "Lower Back Pain"], ["abdominal_cramps", "Abdominal Cramps"],
  ["bloating", "Bloating"], ["heartburn", "Heartburn"], ["diarrhea", "Diarrhea"], ["constipation", "Constipation"], ["vomiting", "Vomiting"],
  ["chills", "Chills"], ["shortness_of_breath", "Shortness of Breath"], ["chest_tightness_or_pain", "Chest Tightness or Pain"],
  ["rapid_pounding_or_fluttering_heartbeat", "Rapid, Pounding, or Fluttering Heartbeat"], ["wheezing", "Wheezing"], ["acne", "Acne"],
  ["hot_flashes", "Hot Flashes"], ["night_sweats", "Night Sweats"], ["memory_lapse", "Memory Lapse"], ["mood_changes", "Mood Changes"],
  ["sleep_changes", "Sleep Changes"], ["appetite_changes", "Appetite Changes"],
];
const SEVERITY = [[2, "Mild"], [3, "Moderate"], [4, "Severe"]];
// Mood uses Apple Health's State of Mind scale (-1 to 1), so check-ins and moods logged on the iPhone chart together.
const MOODS = [[-1, "Very unpleasant"], [-0.5, "Unpleasant"], [0, "Neutral"], [0.5, "Pleasant"], [1, "Very pleasant"]];
const ENERGY = [[1, "Drained"], [2, "Low"], [3, "Okay"], [4, "Good"], [5, "Energized"]];
const STRESS = [[1, "Calm"], [2, "A little"], [3, "Some"], [4, "High"], [5, "Very high"]];
const FLOW = [[2, "Light"], [3, "Medium"], [4, "Heavy"], [0, "Spotting"]];

// What can be logged. Each kind turns its form into an entry, and back again for editing.
const KINDS = [
  { id: "checkin", label: "Check-in", icon: "sparkles" },
  { id: "symptom", label: "Symptom", icon: "stethoscope" },
  { id: "note", label: "Note", icon: "note" },
  { id: "medication", label: "Medication", icon: "pill" },
  { id: "period", label: "Period", icon: "heart" },
  { id: "other", label: "Other", icon: "list" },
];

function kindOf(e) {
  if (e.category === "notes") return "note";
  if (e.category === "symptoms") return "symptom";
  if (e.category === "stateOfMind") return "checkin";
  if (e.category === "medication") return "medication";
  if (e.category === "cycle" && ["menstrual_flow", "intermenstrual_bleeding"].includes(e.event_type)) return "period";
  return "other";
}

/** 1-5 for a mood on the -1..1 scale (State of Mind values from the iPhone are continuous). */
const moodLevel = (v) => Math.max(1, Math.min(5, Math.round((Number(v) + 1) * 2) + 1));
export const moodLabel = (v) => MOODS[moodLevel(v) - 1][1];
const labelFor = (list, v) => list.find(([k]) => k === v)?.[1];

/** A face for each mood level, drawn to match the icon set. */
function face(level) {
  const mouth = ["M8 17c1.2-2 6.8-2 8 0", "M8.5 16.2c1.4-1 5.6-1 7 0", "M8.5 15.5h7", "M8.5 14.8c1.4 1 5.6 1 7 0", "M8 14c1.2 2.4 6.8 2.4 8 0"][level - 1];
  return raw(`<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
    <circle cx="12" cy="12" r="10"/><path d="M9 9.5h.01M15 9.5h.01"/><path d="${mouth}"/></svg>`);
}

const pad = (n) => String(n).padStart(2, "0");
/** An ISO time as the value of a datetime-local field (local time). */
function localInput(iso) {
  const d = iso ? new Date(iso) : new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const dayKey = (iso) => { const d = new Date(iso); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
function dayLabel(iso) {
  const d = new Date(iso), today = new Date();
  const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
  if (dayKey(d) === dayKey(today)) return "Today";
  if (dayKey(d) === dayKey(yesterday)) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}
const timeOf = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

/** "Energy: Good · Stress: Calm" for a check-in. */
function checkinDetail(e) {
  const d = e.metadata || {};
  return [d.energy && `Energy: ${labelFor(ENERGY, d.energy)}`, d.stress && `Stress: ${labelFor(STRESS, d.stress)}`].filter(Boolean).join(" · ");
}

export async function render({ el, params, state }) {
  const profile = state.profile;
  let range = readRange(params, RANGE_KEY, "90");
  let category = CATEGORY_LABELS[params.get("category")] ? params.get("category") : "";
  let q = "";
  let limit = PAGE;
  let res = null;
  let recent = null;       // the last 30 days of check-ins and moods, for today's card and the strips

  const load = () => get("/api/biometrics/events", {
    profile: profile.id, view: "journal", ...rangeQuery(range), category: category || undefined, q: q || undefined, limit,
  });
  const loadRecent = () => get("/api/biometrics/events", { profile: profile.id, view: "journal", days: STRIP_DAYS, limit: 2000 });

  const syncAddress = () => {
    const parts = [rangeHash(range), category ? `category=${category}` : ""].filter(Boolean);
    history.replaceState(null, "", `#/journal${parts.length ? `?${parts.join("&")}` : ""}`);
  };

  // In the iPhone app, New entry is the title bar's +.
  const appNew = setToolbar([{ id: "new", title: "New Entry", symbol: "plus", run: () => openEntry(profile, null, reloadAll, [...(recent?.events || []), ...(res?.events || [])]) }]);
  mount(el, html`<div class="page-head"><div><h1>Journal</h1><p>How you've been feeling, day by day.</p></div>
      ${appNew ? "" : html`<button class="btn btn-primary" data-action="new">${icon("plus")} New entry</button>`}</div>
    <div class="jr-top"><div class="card" id="jr-checkin">${loading(3)}</div><div class="card" id="jr-month">${loading(3)}</div></div>
    <div class="toolbar">
      <div id="jr-range"></div>
      <div class="search toolbar-search">${icon("search")}<input class="input" id="jr-q" placeholder="Search the journal" aria-label="Search the journal"></div>
    </div>
    <div class="chips" id="jr-cats" style="margin-bottom:14px"></div>
    <div id="jr-body">${loading(3)}</div>`);

  // ------------------------------------------------------------------ today's check-in
  let draft = null;        // what's picked on the card but not saved yet
  const todays = () => recent.events.find((e) => e.event_type === "check_in" && e.manual && dayKey(e.start_date) === dayKey(new Date()));

  const drawCheckin = () => {
    const saved = todays();
    const d = draft || (saved ? { mood: saved.value, energy: saved.metadata?.energy, stress: saved.metadata?.stress, note: saved.note || "" }
      : { mood: null, energy: null, stress: null, note: "" });
    draft = d;
    const scale = (name, list, current) => html`<div class="jr-scale" role="radiogroup" aria-label="${name}">${list.map(([v, label]) => html`
      <button type="button" role="radio" class="${current === v ? "active" : ""}" aria-checked="${current === v}" data-action="pick" data-field="${name.toLowerCase()}" data-value="${v}">${label}</button>`)}</div>`;
    mount($("#jr-checkin", el), html`
      <div class="card-head"><h3>How are you feeling?</h3>
        <span class="small muted">${saved ? `Checked in at ${timeOf(saved.start_date)}` : "Today"}</span></div>
      <div class="card-body jr-checkin">
        <div class="jr-moods" role="radiogroup" aria-label="Mood">${MOODS.map(([v, label], i) => html`
          <button type="button" role="radio" class="jr-mood m${i + 1} ${d.mood === v ? "active" : ""}" aria-checked="${d.mood === v}"
            data-action="pick" data-field="mood" data-value="${v}">${face(i + 1)}<span>${label}</span></button>`)}</div>
        <div class="jr-scales">
          <div><div class="label">Energy</div>${scale("Energy", ENERGY, d.energy)}</div>
          <div><div class="label">Stress</div>${scale("Stress", STRESS, d.stress)}</div>
        </div>
        <textarea class="input" id="jr-quick-note" rows="2" maxlength="4000" placeholder="Anything on your mind? What helped, what didn't…">${d.note}</textarea>
        <div class="row between wrap" style="gap:10px">
          <div class="row wrap jr-quick" style="gap:6px">
            <button class="btn btn-sm btn-ghost" data-action="new" data-kind="symptom">${icon("stethoscope")} Symptom</button>
            <button class="btn btn-sm btn-ghost" data-action="new" data-kind="medication">${icon("pill")} Medication</button>
            <button class="btn btn-sm btn-ghost" data-action="new" data-kind="period">${icon("heart")} Period</button>
          </div>
          <button class="btn btn-primary" data-action="save-checkin" ${d.mood == null ? "disabled" : ""}>${saved ? "Update check-in" : "Save check-in"}</button>
        </div>
      </div>`);
  };

  // ------------------------------------------------------------------ the last 30 days
  const drawMonth = () => {
    const days = [];
    for (let i = STRIP_DAYS - 1; i >= 0; i--) { const d = new Date(); d.setDate(d.getDate() - i); days.push(dayKey(d)); }
    const by = Object.fromEntries(days.map((k) => [k, { mood: [], energy: [], stress: [] }]));
    const symptoms = {};
    for (const e of recent.events) {
      const k = dayKey(e.start_date);
      if (!by[k]) continue;
      if (e.category === "stateOfMind" && e.value != null) by[k].mood.push(moodLevel(e.value));
      if (e.metadata?.energy) by[k].energy.push(e.metadata.energy);
      if (e.metadata?.stress) by[k].stress.push(e.metadata.stress);
      if (e.category === "symptoms") symptoms[e.name] = (symptoms[e.name] || 0) + 1;
    }
    const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
    const all = (field) => days.flatMap((k) => by[k][field]);
    const strip = (field, labels, cls) => html`<div class="jr-strip-row"><span class="label">${field[0].toUpperCase() + field.slice(1)}</span>
      <div class="jr-strip ${cls}">${days.map((k) => {
        const v = avg(by[k][field]);
        const level = v == null ? 0 : Math.round(v);
        const when = new Date(`${k}T12:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
        return html`<span class="lvl${level}" title="${when}: ${level ? labels[level - 1][1] : "nothing logged"}"></span>`;
      })}</div></div>`;
    const logged = days.filter((k) => by[k].mood.length || by[k].energy.length || by[k].stress.length).length;
    const top = Object.entries(symptoms).sort((a, b) => b[1] - a[1]).slice(0, 3);
    const moodAvg = avg(all("mood")), energyAvg = avg(all("energy")), stressAvg = avg(all("stress"));
    mount($("#jr-month", el), html`
      <div class="card-head"><h3>Last 30 days</h3><span class="small muted">${logged ? plural(logged, "day") + " logged" : "Nothing logged yet"}</span></div>
      <div class="card-body stack" style="gap:10px">
        ${strip("mood", MOODS, "mood")}${strip("energy", ENERGY, "energy")}${strip("stress", STRESS, "stress")}
        <div class="jr-sum">
          <div><span>Mood</span><b>${moodAvg == null ? "—" : MOODS[Math.round(moodAvg) - 1][1]}</b></div>
          <div><span>Energy</span><b>${energyAvg == null ? "—" : ENERGY[Math.round(energyAvg) - 1][1]}</b></div>
          <div><span>Stress</span><b>${stressAvg == null ? "—" : STRESS[Math.round(stressAvg) - 1][1]}</b></div>
        </div>
        ${top.length ? html`<div class="small muted">Most logged symptoms: ${top.map(([n, c]) => `${n} (${c})`).join(", ")}</div>` : ""}
      </div>
      <div id="jr-patterns"></div>`);
    drawPatterns(logged);
  };

  // How check-ins and symptoms line up with sleep and training, from the same insights as the Overview.
  let patterns = null;
  const drawPatterns = async (logged) => {
    if (!patterns) patterns = get("/api/insights", { profile: profile.id }).then((r) => r.insights.filter((i) => i.kind === "link")).catch(() => []);
    const list = await patterns;
    const box = $("#jr-patterns", el);
    if (!box) return;
    mount(box, list.length ? html`<div class="jr-patterns"><div class="label">Patterns</div>
      ${list.slice(0, 3).map((i) => html`<div class="jr-pattern"><b>${i.title}</b><span class="small muted">${i.text}</span>
        ${i.link ? html`<a class="small" href="${i.link}">Compare ${icon("arrowRight")}</a>` : ""}</div>`)}</div>`
      : logged >= 5 ? "" : html`<div class="jr-patterns"><div class="small muted">Check in on a few more days and patterns with your sleep and training will show here.</div></div>`);
  };

  // ------------------------------------------------------------------ everything logged
  const drawControls = () => mount($("#jr-range", el), rangePicker(range));

  const draw = () => {
    const cats = {};
    for (const row of res.summary) cats[row.category] = (cats[row.category] || 0) + row.count;
    $("#jr-cats", el).hidden = !Object.keys(cats).length && !category;
    mount($("#jr-cats", el), html`
      <button class="chip ${category ? "" : "active"}" data-action="cat" data-cat="">All</button>
      ${Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([c, n]) => html`<button class="chip ${c === category ? "active" : ""}" data-action="cat" data-cat="${c}">
        ${CATEGORY_LABELS[c] || c} <span class="n">${fmtNum(n, 0)}</span></button>`)}
      ${category && !cats[category] ? html`<button class="chip active" data-action="cat" data-cat="${category}">${CATEGORY_LABELS[category]} <span class="n">0</span></button>` : ""}`);

    const events = res.events;
    const days = [];
    for (const e of events) {
      const k = dayKey(e.start_date);
      if (days.at(-1)?.key !== k) days.push({ key: k, label: dayLabel(e.start_date), items: [] });
      days.at(-1).items.push(e);
    }
    const filtered = category || q;
    mount($("#jr-body", el), events.length ? html`<div class="card">${days.map((d) => html`
        <div class="list-head"><b>${d.label}</b><span>${plural(d.items.length, "entry", "entries")}</span></div>
        <div class="list">${d.items.map(entryRow)}</div>`)}
        ${events.length >= limit ? html`<div class="card-body row between small muted"><span>Showing the latest ${fmtNum(events.length, 0)}</span>
          <button class="btn btn-sm" data-action="more">Show more</button></div>` : ""}
      </div>`
      : html`<div class="card">${filtered
        ? emptyState("Nothing matches", `No entries ${rangeLabel(range).toLowerCase() === "all time" ? "" : `in ${rangeLabel(range).toLowerCase()} `}match these filters.`)
        : emptyState("Nothing logged yet", "Check in above, or log a symptom, a dose or a note. Moods and symptoms you log in Apple Health on your iPhone show up here too.")}</div>`);
  };

  drawControls();
  [res, recent] = await Promise.all([load(), loadRecent()]);
  drawCheckin();
  drawMonth();
  draw();

  let seq = 0;
  const reload = async () => {
    syncAddress();
    const n = ++seq;
    const next = await load();
    if (n !== seq) return;   // a newer choice is already loading
    res = next;
    draw();
  };
  const reloadAll = async () => {
    [res, recent] = await Promise.all([load(), loadRecent()]);
    draft = null;
    drawCheckin(); drawMonth(); draw();
  };
  const onRange = wireRange(el, () => range, (r) => { range = r; limit = PAGE; saveRange(range, RANGE_KEY); drawControls(); return reload(); });
  $("#jr-q", el).addEventListener("input", debounce((e) => { q = e.target.value.trim(); limit = PAGE; reload(); }, 250));
  el.addEventListener("input", (e) => { if (e.target.id === "jr-quick-note" && draft) draft.note = e.target.value; });
  const byId = (id) => res.events.find((e) => e.id === id);

  return onAction(el, {
    range: onRange,
    cat: ({ cat }) => { category = cat; limit = PAGE; return reload(); },
    more: () => { limit += PAGE; return reload(); },
    new: ({ kind }) => openEntry(profile, null, reloadAll, [...recent.events, ...res.events], kind),
    entry: ({ id }) => { const e = byId(id); if (e) (e.manual ? openEntry(profile, e, reloadAll, res.events) : openDeviceEntry(e)); },
    pick: ({ field, value }) => {
      draft[field] = Number(value);
      draft.note = $("#jr-quick-note", el)?.value ?? draft.note;
      drawCheckin();
    },
    "save-checkin": async (_, btn) => {
      const saved = todays();
      const body = checkinEntry({ mood: draft.mood, energy: draft.energy, stress: draft.stress,
        note: $("#jr-quick-note", el).value, start: saved ? saved.start_date : new Date().toISOString() });
      btn.disabled = true;
      try {
        if (saved) await put(`/api/journal/${saved.id}?profile=${encodeURIComponent(profile.id)}`, body);
        else await post("/api/journal", body, { profile: profile.id });
        toast(saved ? "Check-in updated" : "Checked in", "good");
        await reloadAll();
      } catch (err) { toast(err.message, "bad"); btn.disabled = false; }
    },
  });
}

/** The entry saved for a check-in. */
function checkinEntry({ mood, energy, stress, note, start }) {
  return { event_type: "check_in", category: "stateOfMind", name: "Check-in", start, value: mood,
    value_label: moodLabel(mood), note: (note || "").trim() || null,
    details: { energy: energy || null, stress: stress || null } };
}

function entryRow(e) {
  const detail = e.event_type === "check_in" ? checkinDetail(e) : "";
  const level = e.category === "stateOfMind" && e.value != null ? moodLevel(e.value) : 0;
  return html`<button class="list-item clickable jr-row" data-action="entry" data-id="${e.id}">
    <span class="ev-icon jr-icon ${e.category} ${level ? `m${level}` : ""}">${level ? face(level) : icon(CATEGORY_ICONS[e.category] || "list")}</span>
    <div class="grow" style="min-width:0">
      <div class="jr-title"><span class="title">${e.name === "State of Mind" ? "Mood" : e.name}</span>
        ${e.category === "stateOfMind" && e.value != null ? html`<span class="badge">${moodLabel(e.value)}</span>`
          : e.value_label ? html`<span class="badge">${e.value_label}</span>` : ""}</div>
      ${detail ? html`<div class="small muted">${detail}</div>` : ""}
      ${e.note ? html`<div class="jr-note">${e.note}</div>` : ""}
      <div class="meta">${timeOf(e.start_date)} · ${e.manual ? "Logged here" : e.source_name || "Device"}</div>
    </div>
    <span class="small muted hide-narrow">${CATEGORY_LABELS[e.category] || e.category}</span>
  </button>`;
}

/** What a device sent, with its details (State of Mind labels and so on). Read only. */
function openDeviceEntry(e) {
  const meta = Object.entries(e.metadata || {}).filter(([, v]) => v !== "" && v != null);
  modal({ title: e.name === "State of Mind" ? "Mood" : e.name, drawer: false, body: html`<div class="stack">
    <div class="grid grid-2">
      <div><div class="small muted">When</div><b>${new Date(e.start_date).toLocaleString()}</b></div>
      ${e.end_date && e.end_date !== e.start_date ? html`<div><div class="small muted">Until</div><b>${new Date(e.end_date).toLocaleString()}</b></div>` : ""}
      ${e.value_label || (e.category === "stateOfMind" && e.value != null) ? html`<div><div class="small muted">Value</div>
        <b>${e.category === "stateOfMind" && e.value != null ? moodLabel(e.value) : e.value_label}</b></div>` : ""}
      <div><div class="small muted">Type</div><b>${CATEGORY_LABELS[e.category] || e.category}</b></div>
      <div><div class="small muted">Source</div><b>${e.source_name || "Device"}</b></div>
    </div>
    ${meta.length ? html`<dl class="kv">${meta.map(([k, v]) => html`<dt>${k.replace(/_/g, " ")}</dt><dd>${Array.isArray(v) ? v.join(", ") : String(v)}</dd>`)}</dl>` : ""}
    <p class="hint">Sent by ${e.source_name || "a device"}. Change it there (for Apple Health, in the Health app); entries you log here can be edited.</p>
  </div>` });
}

// The person's current medications from their health records, offered as one-tap choices when logging a dose.
let activeMeds = null;
async function medicationChips(profile, form, recentNames) {
  if (activeMeds?.profileId !== profile.id) {
    const summary = await get("/api/summary", { profile: profile.id }).catch(() => null);
    activeMeds = { profileId: profile.id, list: (summary?.active_medications || []).map((m) => ({ name: m.title, dose: doseOf(m.title) })) };
  }
  const box = form.querySelector("#jr-med-chips");
  if (!box) return;
  const seen = new Set(activeMeds.list.map((m) => m.name.toLowerCase()));
  const items = [...activeMeds.list, ...recentNames.filter((n) => !seen.has(n.toLowerCase())).map((n) => ({ name: n }))].slice(0, 8);
  mount(box, items.length ? html`${activeMeds.list.length ? html`<span class="tiny faint">Your medications:</span>` : ""}${items.map((m) =>
    html`<button type="button" class="chip" data-med="${m.name}" data-dose="${m.dose || ""}">${icon("pill")} ${m.name}</button>`)}` : "");
  box.onclick = (e) => {
    const b = e.target.closest("[data-med]");
    if (!b) return;
    form.querySelector("[name=med]").value = b.dataset.med;
    const dose = form.querySelector("[name=dose]");
    if (b.dataset.dose && !dose.value) dose.value = b.dataset.dose;
    box.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === b));
  };
}
/** "Lisinopril 10 MG Oral Tablet" → "10 mg". */
function doseOf(title) {
  const m = /(\d+(?:\.\d+)?)\s*(MG|MCG|G|ML|UNT|IU|%)\b/i.exec(title || "");
  return m ? `${m[1]} ${m[2].toLowerCase()}` : "";
}

/** The form for a new entry, or for changing one logged here. */
function openEntry(profile, existing, onSaved, recent, startKind) {
  let kind = existing ? kindOf(existing) : (startKind || "checkin");
  const medNames = [...new Set(recent.filter((e) => e.category === "medication").map((e) => e.name))];
  const m = modal({
    title: existing ? "Edit entry" : "New entry",
    body: html`<form id="jr-form" class="jr-form"></form>`,
    footer: html`${existing ? html`<button class="btn btn-ghost btn-danger" data-delete style="margin-right:auto">${icon("trash")} Delete</button>` : ""}
      <button class="btn" data-cancel type="button">Cancel</button><button class="btn btn-primary" data-save>${existing ? "Save" : "Add to journal"}</button>`,
  });
  const form = m.el.querySelector("#jr-form");
  const v = existing || {};

  const choice = (name, options, current) => html`<div class="seg seg-wrap" role="radiogroup">${options.map(([val, label]) => html`
    <label class="${String(val) === String(current) ? "active" : ""}"><input type="radio" name="${name}" value="${val}" ${String(val) === String(current) ? "checked" : ""}>${label}</label>`)}</div>`;

  const fields = () => {
    if (kind === "checkin") {
      const mood = v.value != null ? MOODS[moodLevel(v.value) - 1][0] : 0;
      return html`<div class="field"><span class="label">Mood</span>${choice("mood", MOODS, mood)}</div>
        <div class="field"><span class="label">Energy <span class="faint">(optional)</span></span>${choice("energy", ENERGY, v.metadata?.energy ?? "")}</div>
        <div class="field"><span class="label">Stress <span class="faint">(optional)</span></span>${choice("stress", STRESS, v.metadata?.stress ?? "")}</div>`;
    }
    if (kind === "symptom") {
      const known = SYMPTOMS.some(([id]) => id === v.event_type);
      return html`<div class="field"><label class="label" for="jr-symptom">Symptom</label>
          <select class="input" id="jr-symptom" name="symptom">${SYMPTOMS.map(([id, label]) => html`<option value="${id}" ${id === (v.event_type || "headache") ? "selected" : ""}>${label}</option>`)}
            <option value="custom" ${existing && !known ? "selected" : ""}>Something else…</option></select></div>
        <div class="field" id="jr-custom-wrap" ${existing && !known ? "" : "hidden"}><label class="label" for="jr-custom">What is it?</label>
          <input class="input" id="jr-custom" name="custom" maxlength="120" value="${existing && !known ? v.name : ""}" placeholder="e.g. Itchy eyes"></div>
        <div class="field"><span class="label">How bad</span>${choice("severity", SEVERITY, v.value ?? 2)}</div>`;
    }
    if (kind === "medication") {
      return html`<div class="field"><label class="label" for="jr-med">Medication</label>
          <input class="input" id="jr-med" name="med" required maxlength="120" list="jr-meds" value="${v.name || ""}" placeholder="e.g. Ibuprofen">
          <datalist id="jr-meds">${medNames.map((n) => html`<option value="${n}"></option>`)}</datalist>
          <div class="chips jr-med-chips" id="jr-med-chips"></div></div>
        <div class="field"><label class="label" for="jr-dose">Dose <span class="faint">(optional)</span></label>
          <input class="input" id="jr-dose" name="dose" maxlength="200" value="${v.value_label || ""}" placeholder="e.g. 200 mg"></div>`;
    }
    if (kind === "period") {
      const current = v.event_type === "intermenstrual_bleeding" ? 0 : (v.value ?? 3);
      return html`<div class="field"><span class="label">Flow</span>${choice("flow", FLOW, current)}</div>`;
    }
    if (kind === "other") {
      return html`<div class="field"><label class="label" for="jr-name">What happened?</label>
          <input class="input" id="jr-name" name="name" required maxlength="120" value="${v.name || ""}" placeholder="e.g. Coffee, Sauna, Allergy shot"></div>
        <div class="field"><label class="label" for="jr-detail">Detail <span class="faint">(optional)</span></label>
          <input class="input" id="jr-detail" name="detail" maxlength="200" value="${v.value_label || ""}" placeholder="e.g. 2 cups"></div>`;
    }
    return "";
  };

  const draw = () => {
    const noteValue = form.querySelector("[name=note]")?.value ?? v.note ?? "";
    const whenValue = form.querySelector("[name=when]")?.value ?? localInput(v.start_date);
    mount(form, html`
      ${existing ? "" : html`<div class="jr-kinds" role="tablist">${KINDS.map((k) => html`
        <button type="button" role="tab" class="jr-kind ${k.id === kind ? "active" : ""}" data-kind="${k.id}" aria-selected="${k.id === kind}">${icon(k.icon)}<span>${k.label}</span></button>`)}</div>`}
      ${fields()}
      <div class="field"><label class="label" for="jr-when">When</label>
        <input class="input" id="jr-when" name="when" type="datetime-local" required value="${whenValue}" max="${localInput()}"></div>
      <div class="field"><label class="label" for="jr-note">${kind === "note" ? "Note" : html`Note <span class="faint">(optional)</span>`}</label>
        <textarea class="input" id="jr-note" name="note" rows="${kind === "note" ? 5 : 3}" maxlength="4000" ${kind === "note" ? "required" : ""}
          placeholder="${kind === "note" ? "What's on your mind? How you slept, what you ate, anything worth remembering…" : "Anything that helps later: triggers, what helped, context"}">${noteValue}</textarea></div>`);
    if (kind === "medication" && !existing) medicationChips(profile, form, medNames);
  };
  draw();

  form.addEventListener("click", (e) => {
    const k = e.target.closest("[data-kind]");
    if (k) { kind = k.dataset.kind; draw(); form.querySelector("input:not([type=radio]), select, textarea")?.focus(); }
  });
  form.addEventListener("change", (e) => {
    if (e.target.name === "symptom") form.querySelector("#jr-custom-wrap").hidden = e.target.value !== "custom";
    if (e.target.type === "radio") {
      for (const l of e.target.closest(".seg").querySelectorAll("label")) l.classList.toggle("active", l.contains(e.target));
    }
  });

  const entry = () => {
    const f = new FormData(form);
    const when = f.get("when");
    if (!when) throw new Error("Choose when it happened.");
    const note = (f.get("note") || "").trim();
    const start = new Date(when).toISOString();
    const base = { start, note: note || null, value: null, value_label: null };
    const pick = (list, key) => list.find(([val]) => String(val) === String(f.get(key))) || list[0];
    if (kind === "checkin") {
      return checkinEntry({ mood: pick(MOODS, "mood")[0], energy: Number(f.get("energy")) || null,
        stress: Number(f.get("stress")) || null, note, start });
    }
    if (kind === "note") {
      if (!note) throw new Error("Write a note first.");
      return { ...base, event_type: "note", category: "notes", name: "Note" };
    }
    if (kind === "symptom") {
      const [val, label] = pick(SEVERITY, "severity");
      if (f.get("symptom") === "custom") {
        const name = (f.get("custom") || "").trim();
        if (!name) throw new Error("Name the symptom.");
        return { ...base, event_type: "symptom_other", category: "symptoms", name, value: val, value_label: label };
      }
      const [id, name] = SYMPTOMS.find(([s]) => s === f.get("symptom")) || SYMPTOMS[0];
      return { ...base, event_type: id, category: "symptoms", name, value: val, value_label: label };
    }
    if (kind === "medication") {
      const name = (f.get("med") || "").trim();
      if (!name) throw new Error("Name the medication.");
      return { ...base, event_type: "medication", category: "medication", name, value_label: (f.get("dose") || "").trim() || null };
    }
    if (kind === "period") {
      const [val, label] = pick(FLOW, "flow");
      return val === 0 ? { ...base, event_type: "intermenstrual_bleeding", category: "cycle", name: "Spotting", value_label: label }
        : { ...base, event_type: "menstrual_flow", category: "cycle", name: "Menstrual Flow", value: val, value_label: label };
    }
    const name = (f.get("name") || "").trim();
    if (!name) throw new Error("Say what happened.");
    return { ...base, event_type: "custom", category: "other", name, value_label: (f.get("detail") || "").trim() || null };
  };

  const save = async () => {
    let body;
    try { body = entry(); } catch (err) { return toast(err.message, "bad"); }
    const btn = m.el.querySelector("[data-save]");
    btn.disabled = true;
    try {
      if (existing) await put(`/api/journal/${existing.id}?profile=${encodeURIComponent(profile.id)}`, body);
      else await post("/api/journal", body, { profile: profile.id });
      m.close();
      toast(existing ? "Entry saved" : "Added to your journal", "good");
      onSaved();
    } catch (err) { toast(err.message, "bad"); btn.disabled = false; }
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); save(); });
  m.el.querySelector("[data-save]").addEventListener("click", save);
  m.el.querySelector("[data-cancel]").addEventListener("click", () => m.close());
  m.el.querySelector("[data-delete]")?.addEventListener("click", async () => {
    if (!(await confirmDialog("Delete this entry?", "It's removed from your journal on this machine.", { confirmLabel: "Delete", danger: true }))) return;
    try {
      await del(`/api/journal/${existing.id}`, { profile: profile.id });
      m.close();
      toast("Entry deleted");
      onSaved();
    } catch (err) { toast(err.message, "bad"); }
  });
  setTimeout(() => form.querySelector("input:not([type=radio]), select, textarea")?.focus(), 30);
}
