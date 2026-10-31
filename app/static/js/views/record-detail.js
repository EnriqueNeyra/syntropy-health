// One clinical record, in a drawer (or its own screen in the iPhone app): normalized fields, sources, note text and raw FHIR.

import { del, get, put } from "../api.js";
import { appBridge, nativeNav } from "../app.js";
import { recordValue } from "../units.js";
import {
  capitalize, CATEGORY_ICON, confirmDialog, flagBadge, fmtDate, fmtDateTime, html, icon, loading, modal, mount, resultStatus, statusBadge, toast,
} from "../ui.js";

function kv(rows) {
  const filtered = rows.filter(([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length));
  if (!filtered.length) return "";
  return html`<dl class="kv">${filtered.map(([k, v]) => html`<dt>${k}</dt><dd>${Array.isArray(v) ? v.join(", ") : v}</dd>`)}</dl>`;
}

function detailRows(r) {
  const d = r.details || {};
  const common = [["Status", r.status ? statusBadge(r.status) : null], ["Date", fmtDate(r.effective_at)]];
  switch (r.category) {
    case "labs": case "vitals": case "observations":
      return [
        ["Result", r.category === "vitals" ? recordValue(r) : r.value_text], ["Status", resultStatus(r.status) || null],
        ["Interpretation", flagBadge(r.interpretation) || (r.interpretation ? capitalize(r.interpretation) : null)],
        ["Reference range", r.ref_text], ["All ranges reported", (d.ranges || []).map((x) => `${x.text || "—"}${x.for ? ` (${x.for})` : ""}`)],
        ["In standard units", r.category === "vitals" ? null : d.converted_value], ["As reported", d.original_value],
        ["Panel", d.panel], ["Measured", fmtDateTime(r.effective_at)],
        ["Components", (d.components || []).map((c) => `${c.name}: ${c.text ?? c.value ?? "No value"}${c.text ? "" : ` ${c.unit || ""}`}`)],
      ];
    case "medications":
      return [...common, ["Reported, not prescribed", d.reported ? "Yes" : null], ["Instructions", d.dosage], ["As needed", d.as_needed ? "Yes" : null],
              ["Prescriber", d.prescriber], ["Refills", d.refills], ["Valid until", d.valid_until ? fmtDate(d.valid_until) : null], ["Reason", d.reason]];
    case "conditions":
      return [...common, ["Onset", d.onset_text || fmtDate(r.effective_at)], ["Resolved", r.effective_end ? fmtDate(r.effective_end) : d.abatement_text],
              ["Type", (d.categories || []).filter(Boolean)], ["ICD-10", d.icd10], ["SNOMED CT", d.snomed], ["Verification", d.verification], ["Severity", d.severity]];
    case "allergies":
      return [...common, ["Category", d.category ? capitalize(d.category) : null], ["Criticality", d.criticality ? capitalize(d.criticality) : null],
              ["Reactions", (d.reactions || []).map((x) => `${x.manifestation || "Reaction"}${x.severity ? ` (${x.severity})` : ""}`)], ["Note", d.note]];
    case "immunizations":
      return [...common, ["Date given", d.date_text], ["Reason not given", d.status_reason], ["Dose", d.dose], ["Lot", d.lot], ["Manufacturer", d.manufacturer],
              ["Site", d.site], ["Route", d.route]];
    case "encounters":
      return [...common, ["Ended", r.effective_end ? fmtDateTime(r.effective_end) : null], ["Type", d.class],
              ["Reason", d.reasons], ["Diagnoses", d.diagnoses], ["Clinicians", d.clinicians], ["Location", d.locations], ["Organization", d.provider]];
    case "procedures":
      return [...common, ["When", d.date_text], ["Performed by", d.performers], ["Reason", d.reasons], ["Outcome", d.outcome], ["Body site", d.body_site]];
    case "reports":
      return [...common, ["Type", d.categories], ["Performed by", d.performer]];
    case "notes":
      return [...common, ["Type", d.type], ["Authors", d.authors]];
    case "care_team":
      return [["Members", (d.members || []).map((m) => `${m.name} — ${m.role}`)],
              ["Others", d.unnamed_members ? `${d.unnamed_members} whose names this source didn't send` : null]];
    case "care_plans":
      return [...common, ["Addresses", d.addresses], ["Activities", (d.activities || []).map((a) => a.description)]];
    case "goals":
      return [...common, ["Targets", d.targets], ["Achievement", d.achievement]];
    case "devices":
      return [...common, ["Type", d.type], ["Manufacturer", d.manufacturer], ["Model", d.model], ["UDI", d.udi]];
    case "coverage":
      return [...common, ["Payor", d.payor], ["Plan", d.plan], ["Member ID", d.subscriber_id], ["Relationship", d.relationship]];
    default:
      return common;
  }
}

/** The record's content: shown in the drawer, or as its own screen in the iPhone app. */
function recordBody(r, text) {
  return html`
    <div class="stack">
      <div class="row"><span class="src-avatar">${icon(CATEGORY_ICON[r.category] || "records")}</span>
        <div class="grow"><div class="muted small">${capitalize(r.category.replace("_", " "))}${r.code ? html` · <span class="mono">${r.code}</span>` : ""}</div>
          <div><b>${r.source_name || ""}</b></div></div></div>
      ${r.status_note ? html`<div class="banner warn small"><div class="grow">${r.status_note}</div></div>` : ""}
      ${r.details?.body_stale ? html`<div class="banner warn small"><div class="grow">This changed at the source, and the new text couldn't be fetched yet.
        This is the earlier version. The next sync tries again.</div></div>` : ""}
      ${kv(detailRows(r))}
      ${r.results?.length ? html`<div><div class="label">Results</div>
        <div class="list card">${r.results.map((x) => html`<button class="list-item small" data-open-result="${x.id}"
          style="text-align:left;width:100%;background:none;border:0;cursor:pointer">
          <span class="grow">${x.title}</span>
          <b>${x.category === "vitals" ? recordValue(x) : x.value_text || "—"}</b>
          ${resultStatus(x.status)}${flagBadge(x.interpretation)}</button>`)}</div></div>` : ""}
      ${r.narrative && !text && !shownAbove(r) ? html`<div><div class="label">Notes</div><p>${r.narrative}</p></div>` : ""}
      ${text ? html`<div><div class="row between" style="margin-bottom:6px"><div class="label" style="margin:0">Full text</div>
        <button class="btn btn-sm" data-copy>Copy</button></div><div class="note-text">${text}</div></div>` : ""}
      ${r.duplicates?.length ? html`<div><div class="label">Also reported by</div>
        <div class="list card">${r.duplicates.map((dup) => html`<div class="list-item small"><span class="grow">${dup.source_name}</span>
          ${["conditions", "allergies", "medications"].includes(r.category) ? statusBadge(dup.status) : ""}
          <span class="muted">${fmtDate(dup.effective_at)}</span></div>`)}</div>
        <p class="hint">Syntropy merges the same fact reported by several institutions into one entry.</p></div>` : ""}
      ${filesBlock(r)}
      ${r.editable ? html`<div class="row"><span class="small muted grow">You entered this.</span>
        <button class="btn btn-sm btn-ghost btn-danger" data-delete>${icon("trash")} Delete</button></div>` : correctionBlock(r)}
      <details><summary class="small muted" style="cursor:pointer">Original FHIR resource</summary>
        <pre class="raw">${JSON.stringify(r.raw, null, 2)}</pre></details>
    </div>`;
}

const FILE_WORDS = {
  scanned: "Scanned document, no text. Open it to read it.", unreadable: "This document's text couldn't be read. Open it to read it.",
  too_large: "This document is too large to keep here. It's still at the source.",
};

/** PDFs and images that came with the record, and the health summary a file import was read from. */
function filesBlock(r) {
  if (!r.files?.length) return "";
  return html`<div><div class="label">Documents</div><div class="list card">${r.files.map((f) => {
    const url = `/api/records/${encodeURIComponent(r.id)}/files/${encodeURIComponent(f.id)}`;
    const canShow = ["application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"].includes(f.content_type);
    return html`<div class="list-item small"><div class="grow"><b>${f.name || (f.content_type === "application/pdf" ? "PDF document" : "Document")}</b>
      ${FILE_WORDS[f.state] ? html`<div class="muted">${FILE_WORDS[f.state]}</div>` : ""}</div>
      ${f.state === "too_large" ? "" : html`${canShow ? html`<a class="btn btn-sm" href="${url}" target="_blank" rel="noopener">Open</a>` : ""}
      <a class="btn btn-sm btn-ghost" href="${url}?download=true">Download</a>`}</div>`;
  })}</div></div>`;
}

const CORRECTIONS = { medications: [["not_taking", "I'm not taking this"], ["taking_differently", "I take this differently"], ["wrong", "This is wrong or not me"]] };
const CORRECTABLE = ["medications", "conditions", "allergies", "labs", "vitals", "observations", "immunizations", "procedures", "encounters", "reports", "notes"];

/** What the person says about this entry: beside what the source says, never over it. */
function correctionBlock(r) {
  if (!CORRECTABLE.includes(r.category)) return "";
  const said = r.override;
  const choices = CORRECTIONS[r.category] || [["wrong", "This is wrong or not me"]];
  return html`<div class="stack" data-correction>
    ${said ? html`<div class="banner info small"><div class="grow"><b>In your own words:</b> ${said.text}<div class="muted">
      Added ${fmtDateTime(new Date(said.set_at * 1000).toISOString())}. What the source says above hasn't changed.</div></div></div>` : ""}
    <details><summary class="small muted" style="cursor:pointer">${said ? "Change what you said" : "Something wrong with this?"}</summary>
      <div class="stack" style="margin-top:8px">
        <p class="small muted">This stays in Syntropy Health, beside what the source says. It doesn't change anything at the source.</p>
        <select class="input" data-correction-state aria-label="What's wrong"><option value="">Just add a note</option>
          ${choices.map(([v, label]) => html`<option value="${v}" ${said?.state === v ? "selected" : ""}>${label}</option>`)}</select>
        <textarea class="input" rows="2" maxlength="2000" data-correction-note aria-label="Note" placeholder="Add a note, in your own words">${said?.note || ""}</textarea>
        <div class="row"><button class="btn btn-sm btn-primary" data-correction-save>Save</button>
          ${said ? html`<button class="btn btn-sm btn-ghost" data-correction-clear>Remove</button>` : ""}</div>
      </div></details></div>`;
}

/** An allergy's narrative is its reactions, already listed above. */
const shownAbove = (r) => r.category === "allergies" && (r.details?.reactions || []).length > 0
  && r.details.reactions.every((x) => r.narrative.includes(x.manifestation || ""));

const fullText = (r) => (r.category === "notes" ? r.narrative : (r.category === "reports" ? (r.details?.full_text || r.narrative) : null));

/** The record's buttons: another result, delete (a result entered by hand), copy the note. `done` runs after a delete. */
function wire(el, r, text, { leave, done }) {
  el.querySelectorAll("[data-open-result]").forEach((b) => b.addEventListener("click", () => {
    leave(); openRecord(b.dataset.openResult);
  }));
  el.querySelector("[data-delete]")?.addEventListener("click", async () => {
    if (!(await confirmDialog("Delete result", `Delete ${r.title} from ${fmtDate(r.effective_at)}?`, { confirmLabel: "Delete", danger: true }))) return;
    try { await del(`/api/labs/${encodeURIComponent(r.id)}`); } catch (err) { toast(err.message, "bad"); return; }
    toast("Deleted");
    done();
  });
  el.querySelector("[data-correction-save]")?.addEventListener("click", async () => {
    const state = el.querySelector("[data-correction-state]").value || null;
    const note = el.querySelector("[data-correction-note]").value.trim() || null;
    try { await put(`/api/records/${encodeURIComponent(r.id)}/override`, { state, note }); }
    catch (err) { toast(err.message, "bad"); return; }
    toast("Saved");
    done();
  });
  el.querySelector("[data-correction-clear]")?.addEventListener("click", async () => {
    try { await del(`/api/records/${encodeURIComponent(r.id)}/override`); } catch (err) { toast(err.message, "bad"); return; }
    toast("Removed");
    done();
  });
  // The clipboard needs a secure page: opened by its home-network address over plain http, it isn't there.
  const copy = el.querySelector("[data-copy]");
  if (copy) copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch { toast("Copy isn't available here; select the text instead.", "bad"); }
  });
}

export async function openRecord(id, onChange) {
  // In the iPhone app a record opens as its own screen, with a back button.
  if (nativeNav && id && appBridge({ type: "visit", hash: `#/_record/${encodeURIComponent(id)}` })) return;
  const m = modal({ title: "Record", body: loading(3), drawer: true });
  let r;
  try { r = await get(`/api/records/${encodeURIComponent(id)}`); }
  catch (err) { m.close(); toast(err.message, "bad"); return; }
  const text = fullText(r);
  m.el.querySelector(".modal-head h2").textContent = r.title;
  m.setBody(recordBody(r, text));
  wire(m.el, r, text, { leave: () => m.close(), done: () => { m.close(); onChange && onChange(); } });
}

/** A record as a page (#/_record/<id>): the iPhone app's record screen. Its heading becomes the screen's title. */
export async function render({ el, parts }) {
  const id = decodeURIComponent(parts[0] || "");
  let r;
  try { r = await get(`/api/records/${encodeURIComponent(id)}`); }
  catch (err) { mount(el, html`<div class="banner bad"><div class="grow"><b>This record couldn't be opened.</b><p>${err.message}</p></div></div>`); return; }
  const text = fullText(r);
  mount(el, html`<div class="page-head"><div><h1>${r.title}</h1></div></div><div class="card record-page">${recordBody(r, text)}</div>`);
  // After a delete the screen has nothing left to show: back to where it was opened from (which then redraws).
  wire(el, r, text, { leave: () => {}, done: () => appBridge({ type: "back" }) });
}
