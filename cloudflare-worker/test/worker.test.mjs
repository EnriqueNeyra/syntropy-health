// Run with: npm test (from cloudflare-worker/)
import test from "node:test";
import assert from "node:assert/strict";
import worker, { assertionAudience, isPrivateHost, resolveTarget } from "../worker.js";

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

test("health names the deployed commit", async () => {
  const res = await worker.fetch(new Request("https://relay.test/health"), { ...env, COMMIT: "abc123" });
  assert.equal((await res.json()).commit, "abc123");
});

async function signingEnv() {
  const { privateKey, publicKey } = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-384" }, true, ["sign", "verify"]);
  const jwk = { ...(await crypto.subtle.exportKey("jwk", privateKey)), kid: "k1" };
  return { env: { ...env, CLIENT_SIGNING_KEY: JSON.stringify(jwk), SIGNED_CLIENT_IDS: "healow-app, other" }, publicKey };
}

const assertionRequest = (body) => new Request("https://relay.test/assertion", { method: "POST", body: JSON.stringify(body) });
const TOKEN_URL = "https://oauthserver.eclinicalworks.com/oauth/oauth2/token";

test("assertions are signed for registered clients and healow token endpoints", async () => {
  const { env: signing, publicKey } = await signingEnv();
  const res = await worker.fetch(assertionRequest({ client_id: "healow-app", aud: TOKEN_URL }), signing);
  assert.equal(res.status, 200);
  const jwt = (await res.json()).client_assertion;
  const [header, claims, sig] = jwt.split(".");
  assert.deepEqual(JSON.parse(Buffer.from(header, "base64url")), { alg: "RS384", typ: "JWT", kid: "k1" });
  const c = JSON.parse(Buffer.from(claims, "base64url"));
  assert.equal(c.iss, "healow-app");
  assert.equal(c.sub, "healow-app");
  assert.equal(c.aud, TOKEN_URL);
  assert.equal(c.exp - c.iat, 300);
  assert.ok(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", publicKey, Buffer.from(sig, "base64url"), new TextEncoder().encode(`${header}.${claims}`)));
});

test("assertions are refused for other clients and audiences", async () => {
  const { env: signing } = await signingEnv();
  for (const body of [{ client_id: "someone-else", aud: TOKEN_URL }, { client_id: "healow-app", aud: "https://evil.example.com/token" },
    { client_id: "healow-app", aud: "http://oauthserver.eclinicalworks.com/token" }, { client_id: "healow-app" }]) {
    assert.equal((await worker.fetch(assertionRequest(body), signing)).status, 400, JSON.stringify(body));
  }
  const unconfigured = await worker.fetch(assertionRequest({ client_id: "healow-app", aud: TOKEN_URL }), { ...signing, CLIENT_SIGNING_KEY: undefined });
  assert.equal(unconfigured.status, 500);
  assert.ok(assertionAudience("https://fhir4.healow.com/fhir/r4/ABC/oauth2/token"));
  assert.ok(!assertionAudience("https://healow.com.evil.example/token"));
  assert.ok(!assertionAudience("https://fhir4.healow.com:8443/token"));
});

test("the JWKS publishes only the public key", async () => {
  const { env: signing } = await signingEnv();
  const { keys } = await (await worker.fetch(new Request("https://relay.test/jwks.json"), signing)).json();
  assert.equal(keys.length, 1);
  assert.deepEqual(Object.keys(keys[0]).sort(), ["alg", "e", "kid", "kty", "n", "use"]);
  const wellKnown = await worker.fetch(new Request("https://relay.test/.well-known/jwks.json"), signing);
  assert.deepEqual((await wellKnown.json()).keys, keys);
  assert.deepEqual(await (await worker.fetch(new Request("https://relay.test/jwks.json"), env)).json(), { keys: [] });
});
