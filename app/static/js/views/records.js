import { get } from "../api.js";
import { setToolbar } from "../app.js";
import { openLabEntry } from "./lab-entry.js";
import { openRecord } from "./record-detail.js";
import { recordValue } from "../units.js";
import {
  $, CATEGORY_ICON, debounce, emptyState, flagBadge, fmtDate, html, icon, loading, mount, onAction, statusBadge,
} from "../ui.js";

const PAGE = 100;

function rowMeta(r) {
  const d = r.details || {};
  const bits = [fmtDate(r.effective_at)];
  if (r.category === "medications" && r.value_text) bits.push(r.value_text);
  else if (r.category === "encounters") bits.push(...(d.clinicians || []).slice(0, 1), ...(d.reasons || []).slice(0, 1));
  else if (r.category === "allergies" && r.narrative) bits.push(r.narrative);
  else if (r.category === "notes") bits.push(...(d.authors || []).slice(0, 1));
  else if (r.category === "immunizations" && d.manufacturer) bits.push(d.manufacturer);
  else if (r.category === "care_team") bits.push(r.narrative);
  const n = (r.sources || []).length;
  bits.push(n > 1 ? `${n} sources` : r.source_name);
  return bits.filter(Boolean).join(" · ");
}

function rowRight(r) {
  if (["labs", "vitals", "observations"].includes(r.category)) {
    return html`<div class="value">${r.category === "vitals" ? recordValue(r) : r.value_text || ""}</div>${flagBadge(r.interpretation)}`;
  }
  if (r.category === "goals" && r.value_text) return html`<span class="small muted">${r.value_text}</span>`;
  return statusBadge(r.status);
}

export async function render({ el, parts, params, state, navigate }) {
  const profile = state.profile;
  let category = parts[0] || params.get("category") || "";
  let q = params.get("q") || "";
  const connection = params.get("connection") || undefined;
  let offset = 0;
  let items = [];
  let total = 0;

  const cats = (await get("/api/categories", { profile: profile.id })).categories;
  const nonEmpty = cats.filter((c) => c.count > 0);
  if (!category && nonEmpty.length) category = "";

  // In the iPhone app, the visit summary and adding lab results are in the title bar.
  const appBar = setToolbar([
    { id: "report", title: "Visit Summary", symbol: "doc.text", run: () => navigate("#/report") },
    { id: "add-labs", title: "Add Lab Results", symbol: "plus", run: () => openLabEntry(profile, () => navigate("#/records/labs")) },
  ]);
  mount(el, html`
    <div class="page-head"><div><h1>Records</h1><p>Everything from every source, merged and de-duplicated.</p></div>
      <div class="row wrap"><div class="search" style="min-width:240px">${icon("search")}<input class="input" id="rec-q" placeholder="Search records, codes, notes…" value="${q}"></div>
        ${appBar ? "" : html`<a class="btn" href="#/report" title="A one-page summary to print or save as PDF">${icon("printer")} Visit summary</a>
        <button class="btn btn-primary" data-action="add-labs">${icon("plus")} Add lab results</button>`}</div></div>
    ${connection ? html`<div class="banner info"><div class="grow">Showing records from one source only.</div><a class="btn btn-sm" href="#/records">Show all</a></div>` : ""}
    <div class="chips" id="rec-cats" style="margin-bottom:16px"></div>
    <div class="card" id="rec-list"></div>`);

  const drawCats = () => mount($("#rec-cats", el), [
    html`<button class="chip ${category === "" ? "active" : ""}" data-action="cat" data-cat="">All</button>`,
    ...nonEmpty.map((c) => html`<button class="chip ${category === c.key ? "active" : ""}" data-action="cat" data-cat="${c.key}">
      ${c.label}<span class="n">${c.count}</span></button>`),
  ]);

  const drawList = () => {
    const list = $("#rec-list", el);
    if (!items.length) {
      mount(list, nonEmpty.length ? emptyState("No matching records", q ? `Nothing matches “${q}”.` : "This category is empty.")
        : emptyState("No records yet", "Connect a health system or import a file to see your records here.",
          html`<a class="btn btn-primary" href="#/sources?add=ehr">${icon("plus")} Connect a health system</a>`));
      return;
    }
    mount(list, html`
      <div class="list">${items.map((r) => html`
        <div class="list-item clickable" data-action="open" data-id="${r.id}">
          <span class="ev-icon">${icon(CATEGORY_ICON[r.category] || "records")}</span>
          <div class="grow" style="min-width:0"><div class="title truncate">${r.title}</div><div class="meta truncate">${rowMeta(r)}</div></div>
          ${rowRight(r)}
        </div>`)}</div>
      <div class="card-body row between small muted"><span>${items.length} of ${total}</span>
        ${items.length < total ? html`<button class="btn btn-sm" data-action="more">Load more</button>` : ""}</div>`);
  };

  let loadSeq = 0;
  const load = async (append = false) => {
    const seq = ++loadSeq;
    if (!append) { offset = 0; mount($("#rec-list", el), loading(4)); }
    const res = await get("/api/records", { profile: profile.id, category: category || undefined, q: q || undefined, connection, limit: PAGE, offset });
    if (seq !== loadSeq) return;      // a newer search or category answered first (or will): this one is stale
    total = res.total;
    items = append ? items.concat(res.items) : res.items;
    drawList();
  };

  drawCats();
  await load();
  const search = debounce(() => { q = $("#rec-q", el).value.trim(); load(); }, 250);
  $("#rec-q", el).addEventListener("input", search);

  return onAction(el, {
    cat: ({ cat }) => { category = cat; drawCats(); history.replaceState(null, "", `#/records${cat ? "/" + cat : ""}`); load(); },
    open: ({ id }) => openRecord(id, () => load()),
    "add-labs": () => openLabEntry(profile, () => navigate("#/records/labs")),
    more: () => { offset += PAGE; return load(true); },
  });
}
