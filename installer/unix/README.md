# Installing Streaming LAN Cast (Linux and macOS)

A step-by-step guide to install the helper (a small local background service) and pair it with
the Firefox extension. The install is per-user and needs no `sudo`.

> On **Windows** the helper ships as a self-contained `.exe` (Python and streamlink are bundled in,
> so nothing else is needed). Use the Inno Setup installer instead, see the project README.

---

## What you need

- **Linux** (with systemd) or **macOS**, on a normal logged-in session.
- **Python 3.10+** (the installer builds an isolated venv from it). Check with `python3 --version`.
  - Linux: almost always preinstalled. If not: `sudo apt install python3 python3-venv` (Debian/Ubuntu),
    `sudo dnf install python3` (Fedora), `sudo pacman -S python` (Arch).
  - macOS: `brew install python` (Homebrew), or `xcode-select --install` (note: that one can be older
    than 3.10, so Homebrew is the safer bet).
- **Firefox 128+**.
- A **DLNA/UPnP renderer** on the same network (most smart TVs, many AV receivers / media players).
- *Optional:* **ffmpeg**. Only a few streams (the ones with separate audio and video tracks) need it.
  Linux: `sudo apt install ffmpeg` (or `dnf` / `pacman`). macOS: `brew install ffmpeg`. The installer
  warns if it's missing; you can add it later.

---

## 1. Install the helper

**One command (recommended, no download needed):**

```bash
curl -fsSL https://raw.githubusercontent.com/fabriziosantidev/streaming-lan-cast/main/installer/unix/install.sh | bash
```

> Prefer to inspect before running? Download it, read it, then run it, same result.

**Or from a checkout**, if you already cloned the repo:

```bash
./installer/unix/install.sh
```

No `sudo`. The installer:

1. Creates an isolated Python environment at `~/.local/share/streaming-lan-cast/venv` and installs
   streamlink into it (bootstrapping `pip` automatically if your system lacks it).
2. Installs the helper (downloaded for you with the one-liner, or copied from your checkout).
3. Sets up autostart so the helper runs now and again at every login: a **systemd user service** on
   Linux, a **launchd LaunchAgent** on macOS.
4. Drops a self-contained uninstaller at `~/.local/share/streaming-lan-cast/uninstall.sh`.
5. Prints your pairing token in a box at the end: a 32-char hex string like `1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d`.

Copy that token. (You can re-read it any time with `cat ~/.streaming-lan-cast/token`.)

---

## 2. Install the Firefox extension

Published (recommended, once listed): install from addons.mozilla.org. The token is the only setup
step and it persists across restarts.

Temporary (development, or before it's listed):

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on** and select `extension/manifest.json`.

> A *temporary* add-on is removed when you restart Firefox, and reloading it resets the optional
> "source detection" permission (a Firefox rule for temporary add-ons). The published version
> doesn't have these limits.

---

## 3. Pair them (paste the token)

1. Click the **Streaming LAN Cast** toolbar button.
2. If it says "Token not set up", click **Open options** (or the gear), paste the token from step 1,
   and **Save**.
3. If it says "Helper not detected", confirm the helper is running (see *Managing the helper*).

---

## 4. Cast a tab

1. Open a tab playing a live stream and click the toolbar button.
2. Pick your TV or renderer, optionally choose a quality, and hit **Cast this tab**.
3. Change quality while casting, or **Stop** from the popup (your TV stops cleanly).

Twitch, Kick and YouTube work out of the box. For other sites, turn on source detection:

1. In the popup, click **Enable source detection** and accept the prompt. It grants an optional
   `webRequest` + `<all_urls>` permission that stays off until you enable it.
2. Reload the stream page so the detector sees the media.
3. When the popup shows "Source detected", cast.

> Official OTT streams (DirecTV, Netflix, Disney+, etc.) are DRM-protected, so no proxy tool can cast
> them, and the popup will tell you so. Only clear HLS (`.m3u8`) and DASH (`.mpd`) sources are castable.

---

## Managing the helper

**Linux:**

```bash
systemctl --user status   streaming-lan-cast.service   # is it running?
systemctl --user restart  streaming-lan-cast.service   # after an update
systemctl --user stop     streaming-lan-cast.service   # stop it
journalctl --user -u streaming-lan-cast.service -e     # service logs
```

**macOS:**

```bash
PLIST=~/Library/LaunchAgents/com.fabriziosantidev.streaming-lan-cast.plist
launchctl list | grep streaming-lan-cast               # is it running?
launchctl unload "$PLIST" && launchctl load -w "$PLIST"  # restart after an update
launchctl unload "$PLIST"                               # stop it
```

Casting diagnostics are logged to `streaming-lan-cast.log` in your temp dir (`/tmp` on Linux,
`$TMPDIR` on macOS).

---

## Updating

Re-run the installer (the one-liner, or `./installer/unix/install.sh` from a checkout). It reuses the
venv, updates streamlink and the helper, and restarts.

---

## Uninstalling

**One command** (works even if you installed via the one-liner, no repo needed):

```bash
curl -fsSL https://raw.githubusercontent.com/fabriziosantidev/streaming-lan-cast/main/installer/unix/uninstall.sh | bash
```

**Or the local copy** the installer dropped:

```bash
~/.local/share/streaming-lan-cast/uninstall.sh
```

It removes the autostart service (systemd or launchd), the venv, and the token directory
(`~/.streaming-lan-cast`), and reaps any running cast.

- To just stop autostart and keep the files: Linux `systemctl --user disable --now streaming-lan-cast.service`;
  macOS `launchctl unload ~/Library/LaunchAgents/com.fabriziosantidev.streaming-lan-cast.plist`.
- From a checkout, you can also run `./installer/unix/uninstall.sh`.

Remove the extension from Firefox separately (`about:addons`).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **"Helper not detected"** | Linux: `systemctl --user status streaming-lan-cast.service` (start it with `... start ...`). macOS: `launchctl list \| grep streaming-lan-cast`. If you reinstalled, re-paste the token. |
| **Install stops: "python3 not found"** | Linux: install `python3` (and `python3-venv`) from your package manager. macOS: `brew install python` or `xcode-select --install`. |
| **Install stops: "Python 3.10+ is required"** | Update Python (macOS: `brew install python`; Linux: your distro's package). |
| **"DRM-protected" message** | The stream is encrypted and can't be cast. Try a clear-HLS source. |
| **Stream won't play** | Install ffmpeg. Linux: `sudo apt install ffmpeg` (or `dnf`/`pacman`). macOS: `brew install ffmpeg`. |
| **It keeps asking to "Enable source detection"** | You're on a *temporary* add-on and reloaded it (or restarted Firefox), which resets the optional permission. Grant it again. The published version keeps it. |
| **No autostart (Linux): "no systemd user session"** | Run the installer from a normal desktop login (not a bare root shell). For headless/SSH: `loginctl enable-linger "$USER"`. |
| **Wrong device** | If you have multiple renderers, make sure you picked the right one in the popup. |
