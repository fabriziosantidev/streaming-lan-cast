# Packaging & signing: Streaming LAN Cast (Firefox AMO & Chrome Web Store)

The extension is plain HTML/CSS/JS/JSON, no bundler and nothing minified. The code under
`extension/` is shared across browsers; it uses the bundled `webextension-polyfill` so `browser.*`
works in both. The only per-browser difference is the manifest's background key (Firefox uses an
event page, Chrome a service worker), so a small build step (`build/build-extension.sh`) copies the
shared source and swaps in the right manifest. Because nothing is bundled or minified, neither store
requires a separate source-code upload.

## 0. Prerequisites
- Node.js LTS (https://nodejs.org).
- `web-ext` (Mozilla's official tool):
  ```
  npm install -g web-ext
  ```
  (or run via `npx web-ext ...` without installing globally)

## 1. Lint (the real gate, do this first)
```
cd streaming-lan-cast
web-ext lint -s extension
```
Fix any errors. Warnings about the loopback host permission or optional `<all_urls>` are expected,
not bugs; explain them in your "Notes for reviewers" at submission time.

## 2. Build the packages (.zip)
```
build/build-extension.sh            # needs only python3 and zip
```
Produces, under `dist/`: `streaming-lan-cast-firefox-<v>.zip` (event-page background, for AMO) and
`streaming-lan-cast-chrome-<v>.zip` (service-worker background, for the Chrome Web Store / Edge), plus
unpacked `dist/firefox/` and `dist/chrome/` for loading during development. Upload the matching zip to
each store. (`web-ext build -s extension -a dist` still works for a Firefox-only zip if you prefer.)

## 3. Get it signed / published

### Option A: Listed on addons.mozilla.org (public, recommended)
1. Create an account at https://addons.mozilla.org and go to the Developer Hub.
2. Click "Submit a New Add-on", upload `dist/...zip`, then choose "On this site" (listed).
3. Fill in the store listing (name/summary/description), add your "Notes for reviewers" explaining the
   local helper and the opt-in `<all_urls>` permission, and set the Privacy Policy to your hosted URL (see §5).
4. AMO reviews (manual review is likely because of the optional `<all_urls>`; that's normal here),
   then signs and hosts it. Users install and auto-update from AMO.

CLI alternative (same result): create API credentials at AMO under *Manage API Keys*, then:
```
web-ext sign -s extension --channel listed --api-key <JWT_ISSUER> --api-secret <JWT_SECRET>
```

### Option B: Unlisted (self-distribution / sideload)
For distributing the `.xpi` yourself (your own site), still signed by Mozilla so it installs on
release Firefox:
```
web-ext sign -s extension --channel unlisted --api-key <KEY> --api-secret <SECRET>
```
Returns a signed `.xpi`. For auto-updates, add `browser_specific_settings.gecko.update_url` to the
manifest pointing at an `updates.json` you host.

### Chrome Web Store (Chrome / Edge)
1. Register once at the Chrome Web Store Developer Dashboard
   (https://chrome.google.com/webstore/devconsole) and pay the one-time developer fee.
2. Click "Add new item" and upload `dist/streaming-lan-cast-chrome-<v>.zip`.
3. Fill in the store listing, add the same "Notes for reviewers" about the local helper and the opt-in
   `<all_urls>` permission, and set the Privacy Policy to your hosted URL (see §5).
4. Submit for review. Broad host permissions usually mean a longer review; once it passes, Google
   publishes it and users install and auto-update from the store. (Edge reuses the same zip on the
   separate Microsoft Edge Add-ons dashboard.)

## 4. Before the first submission: decisions to lock in
- **Extension id** (`browser_specific_settings.gecko.id`): set to
  `streaming-lan-cast@fabriziosantidev.github.io`. It is permanent for the listing and cannot be changed
  later without a new listing.
- **`version`**: bump it (`manifest.json`) for every *extension* update you submit to the stores. A
  helper-only release no longer needs an extension bump (see "Versioning and the update nudge" below).
- **`homepage_url`** (optional): add it once you have a repo/landing page.

## 5. Hosting the privacy policy
AMO needs a URL (or pasted text) and the Chrome Web Store needs a hosted URL. `PRIVACY.md` is ready. Easiest options:
- Push this repo to GitHub and use the raw URL of `PRIVACY.md`, or
- Enable GitHub Pages and link the rendered page, or
- Paste it into a public gist and use its URL.

## 6. The companion helper (separate from the extension)
The extension does nothing without the local helper (`python helper/streaming-lan-cast-helper.py --serve`). For a real
launch, ship a per-OS helper installer and have the "helper not detected" state link to it:
- **Windows** (built): Inno Setup wizard, `installer/windows/streaming-lan-cast.iss` over a PyInstaller bundle.
  Per-user install, autostart via HKCU Run, token shown on the final page. Sign with Azure Trusted Signing.
  Build the bundle the wizard packages (run from `streaming-lan-cast/`):
  ```
  pyinstaller build/streaming-lan-cast-helper.spec --workpath build/work --distpath dist
  ```
  `--workpath build/work` keeps the intermediates inside the gitignored `build/work/`.
- **Linux + macOS** (built): `installer/unix/install.sh`, per-user, no sudo, with a `curl ... | bash`
  one-liner. Creates an isolated Python venv (`~/.local/share/streaming-lan-cast/venv`, pip-installs
  streamlink, bootstrapping pip via get-pip.py when ensurepip is absent), then sets up autostart, a
  systemd user service on Linux (`~/.config/systemd/user/streaming-lan-cast.service`) or a launchd
  LaunchAgent on macOS (`~/Library/LaunchAgents/com.fabriziosantidev.streaming-lan-cast.plist`), and prints
  the pairing token. `installer/unix/uninstall.sh` reverses it. Requires Python 3.10+ (streamlink's floor).
  A drag-to-install `.dmg`/`.pkg` for macOS would need an Apple Developer ID and notarization; the script
  (run from Terminal) avoids Gatekeeper and needs no Apple account.

You can submit the extension first with text install instructions while you polish the installers.

### Linux & macOS: quick use
```
installer/unix/install.sh      # build venv + autostart (systemd/launchd), print the token
installer/unix/uninstall.sh    # stop + remove everything (service, venv, token)
```
Manage it with `systemctl --user {status,restart,stop} streaming-lan-cast.service` (Linux) or
`launchctl {list,unload,load}` on the LaunchAgent (macOS).

## Versioning and the update nudge

There are three version numbers, and they are no longer bumped in lockstep:

- **`HELPER_VERSION`** (`helper/streaming-lan-cast-helper.py`) and **`MyAppVersion`** (the Inno `.iss`)
  move together: they are what a helper/installer release carries.
- **`version`** (`extension/manifest.json`) is independent. Bump it only when the extension's own code
  changes and you resubmit it to the stores.

`docs/version.json` is the single source of truth for the latest helper:

```
{ "helper": "0.5.6", "min_extension": "0.4.0" }
```

The running helper fetches it in the background and reports `latest` + `min_ext` on `/ping`. The popup
prompts "update your helper" only when the running helper is older than `helper` AND this extension is
at least `min_extension`. So:

- **Helper-only release**: bump `HELPER_VERSION` + the `.iss`, build the installer, cut the GitHub
  release, and set `helper` in `docs/version.json` to the new version. Leave `min_extension` and the
  extension alone. Existing users get nudged with no store resubmit.
- **Release that needs a newer extension**: also bump the extension `version`, resubmit it to the
  stores, and raise `min_extension` to that version. The nudge stays silent until a user's extension
  reaches `min_extension`, so nobody is told to install a helper their extension can't drive.

A helper too old to report `latest` falls back to comparing against the extension's own version, so
bumping the extension still nudges those users.

## Quick reference
```
web-ext lint  -s extension                 # check
web-ext build -s extension -a dist         # zip
web-ext sign  -s extension --channel listed --api-key K --api-secret S   # sign/submit
web-ext run   -s extension                 # launch a temp Firefox with it loaded (dev)
```

For Chrome / Edge there is no CLI loader: open `chrome://extensions`, enable Developer mode, and use
"Load unpacked" on `dist/chrome/`.
