// Dependency-free SVG charts: time-series lines (with reference band), daily bars, sparklines.
// Marks follow fixed specs: 2px lines, r=4 markers with a surface ring, 4px rounded bar ends,
// hairline recessive grid, one y-axis only. The pointer only has to be near a value: every chart snaps to the closest one,
// by mouse, touch or the arrow keys, and its tooltip stays inside the chart.

import { esc, fmtNum, fmtShortDate, fmtDate, parseDate } from "./ui.js";

const PAD = { top: 14, right: 16, bottom: 26, left: 44 };
const RAIL = 18;             // the row above the plot where events (a medication, a diagnosis) sit
const INTRO_MARKS = 60;
let chartSeq = 0;

/** What a chart shows, in words, for those who can't see it: how many values, over which dates, the latest, lowest and highest. */
function summary(label, rows, show) {
  if (!rows.length) return label;
  const shown = (r) => `${r.prefix || ""}${show(r.value)}`;
  // Of equal values the limit is the one to name: "<5" says less than "5" does.
  const lowest = rows.reduce((a, r) => (r.value < a.value || (r.value === a.value && /^[<≤]/.test(r.prefix)) ? r : a));
  const highest = rows.reduce((a, r) => (r.value > a.value || (r.value === a.value && /^[>≥]/.test(r.prefix)) ? r : a));
  const span = rows.length > 1 ? ` from ${fmtDate(rows[0].day)} to ${fmtDate(rows.at(-1).day)}` : ` on ${fmtDate(rows[0].day)}`;
  return `${label}: ${rows.length} value${rows.length === 1 ? "" : "s"}${span}. Latest ${shown(rows.at(-1))}, lowest ${shown(lowest)}, highest ${shown(highest)}.`;
}

/** The chart's values as a table under it, drawn when it's opened, and still open if the chart is redrawn. */
function valuesTable(container, rows, show, wasOpen) {
  if (!rows.length) return;
  const box = document.createElement("details");
  box.className = "chart-table";
  box.innerHTML = `<summary class="small muted">Show as table</summary>`;
  const fill = () => {
    if (box.querySelector("table") || !box.open) return;
    box.insertAdjacentHTML("beforeend", `<div class="table-wrap" tabindex="0"><table class="table"><thead><tr><th>Date</th><th>Value</th></tr></thead><tbody>${
      rows.slice().reverse().map((r) => `<tr><td>${esc(fmtDate(r.day))}</td><td>${esc(r.prefix || "")}${esc(show(r.value))}${r.series ? ` <span class="muted">${esc(r.series)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div>`);
  };
  box.addEventListener("toggle", fill);
  box.open = !!wasOpen;
  fill();
  container.appendChild(box);
}

function niceTicks(min, max, count = 4) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)) || 0); // "|| 0": counting up to zero from below stops at -0, which would print "-0"
  return { lo, hi, ticks };
}

/** [min, max] of numbers, without spreading them into arguments (years of points overflow the call stack). */
function extent(values) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  return [lo, hi];
}

/** The value in an ascending array closest to t. */
function closest(sorted, t) {
  let a = 0, b = sorted.length - 1;
  while (b - a > 1) { const m = (a + b) >> 1; if (sorted[m] < t) a = m; else b = m; }
  return Math.abs(sorted[b] - t) < Math.abs(sorted[a] - t) ? sorted[b] : sorted[a];
}

function tipBox(container) {
  let tip = container.querySelector(":scope > .chart-tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; tip.hidden = true; container.appendChild(tip); }
  return tip;
}

/** Axis numbers stay short: 8K, not 8,000. */
const COMPACT = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const axisNumber = (v) => (Math.abs(v) >= 1000 ? COMPACT.format(v) : fmtNum(v));

const DAY_MS = 86400000;
const shortMonth = (d) => d.toLocaleDateString(undefined, { month: "short" });

/**
 * Tick marks on real boundaries (the 1st of a month, the start of a year), so the axis reads like a calendar rather than
 * a run of arbitrary dates. Returns [{ t, label }], never closer together than the labels can bear.
 */
function timeTicks(tmin, tmax, width) {
  const most = Math.max(2, Math.floor(width / 76));
  const span = tmax - tmin;
  if (span < DAY_MS * 2) {
    const step = [1, 2, 3, 4, 6, 12].find((h) => span / (h * 3600000) <= most) || 12;
    const out = [];
    const d = new Date(tmin);
    d.setMinutes(0, 0, 0);
    d.setHours(Math.ceil(d.getHours() / step) * step);
    if (d.getTime() < tmin) d.setHours(d.getHours() + step);
    for (; d.getTime() <= tmax; d.setHours(d.getHours() + step)) out.push({ t: d.getTime(), label: d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) });
    return out;
  }
  if (span < DAY_MS * 60) {
    const step = [1, 2, 7, 14, 21].find((n) => span / (n * DAY_MS) <= most) || 28;
    const out = [];
    const d = new Date(tmin);
    d.setHours(12, 0, 0, 0);
    if (step >= 7) d.setDate(d.getDate() + ((8 - d.getDay()) % 7));     // weeks start on a Monday
    for (; d.getTime() <= tmax; d.setDate(d.getDate() + step)) if (d.getTime() >= tmin) out.push({ t: d.getTime(), label: DATE_SHORT_FMT(d) });
    return out;
  }
  const months = span / (DAY_MS * 30.44);
  const step = [1, 2, 3, 6, 12, 24, 60, 120].find((n) => months / n <= most) || 240;
  const out = [];
  const first = new Date(tmin);
  let y = first.getFullYear(), m = first.getMonth();
  m = Math.ceil(m / step) * step;
  if (step >= 12) { y = Math.ceil(y / (step / 12)) * (step / 12); m = 0; }
  for (;;) {
    const d = new Date(y, m, 1, 12);
    if (d.getTime() > tmax) break;
    if (d.getTime() >= tmin) out.push({ t: d.getTime(), label: step >= 12 ? String(d.getFullYear()) : m % 12 === 0 ? String(d.getFullYear()) : shortMonth(d) });
    m += step;
    if (m > 11) { y += Math.floor(m / 12); m %= 12; }
  }
  return out;
}
const DATE_SHORT_FMT = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });

/**
 * Puts the tooltip beside the value being read, never over it, and inside the chart. It stays level at the top of the plot,
 * so as the pointer moves it glides along rather than hopping up and down with each value.
 */
function placeTip(tip, container, x, top) {
  const wasHidden = tip.hidden;
  tip.hidden = false;
  const w = tip.offsetWidth;
  const left = x + 14 + w <= container.clientWidth ? x + 14 : Math.max(0, x - 14 - w);
  tip.classList.toggle("moving", !wasHidden);
  tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}

/**
 * The pointer layer every chart shares. `at(x, y)` is given a position in the chart's own units and answers with what's
 * nearest, or null; `show(found)` draws it; `hide()` clears it. A finger scrubs sideways and scrolls up and down, and with
 * the keyboard the arrow keys step through `count` values. `onPick(found)` is a click. On a touch screen the first tap only
 * shows the value, since opening something would hide what was tapped to read.
 */
function pointerLayer(svgEl, width, height, { at, show, hide, step, onPick }) {
  let current = null;
  const locate = (ev) => {
    const r = svgEl.getBoundingClientRect();
    return at(((ev.clientX - r.left) / r.width) * width, ((ev.clientY - r.top) / r.height) * height);
  };
  const set = (found) => {
    current = found;
    if (found) show(found); else hide();
  };
  svgEl.style.touchAction = "pan-y";
  svgEl.addEventListener("pointermove", (ev) => set(locate(ev)));
  svgEl.addEventListener("pointerleave", (ev) => { if (ev.pointerType !== "touch") set(null); });
  svgEl.addEventListener("pointercancel", () => set(null));
  if (onPick) {
    svgEl.classList.add("pickable");
    let shownBefore = null;
    svgEl.addEventListener("pointerdown", (ev) => { shownBefore = ev.pointerType === "touch" ? current : undefined; });
    svgEl.addEventListener("click", (ev) => {
      const found = locate(ev);
      if (!found) return;
      if (shownBefore !== undefined && !(shownBefore && shownBefore.key === found.key)) { set(found); return; }
      onPick(found);
    });
  }
  if (step) {
    svgEl.tabIndex = 0;
    svgEl.addEventListener("keydown", (ev) => {
      const by = { ArrowLeft: -1, ArrowRight: 1 }[ev.key];
      if (ev.key === "Escape") { set(null); return; }
      if (!by) return;
      ev.preventDefault();
      set(step(current, by));
    });
    svgEl.addEventListener("blur", () => set(null));
  }
}

/**
 * series: [{ name, points: [{ t, v, flag?, label? }], alt? }]
 * opts: { unit, band: {low, high}, height, digits, goal, markers: [{ t, label }], range: [tmin, tmax] }
 */
export function lineChart(container, series, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const hasMarkers = Boolean(opts.markers?.length);
  const pad = { ...PAD, top: PAD.top + (hasMarkers ? RAIL : 0) };
  const height = (opts.height || 240) + (hasMarkers ? RAIL : 0);
  const all = series.flatMap((s) => s.points.map((p) => ({ ...p, time: parseDate(p.t)?.getTime() }))).filter((p) => p.time);
  if (!all.length) { container.innerHTML = `<div class="empty small">No data points yet.</div>`; return; }
  let [vmin, vmax] = extent(all.map((p) => p.v));
  if (opts.band) {
    if (opts.band.low != null) vmin = Math.min(vmin, opts.band.low);
    if (opts.band.high != null) vmax = Math.max(vmax, opts.band.high);
  }
  if (opts.goal != null) { vmin = Math.min(vmin, opts.goal); vmax = Math.max(vmax, opts.goal); }
  const padV = (vmax - vmin) * 0.12 || Math.abs(vmax) * 0.1 || 1;
  // Values that can't go below zero (steps, minutes) never get a negative axis from the padding.
  const { lo, hi, ticks } = niceTicks(vmin >= 0 ? Math.max(0, vmin - padV) : vmin - padV, vmax + padV);
  let [tmin, tmax] = extent(all.map((p) => p.time));
  if (opts.range) { tmin = Math.min(tmin, parseDate(opts.range[0]).getTime()); tmax = Math.max(tmax, parseDate(opts.range[1]).getTime()); }
  if (tmin === tmax) { tmin -= DAY_MS * 15; tmax += DAY_MS * 15; }
  // A little room each side, so the first and last points aren't cut by the edge of the plot.
  const inset = 8;
  const iw = width - pad.left - pad.right, ih = height - pad.top - pad.bottom;
  const x = (t) => pad.left + inset + ((t - tmin) / (tmax - tmin)) * (iw - inset * 2);
  const y = (v) => pad.top + ih - ((v - lo) / (hi - lo)) * ih;

  const showLine = (v) => `${fmtNum(v, opts.digits)}${opts.unit ? ` ${opts.unit}` : ""}`;
  const lineRows = series.flatMap((s) => s.points.filter((p) => parseDate(p.t)).map((p) => ({ day: p.t, value: p.v, prefix: p.prefix, time: parseDate(p.t).getTime(), series: series.length > 1 ? s.name : "" })))
    .sort((a, b) => a.time - b.time);
  const chartLabel = opts.label || series.map((s) => s.name).join(", ");
  // Systolic and diastolic are two measures: one "latest, lowest, highest" across them would describe neither.
  const spoken = series.length > 1
    ? [`${chartLabel}.`, ...series.map((s) => summary(s.name, lineRows.filter((r) => r.series === s.name), showLine))].join(" ")
    : summary(chartLabel, lineRows, showLine);
  const lineOpen = container.querySelector(":scope > .chart-table")?.open;
  const gradient = `chart-fade-${++chartSeq}`;
  let svg = `<svg class="${intro(container, all.length)}" viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(spoken)}">`;
  svg += `<defs><linearGradient id="${gradient}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" class="fade-top"/><stop offset="1" class="fade-end"/></linearGradient></defs>`;
  svg += `<g class="axis">`;
  for (const tk of ticks) {
    svg += `<line class="grid-line" x1="${pad.left}" x2="${width - pad.right}" y1="${y(tk)}" y2="${y(tk)}"/>`;
    svg += `<text x="${pad.left - 8}" y="${y(tk) + 3.5}" text-anchor="end">${esc(axisNumber(tk))}</text>`;
  }
  for (const tk of timeTicks(tmin, tmax, iw)) {
    const tx = x(tk.t);
    svg += `<text x="${tx}" y="${height - 6}" text-anchor="${tx < pad.left + 24 ? "start" : tx > width - pad.right - 24 ? "end" : "middle"}">${esc(tk.label)}</text>`;
  }
  svg += `</g>`;
  if (opts.band && (opts.band.low != null || opts.band.high != null)) {
    const top = y(opts.band.high != null ? opts.band.high : hi);
    const bottom = y(opts.band.low != null ? opts.band.low : lo);
    svg += `<rect class="band" x="${pad.left}" width="${iw}" y="${top}" height="${Math.max(0, bottom - top)}"/>`;
  }
  if (opts.goal != null) svg += goalLine(opts.goal, y(opts.goal), width, opts.digits);
  const rail = eventRail(opts.markers, (m) => { const t = parseDate(m.t)?.getTime(); return t >= tmin && t <= tmax ? x(t) : null; });
  svg += rail.svg;
  const drawn = series.map((s) => s.points.map((p) => ({ ...p, time: parseDate(p.t)?.getTime() })).filter((p) => p.time).sort((a, b) => a.time - b.time));
  series.forEach((s, i) => {
    const pts = drawn[i];
    if (!pts.length) return;
    const d = pts.map((p, n) => `${n ? "L" : "M"}${x(p.time).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    if (series.length === 1 && pts.length > 1 && !(opts.band && (opts.band.low != null || opts.band.high != null))) {
      svg += `<path class="area" fill="url(#${gradient})" d="${d}L${x(pts[pts.length - 1].time).toFixed(1)},${pad.top + ih}L${x(pts[0].time).toFixed(1)},${pad.top + ih}Z"/>`;
    }
    svg += `<path class="line${s.alt ? " alt" : ""}${s.faint ? " faint" : ""}" d="${d}" pathLength="1"/>`;
    // Every point only while they have room to be told apart. Otherwise the ones that matter, and the rest on hover.
    const roomy = pts.length <= 40 && (iw - inset * 2) / pts.length >= 16;
    pts.forEach((p, n) => {
      if (roomy || p.flag || p.bound || n === pts.length - 1) {
        svg += `<circle class="pt${s.alt ? " alt" : ""}${p.flag ? " flag" : ""}${p.bound ? " bound" : ""}" cx="${x(p.time)}" cy="${y(p.v)}" r="4"/>`;
      }
    });
  });
  svg += `<line class="cursor" y1="${pad.top}" y2="${pad.top + ih}" x1="-10" x2="-10" visibility="hidden"/>`;
  svg += series.map((s, i) => `<circle class="hover-dot${s.alt ? " alt" : ""}" data-series="${i}" r="5.5" cx="-10" cy="-10" visibility="hidden"/>`).join("");
  svg += `<rect class="hit" x="0" y="0" width="${width}" height="${height}" fill="transparent"/></svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
  rail.attach(container);
  if (!opts.noTable) valuesTable(container, lineRows, showLine, lineOpen);

  const tip = tipBox(container);
  const cursor = container.querySelector(".cursor");
  const svgEl = container.querySelector("svg");
  const dots = [...container.querySelectorAll(".hover-dot")];
  const points = all.map((p) => ({ ...p, px: x(p.time), py: y(p.v) })).sort((a, b) => a.px - b.px);
  const times = [...new Set(points.map((p) => p.time))];
  const rowsAt = (time) => series.map((s, i) => {
    const p = drawn[i].find((q) => q.time === time);
    return p ? { s, i, p } : null;
  }).filter(Boolean);
  const nearest = (px, py) => {
    // Close to the pointer across the chart, and where several are about as close, the one nearest it up and down.
    const closestX = points.reduce((best, p) => (Math.abs(p.px - px) < Math.abs(best.px - px) ? p : best), points[0]);
    let best = closestX, score = Infinity;
    for (const p of points) {
      const dx = Math.abs(p.px - px);
      if (dx > Math.abs(closestX.px - px) + 10) continue;
      const d = dx * dx + (py == null ? 0 : ((p.py - py) * (p.py - py)) / 4);
      if (d < score) { score = d; best = p; }
    }
    return best;
  };
  const clear = () => {
    tip.hidden = true;
    cursor.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    rail.highlight(null);
  };
  pointerLayer(svgEl, width, height, {
    at: (px, py) => {
      if (px < pad.left - 8 || px > width - pad.right + 8) return null;
      const event = py < pad.top ? rail.near(px) : null;
      if (event) return { key: `e${event.x}`, event };
      const p = nearest(px, py);
      return { key: p.time, time: p.time, px: p.px, py: p.py };
    },
    show: (found) => {
      if (found.event) {
        dots.forEach((d) => d.setAttribute("visibility", "hidden"));
        cursor.setAttribute("x1", found.event.x); cursor.setAttribute("x2", found.event.x); cursor.setAttribute("visibility", "visible");
        rail.highlight(found.event);
        tip.innerHTML = found.event.items.map((m) => `<div class="tip-when">${esc(fmtDate(m.t))}</div><div class="tip-marker">${esc(m.label)}</div>`).join("");
        placeTip(tip, container, (found.event.x / width) * container.clientWidth, pad.top);
        return;
      }
      rail.highlight(null);
      const rows = rowsAt(found.time);
      cursor.setAttribute("x1", found.px); cursor.setAttribute("x2", found.px); cursor.setAttribute("visibility", "visible");
      dots.forEach((d, i) => {
        const hit = rows.find((r) => r.i === i);
        d.setAttribute("visibility", hit ? "visible" : "hidden");
        if (hit) { d.setAttribute("cx", x(hit.p.time)); d.setAttribute("cy", y(hit.p.v)); d.classList.toggle("flag", Boolean(hit.p.flag)); }
      });
      const when = tmax - tmin < DAY_MS * 2
        ? new Date(found.time).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : fmtDate(new Date(found.time));
      tip.innerHTML = `<div class="tip-when">${esc(when)}</div>${rows.map(({ s, p }) => `<div><b>${esc(p.prefix || "")}${esc(fmtNum(p.v, opts.digits))}</b>${opts.unit ? ` ${esc(opts.unit)}` : ""}${series.length > 1 ? ` <span class="tip-name">${esc(s.name)}</span>` : ""}${p.label ? `<div class="tip-name">${esc(p.label)}</div>` : ""}</div>`).join("")}${markerNotes(opts.markers, found.time)}`;
      const scale = container.clientWidth / width;
      placeTip(tip, container, found.px * scale, pad.top);
    },
    hide: clear,
    step: (current, by) => {
      const at = current?.time ? times.indexOf(current.time) : by > 0 ? -1 : times.length;
      const time = times[Math.max(0, Math.min(times.length - 1, at + by))];
      const p = points.find((q) => q.time === time);
      return { key: time, time, px: p.px, py: p.py };
    },
    onPick: opts.onPointClick && ((found) => { if (found.time) opts.onPointClick(points.find((p) => p.time === found.time)); }),
  });
}

/**
 * Daily bars. points: [{ day, value, source, parts?: { key: hours } }]
 * opts: { unit, avg, digits, goal, markers: [{ t, label }], stack: [{ key, name, color }], format, tick }. `format(v)` writes a
 * value with its unit (hours as "7h 13m") for the average and goal lines and the tooltip; `tick(v)` labels the axis.
 * With ``stack`` each bar is split into its parts, bottom up in the stack's order.
 */
export function barChart(container, points, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const hasMarkers = Boolean(opts.markers?.length);
  const pad = { ...PAD, top: PAD.top + (hasMarkers ? RAIL : 0) };
  const height = (opts.height || 220) + (hasMarkers ? RAIL : 0);
  if (!points.length) { container.innerHTML = `<div class="empty small">No data for this range.</div>`; return; }
  const fmt = opts.format || ((v) => `${fmtNum(v, opts.digits)}${opts.unit ? ` ${opts.unit}` : ""}`);
  const barRows = points.filter((p) => p.value != null).map((p) => ({ day: p.day, value: p.value }));
  const barOpen = container.querySelector(":scope > .chart-table")?.open;
  const vmax = Math.max(extent(points.map((p) => p.value))[1], opts.avg || 0, opts.goal || 0);
  const { hi, ticks } = niceTicks(0, vmax * 1.08);
  const iw = width - pad.left - pad.right, ih = height - pad.top - pad.bottom;
  const slot = iw / points.length;
  const bw = Math.min(24, Math.max(2, slot - 2));
  const y = (v) => pad.top + ih - (v / hi) * ih;
  let svg = `<svg class="${intro(container, points.length)}" viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(summary(opts.label || "Daily values", barRows, fmt))}"><g class="axis">`;
  for (const tk of ticks) {
    svg += `<line class="grid-line" x1="${pad.left}" x2="${width - pad.right}" y1="${y(tk)}" y2="${y(tk)}"/>`;
    svg += `<text x="${pad.left - 8}" y="${y(tk) + 3.5}" text-anchor="end">${esc(opts.tick ? opts.tick(tk) : axisNumber(tk))}</text>`;
  }
  const labelEvery = Math.ceil(points.length / Math.max(2, Math.floor(iw / 70)));
  const last = points.length - 1;
  points.forEach((p, i) => {
    // The last day always gets its date; a regular label too close to it is left out rather than overlapping.
    if (i === last || (i % labelEvery === 0 && (last - i) * slot >= 56)) {
      svg += `<text x="${pad.left + slot * i + slot / 2}" y="${height - 6}" text-anchor="middle">${esc(p.tickLabel || fmtShortDate(p.day))}</text>`;
    }
  });
  svg += `</g>`;
  const base = pad.top + ih;
  points.forEach((p, i) => {
    const bx = pad.left + slot * i + (slot - bw) / 2;
    if (opts.stack && p.parts) {
      let acc = 0;
      const shown = opts.stack.filter((st) => (p.parts[st.key] || 0) > 0);
      shown.forEach((st, k) => {
        const lo = y(acc), top = y(acc + p.parts[st.key]);
        acc += p.parts[st.key];
        const h = Math.max(1, lo - top), r = k === shown.length - 1 ? Math.min(4, bw / 2, h) : 0;
        const d = `M${bx},${lo}V${top + r}Q${bx},${top} ${bx + r},${top}H${bx + bw - r}Q${bx + bw},${top} ${bx + bw},${top + r}V${lo}Z`;
        svg += `<path class="bar seg" d="${d}" style="fill:${st.color};--d:${Math.round((i / points.length) * 280)}ms" data-i="${i}"/>`;
      });
      return;
    }
    const top = y(p.value), h = Math.max(1, base - top), r = Math.min(4, bw / 2, h);
    // Rounded data-end, square at the baseline.
    const d = `M${bx},${base}V${top + r}Q${bx},${top} ${bx + r},${top}H${bx + bw - r}Q${bx + bw},${top} ${bx + bw},${top + r}V${base}Z`;
    svg += `<path class="bar${p.partial ? " partial" : ""}" d="${d}" data-i="${i}" style="--d:${Math.round((i / points.length) * 280)}ms"/>`;
  });
  if (opts.avg) {
    svg += `<line class="avg" x1="${pad.left}" x2="${width - pad.right}" y1="${y(opts.avg)}" y2="${y(opts.avg)}"/>`;
    // Above the plot: over the bars it couldn't be read.
    svg += `<text x="${width - pad.right}" y="${pad.top - 5}" text-anchor="end" class="axis avg-label" style="fill:var(--text-3);font-size:10.5px">avg ${esc(fmt(opts.avg))}</text>`;
  }
  if (opts.goal != null) svg += goalLine(opts.goal, y(opts.goal), width, opts.digits, fmt);
  const dayIndex = new Map(points.map((p, i) => [p.day, i]));
  const rail = eventRail(opts.markers, (m) => {
    const i = dayIndex.get(String(m.t).slice(0, 10));
    return i == null ? null : pad.left + slot * i + slot / 2;
  });
  svg += rail.svg;
  svg += `<rect class="slot" y="${pad.top}" width="${Math.max(slot, 4)}" height="${ih}" x="-99"/>`;
  svg += `<rect class="hit" x="0" y="0" width="${width}" height="${height}" fill="transparent"/></svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
  rail.attach(container);
  if (!opts.noTable) valuesTable(container, barRows, fmt, barOpen);
  const tip = tipBox(container);
  const svgEl = container.querySelector("svg");
  const slotEl = container.querySelector(".slot");
  const barsAt = new Map();
  for (const b of container.querySelectorAll(".bar[data-i]")) {
    if (!barsAt.has(b.dataset.i)) barsAt.set(b.dataset.i, []);
    barsAt.get(b.dataset.i).push(b);
  }
  let lit = [];
  const light = (i) => {
    lit.forEach((b) => b.classList.remove("lit"));
    lit = i == null ? [] : barsAt.get(String(i)) || [];
    lit.forEach((b) => b.classList.add("lit"));
    slotEl.setAttribute("x", i == null ? -99 : pad.left + slot * i);
  };
  pointerLayer(svgEl, width, height, {
    at: (px, py) => {
      if (py < pad.top && rail.near(px)) return { key: `e${rail.near(px).x}`, event: rail.near(px) };
      if (px < pad.left - 8 || px > width - pad.right + 8) return null;
      const i = Math.max(0, Math.min(points.length - 1, Math.floor((px - pad.left) / slot)));
      return { key: i, i };
    },
    show: ({ i, event }) => {
      if (event) {
        light(null);
        rail.highlight(event);
        tip.innerHTML = event.items.map((m) => `<div class="tip-when">${esc(fmtDate(m.t))}</div><div class="tip-marker">${esc(m.label)}</div>`).join("");
        placeTip(tip, container, (event.x / width) * container.clientWidth, pad.top);
        return;
      }
      rail.highlight(null);
      light(i);
      const p = points[i];
      const parts = opts.stack && p.parts ? opts.stack.filter((st) => p.parts[st.key] > 0)
        .map((st) => `<div class="tip-name"><span class="tip-swatch" style="background:${st.color}"></span>${esc(st.name)} ${esc(fmt(p.parts[st.key]))}</div>`).join("") : "";
      tip.innerHTML = `<div class="tip-when">${esc(p.tipLabel || fmtDate(p.day))}${p.partial ? " · so far" : ""}</div><b>${esc(fmt(p.value))}</b>${parts}${p.source ? `<div class="tip-name">${esc(p.source)}</div>` : ""}${markerNotes(opts.markers, p.day)}`;
      const scale = container.clientWidth / width;
      placeTip(tip, container, (pad.left + slot * i + slot / 2) * scale, pad.top);
    },
    hide: () => { tip.hidden = true; light(null); rail.highlight(null); },
    step: (current, by) => {
      const at = current?.i ?? (by > 0 ? -1 : points.length);
      const i = Math.max(0, Math.min(points.length - 1, at + by));
      return { key: i, i };
    },
  });
}

/**
 * "intro" the first time a chart is drawn in its container (it draws itself in), "" when it's redrawn (a resize). A chart
 * with more marks than can animate smoothly fades in instead.
 */
function intro(container, marks = 0) {
  if (container.dataset.drawn) return "";
  container.dataset.drawn = "1";
  if (!visible()) return "";
  return marks > INTRO_MARKS ? "intro-fade" : "intro";
}

/** Whether anyone can see the page. A chart drawn while it's hidden (another screen in the iPhone app, a background
 * tab) is drawn complete: the animation might never run there, which left a line out. */
function visible() { return document.visibilityState === "visible"; }

/** A dashed goal line with its value at the right end. */
function goalLine(goal, gy, width, digits, fmt) {
  const text = fmt ? fmt(goal) : fmtNum(goal, digits);
  return `<line class="goal" x1="${PAD.left}" x2="${width - PAD.right}" y1="${gy}" y2="${gy}"/>`
    + `<text class="goal-label" x="${PAD.left + 4}" y="${gy - 5}">goal ${esc(text)}</text>`;
}

/** The events on the day under the cursor, for the tooltip. */
function markerNotes(markers, when) {
  if (!markers?.length) return "";
  const day = fmtDate(typeof when === "number" ? new Date(when) : when);
  return markers.filter((m) => fmtDate(m.t) === day).map((m) => `<div class="tip-marker">${esc(m.label)}</div>`).join("");
}

/**
 * Events (a medication started, a diagnosis) as small marks in a row above the plot, so they never get between the pointer
 * and the values. Events too close to tell apart share one mark with a count. Pointing at one shows what it is.
 */
function eventRail(markers, xOf) {
  const placed = (markers || []).map((m) => ({ m, x: xOf(m) })).filter((e) => e.x != null).sort((a, b) => a.x - b.x);
  const groups = [];
  for (const e of placed) {
    const last = groups.at(-1);
    if (last && e.x - last.x < 14) last.items.push(e.m);
    else groups.push({ x: e.x, items: [e.m] });
  }
  const row = PAD.top + 2;
  const svg = groups.length ? `<g class="events" aria-hidden="true">${groups.map((g, i) => `<g class="event" data-event="${i}">`
    + `<path d="M${g.x},${row - 5}L${g.x + 5},${row}L${g.x},${row + 5}L${g.x - 5},${row}Z"/>${g.items.length > 1 ? `<text x="${g.x + 8}" y="${row + 3.5}">${g.items.length}</text>` : ""}</g>`).join("")}</g>` : "";
  let nodes = [];
  return {
    svg,
    attach: (container) => { nodes = [...container.querySelectorAll(".event")]; },
    near: (px) => groups.reduce((best, g) => (Math.abs(g.x - px) < 14 && (!best || Math.abs(g.x - px) < Math.abs(best.x - px)) ? g : best), null),
    highlight: (group) => { nodes.forEach((n, i) => n.classList.toggle("lit", groups[i] === group)); },
  };
}

/**
 * Floating bars from one clock time to another, one per day: when you went to bed and got up.
 * rows: [{ day, from, to }] in minutes after noon (so 23:00 is 660 and 07:00 is 1140). opts: { format(minutes) }.
 */
export function rangeChart(container, rows, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const height = opts.height || 220;
  if (!rows.length) { container.innerHTML = `<div class="empty small">No nights in this range.</div>`; return; }
  const fmt = opts.format || ((m) => String(m));
  const wasOpen = container.querySelector(":scope > .chart-table")?.open;
  const lo = Math.floor((extent(rows.map((r) => r.from))[0] - 30) / 60) * 60;
  const hi = Math.ceil((extent(rows.map((r) => r.to))[1] + 30) / 60) * 60;
  const iw = width - PAD.left - PAD.right, ih = height - PAD.top - PAD.bottom;
  const slot = iw / rows.length, bw = Math.min(18, Math.max(3, slot - 3));
  const y = (m) => PAD.top + ((m - lo) / (hi - lo)) * ih;     // earlier times at the top
  const step = (hi - lo) / 60 > 8 ? 120 : 60;
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(`${opts.label || "Bedtime and wake time"}: ${rows.length} night${rows.length === 1 ? "" : "s"} from ${fmtDate(rows[0].day)} to ${fmtDate(rows.at(-1).day)}.`)}"><g class="axis">`;
  for (let m = lo; m <= hi; m += step) {
    svg += `<line class="grid-line" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(m)}" y2="${y(m)}"/>`;
    svg += `<text x="${PAD.left - 8}" y="${y(m) + 3.5}" text-anchor="end">${esc(fmt(m))}</text>`;
  }
  const labelEvery = Math.ceil(rows.length / Math.max(2, Math.floor(iw / 76)));
  rows.forEach((r, i) => {
    if (i % labelEvery === 0 || i === rows.length - 1) svg += `<text x="${PAD.left + slot * i + slot / 2}" y="${height - 6}" text-anchor="middle">${esc(fmtShortDate(r.day))}</text>`;
  });
  svg += `</g>`;
  if (opts.usual) {
    for (const [m, name] of [[opts.usual.from, "usual bedtime"], [opts.usual.to, "usual wake time"]]) {
      if (m != null) svg += `<line class="avg" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(m)}" y2="${y(m)}"><title>${esc(name)} ${esc(fmt(m))}</title></line>`;
    }
  }
  rows.forEach((r, i) => {
    const bx = PAD.left + slot * i + (slot - bw) / 2, top = y(r.from), h = Math.max(2, y(r.to) - top), rr = Math.min(4, bw / 2, h / 2);
    svg += `<rect class="bar" x="${bx}" y="${top}" width="${bw}" height="${h}" rx="${rr}"/>`;
  });
  svg += `<rect class="slot" y="${PAD.top}" width="${Math.max(slot, 4)}" height="${ih}" x="-99"/></svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
  valuesTable(container, rows.map((r) => ({ day: r.day, value: `${fmt(r.from)} – ${fmt(r.to)}` })), String, wasOpen);
  const tip = tipBox(container);
  const slotEl = container.querySelector(".slot");
  pointerLayer(container.querySelector("svg"), width, height, {
    at: (px) => {
      if (px < PAD.left - 8 || px > width - PAD.right + 8) return null;
      const i = Math.max(0, Math.min(rows.length - 1, Math.floor((px - PAD.left) / slot)));
      return { key: i, i };
    },
    show: ({ i }) => {
      const r = rows[i];
      slotEl.setAttribute("x", PAD.left + slot * i);
      tip.innerHTML = `<div class="tip-when">${esc(fmtDate(r.day))}</div><b>${esc(fmt(r.from))} – ${esc(fmt(r.to))}</b>`;
      const scale = container.clientWidth / width;
      placeTip(tip, container, (PAD.left + slot * i + slot / 2) * scale, PAD.top);
    },
    hide: () => { tip.hidden = true; slotEl.setAttribute("x", -99); },
    step: (current, by) => {
      const at = current?.i ?? (by > 0 ? -1 : rows.length);
      const i = Math.max(0, Math.min(rows.length - 1, at + by));
      return { key: i, i };
    },
  });
}

/**
 * One night stage by stage. segments: [{ start, end, stage }] (ISO times); lanes: [{ key, name, color }], top to bottom.
 */
export function hypnogram(container, segments, lanes, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const laneH = 26, top = 6, left = 58, right = PAD.right;
  const height = top + lanes.length * laneH + 24;
  const segs = segments.map((s) => ({ ...s, a: parseDate(s.start).getTime(), b: parseDate(s.end).getTime() })).filter((s) => s.b > s.a);
  if (!segs.length) { container.innerHTML = ""; return; }
  const t0 = extent(segs.map((s) => s.a))[0], t1 = extent(segs.map((s) => s.b))[1];
  const iw = width - left - right;
  const x = (t) => left + ((t - t0) / (t1 - t0)) * iw;
  const lane = new Map(lanes.map((l, i) => [l.key, i]));
  const clock = (t) => new Date(t).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(opts.label || "Sleep stages through the night")}"><g class="axis">`;
  lanes.forEach((l, i) => {
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${top + i * laneH + laneH / 2}" y2="${top + i * laneH + laneH / 2}"/>`;
    svg += `<text x="${left - 8}" y="${top + i * laneH + laneH / 2 + 3.5}" text-anchor="end">${esc(l.name)}</text>`;
  });
  const n = Math.min(6, Math.max(2, Math.floor(iw / 90)));
  for (let i = 0; i <= n; i++) {
    const t = t0 + ((t1 - t0) * i) / n;
    svg += `<text x="${x(t)}" y="${height - 6}" text-anchor="${i === 0 ? "start" : i === n ? "end" : "middle"}">${esc(clock(t))}</text>`;
  }
  svg += `</g>`;
  for (const s of segs) {
    const i = lane.get(s.stage);
    if (i == null) continue;
    const w = Math.max(1, x(s.b) - x(s.a));
    svg += `<rect x="${x(s.a)}" y="${top + i * laneH + 4}" width="${w}" height="${laneH - 8}" rx="${Math.min(3, w / 2)}" style="fill:${lanes[i].color}">`
      + `<title>${esc(`${lanes[i].name} ${clock(s.a)}–${clock(s.b)}`)}</title></rect>`;
  }
  svg += `</svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
}

/**
 * Two measures against each other, one dot per day.
 * points: [{ x, y, day }]; opts: { xLabel, yLabel, fx(v), fy(v) }
 */
export function scatterChart(container, points, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const height = opts.height || 260;
  if (points.length < 3) { container.innerHTML = `<div class="empty small">Not enough days with both measures.</div>`; return; }
  const pad = { ...PAD, bottom: 40, left: 52 };
  const fx = opts.fx || ((v) => fmtNum(v)), fy = opts.fy || ((v) => fmtNum(v));
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const xt = niceTicks(...extent(xs)), yt = niceTicks(...extent(ys));
  const iw = width - pad.left - pad.right, ih = height - pad.top - pad.bottom;
  const x = (v) => pad.left + ((v - xt.lo) / (xt.hi - xt.lo)) * iw;
  const y = (v) => pad.top + ih - ((v - yt.lo) / (yt.hi - yt.lo)) * ih;
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(`${opts.yLabel} against ${opts.xLabel}`)}"><g class="axis">`;
  for (const tk of yt.ticks) {
    svg += `<line class="grid-line" x1="${pad.left}" x2="${width - pad.right}" y1="${y(tk)}" y2="${y(tk)}"/>`;
    svg += `<text x="${pad.left - 8}" y="${y(tk) + 3.5}" text-anchor="end">${esc(fy(tk))}</text>`;
  }
  xt.ticks.forEach((tk) => { svg += `<text x="${x(tk)}" y="${pad.top + ih + 16}" text-anchor="middle">${esc(fx(tk))}</text>`; });
  svg += `<text x="${pad.left + iw / 2}" y="${height - 4}" text-anchor="middle">${esc(opts.xLabel || "")}</text></g>`;
  points.forEach((p, i) => { svg += `<circle class="pt dot" cx="${x(p.x)}" cy="${y(p.y)}" r="4" data-i="${i}"/>`; });
  svg += `<circle class="hover-dot" r="5.5" cx="-10" cy="-10" visibility="hidden"/></svg>`;
  container.classList.add("chart");
  const scatterOpen = container.querySelector(":scope > .chart-table")?.open;
  container.innerHTML = svg;
  valuesTable(container, points.map((p) => ({ day: p.day, value: `${opts.xLabel} ${fx(p.x)}, ${opts.yLabel} ${fy(p.y)}` })), String, scatterOpen);
  const tip = tipBox(container);
  const hover = container.querySelector(".hover-dot");
  pointerLayer(container.querySelector("svg"), width, height, {
    at: (px, py) => {
      const near = points.reduce((best, p) => { const d = (x(p.x) - px) ** 2 + (y(p.y) - py) ** 2; return d < best.d ? { p, d } : best; }, { d: Infinity });
      return near.p && near.d <= 40 ** 2 ? { key: near.p.day, p: near.p } : null;
    },
    show: ({ p }) => {
      hover.setAttribute("cx", x(p.x)); hover.setAttribute("cy", y(p.y)); hover.setAttribute("visibility", "visible");
      tip.innerHTML = `<div class="tip-when">${esc(fmtDate(p.day))}</div><div><b>${esc(fx(p.x))}</b> <span class="tip-name">${esc(opts.xLabel)}</span></div><div><b>${esc(fy(p.y))}</b> <span class="tip-name">${esc(opts.yLabel)}</span></div>`;
      const scale = container.clientWidth / width;
      placeTip(tip, container, x(p.x) * scale, pad.top);
    },
    hide: () => { tip.hidden = true; hover.setAttribute("visibility", "hidden"); },
  });
}

/** Tiny inline sparkline (returns SVG markup). */
export function sparkline(values) {
  const vals = values.filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return "";
  const w = 160, h = 34, pad = 5;
  const [min, max] = extent(vals);
  const span = max - min || 1;
  const pts = vals.map((v, i) => [pad + (i / (vals.length - 1)) * (w - pad * 2), h - pad - ((v - min) / span) * (h - pad * 2)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  // The line stretches to the tile's shape. The latest value's dot is drawn over it in page units, or it would stretch too.
  return `<span class="spark ${visible() ? "intro" : ""}" aria-hidden="true"><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" vector-effect="non-scaling-stroke"/></svg>`
    + `<i style="left:${((last[0] / w) * 100).toFixed(2)}%;top:${((last[1] / h) * 100).toFixed(2)}%"></i></span>`;
}

/** Re-render charts when their container width changes. */
export function responsive(container, draw) {
  let last = 0;
  const ro = new ResizeObserver(() => {
    const w = container.clientWidth;
    if (Math.abs(w - last) > 8) { last = w; draw(); }
  });
  ro.observe(container);
  return () => ro.disconnect();
}
