# Syntropy Health for Mac and Windows

The app runs the Syntropy Health server on this computer and shows it in a native window: WebKit on a Mac, Edge
WebView2 on Windows. Closing the window leaves the server running behind a menu bar icon (Mac) or tray icon (Windows),
so the iPhone app keeps syncing and automations keep running. The icon's menu has:

- **Let Your iPhone and Other Devices Connect** — listen on the home network and Tailscale, not just this computer, and
  announce `syntropyhealth.local`. A password is always set (the app asks for one at setup), so other devices sign in with
  it. Setup, Settings → Security and the pairing dialog have the same switch. Pairing then gives the
  phone this computer's home address, and suggests the Tailscale name as its away-from-home address.
- **Open at Login** — start in the background when you log in (a LaunchAgent on a Mac, the Run key on Windows).
- **Check for Updates…** — also in the Mac app menu. See [Updates](#updates).
- **Quit** — stops the server. Cmd-Q and Quit in the Dock also quit; the window's close button only hides it.

Data lives in `~/Library/Application Support/Syntropy Health` or `%LOCALAPPDATA%\Syntropy Health`. The window opens
`http://localhost:8000`: the app keeps port 8000 (saved in `desktop.json`), or takes the next free one the first time
if something else already has it.

Started with a command, the app acts as the `syntropy-health` command instead, without a window:
`"Syntropy Health.app/Contents/MacOS/Syntropy Health" mcp` (AI apps' stdio MCP), `… backup ~/Desktop`, `… info`. On
Windows, `syntropy-health.exe` next to the app is the console version of the same program.

## Updates

The server checks for new releases daily (app/services/updates.py); Settings → General → Updates shows the result, with
**Install and restart** and **Install updates automatically**. [updater.py](updater.py) does the installing:

1. Downloads the release's `Syntropy-Health-mac-arm64.dmg` or `Syntropy-Health-windows-x64-setup.exe` and `SHA256SUMS`
   from GitHub, and checks one against the other. When the running app is signed, the new one must be signed by the same
   developer (Apple Team ID, or Authenticode certificate).
2. Automatic updates wait until the window is closed; one the owner asked for installs straight away.
3. Mac: the new app is copied next to this one, and a small script swaps them once the app has quit and opens it
   again (in the background if it was). The app has to be somewhere it can write, like Applications. Windows: the setup
   program runs silently (`/VERYSILENT /RELAUNCH=window|background`), keeping the install folder and choices, and
   [installer.iss](installer.iss) opens the new version.

A Mac that opens another computer's server checks from **Check for Updates…** only.

## Signing

Unsigned apps work, but people have to get past a warning, and an update can't be checked against a signature.

**Mac.** Join the [Apple Developer Program](https://developer.apple.com/programs/) (yearly fee), create a **Developer ID
Application** certificate (Xcode → Settings → Accounts → Manage Certificates, or the developer website), export it from
Keychain Access as a `.p12`, and add these repository secrets; the release workflow then signs with the hardened runtime
and notarizes:

| Secret | Value |
|---|---|
| `MACOS_CERT_P12` | the `.p12`, base64 encoded (`base64 -i cert.p12 \| pbcopy`) |
| `MACOS_CERT_PASSWORD` | the password chosen when exporting it |
| `MACOS_SIGN_ID` | the certificate's name, `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID`, `APPLE_TEAM_ID` | the Apple Account and Team ID the membership belongs to |
| `APPLE_APP_PASSWORD` | an app-specific password for that Apple Account (account.apple.com → Sign-In and Security) |

**Windows.** Sign the setup program and the app with a code-signing certificate from a certificate authority, or with
Microsoft's Azure signing service. Either way the key lives in a hardware token or cloud service, so the workflow signs
through that service's tool. SmartScreen's warning fades as signed downloads build a reputation.

## Building

```bash
python3.12 -m venv .venv-desktop && .venv-desktop/bin/pip install -r desktop/requirements.txt
PYTHON=.venv-desktop/bin/python desktop/build-mac.sh        # desktop/dist/Syntropy-Health-<version>-mac-arm64.dmg
```

Icons are rendered from `app/static/img/icon.svg` by [icons/make-icons.sh](icons/make-icons.sh). The Mac icon follows
Apple's grid (an 824-pixel tile on a 1024 canvas), so it sits at the same size as other apps in the Dock.

`build-mac.sh` signs ad hoc by default, which runs on the Mac that built it. To share the app, sign it with a
"Developer ID Application" certificate and notarize it: set `SIGN_ID` and `NOTARY_PROFILE` (see the script, and
[Signing](#signing)).

Windows builds run on GitHub Actions ([.github/workflows/desktop.yml](../.github/workflows/desktop.yml)): PyInstaller,
then Inno Setup ([installer.iss](installer.iss)) makes a per-user installer that needs no administrator rights. Run the
workflow by hand, or push a tag like `v1.0.0` (matching `APP_VERSION`) to also draft a GitHub release with both apps,
the Python wheel and `SHA256SUMS`. Publishing the draft makes it the version everyone's updates install. Without a code-signing
certificate, Windows SmartScreen warns on first launch ("More info → Run anyway").

Running from source, for development: `SYNTROPY_DATA_DIR=/tmp/syntropy-dev .venv-desktop/bin/python desktop/syntropy_desktop.py`
(set the data folder, or a clone would use the repository's `./data`).
