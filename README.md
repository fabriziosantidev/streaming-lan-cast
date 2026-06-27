# Streaming LAN Cast

Cast the live video playing in your current browser tab to a DLNA/UPnP media renderer (a smart TV, an
AV receiver, or a media player) or a Google Cast device (Chromecast, Android TV, Google TV) on your
local network, straight from Firefox.

Everything stays on your own machine and LAN: nothing about what you watch is ever sent to a server we
control. There's no tracking, no analytics, no account.

> Casting may be subject to the terms of service of the site you're streaming from. Use it only with
> content and networks you are allowed to use.

---

## How it works

Streaming LAN Cast is two pieces that talk over your machine's loopback address:

```
                                            ┌─ DLNA/UPnP ──▶  smart TV / AV receiver
                                            │  (streamlink live MPEG-TS over your LAN)
  Firefox extension ─HTTP─▶ local helper ───┤
  (picks device,           (127.0.0.1:9988) │
   sends the tab URL)                       └─ Google Cast ──▶  Chromecast / Android TV
                                               (ffmpeg HLS remux via pychromecast)
```

- The **extension** (MV3 WebExtension) is the UI: it discovers both DLNA (SSDP) and Google Cast (mDNS,
  `_googlecast._tcp`) devices, lets you pick one and a quality, and tells the helper what to cast. If a
  device exposes both protocols, the extension prefers DLNA.
- The **helper** (a single Python script) does the actual work, and it has a path for each target. For a
  **DLNA/UPnP** renderer it uses [streamlink](https://github.com/streamlink/streamlink) to pull the live
  stream and re-serves it as a non-seekable live MPEG-TS feed over your LAN, pushed to the device via
  UPnP AVTransport. For a **Google Cast** device it connects with
  [pychromecast](https://github.com/home-assistant-libs/pychromecast) and tells the Default Media
  Receiver what to play; because cast receivers need a stable HLS source, the helper runs ffmpeg to
  remux the live source into a local short-segment (~2s) HLS playlist served on your LAN with CORS,
  so token-gated, expiring-segment live streams keep playing reliably.

The extension only ever connects to `http://127.0.0.1:9988` (your own computer), authenticated with a
per-install token. See [`PRIVACY.md`](PRIVACY.md).

## Requirements

- **Firefox 128+**
- **Python 3.10+** (the Linux/macOS installer builds an isolated venv with streamlink, pychromecast,
  and ffmpeg for you; the Windows `.exe` bundles them all, so Windows needs nothing extra)
- A **DLNA/UPnP renderer** (most smart TVs, many AV receivers / media players) or a **Google Cast**
  device (Chromecast, Android TV, Google TV) on the same network
- **ffmpeg** for the Google Cast path, where the helper uses it to remux the stream to HLS (DLNA streams
  that carry separate audio and video tracks may use it too). The installer handles this for you: on
  Linux/macOS via a static build (the `static-ffmpeg` package), and on Windows bundled into the `.exe`.
  If you already have a system ffmpeg, the installer uses that instead.

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
bundle). Python, streamlink, pychromecast, and ffmpeg are bundled into the `.exe`, so you need nothing else; it autostarts
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

**Published:** install from addons.mozilla.org *(link once listed)*.

Then open the toolbar popup:
- If it says **"Helper not detected"**, start the helper (step 1).
- If it says **"Token not set up"**, open the add-on's **Options**, paste the token from
  `~/.streaming-lan-cast/token`, and Save.

## Usage

1. Open a tab with a live stream and click the **Streaming LAN Cast** toolbar button.
2. Pick a device, optionally choose a quality and, for Google Cast devices, a buffer (latency) level,
   then **Cast**. You can change the quality and the buffer while casting; a larger buffer starts
   playback further behind the live edge, trading latency for stability.
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

The extension is plain HTML/CSS/JS/JSON with no build step. [`PACKAGING.md`](PACKAGING.md) covers
packaging, linting, and signing (`web-ext lint` → `web-ext build` → `web-ext sign`).

## Repository layout

```
extension/            the Firefox WebExtension (popup, options, background sniffer, _locales, icons)
helper/               the Python casting helper (streaming-lan-cast-helper.py)
requirements.txt      the helper's Python runtime dependencies (pip install -r)
installer/            per-OS helper installers (unix/ for Linux+macOS, windows/)
build/                the PyInstaller spec for the Windows helper bundle (work output gitignored)
README.md             this file
PRIVACY.md            privacy policy
PACKAGING.md          web-ext lint/build/sign steps
THIRD-PARTY.md        third-party licenses (streamlink, etc.)
LICENSE               PolyForm Noncommercial 1.0.0
```

> Note: the helper script is the casting engine; its filename here (`streaming-lan-cast-helper.py`) is neutral by
> design. If you run a local copy under a different name, it's the same program.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) © 2026 Fabrizio Santi. Free for any **noncommercial** use;
commercial use, including selling it, is not permitted. Third-party runtime dependencies (streamlink, etc.)
keep their own licenses, see [`THIRD-PARTY.md`](THIRD-PARTY.md).
