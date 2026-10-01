# Security policy

Syntropy Health keeps people's medical records, so security reports are the most important issues we get. Thank you
for taking the time to send one.

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately, either way:

- [Open a private security advisory](https://github.com/EnriqueNeyra/syntropy-health/security/advisories/new) on GitHub
  (preferred), or
- email **contact@syntropylabs.io** with "Security" in the subject.

Include what you found, how to reproduce it (the built-in EHR simulator and its synthetic patients are the easiest way to
show it), which version (Settings → General, or `syntropy-health info`) and how it was installed. Never send real health
information.

What to expect:

- A reply within **3 business days** acknowledging the report.
- An assessment and a plan within **10 business days**.
- A fixed release as soon as it's ready, then a published advisory crediting you (unless you'd rather not be named).

We ask that you give us a reasonable time to ship a fix before disclosing, and that you don't access anyone else's data
while testing: run your own instance.

## Supported versions

Security fixes go into the latest release. Running any install command again updates in place
(see [Set up](https://health.syntropylabs.io/setup/)); `sudo syntropy-health update` on the Linux home server.

| Version | Supported |
|---|---|
| 1.x (latest) | Yes |
| Before 1.0 (the prototype) | No: upgrade by starting 1.x against the same data folder |

## In scope

- The server and web app in this repository (`app/`), the `syntropy-health` command, the installers in `scripts/`, the
  Docker image and the Mac and Windows apps (`desktop/`).
- The OAuth relay worker (`cloudflare-worker/`) and the static callback page it works with.
- The MCP server and the AI integrations (for example a way for a connected model to reach beyond its read-only tools).

Especially interesting: anything that lets someone read or change records without the password, bypasses the first-run
"nearby only" rule, leaks tokens or the encryption key, crosses between the people in one household, or breaks out of
the Content-Security-Policy.

## Out of scope

- An instance deliberately exposed to the internet without TLS, or with `SYNTROPY_ALLOW_NO_PASSWORD` set (a
  development-only setting).
- Someone with access to the computer's account or the data folder: the database, `secret.key` and backups are the
  keys to the record by design. Protect them like the records themselves.
- Findings from automated scanners with no demonstrated impact, and missing headers that don't lead to one.

## How Syntropy Health is protected

The [security overview](https://health.syntropylabs.io/docs/#security) lists the controls: password and sessions, first-run
restrictions, encryption at rest, PKCE and single-use OAuth state, CSRF and CSP, device tokens and the access log.

## Checking what you run

- **Release files** (the Mac and Windows apps, the wheel, `SHA256SUMS`) and the **Docker image** are built by this
  repository's workflows, which sign a build provenance attestation for each:
  `gh attestation verify <file> -R EnriqueNeyra/syntropy-health`, or
  `gh attestation verify oci://ghcr.io/enriqueneyra/syntropy-health:latest -R EnriqueNeyra/syntropy-health`.
  The image also carries an SBOM.
- **The OAuth relay** is deployed from `main` by a workflow, and its `/health` names the commit it runs:
  `curl https://syntropy-auth-relay.syntropylabs.workers.dev/health`.
