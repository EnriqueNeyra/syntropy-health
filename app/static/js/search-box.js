// The search box at the top of the page: records appear as you type, the arrow keys move through them, and Enter opens the
// chosen one, or all the results in Records when none is chosen.
import { get } from "./api.js";
import { CATEGORY_ICON, closeAnimated, debounce, html, icon, mount, recordDate } from "./ui.js";

const SHOWN = 8;

export function searchBox(form, { profileId, navigate }) {
  const input = form.querySelector("input");
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.autocomplete = "off";
  let menu = null;
  let options = [];
  let chosen = -1;
  let seq = 0;

  const close = () => {
    seq++;
    if (menu) closeAnimated(menu);
    menu = null;
    options = [];
    chosen = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  };
  const place = () => {
    const r = input.getBoundingClientRect();
    const width = Math.min(460, window.innerWidth - 24);
    menu.style.width = `${width}px`;
    menu.style.top = `${r.bottom + 6}px`;
    menu.style.left = `${Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12))}px`;
  };
  const choose = (i) => {
    chosen = i;
    options.forEach((o, k) => o.classList.toggle("active", k === i));
    if (i >= 0) {
      options[i].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", options[i].id);
    } else input.removeAttribute("aria-activedescendant");
  };
  const showAll = (q) => {
    close();
    input.value = "";
    input.blur();
    navigate(q ? `#/records?q=${encodeURIComponent(q)}` : "#/records");
  };
  const open = async (row) => {
    close();
    input.value = "";
    input.blur();
    (await import("./views/record-detail.js")).openRecord(row.dataset.id);
  };

  const look = debounce(async () => {
    const q = input.value.trim();
    if (!q) { close(); return; }
    const mine = ++seq;
    let res;
    try { res = await get("/api/records", { profile: profileId(), q, limit: SHOWN }); } catch { return; }
    // Typed on, or left the box, while this was on its way.
    if (mine !== seq || document.activeElement !== input) return;
    if (!menu) {
      menu = document.createElement("div");
      menu.className = "popover search-results";
      menu.id = "search-results";
      menu.setAttribute("role", "listbox");
      menu.setAttribute("aria-label", "Search results");
      document.body.append(menu);
      input.setAttribute("aria-controls", menu.id);
      input.setAttribute("aria-expanded", "true");
      place();
      // The box keeps the focus, so typing carries on while a result is being chosen with the mouse.
      menu.addEventListener("mousedown", (e) => e.preventDefault());
      menu.addEventListener("click", (e) => {
        const row = e.target.closest("[data-id]");
        if (row) open(row);
        else if (e.target.closest("[data-all]")) showAll(input.value.trim());
      });
    }
    const rows = res.items;
    mount(menu, rows.length ? html`
      <div class="search-rows">${rows.map((r, i) => html`<div class="search-row" role="option" id="search-${i}" data-id="${r.id}" aria-selected="false">
        <span class="ev-icon">${icon(CATEGORY_ICON[r.category] || "records")}</span>
        <div class="grow"><div class="truncate"><b>${r.title}</b></div><div class="small muted truncate">${[recordDate(r), r.source_name].filter(Boolean).join(" · ")}</div></div>
        ${r.value_text ? html`<span class="small" style="flex:none">${r.value_text}</span>` : ""}</div>`)}</div>
      <button class="search-all" type="button" data-all>See all ${res.total > SHOWN ? res.total.toLocaleString() : ""} results in Records ${icon("arrowRight")}</button>`
      : html`<div class="small muted" style="padding:14px 16px">Nothing on record matches “${q}”.</div>`);
    options = [...menu.querySelectorAll(".search-row")];
    chosen = -1;
  }, 160);

  input.addEventListener("input", look);
  input.addEventListener("focus", () => { if (input.value.trim()) look(); });
  input.addEventListener("blur", close);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { input.value = ""; close(); input.blur(); return; }
    if (!options.length || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    const by = e.key === "ArrowDown" ? 1 : -1;
    choose(chosen + by < -1 ? options.length - 1 : chosen + by >= options.length ? -1 : chosen + by);
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (chosen >= 0 && options[chosen]) open(options[chosen]);
    else showAll(input.value.trim());
  });
  window.addEventListener("hashchange", close);
  window.addEventListener("resize", () => { if (menu) place(); });
}
