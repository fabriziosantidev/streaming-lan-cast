# Third-party software

Streaming LAN Cast itself is licensed under the PolyForm Noncommercial License 1.0.0 (see `LICENSE`):
it is free for any noncommercial use, and commercial use (including selling it) is not permitted. The only third-party code committed to the repo is the extension's `browser-polyfill.js` (see Extension
dependencies). The helper's dependencies are not committed: on Linux and macOS the user installs them into an isolated
virtual environment (`pip install streamlink pychromecast`); the Windows PyInstaller `.exe`
bundles streamlink, pychromecast, and zeroconf. Each dependency keeps its own license.

## Helper runtime dependencies

### streamlink
- Role: the helper invokes `streamlink` as a subprocess (`python -m streamlink ...`) to extract the
  live stream and to enumerate qualities.
- License: BSD 2-Clause "Simplified" License.
- Project: https://github.com/streamlink/streamlink

### pychromecast
- Role: the helper uses pychromecast to discover and control Google Cast devices (Chromecast, Android
  TV, Google TV). It connects over TLS on port 8009 and tells the device's Default Media Receiver to
  play the stream.
- License: MIT License.
- Project: https://github.com/home-assistant-libs/pychromecast

### zeroconf
- Role: provides the mDNS (`_googlecast._tcp`) discovery that finds cast devices on the LAN. It is
  pulled in as a dependency of pychromecast.
- License: LGPL-2.1-or-later.
- Project: https://github.com/python-zeroconf/python-zeroconf

### Python standard library
- Role: the helper is built entirely on the Python standard library otherwise (http.server, socket,
  urllib, subprocess, secrets, etc.). License: PSF License.

## Extension dependencies

### webextension-polyfill
- Role: a small compatibility shim that exposes the standard promise-based `browser.*` API in Chromium
  browsers (Chrome, Edge, Brave), which natively provide only the callback-based `chrome.*` API, so the
  extension can share one codebase across Firefox and Chrome. It ships as `extension/browser-polyfill.js`,
  loaded by a script tag in the popup and options pages and by `importScripts` in the Chrome service worker.
- Version: 0.12.0 (unmodified).
- License: Mozilla Public License 2.0.
- Project: https://github.com/mozilla/webextension-polyfill

The rest of the WebExtension is plain HTML/CSS/JS, not bundled or minified.
