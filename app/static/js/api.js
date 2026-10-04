// Thin fetch wrapper: JSON in/out, CSRF header, uniform errors.

export class ApiError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body; }
}

/** What to say when the request never reached the server (it's off, asleep or on another network, or we're offline). */
export function unreachableMessage() {
  return navigator.onLine === false
    ? "You're offline. Reconnect to your network, then try again."
    : "Can't reach Syntropy Health. Check that the computer it runs on is on and connected, then try again.";
}

const listeners = { unauthorized: [], changed: [] };
export function onUnauthorized(fn) { listeners.unauthorized.push(fn); }
/** Called after any change is saved (not bookkeeping), e.g. so the iPhone app can refresh its other screens. */
export function onDataChanged(fn) { listeners.changed.push(fn); }

/** `keepCache` is for bookkeeping writes (e.g. which alerts were seen) that don't change any data a page shows. */
export async function api(path, { method = "GET", body, query, form, raw = false, keepCache = false } = {}) {
  if (method !== "GET" && !keepCache) cache.clear();   // whatever changed may appear in any cached response
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") params.set(k, v);
    const qs = params.toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }
  const headers = { "X-Requested-With": "syntropy", Accept: "application/json" };
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError(0, unreachableMessage(), null);
  }
  if (raw) return res;
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = { detail: text }; } }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) listeners.unauthorized.forEach((fn) => fn());
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => String(d.msg || "").replace(/^Value error, /, "")).join("; ");
    throw new ApiError(res.status, msg || `Request failed (${res.status})`, data);
  }
  if (method !== "GET" && !keepCache) listeners.changed.forEach((fn) => fn());
  return data;
}

// Data fetched in the last few seconds, so going back to a section you just viewed draws straight away instead of
// waiting on the network again. Any change (POST, PUT, PATCH, DELETE) empties it; pages that poll pass { fresh: true }.
// Callers get their own copy, since pages adjust what they're given (unit conversions and the like).
const CACHE_MS = 60000;
const cache = new Map();

function cacheKey(path, query) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query || {})) if (v !== undefined && v !== null && v !== "") params.set(k, v);
  return `${path}?${params}`;
}

export function get(path, query, { fresh = false } = {}) {
  const key = cacheKey(path, query);
  const hit = cache.get(key);
  if (!fresh && hit && Date.now() - hit.at < CACHE_MS) return hit.promise.then((d) => structuredClone(d));
  const promise = api(path, { query });
  cache.set(key, { at: Date.now(), promise });
  promise.catch(() => cache.delete(key));
  return promise.then((d) => structuredClone(d));
}

export function clearCache() { cache.clear(); }
export const post = (path, body, query) => api(path, { method: "POST", body: body ?? {}, query });
export const put = (path, body) => api(path, { method: "PUT", body });
export const patch = (path, body) => api(path, { method: "PATCH", body });
export const del = (path, query) => api(path, { method: "DELETE", query });
