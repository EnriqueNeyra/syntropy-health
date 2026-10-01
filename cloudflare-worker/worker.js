/**
 * Syntropy Health — OAuth relay & wearable token broker (Cloudflare Worker)
 *
 * Why this exists: OAuth providers require a fixed, pre-registered HTTPS redirect URI,
 * but every Syntropy instance runs at its own private address (localhost, a LAN IP,
 * a Tailscale name). The worker is that fixed URI and forwards the browser back to the
 * instance named in the OAuth `state`.
 *
 * Routes
 *   GET  /callback   OAuth redirect target.
 *                    - SMART on FHIR (EHRs): forwards `code` + `state` untouched. The code is
 *                      useless without the PKCE verifier, which never leaves the instance.
 *                    - Oura / WHOOP / Google Health: these require a client secret, so the worker performs the
 *                      code exchange here and hands the tokens to the instance in the URL
 *                      *fragment* (never sent to any server, never logged), where the instance's
 *                      own page picks them up. Nothing is stored.
 *   POST /refresh    { provider, refresh_token } -> fresh tokens (same secret-holding role).
 *   GET  /health     liveness probe, with the commit this deploy was built from (set by relay.yml), so anyone
 *                    can check the running relay against the code in the repository.
 *
 * Callback and refresh requests are rate-limited per client IP (the RATE_LIMITER binding in
 * wrangler.toml), so nobody can flood Oura or WHOOP through the Syntropy client IDs.
 *
 * Destination safety: the instance address comes from `state.d` and must be a private /
 * loopback / Tailscale / mDNS host, so the relay cannot be used as an open redirect to
 * the public internet.
 */

const PROVIDERS = {
  whoop: { tokenUrl: "https://api.prod.whoop.com/oauth/oauth2/token", idVar: "WHOOP_CLIENT_ID", secretVar: "WHOOP_CLIENT_SECRET", refreshScope: "offline" },
  oura: { tokenUrl: "https://api.ouraring.com/oauth/token", idVar: "OURA_CLIENT_ID", secretVar: "OURA_CLIENT_SECRET" },
  // Configured once Syntropy's Google Cloud app passes Google's restricted-scope review; Google omits a new
  // refresh_token on refresh, and the instance keeps the one it has.
  google: { tokenUrl: "https://oauth2.googleapis.com/token", idVar: "GOOGLE_HEALTH_CLIENT_ID", secretVar: "GOOGLE_HEALTH_CLIENT_SECRET" },
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
      if (path === "/refresh" && request.method === "POST") return await handleRefresh(request, env);
      if (path === "/") return Response.redirect(SITE, 302);
      return new Response("Not found", { status: 404 });
    } catch (err) {
      return page("Something went wrong", String(err && err.message || err), DEFAULT_TARGET, 500);
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
    const m = /(?:^|;)dest=([^;]+)/.exec(state);
    return m ? { d: decodeURIComponent(m[1]) } : {};
  }
}

export function isPrivateHost(host) {
  host = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (host === "localhost" || host === "::1" || host.endsWith(".localhost")) return true;
  if (host.endsWith(".local") || host.endsWith(".ts.net") || host.endsWith(".home.arpa") || host.endsWith(".internal") || host.endsWith(".lan")) return true;
  const ip = host.split(".").map(Number);
  if (ip.length === 4 && ip.every((n) => Number.isInteger(n) && n >= 0 && n <= 255)) {
    const [a, b] = ip;
    return a === 10 || a === 127 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 100 && b >= 64 && b <= 127);
  }
  return /^f[cd][0-9a-f]{2}:/.test(host); // IPv6 unique-local
}

export function resolveTarget(state) {
  const dest = decodeState(state).d;
  if (!dest) return DEFAULT_TARGET;
  try {
    const u = new URL(dest);
    if ((u.protocol === "http:" || u.protocol === "https:") && isPrivateHost(u.hostname)) return u.origin;
  } catch (_) { /* fall through */ }
  return DEFAULT_TARGET;
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async function handleCallback(url, env) {
  const params = url.searchParams;
  const state = params.get("state") || "";
  const target = resolveTarget(state);
  const back = new URL("/callback", target);

  if (params.get("error")) {
    for (const k of ["error", "error_description", "state"]) if (params.get(k)) back.searchParams.set(k, params.get(k));
    return redirect(back.toString());
  }
  const code = params.get("code");
  if (!code) return page("Missing authorization code", "The provider did not return an authorization code.", target, 400);

  const provider = (decodeState(state).p || "").toLowerCase();
  const cfg = PROVIDERS[provider];
  if (!cfg) {
    // SMART on FHIR: forward the one-time code; the instance completes PKCE itself.
    back.searchParams.set("code", code);
    back.searchParams.set("state", state);
    return redirect(back.toString());
  }

  const secret = env[cfg.secretVar];
  if (!secret) return page("Relay not configured", `${provider} is not configured on this relay.`, target, 500);
  const redirectUri = `${url.origin}/callback`;
  const tokens = await tokenRequest(cfg.tokenUrl, {
    grant_type: "authorization_code", code, redirect_uri: redirectUri,
    client_id: env[cfg.idVar], client_secret: secret,
  });
  if (tokens.error) return page(`${provider} sign-in failed`, tokens.error, target, 502);

  const fragment = new URLSearchParams({
    relay_provider: provider, state,
    access_token: tokens.access_token || "",
    refresh_token: tokens.refresh_token || "",
    expires_in: String(tokens.expires_in || ""),
    scope: tokens.scope || "",
  });
  back.hash = fragment.toString();
  return redirect(back.toString());
}

async function handleRefresh(request, env) {
  let body;
  try { body = await request.json(); } catch (_) { return json({ error: "invalid_request" }, 400); }
  const cfg = PROVIDERS[String(body.provider || "").toLowerCase()];
  if (!cfg || !body.refresh_token || typeof body.refresh_token !== "string" || body.refresh_token.length > 4096) {
    return json({ error: "invalid_request" }, 400);
  }
  const secret = env[cfg.secretVar];
  if (!secret) return json({ error: "not_configured" }, 500);
  const form = { grant_type: "refresh_token", refresh_token: body.refresh_token, client_id: env[cfg.idVar], client_secret: secret };
  if (cfg.refreshScope) form.scope = cfg.refreshScope;
  const tokens = await tokenRequest(cfg.tokenUrl, form);
  if (tokens.error) return json({ error: "refresh_failed", error_description: tokens.error }, 400);
  return json({ access_token: tokens.access_token, refresh_token: tokens.refresh_token, expires_in: tokens.expires_in, scope: tokens.scope });
}

async function tokenRequest(tokenUrl, form) {
  const resp = await fetch(tokenUrl, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams(form),
  });
  let data = {};
  try { data = await resp.json(); } catch (_) { /* non-JSON error body */ }
  if (!resp.ok || !data.access_token) {
    return { error: data.error_description || data.error || `HTTP ${resp.status}` };
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

function redirect(location) {
  return new Response(null, { status: 302, headers: { Location: location, ...SECURITY_HEADERS } });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...SECURITY_HEADERS } });
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function page(title, message, target, status) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)} — Syntropy Health</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0e;color:#f4f4f5;font:15px -apple-system,system-ui,sans-serif;padding:20px}
.c{max-width:420px;background:#16161a;border:1px solid #27272a;border-radius:14px;padding:28px;text-align:center}
p{color:#a1a1aa;line-height:1.5}a{display:inline-block;margin-top:16px;color:#0b0b0e;background:#f4f4f5;padding:9px 14px;border-radius:9px;text-decoration:none;font-weight:600}</style></head>
<body><div class="c"><h1 style="font-size:18px">${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p>
<a href="${escapeHtml(target)}/#/sources">Back to Syntropy Health</a></div></body></html>`;
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", ...SECURITY_HEADERS },
  });
}
