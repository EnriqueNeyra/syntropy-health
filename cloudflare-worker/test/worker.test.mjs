// Run with: npm test (from cloudflare-worker/)
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker, { isPrivateHost, releaseBuild, resolveTarget } from "../worker.js";

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const env = { WHOOP_CLIENT_ID: "wid", WHOOP_CLIENT_SECRET: "wsecret", OURA_CLIENT_ID: "oid", OURA_CLIENT_SECRET: "osecret" };

test("private hosts only", () => {
  for (const h of ["localhost", "127.0.0.1", "192.168.1.20", "10.0.0.5", "172.20.1.1", "100.101.1.2", "nas.local", "box.tail1234.ts.net", "::1"]) assert.ok(isPrivateHost(h), h);
  for (const h of ["example.com", "8.8.8.8", "172.32.0.1", "100.200.1.1", "evil.ts.net.example.com"]) assert.ok(!isPrivateHost(h), h);
});

test("destination comes from state and is validated", () => {
  assert.equal(resolveTarget(b64({ d: "http://192.168.1.20:8000" })), "http://192.168.1.20:8000");
  assert.equal(resolveTarget(b64({ d: "https://evil.example.com" })), "http://localhost:8000");
  assert.equal(resolveTarget("abc;dest=" + encodeURIComponent("http://nas.local:8000")), "http://nas.local:8000");
  assert.equal(resolveTarget(""), "http://localhost:8000");
});

test("SMART callbacks forward code and state untouched", async () => {
  const state = b64({ p: "smart", n: "x", d: "http://192.168.1.20:8000" });
  const res = await worker.fetch(new Request(`https://relay.test/callback?code=abc&state=${state}`), env);
  assert.equal(res.status, 302);
  const loc = new URL(res.headers.get("location"));
  assert.equal(loc.origin, "http://192.168.1.20:8000");
  assert.equal(loc.searchParams.get("code"), "abc");
  assert.equal(loc.searchParams.get("state"), state);
});

test("wearable tokens are returned in the fragment, never the query", async () => {
  globalThis.fetch = async (url, init) => {
    assert.match(String(init.body), /client_secret=wsecret/);
    return new Response(JSON.stringify({ access_token: "AT", refresh_token: "RT", expires_in: 3600, scope: "offline" }), { status: 200 });
  };
  const state = b64({ p: "whoop", n: "x", d: "http://localhost:8000" });
  const res = await worker.fetch(new Request(`https://relay.test/callback?code=abc&state=${state}`), env);
  const loc = new URL(res.headers.get("location"));
  assert.equal(loc.search, "");
  const frag = new URLSearchParams(loc.hash.slice(1));
  assert.equal(frag.get("access_token"), "AT");
  assert.equal(frag.get("state"), state);
});

test("refresh broker", async () => {
  globalThis.fetch = async (url, init) => {
    assert.match(String(init.body), /grant_type=refresh_token/);
    return new Response(JSON.stringify({ access_token: "AT2", refresh_token: "RT2", expires_in: 86400 }), { status: 200 });
  };
  const res = await worker.fetch(new Request("https://relay.test/refresh", { method: "POST", body: JSON.stringify({ provider: "oura", refresh_token: "RT" }) }), env);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).access_token, "AT2");
  const bad = await worker.fetch(new Request("https://relay.test/refresh", { method: "POST", body: JSON.stringify({ provider: "nope", refresh_token: "RT" }) }), env);
  assert.equal(bad.status, 400);
});

test("provider errors are forwarded to the instance", async () => {
  const state = b64({ p: "smart", d: "http://localhost:8000" });
  const res = await worker.fetch(new Request(`https://relay.test/callback?error=access_denied&state=${state}`), env);
  const loc = new URL(res.headers.get("location"));
  assert.equal(loc.searchParams.get("error"), "access_denied");
});

test("refresh tells a provider's bad moment from a refusal", async () => {
  const refresh = () => worker.fetch(new Request("https://relay.test/refresh", { method: "POST", body: JSON.stringify({ provider: "oura", refresh_token: "RT" }) }), env);
  for (const status of [429, 500, 503]) {
    globalThis.fetch = async () => new Response("busy", { status, headers: { "Retry-After": "120" } });
    const res = await refresh();
    assert.equal(res.status, 503);
    assert.equal(res.headers.get("retry-after"), "120");
    assert.equal((await res.json()).error, "provider_unavailable");
  }
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "temporarily_unavailable" }), { status: 400 });
  assert.equal((await refresh()).status, 503);
  globalThis.fetch = async () => { throw new TypeError("network"); };
  assert.equal((await refresh()).status, 503);
  globalThis.fetch = async () => new Response(JSON.stringify({ token_type: "bearer" }), { status: 200 });
  assert.equal((await refresh()).status, 503);

  globalThis.fetch = async () => new Response(JSON.stringify({ error: "invalid_grant", error_description: "revoked" }), { status: 400 });
  const refused = await refresh();
  assert.equal(refused.status, 400);
  assert.deepEqual(await refused.json(), { error: "refresh_failed", error_description: "revoked", oauth_error: "invalid_grant" });
});

test("callback and refresh are rate-limited per client IP", async () => {
  const seen = [];
  const limited = { ...env, RATE_LIMITER: { limit: async ({ key }) => { seen.push(key); return { success: false }; } } };
  const headers = { "cf-connecting-ip": "203.0.113.9" };
  const cb = await worker.fetch(new Request("https://relay.test/callback?code=abc", { headers }), limited);
  const rf = await worker.fetch(new Request("https://relay.test/refresh", { method: "POST", headers, body: "{}" }), limited);
  assert.equal(cb.status, 429);
  assert.equal(rf.status, 429);
  assert.deepEqual(seen, ["203.0.113.9", "203.0.113.9"]);
  const health = await worker.fetch(new Request("https://relay.test/health"), limited);
  assert.equal(health.status, 200);
});

test("with REQUIRE_KEY on, only release builds' sign-ins and refreshes are served", async () => {
  const keyHash = createHash("sha256").update("release-key").digest("hex");
  const strict = { ...env, REQUIRE_KEY: "true", KEY_HASHES: `0000, ${keyHash.toUpperCase()}` };
  globalThis.fetch = async () => new Response(JSON.stringify({ access_token: "AT", refresh_token: "RT", expires_in: 3600 }), { status: 200 });
  const callback = (state) => worker.fetch(new Request(`https://relay.test/callback?code=abc&state=${b64(state)}`), strict);
  const refresh = (headers) => worker.fetch(new Request("https://relay.test/refresh",
    { method: "POST", headers, body: JSON.stringify({ provider: "oura", refresh_token: "RT" }) }), strict);

  assert.equal((await callback({ p: "whoop", d: "http://localhost:8000", k: "release-key" })).status, 302);
  assert.equal((await callback({ p: "smart", d: "http://localhost:8000", k: "release-key" })).status, 302);
  for (const state of [{ p: "whoop", d: "http://localhost:8000" }, { p: "whoop", k: "copied-wrong" }, { p: "smart", k: keyHash }]) {
    const res = await callback(state);
    assert.equal(res.status, 403);
    assert.match(await res.text(), /Update Syntropy Health/);
  }
  assert.equal((await refresh({ "X-Syntropy-Key": "release-key" })).status, 200);
  const turnedAway = await refresh({});
  assert.equal(turnedAway.status, 403);
  assert.equal((await turnedAway.json()).error, "release_build_required");
  assert.equal((await refresh({ "X-Syntropy-Key": "nope" })).status, 403);
});

test("until REQUIRE_KEY is on, releases without the key are still served", async () => {
  assert.ok(await releaseBuild(undefined, { ...env, KEY_HASHES: "abc" }));
  assert.ok(await releaseBuild(undefined, { ...env, REQUIRE_KEY: "false" }));
  assert.ok(!(await releaseBuild(undefined, { ...env, REQUIRE_KEY: "true", KEY_HASHES: "" })));
  assert.ok(!(await releaseBuild("x".repeat(300), { ...env, REQUIRE_KEY: "true", KEY_HASHES: "" })));
});

test("health names the deployed commit", async () => {
  const res = await worker.fetch(new Request("https://relay.test/health"), { ...env, COMMIT: "abc123" });
  assert.equal((await res.json()).commit, "abc123");
});
