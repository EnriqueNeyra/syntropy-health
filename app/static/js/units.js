// Display units. Everything is stored in metric; this converts for display when US units are chosen
// (Settings → General → Units), and picks friendlier metric units too (km rather than thousands of metres).
// Lab results keep the units the lab reported.

import { fmtNum } from "./ui.js";

let preference = null;   // "metric", "us", or null to follow the browser's region

// Regions that use US customary units day to day.
const US_REGIONS = new Set(["US", "LR", "MM", "PR", "GU", "VI", "AS", "MP", "UM"]);

export function setUnitPreference(value) { preference = value === "metric" || value === "us" ? value : null; }
export function unitPreference() { return preference; }

/** "metric" or "us" for the browser's region. */
export function regionSystem() {
  try {
    const region = new Intl.Locale(navigator.language || "en-US").maximize().region;
    return US_REGIONS.has(region) ? "us" : "metric";
  } catch { return "metric"; }
}

/** "metric" or "us": the chosen system, or the browser region's. */
export function unitSystem() { return preference || regionSystem(); }

// Wearable metrics whose value is a change in temperature rather than a temperature.
const DELTA_TEMPERATURE = new Set(["body_temperature_deviation"]);
// Distances measured in metres that read best in km / mi (daily totals), versus ones that stay short.
const LONG_DISTANCE = new Set(["distance_walking_running", "distance_cycling", "distance_downhill_snow_sports", "distance_wheelchair"]);

// UCUM codes (from health records) to the plain symbols used below.
const UCUM = { "[lb_av]": "lb", "[in_i]": "in", "[in_us]": "in", "[ft_i]": "ft", Cel: "°C", "[degF]": "°F", "kg/m2": "kg/m²", "/min": "/min" };

/**
 * A value in the units to show: { value, unit, digits }. `metric` is the wearable metric id when known (some
 * conversions depend on what's measured). Units that don't need converting come back as they are.
 */
export function convert(value, unit, metric = "") {
  if (value === null || value === undefined || isNaN(value)) return { value, unit, digits: undefined };
  const u = UCUM[unit] || unit;
  const us = unitSystem() === "us";
  const v = Number(value);
  switch (u) {
    case "kg": return us ? { value: v * 2.2046226, unit: "lb", digits: 1 } : { value: v, unit: "kg", digits: 1 };
    case "lb": return us ? { value: v, unit: "lb", digits: 1 } : { value: v / 2.2046226, unit: "kg", digits: 1 };
    case "°C":
      if (DELTA_TEMPERATURE.has(metric)) return us ? { value: v * 1.8, unit: "°F", digits: 1 } : { value: v, unit: "°C", digits: 2 };
      return us ? { value: v * 1.8 + 32, unit: "°F", digits: 1 } : { value: v, unit: "°C", digits: 1 };
    case "°F": return us ? { value: v, unit: "°F", digits: 1 } : { value: (v - 32) / 1.8, unit: "°C", digits: 1 };
    case "cm":
      if (metric === "height") return us ? { value: v / 2.54, unit: "in", digits: 1, height: true } : { value: v, unit: "cm", digits: 0 };
      return us ? { value: v / 2.54, unit: "in", digits: 1 } : { value: v, unit: "cm", digits: 1 };
    case "in": return us ? { value: v, unit: "in", digits: 1 } : { value: v * 2.54, unit: "cm", digits: 1 };
    case "m":
      if (LONG_DISTANCE.has(metric)) return us ? { value: v / 1609.344, unit: "mi", digits: 2 } : { value: v / 1000, unit: "km", digits: 2 };
      if (metric === "distance_swimming") return us ? { value: v * 1.0936133, unit: "yd", digits: 0 } : { value: v, unit: "m", digits: 0 };
      return us ? { value: v * 3.2808399, unit: "ft", digits: metric === "running_stride_length" ? 1 : 0 } : { value: v, unit: "m", digits: metric === "running_stride_length" ? 2 : 0 };
    case "km": return us ? { value: v / 1.609344, unit: "mi", digits: 2 } : { value: v, unit: "km", digits: 2 };
    case "m/s":
      if (metric.startsWith("stair_")) return us ? { value: v * 3.2808399, unit: "ft/s", digits: 1 } : { value: v, unit: "m/s", digits: 2 };
      return us ? { value: v * 2.2369363, unit: "mph", digits: 1 } : { value: v * 3.6, unit: "km/h", digits: 1 };
    case "mL": return us ? { value: v / 29.5735296, unit: "fl oz", digits: 0 } : v >= 1000 ? { value: v / 1000, unit: "L", digits: 2 } : { value: v, unit: "mL", digits: 0 };
    default: return { value: v, unit, digits: undefined };
  }
}

/** Just the unit a metric is shown in, for axis labels and headings. */
export function displayUnit(unit, metric = "") { return convert(1, unit, metric).unit; }

/** A converted value as text, e.g. "154.3 lb" or 5′ 9″ for heights in US units. */
export function fmtMeasure(value, unit, metric = "", { withUnit = true, digits } = {}) {
  if (value === null || value === undefined || value === "" || isNaN(value)) return "—";
  const c = convert(value, unit, metric);
  if (c.height && unitSystem() === "us") {
    const total = Math.round(c.value);
    return `${Math.floor(total / 12)}′ ${total % 12}″`;
  }
  const n = fmtNum(c.value, digits ?? c.digits);
  return withUnit && c.unit ? `${n} ${c.unit}` : n;
}

/** Converts a list of chart points ({ v } or { value }) in place of copies. */
export function convertPoints(points, unit, metric = "", key = "v") {
  return points.map((p) => (p[key] == null ? p : { ...p, [key]: convert(p[key], unit, metric).value }));
}

/**
 * A health-record value (a vital sign like weight or temperature) in the chosen units. Records keep the number and
 * unit they were recorded with; anything that isn't a body measurement is returned as recorded.
 */
export function recordValue(rec) {
  if (rec?.value_num == null || !rec.unit) return rec?.value_text || "—";
  const u = UCUM[rec.unit] || rec.unit;
  if (!["kg", "lb", "°C", "°F", "cm", "in"].includes(u)) return rec.value_text || `${fmtNum(rec.value_num)} ${rec.unit}`;
  return fmtMeasure(rec.value_num, u, /height|length/i.test(rec.title || "") ? "height" : "");
}
