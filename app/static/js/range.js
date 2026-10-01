// A time range picker shared by Workouts and the Journal: preset spans plus a custom from/to, kept in the page's
// address (so a range can be bookmarked or shared) and remembered per page for next time.

import { html } from "./ui.js";

export const PRESETS = [["30", "30 days"], ["90", "90 days"], ["365", "1 year"], ["0", "All"], ["custom", "Custom"]];

const pad = (n) => String(n).padStart(2, "0");
/** A date as YYYY-MM-DD in local time. */
export const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const validDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "");

/** The range from the address, else the last one used on this page, else `fallback` days. */
export function readRange(params, storageKey, fallback = "90") {
  if (validDay(params.get("from")) || validDay(params.get("to"))) {
    return { preset: "custom", from: validDay(params.get("from")) ? params.get("from") : "", to: validDay(params.get("to")) ? params.get("to") : "" };
  }
  let preset = params.get("range");
  if (!preset) { try { preset = localStorage.getItem(storageKey); } catch {} }
  if (preset?.startsWith("custom:")) {
    const [, from = "", to = ""] = preset.split(":");
    return { preset: "custom", from: validDay(from) ? from : "", to: validDay(to) ? to : "" };
  }
  return { preset: PRESETS.some(([k]) => k === preset && k !== "custom") ? preset : fallback, from: "", to: "" };
}

export function saveRange(range, storageKey) {
  try { localStorage.setItem(storageKey, range.preset === "custom" ? `custom:${range.from}:${range.to}` : range.preset); } catch {}
}

/** Query parameters for the API: `days` (0 = everything) or `start`/`end` dates. */
export function rangeQuery(range) {
  if (range.preset === "custom") return { days: 0, start: range.from || undefined, end: range.to || undefined };
  return { days: Number(range.preset) };
}

/** The part of the address that describes the range. */
export function rangeHash(range) {
  if (range.preset === "custom") return new URLSearchParams(Object.entries({ from: range.from, to: range.to }).filter(([, v]) => v)).toString();
  return `range=${range.preset}`;
}

/** Words for headings: "Last 90 days", "All time", "Mar 1 – Apr 30, 2026". */
export function rangeLabel(range) {
  if (range.preset === "custom") {
    const fmt = (s) => new Date(`${s}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    if (range.from && range.to) return `${fmt(range.from)} – ${fmt(range.to)}`;
    if (range.from) return `Since ${fmt(range.from)}`;
    if (range.to) return `Until ${fmt(range.to)}`;
    return "All time";
  }
  if (range.preset === "0") return "All time";
  return range.preset === "365" ? "Last 12 months" : `Last ${range.preset} days`;
}

/** The chips, and the date fields when Custom is chosen. Clicks arrive as data-action="range" / inputs as data-range-*. */
export function rangePicker(range) {
  const today = isoDay(new Date());
  return html`<div class="range-picker">
    <div class="seg" role="group" aria-label="Time range">${PRESETS.map(([k, label]) =>
      html`<button class="${range.preset === k ? "active" : ""}" data-action="range" data-range="${k}" aria-pressed="${range.preset === k}">${label}</button>`)}</div>
    ${range.preset === "custom" ? html`<div class="range-dates">
      <label><span>From</span><input class="input" type="date" data-range-from value="${range.from}" max="${range.to || today}"></label>
      <label><span>To</span><input class="input" type="date" data-range-to value="${range.to}" min="${range.from}" max="${today}"></label>
    </div>` : ""}
  </div>`;
}

/**
 * Wires a picker inside `root`: calls `onChange(range)` when a chip is chosen or a date is changed. Returns a function
 * to handle the "range" action (for the page's onAction table).
 */
export function wireRange(root, getRange, onChange) {
  root.addEventListener("change", (e) => {
    const from = e.target.closest("[data-range-from]"), to = e.target.closest("[data-range-to]");
    if (!from && !to) return;
    const r = { ...getRange(), preset: "custom" };
    if (from) r.from = from.value;
    if (to) r.to = to.value;
    onChange(r);
  });
  return ({ range: preset }) => {
    const current = getRange();
    if (preset === current.preset) return;
    if (preset === "custom") {
      // Start from the span that was showing, so Custom begins somewhere sensible.
      const days = Number(current.preset) || 90;
      const from = new Date();
      from.setDate(from.getDate() - days + 1);
      return onChange({ preset, from: isoDay(from), to: isoDay(new Date()) });
    }
    return onChange({ preset, from: "", to: "" });
  };
}
