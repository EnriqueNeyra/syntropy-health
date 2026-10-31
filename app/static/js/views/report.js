import { get, put } from "../api.js";
import { appBridge, nativeNav, setToolbar } from "../app.js";
import { labStatusText, metricUnit, metricValue } from "./overview.js";
import { fmtMeasure, recordValue } from "../units.js";
import { ageText, capitalize, fmtDate, fmtDateTime, html, icon, loading, mount, plural, recordDate, resultStatus, toast } from "../ui.js";

const STALE_METRIC_DAYS = 14;   // a measure with nothing newer than this isn't shown as if it were current

// What can go on the summary, in print order: the first five are what a short visit needs. "key" ones stay in "Show to a clinician".
export const SECTIONS = [
  { id: "allergies", title: "Allergies", key: true }, { id: "problems", title: "Active problems", key: true },
  { id: "medications", title: "Current medications", key: true }, { id: "corrected", title: "Entries the patient corrected", key: true }, { id: "flagged", title: "Out-of-range results", key: true },
  { id: "labs", title: "Lab results", key: true }, { id: "questions", title: "My questions", key: true }, { id: "vitals", title: "Vital signs" },
  { id: "home", title: "Home readings" }, { id: "wearables", title: "Wearable measures" }, { id: "procedures", title: "Procedures and surgery" },
  { id: "imaging", title: "Imaging" }, { id: "smoking", title: "Smoking status" }, { id: "journal", title: "Journal entries" },
  { id: "immunizations", title: "Immunizations" }, { id: "visits", title: "Visits" }, { id: "care_team", title: "Care team" },
  { id: "insurance", title: "Insurance", off: true }, { id: "sources", title: "Sources" },
];

const flagWord = (interp) => (interp && interp !== "normal" ? capitalize(interp.replace("_", " ")) : "");
const saidOf = (r) => (r.override ? html`<div class="small muted">Patient-reported: ${r.override.text}</div>` : "");
const assessedWord = (interp) => (interp === "normal" ? "In range" : flagWord(interp) || "Not assessed");
const sourcesOf = (m) => (m.sources || []).map((s) => s.source_name).filter(Boolean).join(", ") || m.source_name || "—";
/** A date with where it came from, on two lines so a table stays narrow. */
const CONCLUSION_SHOWN = 300;
function conclusion(p) {
  const text = p.narrative || "—";
  if (text.length <= CONCLUSION_SHOWN) return text;
  const space = text.lastIndexOf(" ", CONCLUSION_SHOWN);
  return html`${text.slice(0, space > 200 ? space : CONCLUSION_SHOWN)}… <span class="small muted">This text continues. The whole report is in Records, under <a href="#/records/reports">Diagnostic reports</a>.</span>`;
}
const dated = (date, r) => html`${date}<div class="small muted">${sourcesOf(r)}</div>`;
const previous = (l) => (l.previous ? html`${l.previous.value_text}<div class="small muted">${fmtDate(l.previous.effective_at)}</div>` : "—");
const flaggedRow = (l) => [html`${l.title}${saidOf(l)}`, l.value_text, flagWord(l.interpretation), l.ref_text || "—", previous(l), dated(fmtDate(l.effective_at), l)];
const FLAGGED_HEAD = ["Test", "Result", "Flag", "Range", "Previous", "Date and source"];
const more = (n, what = "more") => (n > 0 ? html`<p class="small muted" style="margin-top:6px">and ${n} ${what}.</p>` : "");

function table(headers, rows, empty = "None on record.") {
  if (!rows.length) return html`<p class="muted small">${empty}</p>`;
  // The roles keep it a table for screen readers when a phone's styles stack its rows.
  return html`<div class="table-wrap"><table class="table" role="table"><thead role="rowgroup"><tr role="row">${headers.map((h) => html`<th role="columnheader">${h}</th>`)}</tr></thead>
    <tbody role="rowgroup">${rows.map((r) => html`<tr role="row">${r.map((c, i) => html`<td role="cell" data-label="${headers[i]}">${c}</td>`)}</tr>`)}</tbody></table></div>`;
}

/** A title with what other sources said about it, where they disagree. */
const conflicted = (rec) => (rec.status_note ? html`${rec.title}<div class="small muted">${rec.status_note}</div>` : rec.title);

function medRow(m) {
  const status = m.status && String(m.status).toLowerCase() !== "active" ? capitalize(m.status) : null;
  const notes = [status, m.group_note, m.status_note, m.details?.reported ? "Reported, not prescribed" : null,
    m.override ? `Patient-reported: ${m.override.text}` : null].filter(Boolean).join(". ");
  return [notes ? html`${m.title}<div class="small muted">${notes}</div>` : m.title, m.value_text || "—", m.details?.prescriber || "—",
    dated(fmtDate(m.last_prescribed), m)];
}

const visitRow = (e) => [fmtDate(e.effective_at), html`${e.title}${saidOf(e)}`, (e.details?.clinicians || [])[0] || "—", e.details?.provider || e.source_name || "—"];

/** Doses of the same vaccine on one row, with every date. Nothing is left off. */
function vaccineRows(vaccines) {
  const byName = new Map();
  for (const v of vaccines) {
    const key = v.title.trim().toLowerCase();
    if (!byName.has(key)) byName.set(key, []);
    byName.get(key).push(v);
  }
  return [...byName.values()].map((doses) => [html`${doses[0].title}${doses.map(saidOf)}`, String(doses.length),
    doses.map(recordDate).join(", "), [...new Set(doses.map(sourcesOf))].join(", ")]);
}

const homeCell = (m, w) => (w?.count
  ? html`${fmtMeasure(w.avg, m.unit, m.metric)}<div class="small muted">${plural(w.count, "reading")}, ${fmtMeasure(w.low, m.unit, m.metric, { withUnit: false })} to ${fmtMeasure(w.high, m.unit, m.metric)}</div>`
  : "None");

const sectionOn = (prefs, s) => (prefs.sections && s.id in prefs.sections ? prefs.sections[s.id] : !s.off);

const questionsBlock = (questions) => html`<ol class="report-questions">${questions.map((q) => html`<li>${q}</li>`)}</ol>`;

export async function render({ el, state }) {
  mount(el, loading(4));
  const pid = state.profile.id;
  const [r, prefs] = await Promise.all([get("/api/report", { profile: pid }), get("/api/report/preferences", { profile: pid })]);
  const conflict = r.identity_conflict || [];
  const corrected = [...(r.medications_corrected || []), ...(r.conditions_corrected || []), ...(r.allergies_corrected || [])];
  const identity = conflict.length ? {} : r.identities[0] || {};   // never print one person's details over mixed records
  const dob = r.profile.birth_date || identity.birth_date;
  const years = ageText(dob);
  const sources = [...new Set(r.identities.map((i) => i.source_name))];
  const currentTeams = r.care_team.filter((t) => !["inactive", "suspended"].includes(String(t.status).toLowerCase()));
  const today = Date.now() / 86400000;
  const measured = r.biometrics.map((m) => ({ m, age: today - new Date(`${m.latest.day}T00:00:00`).getTime() / 86400000 }));
  const current = measured.filter((x) => x.age <= STALE_METRIC_DAYS).map((x) => x.m);
  const oldMeasures = measured.filter((x) => x.age > STALE_METRIC_DAYS).map((x) => `${x.m.label} (last ${fmtDate(x.m.latest.day)})`);
  const name = (identity.full_name !== "Unknown" && identity.full_name) || r.profile.name;
  // In the iPhone app, printing (or saving a PDF) is the title bar's button.
  const appPrint = setToolbar([{ id: "print", title: "Print or Save PDF", symbol: "printer", run: () => appBridge({ type: "print" }) }]);
  const section = (id, title, body) => html`<section data-section="${id}" class="${SECTIONS.find((s) => s.id === id)?.key ? "report-key" : ""} ${sectionOn(prefs, SECTIONS.find((s) => s.id === id)) ? "" : "report-off"}"><h2>${title}</h2>${body}</section>`;
  mount(el, html`
    <div class="page-head no-print"><div><h1>Visit summary</h1>
      <p>A summary of ${r.profile.name}'s record to bring to appointments.</p></div>
      <div class="row wrap">${nativeNav ? "" : html`<a class="btn btn-ghost" href="#/records">${icon("chevronLeft")} Records</a>`}
        <a class="btn" href="#/prepare">${icon("note")} Prepare for a visit</a>
        <button class="btn" id="clinician-btn" aria-pressed="false">Show to a clinician</button>
        ${appPrint ? "" : html`<button class="btn btn-primary" id="print-btn">${icon("printer")} Print or save PDF</button>`}</div></div>
    <details class="card card-pad no-print report-choices"><summary>Choose what's included</summary>
      <div class="report-choice-list">${SECTIONS.map((s) => html`<label class="row"><input type="checkbox" data-choice="${s.id}" ${sectionOn(prefs, s) ? "checked" : ""}> ${s.title}</label>`)}</div></details>
    <article class="report card card-pad">
      <div class="report-head">
        <div><h1>${name}</h1>
          <div class="muted">${[dob ? `Born ${fmtDate(dob)}${years ? ` (${years})` : ""}` : null, identity.gender ? capitalize(identity.gender) : null,
            identity.mrn ? `MRN ${identity.mrn}${identity.source_name ? ` (${identity.source_name})` : ""}` : null].filter(Boolean).join(" · ")}</div></div>
        <div class="small muted" style="text-align:right">Health summary<br>Generated ${fmtDateTime(r.generated_at)}</div>
      </div>
      ${conflict.length ? html`<div class="banner warn"><div class="grow"><p><b>Warning: this summary mixes records from different people</b> —
        ${conflict.map((p) => `${p.full_name || "unnamed patient"}${p.birth_date ? ` (born ${fmtDate(p.birth_date)})` : ""}`).join(", ")}.
        Remove the sources that don't belong to ${r.profile.name} before sharing it.</p></div></div>` : ""}

      ${section("allergies", "Allergies", table(["Allergen", "Reaction", "Criticality", "Source"],
        r.allergies.map((a) => [html`${conflicted(a)}${saidOf(a)}`, a.narrative || "—", capitalize(a.details?.criticality) || "—", sourcesOf(a)]), r.allergy_note || "No allergies on record."))}

      ${section("problems", "Active problems", html`${table(["Condition", "ICD-10", "Since", "Source"],
        r.active_conditions.map((c) => [html`${conflicted(c)}${saidOf(c)}`, c.details?.icd10 || "—", c.details?.onset_text || fmtDate(c.effective_at), sourcesOf(c)]))}
        ${r.unconfirmed_conditions?.length ? html`<h3>Unconfirmed, not established diagnoses</h3>${table(["Condition", "Status", "Since", "Source"],
          r.unconfirmed_conditions.map((c) => [html`${c.title}${saidOf(c)}`, capitalize(c.details?.verification) || "Unconfirmed", fmtDate(c.effective_at), sourcesOf(c)]))}` : ""}
        ${r.conditions_status_unknown?.length ? html`<h3>Status not stated</h3>${table(["Condition", "ICD-10", "Recorded", "Source"],
          r.conditions_status_unknown.map((c) => [html`${c.title}${saidOf(c)}`, c.details?.icd10 || "—", fmtDate(c.effective_at), sourcesOf(c)]))}` : ""}
        ${r.visit_diagnoses?.length ? html`<h3>Diagnoses recorded at visits</h3>${table(["Condition", "ICD-10", "Date", "Source"],
          r.visit_diagnoses.slice(0, 10).map((c) => [html`${c.title}${saidOf(c)}`, c.details?.icd10 || "—", fmtDate(c.effective_at), sourcesOf(c)]))}${more(r.visit_diagnoses.length - 10)}` : ""}`)}

      ${section("medications", "Current medications", html`${table(["Medication", "Instructions", "Prescriber", "Last prescribed and source"], r.active_medications.map(medRow))}
        ${r.medications_to_check?.length ? html`<h3>May be out of date</h3>${table(["Medication", "Why", "Last prescribed and source"],
          r.medications_to_check.map((m) => [html`${m.title}${saidOf(m)}`, m.med_reason || "—", dated(fmtDate(m.last_prescribed), m)]))}` : ""}
        ${r.medications_planned?.length ? html`<h3>Planned or proposed, not started</h3>${table(["Medication", "Date and source"],
          r.medications_planned.map((m) => [html`${m.title}${saidOf(m)}`, dated(fmtDate(m.last_prescribed), m)]))}` : ""}
        <p class="small muted" style="margin-top:6px">From records, not from what's actually being taken. Confirm this list with the patient.</p>`)}

      ${corrected.length ? section("corrected", "On record, and the patient says otherwise", html`${table(["Entry", "The source says", "Patient-reported", "Source"],
        corrected.map((c) => [c.title, capitalize(c.status) || "—", c.override.text, sourcesOf(c)]))}
        <p class="small muted" style="margin-top:6px">Both are shown so they can be checked. The patient's words don't change the source's record.</p>`) : ""}

      ${section("flagged", "Out-of-range results (most recent)", html`${table(FLAGGED_HEAD,
        r.flagged_labs.filter((l) => !l.older).map(flaggedRow), "No recent result is flagged out of range.")}
        <p class="small muted" style="margin-top:6px">${labStatusText(r.lab_status)}</p>
        ${r.flagged_labs.some((l) => l.older) ? html`<h3 class="small muted" style="margin-top:10px">Older results, not repeated since</h3>${table(FLAGGED_HEAD,
          r.flagged_labs.filter((l) => l.older).map(flaggedRow))}` : ""}`)}

      ${section("labs", "Other latest lab results", html`${table(["Test", "Result", "Flag", "Range", "Previous", "Date and source"],
        r.recent_labs.filter((l) => !["high", "low", "critical_high", "critical_low", "abnormal"].includes(l.interpretation)).map((l) =>
          [html`${l.title} ${resultStatus(l.status)}${saidOf(l)}`, l.value_text, assessedWord(l.interpretation), l.ref_text || "—", previous(l), dated(fmtDate(l.effective_at), l)]),
        r.lab_status?.total ? "Every latest result is listed above." : "No lab results on record.")}${more(r.labs_more, "more tests, in Records")}`)}

      ${prefs.questions?.length ? section("questions", "My questions", questionsBlock(prefs.questions)) : ""}

      ${section("vitals", "Latest vital signs", html`${table(["Measure", "Value", "Date and source"],
        r.latest_vitals.map((v) => [html`${v.title}${saidOf(v)}`, recordValue(v), dated(fmtDate(v.effective_at), v)]))}${more(r.vitals_more)}`)}

      ${r.home_readings?.length ? section("home", "Home readings", table(["Measure", "Last 7 days", "Last 30 days", "Last reading and source"],
        r.home_readings.map((m) => [m.label, homeCell(m, m.d7), homeCell(m, m.d30), html`${fmtDate(m.last_at)}<div class="small muted">${m.sources.join(", ") || "—"}</div>`]))) : ""}

      ${current.length || oldMeasures.length ? section("wearables", "Wearable and app measures (30 days)", html`${table(["Measure", "Latest", "7-day average", "30-day average", "Days", "As of and source"],
        current.map((m) => [m.label, `${metricValue(m, m.latest.value)} ${metricUnit(m)}`, metricValue(m, m.avg_7d), metricValue(m, m.avg_30d), String(m.spark.length),
          html`${fmtDate(m.latest.day)}<div class="small muted">${m.latest.source || "—"}</div>`]), "Nothing in the last two weeks.")}
        ${oldMeasures.length ? html`<p class="small muted" style="margin-top:6px">Not shown, nothing recent: ${oldMeasures.join(", ")}.</p>` : ""}`) : ""}

      ${r.procedures?.length || r.procedures_unfinished?.length ? section("procedures", "Procedures and surgery", html`${r.procedures.length ? table(["Procedure", "Date and source"],
        r.procedures.map((p) => [html`${p.title}${saidOf(p)}`, dated(recordDate(p), p)])) : ""}${more(r.procedures_more)}
        ${r.procedures_unfinished?.length ? html`<h3>Not marked completed</h3>${table(["Procedure", "Status", "Date and source"],
          r.procedures_unfinished.map((p) => [html`${p.title}${saidOf(p)}`, capitalize(p.status.replace(/-/g, " ")), dated(recordDate(p), p)]))}` : ""}`) : ""}

      ${r.imaging?.length ? section("imaging", "Recent imaging", html`${table(["Study", "Conclusion", "Date and source"],
        r.imaging.map((p) => [html`${p.title}${saidOf(p)}`, conclusion(p), dated(fmtDate(p.effective_at), p)]))}${more(r.imaging_more)}`) : ""}

      ${r.smoking ? section("smoking", "Smoking status", html`<p class="small">${r.smoking.value_text || "—"} · ${fmtDate(r.smoking.effective_at)} · ${sourcesOf(r.smoking)}</p>${saidOf(r.smoking)}`) : ""}

      ${r.journal?.length ? section("journal", "Logged in the journal (90 days)", table(["Entry", "Times", "Last logged"],
        r.journal.map((j) => [j.name, String(j.count), fmtDate(j.last_at)]))) : ""}

      ${section("immunizations", "Immunizations", html`${table(["Vaccine", "Doses", "Dates", "Source"], vaccineRows(r.immunizations))}
        ${r.immunizations_not_given?.length ? html`<p class="small muted" style="margin-top:6px">Not given: ${r.immunizations_not_given.map((i) =>
          `${i.title} (${recordDate(i)}${i.details?.status_reason ? `, ${i.details.status_reason}` : ""})`).join("; ")}</p>` : ""}`)}

      ${section("visits", "Recent visits", html`${table(["Date", "Visit", "Clinician", "Where"], r.recent_encounters.map(visitRow))}${more(r.encounters_more, "earlier visits, in Records")}
        ${r.upcoming_encounters?.length ? html`<h3>Upcoming</h3>${table(["Date", "Visit", "Clinician", "Where"], r.upcoming_encounters.map(visitRow))}` : ""}`)}

      ${currentTeams.length ? section("care_team", "Care team", table(["Name", "Role", "Source"],
        currentTeams.flatMap((t) => (t.details?.members || []).map((m) => [m.name, m.role, t.source_name])))) : ""}

      ${r.coverage.length ? section("insurance", "Insurance", table(["Plan", "Member ID", "Status"],
        r.coverage.map((c) => [html`${c.title}${saidOf(c)}`, c.details?.subscriber_id || "—", capitalize(c.status) || "—"]))) : ""}

      ${r.source_status?.length ? section("sources", "Sources and how old they are", html`${table(["Source", "Last synced", "Newest record"],
        r.source_status.map((s) => [s.name, s.last_sync_at ? `${s.one_time ? "Imported " : ""}${fmtDate(s.last_sync_at)}` : "—", s.newest_record_at ? fmtDate(s.newest_record_at) : "—"]))}
        ${r.source_status.filter((s) => s.not_fetched?.length).map((s) => html`<p class="small muted" style="margin-top:6px">${s.name}: ${s.not_fetched.join(" ")}</p>`)}`) : ""}

      <p class="disclaimer">Compiled by Syntropy Health from ${sources.length ? sources.join(", ") : "connected sources"}.
        Patient-assembled record for informational purposes; verify against the original source records. Not a medical device and not medical advice.</p>
    </article>`);
  // In the iPhone app, the app prints the page (and offers Save as PDF); web views can't open the print dialog.
  el.querySelector("#print-btn")?.addEventListener("click", () => appBridge({ type: "print" }) || window.print());
  const article = el.querySelector("article.report");
  el.querySelector("#clinician-btn").addEventListener("click", (e) => {
    const on = article.classList.toggle("report-clinician");
    e.currentTarget.setAttribute("aria-pressed", String(on));
    e.currentTarget.textContent = on ? "Show everything" : "Show to a clinician";
  });
  el.querySelectorAll("[data-choice]").forEach((box) => box.addEventListener("change", async () => {
    article.querySelector(`[data-section="${box.dataset.choice}"]`)?.classList.toggle("report-off", !box.checked);
    const choices = Object.fromEntries([...el.querySelectorAll("[data-choice]")].map((b) => [b.dataset.choice, b.checked]));
    try { await put("/api/report/preferences", { profile_id: pid, sections: choices }); }
    catch (err) { toast(`Couldn't remember that: ${err.message}`, "bad"); }
  }));
  // Each printed page carries the patient's name and a page number, where the browser can do it.
  const page = document.getElementById("report-page-style") || document.head.appendChild(Object.assign(document.createElement("style"), { id: "report-page-style" }));
  page.textContent = `@media print { @page { margin: 14mm 12mm; @bottom-left { content: ${JSON.stringify(name)}; font-size: 9pt; } @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 9pt; } } }`;
  return () => page.remove();
}
