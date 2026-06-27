# Third-party software

Streaming LAN Cast itself is licensed under the PolyForm Noncommercial License 1.0.0 (see `LICENSE`):
it is free for any noncommercial use, and commercial use (including selling it) is not permitted. The source
repo bundles no third-party code. On Linux and macOS the user installs the dependencies below into an isolated
virtual environment (`pip install streamlink pychromecast static-ffmpeg`); the Windows PyInstaller `.exe`
bundles streamlink, pychromecast, zeroconf, and static-ffmpeg. Each dependency keeps its own license.

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

### static-ffmpeg
- Role: a Python wrapper that provides an ffmpeg binary. On Linux and macOS the installer uses it to
  fetch a static ffmpeg when no system ffmpeg is available. The wrapper is MIT licensed, but the
  ffmpeg binary it ships is a GPL/LGPL build (see FFmpeg below).
- License: MIT License (the wrapper).
- Project: https://github.com/cdgriffith/static_ffmpeg

### FFmpeg
- Role: the helper calls `ffmpeg` directly to remux the live source into a local short-segment HLS
  playlist for Google Cast devices, and DLNA streams that carry separate audio and video tracks may
  use it as well. On Linux and macOS the installer pulls a static ffmpeg through the static-ffmpeg
  package, or uses a system ffmpeg if one is present; the Windows `.exe` bundles it.
- License: LGPL-2.1+ or GPL depending on the build.
- Project: https://ffmpeg.org

### Python standard library
- Role: the helper is built entirely on the Python standard library otherwise (http.server, socket,
  urllib, subprocess, secrets, etc.). License: PSF License.

## Extension dependencies
None. The WebExtension is plain HTML/CSS/JS using only the WebExtensions (`browser.*`) APIs provided
by Firefox. It uses no third-party libraries and is not bundled or minified.
