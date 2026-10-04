// Prints a new private signing key for the relay's /assertion and /jwks.json routes, as one line of JSON. Pipe it
// straight into the Worker's secrets so it is never shown or saved anywhere else:
//   node new-signing-key.mjs | npx wrangler secret put CLIENT_SIGNING_KEY
// Replacing the key breaks healow connections until healow fetches the new /jwks.json, so do it only if it leaks.
const { privateKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-384" },
  true, ["sign", "verify"]);
const jwk = await crypto.subtle.exportKey("jwk", privateKey);
process.stdout.write(JSON.stringify({ ...jwk, kid: crypto.randomUUID() }));
