#!/bin/sh
# Syntropy Health from the command line, on a Mac or a Linux home server (Ubuntu, Debian, Raspberry Pi OS, Fedora,
# Arch and others). On Windows, use scripts/install.ps1.
#
#   Mac:    curl -fsSL https://health.syntropylabs.io/install.sh | sh
#   Linux:  curl -fsSL https://health.syntropylabs.io/install.sh | sudo sh
#
# On a Mac it installs the Syntropy Health app in Applications and opens it, plus the `syntropy-health` command.
# With --server it instead runs Syntropy Health as a background service for your account, with no app (for a Mac
# mini kept as a home server). Data lives in ~/Library/Application Support/Syntropy Health either way.
#
# On Linux, all in plain view below:
#   - installs uv (Astral's Python installer) if it's missing, and a private Python 3.12 under /opt/syntropy-health
#   - installs Syntropy Health there, checked against the release's SHA256SUMS, with the exact dependencies that
#     release was built with, and the `syntropy-health` command in /usr/local/bin
#   - creates a "syntropy" system account that owns the data in /var/lib/syntropy-health
#   - installs Tesseract (reads photos of paper lab reports) with your package manager, if it can
#   - starts a systemd service that restarts on failure and starts with the computer
#   - adds a daily timer that installs new releases, only if you turn on automatic updates in Settings
#
# Run it again to update: the server keeps the port and address it has. Options:
#   --server           Mac: a background service instead of the app (always the case on Linux)
#   --port N           listen on port N (default: the port it has now, or 8000)
#   --no-service       install only; don't create or start the service
#   --no-ocr           skip Tesseract
#   --source SPEC      install from SPEC instead of the latest release (a path, wheel or pip requirement; on a Mac
#                      without --server, a .dmg). `syntropy-health update` then reinstalls from SPEC too.
#   --constraints FILE with --source: install SPEC's dependencies at the versions in FILE, a requirements file (as
#                      `uv export` writes one, with hashes), where a release brings its own
#   --import DIR       Linux: copy an existing data folder (e.g. the ./data of a Docker install) before starting
#   --uninstall        remove the program and service; keeps your data (add --purge to delete it too)
#
# Everything happens in the last line's call: if a download of this script is cut short, nothing has run.
set -eu

PREFIX=/opt/syntropy-health
DATA=/var/lib/syntropy-health
ETC=/etc/syntropy-health
RUN_AS=syntropy
PORT=""
UV_VERSION=0.11.15      # the same in the Dockerfile and the workflows (tests/test_deployment_files.py)
TEAM_ID=RJ2YA2S2C8      # who signs the Mac app
REPO=https://github.com/EnriqueNeyra/syntropy-health
SOURCE=""
CONSTRAINTS=""
SERVICE=1
OCR=1
IMPORT=""
UNINSTALL=0
PURGE=0
MODE=app
APP_DIR="${SYNTROPY_APP_DIR:-/Applications}"
RELEASES=$REPO/releases/latest/download
REQUIREMENTS=Syntropy-Health-requirements.txt

while [ $# -gt 0 ]; do
  case "$1" in
    --server) MODE=server; shift;;
    --app) MODE=app; shift;;
    --port) PORT="$2"; shift 2;;
    --no-service) SERVICE=0; shift;;
    --no-ocr) OCR=0; shift;;
    --source) SOURCE="$2"; shift 2;;
    --constraints) CONSTRAINTS="$2"; shift 2;;
    --import) IMPORT="$2"; shift 2;;
    --uninstall) UNINSTALL=1; shift;;
    --purge) PURGE=1; shift;;
    -h|--help) sed -n '2,/^set -eu/p' "$0"; exit 0;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# check_sum FILE NAME SUMS: FILE is what the release's checksum list SUMS says its file NAME is.
check_sum() {
  expected="$(awk -v name="$2" '$2 == name || $2 == "*" name {print $1; exit}' "$3")"
  [ -n "$expected" ] || die "the release doesn't list a checksum for $2, so it wasn't installed. Try again later."
  [ "$(sha256_of "$1")" = "$expected" ] \
    || die "the download of $2 doesn't match the checksum the release published, so it wasn't installed. Try again; if this keeps happening, tell us: $REPO/issues"
}

work_folder() {
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
}

install_uv() {
  say "Installing uv (Python installer from astral.sh)"
  command -v curl >/dev/null 2>&1 || die "curl is needed to download uv"
  curl -fLsS -o "$WORK/uv-installer.sh" "https://astral.sh/uv/$UV_VERSION/install.sh" || die "couldn't download uv. Try again later."
  env UV_NO_MODIFY_PATH=1 "$@" sh "$WORK/uv-installer.sh"
}

# The latest release's wheel, downloaded and checked. Sets PACKAGE; LOCK, the exact dependencies that release was built
# with (none for one from before they were published); and RECORD: what `syntropy-health update` installs from later
# ("latest" follows releases). Never the source code instead: only a release build carries Syntropy's sign-in
# registrations. With --source, LOCK is --constraints, if given.
resolve_package() {
  LOCK=""
  if [ -n "$CONSTRAINTS" ]; then
    [ -n "$SOURCE" ] || die "--constraints goes with --source: a release brings its own."
    [ -f "$CONSTRAINTS" ] || die "there's no file at $CONSTRAINTS"
  fi
  if [ -n "$SOURCE" ]; then PACKAGE="$SOURCE"; RECORD="$SOURCE"; LOCK="$CONSTRAINTS"; return; fi
  RECORD=latest
  TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" 2>/dev/null | sed -n 's|.*/releases/tag/||p')"
  [ -n "$TAG" ] || die "Couldn't find the latest release on GitHub. Check the internet connection and try again."
  from="$REPO/releases/download/$TAG"
  wheel="syntropy_health-${TAG#v}-py3-none-any.whl"
  curl -fLsS -o "$WORK/SHA256SUMS" "$from/SHA256SUMS" || die "couldn't download the release's checksums. Try again later."
  curl -fLsS -o "$WORK/$wheel" "$from/$wheel" || die "couldn't download Syntropy Health. Try again later."
  check_sum "$WORK/$wheel" "$wheel" "$WORK/SHA256SUMS"
  PACKAGE="$WORK/$wheel"
  if grep -q "$REQUIREMENTS" "$WORK/SHA256SUMS"; then
    curl -fLsS -o "$WORK/$REQUIREMENTS" "$from/$REQUIREMENTS" || die "couldn't download the release's dependency list. Try again later."
    check_sum "$WORK/$REQUIREMENTS" "$REQUIREMENTS" "$WORK/SHA256SUMS"
    LOCK="$WORK/$REQUIREMENTS"
  fi
}

# install_service PROGRAM [ARGUMENTS]: keeps the service's port unless --port was given, and sets PORT to the one it uses.
install_service() {
  program="$1"; shift
  installed="$("$program" service install "$@" ${PORT:+--port "$PORT"})"
  PORT="$(printf '%s\n' "$installed" | sed -n 's|^Open http://localhost:||p')"
}

# wait_for_server WHERE_THE_LOG_IS: not a failure when it doesn't answer, because a big database can take minutes to upgrade.
wait_for_server() {
  command -v curl >/dev/null 2>&1 || return 0
  i=0
  until curl -fs "http://localhost:${PORT:-8000}/health" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 120 ]; then
      say "The server hasn't answered yet. A big database can take a few minutes to upgrade. If it stays that way, look at: $1"
      return 1
    fi
    sleep 0.5
  done
}

# ================================================================ Mac
mac_quit_app() {
  if pgrep -f "Syntropy Health.app/Contents/MacOS/Syntropy Health" >/dev/null 2>&1; then
    osascript -e 'quit app "Syntropy Health"' >/dev/null 2>&1 || true
    i=0; while pgrep -f "Syntropy Health.app/Contents/MacOS/Syntropy Health" >/dev/null 2>&1 && [ $i -lt 30 ]; do sleep 0.5; i=$((i + 1)); done
  fi
}

mac_path_hint() {
  case ":$ORIGINAL_PATH:" in *":$HOME/.local/bin:"*) return;; esac
  if ! grep -qs '.local/bin' "$HOME/.zprofile"; then
    # shellcheck disable=SC2016  # (written as it should read in the file)
    printf '\n# Added by the Syntropy Health installer: the syntropy-health command\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zprofile"
    say "Added ~/.local/bin to your PATH in ~/.zprofile (open a new terminal to use syntropy-health)"
  fi
}

mac_remove_path_hint() {
  if ! grep -qs '^# Added by the Syntropy Health installer' "$HOME/.zprofile"; then return 0; fi
  kept="$(awk '
    skip { skip = 0; if ($0 == "export PATH=\"$HOME/.local/bin:$PATH\"") next }
    /^# Added by the Syntropy Health installer/ { blank = 0; skip = 1; next }
    { if (blank) print ""; blank = 0 }
    $0 == "" { blank = 1; next }
    { print }
    END { if (blank) print "" }
  ' "$HOME/.zprofile")"
  printf '%s\n' "$kept" > "$HOME/.zprofile"
}

# The app in a downloaded release's .dmg is ours, signed by us and accepted by Gatekeeper. curl sets no quarantine flag,
# so macOS itself wouldn't look.
mac_check_signature() {
  requirement="anchor apple generic and identifier \"io.syntropyhealth.desktop\" and certificate leaf[subject.OU] = \"$TEAM_ID\""
  codesign --verify --deep --strict "-R=$requirement" "$2" 2>/dev/null \
    || die "the downloaded app isn't signed by Syntropy Labs, so it wasn't installed. Try again; if this keeps happening, tell us: $REPO/issues"
  spctl -a -t open --context context:primary-signature "$1" 2>/dev/null \
    || die "macOS doesn't accept the downloaded app, so it wasn't installed. Try again; if this keeps happening, tell us: $REPO/issues"
}

# Copied beside the old app first and swapped in only when that worked, so a failure leaves the app there was.
mac_put_app() {
  new="$APP.new"
  old="$APP.old"
  rm -rf "$new" "$old"
  ditto "$1" "$new" || { rm -rf "$new"; die "couldn't copy the app to $APP_DIR. Your installed app wasn't changed."; }
  if [ -e "$APP" ]; then mv "$APP" "$old"; fi
  mv "$new" "$APP" || { [ ! -e "$old" ] || mv "$old" "$APP"; die "couldn't put the new app in $APP_DIR. Your installed app wasn't changed."; }
  rm -rf "$old"
}

mac_main() {
  [ "$(id -u)" != 0 ] || die "on a Mac, run this without sudo: it installs for your account"
  DATA="${SYNTROPY_DATA_DIR:-$HOME/Library/Application Support/Syntropy Health}"
  APP="$APP_DIR/Syntropy Health.app"
  BIN="$HOME/.local/bin"
  SERVER_PLIST="$HOME/Library/LaunchAgents/io.syntropyhealth.server.plist"
  ORIGINAL_PATH="$PATH"
  export PATH="$BIN:$PATH"

  if [ "$UNINSTALL" = 1 ]; then
    mac_quit_app
    [ -f "$SERVER_PLIST" ] && { launchctl bootout "gui/$(id -u)" "$SERVER_PLIST" 2>/dev/null || true; rm -f "$SERVER_PLIST"; }
    rm -f "$HOME/Library/LaunchAgents/io.syntropyhealth.desktop.plist"
    rm -rf "$APP"
    if command -v uv >/dev/null 2>&1; then uv tool uninstall syntropy-health >/dev/null 2>&1 || true; fi
    rm -f "$BIN/syntropy-health"
    mac_remove_path_hint
    if [ "$PURGE" = 1 ]; then
      # The app's window keeps its own sign-in cookies, cache and window position under its bundle identifier.
      for kept in WebKit Caches HTTPStorages; do rm -rf "$HOME/Library/$kept/io.syntropyhealth.desktop"; done
      # The keys that opened the data, from the Keychain: the ones key.json names, and every other one saved for this
      # folder (each item names its folder), such as those left by a data folder deleted by hand. Other folders' stay.
      accounts="$(sed -n 's/.*"account": "\([0-9a-f]*\)".*/\1/p' "$DATA/key.json" 2>/dev/null || true)
$(grep -o '"[0-9a-f]\{32\}"' "$DATA/key.json" 2>/dev/null | tr -d '"' || true)"
      for account in $accounts; do
        security delete-generic-password -s "Syntropy Health encryption key" -a "$account" >/dev/null 2>&1 || true
      done
      for folder in "$DATA" "$(cd "$DATA" 2>/dev/null && pwd -P || true)"; do
        tries=0
        while [ -n "$folder" ] && [ "$tries" -lt 200 ] &&
          security delete-generic-password -s "Syntropy Health encryption key" -G "$folder" >/dev/null 2>&1; do
          tries=$((tries + 1))
        done
      done
      rm -rf "$DATA" "$HOME/Library/HTTPStorages/io.syntropyhealth.desktop.binarycookies" \
        "$HOME/Library/Saved Application State/io.syntropyhealth.desktop.savedState"
      defaults delete io.syntropyhealth.desktop >/dev/null 2>&1 || true
      say "Removed Syntropy Health and its data."
    else say "Removed Syntropy Health. Your data is still in $DATA (delete it with --uninstall --purge)."; fi
    exit 0
  fi

  if [ "$MODE" = app ] && [ "$(uname -m)" != arm64 ]; then
    say "The app is for Apple silicon Macs; installing the background server instead"
    MODE=server
  fi

  if [ "$MODE" = app ]; then
    TMP="$(mktemp -d)"; trap 'hdiutil detach "$TMP/mnt" -quiet 2>/dev/null; rm -rf "$TMP"' EXIT
    case "$SOURCE" in
      *.dmg) CHECKED=0
             if [ -f "$SOURCE" ]; then cp "$SOURCE" "$TMP/app.dmg"; else curl -fL --progress-bar -o "$TMP/app.dmg" "$SOURCE"; fi;;
      *) CHECKED=1
         say "Downloading the Syntropy Health app"
         curl -fL --progress-bar -o "$TMP/app.dmg" "$RELEASES/Syntropy-Health-mac-arm64.dmg" \
           || die "couldn't download the app. Try again later, or install the background server: add --server"
         curl -fLsS -o "$TMP/SHA256SUMS" "$RELEASES/SHA256SUMS" || die "couldn't download the release's checksums. Try again later."
         check_sum "$TMP/app.dmg" Syntropy-Health-mac-arm64.dmg "$TMP/SHA256SUMS";;
    esac
    hdiutil attach -nobrowse -readonly -quiet -mountpoint "$TMP/mnt" "$TMP/app.dmg" || die "couldn't open the downloaded app"
    if [ "$CHECKED" = 1 ]; then mac_check_signature "$TMP/app.dmg" "$TMP/mnt/Syntropy Health.app"; fi
    [ -w "$APP_DIR" ] || { APP_DIR="$HOME/Applications"; APP="$APP_DIR/Syntropy Health.app"; mkdir -p "$APP_DIR"; }
    mac_quit_app
    say "Installing $APP"
    mac_put_app "$TMP/mnt/Syntropy Health.app"
    hdiutil detach "$TMP/mnt" -quiet || true
    if [ -f "$SERVER_PLIST" ]; then
      say "Stopping the background server installed earlier: the app runs the server now, with the same data"
      launchctl bootout "gui/$(id -u)" "$SERVER_PLIST" 2>/dev/null || true
      rm -f "$SERVER_PLIST"
    fi
    # The command line runs the app's own program, without a window.
    mkdir -p "$BIN"
    printf '#!/bin/sh\nexec "%s/Contents/MacOS/Syntropy Health" "$@"\n' "$APP" > "$BIN/syntropy-health"
    chmod 755 "$BIN/syntropy-health"
    mac_path_hint
    open "$APP"
    echo
    say "Syntropy Health $("$APP/Contents/MacOS/Syntropy Health" version) is installed and open."
    echo "    It lives in the menu bar; choose Let Your iPhone and Other Devices Connect there, or in Settings."
    echo "    Data:     $DATA   (back up with: syntropy-health backup /a/folder/on/another/drive)"
    echo "    Update:   run this installer again"
    exit 0
  fi

  # ---- --server: a background service for this account (launchd), no app
  if pgrep -f "Syntropy Health.app/Contents/MacOS/Syntropy Health" >/dev/null 2>&1; then
    die "the Syntropy Health app is running, and it already runs the server. Quit it (and remove it from Applications) first"
  fi
  work_folder
  command -v uv >/dev/null 2>&1 || install_uv >/dev/null
  say "Installing Syntropy Health"
  resolve_package
  uv tool install --quiet --force --python 3.12 ${LOCK:+--constraints "$LOCK"} "$PACKAGE"
  printf '%s\n' "$RECORD" > "$(uv tool dir)/syntropy-health/syntropy-source.txt"
  command -v uv > "$(uv tool dir)/syntropy-health/syntropy-uv.txt"
  mac_path_hint
  if [ "$OCR" = 1 ] && ! command -v tesseract >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      say "Installing Tesseract with Homebrew, to read photos of paper lab reports"
      brew install --quiet tesseract >/dev/null 2>&1 || say "Couldn't install Tesseract; photos of lab reports won't be read"
    else
      say "Photos of paper lab reports need Tesseract: install Homebrew, then brew install tesseract"
    fi
  fi
  if [ "$SERVICE" = 1 ]; then
    say "Starting the background service"
    install_service syntropy-health
    if wait_for_server "$DATA/server.log"; then open "http://localhost:$PORT" 2>/dev/null || true; fi
  fi
  PORT="${PORT:-8000}"
  echo
  say "Syntropy Health $(syntropy-health version) is installed."
  echo "    Open http://localhost:$PORT to finish setup. From your other devices: http://syntropyhealth.local:$PORT"
  echo "    Data:     $DATA   (back up with: syntropy-health backup /a/folder/on/another/drive)"
  echo "    Update:   syntropy-health update   (or run this installer again)"
  echo "    Logs:     $DATA/server.log"
  exit 0
}

has_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }

linux_uninstall() {
  if has_systemd && [ -f /etc/systemd/system/syntropy-health.service ]; then
    systemctl disable --now syntropy-health syntropy-health-update.timer 2>/dev/null || true
    rm -f /etc/systemd/system/syntropy-health.service /etc/systemd/system/syntropy-health-update.service \
      /etc/systemd/system/syntropy-health-update.timer
    systemctl daemon-reload
  fi
  rm -rf "$PREFIX" /usr/local/bin/syntropy-health
  if [ "$PURGE" = 1 ]; then
    # uv only if it wasn't here before, and the account only if it's the one made below, whose home is the data folder.
    if [ -f "$ETC/installed-uv" ]; then rm -f /usr/local/bin/uv /usr/local/bin/uvx; fi
    rm -rf "$DATA" "$ETC"
    if [ "$(awk -F: -v name="$RUN_AS" '$1 == name {print $6}' /etc/passwd)" = "$DATA" ]; then
      if command -v userdel >/dev/null 2>&1; then userdel "$RUN_AS"; else deluser "$RUN_AS"; fi
    fi
    say "Removed Syntropy Health and its data."
  else
    say "Removed Syntropy Health. Your data is still in $DATA (delete it with --uninstall --purge)."
  fi
}

linux_main() {
  [ "$(id -u)" = 0 ] || die "run as root, for example: curl -fsSL …/install.sh | sudo sh"
  [ "$(uname -s)" = Linux ] || die "this installer is for Mac and Linux. On Windows, use install.ps1."

  if [ "$UNINSTALL" = 1 ]; then
    linux_uninstall
    exit 0
  fi

  # ---------------------------------------------------------------- uv and Python
  export PATH="/usr/local/bin:$PATH"
  work_folder
  mkdir -p "$ETC"
  if ! command -v uv >/dev/null 2>&1; then
    install_uv UV_INSTALL_DIR=/usr/local/bin
    : > "$ETC/installed-uv"
  fi

  # Python lives inside $PREFIX so the service account can read it and nothing else on the system changes.
  export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
  if [ ! -x "$PREFIX/venv/bin/python" ]; then
    say "Creating $PREFIX with Python 3.12"
    mkdir -p "$PREFIX"
    uv venv --quiet --python 3.12 "$PREFIX/venv"
  fi

  say "Installing Syntropy Health"
  resolve_package
  uv pip install --quiet --python "$PREFIX/venv/bin/python" --upgrade ${LOCK:+-c "$LOCK"} "$PACKAGE"
  printf '%s\n' "$RECORD" > "$PREFIX/venv/syntropy-source.txt"
  command -v uv > "$PREFIX/venv/syntropy-uv.txt"
  chmod -R a+rX "$PREFIX"

  # ---------------------------------------------------------------- OCR (optional)
  if [ "$OCR" = 1 ] && ! command -v tesseract >/dev/null 2>&1; then
    say "Installing Tesseract, to read photos of paper lab reports"
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y -q tesseract-ocr >/dev/null || say "Couldn't install Tesseract; photos of lab reports won't be read"
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q tesseract >/dev/null || true
    elif command -v pacman >/dev/null 2>&1; then pacman -S --noconfirm --needed tesseract tesseract-data-eng >/dev/null || true
    elif command -v apk >/dev/null 2>&1; then apk add -q tesseract-ocr >/dev/null || true
    elif command -v zypper >/dev/null 2>&1; then zypper -q install -y tesseract-ocr >/dev/null || true
    fi
  fi

  # ---------------------------------------------------------------- account, data, settings
  if ! id "$RUN_AS" >/dev/null 2>&1; then
    say "Creating the $RUN_AS account"
    if command -v useradd >/dev/null 2>&1; then
      useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$RUN_AS"
    else
      adduser -S -D -H -h "$DATA" -s /sbin/nologin "$RUN_AS"
    fi
  fi
  mkdir -p "$DATA"
  if [ -n "$IMPORT" ]; then
    [ -f "$IMPORT/syntropy.db" ] || die "$IMPORT has no syntropy.db"
    [ ! -f "$DATA/syntropy.db" ] || die "$DATA already has a database; move it aside first"
    say "Copying your data from $IMPORT"
    if has_systemd; then systemctl stop syntropy-health 2>/dev/null || true; fi
    cp -a "$IMPORT/." "$DATA/"
  fi
  chown -R "$RUN_AS:" "$DATA"
  chmod 700 "$DATA"

  if [ ! -f "$ETC/env" ]; then
    TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)"
    cat > "$ETC/env" <<EOF
# Settings for the Syntropy Health service (restart after changing: sudo systemctl restart syntropy-health).
TZ=${TZ_NAME:-UTC}
# Behind Tailscale Serve, Caddy, nginx or Cloudflare Tunnel on this machine, keep this; list other proxies' addresses.
FORWARDED_ALLOW_IPS=127.0.0.1
EOF
    chmod 640 "$ETC/env"
    chgrp "$RUN_AS" "$ETC/env" 2>/dev/null || true
  fi

  # The command everyone uses. Commands that touch the data run as the service account, so files stay owned by it.
  cat > /usr/local/bin/syntropy-health <<EOF
#!/bin/sh
export SYNTROPY_DATA_DIR="$DATA"
[ -r "$ETC/env" ] && set -a && . "$ETC/env" && set +a
case "\${1:-}" in
  serve|mcp|info|accounts|key|dev)
    if [ "\$(id -u)" = 0 ]; then
      exec runuser -u "$RUN_AS" -- "$PREFIX/venv/bin/syntropy-health" "\$@"
    fi;;
  backup|restore)
    # Root reads and writes the folder named here; the service's account reads and writes the data.
    if [ "\$(id -u)" = 0 ]; then
      exec "$PREFIX/venv/bin/syntropy-health" --service-account "$RUN_AS" "\$@"
    fi;;
esac
exec "$PREFIX/venv/bin/syntropy-health" "\$@"
EOF
  chmod 755 /usr/local/bin/syntropy-health

  # ---------------------------------------------------------------- service
  if [ "$SERVICE" = 1 ]; then
    if has_systemd; then
      say "Starting the syntropy-health service"
      export SYNTROPY_DATA_DIR="$DATA"
      install_service "$PREFIX/venv/bin/syntropy-health" --user "$RUN_AS"
      systemctl restart syntropy-health
      wait_for_server "journalctl -u syntropy-health -n 50" || true
      # Daily: if the owner turned on automatic updates (Settings → General → Updates) and a new release is out, install
      # it. The check runs as the service account, which owns the database; installing needs root.
      cat > /etc/systemd/system/syntropy-health-update.service <<UNIT
[Unit]
Description=Install a new Syntropy Health release, if automatic updates are on
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'if runuser -u $RUN_AS -- /usr/local/bin/syntropy-health update --due; then exec /usr/local/bin/syntropy-health update; fi'
UNIT
      cat > /etc/systemd/system/syntropy-health-update.timer <<UNIT
[Unit]
Description=Check for Syntropy Health updates daily

[Timer]
OnCalendar=*-*-* 03:00
RandomizedDelaySec=2h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
      systemctl daemon-reload
      systemctl enable --now syntropy-health-update.timer >/dev/null 2>&1 || true
    else
      say "No systemd here: start it with your init system, running: syntropy-health serve --port ${PORT:-8000}"
    fi
  fi

  PORT="${PORT:-8000}"
  VERSION="$("$PREFIX/venv/bin/syntropy-health" version)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  say "Syntropy Health $VERSION is installed."
  if grep -qs -- '--host 127.0.0.1' /etc/systemd/system/syntropy-health.service; then
    echo "    Open http://localhost:$PORT on this computer to finish setup. It's set to answer only here, so other devices can't connect."
  else
    echo "    Open http://${IP:-this-computer}:$PORT from any device on your network to finish setup."
  fi
  echo "    Data:    $DATA (back up with: sudo syntropy-health backup /a/folder/on/another/drive)"
  echo "    Update:  sudo syntropy-health update   (or run this installer again)"
  echo "    Logs:    journalctl -u syntropy-health -f"
}

main() {
  if [ "$(uname -s)" = Darwin ]; then mac_main; fi
  linux_main
}

main
