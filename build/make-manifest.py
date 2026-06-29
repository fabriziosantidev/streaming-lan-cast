#!/usr/bin/env python3
"""Generate a per-browser MV3 manifest from the shared (Firefox) manifest.

The only cross-browser manifest differences live here, so the rest of extension/
stays a single shared codebase:

  - background: Firefox uses an event page (background.scripts); Chrome requires a
    service worker (background.service_worker). The webextension-polyfill is loaded
    via the scripts array on Firefox and via importScripts() on Chrome.
  - browser_specific_settings is Firefox-only; Chrome ignores it, so it is dropped
    from the Chrome build and minimum_chrome_version is declared instead.

Usage: make-manifest.py <firefox|chrome> <src-manifest> <dst-manifest>
"""
import json
import sys

target, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
manifest = json.load(open(src, encoding="utf-8"))

if target == "firefox":
    manifest["background"] = {"scripts": ["browser-polyfill.js", "background.js"]}
elif target == "chrome":
    manifest["background"] = {"service_worker": "background.js"}
    manifest.pop("browser_specific_settings", None)
    manifest["minimum_chrome_version"] = "102"
else:
    sys.exit(f"unknown target: {target!r} (expected 'firefox' or 'chrome')")

with open(dst, "w", encoding="utf-8") as out:
    json.dump(manifest, out, indent=2, ensure_ascii=False)
    out.write("\n")
