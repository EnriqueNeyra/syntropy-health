import { get } from "../api.js";
import { appBridge, nativeNav, setToolbar } from "../app.js";
import { metricUnit, metricValue } from "./overview.js";
import { recordValue } from "../units.js";
import { age, capitalize, fmtDate, fmtDateTime, html, icon, loading, mount, recordDate, resultStatus } from "../ui.js";

const flagWord = (interp) => (interp && interp !== "normal" ? capitalize(interp.replace("_", " ")) : "");
const flaggedRow = (l) => [l.title, l.value_text, flagWord(l.interpretation), l.ref_text || "—", fmtDate(l.effective_at)];

function table(headers, rows, empty = "None on record.") {
  if (!rows.length) return html`<p class="muted small">${empty}</p>`;
  return html`<div class="table-wrap"><table class="table"><thead><tr>${headers.map((h) => html`<th>${h}</th>`)}</tr></thead>
    <tbody>${rows.map((r) => html`<tr>${r.map((c) => html`<td>${c}</td>`)}</tr>`)}</tbody></table></div>`;
}

/** A title with what other sources said about it, where they disagree. */
const conflicted = (rec) => (rec.status_note ? html`${rec.title}<div class="small muted">${rec.status_note}</div>` : rec.title);

const sourcesOf = (m) => (m.sources || []).map((s) => s.source_name).filter(Boolean).join(", ") || m.source_name || "—";

function medRow(m) {
  const notes = [m.group_note, m.status_note, m.details?.reported ? "Reported, not prescribed" : null,
    m.override ? `Patient-reported: ${m.override.text}` : null].filter(Boolean).join(". ");
  return [notes ? html`${m.title}<div class="small muted">${notes}</div>` : m.title, m.value_text || "—", m.details?.prescriber || "—",
    fmtDate(m.last_prescribed), sourcesOf(m)];
}

export async function render({ el, state }) {
  mount(el, loading(4));
  const r = await get("/api/report", { profile: state.profile.id });
  const conflict = r.identity_conflict || [];
  const corrected = [...(r.medications_corrected || []), ...(r.conditions_corrected || []), ...(r.allergies_corrected || [])];
  const identity = conflict.length ? {} : r.identities[0] || {};   // never print one person's details over mixed records
  const dob = r.profile.birth_date || identity.birth_date;
  const years = age(dob);
  const sources = [...new Set(r.identities.map((i) => i.source_name))];
  const flu = r.immunizations.filter((i) => /influenza/i.test(i.title));
  const vaccines = r.immunizations.filter((i) => !/influenza/i.test(i.title));
  const currentTeams = r.care_team.filter((t) => !["inactive", "suspended"].includes(String(t.status).toLowerCase()));
  // In the iPhone app, printing (or saving a PDF) is the title bar's button.
  const appPrint = setToolbar([{ id: "print", title: "Print or Save PDF", symbol: "printer", run: () => appBridge({ type: "print" }) }]);
  mount(el, html`
    <div class="page-head no-print"><div><h1>Visit summary</h1>
      <p>A one-page summary of ${r.profile.name}'s record to bring to appointments.</p></div>
      <div class="row">${nativeNav ? "" : html`<a class="btn btn-ghost" href="#/records">${icon("chevronLeft")} Records</a>`}
        ${appPrint ? "" : html`<button class="btn btn-primary" id="print-btn">${icon("printer")} Print or save PDF</button>`}</div></div>
    <article class="report card card-pad">
      <div class="report-head">
        <div><h1>${(identity.full_name !== "Unknown" && identity.full_name) || r.profile.name}</h1>
          <div class="muted">${[dob ? `Born ${fmtDate(dob)}${years != null ? ` (${years})` : ""}` : null, identity.gender ? capitalize(identity.gender) : null,
            identity.mrn ? `MRN ${identity.mrn}` : null].filter(Boolean).join(" · ")}</div></div>
        <div class="small muted" style="text-align:right">Health summary<br>Generated ${fmtDateTime(r.generated_at)}</div>
      </div>
      ${conflict.length ? html`<div class="banner warn"><div class="grow"><p><b>Warning: this summary mixes records from different people</b> —
        ${conflict.map((p) => `${p.full_name || "unnamed patient"}${p.birth_date ? ` (born ${fmtDate(p.birth_date)})` : ""}`).join(", ")}.
        Remove the sources that don't belong to ${r.profile.name} before sharing it.</p></div></div>` : ""}

      <section><h2>Allergies</h2>${table(["Allergen", "Reaction", "Criticality"],
        r.allergies.map((a) => [conflicted(a), a.narrative || "—", capitalize(a.details?.criticality) || "—"]), r.allergy_note || "No allergies on record.")}</section>

      <section><h2>Active problems</h2>${table(["Condition", "ICD-10", "Since"],
        r.active_conditions.map((c) => [conflicted(c), c.details?.icd10 || "—", c.details?.onset_text || fmtDate(c.effective_at)]))}
        ${r.unconfirmed_conditions?.length ? html`<h3>Unconfirmed, not established diagnoses</h3>${table(["Condition", "Status", "Since"],
          r.unconfirmed_conditions.map((c) => [c.title, capitalize(c.details?.verification) || "Unconfirmed", fmtDate(c.effective_at)]))}` : ""}
        ${r.conditions_status_unknown?.length ? html`<h3>Status not stated</h3>${table(["Condition", "ICD-10", "Recorded"],
          r.conditions_status_unknown.map((c) => [c.title, c.details?.icd10 || "—", fmtDate(c.effective_at)]))}` : ""}
        ${r.visit_diagnoses?.length ? html`<h3>Diagnoses recorded at visits</h3>${table(["Condition", "ICD-10", "Date"],
          r.visit_diagnoses.slice(0, 10).map((c) => [c.title, c.details?.icd10 || "—", fmtDate(c.effective_at)]))}` : ""}</section>

      <section><h2>Current medications</h2>${table(["Medication", "Instructions", "Prescriber", "Last prescribed", "Source"],
        r.active_medications.map(medRow))}
        ${r.medications_to_check?.length ? html`<h3>May be out of date</h3>${table(["Medication", "Why", "Last prescribed", "Source"],
          r.medications_to_check.map((m) => [m.title, m.med_reason || "—", fmtDate(m.last_prescribed), sourcesOf(m)]))}` : ""}
        ${r.medications_planned?.length ? html`<h3>Planned or proposed, not started</h3>${table(["Medication", "Date", "Source"],
          r.medications_planned.map((m) => [m.title, fmtDate(m.last_prescribed), sourcesOf(m)]))}` : ""}
        <p class="small muted" style="margin-top:6px">From records, not from what's actually being taken. Confirm this list with the patient.</p></section>

      ${corrected.length ? html`<section><h2>On record, and the patient says otherwise</h2>${table(["Entry", "The source says", "Patient-reported", "Source"],
        corrected.map((c) => [c.title, capitalize(c.status) || "—", c.override.text, sourcesOf(c)]))}
        <p class="small muted" style="margin-top:6px">Both are shown so they can be checked. The patient's words don't change the source's record.</p></section>` : ""}

      <section><h2>Latest vital signs</h2>${table(["Measure", "Value", "Date"],
        r.latest_vitals.map((v) => [v.title, recordValue(v), fmtDate(v.effective_at)]))}</section>

      <section><h2>Out-of-range results (most recent)</h2>${table(["Test", "Result", "Flag", "Reference", "Date"],
        r.flagged_labs.filter((l) => !l.older).map(flaggedRow), "All recent lab results are within reference ranges.")}
        ${r.flagged_labs.some((l) => l.older) ? html`<h3 class="small muted" style="margin-top:10px">Older results, not repeated since</h3>${table(["Test", "Result", "Flag", "Reference", "Date"],
          r.flagged_labs.filter((l) => l.older).map(flaggedRow))}` : ""}</section>

      <section><h2>Recent lab results</h2>${table(["Test", "Result", "Flag", "Date"],
        r.recent_labs.map((l) => [html`${l.title} ${resultStatus(l.status)}`, l.value_text, flagWord(l.interpretation), fmtDate(l.effective_at)]))}</section>

      ${r.biometrics.length ? html`<section><h2>Wearable trends (30 days)</h2>${table(["Metric", "Latest", "7-day avg", "30-day avg"],
        r.biometrics.map((m) => [m.label, `${metricValue(m, m.latest.value)} ${metricUnit(m)}`,
          metricValue(m, m.avg_7d), metricValue(m, m.avg_30d)]))}</section>` : ""}

      <section><h2>Immunizations</h2>${table(["Vaccine", "Date"], vaccines.slice(0, 15).map((i) => [i.title, recordDate(i)]))}
        ${flu.length ? html`<p class="small muted" style="margin-top:6px">Most recent influenza vaccine: ${recordDate(flu[0])}</p>` : ""}
        ${r.immunizations_not_given?.length ? html`<p class="small muted" style="margin-top:6px">Not given: ${r.immunizations_not_given.slice(0, 5).map((i) =>
          `${i.title} (${recordDate(i)}${i.details?.status_reason ? `, ${i.details.status_reason}` : ""})`).join("; ")}</p>` : ""}</section>

      <section><h2>Recent visits</h2>${table(["Date", "Visit", "Clinician", "Where"],
        r.recent_encounters.map((e) => [fmtDate(e.effective_at), e.title, (e.details?.clinicians || [])[0] || "—", e.details?.provider || e.source_name || "—"]))}
        ${r.upcoming_encounters?.length ? html`<h3>Upcoming</h3>${table(["Date", "Visit", "Clinician", "Where"],
          r.upcoming_encounters.map((e) => [fmtDate(e.effective_at), e.title, (e.details?.clinicians || [])[0] || "—", e.details?.provider || e.source_name || "—"]))}` : ""}</section>

      ${currentTeams.length ? html`<section><h2>Care team</h2>${table(["Name", "Role", "Source"],
        currentTeams.flatMap((t) => (t.details?.members || []).map((m) => [m.name, m.role, t.source_name])))}</section>` : ""}

      ${r.coverage.length ? html`<section><h2>Insurance</h2>${table(["Plan", "Member ID", "Status"],
        r.coverage.map((c) => [c.title, c.details?.subscriber_id || "—", capitalize(c.status) || "—"]))}</section>` : ""}

      <p class="disclaimer">Compiled by Syntropy Health from ${sources.length ? sources.join(", ") : "connected sources"}.
        Patient-assembled record for informational purposes; verify against the original source records. Not a medical device and not medical advice.</p>
    </article>`);
  // In the iPhone app, the app prints the page (and offers Save as PDF); web views can't open the print dialog.
  el.querySelector("#print-btn")?.addEventListener("click", () => appBridge({ type: "print" }) || window.print());
}
