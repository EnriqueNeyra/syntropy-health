// Dependency-free SVG charts: time-series lines (with reference band), daily bars, sparklines.
// Marks follow fixed specs: 2px lines, r=4 markers with a surface ring, 4px rounded bar ends,
// hairline recessive grid, crosshair + tooltip on hover, one y-axis only.

import { esc, fmtNum, fmtShortDate, fmtDate, parseDate } from "./ui.js";

const PAD = { top: 14, right: 16, bottom: 26, left: 44 };

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
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return { lo, hi, ticks };
}

function tipBox(container) {
  let tip = container.querySelector(".chart-tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; tip.hidden = true; container.appendChild(tip); }
  return tip;
}

/**
 * series: [{ name, points: [{ t, v, flag?, label? }], alt? }]
 * opts: { unit, band: {low, high}, height, digits, goal, markers: [{ t, label }], range: [tmin, tmax] }
 */
export function lineChart(container, series, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const height = opts.height || 240;
  const all = series.flatMap((s) => s.points.map((p) => ({ ...p, time: parseDate(p.t)?.getTime() }))).filter((p) => p.time);
  if (!all.length) { container.innerHTML = `<div class="empty small">No data points yet.</div>`; return; }
  let vmin = Math.min(...all.map((p) => p.v)), vmax = Math.max(...all.map((p) => p.v));
  if (opts.band) {
    if (opts.band.low != null) vmin = Math.min(vmin, opts.band.low);
    if (opts.band.high != null) vmax = Math.max(vmax, opts.band.high);
  }
  if (opts.goal != null) { vmin = Math.min(vmin, opts.goal); vmax = Math.max(vmax, opts.goal); }
  const padV = (vmax - vmin) * 0.12 || Math.abs(vmax) * 0.1 || 1;
  // Values that can't go below zero (steps, minutes) never get a negative axis from the padding.
  const { lo, hi, ticks } = niceTicks(vmin >= 0 ? Math.max(0, vmin - padV) : vmin - padV, vmax + padV);
  let tmin = Math.min(...all.map((p) => p.time)), tmax = Math.max(...all.map((p) => p.time));
  if (opts.range) { tmin = Math.min(tmin, parseDate(opts.range[0]).getTime()); tmax = Math.max(tmax, parseDate(opts.range[1]).getTime()); }
  if (tmin === tmax) { tmin -= 86400000 * 15; tmax += 86400000 * 15; }
  const iw = width - PAD.left - PAD.right, ih = height - PAD.top - PAD.bottom;
  const x = (t) => PAD.left + ((t - tmin) / (tmax - tmin)) * iw;
  const y = (v) => PAD.top + ih - ((v - lo) / (hi - lo)) * ih;

  let svg = `<svg class="${intro(container)}" viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(opts.label || series.map((s) => s.name).join(", "))}">`;
  svg += `<g class="axis">`;
  for (const tk of ticks) {
    svg += `<line class="grid-line" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(tk)}" y2="${y(tk)}"/>`;
    svg += `<text x="${PAD.left - 8}" y="${y(tk) + 3.5}" text-anchor="end">${esc(fmtNum(tk))}</text>`;
  }
  const xticks = Math.min(6, Math.max(2, Math.floor(iw / 110)));
  for (let i = 0; i <= xticks; i++) {
    const t = tmin + ((tmax - tmin) * i) / xticks;
    const span = tmax - tmin > 86400000 * 400;
    const intraday = tmax - tmin < 86400000 * 2;   // e.g. a workout: clock times, not the same date repeated
    const label = span ? new Date(t).toLocaleDateString(undefined, { month: "short", year: "numeric" })
      : intraday ? new Date(t).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : fmtShortDate(new Date(t));
    svg += `<text x="${x(t)}" y="${height - 6}" text-anchor="${i === 0 ? "start" : i === xticks ? "end" : "middle"}">${esc(label)}</text>`;
  }
  svg += `</g>`;
  if (opts.band && (opts.band.low != null || opts.band.high != null)) {
    const top = y(opts.band.high != null ? opts.band.high : hi);
    const bottom = y(opts.band.low != null ? opts.band.low : lo);
    svg += `<rect class="band" x="${PAD.left}" width="${iw}" y="${top}" height="${Math.max(0, bottom - top)}"/>`;
    if (opts.band.high != null) svg += `<line class="band-edge" x1="${PAD.left}" x2="${width - PAD.right}" y1="${top}" y2="${top}"/>`;
    if (opts.band.low != null) svg += `<line class="band-edge" x1="${PAD.left}" x2="${width - PAD.right}" y1="${bottom}" y2="${bottom}"/>`;
  }
  if (opts.goal != null) svg += goalLine(opts.goal, y(opts.goal), width, opts.digits);
  svg += markerLines(opts.markers, (m) => { const t = parseDate(m.t)?.getTime(); return t >= tmin && t <= tmax ? x(t) : null; }, ih);
  series.forEach((s) => {
    const pts = s.points.map((p) => ({ ...p, time: parseDate(p.t)?.getTime() })).filter((p) => p.time).sort((a, b) => a.time - b.time);
    if (!pts.length) return;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.time).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    if (series.length === 1 && pts.length > 1 && !(opts.band && (opts.band.low != null || opts.band.high != null))) {
      svg += `<path class="area" d="${d}L${x(pts[pts.length - 1].time).toFixed(1)},${PAD.top + ih}L${x(pts[0].time).toFixed(1)},${PAD.top + ih}Z"/>`;
    }
    svg += `<path class="line${s.alt ? " alt" : ""}" d="${d}" pathLength="1"/>`;
    const showAll = pts.length <= 40;
    pts.forEach((p, i) => {
      if (showAll || p.flag || i === pts.length - 1) {
        svg += `<circle class="pt${s.alt ? " alt" : ""}${p.flag ? " flag" : ""}" cx="${x(p.time)}" cy="${y(p.v)}" r="4.5"/>`;
      }
    });
  });
  svg += `<line class="cursor" y1="${PAD.top}" y2="${PAD.top + ih}" x1="-10" x2="-10" visibility="hidden"/>`;
  svg += `<rect x="${PAD.left}" y="${PAD.top}" width="${iw}" height="${ih}" fill="transparent" data-hit/></svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;

  const tip = tipBox(container);
  const cursor = container.querySelector(".cursor");
  const svgEl = container.querySelector("svg");
  const times = [...new Set(all.map((p) => p.time))].sort((a, b) => a - b);
  svgEl.addEventListener("mousemove", (ev) => {
    const rect = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width;
    const t = tmin + ((px - PAD.left) / iw) * (tmax - tmin);
    const nearest = times.reduce((a, b) => (Math.abs(b - t) < Math.abs(a - t) ? b : a), times[0]);
    const rows = series.map((s) => {
      const p = s.points.find((q) => parseDate(q.t)?.getTime() === nearest);
      return p ? `<div>${series.length > 1 ? esc(s.name) + ": " : ""}<b>${esc(fmtNum(p.v, opts.digits))}</b> ${esc(opts.unit || "")}${p.label ? " · " + esc(p.label) : ""}</div>` : "";
    }).join("");
    if (!rows) return;
    const cx = x(nearest);
    cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx); cursor.setAttribute("visibility", "visible");
    const when = tmax - tmin < 86400000 * 2
      ? new Date(nearest).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : fmtDate(new Date(nearest));
    tip.innerHTML = `<div style="opacity:.7">${esc(when)}</div>${rows}${markerNotes(opts.markers, nearest)}`;
    tip.hidden = false;
    tip.style.left = `${(cx / width) * rect.width}px`;
    tip.style.top = `${PAD.top + 10}px`;
  });
  svgEl.addEventListener("mouseleave", () => { tip.hidden = true; cursor.setAttribute("visibility", "hidden"); cursor.setAttribute("x1", -10); cursor.setAttribute("x2", -10); });
  if (opts.onPointClick) {
    svgEl.addEventListener("click", () => {
      const cx = Number(cursor.getAttribute("x1"));
      if (cx < 0) return;
      const t = tmin + ((cx - PAD.left) / iw) * (tmax - tmin);
      const p = all.reduce((a, b) => (Math.abs(b.time - t) < Math.abs(a.time - t) ? b : a), all[0]);
      opts.onPointClick(p);
    });
  }
}

/**
 * Daily bars. points: [{ day, value, source, parts?: { key: hours } }]
 * opts: { unit, avg, digits, goal, markers: [{ t, label }], stack: [{ key, name, color }], format, tick }. `format(v)` writes a
 * value with its unit (hours as "7h 13m") for the average and goal lines and the tooltip; `tick(v)` labels the axis.
 * With ``stack`` each bar is split into its parts, bottom up in the stack's order.
 */
export function barChart(container, points, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const height = opts.height || 220;
  if (!points.length) { container.innerHTML = `<div class="empty small">No data for this range.</div>`; return; }
  const fmt = opts.format || ((v) => `${fmtNum(v, opts.digits)}${opts.unit ? ` ${opts.unit}` : ""}`);
  const vmax = Math.max(...points.map((p) => p.value), opts.avg || 0, opts.goal || 0);
  const { hi, ticks } = niceTicks(0, vmax * 1.08);
  const iw = width - PAD.left - PAD.right, ih = height - PAD.top - PAD.bottom;
  const slot = iw / points.length;
  const bw = Math.min(24, Math.max(2, slot - 2));
  const y = (v) => PAD.top + ih - (v / hi) * ih;
  let svg = `<svg class="${intro(container)}" viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(opts.label || "Daily values")}"><g class="axis">`;
  for (const tk of ticks) {
    svg += `<line class="grid-line" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(tk)}" y2="${y(tk)}"/>`;
    svg += `<text x="${PAD.left - 8}" y="${y(tk) + 3.5}" text-anchor="end">${esc(opts.tick ? opts.tick(tk) : fmtNum(tk))}</text>`;
  }
  const labelEvery = Math.ceil(points.length / Math.max(2, Math.floor(iw / 70)));
  const last = points.length - 1;
  points.forEach((p, i) => {
    // The last day always gets its date; a regular label too close to it is left out rather than overlapping.
    if (i === last || (i % labelEvery === 0 && (last - i) * slot >= 56)) {
      svg += `<text x="${PAD.left + slot * i + slot / 2}" y="${height - 6}" text-anchor="middle">${esc(p.tickLabel || fmtShortDate(p.day))}</text>`;
    }
  });
  svg += `</g>`;
  const base = PAD.top + ih;
  points.forEach((p, i) => {
    const bx = PAD.left + slot * i + (slot - bw) / 2;
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
    svg += `<line class="avg" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(opts.avg)}" y2="${y(opts.avg)}"/>`;
    svg += `<text x="${width - PAD.right}" y="${y(opts.avg) - 5}" text-anchor="end" class="axis avg-label" style="fill:var(--text-3);font-size:10.5px">avg ${esc(fmt(opts.avg))}</text>`;
  }
  if (opts.goal != null) svg += goalLine(opts.goal, y(opts.goal), width, opts.digits, fmt);
  const dayIndex = new Map(points.map((p, i) => [p.day, i]));
  svg += markerLines(opts.markers, (m) => {
    const i = dayIndex.get(String(m.t).slice(0, 10));
    return i == null ? null : PAD.left + slot * i + slot / 2;
  }, ih);
  svg += `<rect x="${PAD.left}" y="${PAD.top}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
  const tip = tipBox(container);
  const svgEl = container.querySelector("svg");
  svgEl.addEventListener("mousemove", (ev) => {
    const rect = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width;
    const i = Math.max(0, Math.min(points.length - 1, Math.floor((px - PAD.left) / slot)));
    const p = points[i];
    const parts = opts.stack && p.parts ? opts.stack.filter((st) => p.parts[st.key] > 0)
      .map((st) => `<div><span class="tip-swatch" style="background:${st.color}"></span>${esc(st.name)} ${esc(fmt(p.parts[st.key]))}</div>`).join("") : "";
    tip.innerHTML = `<div style="opacity:.7">${esc(p.tipLabel || fmtDate(p.day))}${p.partial ? " · so far" : ""}</div><b>${esc(fmt(p.value))}</b>${parts}${p.source ? `<div style="opacity:.7">${esc(p.source)}</div>` : ""}${markerNotes(opts.markers, p.day)}`;
    tip.hidden = false;
    tip.style.left = `${((PAD.left + slot * i + slot / 2) / width) * rect.width}px`;
    tip.style.top = `${(y(p.value) / height) * rect.height}px`;
  });
  svgEl.addEventListener("mouseleave", () => { tip.hidden = true; });
}

/** "intro" the first time a chart is drawn in its container (it draws itself in), "" when it's redrawn (a resize). */
function intro(container) {
  if (container.dataset.drawn) return "";
  container.dataset.drawn = "1";
  return visible() ? "intro" : "";
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

/** Thin vertical lines for events (a medication started, a diagnosis), labelled on hover. */
function markerLines(markers, xOf, ih) {
  if (!markers?.length) return "";
  return markers.map((m) => {
    const mx = xOf(m);
    if (mx == null) return "";
    return `<g class="marker"><title>${esc(`${fmtDate(m.t)} · ${m.label}`)}</title>
      <line x1="${mx}" x2="${mx}" y1="${PAD.top}" y2="${PAD.top + ih}"/><circle cx="${mx}" cy="${PAD.top}" r="3.5"/></g>`;
  }).join("");
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
  const lo = Math.floor((Math.min(...rows.map((r) => r.from)) - 30) / 60) * 60;
  const hi = Math.ceil((Math.max(...rows.map((r) => r.to)) + 30) / 60) * 60;
  const iw = width - PAD.left - PAD.right, ih = height - PAD.top - PAD.bottom;
  const slot = iw / rows.length, bw = Math.min(18, Math.max(3, slot - 3));
  const y = (m) => PAD.top + ((m - lo) / (hi - lo)) * ih;     // earlier times at the top
  const step = (hi - lo) / 60 > 8 ? 120 : 60;
  let svg = `<svg viewBox="0 0 ${width} ${height}" height="${height}" role="img" aria-label="${esc(opts.label || "Bedtime and wake time")}"><g class="axis">`;
  for (let m = lo; m <= hi; m += step) {
    svg += `<line class="grid-line" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(m)}" y2="${y(m)}"/>`;
    svg += `<text x="${PAD.left - 8}" y="${y(m) + 3.5}" text-anchor="end">${esc(fmt(m))}</text>`;
  }
  const labelEvery = Math.ceil(rows.length / Math.max(2, Math.floor(iw / 70)));
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
  svg += `</svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
  const tip = tipBox(container);
  const svgEl = container.querySelector("svg");
  svgEl.addEventListener("mousemove", (ev) => {
    const rect = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width;
    const i = Math.max(0, Math.min(rows.length - 1, Math.floor((px - PAD.left) / slot)));
    const r = rows[i];
    tip.innerHTML = `<div style="opacity:.7">${esc(fmtDate(r.day))}</div><b>${esc(fmt(r.from))} – ${esc(fmt(r.to))}</b>`;
    tip.hidden = false;
    tip.style.left = `${((PAD.left + slot * i + slot / 2) / width) * rect.width}px`;
    tip.style.top = `${(y(r.from) / height) * rect.height}px`;
  });
  svgEl.addEventListener("mouseleave", () => { tip.hidden = true; });
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
  const t0 = Math.min(...segs.map((s) => s.a)), t1 = Math.max(...segs.map((s) => s.b));
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
 * Two measures against each other, one dot per day, with a least-squares line.
 * points: [{ x, y, day }]; opts: { xLabel, yLabel, fx(v), fy(v) }
 */
export function scatterChart(container, points, opts = {}) {
  const width = Math.max(container.clientWidth || 600, 280);
  const height = opts.height || 260;
  if (points.length < 3) { container.innerHTML = `<div class="empty small">Not enough days with both measures.</div>`; return; }
  const pad = { ...PAD, bottom: 40, left: 52 };
  const fx = opts.fx || ((v) => fmtNum(v)), fy = opts.fy || ((v) => fmtNum(v));
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const xt = niceTicks(Math.min(...xs), Math.max(...xs)), yt = niceTicks(Math.min(...ys), Math.max(...ys));
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
  const n = points.length, mx = xs.reduce((a, b) => a + b) / n, my = ys.reduce((a, b) => a + b) / n;
  const sxx = xs.reduce((a, v) => a + (v - mx) ** 2, 0);
  if (sxx > 0) {
    const slope = points.reduce((a, p) => a + (p.x - mx) * (p.y - my), 0) / sxx;
    const a = xt.lo, b = xt.hi;
    svg += `<line class="fit" x1="${x(a)}" y1="${y(my + slope * (a - mx))}" x2="${x(b)}" y2="${y(my + slope * (b - mx))}"/>`;
  }
  points.forEach((p, i) => { svg += `<circle class="pt dot" cx="${x(p.x)}" cy="${y(p.y)}" r="4" data-i="${i}"/>`; });
  svg += `</svg>`;
  container.classList.add("chart");
  container.innerHTML = svg;
  const tip = tipBox(container);
  const svgEl = container.querySelector("svg");
  svgEl.addEventListener("mousemove", (ev) => {
    const rect = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width, py = ((ev.clientY - rect.top) / rect.height) * height;
    const near = points.reduce((best, p) => { const d = (x(p.x) - px) ** 2 + (y(p.y) - py) ** 2; return d < best.d ? { p, d } : best; }, { d: Infinity });
    if (!near.p || near.d > 400) { tip.hidden = true; return; }
    const p = near.p;
    tip.innerHTML = `<div style="opacity:.7">${esc(fmtDate(p.day))}</div>${esc(opts.xLabel)} <b>${esc(fx(p.x))}</b><br>${esc(opts.yLabel)} <b>${esc(fy(p.y))}</b>`;
    tip.hidden = false;
    tip.style.left = `${(x(p.x) / width) * rect.width}px`;
    tip.style.top = `${(y(p.y) / height) * rect.height}px`;
  });
  svgEl.addEventListener("mouseleave", () => { tip.hidden = true; });
}

/** Tiny inline sparkline (returns SVG markup). */
export function sparkline(values) {
  const vals = values.filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return "";
  const w = 160, h = 34, pad = 5;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const pts = vals.map((v, i) => [pad + (i / (vals.length - 1)) * (w - pad * 2), h - pad - ((v - min) / span) * (h - pad * 2)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  return `<svg class="spark ${visible() ? "intro" : ""}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" vector-effect="non-scaling-stroke"/><circle cx="${last[0]}" cy="${last[1]}" r="3"/></svg>`;
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
