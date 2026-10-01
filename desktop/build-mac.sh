#!/usr/bin/env bash
# Builds "Syntropy Health.app" and a .dmg in desktop/dist.
#   desktop/build-mac.sh                      ad-hoc signed: runs on this Mac
#   SIGN_ID="Developer ID Application: …" NOTARY_PROFILE=syntropy desktop/build-mac.sh
#                                             signed and notarized, for anyone to download
# NOTARY_PROFILE is a keychain profile made once with: xcrun notarytool store-credentials syntropy
# (or set NOTARY_APPLE_ID, NOTARY_TEAM_ID and NOTARY_PASSWORD, an app-specific password, as CI does)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
OUT=desktop/dist
VERSION="$("$PY" -c 'from app.core.config import APP_VERSION; print(APP_VERSION)')"
ARCH="$(uname -m)"

"$PY" -m PyInstaller --noconfirm --clean --distpath "$OUT" --workpath desktop/build desktop/syntropy-health.spec
APP="$OUT/Syntropy Health.app"

if [ -n "${SIGN_ID:-}" ]; then
  codesign --force --deep --options runtime --timestamp --entitlements desktop/entitlements.plist --sign "$SIGN_ID" "$APP"
else
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

DMG="$OUT/Syntropy-Health-$VERSION-mac-$ARCH.dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Syntropy Health" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
if [ -n "${SIGN_ID:-}" ]; then
  codesign --sign "$SIGN_ID" --timestamp "$DMG"
  if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
  elif [ -n "${NOTARY_APPLE_ID:-}" ]; then
    xcrun notarytool submit "$DMG" --apple-id "$NOTARY_APPLE_ID" --team-id "$NOTARY_TEAM_ID" --password "$NOTARY_PASSWORD" --wait
    xcrun stapler staple "$DMG"
  fi
fi
echo "Built $DMG"
