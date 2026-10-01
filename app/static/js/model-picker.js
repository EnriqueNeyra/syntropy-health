// The model picker: every AI that's connected (models on this computer, providers with a key, AI apps signed in here),
// grouped by provider with its logo, searchable, and usable from the keyboard. Ask and Settings both open it.

import { get } from "./api.js";
import { closeAnimated, html, icon, mount } from "./ui.js";
import { logo, logoId } from "./ai-logos.js";

const SHOWN = 5;      // models shown per provider before "Show all", unless searching
const TAG = { local: "Private", key: "Your key", agent: "Your plan" };

/** Where a group's data goes, as a short tag. */
function groupTag(g) {
  if (g.kind === "local") return g.private ? html`<span class="badge good">Private</span>` : html`<span class="badge">Your server</span>`;
  return html`<span class="badge">${TAG[g.kind]}</span>`;
}

const sameChoice = (o, cur) => cur && o.provider === cur.provider && (o.agent || null) === (cur.agent || null) && o.model === cur.model;

/**
 * Opens the picker under ``anchor``. ``onPick(option, group)`` is called with the chosen model; the picker closes
 * first. Returns a function that closes it.
 */
export function openModelPicker(anchor, { onPick, manageHref = "#/settings/ai" } = {}) {
  const menu = document.createElement("div");
  menu.className = "popover model-menu";
  menu.setAttribute("role", "dialog");
  menu.setAttribute("aria-label", "Choose a model");
  menu.innerHTML = `<div class="model-menu-loading">Finding your models…</div>`;
  document.body.appendChild(menu);
  const place = () => {
    const r = anchor.getBoundingClientRect();
    const width = Math.min(380, window.innerWidth - 24);
    menu.style.width = `${width}px`;
    menu.style.top = `${r.bottom + 6}px`;
    menu.style.left = `${Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12))}px`;
    menu.style.maxHeight = `${Math.max(240, window.innerHeight - r.bottom - 24)}px`;
  };
  place();
  anchor.setAttribute("aria-expanded", "true");

  let data = null;
  let query = "";
  const expanded = new Set();
  let flat = [];        // the options drawn, in order, for the keyboard
  let active = -1;

  const close = () => {
    closeAnimated(menu);
    anchor.setAttribute("aria-expanded", "false");
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", keys, true);
    window.removeEventListener("resize", place);
  };
  const outside = (e) => { if (!menu.contains(e.target) && !anchor.contains(e.target)) close(); };

  const matches = (o, g) => {
    if (!query) return true;
    const q = query.toLowerCase();
    return `${o.name} ${o.model} ${g.label}`.toLowerCase().includes(q);
  };

  const drawList = () => {
    flat = [];
    const groups = data.groups.map((g) => {
      const all = g.options.filter((o) => matches(o, g));
      if (!all.length) return "";
      const open = query || expanded.has(g.id) || all.length <= SHOWN + 1;
      const pinned = all.filter((o) => o.pinned);
      const shown = open ? all : [...pinned, ...all.filter((o) => !o.pinned)].slice(0, Math.max(SHOWN, pinned.length));
      return html`<div class="model-group" role="group" aria-label="${g.label}">
        <div class="model-group-head">${logo(logoId({ provider: g.kind === "agent" ? "agent" : g.id, agent: g.agent, label: g.label }), g.label, "", g.kind === "local" ? "monitor" : "globe")}
          <span class="grow truncate">${g.label}</span>${groupTag(g)}</div>
        ${g.kind === "local" && !g.reachable ? html`<div class="model-empty">Not reachable right now. Is it running?</div>` : ""}
        ${shown.map((o) => {
          const i = flat.push({ o, g }) - 1;
          const on = sameChoice(o, data.current);
          return html`<button class="model-option ${on ? "current" : ""}" role="option" aria-selected="${on}" data-i="${i}" id="mo-${i}">
            <span class="grow" style="min-width:0"><span class="model-name truncate">${o.name}</span>
              ${o.name !== o.model && o.model ? html`<span class="model-id truncate">${o.model}</span>` : ""}</span>
            ${o.tools === false ? html`<span class="model-tag" title="Can't look things up: answers from a summary of your data">Summary</span>` : ""}
            ${on ? icon("check") : ""}</button>`;
        })}
        ${!open ? html`<button class="model-more" data-expand="${g.id}">Show all ${all.length} models ${icon("chevronDown")}</button>` : ""}
      </div>`;
    });
    const any = groups.some(Boolean);
    mount(menu.querySelector(".model-list"), any ? groups
      : html`<div class="model-empty">${data.groups.length ? `No models match “${query}”.` : "No AI is connected yet."}</div>`);
    active = flat.findIndex(({ o }) => sameChoice(o, data.current));
    highlight(false);
  };

  const highlight = (scroll = true) => {
    menu.querySelectorAll(".model-option.active").forEach((b) => b.classList.remove("active"));
    const b = active >= 0 ? menu.querySelector(`#mo-${active}`) : null;
    if (b) { b.classList.add("active"); if (scroll) b.scrollIntoView({ block: "nearest" }); }
  };

  const pick = (i) => { const hit = flat[i]; if (!hit) return; close(); onPick?.(hit.o, hit.g); };

  const keys = (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); anchor.focus(); return; }
    if (!flat.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = e.key === "ArrowDown" ? Math.min(flat.length - 1, active + 1) : Math.max(0, active - 1);
      highlight();
    } else if (e.key === "Enter" && active >= 0 && menu.contains(document.activeElement)) {
      e.preventDefault();
      pick(active);
    }
  };

  setTimeout(() => document.addEventListener("mousedown", outside, true));
  document.addEventListener("keydown", keys, true);
  window.addEventListener("resize", place);

  get("/api/ai/choices", null, { fresh: true }).then((res) => {
    if (!menu.isConnected) return;
    data = res;
    const total = data.groups.reduce((n, g) => n + g.options.length, 0);
    mount(menu, html`${total > 8 ? html`<div class="model-search">${icon("search")}<input class="input" type="search" placeholder="Search models"
          aria-label="Search models" autocomplete="off" spellcheck="false"></div>` : ""}
      <div class="model-list" role="listbox"></div>
      <a class="model-manage" href="${manageHref}">${icon("settings")}<span>Manage AI connections</span></a>`);
    drawList();
    const input = menu.querySelector("input");
    (input || menu.querySelector(".model-option.current") || menu.querySelector(".model-option"))?.focus();
    input?.addEventListener("input", () => { query = input.value.trim(); drawList(); if (flat.length) { active = 0; highlight(); } });
  }).catch((err) => { if (menu.isConnected) menu.textContent = err.message; });

  menu.addEventListener("click", (e) => {
    const more = e.target.closest("[data-expand]");
    if (more) { expanded.add(more.dataset.expand); drawList(); return; }
    const b = e.target.closest("[data-i]");
    if (b) pick(Number(b.dataset.i));
    if (e.target.closest(".model-manage")) close();
  });
  return close;
}
