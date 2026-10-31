// One clinical record, in a drawer (or its own screen in the iPhone app): normalized fields, sources, note text and raw FHIR.

import { del, get } from "../api.js";
import { appBridge, nativeNav } from "../app.js";
import { recordValue } from "../units.js";
import { printLetter } from "./records-request.js";
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

const VISIT_GROUPS = [["notes", "Notes"], ["labs", "Results"], ["vitals", "Vital signs"], ["reports", "Reports"], ["conditions", "Diagnoses"],
  ["medications", "Medications ordered"], ["procedures", "Procedures"], ["observations", "Other observations"], ["immunizations", "Vaccines"]];

const linkRow = (x) => html`<button class="list-item small" data-open-result="${x.id}" style="text-align:left;width:100%;background:none;border:0;cursor:pointer">
  <span class="grow">${x.title}</span><b>${x.category === "vitals" ? recordValue(x) : x.value_text || ""}</b>${resultStatus(x.status)}${flagBadge(x.interpretation)}</button>`;

/** A visit's results, with each panel folded under its name. */
function visitResults(rows) {
  const panels = new Map();
  const loose = [];
  for (const x of rows) {
    if (x.details?.panel) panels.set(x.details.panel, [...(panels.get(x.details.panel) || []), x]);
    else loose.push(x);
  }
  return html`<div class="list card">${loose.map(linkRow)}${[...panels].map(([name, members]) => html`<details class="list-item small" style="display:block">
    <summary style="cursor:pointer">${name} (${members.length})</summary>${members.map(linkRow)}</details>`)}</div>`;
}

const visitBlock = (r) => (r.category === "encounters" ? VISIT_GROUPS.filter(([key]) => r.visit_records?.[key]?.length).map(([key, label]) => html`<div><div class="label">${label}</div>
  ${key === "labs" ? visitResults(r.visit_records[key]) : html`<div class="list card">${r.visit_records[key].map(linkRow)}</div>`}</div>`) : "");

const seriesLink = (r) => (r.category === "labs" || r.category === "vitals"
  ? `#/trends/${r.category}?${r.code ? `code=${encodeURIComponent(r.code)}` : `title=${encodeURIComponent(r.title)}`}` : null);

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
      ${r.previous ? html`<div class="small muted">Previous result: ${r.previous.value_text || "—"} on ${fmtDate(r.previous.effective_at)}${r.previous.source_name ? ` from ${r.previous.source_name}` : ""}</div>` : ""}
      ${seriesLink(r) ? html`<div><a class="btn btn-sm" href="${seriesLink(r)}" data-leave>See history ${icon("arrowRight")}</a></div>` : ""}
      ${r.visit ? html`<div class="small">From the visit: <button class="btn btn-sm btn-ghost" data-open-result="${r.visit.id}">${r.visit.title} · ${fmtDate(r.visit.effective_at)}</button></div>` : ""}
      ${visitBlock(r)}
      ${r.results?.length ? html`<div><div class="label">Results</div>
        <div class="list card">${r.results.map((x) => html`<button class="list-item small" data-open-result="${x.id}"
          style="text-align:left;width:100%;background:none;border:0;cursor:pointer">
          <span class="grow">${x.title}</span>
          <b>${x.category === "vitals" ? recordValue(x) : x.value_text || "—"}</b>
          ${resultStatus(x.status)}${flagBadge(x.interpretation)}</button>`)}</div></div>` : ""}
      ${r.narrative && !text && !shownAbove(r) ? html`<div><div class="label">Notes</div><p>${r.narrative}</p></div>` : ""}
      ${text ? html`<div><div class="row between" style="margin-bottom:6px"><div class="label" style="margin:0">Full text</div>
        <div class="row"><button class="btn btn-sm" data-print>${icon("printer")} Print</button><button class="btn btn-sm" data-copy>Copy</button></div></div><div class="note-text">${text}</div></div>` : ""}
      ${r.duplicates?.length ? html`<div><div class="label">Also reported by</div>
        <div class="list card">${r.duplicates.map((dup) => html`<div class="list-item small"><span class="grow">${dup.source_name}</span>
          ${["conditions", "allergies", "medications"].includes(r.category) ? statusBadge(dup.status) : ""}
          <span class="muted">${fmtDate(dup.effective_at)}</span></div>`)}</div>
        <p class="hint">Syntropy merges the same fact reported by several institutions into one entry.</p></div>` : ""}
      ${r.editable ? html`<div class="row"><span class="small muted grow">You entered this result.</span>
        <button class="btn btn-sm btn-ghost btn-danger" data-delete>${icon("trash")} Delete</button></div>` : ""}
      <details><summary class="small muted" style="cursor:pointer">Original FHIR resource</summary>
        <pre class="raw">${JSON.stringify(r.raw, null, 2)}</pre></details>
    </div>`;
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
  el.querySelector("[data-leave]")?.addEventListener("click", () => leave());
  el.querySelector("[data-print]")?.addEventListener("click", () => {
    printLetter(`${r.title}\n${fmtDate(r.effective_at)}${r.source_name ? ` · ${r.source_name}` : ""}\n\n${text}`);
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
