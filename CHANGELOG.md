# Changelog

All notable changes to Syntropy Health. The version is in `app/core/config.py` (`APP_VERSION`); releases are tagged
`vX.Y.Z`, which also builds the Mac and Windows apps.

## Unreleased

- **Oracle Health and eClinicalWorks (healow) health systems connect for real.** Syntropy Health's client IDs for both
  are built in, so choosing one takes you to its own sign-in, with nothing to set up. On October 3, 1,311 of Oracle
  Health's 1,319 organizations and 95% of a sample of 300 eClinicalWorks practices reached their sign-in; most of the
  rest are eClinicalWorks practices that don't offer patients online access. Whether eClinicalWorks keeps a connection
  syncing after the first sign-in (with a refresh token) isn't confirmed yet; if it doesn't, a later sync asks you to
  reconnect.
- The weekly sign-in check covers Oracle Health and eClinicalWorks too, so their health systems whose sign-in is broken
  are marked before you try, like Epic's.
- **athenahealth practices connect for real.** Syntropy Health's production client ID is built in, so choosing
  athenahealth takes you to its patient sign-in with nothing to set up.
- **athenahealth connections keep syncing.** Renewing access now sends the scope athenahealth requires, so a connection
  stays live past its first hour (athenahealth's refresh token lasts 90 days from its last use, and each sync renews
  it). The sign-in no longer asks for insurance coverage, which athenahealth refused outright, and medications are
  imported: athenahealth only answers a medication search that names the order's intent.

## 1.3.0

- **Health systems whose sign-in isn't working say so before you try.** A weekly check opens every Epic health
  system's sign-in, the way a browser would, and records the ones that fail. Choosing one now explains what's wrong
  (its sign-in page is broken, its server isn't answering, or it hasn't finished setting up Syntropy Health), offers to
  import a record file, and still lets you try. When another listing of the same health system works, it's suggested:
  UPMC's broken listing points to **UPMC Patient Portal**. Listings that work come first in search.
- **Kaiser Permanente connects again** in every region. Kaiser's sign-in page for all regions but Washington has been
  failing with "Page Not Found", while the same page spelled in lowercase works, so sign-in now goes there. This is a
  temporary workaround: it switches itself off once Kaiser fixes the page.
- **Kaiser's later pages of clinical notes are imported.** Kaiser writes its server name in capitals in the links to
  the next page (FHIR.KP.ORG), and a sync stopped there as if the link led to another server, marking it partial.
- **The health-system directory is current again**, from Epic's, Oracle Health's and eClinicalWorks's lists of
  October 3: 3 Epic health systems and 509 eClinicalWorks practices are new, and SGMC's address changed. A weekly job
  keeps it current from now on.
- Refreshing the directory no longer swaps two health systems that share a name (Memorial Health in Ohio and in Georgia),
  which would have shown a saved connection under the other one.

## 1.2.1

- **A way back from a health system's sign-in in the Mac app.** While the window shows another site, it has an
  ordinary title bar naming the site and a **‹ Syntropy Health** button that returns to the app (also Go → Back to
  Syntropy Health, ⇧⌘H). Before, the page filled the window with no way back but swiping or ⌘[.

## 1.2.0

- **Health systems connect for real.** Choosing Kaiser Permanente, or any Epic health system, takes you to its own
  MyChart sign-in. Before, a sandbox mode or client ID left in Settings sent it to Epic's test server instead.
- **Developer mode** (Settings → Developer) is now one switch. Off, every platform connects for real with its production
  client ID. On, each platform uses the mode you choose: the simulator, the vendor's sandbox, or production. Each platform
  has a separate sandbox and production client ID, and a client ID saved before this version moves to the mode it was
  saved for. In the environment, `EPIC_CLIENT_ID` and the like are production IDs; sandbox IDs go in
  `EPIC_SANDBOX_CLIENT_ID` and the like. **Verify** checks a production ID against a real health system's sign-in.
- **Health system search** shows full names instead of cutting them off. Health systems that can't be connected yet
  (vendors Syntropy Health isn't registered with) are marked, listed after those that can, and suggest importing a
  record file instead. Popular shows only ones that connect, and the SMART test server appears in developer mode only.
- Releases carry each app once, under its fixed name (`Syntropy-Health-mac-arm64.dmg`,
  `Syntropy-Health-windows-x64-setup.exe`), instead of a second copy with the version in its name.

## 1.1.2

- **Updates download again in the Mac app.** It couldn't verify GitHub's certificate, so installing an update failed.
  The app also reaches a Syntropy Health server at an `https://` address when joining one. Updating to 1.1.2 itself
  needs [downloading it](https://github.com/EnriqueNeyra/syntropy-health/releases/latest) once; later updates
  install themselves.
- Releasing is a pull request: merging a change to `APP_VERSION` tags it and publishes the release, with the changelog
  as its notes. Pull requests need one CI check, **CI passed**, which covers every job, and the tests also run on the
  newest Python.

## 1.1.1

- **Epic (MyChart) connects for real.** Syntropy Health is registered with Epic, so choosing an Epic health system
  takes you to its MyChart sign-in, with no client ID to set up. Health systems receive the app from Epic over time;
  until yours has it, signing in there won't work yet. Settings → Developer still switches Epic to
  Simulated or Sandbox.

## 1.1.0

### Insights across your sources
- The Overview opens with **What's changed**: a number drifting from your own normal (resting heart rate, HRV, sleep,
  steps, weight…), how your sleep lines up with the mood, energy and stress you check in with, symptoms against the
  night before, training-load jumps and HRV after hard days, lab results moving further out of range, measurements
  before and after a medication started, and goals kept. Each says what lined up, with its numbers, never a diagnosis.
- **Compare** under every Trends chart: any two measures day by day (wearables, check-ins, workouts), the same day, the
  day before or the next, with a scatter plot and a plain-language summary.
- Trends charts mark when medications started and conditions were diagnosed, including lab charts.
- **Goals** for any daily measure (at least or at most): a line on the chart, days met, streaks, a Goals card and
  progress on the Overview's number tiles.
- Trends goes back as far as your data (All), in weekly or monthly averages for long spans.

### Sleep and training
- **Sleep, night by night**: stages for each night, bedtime and wake time and how much they vary, the latest night
  stage by stage, and how your nights divide up against typical ranges.
- **Workouts**: training load this week against your usual (heart-rate-weighted), heart-rate zones, weekly volume,
  pace and heart rate over time and best efforts for each kind of workout, an icon per kind, and pace, zones and
  splits in each workout.

### Journal, Timeline and Ask
- The Journal shows patterns with your sleep and training, and offers your current medications when you log a dose.
- The Timeline includes symptoms, doses, notes and device alerts (ECGs, notifications) alongside records, with filters
  for workouts and check-ins.
- Ask keeps past conversations (**History**) on the server, per person and account, so they follow you between the
  computer and the iPhone app. The conversation kept in this browser before becomes the first one.
- AI apps get new tools: `get_insights`, `compare_measures`, `get_sleep_nights`, `get_training_summary`, `get_goals`.
- On phones, Journal is in the tab bar; Records moved under More.

### Updates and installing
- **Update notices**: once a day Syntropy Health asks GitHub whether a newer release is out (nothing about you or your
  records is sent) and tells the server's owner in Alerts and **Settings → General → Updates**, with how to update this
  installation. Checks can be turned off there, or for good with `SYNTROPY_UPDATE_CHECK=false`.
- The Mac and Windows apps **install updates themselves**: from Settings, from **Check for Updates…** in the app and
  menu bar or tray menus, or automatically while the window is closed with **Install updates automatically** on. The
  download is checked against the release's SHA-256 checksums (and, once the apps are signed, the developer's
  signature) before it's installed, and the app opens again the way it was.
- The Linux installer adds a daily timer that installs new releases when automatic updates are on.
- Installers and `syntropy-health update` install the **latest release** instead of the newest code on `main`.
- A **Docker image** for amd64 and arm64 (Raspberry Pi, ARM NAS boxes) at `ghcr.io/enriqueneyra/syntropy-health`:
  `docker compose pull` updates it, and no clone of the repository is needed.
- Releases include the Python wheel and `SHA256SUMS`, and a release's tag has to match the version in the code.

### Project
- A shorter README, with detailed install and configuration moved to [the setup guides](https://health.syntropylabs.io/setup/), a
  code of conduct, and richer package metadata.
- Release files and the Docker image carry signed build provenance (`gh attestation verify`, see
  [SECURITY.md](.github/SECURITY.md)), and the relay's `/health` names the commit it's running.
- Python 3.12 or newer is required. Dependencies are listed once, in `pyproject.toml`: `pip install -e ".[desktop]"`
  for the desktop app, `pip install --group dev` for tests and linting, and `./run.sh lint`.
- When a sign-in server can't be reached or an update fails unexpectedly, Settings shows a plain sentence; the details
  go to the log.

### Removed
- The upgrade for databases from the prototype that came before 1.0.
- The relay's older hand-off that put tokens in the address's query string.

### Ask
- Answers stream in as they're written, from API keys (Anthropic and OpenAI-compatible services, including models on
  your own computer) and from Claude Code and Gemini CLI. Text appears word by word with a caret, at a pace that keeps
  up with the model; tables appear a row at a time and bold or code never flashes raw symbols. Codex CLI answers arrive
  whole and are revealed the same way.
- **Stop** ends an answer mid-way (and the AI app running it on this computer); what was written so far is kept.
- Messages and lookups ease in; the conversation follows the answer unless you've scrolled up to read.
- The conversation fills the window and scrolls on its own: no second scrollbar for the page, in the browser and in
  the Mac app. A saved conversation opens at its latest message.

### Motion and finishing touches
- Switching sections cross-fades the window while the new page slides in; tabs and filters within a page cross-fade.
- Light and dark cross-fade instead of snapping, and changing the theme or accent no longer redraws the page or jumps
  back to the top. The system switching between light and dark fades too.
- Dialogs, drawers, menus and toasts ease in and out (toasts can be clicked away); buttons and chips respond to a
  press; keyboard focus moves into a dialog and returns to where it was when it closes.
- Charts draw themselves in the first time they appear: lines trace across, bars grow, sparklines wipe in. Redraws with
  fresh data don't replay it.
- Scrollbars are thin and follow the theme where the browser allows it.
- Everything above is turned off for people who ask their system for reduced motion.

### Fixed
- Settings → General → Time zone showed the first zone in the list (Africa/Abidjan) on a server running on UTC, and
  saving the page would have switched to it. UTC is now listed first, and the zone in use always appears.
- Lab results keep the precision the lab reported: a series reads 5.9, 6.0, 6.1 % rather than 5.9, 6, 6.1 %, and the
  reference range matches (4.0–5.6 %).
- The last date on daily bar charts no longer overlaps the one before it.
- The sleep chart's average and tooltips read in hours and minutes (avg 7h 13m) like the rest of the page, and a
  duration can no longer round to "7h 60m".
- Lab insights keep the lab's precision ("up from 6.0 %", not "6 %").
- The Overview's vitals card link reads "Vital trends" instead of "Vital trends 8".
- The visit summary capitalizes allergy criticality and insurance status.
- The step dots on first-run setup line up with the wordmark.
- Signing in again after locking no longer starts a second copy of the background alert checks.

### Improved
- When the server can't be reached (it's off, asleep, or you're on another network), pages say so in words and offer
  **Try again**, instead of "Failed to fetch". The start screen retries on its own when the network comes back, and a
  page whose code didn't load after an update offers **Reload**.
- Unexpected server errors answer with a sentence the app can show and are logged with their details, rather than a bare
  "Internal Server Error".
- `/` and `/health` answer `HEAD` requests, for uptime monitors (Uptime Kuma, Healthchecks and the like).

### Security
- The Content-Security-Policy now covers every address that shows the app, not only `/` (other addresses fall back to
  the same page), and the OAuth hand-off page at `/callback` gets its own strict policy allowing only its script, by
  hash.
- `Permissions-Policy` turns off the camera, microphone, location, payment and other browser features the app never
  uses; `Cross-Origin-Opener-Policy: same-origin` isolates its window.

### Project
- [SECURITY.md](.github/SECURITY.md): how to report a vulnerability privately, and what's in scope.
- Issue forms for bugs and ideas (with a reminder never to post health data), a pull request checklist, and Dependabot
  for Python packages and GitHub Actions.
- CI lints the Python code with Ruff (settings in `pyproject.toml`).

## 1.0.0

The first release: a self-hosted personal health record.

- **Health systems** through SMART on FHIR patient access (Epic, Oracle Health, eClinicalWorks and any FHIR R4 server),
  with PKCE, paged download, normalization to one set of units and de-duplication across institutions. A built-in EHR
  simulator with synthetic patients runs the whole pipeline until vendor registrations are in place.
- **Wearables**: Oura, WHOOP and Google Health (Fitbit, Pixel Watch), through a relay that completes the sign-in and
  keeps nothing; Apple Health through the iPhone app or an export file. Daily roll-ups choose one device per day.
- **Pages**: a customizable Overview, Ask, Trends (labs, vitals, heart, sleep, activity, body), Workouts, Journal,
  Timeline, Records, Sources, a printable visit summary and Settings.
- **Lab reports** from PDFs and photos of paper reports, read on this computer with Tesseract.
- **Households**: each person signs in with their own password and shares with others to view or edit.
- **AI**: models on your own computer, your own API key, Claude Code or Codex already signed in, or any MCP app, all
  read-only and logged.
- **Export**: FHIR R4 bundles, CSV, and full database backups.
- **Installs**: Mac and Windows apps, a Linux home-server installer (systemd), Docker, and `uv`/`pipx`.
