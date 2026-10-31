/**
 * Syntropy Health — OAuth relay & wearable token broker (Cloudflare Worker)
 *
 * Why this exists: OAuth providers require a fixed, pre-registered HTTPS redirect URI,
 * but every Syntropy instance runs at its own address (localhost, a LAN IP, a Tailscale
 * name, a domain of its own). The worker is that fixed URI and forwards the browser back to
 * the instance named in the OAuth `state`.
 *
 * Routes
 *   GET  /callback   OAuth redirect target.
 *                    - SMART on FHIR (EHRs): forwards `code` + `state` untouched.
 *                    - Oura / WHOOP: these require a client secret, so the worker performs the
 *                      code exchange here and hands the tokens to the instance in the URL
 *                      *fragment* (never sent to any server, never logged), where the instance's
 *                      own page picks them up. Nothing is stored.
 *   POST /callback   The same, once the person has confirmed an address the worker asked them about.
 *   POST /refresh    { provider, refresh_token } -> fresh tokens (same secret-holding role).
 *   GET  /health     liveness probe, with the commit this deploy was built from (set by relay.yml), so anyone
 *                    can check the running relay against the code in the repository.
 *
 * Callback and refresh requests are rate-limited per client IP (the RATE_LIMITER binding in
 * wrangler.toml), so nobody can flood Oura or WHOOP through the Syntropy client IDs.
 *
 * Release builds only: Syntropy Labs' release builds carry a relay key (app/core/config.py, relay_key), which they put
 * in the OAuth state (`k`) and send on /refresh (X-Syntropy-Key). The worker knows only the SHA-256 of each key it
 * accepts (KEY_HASHES in wrangler.toml). With REQUIRE_KEY "true" it turns away sign-ins and refreshes without one, so a
 * fork or other build from the source code can't use Syntropy's Oura and WHOOP apps. Until then every request is
 * served, while releases from before the key update.
 *
 * Destination safety: the instance address comes from `state.d`, and `state` is written by whoever made the sign-in
 * link. Client IDs are public, so someone else can start a sign-in that names their own server, with their own PKCE
 * verifier, and send the link to a victim. Only this computer returns with no stop. Anything else gets a page naming the
 * address, and nothing is exchanged or forwarded until the person clicks to say it's their own: LAN, Tailscale and .local
 * addresses, which anyone on the same Wi-Fi or tailnet can hold, as well as Tailscale Funnel hosts (those are public) and
 * domains. The click is a POST from that page, so no link can stand in for it. The callback page on the website follows
 * the same rules; test/relay-rules.json holds the cases both must pass.
 */

const PROVIDERS = {
  whoop: { name: "WHOOP", tokenUrl: "https://api.prod.whoop.com/oauth/oauth2/token", idVar: "WHOOP_CLIENT_ID", secretVar: "WHOOP_CLIENT_SECRET", refreshScope: "offline" },
  oura: { name: "Oura", tokenUrl: "https://api.ouraring.com/oauth/token", idVar: "OURA_CLIENT_ID", secretVar: "OURA_CLIENT_SECRET" },
};
const DEFAULT_TARGET = "http://localhost:8000";
const SITE = "https://health.syntropylabs.io";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    try {
      if (path === "/health") return json({ status: "ok", service: "syntropy-auth-relay", commit: env.COMMIT || null });
      if ((path === "/callback" || path === "/refresh") && !(await allowed(request, env))) {
        return json({ error: "rate_limited" }, 429);
      }
      if (path === "/callback" && request.method === "GET") return await handleCallback(url, env);
      if (path === "/callback" && request.method === "POST") return await handleConfirmed(request, url, env);
      if (path === "/refresh" && request.method === "POST") return await handleRefresh(request, env);
      if (path === "/") return Response.redirect(SITE, 302);
      return new Response("Not found", { status: 404 });
    } catch (_) {
      if (path === "/refresh") return json({ error: "relay_error" }, 500);
      return page("Something went wrong", "The sign-in relay hit a problem, and nothing was saved. Start the connection again from Syntropy Health.", null, 500);
    }
  },
};

// Per-IP limit. Without the binding (local tests, or a deployment that dropped it) requests pass.
async function allowed(request, env) {
  if (!env.RATE_LIMITER) return true;
  const key = request.headers.get("cf-connecting-ip") || "unknown";
  const { success } = await env.RATE_LIMITER.limit({ key });
  return success;
}

// Whether to serve a request carrying this relay key: one whose SHA-256 is in KEY_HASHES, or any while REQUIRE_KEY isn't
// "true".
export async function releaseBuild(key, env) {
  if (env.REQUIRE_KEY !== "true") return true;
  if (typeof key !== "string" || !key || key.length > 256) return false;
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key)));
  const hex = Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
  return String(env.KEY_HASHES || "").split(",").map((h) => h.trim().toLowerCase()).includes(hex);
}

const NOT_RELEASE = "Syntropy Labs' sign-in relay serves the Syntropy Health apps from health.syntropylabs.io. Update to the "
  + "latest version. A copy built from the source code needs its own Oura or WHOOP app (Settings → Wearables), "
  + "registered with this instance's own /callback.";

// ---------------------------------------------------------------------------
// State & destination
// ---------------------------------------------------------------------------

function decodeState(state) {
  if (!state) return {};
  try {
    let b64 = state.replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    const parsed = JSON.parse(atob(b64));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_) {
    // Legacy format: "<nonce>;dest=<urlencoded origin>"
    try {
      const m = /(?:^|;)dest=([^;]+)/.exec(state);
      return m ? { d: decodeURIComponent(m[1]) } : {};
    } catch (_) {
      return {};
    }
  }
}

// Whether a sign-in goes straight back to this host, with no question: only this computer.
export function returnsWithoutStop(host) {
  host = host.toLowerCase().replace(/^\[|\]$/g, "");
  return host === "localhost" || host === "::1" || host.endsWith(".localhost") || /^127(\.\d{1,3}){3}$/.test(host);
}

// Where this state's sign-in goes back to, and on what terms: class "direct" or "confirm" with its origin, or "refused"
// when it names something that can't be returned to, with `shown` for the page that says so.
export function destination(state) {
  const dest = decodeState(state).d;
  if (dest === undefined || dest === null || dest === "") return { class: "direct", origin: DEFAULT_TARGET };
  try {
    const u = typeof dest === "string" ? new URL(dest) : null;
    if (u && (u.protocol === "http:" || u.protocol === "https:")) {
      return { class: returnsWithoutStop(u.hostname) ? "direct" : "confirm", origin: u.origin };
    }
  } catch (_) { /* refused below */ }
  return { class: "refused", shown: (typeof dest === "string" ? dest : JSON.stringify(dest)).slice(0, 200) };
}

function providerFor(name) {
  const key = typeof name === "string" ? name.toLowerCase() : "";
  return Object.hasOwn(PROVIDERS, key) ? PROVIDERS[key] : null;
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async function handleCallback(url, env) {
  const params = url.searchParams;
  const state = params.get("state") || "";
  const target = destination(state);
  if (target.class === "refused") return refused(target);
  if (target.class === "direct") return finish(params, target.origin, url.origin, env, 302);

  if (!params.get("error")) {
    if (!params.get("code")) return missingCode(null);
    if (!(await releaseBuild(decodeState(state).k, env))) return page("Update Syntropy Health to connect", NOT_RELEASE, null, 403);
  }
  return confirmation(params, target.origin);
}

// The person's answer to confirmation(). A sign-in only ever arrives here from that page: a post from anywhere else
// could be a provider told to answer by form, which would skip the question.
async function handleConfirmed(request, url, env) {
  const site = request.headers.get("sec-fetch-site");
  const from = request.headers.get("origin");
  if (site ? site !== "same-origin" : from && from !== "null" && from !== url.origin) {
    return page("This sign-in can't continue", "That request came from another website, so nothing was sent. Start the connection again from Syntropy Health.", null, 403);
  }
  if (!(request.headers.get("content-type") || "").startsWith("application/x-www-form-urlencoded")) {
    return page("This sign-in can't continue", "The request wasn't one this page sends, so nothing was sent. Start the connection again from Syntropy Health.", null, 400);
  }
  const params = new URLSearchParams(await request.text());
  if (params.get("stop")) return page("Sign-in stopped", "Nothing was sent. You can close this page.", null, 200);
  const target = destination(params.get("state") || "");
  if (target.class === "refused") return refused(target);
  return finish(params, target.origin, url.origin, env, 303);
}

// Sends the sign-in's result to an address that needed no question or has had its answer.
async function finish(params, targetOrigin, relayOrigin, env, status) {
  const state = params.get("state") || "";
  const back = new URL("/callback", targetOrigin);

  if (params.get("error")) {
    for (const k of ["error", "error_description", "state"]) if (params.get(k)) back.searchParams.set(k, params.get(k));
    return redirect(back.toString(), status);
  }
  const code = params.get("code");
  if (!code) return missingCode(targetOrigin);
  const decoded = decodeState(state);
  if (!(await releaseBuild(decoded.k, env))) return page("Update Syntropy Health to connect", NOT_RELEASE, targetOrigin, 403);

  const cfg = providerFor(decoded.p);
  if (!cfg) {
    // SMART on FHIR: forward the one-time code; the instance completes PKCE itself.
    back.searchParams.set("code", code);
    back.searchParams.set("state", state);
    return redirect(back.toString(), status);
  }

  const secret = env[cfg.secretVar];
  if (!secret) return page("Relay not configured", `${cfg.name} is not configured on this relay.`, targetOrigin, 500);
  const redirectUri = `${relayOrigin}/callback`;
  const tokens = await tokenRequest(cfg.tokenUrl, {
    grant_type: "authorization_code", code, redirect_uri: redirectUri,
    client_id: env[cfg.idVar], client_secret: secret,
  });
  if (tokens.unavailable) return page(`Couldn't reach ${cfg.name}`, `${cfg.name} didn't answer. Start the connection again from Syntropy Health.`, targetOrigin, 502);
  if (tokens.error) return page(`${cfg.name} sign-in failed`, tokens.error, targetOrigin, 502);

  // A field the provider left out stays out: the instance would have to parse an empty one.
  const fragment = new URLSearchParams({ relay_provider: decoded.p.toLowerCase(), state });
  for (const k of ["access_token", "refresh_token", "expires_in", "scope"]) {
    if (tokens[k] !== undefined && tokens[k] !== null && tokens[k] !== "") fragment.set(k, String(tokens[k]));
  }
  back.hash = fragment.toString();
  return redirect(back.toString(), status);
}

async function handleRefresh(request, env) {
  let body;
  try { body = await request.json(); } catch (_) { return json({ error: "invalid_request" }, 400); }
  const cfg = body && typeof body === "object" ? providerFor(body.provider) : null;
  if (!cfg || !body.refresh_token || typeof body.refresh_token !== "string" || body.refresh_token.length > 4096) {
    return json({ error: "invalid_request" }, 400);
  }
  if (!(await releaseBuild(request.headers.get("x-syntropy-key"), env))) {
    return json({ error: "release_build_required", error_description: NOT_RELEASE }, 403);
  }
  const secret = env[cfg.secretVar];
  if (!secret) return json({ error: "not_configured" }, 500);
  const form = { grant_type: "refresh_token", refresh_token: body.refresh_token, client_id: env[cfg.idVar], client_secret: secret };
  if (cfg.refreshScope) form.scope = cfg.refreshScope;
  const tokens = await tokenRequest(cfg.tokenUrl, form);
  if (tokens.unavailable) return json({ error: "provider_unavailable" }, 502);
  if (tokens.error) return json({ error: "refresh_failed", error_description: tokens.error }, 400);
  return json({ access_token: tokens.access_token, refresh_token: tokens.refresh_token, expires_in: tokens.expires_in, scope: tokens.scope });
}

async function tokenRequest(tokenUrl, form) {
  let resp;
  try {
    resp = await fetch(tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: new URLSearchParams(form),
    });
  } catch (_) {
    return { unavailable: true };
  }
  if (resp.status >= 500 || resp.status === 429) return { unavailable: true };
  let data = {};
  try { data = await resp.json(); } catch (_) { /* non-JSON error body */ }
  if (!resp.ok || !data || !data.access_token) {
    return { error: (data && (data.error_description || data.error)) || `HTTP ${resp.status}` };
  }
  return data;
}

// ---------------------------------------------------------------------------
// Responses
// ---------------------------------------------------------------------------

const SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

function redirect(location, status = 302) {
  return new Response(null, { status, headers: { Location: location, ...SECURITY_HEADERS } });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...SECURITY_HEADERS } });
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const PAGE_STYLE = `body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0e;color:#f4f4f5;font:15px -apple-system,system-ui,sans-serif;padding:20px;box-sizing:border-box}
.c{max-width:420px;width:100%;box-sizing:border-box;background:#16161a;border:1px solid #27272a;border-radius:14px;padding:28px;text-align:center}
h1{font-size:18px}p{color:#a1a1aa;line-height:1.5}form{margin:0}
.host{color:#f4f4f5;background:#27272a;border-radius:8px;padding:10px 12px;font:14px ui-monospace,Menlo,Consolas,monospace;overflow-wrap:anywhere}
a,.main,.quiet{display:inline-block;margin-top:16px;padding:9px 14px;border-radius:9px;font:600 15px -apple-system,system-ui,sans-serif;text-decoration:none;cursor:pointer;overflow-wrap:anywhere}
a,.main{color:#0b0b0e;background:#f4f4f5;border:1px solid #f4f4f5}.quiet{margin-top:12px;color:#a1a1aa;background:none;border:1px solid #3f3f46;font-weight:400}`;

function htmlPage(title, content, status, formAction = "'none'") {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)} — Syntropy Health</title>
<style>${PAGE_STYLE}</style></head>
<body><div class="c"><h1>${escapeHtml(title)}</h1>${content}</div></body></html>`;
  return new Response(html, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": `default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action ${formAction}; frame-ancestors 'none'`,
      "X-Frame-Options": "DENY",
      ...SECURITY_HEADERS,
    },
  });
}

// `target`, when there is one, is an address to link back to: only one that needs no question or has had its answer.
function page(title, message, target, status) {
  return htmlPage(title, `<p>${escapeHtml(message)}</p>${target ? `<a href="${escapeHtml(target)}/#/sources">Back to Syntropy Health</a>` : ""}`, status);
}

function missingCode(target) {
  return page("Missing authorization code", "The provider did not return an authorization code.", target, 400);
}

function refused(target) {
  return page("This sign-in can't go back to Syntropy Health",
    `It names "${target.shown}" as the address to return to, which isn't a web address. Nothing was sent. Open Syntropy Health at its usual address and connect again.`, null, 400);
}

// Asks before anything leaves for an address that isn't this computer. The answer is a
// form: a link could be followed by a mail scanner, and on Android a tapped link to another site leaves the sign-in's
// web view, where a form and the redirect after it stay.
function confirmation(params, origin) {
  const failed = Boolean(params.get("error"));
  const fields = failed ? ["error", "error_description", "state"] : ["code", "state"];
  const hidden = fields.filter((k) => params.get(k)).map((k) => `<input type="hidden" name="${k}" value="${escapeHtml(params.get(k))}">`).join("");
  const cfg = providerFor(decodeState(params.get("state") || "").p);
  const host = escapeHtml(origin.replace(/^https?:\/\//, ""));
  const address = `<p class="host">${escapeHtml(origin)}</p>`;
  const warning = "<p>Continue only if this is your own Syntropy Health server. If you did not start this from your own Syntropy Health, do not continue.</p>";
  const stop = (label) => `<form method="post" action="/callback"><input type="hidden" name="stop" value="1"><button class="main">${label}</button></form>`;
  const go = (label) => `<form method="post" action="/callback">${hidden}<button class="quiet">${label} ${host}</button></form>`;
  // A form's redirect is held to form-action too, in Chrome and Safari. A policy can't name an IPv6 address or a name
  // with an underscore, so those get the plain schemes.
  const allowed = /^https?:\/\/[a-z0-9.-]+(:\d+)?$/.test(origin) ? origin : "http: https:";
  const content = failed
    ? `<p>No access was given. This is the address the sign-in came from:</p>${address}${warning}${stop("Stop here")}${go("Go back to")}`
    : `<p>Going on gives the computer at this address access to ${cfg ? `your ${cfg.name} data` : "your health records"}:</p>${address}${warning}${stop("Don't continue")}${go("Continue to")}`;
  return htmlPage(failed ? "The sign-in didn't finish" : "Check this address before you continue", content, 200, `'self' ${allowed}`);
}
