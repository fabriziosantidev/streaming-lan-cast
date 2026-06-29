#!/usr/bin/env bash
# Build the per-browser extension packages from the shared extension/ source.
#
#   build/build-extension.sh             # builds both targets
#   build/build-extension.sh firefox     # builds one
#
# Output (under dist/):
#   dist/firefox/                          unpacked, for about:debugging "Load Temporary Add-on"
#   dist/chrome/                           unpacked, for chrome://extensions "Load unpacked"
#   dist/streaming-lan-cast-firefox-<v>.zip
#   dist/streaming-lan-cast-chrome-<v>.zip
#
# The code under extension/ is shared. It uses the webextension-polyfill so browser.* works in
# both browsers; only the manifest's background key differs (see build/make-manifest.py).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/extension"
DIST="$ROOT/dist"
VER="$(python3 -c "import json;print(json.load(open('$SRC/manifest.json'))['version'])")"

build() {
  local target="$1"
  local out="$DIST/$target"
  rm -rf "$out"; mkdir -p "$out"
  cp -r "$SRC/." "$out/"
  find "$out" -name '.DS_Store' -delete
  rm -f "$out/manifest.json"
  python3 "$ROOT/build/make-manifest.py" "$target" "$SRC/manifest.json" "$out/manifest.json"
  python3 -c "import json;json.load(open('$out/manifest.json'))"   # fail loudly on a bad manifest
  local zip="$DIST/streaming-lan-cast-$target-$VER.zip"
  rm -f "$zip"
  ( cd "$out" && zip -rqX "$zip" . )
  echo "  $target  ->  dist/$target/  +  $(basename "$zip")  ($(du -h "$zip" | cut -f1))"
}

targets=("$@"); [ ${#targets[@]} -eq 0 ] && targets=(firefox chrome)
echo "Building Streaming LAN Cast $VER"
for t in "${targets[@]}"; do build "$t"; done
echo "Done."
