# Packaging & signing: Streaming LAN Cast (Firefox AMO)

The extension is plain HTML/CSS/JS/JSON: no build step, no bundler, not minified.
"Packaging" is just zipping `extension/`. That also means AMO does not require a separate
source-code upload (that's only for minified or bundled add-ons).

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

## 2. Build the package (.zip)
```
web-ext build -s extension -a dist
```
Produces `dist/streaming_lan_cast-0.2.0.zip`. That zip is what you upload to AMO.

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

## 4. Before the first submission: decisions to lock in
- **Extension id** (`browser_specific_settings.gecko.id`): set to
  `streaming-lan-cast@fabriziosantidev.github.io`. It is permanent for the listing and cannot be changed
  later without a new listing.
- **`version`**: bump it (`manifest.json`) for every update you submit.
- **`homepage_url`** (optional): add it once you have a repo/landing page.

## 5. Hosting the privacy policy
AMO needs a URL (or pasted text). `PRIVACY.md` is ready. Easiest options:
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
  python -c "import static_ffmpeg.run as r; r.get_or_fetch_platform_executables_else_raise()"   # pre-fetch the static ffmpeg binary
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

## Quick reference
```
web-ext lint  -s extension                 # check
web-ext build -s extension -a dist         # zip
web-ext sign  -s extension --channel listed --api-key K --api-secret S   # sign/submit
web-ext run   -s extension                 # launch a temp Firefox with it loaded (dev)
```
