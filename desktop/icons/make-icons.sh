#!/bin/sh
# Renders the app icons from app/static/img/icon.svg with a Chromium browser (Chrome, Brave or Edge), on a Mac.
#   icon.icns     the Mac app: the dark tile (white S), on Apple's icon grid: an 824-pixel tile on a 1024 canvas with
#                 a soft shadow, so it sits at the same size as other apps in the Dock. Dark because macOS darkens
#                 light icons itself in its dark and tinted Dock styles, which turned the black S nearly invisible.
#   icon.ico      Windows, full bleed, as Windows draws icons
#   icon-256.png  the window and Windows tray icon
set -eu
cd "$(dirname "$0")"
SVG=../../app/static/img/icon.svg
CHROME="${CHROME:-}"
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
         "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"; do
  [ -z "$CHROME" ] && [ -x "$c" ] && CHROME="$c"
done
[ -n "$CHROME" ] || { echo "Set CHROME to a Chromium-based browser" >&2; exit 1; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# The light (white) tile drops the SVG's dark-mode style; the dark tile applies it whatever the system setting.
sed 's|<style>[^<]*</style>||' "$SVG" > "$TMP/icon.svg"
sed -E 's|<style>@media \(prefers-color-scheme: dark\)\{([^}]*\}[^}]*\})\}</style>|<style>\1</style>|' "$SVG" > "$TMP/icon-dark.svg"
grep -q 'bg-dark)}' "$TMP/icon-dark.svg" || { echo "Couldn't find the dark style in $SVG" >&2; exit 1; }

render() {  # render <html body> <output.png>
  printf '<!doctype html><html><body style="margin:0;background:transparent">%s</body></html>' "$1" > "$TMP/page.html"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --default-background-color=00000000 --window-size=1024,1024 --screenshot="$2" "file://$TMP/page.html" >/dev/null 2>&1
}

render '<img src="icon-dark.svg" style="position:absolute;left:100px;top:92px;width:824px;height:824px;filter:drop-shadow(0 10px 14px rgba(0,0,0,.3))">' "$TMP/mac.png"
render '<img src="icon.svg" style="width:1024px;height:1024px">' "$TMP/full.png"

mkdir "$TMP/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/mac.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/mac.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/icon.iconset" -o icon.icns
sips -z 256 256 "$TMP/full.png" --out icon-256.png >/dev/null
python3 -c "from PIL import Image; Image.open('$TMP/full.png').save('icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
echo "Wrote icon.icns, icon.ico and icon-256.png"
