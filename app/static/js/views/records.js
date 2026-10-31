import { get } from "../api.js";
import { setToolbar } from "../app.js";
import { openLabEntry } from "./lab-entry.js";
import { openRecord } from "./record-detail.js";
import { recordValue } from "../units.js";
import {
  $, CATEGORY_ICON, debounce, emptyState, flagBadge, html, icon, loading, mount, onAction, recordDate, resultStatus, statusBadge,
} from "../ui.js";

const PAGE = 100;

function rowMeta(r) {
  const d = r.details || {};
  const bits = [recordDate(r)];
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
    return html`<div class="value">${r.category === "vitals" ? recordValue(r) : r.value_text || ""}</div>${resultStatus(r.status)}${flagBadge(r.interpretation)}`;
  }
  if (r.category === "goals" && r.value_text) return html`<span class="small muted">${r.value_text}</span>${statusBadge(r.status)}`;
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
      <div class="row wrap"><div class="search" style="min-width:240px">${icon("search")}<input class="input" id="rec-q" placeholder="Search records, codes, notes…" aria-label="Search records" value="${q}"></div>
        ${appBar ? "" : html`<a class="btn" href="#/report" title="A summary to print or save as PDF">${icon("printer")} Visit summary</a>
        <button class="btn btn-primary" data-action="add-labs">${icon("plus")} Add lab results</button>`}</div></div>
    ${connection ? html`<div class="banner info"><div class="grow">Showing records from one source only.</div><a class="btn btn-sm" href="#/records">Show all</a></div>` : ""}
    <div class="chips" id="rec-cats" style="margin-bottom:10px"></div>
    <div class="row wrap" id="rec-filters" style="gap:8px;margin-bottom:16px"></div>
    <div class="card" id="rec-list"></div>`);

  const drawCats = () => mount($("#rec-cats", el), [
    html`<button class="chip ${category === "" ? "active" : ""}" data-action="cat" data-cat="">All</button>`,
    ...nonEmpty.map((c) => html`<button class="chip ${category === c.key ? "active" : ""}" data-action="cat" data-cat="${c.key}">
      ${c.label}<span class="n">${c.count}</span></button>`),
  ]);

  const narrow = { kind: "", note_type: "", author: "", connection: connection || "", since: "", until: "" };
  const KIND_WORD = { lab: "Lab reports", imaging: "Imaging", other: "Other reports" };
  const select = (key, label, options) => (options.length ? html`<select class="input input-sm" data-narrow="${key}" aria-label="${label}" style="width:auto">
    <option value="">${label}</option>${options.map(([v, t]) => html`<option value="${v}" ${narrow[key] === String(v) ? "selected" : ""}>${t}</option>`)}</select>` : "");
  const drawFilters = async () => {
    const f = await get("/api/record-filters", { profile: profile.id, category: category || undefined }).catch(() => null);
    const box = $("#rec-filters", el);
    if (!f || !box) return;
    mount(box, html`
      ${select("connection", "All sources", f.sources.length > 1 || narrow.connection ? f.sources.map((s) => [s.id, s.name]) : [])}
      ${select("kind", "All reports", f.kinds.map((k) => [k, KIND_WORD[k] || k]))}
      ${select("note_type", "All note types", f.note_types.map((t) => [t, t]))}
      ${select("author", "All authors", f.authors.map((a) => [a, a]))}
      <label class="row small muted" style="gap:4px">From <input class="input input-sm" type="date" data-narrow="since" value="${narrow.since}" style="width:auto"></label>
      <label class="row small muted" style="gap:4px">To <input class="input input-sm" type="date" data-narrow="until" value="${narrow.until}" style="width:auto"></label>`);
    box.querySelectorAll("[data-narrow]").forEach((c) => c.addEventListener("change", () => { narrow[c.dataset.narrow] = c.value; load(); }));
  };

  const drawList = () => {
    const list = $("#rec-list", el);
    if (!items.length) {
      mount(list, nonEmpty.length ? emptyState("No matching records", q ? `Nothing matches “${q}”.` : "This category is empty.")
        : emptyState("No records yet", "Connect a health system or import a file to see your records here.",
          html`<a class="btn btn-primary" href="#/sources?add=ehr">${icon("plus")} Connect a health system</a>`));
      return;
    }
    const doses = new Map();
    if (category === "immunizations") {
      for (const r of items) doses.set(r.title.trim().toLowerCase(), [...(doses.get(r.title.trim().toLowerCase()) || []), r]);
    }
    mount(list, html`
      <div class="list">${category === "immunizations" ? [...doses.values()].map((group) => html`
        <div class="list-item ${group.length === 1 ? "clickable" : ""}" ${group.length === 1 ? html`data-action="open" data-id="${group[0].id}"` : ""} style="${group.length > 1 ? "display:block" : ""}">
          ${group.length === 1 ? html`<span class="ev-icon">${icon(CATEGORY_ICON.immunizations)}</span><div class="grow" style="min-width:0"><div class="title truncate">${group[0].title}</div><div class="meta truncate">${rowMeta(group[0])}</div></div>${rowRight(group[0])}`
            : html`<div class="title">${group[0].title} <span class="badge">${group.length} doses</span></div>
              <div class="meta">${group.map((r, i) => html`${i ? ", " : ""}<a href="#" data-action="open" data-id="${r.id}">${recordDate(r)}</a>`)}</div>`}
        </div>`) : items.map((r) => html`
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
    const res = await get("/api/records", { profile: profile.id, category: category || undefined, q: q || undefined, connection: narrow.connection || undefined,
      kind: narrow.kind || undefined, note_type: narrow.note_type || undefined, author: narrow.author || undefined, since: narrow.since || undefined, until: narrow.until || undefined,
      limit: PAGE, offset });
    if (seq !== loadSeq) return;      // a newer search or category answered first (or will): this one is stale
    total = res.total;
    items = append ? items.concat(res.items) : res.items;
    drawList();
  };

  drawCats();
  drawFilters();
  await load();
  const search = debounce(() => { q = $("#rec-q", el).value.trim(); load(); }, 250);
  $("#rec-q", el).addEventListener("input", search);

  return onAction(el, {
    cat: ({ cat }) => {
      category = cat;
      Object.assign(narrow, { kind: "", note_type: "", author: "" });
      drawCats(); drawFilters();
      history.replaceState(null, "", `#/records${cat ? "/" + cat : ""}`);
      load();
    },
    open: ({ id }) => openRecord(id, () => load()),
    "add-labs": () => openLabEntry(profile, () => navigate("#/records/labs")),
    more: () => { offset += PAGE; return load(true); },
  });
}
