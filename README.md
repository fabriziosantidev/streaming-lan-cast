# Streaming LAN Cast

Cast the video playing in your current browser tab to a DLNA/UPnP media renderer (a smart TV, an
AV receiver, or a media player) or a Google Cast device (Chromecast, Android TV, Google TV) on your
local network, straight from your browser. It works with live streams as well as on-demand video and
direct video files, and on-demand playback stays seekable on the TV, so you can skip around with the
remote.

Everything stays on your own machine and LAN: nothing about what you watch is ever sent to a server we
control. It does no tracking or analytics, and there's no account to create.

> Casting may be subject to the terms of service of the site you're streaming from. Use it only with
> content and networks you are allowed to use.

---

## How it works

Streaming LAN Cast is two pieces that talk over your machine's loopback address:

```
                                            ┌─ DLNA/UPnP ──▶  smart TV / AV receiver
                                            │  (streamlink live MPEG-TS over your LAN)
  browser extension ─HTTP─▶ local helper ───┤
  (picks device,           (127.0.0.1:9988) │
   sends the tab URL)                       └─ Google Cast ──▶  Chromecast / Android TV
                                               (authenticating HLS proxy via pychromecast)
```

- The **extension** (MV3 WebExtension) is the UI: it discovers both DLNA (SSDP) and Google Cast (mDNS,
  `_googlecast._tcp`) devices, lets you pick one and a quality, and tells the helper what to cast. If a
  device exposes both protocols, the extension prefers DLNA.
- The **helper** (a single Python script) does the actual work, and it has a path for each target. For a
  **DLNA/UPnP** renderer it uses [streamlink](https://github.com/streamlink/streamlink) to pull the live
  stream and re-serves it as a non-seekable live MPEG-TS feed over your LAN, pushed to the device via
  UPnP AVTransport. For a **Google Cast** device it connects with
  [pychromecast](https://github.com/home-assistant-libs/pychromecast) and loads a branded Cast
  receiver that plays the stream; the helper runs an authenticating HLS reverse-proxy that re-serves the
  source over your LAN with CORS, injecting the headers and token the source needs, so token-gated,
  expiring-segment live streams keep playing reliably. A live source plays live; on-demand video and
  direct video files (`.mp4`, `.webm`, and similar) are served with byte ranges so they stay seekable on
  the TV. Sites that aren't a direct playlist (Twitch and other streamlink-supported sites) are resolved
  first.

The extension only ever connects to `http://127.0.0.1:9988` (your own computer), authenticated with a
per-install token. See [`PRIVACY.md`](PRIVACY.md).

## Requirements

- **Firefox 140+**, or **Chrome / Edge / Brave 102+**
- **Python 3.10+** (the Linux/macOS installer builds an isolated venv with streamlink and pychromecast
  for you; the Windows `.exe` bundles those, so Windows needs nothing extra)
- A **DLNA/UPnP renderer** (most smart TVs, many AV receivers / media players) or a **Google Cast**
  device (Chromecast, Android TV, Google TV) on the same network
- **ffmpeg (optional)** for streams that carry separate audio and video tracks (which streamlink muxes
  with it) and for remuxing some on-demand video into a castable form. Install a system ffmpeg if you
  need one (Linux `apt install ffmpeg`, macOS `brew install ffmpeg`, Windows: add it to PATH). Most live
  streams are a single muxed track and need none.

## Install

### 1. Helper

**Linux and macOS (no sudo):** sets up an isolated venv + an autostart service (systemd on Linux,
launchd on macOS), installs a local uninstaller, and prints your token. Full walkthrough:
[`installer/unix/README.md`](installer/unix/README.md).

One command, no checkout needed:

```bash
curl -fsSL https://raw.githubusercontent.com/fabriziosantidev/streaming-lan-cast/main/installer/unix/install.sh | bash
```

Or from a local checkout:

```bash
./installer/unix/install.sh
```

Uninstall any time with `~/.local/share/streaming-lan-cast/uninstall.sh`, or the matching
`curl -fsSL .../installer/unix/uninstall.sh | bash` one-liner.

**Windows:** run the Inno Setup installer (`installer/windows/streaming-lan-cast.iss` over a PyInstaller
bundle). Python, streamlink, and pychromecast are bundled into the `.exe`, so you need nothing else; it autostarts
the helper and shows the token on the final page. See [`PACKAGING.md`](PACKAGING.md).

**Manual / other platforms:**

```bash
pip install -r requirements.txt
python helper/streaming-lan-cast-helper.py --serve
```

On first run it generates a token at `~/.streaming-lan-cast/token` (you'll paste it into the extension once).
Keep this running while you cast. To start it automatically at login:

- **Linux:** a `systemd --user` service; **macOS:** a `launchd` LaunchAgent (both handled by the `installer/unix` script above)
- **Windows:** a `.vbs` launcher (`pythonw streaming-lan-cast-helper.py --serve`, hidden) in the Startup folder

### 2. Extension

**Development (unsigned):** open `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on** →
select `extension/manifest.json`.

On **Chrome / Edge / Brave**, run `build/build-extension.sh`, then open `chrome://extensions` → enable
**Developer mode** → **Load unpacked** → select `dist/chrome/`.

**Published:** install from addons.mozilla.org *(link once listed)*. On **Chrome / Edge / Brave**, install from the Chrome Web Store *(link once listed)*.

Then open the toolbar popup:
- If it says **"Helper not detected"**, start the helper (step 1).
- If it says **"Token not set up"**, open the add-on's **Options**, paste the token from
  `~/.streaming-lan-cast/token`, and Save.

## Usage

1. Open a tab with a live stream or an on-demand video and click the **Streaming LAN Cast** toolbar button.
2. Pick a device, optionally choose a quality, then **Cast**. You can change the quality while
   casting, and on-demand video is seekable from the TV remote.
3. Stop from the popup, or from the TV's own remote (the popup notices and resets).

### Casting from arbitrary sites (optional "source detection")

Out of the box the helper resolves the usual streamlink-supported sites from the page URL. For other
pages whose player loads an **HLS (`.m3u8`)** or **DASH (`.mpd`)** stream, the extension can detect the
media source on the page and cast that instead:

1. In the popup, click **"Enable source detection"** and accept the permission prompt. This grants the
   **optional** `webRequest` + `<all_urls>` permission. It is **off until you turn it on**.
2. Reload the page / start the video so the detector sees the stream.
3. The popup shows **"Source detected"** → Cast.

This watches the active tab's network for media manifests (and the headers needed to fetch them) and
hands them to the **local** helper. Nothing leaves your machine. **Limitations:** DRM/Widevine streams
can't be cast, and players that use MSE/blob with no observable manifest can't be detected.

## Privacy

No data collection, no telemetry. The current tab's address (and any detected media address/headers)
are sent only to the local helper on `127.0.0.1`. Full policy: [`PRIVACY.md`](PRIVACY.md).

## Building & publishing

The extension is plain HTML/CSS/JS/JSON (no bundler or transpiler); a small step packages the Firefox and Chrome variants. [`PACKAGING.md`](PACKAGING.md) covers
packaging, linting, and signing (`web-ext lint` → `web-ext build` → `web-ext sign`), and the matching steps for building the Chrome/Edge package and submitting it to the Chrome Web Store.

## Repository layout

```
extension/            the cross-browser WebExtension (popup, options, background sniffer, _locales, icons)
helper/               the Python casting helper (streaming-lan-cast-helper.py)
requirements.txt      the helper's Python runtime dependencies (pip install -r)
installer/            per-OS helper installers (unix/ for Linux+macOS, windows/)
build/                the PyInstaller spec for the Windows helper bundle (work output gitignored)
docs/                 GitHub Pages site: landing page and the branded Cast receiver (docs/receiver/)
README.md             this file
PRIVACY.md            privacy policy
PACKAGING.md          packaging, signing, and store submission steps
THIRD-PARTY.md        third-party licenses (streamlink, etc.)
LICENSE               PolyForm Noncommercial 1.0.0
```

> Note: the helper script is the casting engine; its filename here (`streaming-lan-cast-helper.py`) is neutral by
> design. If you run a local copy under a different name, it's the same program.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) © 2026 Fabrizio Santi. Free for any **noncommercial** use;
commercial use, including selling it, is not permitted. Third-party runtime dependencies (streamlink, etc.)
keep their own licenses, see [`THIRD-PARTY.md`](THIRD-PARTY.md).
