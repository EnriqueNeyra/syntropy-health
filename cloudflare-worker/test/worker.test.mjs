// Run with: npm test (from cloudflare-worker/)
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, parse } from "node:path";
import { fileURLToPath } from "node:url";
import worker, { destination, returnsWithoutStop, releaseBuild } from "../worker.js";

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const env = { WHOOP_CLIENT_ID: "wid", WHOOP_CLIENT_SECRET: "wsecret", OURA_CLIENT_ID: "oid", OURA_CLIENT_SECRET: "osecret" };
const here = dirname(fileURLToPath(import.meta.url));
const rulesText = readFileSync(join(here, "relay-rules.json"), "utf8");
const rules = JSON.parse(rulesText);

const FUNNEL = "https://evil-funnel.tail1234.ts.net";
const get = (query) => worker.fetch(new Request(`https://relay.test/callback?${new URLSearchParams(query)}`), env);
const confirm = (fields, headers = { "Sec-Fetch-Site": "same-origin" }) => worker.fetch(new Request("https://relay.test/callback", {
  method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", ...headers }, body: new URLSearchParams(fields),
}), env);

// Every call the worker makes to a provider. A test that expects none asserts the list stays empty.
function providerAnswers(status = 200, body = { access_token: "VICTIM_AT", refresh_token: "VICTIM_RT", expires_in: 3600, scope: "offline" }) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: String(init.body) });
    return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
  };
  return calls;
}

// What the page's continue form would send.
const hidden = (html) => Object.fromEntries([...html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)]
  .filter(([, name]) => name !== "stop")
  .map(([, name, value]) => [name, value.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">")]));

test("each destination in the shared rules is classed the same as the callback page classes it", () => {
  for (const c of rules.destinations) {
    const got = destination(b64({ d: c.d }));
    assert.equal(got.class, c.class, JSON.stringify(c.d));
    if (c.origin) assert.equal(got.origin, c.origin, JSON.stringify(c.d));
  }
  for (const c of rules.states) {
    const got = destination(c.state);
    assert.equal(got.class, c.class, c.name);
    assert.equal(got.origin, c.origin, c.name);
  }
  assert.ok(returnsWithoutStop("[::1]") && returnsWithoutStop("NAS.local") && !returnsWithoutStop("10..") && !returnsWithoutStop("1.2.3.4.5"));
});

test("the shared rules match the callback page's copy", (t) => {
  const named = process.env.SYNTROPY_SITE_DIR;
  let other = named ? join(named, "test", "relay-rules.json") : null;
  if (!other) {
    for (let dir = here; !other && dir !== parse(dir).root; dir = dirname(dir)) {
      const candidate = join(dir, "syntropy-health-site", "test", "relay-rules.json");
      if (existsSync(candidate)) other = candidate;
    }
  }
  if (!other || !existsSync(other)) return t.skip("the website repository isn't next to this one; its own CI compares the two");
  assert.equal(rulesText, readFileSync(other, "utf8"), `${other} differs from this copy`);
});

test("the shared rules' key hashes and default are the worker's", () => {
  const toml = readFileSync(join(here, "..", "wrangler.toml"), "utf8");
  assert.deepEqual(/KEY_HASHES = "([^"]*)"/.exec(toml)[1].split(","), rules.keyHashes);
  assert.equal(destination("").origin, rules.defaultOrigin);
});

test("a Funnel host gets a question, and no redirect, no code exchange, no token until someone clicks (F022)", async () => {
  for (const p of ["oura", "whoop", "smart"]) {
    const calls = providerAnswers();
    const state = b64({ p, n: "x", d: FUNNEL });
    const res = await get({ code: "victim-code", state });
    assert.equal(res.status, 200, p);
    assert.equal(res.headers.get("location"), null, p);
    assert.deepEqual(calls, [], p);
    const html = await res.text();
    assert.match(html, /evil-funnel\.tail1234\.ts\.net/);
    assert.match(html, /did not start this from your own Syntropy Health, do not continue/);
    assert.doesNotMatch(html, /<a [^>]*evil-funnel/, "the way forward is only a form, so nothing can follow it");
    assert.deepEqual(hidden(html), { code: "victim-code", state });
    assert.match(html, /<form method="post" action="\/callback">/);
    const csp = res.headers.get("content-security-policy");
    assert.match(csp, /frame-ancestors 'none'/);
    assert.match(csp, /form-action 'self' https:\/\/evil-funnel\.tail1234\.ts\.net(;|$)/, "browsers check a form's redirect against this too");
    assert.equal(res.headers.get("x-frame-options"), "DENY");
    assert.equal(res.headers.get("cache-control"), "no-store");
  }
});

test("nothing in the link can stand in for the click", async () => {
  const calls = providerAnswers();
  for (const extra of [{ confirm: "1" }, { confirmed: "true" }, { method: "POST" }]) {
    const res = await get({ code: "abc", state: b64({ p: "oura", d: FUNNEL, confirm: true, confirmed: 1 }), ...extra });
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("location"), null);
  }
  assert.deepEqual(calls, []);
});

test("clicking continue sends the code on, and only then does the worker exchange a wearable's", async () => {
  const smart = b64({ p: "smart", n: "x", d: "https://health.example.com" });
  let calls = providerAnswers();
  let res = await confirm({ code: "abc", state: smart });
  assert.equal(res.status, 303);
  const loc = new URL(res.headers.get("location"));
  assert.equal(loc.origin, "https://health.example.com");
  assert.equal(loc.pathname, "/callback");
  assert.equal(loc.searchParams.get("code"), "abc");
  assert.equal(loc.searchParams.get("state"), smart);
  assert.deepEqual(calls, []);

  calls = providerAnswers();
  const state = b64({ p: "oura", n: "x", d: "http://nas:8000" });
  res = await confirm({ code: "abc", state });
  assert.equal(res.status, 303);
  const tokens = new URL(res.headers.get("location"));
  assert.equal(tokens.origin, "http://nas:8000");
  assert.equal(tokens.search, "");
  const frag = new URLSearchParams(tokens.hash.slice(1));
  assert.equal(frag.get("access_token"), "VICTIM_AT");
  assert.equal(frag.get("state"), state);
  assert.equal(calls.length, 1);
  assert.match(calls[0].body, /redirect_uri=https%3A%2F%2Frelay\.test%2Fcallback/);
});

test("the form's addresses a browser can't name in its policy still get through", async () => {
  providerAnswers();
  for (const d of ["http://[fd12:3456::1]:8000", "http://my_nas:8000"]) {
    const res = await get({ code: "abc", state: b64({ p: "smart", d }) });
    assert.match(res.headers.get("content-security-policy"), /form-action 'self' http: https:(;|$)/, d);
  }
});

test("a post from another site is refused before anything happens (F022)", async () => {
  const calls = providerAnswers();
  const fields = { code: "abc", state: b64({ p: "oura", d: FUNNEL }) };
  for (const headers of [{ "Sec-Fetch-Site": "cross-site" }, { "Sec-Fetch-Site": "same-site" }, { "Sec-Fetch-Site": "none" }, { Origin: "https://evil.example.com" }]) {
    const res = await confirm(fields, headers);
    assert.equal(res.status, 403, JSON.stringify(headers));
    assert.equal(res.headers.get("location"), null);
  }
  assert.deepEqual(calls, []);
  // Older browsers send neither header, and a no-referrer page's form sends Origin: null.
  for (const headers of [{}, { Origin: "null" }, { Origin: "https://relay.test" }]) {
    assert.equal((await confirm(fields, headers)).status, 303, JSON.stringify(headers));
  }
});

test("don't continue forwards nothing and spends nothing", async () => {
  const calls = providerAnswers();
  const html = await (await get({ code: "abc", state: b64({ p: "oura", d: FUNNEL }) })).text();
  const stop = /<form method="post" action="\/callback"><input type="hidden" name="stop" value="1"><button class="main">([^<]+)<\/button><\/form>/.exec(html);
  assert.ok(stop, "the safe choice is the one that looks like the main button");
  assert.ok(html.indexOf(stop[0]) < html.indexOf('name="code"'), "and comes first");
  const res = await confirm({ stop: "1" });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("location"), null);
  assert.match(await res.text(), /Nothing was sent/);
  assert.deepEqual(calls, []);
});

test("the address on the page can't be used to write into it", async () => {
  providerAnswers();
  const code = `"><script>alert(1)</script>`;
  const res = await get({ code, state: b64({ p: "smart", d: FUNNEL, n: "<img>" }) });
  const html = await res.text();
  assert.doesNotMatch(html, /<script/);
  assert.equal(hidden(html).code, code);
});

test("private addresses and this computer return with no stop (1.6.0 and earlier included)", async () => {
  providerAnswers();
  for (const d of ["http://localhost:8000", "http://192.168.1.20:8000", "http://100.101.1.2:8000", "http://nas.local:8000"]) {
    const res = await get({ code: "abc", state: b64({ p: "smart", n: "x", d }) });
    assert.equal(res.status, 302, d);
    assert.equal(new URL(res.headers.get("location")).origin, d);
  }
  const noAddress = await get({ code: "abc", state: b64({ p: "smart", n: "x" }) });
  assert.equal(new URL(noAddress.headers.get("location")).origin, "http://localhost:8000");
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

test("a token field the provider leaves out isn't sent empty (F329)", async () => {
  providerAnswers(200, { access_token: "AT" });
  const res = await get({ code: "abc", state: b64({ p: "oura", d: "http://localhost:8000" }) });
  const frag = new URLSearchParams(new URL(res.headers.get("location")).hash.slice(1));
  assert.deepEqual([...frag.keys()].sort(), ["access_token", "relay_provider", "state"]);
});

test("an address that can't be returned to is named, with no code spent and no trip to localhost (F097)", async () => {
  const calls = providerAnswers();
  for (const d of ["javascript:alert(1)", "not a url", 7]) {
    const res = await get({ code: "abc", state: b64({ p: "oura", d }) });
    assert.equal(res.status, 400, String(d));
    assert.equal(res.headers.get("location"), null);
    const html = await res.text();
    assert.match(html, /Nothing was sent/);
    assert.doesNotMatch(html, /localhost/);
    assert.doesNotMatch(html, /<a /);
    if (typeof d === "string") assert.match(html, new RegExp(d.replace(/[()]/g, "\\$&")));
    assert.equal((await confirm({ code: "abc", state: b64({ p: "oura", d }) })).status, 400);
  }
  assert.deepEqual(calls, []);
});

test("public domains and bare names reach the question, not localhost (F097)", async () => {
  const calls = providerAnswers();
  for (const d of ["https://health.example.com", "http://nas:8000", "http://synology:5000", "http://box.fritz.box:8000"]) {
    const res = await get({ code: "abc", state: b64({ p: "oura", d }) });
    assert.equal(res.status, 200, d);
    assert.match(await res.text(), new RegExp(d.replace(/^https?:\/\//, "").replace(/\./g, "\\.")));
  }
  assert.deepEqual(calls, []);
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

test("refresh takes only a JSON object with a known provider (F329)", async () => {
  const calls = providerAnswers();
  const refresh = (body) => worker.fetch(new Request("https://relay.test/refresh", { method: "POST", body }), env);
  for (const body of ["null", "1", '"x"', "[]", "{", '{"provider":"constructor","refresh_token":"RT"}', '{"provider":"__proto__","refresh_token":"RT"}',
    '{"provider":7,"refresh_token":"RT"}', '{"provider":"google","refresh_token":"RT"}', '{"provider":"oura","refresh_token":7}']) {
    const res = await refresh(body);
    assert.equal(res.status, 400, body);
    assert.match(res.headers.get("content-type"), /json/, body);
    assert.equal((await res.json()).error, "invalid_request", body);
  }
  assert.deepEqual(calls, []);
});

test("a provider that's down is told apart from one that refuses (F236)", async () => {
  const refresh = () => worker.fetch(new Request("https://relay.test/refresh", { method: "POST", body: JSON.stringify({ provider: "oura", refresh_token: "RT" }) }), env);
  providerAnswers(503, "<html>Service Unavailable</html>");
  let res = await refresh();
  assert.equal(res.status, 502);
  assert.equal((await res.json()).error, "provider_unavailable");

  providerAnswers(429, { error: "slow down" });
  assert.equal((await refresh()).status, 502);

  globalThis.fetch = async () => { throw new TypeError("network connection lost"); };
  res = await refresh();
  assert.equal(res.status, 502);
  assert.doesNotMatch(await res.text(), /connection lost/);

  providerAnswers(400, { error: "invalid_grant" });
  res = await refresh();
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error, "refresh_failed");

  providerAnswers(400, { error: "invalid_grant" });
  const page = await get({ code: "abc", state: b64({ p: "oura", d: "http://localhost:8000" }) });
  assert.equal(page.status, 502);
  assert.match(await page.text(), /Oura sign-in failed/);

  globalThis.fetch = async () => { throw new TypeError("network connection lost"); };
  const down = await get({ code: "abc", state: b64({ p: "oura", d: "http://localhost:8000" }) });
  assert.equal(down.status, 502);
  const text = await down.text();
  assert.doesNotMatch(text, /connection lost/);
  assert.match(text, /Couldn&#39;t reach Oura/);
});

test("a crafted state gets a plain answer, not a raw error (F329)", async () => {
  const calls = providerAnswers();
  for (const state of ["x;dest=%E0%A4%A", b64({ p: 1 }), b64({ p: ["oura"] }), b64({ p: "constructor" }), b64({ p: "__proto__" })]) {
    const res = await get({ code: "abc", state });
    assert.equal(res.status, 302, state);
    assert.equal(new URL(res.headers.get("location")).origin, "http://localhost:8000", state);
  }
  assert.deepEqual(calls, []);
});

test("anything that goes wrong inside says so plainly", async () => {
  const broken = { ...env, RATE_LIMITER: { limit: async () => { throw new Error("internal detail"); } } };
  const res = await worker.fetch(new Request("https://relay.test/callback?code=abc"), broken);
  assert.equal(res.status, 500);
  const html = await res.text();
  assert.doesNotMatch(html, /internal detail/);
  assert.doesNotMatch(html, /<a /);
  const rf = await worker.fetch(new Request("https://relay.test/refresh", { method: "POST", body: "{}" }), broken);
  assert.equal(rf.status, 500);
  assert.match(rf.headers.get("content-type"), /json/);
});

test("the unused Google route is gone: Google Health is always the person's own app (F235)", async () => {
  const calls = providerAnswers();
  const state = b64({ p: "google", n: "x", d: "http://localhost:8000" });
  const res = await get({ code: "abc", state });
  assert.equal(new URL(res.headers.get("location")).searchParams.get("code"), "abc");
  assert.deepEqual(calls, []);
  const source = readFileSync(join(here, "..", "worker.js"), "utf8") + readFileSync(join(here, "..", "wrangler.toml"), "utf8");
  assert.doesNotMatch(source, /GOOGLE|googleapis/);
});

test("provider errors are forwarded to the instance", async () => {
  const state = b64({ p: "smart", d: "http://localhost:8000" });
  const res = await worker.fetch(new Request(`https://relay.test/callback?error=access_denied&state=${state}`), env);
  const loc = new URL(res.headers.get("location"));
  assert.equal(loc.searchParams.get("error"), "access_denied");
});

test("an error for an address that needs a question gets the same question, without the provider's text", async () => {
  const state = b64({ p: "smart", d: FUNNEL });
  const res = await get({ error: "access_denied", error_description: "Call 555 to fix your account", state });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("location"), null);
  const html = await res.text();
  assert.match(html, /evil-funnel/);
  assert.doesNotMatch(html.replace(/<input [^>]*>/g, ""), /Call 555/);
  assert.equal(hidden(html).error, "access_denied");
  const back = await confirm({ error: "access_denied", error_description: "denied", state });
  assert.equal(back.status, 303);
  const loc = new URL(back.headers.get("location"));
  assert.equal(loc.origin, FUNNEL);
  assert.equal(loc.searchParams.get("error"), "access_denied");
  assert.equal(loc.searchParams.get("state"), state);
});

test("no code and no error is said plainly, on either kind of address", async () => {
  for (const d of ["http://localhost:8000", FUNNEL]) {
    const res = await get({ state: b64({ p: "smart", d }) });
    assert.equal(res.status, 400);
    const html = await res.text();
    assert.match(html, /Missing authorization code/);
    assert.equal(/<a /.test(html), d !== FUNNEL, "only a return address that needs no question gets a link");
  }
  assert.equal((await confirm({ state: b64({ p: "smart", d: FUNNEL }) })).status, 400);
});

test("callback and refresh are rate-limited per client IP", async () => {
  const seen = [];
  const limited = { ...env, RATE_LIMITER: { limit: async ({ key }) => { seen.push(key); return { success: false }; } } };
  const headers = { "cf-connecting-ip": "203.0.113.9" };
  const cb = await worker.fetch(new Request("https://relay.test/callback?code=abc", { headers }), limited);
  const rf = await worker.fetch(new Request("https://relay.test/refresh", { method: "POST", headers, body: "{}" }), limited);
  const post = await worker.fetch(new Request("https://relay.test/callback", { method: "POST", headers, body: "code=abc" }), limited);
  assert.equal(cb.status, 429);
  assert.equal(rf.status, 429);
  assert.equal(post.status, 429);
  assert.deepEqual(seen, ["203.0.113.9", "203.0.113.9", "203.0.113.9"]);
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
  // Turned away before anyone is asked about an address, and again if they post anyway.
  assert.equal((await callback({ p: "smart", d: FUNNEL })).status, 403);
  const posted = await worker.fetch(new Request("https://relay.test/callback", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ code: "abc", state: b64({ p: "smart", d: FUNNEL }) }) }), strict);
  assert.equal(posted.status, 403);
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

test("REQUIRE_KEY stays off in what's deployed", () => {
  assert.match(readFileSync(join(here, "..", "wrangler.toml"), "utf8"), /REQUIRE_KEY = "false"/);
});

test("health names the deployed commit", async () => {
  const res = await worker.fetch(new Request("https://relay.test/health"), { ...env, COMMIT: "abc123" });
  assert.equal((await res.json()).commit, "abc123");
});
