// One clinical record, in a drawer (or its own screen in the iPhone app): normalized fields, sources, note text and raw FHIR.

import { del, get } from "../api.js";
import { appBridge, nativeNav } from "../app.js";
import { recordValue } from "../units.js";
import {
  capitalize, CATEGORY_ICON, confirmDialog, flagBadge, fmtDate, fmtDateTime, fmtNum, html, icon, loading, modal, mount, statusBadge, toast,
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
        ["Result", r.category === "vitals" ? recordValue(r) : r.value_text], ["Interpretation", flagBadge(r.interpretation) || (r.interpretation ? capitalize(r.interpretation) : null)],
        ["Reference range", r.ref_text], ["As reported", d.original_value],
        ["Panel", d.panel], ["Measured", fmtDateTime(r.effective_at)],
        ["Components", (d.components || []).map((c) => `${c.name}: ${c.value ?? c.text} ${c.unit || ""}`)],
      ];
    case "medications":
      return [...common, ["Instructions", d.dosage], ["As needed", d.as_needed ? "Yes" : null], ["Prescriber", d.prescriber],
              ["Refills", d.refills], ["Reason", d.reason]];
    case "conditions":
      return [...common, ["Onset", fmtDate(r.effective_at)], ["Resolved", r.effective_end ? fmtDate(r.effective_end) : null],
              ["ICD-10", d.icd10], ["SNOMED CT", d.snomed], ["Verification", d.verification], ["Severity", d.severity]];
    case "allergies":
      return [...common, ["Category", d.category], ["Criticality", d.criticality],
              ["Reactions", (d.reactions || []).map((x) => `${x.manifestation || "Reaction"}${x.severity ? ` (${x.severity})` : ""}`)]];
    case "immunizations":
      return [...common, ["Dose", d.dose], ["Lot", d.lot], ["Manufacturer", d.manufacturer], ["Site", d.site], ["Route", d.route]];
    case "encounters":
      return [...common, ["Ended", r.effective_end ? fmtDateTime(r.effective_end) : null], ["Type", d.class],
              ["Reason", d.reasons], ["Clinicians", d.clinicians], ["Location", d.locations], ["Organization", d.provider]];
    case "procedures":
      return [...common, ["Performed by", d.performers], ["Reason", d.reasons], ["Outcome", d.outcome], ["Body site", d.body_site]];
    case "reports":
      return [...common, ["Type", d.categories], ["Performed by", d.performer]];
    case "notes":
      return [...common, ["Type", d.type], ["Authors", d.authors]];
    case "care_team":
      return [["Members", (d.members || []).map((m) => `${m.name} — ${m.role}`)]];
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
      ${kv(detailRows(r))}
      ${r.results?.length ? html`<div><div class="label">Results</div>
        <div class="list card">${r.results.map((x) => html`<button class="list-item small" data-open-result="${x.id}"
          style="text-align:left;width:100%;background:none;border:0;cursor:pointer">
          <span class="grow">${x.title}</span>
          <b>${x.category === "vitals" ? recordValue(x) : x.value_num != null ? `${fmtNum(x.value_num)}${x.unit ? ` ${x.unit}` : ""}` : x.value_text || "—"}</b>
          ${x.interpretation && x.interpretation !== "normal" ? html`<span class="badge warn">${x.interpretation}</span>` : ""}</button>`)}</div></div>` : ""}
      ${r.narrative && !text ? html`<div><div class="label">Notes</div><p>${r.narrative}</p></div>` : ""}
      ${text ? html`<div><div class="row between" style="margin-bottom:6px"><div class="label" style="margin:0">Full text</div>
        <button class="btn btn-sm" data-copy>Copy</button></div><div class="note-text">${text}</div></div>` : ""}
      ${r.duplicates?.length ? html`<div><div class="label">Also reported by</div>
        <div class="list card">${r.duplicates.map((dup) => html`<div class="list-item small"><span class="grow">${dup.source_name}</span>
          <span class="muted">${fmtDate(dup.effective_at)}</span></div>`)}</div>
        <p class="hint">Syntropy merges the same fact reported by several institutions into one entry.</p></div>` : ""}
      ${r.editable ? html`<div class="row"><span class="small muted grow">You entered this result.</span>
        <button class="btn btn-sm btn-ghost btn-danger" data-delete>${icon("trash")} Delete</button></div>` : ""}
      <details><summary class="small muted" style="cursor:pointer">Original FHIR resource</summary>
        <pre class="raw">${JSON.stringify(r.raw, null, 2)}</pre></details>
    </div>`;
}

const fullText = (r) => (r.category === "notes" ? r.narrative : (r.category === "reports" ? (r.details?.full_text || r.narrative) : null));

/** The record's buttons: another result, delete (a result entered by hand), copy the note. `done` runs after a delete. */
function wire(el, r, text, { leave, done }) {
  el.querySelectorAll("[data-open-result]").forEach((b) => b.addEventListener("click", () => {
    leave(); openRecord(b.dataset.openResult);
  }));
  el.querySelector("[data-delete]")?.addEventListener("click", async () => {
    if (!(await confirmDialog("Delete result", `Delete ${r.title} from ${fmtDate(r.effective_at)}?`, { confirmLabel: "Delete", danger: true }))) return;
    await del(`/api/labs/${encodeURIComponent(r.id)}`);
    toast("Deleted");
    done();
  });
  const copy = el.querySelector("[data-copy]");
  if (copy) copy.addEventListener("click", async () => { await navigator.clipboard.writeText(text); toast("Copied"); });
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
