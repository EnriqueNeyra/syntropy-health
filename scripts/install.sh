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
#   - installs Syntropy Health there, and the `syntropy-health` command in /usr/local/bin
#   - creates a "syntropy" system account that owns the data in /var/lib/syntropy-health
#   - installs Tesseract (reads photos of paper lab reports) with your package manager, if it can
#   - starts a systemd service that restarts on failure and starts with the computer
#   - adds a daily timer that installs new releases, only if you turn on automatic updates in Settings
#
# Run it again to update. Options:
#   --server           Mac: a background service instead of the app (always the case on Linux)
#   --port N           listen on port N (default 8000)
#   --no-service       install only; don't create or start the service
#   --no-ocr           skip Tesseract
#   --source SPEC      install from SPEC instead of the latest release (a path, wheel or pip requirement; on a Mac
#                      without --server, a .dmg). `syntropy-health update` then reinstalls from SPEC too.
#   --import DIR       Linux: copy an existing data folder (e.g. the ./data of a Docker install) before starting
#   --uninstall        remove the program and service; keeps your data (add --purge to delete it too)
set -eu

PREFIX=/opt/syntropy-health
DATA=/var/lib/syntropy-health
ETC=/etc/syntropy-health
RUN_AS=syntropy
PORT=8000
REPO=https://github.com/EnriqueNeyra/syntropy-health
SOURCE=""
SERVICE=1
OCR=1
IMPORT=""
UNINSTALL=0
PURGE=0
MODE=app
APP_DIR="${SYNTROPY_APP_DIR:-/Applications}"
RELEASES=$REPO/releases/latest/download

while [ $# -gt 0 ]; do
  case "$1" in
    --server) MODE=server; shift;;
    --app) MODE=app; shift;;
    --port) PORT="$2"; shift 2;;
    --no-service) SERVICE=0; shift;;
    --no-ocr) OCR=0; shift;;
    --source) SOURCE="$2"; shift 2;;
    --import) IMPORT="$2"; shift 2;;
    --uninstall) UNINSTALL=1; shift;;
    --purge) PURGE=1; shift;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# The latest release's wheel (or, before the first release, the latest code on main). Sets PACKAGE, and RECORD: what
# `syntropy-health update` installs from later ("latest" follows releases).
resolve_package() {
  if [ -n "$SOURCE" ]; then PACKAGE="$SOURCE"; RECORD="$SOURCE"; return; fi
  RECORD=latest
  TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" 2>/dev/null | sed -n 's|.*/releases/tag/||p')"
  if [ -n "$TAG" ]; then
    PACKAGE="syntropy-health @ $REPO/releases/download/$TAG/syntropy_health-${TAG#v}-py3-none-any.whl"
  else
    PACKAGE="syntropy-health @ $REPO/archive/refs/heads/main.tar.gz"
  fi
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
    printf '\n# Added by the Syntropy Health installer: the syntropy-health command\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zprofile"
    say "Added ~/.local/bin to your PATH in ~/.zprofile (open a new terminal to use syntropy-health)"
  fi
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
    command -v uv >/dev/null 2>&1 && uv tool uninstall syntropy-health >/dev/null 2>&1 || true
    rm -f "$BIN/syntropy-health"
    if [ "$PURGE" = 1 ]; then rm -rf "$DATA"; say "Removed Syntropy Health and its data."
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
      *.dmg) [ -f "$SOURCE" ] && cp "$SOURCE" "$TMP/app.dmg" || curl -fL --progress-bar -o "$TMP/app.dmg" "$SOURCE";;
      *) say "Downloading the Syntropy Health app"
         curl -fL --progress-bar -o "$TMP/app.dmg" "$RELEASES/Syntropy-Health-mac-arm64.dmg" \
           || die "couldn't download the app. Try again later, or install the background server: add --server";;
    esac
    hdiutil attach -nobrowse -readonly -quiet -mountpoint "$TMP/mnt" "$TMP/app.dmg" || die "couldn't open the downloaded app"
    [ -w "$APP_DIR" ] || { APP_DIR="$HOME/Applications"; APP="$APP_DIR/Syntropy Health.app"; mkdir -p "$APP_DIR"; }
    mac_quit_app
    say "Installing $APP"
    rm -rf "$APP"
    ditto "$TMP/mnt/Syntropy Health.app" "$APP"
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
    echo "    Data:     $DATA   (back up with: syntropy-health backup ~/Desktop)"
    echo "    Update:   run this installer again"
    exit 0
  fi

  # ---- --server: a background service for this account (launchd), no app
  if pgrep -f "Syntropy Health.app/Contents/MacOS/Syntropy Health" >/dev/null 2>&1; then
    die "the Syntropy Health app is running, and it already runs the server. Quit it (and remove it from Applications) first"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (Python installer from astral.sh)"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  fi
  say "Installing Syntropy Health"
  resolve_package
  uv tool install --quiet --force --python 3.12 "$PACKAGE"
  printf '%s\n' "$RECORD" > "$(uv tool dir)/syntropy-health/syntropy-source.txt"
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
    syntropy-health service install --port "$PORT" >/dev/null
    i=0; until curl -fs "http://localhost:$PORT/health" >/dev/null 2>&1 || [ $i -ge 60 ]; do sleep 0.5; i=$((i + 1)); done
    open "http://localhost:$PORT" 2>/dev/null || true
  fi
  echo
  say "Syntropy Health $(syntropy-health version) is installed."
  echo "    Open http://localhost:$PORT to finish setup. From your other devices: http://syntropyhealth.local:$PORT"
  echo "    Data:     $DATA   (back up with: syntropy-health backup ~/Desktop)"
  echo "    Update:   syntropy-health update   (or run this installer again)"
  echo "    Logs:     $DATA/server.log"
  exit 0
}

[ "$(uname -s)" = Darwin ] && mac_main

# ================================================================ Linux
[ "$(id -u)" = 0 ] || die "run as root, for example: curl -fsSL …/install.sh | sudo sh"
[ "$(uname -s)" = Linux ] || die "this installer is for Mac and Linux. On Windows, use install.ps1."

has_systemd() { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }

if [ "$UNINSTALL" = 1 ]; then
  if has_systemd && [ -f /etc/systemd/system/syntropy-health.service ]; then
    systemctl disable --now syntropy-health syntropy-health-update.timer 2>/dev/null || true
    rm -f /etc/systemd/system/syntropy-health.service /etc/systemd/system/syntropy-health-update.service \
      /etc/systemd/system/syntropy-health-update.timer
    systemctl daemon-reload
  fi
  rm -rf "$PREFIX" /usr/local/bin/syntropy-health
  if [ "$PURGE" = 1 ]; then
    rm -rf "$DATA" "$ETC"
    say "Removed Syntropy Health and its data."
  else
    say "Removed Syntropy Health. Your data is still in $DATA (delete it with --uninstall --purge)."
  fi
  exit 0
fi

# ---------------------------------------------------------------- uv and Python
export PATH="/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python installer from astral.sh)"
  command -v curl >/dev/null 2>&1 || die "curl is needed to download uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
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
uv pip install --quiet --python "$PREFIX/venv/bin/python" --upgrade "$PACKAGE"
printf '%s\n' "$RECORD" > "$PREFIX/venv/syntropy-source.txt"
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
mkdir -p "$DATA" "$ETC"
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
  serve|mcp|backup|info)
    if [ "\$(id -u)" = 0 ]; then
      exec runuser -u "$RUN_AS" -- "$PREFIX/venv/bin/syntropy-health" "\$@"
    fi;;
esac
exec "$PREFIX/venv/bin/syntropy-health" "\$@"
EOF
chmod 755 /usr/local/bin/syntropy-health

# ---------------------------------------------------------------- service
if [ "$SERVICE" = 1 ]; then
  if has_systemd; then
    say "Starting the syntropy-health service"
    SYNTROPY_DATA_DIR="$DATA" "$PREFIX/venv/bin/syntropy-health" service install --user "$RUN_AS" --port "$PORT" >/dev/null
    systemctl restart syntropy-health
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
    say "No systemd here: start it with your init system, running: syntropy-health serve --port $PORT"
  fi
fi

VERSION="$("$PREFIX/venv/bin/syntropy-health" version)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Syntropy Health $VERSION is installed."
echo "    Open http://${IP:-this-computer}:$PORT from any device on your network to finish setup."
echo "    Data:    $DATA (back up with: sudo syntropy-health backup /some/folder)"
echo "    Update:  sudo syntropy-health update   (or run this installer again)"
echo "    Logs:    journalctl -u syntropy-health -f"
