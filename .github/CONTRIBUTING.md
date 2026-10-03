# Contributing

Bug reports and ideas are welcome: open an issue. Never include real health information, tokens or keys; the
simulator's synthetic patients are the easiest way to show a problem. Security problems go through
[SECURITY.md](SECURITY.md), not a public issue. Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Code contributions

Syntropy Health is open source under the [GNU Affero General Public License v3.0 only](../LICENSE). Every contributor
signs the [Contributor License Agreement](CLA.md) once. It lets Syntropy Labs license your contributions under the AGPL
and under other terms, such as for the parts of Syntropy Health that aren't open source, and you keep the copyright to
your work.

1. Open an issue before starting on a pull request, so we can agree on the change.
2. Read the [CLA](CLA.md), then sign it by posting this comment on your pull request:

   > I have read the CLA Document and I hereby sign the CLA

   If you contribute as part of your job, your employer needs to sign a corporate agreement first
   (contact@syntropylabs.io).

Pull requests from contributors who haven't signed can't be merged.

## Getting set up

```bash
git clone https://github.com/EnriqueNeyra/syntropy-health && cd syntropy-health
./run.sh dev          # a local server with auto-reload at http://localhost:8000 (Python 3.12+)
```

Use the built-in EHR simulator and the simulated Oura and WHOOP sources for data; never develop against real records.

## Where things are

```
app/
  main.py            FastAPI app, security headers, startup (migrations, scheduler)
  api/               HTTP routers
  core/              config, SQLite and migrations, encryption, auth, settings
  store/             data access: profiles, records, biometrics, sleep, training, goals, chats
  services/          sync, imports, insights, the assistant and its AI connections
  connectors/        SMART on FHIR client, FHIR normalizer, UCUM units, Oura, WHOOP, Google Health, Apple Health
  simulator/         built-in SMART on FHIR EHR with synthetic patients
  mcp_server.py      MCP server for AI apps
  cli.py             the syntropy-health command
  static/            the web app: ES modules and CSS, no build step
  data/              institution directory, lab catalog
desktop/             Mac and Windows apps
scripts/             installers, directory refresh
cloudflare-worker/   OAuth relay for Oura, WHOOP and Google Health (+ tests)
tests/               pytest suite, fully offline
```

See [How it works](https://health.syntropylabs.io/docs/) for how it fits together.

## Before you open a pull request

```bash
./run.sh test                      # the whole suite, offline
./run.sh lint                      # ruff
cd cloudflare-worker && npm test   # if you changed the relay worker
```

The web app has no build step: check your change in the browser in light and dark appearance, and at phone width.
Add an entry under **Unreleased** in [CHANGELOG.md](../CHANGELOG.md).

## What runs on GitHub

| Workflow | When | What |
|---|---|---|
| CI | pushes to main, pull requests | lint, the test suite (Python 3.12 and the newest), the relay worker's tests, and the installers on real Linux, Mac and Windows machines |
| Docker image | pushes to main, pull requests, releases | builds and runs the image; publishes `:main` from main, and `:latest` and the version for a release |
| Release | a `v*` tag; changes under `desktop/` | builds and checks the Mac and Windows apps; a tag also runs CI and publishes the release |
| Tag release | a change to `APP_VERSION` merged into main | tags that version, which starts Release |
| CodeQL | pushes to main, pull requests, weekly | security scanning (public repository only) |
| CLA | pull requests | checks everyone with commits in it has signed the CLA |

Pull requests into main need **CI passed** (every CI job succeeded), **image** and **CLA**.

Dependabot proposes dependency updates weekly (Python, the Docker base image) and monthly (the workflows' actions).

## Releasing

Open a pull request that sets `APP_VERSION` in `app/core/config.py` to the new version and renames the changelog's
**Unreleased** heading to it (`## 1.2.0`). Merging it releases: the version is tagged, CI runs again on it, and the Mac
and Windows apps, the Python wheel and the Docker image are built and published, with that changelog section as the
release notes. Installers, `syntropy-health update`, the apps' update checks and Docker's `:latest` then use it.

To try the apps before releasing: Actions → Release → Run workflow, then download them from the run.
