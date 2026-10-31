// Everyone this account looks after, on one page: what's new, what's out of range, and how their sources are.
import { get } from "../api.js";
import { setProfile } from "../app.js";
import { $, fmtDate, html, icon, loading, mount, plural } from "../ui.js";

function facts(p) {
  const arrived = p.new_records == null ? "New records are counted from today" : p.new_records ? `${plural(p.new_records, "new record")} since ${fmtDate(p.new_since)}` : `Nothing new since ${fmtDate(p.new_since)}`;
  const bits = [arrived, p.flagged_labs ? `${plural(p.flagged_labs, "recent result")} out of range` : null,
    `${plural(p.sources, "source")}`, p.sources_need_signing_in ? `${p.sources_need_signing_in} needs signing in again` : null,
    p.sources_failed ? `${p.sources_failed} failed to sync` : null, p.sources_not_synced_lately ? `${p.sources_not_synced_lately} not synced lately` : null];
  return bits.filter(Boolean).join(" · ");
}

export async function render({ el }) {
  mount(el, loading(3));
  const { people } = await get("/api/profiles/at-a-glance", undefined, { fresh: true });
  mount(el, html`
    <div class="page-head"><div><h1>Your people</h1><p>Everyone you can see, without switching to each.</p></div></div>
    <div class="card"><div class="list" id="people-list">${people.map((p) => html`
      <button class="list-item" data-person="${p.id}" style="text-align:left;width:100%;background:none;border:0;cursor:pointer">
        <div class="grow" style="min-width:0"><div class="title">${p.name}</div><div class="meta">${facts(p)}</div></div>
        ${p.sources_need_signing_in + p.sources_failed + p.sources_not_synced_lately ? html`<span class="badge warn">Sources</span>` : ""}${icon("arrowRight")}</button>`)}</div></div>`);
  $("#people-list", el).addEventListener("click", (e) => {
    const b = e.target.closest("[data-person]");
    if (!b) return;
    setProfile(b.dataset.person);
    location.hash = "#/overview";
  });
}
