import { get, post } from "../api.js";
import { setToolbar } from "../app.js";
import { openLabEntry } from "./lab-entry.js";
import { openRecord } from "./record-detail.js";
import { recordValue } from "../units.js";
import {
  $, CATEGORY_ICON, debounce, emptyState, fieldsChanged, flagBadge, html, icon, loadInto, modal, mount, onAction, parseDate, recordDate, resultStatus, showActiveChip, statusBadge, toast,
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
  if (r.override) bits.push(`You said: ${r.override.text}`);
  return bits.filter(Boolean).join(" · ");
}

/** The list by month, newest first, as the server sends it. A search answers by match, so it stays one list. */
function byMonth(items, q) {
  const months = [];
  for (const r of items) {
    const d = q ? null : parseDate(r.effective_at);
    // A source that gave only a year gave no month, so the record goes under the year.
    const label = q ? "" : /^\d{4}$/.test(r.effective_at) ? r.effective_at : d ? d.toLocaleDateString(undefined, { month: "long", year: "numeric" }) : "No date";
    if (months.at(-1)?.label !== label) months.push({ label, items: [] });
    months.at(-1).items.push(r);
  }
  return months.length === 1 && months[0].label === "No date" ? [{ label: "", items }] : months;
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
        <button class="btn btn-primary" data-action="add-labs">${icon("plus")} Add lab results</button>`}
        <button class="btn" data-action="add-own">${icon("plus")} Add a medication or allergy</button></div></div>
    ${connection ? html`<div class="banner info"><div class="grow">Showing records from one source only.</div><a class="btn btn-sm" href="#/records">Show all</a></div>` : ""}
    <div class="chips chips-strip" id="rec-cats" style="margin-bottom:10px"></div>
    <div class="row wrap" id="rec-filters" style="gap:8px;margin-bottom:16px"></div>
    <div class="card" id="rec-list"></div>`);

  const drawCats = () => {
    mount($("#rec-cats", el), [
      html`<button class="chip ${category === "" ? "active" : ""}" data-action="cat" data-cat="">All</button>`,
      ...nonEmpty.map((c) => html`<button class="chip ${category === c.key ? "active" : ""}" data-action="cat" data-cat="${c.key}">
        ${c.label}<span class="n">${c.count}</span></button>`),
    ]);
    showActiveChip($("#rec-cats", el));
  };

  const narrow = { kind: "", note_type: "", author: "", has: "", connection: connection || "", since: "", until: "" };
  const HAS_WORD = { documents: "With a document", corrected: "Your corrections" };
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
      ${select("has", "All entries", (f.has || []).map((k) => [k, HAS_WORD[k]]))}
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
      ${category === "immunizations" ? html`<div class="list">${[...doses.values()].map((group) => html`
        <div class="list-item ${group.length === 1 ? "clickable" : ""}" ${group.length === 1 ? html`data-action="open" data-id="${group[0].id}"` : ""} style="${group.length > 1 ? "display:block" : ""}">
          ${group.length === 1 ? html`<span class="ev-icon">${icon(CATEGORY_ICON.immunizations)}</span><div class="grow" style="min-width:0"><div class="title truncate">${group[0].title}</div><div class="meta truncate">${rowMeta(group[0])}</div></div>${rowRight(group[0])}`
            : html`<div class="title">${group[0].title} <span class="badge">${group.length} doses</span></div>
              <div class="meta">${group.map((r, i) => html`${i ? ", " : ""}<a href="#" data-action="open" data-id="${r.id}">${recordDate(r)}</a>`)}</div>`}
        </div>`)}</div>` : byMonth(items, q).map((m) => html`${m.label ? html`<div class="list-head"><b>${m.label}</b></div>` : ""}
      <div class="list">${m.items.map((r) => html`
        <div class="list-item clickable" data-action="open" data-id="${r.id}">
          <span class="ev-icon">${icon(CATEGORY_ICON[r.category] || "records")}</span>
          <div class="grow" style="min-width:0"><div class="title truncate">${r.title}</div><div class="meta truncate">${rowMeta(r)}</div></div>
          ${rowRight(r)}
        </div>`)}</div>`)}
      <div class="card-body row between small muted"><span>${items.length} of ${total}</span>
        ${items.length < total ? html`<button class="btn btn-sm" data-action="more">Load more</button>` : ""}</div>`);
  };

  let loadSeq = 0;
  const load = (append = false) => {
    const seq = ++loadSeq;
    if (!append) offset = 0;
    return append ? fetchInto(seq, append) : loadInto($("#rec-list", el), () => fetchInto(seq, append), { lines: 4 });
  };
  const fetchInto = async (seq, append) => {
    const res = await get("/api/records", { profile: profile.id, category: category || undefined, q: q || undefined, connection: narrow.connection || undefined,
      kind: narrow.kind || undefined, note_type: narrow.note_type || undefined, author: narrow.author || undefined, has: narrow.has || undefined, since: narrow.since || undefined, until: narrow.until || undefined,
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
      Object.assign(narrow, { kind: "", note_type: "", author: "", has: "" });
      drawCats(); drawFilters();
      history.replaceState(null, "", `#/records${cat ? "/" + cat : ""}`);
      load();
    },
    open: ({ id }) => openRecord(id, () => load()),
    "add-labs": () => openLabEntry(profile, () => navigate("#/records/labs")),
    "add-own": () => openSelfReported(profile, () => load()),
    more: () => { offset += PAGE; return load(true); },
  });
}

/** A medication, supplement or allergy no source lists. Goes in as the person's own word, and says so wherever it's shown. */
function openSelfReported(profile, done) {
  const m = modal({ title: "Add a medication or allergy", dirty: () => fieldsChanged(m.el), body: html`<form class="stack" id="own-form">
    <p class="small muted">For what your record is missing, such as something bought over the counter. It's shown as patient-reported.</p>
    <label class="field"><span class="label">What is it</span><select class="input" name="kind">
      <option value="medication">A medication</option><option value="supplement">A supplement</option><option value="allergy">An allergy</option></select></label>
    <label class="field"><span class="label">Name</span><input class="input" name="name" required maxlength="200"></label>
    <label class="field"><span class="label">How it's taken, or the reaction</span><input class="input" name="how" maxlength="500"></label>
    <label class="field"><span class="label">Since (optional)</span><input class="input" type="date" name="started"></label>
    <label class="field"><span class="label">Note (optional)</span><textarea class="input" name="note" rows="2" maxlength="2000"></textarea></label>
    <div class="row"><button class="btn btn-primary" type="submit">Add</button></div></form>` });
  m.el.querySelector("#own-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const name = String(f.get("name") || "").trim();
    if (!name) { toast("Type a name first.", "bad"); return; }
    try {
      await post("/api/records/self-reported", { profile_id: profile.id, kind: f.get("kind"), name,
        how: f.get("how") || null, started: f.get("started") || null, note: f.get("note") || null });
    } catch (err) { toast(err.message, "bad"); return; }
    m.close();
    toast("Added");
    done();
  });
}
