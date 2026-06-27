#!/usr/bin/env python3
"""
streaming-lan-cast-helper.py

Grab a live stream that streamlink supports and play it on a renderer on the
local network (a smart TV, an AV receiver, a media player). Two delivery paths,
picked per device:

DLNA/UPnP push (--proxy): serve the stream as live MPEG-TS from OUR OWN HTTP
server and push it to a UPnP AVTransport renderer (SetAVTransportURI + Play), so
the TV sees a true LIVE, non-seekable stream:
  - no Content-Length, no Range/seek support  -> the TV won't show a recorded-video
    scrub bar that strands you behind live
  - contentFeatures.dlna.org: DLNA.ORG_OP=00   -> advertised live on the HTTP
    response itself (the renderer reads this; the DIDL flags are often ignored)
  - each TV (re)connect launches a FRESH streamlink at the live edge -> pause &
    reconnect resyncs to live automatically

Google Cast (--cast, Chromecast / Android TV): ffmpeg remuxes the source into
local 2s HLS segments that we serve to the Cast receiver, driven over the Cast
protocol via pychromecast. Same live behaviour, plus a buffer/latency knob.

The control server (--serve) is what the Firefox extension talks to: it discovers
both kinds of renderer and launches the matching path per cast.

Usage:
  python streaming-lan-cast-helper.py <stream-url> --proxy            # push to a DLNA renderer
  python streaming-lan-cast-helper.py <stream-url> --proxy --low-latency
  python streaming-lan-cast-helper.py <stream-url> --proxy --quality 720p60
  python streaming-lan-cast-helper.py <stream-url> --cast --tv <ip>   # send to a Google Cast device
  python streaming-lan-cast-helper.py --serve        # control server for the extension
  python streaming-lan-cast-helper.py --stop
"""

# Table of contents (top-level sections, in file order):
#   1. Process spawning and shared helpers
#   2. PID management and proxy liveness
#   3. Cast-state persistence
#   4. Per-install auth token
#   5. DLNA discovery and device description
#   6. Device cache, stream metadata and sanitising
#   7. UPnP AVTransport SOAP and DIDL control
#   8. Live MPEG-TS HTTP server (DLNA push)
#   9. Google Cast (Chromecast / Android TV)
#  10. Control server for the browser extension
#  11. Entry point and argument dispatch (main)

import argparse
import getpass
import html
import http.server
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import atexit
import shutil
import urllib.parse
import urllib.request
from collections import OrderedDict

DEFAULT_URL = ""           # the extension passes the stream URL per cast
DEFAULT_TV = ""            # the extension passes the chosen renderer's IP per cast
DMR_PORT = 9197
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
PIDFILE = os.path.join(tempfile.gettempdir(), "streaming-lan-cast-proxy.pid")
STATEFILE = os.path.join(tempfile.gettempdir(), "streaming-lan-cast-state.json")  # survives --serve restarts
TOKEN_DIR = os.path.join(os.path.expanduser("~"), ".streaming-lan-cast")
TOKEN_FILE = os.path.join(TOKEN_DIR, "token")   # per-install secret shared with the extension
LOGFILE = os.path.join(tempfile.gettempdir(), "streaming-lan-cast.log")   # caster diagnostics (pythonw has no console)
# Advertised on the HTTP response so the renderer treats it as a LIVE, non-seekable
# broadcast (OP=00 = no seek; sender-paced / sliding-window live flags).
DLNA_LIVE_CF = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=8D500000000000000000000000000000"
MIME = "video/mpeg"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW on Windows; 0 (no-op) on macOS/Linux
FROZEN = getattr(sys, "frozen", False)    # True inside the PyInstaller bundle (single-exe build)

# --- Process spawning and shared helpers -------------------------------------
# On Linux, ask the kernel to kill our long-running children (ffmpeg/streamlink) if THIS process
# dies by any means -- even SIGKILL or a crash, which run no Python cleanup. Without this an ffmpeg
# that writes to files (not a pipe) never notices the helper died and leaks as an orphan.
try:
    import ctypes
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True) if sys.platform.startswith("linux") else None
except Exception:
    _LIBC = None

def _set_pdeathsig():
    """preexec_fn: PR_SET_PDEATHSIG=SIGKILL so the child can't outlive us. Linux-only; elsewhere the
    atexit/finally cleanups (which DO run on SIGTERM/normal exit) are the backstop."""
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGKILL, 0, 0, 0)   # 1 = PR_SET_PDEATHSIG

_PDEATHSIG = _set_pdeathsig if _LIBC is not None else None


def _streamlink_cmd(*args):
    """Command to run streamlink. As a frozen .exe there's no `python -m streamlink`, so
    re-invoke this same executable with a shim flag that dispatches to the bundled
    streamlink CLI (handled at the top of main())."""
    if FROZEN:
        return [sys.executable, "--__sl", *args]
    return [sys.executable, "-m", "streamlink", *args]


def _child_python():
    """Interpreter to spawn child python processes with. On Windows prefer
    pythonw.exe (no console); on macOS/Linux just use the current interpreter."""
    exe = sys.executable or "python3"
    if IS_WIN and exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe


def log(msg):
    try:
        print(f"[cast] {msg}", flush=True)
    except Exception:
        pass  # no console (e.g. launched via pythonw)
    try:
        # create owner-only (0600): the log lives in the shared temp dir and can carry media
        # URLs, so don't leave it world-readable for other local users.
        fd = os.open(LOGFILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [{os.getpid()}] {msg}\n")
    except Exception:
        pass


def _install_safe_url_opener():
    """Restrict urllib to HTTP(S) only. build_opener() can't do this: it only *replaces* the
    http/https defaults, leaving File/FTP/Data handlers registered, so a file:// or ftp:// URL
    we're handed (e.g. a spoofed SSDP LOCATION) would still read a local file. So build the
    OpenerDirector by hand with just the http(s) machinery + UnknownHandler -> any non-http(s)
    URL raises URLError instead, closing that SSRF / local-file-disclosure class process-wide."""
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
              urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
              urllib.request.HTTPRedirectHandler(), urllib.request.HTTPErrorProcessor(),
              urllib.request.HTTPDefaultErrorHandler(),
              # carry cookies across fetches: a tokenized HLS playlist often sets a session cookie
              # that its segments need -- without this the segments 404/403 (the cast HLS proxy
              # fetches them itself, unlike the DLNA path where streamlink keeps the session).
              urllib.request.HTTPCookieProcessor()):
        opener.add_handler(h)
    urllib.request.install_opener(opener)


# --- PID management and proxy liveness ---------------------------------------
def _proxy_image_name():
    """Basename of the executable a detached proxy child runs as (for PID-identity checks)."""
    return os.path.basename(sys.executable or "python").lower()


# A cast proxy's argv always contains this token (the script / frozen-exe name) AND one of the
# serving-mode flags, so we can tell a real proxy from an unrelated process that reused a PID.
PROXY_IDENT = "streaming-lan-cast-helper"
PROXY_MODE_FLAGS = ("--proxy", "--cast")   # DLNA (MPEG-TS) and Google Cast (HLS) serving modes


def _read_pidfile():
    """The pid recorded in PIDFILE, or None if absent/garbage."""
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pidfile(pid):
    """Record the casting proxy's pid. The CONTROL SERVER writes this synchronously the instant
    it spawns the proxy (under its state lock), so proxy_alive() is true immediately -- closing
    the window where state says 'casting' but the pidfile is still empty (a /status poll there
    used to read 'stopped' and tear the fresh cast down)."""
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        pass


def _remove_pidfile():
    """Remove PIDFILE, but only if it still names THIS process -- so a proxy exiting late can't
    delete the pidfile of a newer proxy the control server already started."""
    pid = _read_pidfile()
    if pid is not None and pid != os.getpid():
        return
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def _arm_pidfile_cleanup():
    """Write our pid to the pidfile and drop it on normal exit + on SIGTERM, so a crashed or
    signalled proxy doesn't leave a stale PID. (When the control server launched us it already
    wrote the same pid; rewriting it is idempotent.) SIGINT is left to KeyboardInterrupt so the
    existing Ctrl+C shutdown paths still run."""
    _write_pidfile(os.getpid())
    atexit.register(_remove_pidfile)

    def _term(_signum, _frame):
        _remove_pidfile()
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError, AttributeError):
        pass   # not main thread / unsupported -> atexit still covers normal exit


def _pid_cmdline(pid):
    """Best-effort command line of a pid (Linux /proc, else `ps`); '' on failure."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=3)
        return out.stdout or ""
    except Exception:
        return ""


def _pid_is_proxy(pid):
    """True only if `pid` is alive AND is one of OUR cast proxies -- not merely a process that
    reused the recorded PID. Without this an old crash-leaked pidfile + PID reuse would make us
    report a phantom cast and (in kill_previous_proxy) signal an unrelated process."""
    if pid is None:
        return False
    if IS_WIN:
        # Require the PID to be a python/our-exe image (CSV-parsed); tasklist doesn't expose the
        # command line without extra tooling, so image identity is the available check here.
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=NO_WINDOW)
        line = (out.stdout or "").strip().lower()
        if not line or "no tasks" in line:
            return False
        image = line.split('","', 1)[0].lstrip('"')   # first CSV field = image name
        return ("python" in image) or (image == _proxy_image_name())
    try:
        os.kill(pid, 0)   # 0 = existence check; EPERM => alive but another user's => not ours
    except (ProcessLookupError, PermissionError, OSError, OverflowError):
        return False      # OverflowError: garbage/out-of-range pid in a corrupt pidfile
    cmd = _pid_cmdline(pid).lower()
    return bool(cmd) and PROXY_IDENT in cmd and any(f in cmd for f in PROXY_MODE_FLAGS)


def kill_previous_proxy():
    """Stop a cast proxy from a prior run so re-running == resync to live edge. Identity-checked
    so a stale pidfile pointing at a reused PID can't make us signal an unrelated process."""
    pid = _read_pidfile()
    if pid is None or not _pid_is_proxy(pid):
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)  # no console flash
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    log(f"resync: stopped previous proxy (pid {pid})")


def proxy_alive():
    """True if a cast proxy from a prior run is still running (PID-identity checked, so a
    recycled PID after a crash can't masquerade as the proxy)."""
    return _pid_is_proxy(_read_pidfile())


# --- Cast-state persistence --------------------------------------------------
# Single source of truth for the cast-state shape: the fields a /status or /cast snapshot
# returns, and the empty/default record. Defined once so adding a field is a one-line edit.
_CAST_SNAP_FIELDS = ("url", "device", "name", "title", "quality", "buffer")


def _empty_cast_state():
    return {"url": "", "device": "", "name": "", "title": "", "quality": "best",
            "media": "", "headers": [], "epoch": 0, "kind": "dlna", "cast": {}, "buffer": 5}


def save_cast_state(d, grace_until=0.0):
    """Persist the current cast target so a --serve restart (or crash/update) can
    recover what's playing instead of showing an empty 'casting' view."""
    try:
        tmp = STATEFILE + ".tmp"
        rec = {k: d.get(k, "") for k in _CAST_SNAP_FIELDS}
        rec["media"] = d.get("media", "")        # sniffed direct media URL (generic-site casts)
        rec["headers"] = d.get("headers", []) or []
        rec["kind"] = d.get("kind", "dlna")      # dlna (SOAP) vs cast (Chromecast) target
        rec["cast"] = d.get("cast") or {}        # cast device details (port/uuid/name/model)
        rec["buffer"] = int(d.get("buffer", 5) or 5)   # latency/stability knob (seconds behind live)
        rec["grace_until"] = grace_until   # survive a restart that lands mid quality-recast
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, STATEFILE)   # atomic
    except Exception as e:
        log(f"save_cast_state failed: {type(e).__name__}: {e}")


def load_cast_state():
    """Read the persisted cast target, or None if absent/invalid."""
    try:
        with open(STATEFILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def clear_cast_state():
    try:
        os.remove(STATEFILE)
    except OSError:
        pass


# --- Per-install auth token --------------------------------------------------
def load_or_create_token():
    """Per-install secret the extension must present (X-LanCast-Token). Generated once,
    persisted to ~/.streaming-lan-cast/token so it survives restarts. Defends against any web page
    forging requests to the loopback control server (CSRF / drive-by casts)."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        # create owner-only from the start (mode is honored on POSIX; ignored on Windows,
        # where secure_token_file() tightens the ACL instead)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError as e:
        log(f"token persist failed: {type(e).__name__}: {e}")
    return tok


def secure_token_file():
    """Harden the token file to owner-only and report whether it is safe.
    Returns (ok, detail). POSIX: chmod 600 + verify no group/other bits (ok=False if it
    stays loose -> caller refuses to serve). Windows: restrict the ACL to the current user
    (best-effort; the user-profile ACL already scopes it, so Windows never hard-fails)."""
    if IS_WIN:
        try:
            user = getpass.getuser()
            r = subprocess.run(
                ["icacls", TOKEN_FILE, "/inheritance:r", "/grant:r", f"{user}:(F)"],
                capture_output=True, text=True, creationflags=NO_WINDOW)
            if r.returncode == 0:
                return True, f"ACL restricted to {user}"
            return True, f"icacls rc={r.returncode}; user-profile ACL still applies"
        except Exception as e:
            return True, f"icacls skipped ({type(e).__name__}); user-profile ACL still applies"
    # POSIX
    try:
        os.chmod(TOKEN_DIR, 0o700)
    except OSError:
        pass
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as e:
        log(f"token chmod failed: {e}")
    try:
        mode = os.stat(TOKEN_FILE).st_mode & 0o777
    except OSError as e:
        return True, f"stat failed, skipping check ({e})"
    if mode & 0o077:
        return False, f"group/other-accessible (mode {oct(mode)})"
    return True, f"owner-only (mode {oct(mode)})"


# --- DLNA discovery and device description -----------------------------------
# DLNA device discovery: SSDP M-SEARCH for UPnP MediaRenderers,
# so the user picks a TV by name instead of hardcoding an IP. No new deps.
def _avtransport_control_url(desc_xml, base_url):
    """Extract the AVTransport <controlURL> from a UPnP device description and resolve it
    against base_url (handles relative paths and absolute-with-host correctly). Returns an
    absolute control URL or None. Shared by SSDP discovery and the direct --tv path."""
    for block in re.findall(r"<service>(.*?)</service>", desc_xml, re.S):
        if "AVTransport" in block:
            m = re.search(r"<controlURL>(.*?)</controlURL>", block, re.S)
            if m:
                ctrl = urllib.parse.urljoin(base_url, m.group(1).strip())
                # pin to the device's own host: a spoofed/hostile description can't redirect our
                # SOAP POSTs at some other LAN service (request-forgery).
                if urllib.parse.urlparse(ctrl).hostname != urllib.parse.urlparse(base_url).hostname:
                    return None
                return ctrl
            break
    return None


def _parse_dlna_device(location):
    """Fetch a UPnP device description and return a device dict if it's an
    AVTransport-capable MediaRenderer; else None."""
    try:
        with urllib.request.urlopen(location, timeout=4) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    ctrl = _avtransport_control_url(xml, location)
    if not ctrl:
        return None  # not a renderer we can drive
    name = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S)
    udn = re.search(r"<UDN>(.*?)</UDN>", xml, re.S)
    model = re.search(r"<modelName>(.*?)</modelName>", xml, re.S)
    host = urllib.parse.urlparse(location).hostname
    return {
        "id": (udn.group(1).strip() if udn else host),
        "name": html.unescape(name.group(1).strip()) if name else host,
        "host": host,
        "model": html.unescape(model.group(1).strip()) if model else "",
        "kind": "dlna",
        "control_url": ctrl,
    }


def _primary_multicast_ip():
    """The local IP the OS uses to reach the multicast group: the right NIC,
    found instantly without enumerating adapters."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("239.255.255.250", 1900))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _ssdp_search(src_ip, timeout):
    """One SSDP M-SEARCH from src_ip ('' = default NIC); return a set of LOCATION URLs."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
    ).encode("ascii")
    locs = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        if src_ip:
            s.bind((src_ip, 0))
        s.settimeout(0.4)
        s.sendto(msg, ("239.255.255.250", 1900))
        s.sendto(msg, ("239.255.255.250", 1900))   # 2 probes; UDP can drop
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            m = re.search(r"LOCATION:\s*(\S+)", data.decode("utf-8", "replace"), re.I)
            if m:
                locs.add(m.group(1).strip())
    except OSError:
        pass
    finally:
        s.close()
    return locs


def discover_dlna_renderers(timeout=1.8):
    """Fast SSDP discovery of DLNA MediaRenderers: probe ALL real LAN interfaces
    in PARALLEL (one timeout window total, not per-NIC), then fetch device
    descriptions in parallel. Tailscale/VPN/APIPA IPs are filtered out."""
    from concurrent.futures import ThreadPoolExecutor
    srcs = _candidate_source_ips()
    locs = set()
    with ThreadPoolExecutor(max_workers=max(2, len(srcs))) as ex:
        for found in ex.map(lambda ip: _ssdp_search(ip, timeout), srcs):
            locs |= found
    if not locs:                      # last resort: OS default interface
        locs = _ssdp_search("", timeout)
    devices, seen = [], set()
    if locs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for dev in ex.map(_parse_dlna_device, list(locs)):
                if dev and dev["id"] not in seen:
                    seen.add(dev["id"])
                    devices.append(dev)
    devices.sort(key=lambda d: d["name"].lower())
    return devices


# --- Device cache, stream metadata and sanitising ----------------------------
# Device cache so /devices is instant after the first scan.
_dev_cache = []
_dev_lock = threading.Lock()
_dev_scanned = False


def _refresh_devices():
    global _dev_cache, _dev_scanned
    devs = discover_all_devices()
    with _dev_lock:
        _dev_cache = devs
        _dev_scanned = True


def cached_devices():
    with _dev_lock:
        if _dev_scanned:
            return list(_dev_cache)
    _refresh_devices()   # first ever call: scan synchronously (~1.6s)
    with _dev_lock:
        return list(_dev_cache)


def peek_device_by_host(host):
    """A discovered device by host WITHOUT triggering a scan (None if unknown). Lets /cast learn
    a target's kind (dlna vs cast) + cast details from the warm cache without blocking."""
    if not host:
        return None
    with _dev_lock:
        cache = list(_dev_cache) if _dev_scanned else []
    return next((d for d in cache if d.get("host") == host), None)


_meta_cache = OrderedDict()
_META_CACHE_MAX = 64           # bound the cache in the long-lived --serve daemon
_meta_lock = threading.Lock()


def _quality_rank(q):
    m = re.match(r"(\d+)p(\d+)?", q)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (-1, 0)


def stream_meta(url):
    """One `streamlink --json` call: returns {title, author, qualities[]}.
    Cached per-url (~90s) so /qualities and the /cast title worker reuse it.
    Qualities are real renditions (best/worst aliases dropped), highest first."""
    now = time.time()
    with _meta_lock:
        c = _meta_cache.get(url)
        if c and now - c[0] < 90:
            return c[1]
    meta = {"title": "", "author": "", "qualities": []}
    try:
        out = subprocess.run(
            _streamlink_cmd("--json", "--", url),
            capture_output=True, text=True, creationflags=NO_WINDOW, timeout=25)
        data = json.loads(out.stdout or "{}")
        md = data.get("metadata") or {}
        meta["title"] = (md.get("title") or "").strip()
        meta["author"] = (md.get("author") or "").strip()
        streams = data.get("streams") or {}
        qs = [q for q in streams
              if q not in ("best", "worst", "best-unfiltered", "worst-unfiltered")]
        qs.sort(key=_quality_rank, reverse=True)
        meta["qualities"] = qs
    except Exception:
        pass
    with _meta_lock:
        _meta_cache[url] = (now, meta)
        _meta_cache.move_to_end(url)
        while len(_meta_cache) > _META_CACHE_MAX:
            _meta_cache.popitem(last=False)   # evict oldest
    return meta


def fetch_stream_title(url):
    """Display string for the current stream (title, author), or ''."""
    m = stream_meta(url)
    title, author = m["title"], m["author"]
    if title and author and author.lower() not in title.lower():
        return f"{title} - {author}"
    return title or author


def _proto_target(url):
    """Prefix the streamlink protocol plugin so it's selected regardless of the URL's
    extension/query (.m3u8 -> hls://, .mpd -> dash://; otherwise passed as-is)."""
    low = url.lower()
    if ".m3u8" in low:
        return "hls://" + url
    if ".mpd" in low:
        return "dash://" + url
    return url


def probe_stream(target, header_cfg=""):
    """Pre-flight a sniffed media target with `streamlink --json` so a doomed cast
    (DRM-protected, offline, unsupported) fails fast with a clear reason instead of
    silently leaving the TV black. Returns (ok, reason, detail):
      ok=True             -> streamlink resolved playable streams
      reason='drm'        -> DRM-protected (uncasteable by design)
      reason='unplayable' -> no plugin / offline / geo-blocked / other resolve failure
    Never raises: if the probe itself can't run, it soft-passes so a transient hiccup
    doesn't block a stream that might actually play. header_cfg is a private streamlink
    --config file with the replay headers (keeps Cookie out of argv)."""
    opts = ["--json"]
    if header_cfg:
        opts += ["--config", header_cfg]
    try:
        out = subprocess.run(_streamlink_cmd(*opts, "--", target),
                             capture_output=True, text=True, creationflags=NO_WINDOW, timeout=25)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return True, "", ""          # couldn't probe -> don't block the cast
    if data.get("streams"):
        return True, "", ""
    err = (data.get("error") or "").strip()
    low = err.lower()
    if "drm" in low or "protected" in low:
        return False, "drm", err
    return False, "unplayable", (err or "streamlink could not resolve the stream")


# headers the extension is allowed to replay to streamlink (the ones a player needs to
# fetch a protected stream). Anything else is dropped: no arbitrary header injection.
# This set is the SECURITY BOUNDARY; keep it in sync with the extension's WANT list in
# extension/background.js (that list must be a subset of this one).
_REPLAY_HEADERS = {"referer", "origin", "user-agent", "cookie"}


def parse_replay_headers(raw):
    """Parse the extension's headers payload (JSON object {Name: Value}) into streamlink
    'Name=Value' strings. Sanitised: allow-listed names only, no CR/LF, bounded length/count."""
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    out = []
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.lower() not in _REPLAY_HEADERS:
            continue
        if any(c in k or c in v for c in ("\r", "\n")):
            continue
        out.append(f"{k}={v[:4096]}")
        if len(out) >= 6:
            break
    return out


_QUALITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")   # must start alphanumeric (no leading '-')


def _safe_quality(value):
    """Quality selector, validated. streamlink takes it as a trailing POSITIONAL, so an
    unvalidated value like '--http-proxy=...' could masquerade as a streamlink option; restrict
    it to a conservative charset and fall back to 'best'."""
    value = (value or "").strip()
    return value if (value and _QUALITY_RE.match(value)) else "best"


def _safe_buffer(value, default=5):
    """Buffer/latency target in seconds (how far behind live the cast starts). Clamped to a sane
    range. Lower = less delay but more rebuffer risk; higher = more delay but steadier."""
    try:
        return max(2, min(20, int(float(value))))
    except (TypeError, ValueError):
        return default


def _safe_unlink(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _write_header_config(headers):
    """Write replay headers to a PRIVATE (0600) streamlink --config file and return its path
    ('' if none). Credentials (Cookie) then reach streamlink via the config file instead of argv
    -- argv is world-readable via /proc/<pid>/cmdline, a config file is owner-only."""
    if not headers:
        return ""
    try:
        fd, path = tempfile.mkstemp(prefix="slc-hdr-", suffix=".conf")   # mkstemp creates 0600
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for h in headers:
                f.write(f"http-header={h}\n")
        return path
    except OSError:
        return ""


def _redact_url(url):
    """A URL with its query string stripped, for logging -- signed/tokenised query params don't
    belong in the (shared-temp-dir) log even though it's now created owner-only."""
    return (url or "").split("?", 1)[0]


def _candidate_source_ips():
    """Fast (no subprocess) list of local IPv4 source addresses to probe, with
    loopback / APIPA / Tailscale-CGNAT filtered out. Returns [''] (default NIC)
    if none survive."""
    ips = set()
    p = _primary_multicast_ip()
    if p:
        ips.add(p)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    out = []
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip.startswith("100."):                      # Tailscale CGNAT 100.64.0.0/10
            try:
                if 64 <= int(ip.split(".")[1]) <= 127:
                    continue
            except (IndexError, ValueError):
                pass
        out.append(ip)
    return out or [""]


def lan_ip(target):
    """Host IP to advertise to the TV for the local proxy URL. It MUST be on the TV's
    subnet, never a Tailscale/VPN address (the TV can't reach 100.64.0.0/10). The OS
    routing table can pick a VPN source IP when Tailscale hijacks the LAN subnet, so we
    prefer a same-subnet host address and only fall back to the routing choice."""
    route_ip = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        route_ip = s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()

    t = target.split(".")

    def shared(ip):                                # count of matching leading octets with the TV
        n = 0
        for x, y in zip(t, (ip or "").split(".")):
            if x != y:
                break
            n += 1
        return n

    if shared(route_ip) >= 3:
        return route_ip                            # routing already chose a same-/24 NIC -> trust it
    # routing went out a VPN/other NIC (e.g. Tailscale hijacking the subnet). _candidate_source_ips()
    # already drops Tailscale/APIPA/loopback, so pick the real host IP closest to the TV's subnet.
    cands = _candidate_source_ips()
    best = max(cands, key=shared) if cands else ""
    if best and shared(best) >= 1:
        if best != route_ip:
            log(f"lan_ip: routing chose {route_ip or '?'} (off-subnet); using {best} for the TV")
        return best
    return route_ip or "127.0.0.1"


# --- UPnP AVTransport SOAP and DIDL control ----------------------------------
def discover_control_url(tv, port):
    base = f"http://{tv}:{port}/dmr"
    with urllib.request.urlopen(base, timeout=5) as r:
        desc = r.read().decode("utf-8", "replace")
    ctrl = _avtransport_control_url(desc, base)
    if not ctrl:
        raise SystemExit("Could not find AVTransport controlURL in the TV's description")
    return ctrl


def soap(control_url, action, inner, retries=3):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{AVT}">{inner}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(control_url, data=body, method="POST")
            req.add_header("Content-Type", 'text/xml; charset="utf-8"')
            req.add_header("SOAPAction", f'"{AVT}#{action}"')
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(0.7)
    raise last


def _didl(url, title, upnp_class, protocol_info):
    """Shared DIDL-Lite metadata envelope for SetAVTransportURI (namespaces, item wrapper,
    title + res). Only upnp:class and protocolInfo differ between the live-TS and HLS paths."""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:sec="http://www.sec.co.kr/dlna">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        f"<upnp:class>{upnp_class}</upnp:class>"
        f'<res protocolInfo="{protocol_info}">{html.escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def didl(stream_url, title):
    cls = "object.item.videoItem.videoBroadcast"
    proto = f"http-get:*:{MIME}:{DLNA_LIVE_CF}"
    return _didl(stream_url, title, cls, proto)


def set_and_play(control_url, url, title):
    log("SetAVTransportURI ...")
    soap(control_url, "SetAVTransportURI",
         f"<InstanceID>0</InstanceID><CurrentURI>{html.escape(url)}</CurrentURI>"
         f"<CurrentURIMetaData>{html.escape(didl(url, title))}</CurrentURIMetaData>")
    time.sleep(0.8)
    log("Play ...")
    try:
        soap(control_url, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
    except Exception as e:
        log(f"Play got no response ({type(e).__name__}); TV likely auto-plays from SetURI")


def transport_state(control_url):
    try:
        resp = soap(control_url, "GetTransportInfo", "<InstanceID>0</InstanceID>")
        m = re.search(r"<CurrentTransportState>(.*?)</CurrentTransportState>", resp)
        return m.group(1) if m else "?"
    except Exception as e:
        return f"err:{e}"


# --- Live MPEG-TS HTTP server (DLNA push) ------------------------------------
# Live HTTP server: serves the stream as non-seekable live, fresh per connect.
def make_handler(target, quality, sl_flags, extra_sl=(), tv=""):
    class LiveHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # no Content-Length needed; close = EOF

        def log_message(self, *a):
            pass

        def _send_live_headers(self):
            self.send_response(200)
            self.send_header("Content-Type", MIME)
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", DLNA_LIVE_CF)
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()

        def _foreign(self):
            # fail CLOSED: only the cast target (and loopback) may pull the stream. tv is always
            # set on a real cast, so an empty tv refusing everyone is the safe default.
            return self.client_address[0] not in (tv, "127.0.0.1")

        def do_HEAD(self):
            if self._foreign():
                self.send_error(403)
                return
            self._send_live_headers()

        def do_GET(self):
            if self._foreign():
                log(f"proxy: refused {self.client_address[0]} (not the cast target {tv})")
                self.send_error(403)
                return
            log(f"TV connected ({self.client_address[0]}) -> streamlink target: {_redact_url(target)}")
            self._send_live_headers()
            cmd = _streamlink_cmd(*sl_flags, *extra_sl, "--stdout", "--", target, quality)
            errf = tempfile.TemporaryFile()
            sl = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, creationflags=NO_WINDOW)
            total = 0
            try:
                while True:
                    chunk = sl.stdout.read(188 * 350)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    total += len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    sl.terminate()
                    try:
                        sl.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        sl.kill()       # streamlink ignored terminate -> force it
                        sl.wait()
                except Exception:
                    pass
                try:
                    if sl.stdout:
                        sl.stdout.close()   # release the pipe; also unblocks streamlink if mid-write
                except Exception:
                    pass
                if total < 100000:   # streamlink barely produced anything -> likely failed; log why
                    try:
                        errf.seek(0)
                        err = errf.read().decode("utf-8", "replace").strip()[-1500:]
                        log(f"streamlink sent only {total}B (likely FAILED) for {_redact_url(target)}\nstderr: {err}")
                    except Exception:
                        pass
                else:
                    log(f"TV disconnected; streamlink stopped (sent {total} bytes)")
                try:
                    errf.close()
                except Exception:
                    pass

    return LiveHandler


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # TVs/Chromecasts reset connections constantly (closing keep-alives, seeking); those are
        # benign -- don't spew a traceback for them. Real handler errors are logged via log().
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# --- Google Cast (Chromecast / Android TV) -----------------------------------
# Cast speaks a TLS+protobuf protocol (not DLNA/SOAP) and its receiver plays HLS, not raw
# MPEG-TS -- so cast targets get the HLS proxy below instead of the live.ts server. pychromecast
# (+ zeroconf) is imported lazily so a DLNA-only install never needs it.
def discover_cast_devices(timeout=2.0):
    """mDNS browse for _googlecast._tcp devices. Returns device dicts (kind='cast'); [] if
    zeroconf isn't installed (cast support is optional)."""
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except Exception:
        return []
    found = {}

    def _txt(props, key):
        v = props.get(key.encode()) if isinstance(props, dict) else None
        if v is None and isinstance(props, dict):
            v = props.get(key)
        return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else (v or "")

    class _Listener:
        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=1500)
            except Exception:
                info = None
            if not info:
                return
            try:
                addrs = info.parsed_addresses()
            except Exception:
                addrs = []
            host = next((a for a in addrs if ":" not in a), addrs[0] if addrs else None)
            if not host:
                return
            props = info.properties or {}
            uid = _txt(props, "id") or name
            found[uid] = {
                "id": "cast:" + uid,
                "name": _txt(props, "fn") or host,
                "host": host,
                "model": _txt(props, "md") or "",
                "kind": "cast",
                "port": info.port or 8009,
                "uuid": _txt(props, "id"),
            }

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_googlecast._tcp.local.", _Listener())
        time.sleep(timeout)
    except Exception:
        pass
    finally:
        try:
            zc.close()
        except Exception:
            pass
    return list(found.values())


def discover_all_devices():
    """DLNA (SSDP) + Cast (mDNS) renderers, discovered in parallel and merged. If one physical
    device exposes BOTH protocols (e.g. an Android TV with a DLNA app + Chromecast built-in), keep
    the DLNA entry -- the MPEG-TS/SOAP path is more reliable there than re-muxing HLS for Cast."""
    from concurrent.futures import ThreadPoolExecutor
    devs = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(discover_dlna_renderers), ex.submit(discover_cast_devices)]
        for f in futs:
            try:
                devs += f.result()
            except Exception:
                pass
    by_host, extras = {}, []
    for d in devs:
        h = d.get("host")
        if not h:
            extras.append(d)
            continue
        cur = by_host.get(h)
        if cur is None or (cur.get("kind") == "cast" and d.get("kind") == "dlna"):
            by_host[h] = d          # prefer DLNA when the same host appears under both protocols
    devs = list(by_host.values()) + extras
    devs.sort(key=lambda d: d["name"].lower())
    return devs


def _tcp_open(host, port, timeout=4):
    """True if a TCP connect to host:port succeeds. The Cast service (:8009) stops listening when
    the TV is in standby/off (it still answers ping), so we check this before connecting -- else
    pychromecast retries the connection forever and the cast hangs showing 'casting' with a blank TV."""
    try:
        with socket.create_connection((host, int(port or 8009)), timeout=timeout):
            return True
    except OSError:
        return False


def _cast_connect(host, port, uuid, model, name, timeout=15):
    """Connect to a Cast device by host (no rediscovery needed) and wait for it to be ready."""
    import pychromecast
    from uuid import UUID
    u = None
    if uuid:
        try:
            u = UUID(uuid)
        except (ValueError, AttributeError, TypeError):
            u = None
    cc = pychromecast.get_chromecast_from_host(
        (host, int(port or 8009), u, model or None, name or None), timeout=timeout)
    cc.wait(timeout=timeout)
    return cc


# The Default Media Receiver app id. Quitting it returns the device to its home screen, BUT
# calling quit on a device that's already idle spuriously RELAUNCHES it (showing the receiver
# splash), so every quit below is guarded by checking this is still the running app.
_CAST_RECEIVER_APP = "CC1AD845"


def cast_quit(host, port, uuid, model, name):
    """Stop playback on a cast device (back to home). Only quits if OUR media receiver is still
    the running app -- quitting an already-idle device just relaunches its splash screen."""
    try:
        cc = _cast_connect(host, port, uuid, model, name, timeout=8)
        if getattr(cc, "app_id", None) == _CAST_RECEIVER_APP:
            cc.quit_app()
            time.sleep(0.5)
        cc.disconnect()
    except Exception as e:
        log(f"cast quit failed: {type(e).__name__}: {str(e)[:80]}")


# Google Cast HLS: ffmpeg reads the source HLS DIRECTLY at the live edge (handling its tokens and
# headers itself) and re-muxes it into small 2s segments in a temp dir. We serve those LOCAL files
# to the Cast receiver. Robust the way the DLNA path is -- no re-fetching source segments that
# expire (no 404s) -- and low-latency (2s chunks regardless of the source's segment size).
_HLS_MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nmedia.m3u8\n"


def _find_ffmpeg():
    """An ffmpeg binary path: a system ffmpeg if present, else the bundled static-ffmpeg."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import static_ffmpeg.run as _r
        # locate static-ffmpeg's ffmpeg directly: get_or_fetch also requires the unused ffprobe, which
        # the frozen build no longer ships, and a missing ffprobe would trigger a runtime download.
        binroot = os.path.join(os.path.dirname(_r.__file__), "bin")
        name = "ffmpeg.exe" if IS_WIN else "ffmpeg"
        for root, _dirs, files in os.walk(binroot):
            if name in files:
                return os.path.join(root, name)
        return _r.get_or_fetch_platform_executables_else_raise()[0]   # not pre-fetched -> fetch it
    except Exception:
        return ""


def _ffmpeg_input_opts(headers):
    """ffmpeg -user_agent / -headers for the replay headers, so a Referer/Cookie-gated source loads."""
    ua, hdr = "", []
    for k, v in (headers or {}).items():
        if k.lower() == "user-agent":
            ua = v
        else:
            hdr.append(f"{k}: {v}")
    opts = []
    if ua:
        opts += ["-user_agent", ua]
    if hdr:
        opts += ["-headers", "".join(h + "\r\n" for h in hdr)]
    return opts


class FfmpegHls:
    """Remux a source HLS URL into a local 2s-segment HLS in a temp dir via ffmpeg. last_hit tracks
    renderer liveness (the renderer pulling our files == the cast is alive)."""
    def __init__(self, src, headers, ffmpeg, buffer=5):
        self.workdir = tempfile.mkdtemp(prefix="slc-cast-")
        self.last_hit = time.time()
        self.buffer = max(2, int(buffer))         # seconds behind live the receiver should start
        # keep enough 2s segments in the playlist to cover the start offset + a little history
        list_size = max(6, self.buffer // 2 + 4)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
               # start muxing fast: cap the input probe so ffmpeg doesn't spend seconds analysing.
               "-fflags", "+genpts", "-analyzeduration", "2000000", "-probesize", "3000000",
               *_ffmpeg_input_opts(headers), "-i", src,
               "-c", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", str(list_size),
               "-hls_flags", "delete_segments+omit_endlist+independent_segments",
               "-hls_segment_type", "mpegts",
               "-hls_segment_filename", os.path.join(self.workdir, "s%05d.ts"),
               os.path.join(self.workdir, "media.m3u8")]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                                     preexec_fn=_PDEATHSIG)   # die with the helper, never orphan

    def ready(self):
        # one segment is enough to LOAD -- the receiver buffers as ffmpeg keeps adding. Waiting for
        # more just delays playback (badly for large-GOP sources where each segment takes seconds).
        try:
            with open(os.path.join(self.workdir, "media.m3u8")) as f:
                return f.read().count("#EXTINF") >= 1
        except OSError:
            return False

    def alive(self):
        return self.proc.poll() is None

    def touch(self):
        self.last_hit = time.time()

    def idle_secs(self):
        return time.time() - self.last_hit

    def file_path(self, name):
        """Path inside the workdir for media.m3u8 or s*.ts only (no traversal)."""
        if "/" in name or ".." in name:
            return None
        if name == "media.m3u8" or (name.startswith("s") and name.endswith(".ts")):
            return os.path.join(self.workdir, name)
        return None

    def stop(self):
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
        except Exception:
            pass
        shutil.rmtree(self.workdir, ignore_errors=True)


def make_ffmpeg_hls_handler(fh, tv=""):
    class HlsHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _foreign(self):
            return bool(tv) and self.client_address[0] not in (tv, "127.0.0.1")

        def _serve(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")   # Cast/Shaka enforces CORS on HLS
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):
            if self._foreign():
                self.send_error(403)
                return
            fh.touch()   # the renderer is actively pulling -> the cast is alive
            name = urllib.parse.urlparse(self.path).path.lstrip("/")
            if name == "live.m3u8":
                self._serve(_HLS_MASTER, "application/vnd.apple.mpegurl")   # master -> media.m3u8
                return
            fp = fh.file_path(name)
            if not fp:
                self.send_error(404)
                return
            try:
                with open(fp, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            if name == "media.m3u8":
                # tell the receiver to start `buffer` seconds behind live (its default is ~3x the
                # target duration); this is the user's latency/stability knob.
                body = _inject_live_start(body, fh.buffer)
            self._serve(body, "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t")

    return HlsHandler


def _inject_live_start(body, offset):
    """Insert an #EXT-X-START tag into a media playlist so the receiver starts `offset` seconds
    behind the live edge (lower offset = less latency). No-op if already present."""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return body
    if offset <= 0 or "#EXT-X-START" in text:
        return body
    out, done = [], False
    for ln in text.splitlines():
        if not done and ln.startswith("#EXTINF"):
            out.append(f"#EXT-X-START:TIME-OFFSET=-{int(offset)},PRECISE=YES")
            done = True
        out.append(ln)
    return ("\n".join(out) + "\n").encode("utf-8") if done else body


# --- Control server for the browser extension --------------------------------
# Control server: lets the Firefox extension trigger casting WITHOUT native
# messaging. The extension just does fetch("http://127.0.0.1:9988/cast?url=").
# Runs in the background; on each /cast it launches a normal --proxy cast.
def serve_control(port):
    self_script = os.path.abspath(__file__)
    TOKEN = load_or_create_token()
    _tok_ok, _tok_detail = secure_token_file()
    log(f"token file: {TOKEN_FILE} ({_tok_detail})")
    if not _tok_ok:
        log(f"ABORT: token file is not owner-only ({_tok_detail}); fix its permissions "
            f"(chmod 600 {TOKEN_FILE}) and restart.")
        raise SystemExit(1)
    log(f"auth token loaded: {TOKEN[:4]}... (len {len(TOKEN)})")   # never log the full secret

    state = _empty_cast_state()  # current cast (for /status + relaunch)
    grace = {"until": 0.0}  # during a quality re-cast the proxy briefly looks dead; don't drop state then
    stopping = {"until": 0.0}  # during a clean Stop, report idle while --stop tears the proxy down (SOAP-then-kill)
    epoch = {"n": 0}           # bumped each (re)cast so a stale title worker can detect it's outdated
    _state_lock = threading.Lock()  # serialize all state read-modify-write (ThreadingHTTPServer = parallel handlers)

    # recover an in-flight cast after a --serve restart (crash / code update / reboot of just the server)
    _saved = load_cast_state()
    _saved_grace = 0.0
    if _saved:
        try:
            _saved_grace = float(_saved.get("grace_until", 0) or 0)
        except (TypeError, ValueError):
            _saved_grace = 0.0
    if _saved and _saved.get("url") and (proxy_alive() or time.time() < _saved_grace):
        for k in state:
            if k in _saved:                       # round-trip explicit values, incl. device=""
                state[k] = _saved[k]
        grace["until"] = max(grace["until"], time.time() + 10)   # re-arm so first /status won't clear it
        log(f"recovered cast state: {state['name'] or state['device']} <- {state['url']}")
    else:
        clear_cast_state()   # stale state from a proxy that already exited

    def build_cast_args(u, d, qy, media, kind="dlna", cast=None, title="", buffer=5):
        """(url, device, quality, media, kind) -> proxy CLI argv. kind 'cast' targets a Chromecast
        (HLS + pychromecast); 'dlna' targets a UPnP renderer (MPEG-TS + SOAP). Replay headers are
        NOT here -- they ride in the env (see launch). --managed = control server owns kill+pidfile."""
        extra = [u, ("--cast" if kind == "cast" else "--proxy"), "--low-latency", "--managed"]
        if d:
            extra += ["--tv", d]
        if qy and qy != "best":
            extra += ["--quality", qy]
        if media:
            extra += ["--media-url", media]
        if title:
            extra += ["--title", title]      # shown on the renderer instead of the raw URL
        if kind == "cast":
            extra += ["--buffer", str(buffer)]   # latency/stability knob
        if kind == "cast" and cast:
            if cast.get("port"):
                extra += ["--cast-port", str(cast["port"])]
            if cast.get("uuid"):
                extra += ["--cast-uuid", cast["uuid"]]
            if cast.get("name"):
                extra += ["--cast-name", cast["name"]]
            if cast.get("model"):
                extra += ["--cast-model", cast["model"]]
        return extra

    def launch(extra, headers=None):
        # frozen build: re-invoke the exe itself (no separate python + script); script build:
        # run the current interpreter on this file. Replay headers ride in the environment
        # (SLC_HEADERS), not argv, so credentials stay out of /proc/<pid>/cmdline.
        cmd = ([sys.executable] + extra) if FROZEN else [_child_python(), self_script] + extra
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
        if headers:
            env = os.environ.copy()
            env["SLC_HEADERS"] = "\n".join(headers)
            kw["env"] = env
        if IS_WIN:
            kw["creationflags"] = 0x00000008 | NO_WINDOW   # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True                 # detach on macOS/Linux
        return subprocess.Popen(cmd, **kw)

    def _spawn_proxy(extra, headers):
        """Stop the current proxy and start a new one, recording its pid SYNCHRONOUSLY so
        proxy_alive() is true the instant the cast is registered (no startup teardown race)."""
        kill_previous_proxy()
        proc = launch(extra, headers)
        _write_pidfile(proc.pid)
        return proc.pid

    def relaunch_current(u, d, qy, media, headers, kind="dlna", cast=None, title="", buffer=5):
        """Re-cast the given stream at quality qy (snapshot args, not live state reads)."""
        return _spawn_proxy(build_cast_args(u, d, qy, media, kind, cast, title, buffer), headers)

    class CtrlHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # no keep-alive: each request closes cleanly

        def log_message(self, *a):
            pass

        def _allow_origin(self):
            # echo ONLY a browser-extension origin (never "*"): lets the extension read
            # responses while blocking arbitrary web pages from reading them.
            o = self.headers.get("Origin", "")
            return o if (o.startswith("moz-extension://") or o.startswith("chrome-extension://")) else ""

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            ao = self._allow_origin()
            if ao:
                self.send_header("Access-Control-Allow-Origin", ao)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_OPTIONS(self):
            # CORS preflight: only extension origins are allowed; a web page's preflight
            # gets no CORS headers, so it can't send the X-LanCast-Token header at all.
            ao = self._allow_origin()
            self.send_response(204)
            if ao:
                self.send_header("Access-Control-Allow-Origin", ao)
                self.send_header("Access-Control-Allow-Headers", "X-LanCast-Token, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authed(self):
            if not secrets.compare_digest(self.headers.get("X-LanCast-Token", ""), TOKEN):
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return False
            return True

        def _host_ok(self):
            # Defense-in-depth vs DNS rebinding: a rebound page becomes same-origin with this
            # loopback server, so only accept a loopback Host. The token is still the primary
            # gate; this rejects the rebinding class before dispatch. Port is ignored.
            host = self.headers.get("Host", "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._json({"ok": False, "error": "forbidden"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._host_ok() or not self._authed():
                return
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            handler = {
                "/devices": self._devices,
                "/qualities": self._qualities,
                "/quality": self._quality,
                "/buffer": self._buffer,
                "/stop": self._stop,
                "/status": self._status,
                "/ping": self._ping,
            }.get(u.path)
            if handler:
                handler(q)
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            # /cast carries the captured request headers (incl. Cookie) in the POST body, not the
            # URL/query string, so credentials never land in a URL or access log (and the helper
            # forwards them to streamlink via env + a 0600 config file, never argv).
            if not self._host_ok() or not self._authed():
                return
            u = urllib.parse.urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > 262144:   # legit /cast bodies are a few KB; cap to bound memory
                self._json({"ok": False, "error": "request too large"}, 413)
                return
            body = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
            q = urllib.parse.parse_qs(body)
            if u.path == "/cast":
                self._cast(q)
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def _cast(self, q):
            url = (q.get("url", [""])[0]).strip()
            device = (q.get("device", [""])[0]).strip()   # chosen TV host/IP
            name = (q.get("name", [""])[0]).strip()        # friendly name (for status)
            title = (q.get("title", [""])[0]).strip()      # stream title (for status)
            quality = _safe_quality(q.get("quality", [""])[0])
            media = (q.get("media", [""])[0]).strip()      # sniffed direct media URL (optional)
            headers = parse_replay_headers(q.get("headers", [""])[0])
            if not (url.startswith("http://") or url.startswith("https://")):
                self._json({"ok": False, "error": "invalid URL: " + url[:60]})
                return
            if media and not (media.startswith("http://") or media.startswith("https://")):
                media = ""    # ignore a bogus media URL -> fall back to resolving the page
            # ignore an in-flight proxy during a Stop window: it's being torn down, so a re-cast
            # should start a fresh one rather than be told it's "already" casting (empty target).
            if proxy_alive() and time.time() >= stopping["until"]:
                with _state_lock:
                    snap = {k: state[k] for k in _CAST_SNAP_FIELDS}
                self._json({"ok": True, "already": True, **snap})
                return
            if media:
                # pre-flight a sniffed source so a DRM/offline stream fails fast with a clear
                # message instead of launching a cast that leaves the TV black. Headers (Cookie)
                # reach the probe via a private 0600 --config file, not argv.
                cfg = _write_header_config(headers)
                try:
                    ok, reason, detail = probe_stream(_proto_target(media), cfg)
                finally:
                    _safe_unlink(cfg)
                if not ok:
                    log(f"control: probe rejected ({reason}) {_redact_url(media)[:80]} -> {detail[:120]}")
                    self._json({"ok": False, "reason": reason, "error": detail})
                    return
            # pick the cast protocol: prefer the discovered device's kind, fall back to the hint
            # the extension sends, default DLNA. For cast, carry the device's connect details.
            kind = (q.get("kind", [""])[0]).strip().lower()
            dev = peek_device_by_host(device)
            if dev and dev.get("kind"):
                kind = dev["kind"]
            if kind not in ("cast", "dlna"):
                kind = "dlna"
            cinfo = {}
            if kind == "cast":
                cinfo = {"port": (dev or {}).get("port", 8009), "uuid": (dev or {}).get("uuid", ""),
                         "name": (dev or {}).get("name", name), "model": (dev or {}).get("model", "")}
            buffer = _safe_buffer(q.get("buffer", [""])[0])
            extra = build_cast_args(url, device, quality, media, kind, cinfo, title, buffer)
            already, this_epoch = None, 0
            with _state_lock:
                if proxy_alive() and time.time() >= stopping["until"]:   # re-check atomically
                    already = {k: state[k] for k in _CAST_SNAP_FIELDS}
                else:
                    stopping["until"] = 0  # a fresh cast cancels any pending stop-suppression
                    _spawn_proxy(extra, headers)        # kill old (if any) + launch + record pid
                    epoch["n"] += 1
                    this_epoch = epoch["n"]
                    grace["until"] = time.time() + 10   # cover the child's startup window
                    state.update(url=url, device=device, name=name, title=title, quality=quality,
                                 media=media, headers=headers, epoch=this_epoch, kind=kind, cast=cinfo,
                                 buffer=buffer)
                    save_cast_state(state, grace["until"])
            if already is not None:
                self._json({"ok": True, "already": True, **already})
                return

            def _title_worker(u2, ep):
                t = fetch_stream_title(u2)
                with _state_lock:
                    if t and state.get("url") == u2 and state.get("epoch") == ep:  # still this cast
                        state["title"] = t
                        save_cast_state(state, grace["until"])
            threading.Thread(target=_title_worker, args=(url, this_epoch), daemon=True).start()

            log(f"control: cast {_redact_url(url)} -> {name or device or 'default'}")
            self._json({"ok": True, "url": url, "device": device, "name": name,
                        "title": title, "quality": quality})

        def _devices(self, q):
            if q.get("fresh", [""])[0] == "1":
                _refresh_devices()          # live scan -> drops powered-off devices
            self._json({"ok": True, "devices": cached_devices()})

        def _qualities(self, q):
            qurl = (q.get("url", [""])[0]).strip()
            m = stream_meta(qurl) if qurl else {"qualities": []}
            self._json({"ok": True, "qualities": m.get("qualities", [])})

        def _relaunch_locked(self, **changes):
            """Apply state changes and re-cast the current stream (caller holds _state_lock).
            Returns the response dict. Shared by /quality and /buffer."""
            if not state["url"]:
                state.update(changes)
                return {"ok": True, "casting": False}
            state.update(changes)
            u2, d2 = state["url"], state["device"]
            m2, h2 = state["media"], state["headers"]
            k2, c2 = state.get("kind", "dlna"), state.get("cast") or {}
            t2, b2 = state.get("title", ""), state.get("buffer", 5)
            epoch["n"] += 1
            state["epoch"] = epoch["n"]
            grace["until"] = time.time() + 10        # tolerate the brief proxy gap during relaunch
            save_cast_state(state, grace["until"])
            relaunch_current(u2, d2, state["quality"], m2, h2, k2, c2, t2, b2)
            return {"ok": True, "casting": True}

        def _quality(self, q):
            val = _safe_quality(q.get("value", [""])[0])
            with _state_lock:
                if state["url"] and val == state["quality"]:
                    resp = {"ok": True, "quality": val, "casting": True, "unchanged": True}  # no-op
                else:
                    resp = self._relaunch_locked(quality=val)
                resp["quality"] = val
            self._json(resp)

        def _buffer(self, q):
            val = _safe_buffer(q.get("value", [""])[0])
            with _state_lock:
                if state["url"] and val == state.get("buffer", 5):
                    resp = {"ok": True, "buffer": val, "casting": True, "unchanged": True}  # no-op
                else:
                    resp = self._relaunch_locked(buffer=val)
                resp["buffer"] = val
            self._json(resp)

        def _stop(self, q):
            with _state_lock:
                dev = state.get("device") or ""               # the casting TV, capture before clearing
                kind = state.get("kind", "dlna")
                cinfo = state.get("cast") or {}
                pid = _read_pidfile()                          # the EXACT proxy to stop
                state.update(_empty_cast_state())
                grace["until"] = 0                            # cancel any in-flight recast grace
                clear_cast_state()
                # Report idle right away (so /status can't flip the popup back to "casting"),
                # but let --stop tear the proxy down CLEANLY: tell the TV to stop first (SOAP for
                # DLNA, quit-app for cast), then kill the proxy. --kill-pid so a slow stop can't
                # later kill a DIFFERENT proxy that a quick re-cast started.
                stopping["until"] = time.time() + 6
                argv = ["--stop"]
                if dev:
                    argv += ["--tv", dev]
                if kind == "cast":
                    argv += ["--cast"]
                    if cinfo.get("port"):
                        argv += ["--cast-port", str(cinfo["port"])]
                    if cinfo.get("uuid"):
                        argv += ["--cast-uuid", cinfo["uuid"]]
                    if cinfo.get("name"):
                        argv += ["--cast-name", cinfo["name"]]
                    if cinfo.get("model"):
                        argv += ["--cast-model", cinfo["model"]]
                if pid is not None:
                    argv += ["--kill-pid", str(pid)]
                launch(argv)
            self._json({"ok": True, "stopped": True})

        def _status(self, q):
            with _state_lock:
                now = time.time()
                if now < stopping["until"]:
                    # a clean Stop is in progress: report idle and leave the proxy alone so
                    # --stop can do SOAP-then-kill without the TV erroring on a cut stream.
                    alive = False
                    snap = {k: _empty_cast_state()[k] for k in _CAST_SNAP_FIELDS}
                else:
                    alive = proxy_alive() or now < grace["until"]   # grace: re-cast in flight
                    # orphan reconcile: a live proxy we have no target for (state lost across a
                    # --serve restart/crash) can't be represented -> stop it and report idle,
                    # instead of a phantom "casting to nowhere". The grace check keeps a legit
                    # quality re-cast (url still set) from tripping this.
                    if alive and not state.get("url") and now >= grace["until"]:
                        kill_previous_proxy()
                        try:
                            os.remove(PIDFILE)
                        except OSError:
                            pass
                        alive = False
                    if not alive:
                        state.update(_empty_cast_state())
                        clear_cast_state()
                    snap = {k: state[k] for k in _CAST_SNAP_FIELDS}
            self._json({"ok": True, "casting": alive, **snap})

        def _ping(self, q):
            self._json({"ok": True, "pong": True})

    def _dev_loop():
        while True:
            try:
                _refresh_devices()
            except Exception:
                pass
            time.sleep(25)
    threading.Thread(target=_dev_loop, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), CtrlHandler)
    log(f"control server on http://127.0.0.1:{port}  (/cast?url=  /stop  /ping  /devices)")
    httpd.serve_forever()


# --- Entry points: per-mode runners (stop/cast/proxy) and main() dispatch ---
def run_stop(args):
    target = int(args.kill_pid) if args.kill_pid.isdigit() else 0
    managed = bool(args.kill_pid)
    # DLNA: tell the TV to Stop (clean) BEFORE killing the proxy. Cast targets are NOT
    # pre-quit here -- the proxy's own (guarded) atexit quits the receiver when we kill it;
    # a second quit on an already-idle device would spuriously relaunch its splash. Only act
    # if our proxy is still the live one (a quick re-cast may have replaced it).
    if (not args.cast) and ((not managed) or (target and _pid_is_proxy(target))):
        try:
            soap(discover_control_url(args.tv, DMR_PORT), "Stop", "<InstanceID>0</InstanceID>")
        except Exception:
            pass   # TV unreachable/off -> still tear the proxy down below
        time.sleep(0.4)
    if managed:
        # kill EXACTLY the proxy the control server named (its guarded atexit quits the cast),
        # and touch the pidfile only if it still refers to it. STATEFILE was already cleared
        # by the control server under its lock, so don't clear it here.
        if target and _pid_is_proxy(target):
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                try:
                    os.kill(target, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        if _read_pidfile() == target:
            _safe_unlink(PIDFILE)
    else:
        kill_previous_proxy()
        if args.cast:
            # standalone fallback: if no live proxy quit it, stop the receiver directly (guarded)
            cast_quit(args.tv, args.cast_port, args.cast_uuid, args.cast_model, args.cast_name)
        _safe_unlink(PIDFILE)
        clear_cast_state()   # standalone `--stop` also clears the persisted cast
    return


def _cast_monitor(cc, mc, fh, m3u8, cast_title, stop_evt, stop_why):
    """Steady-state cast watchdog: tolerate buffering/blips, re-LOAD a few times, and
    return when the cast should be torn down (TV stopped, source ended, unrecoverable)."""
    POLL = 1.5
    MAX_RETRIES, STALL_POLLS = 3, 3   # ~4.5s of trouble -> re-LOAD; give up only after 3 tries
    played, startup, trouble, retries = False, 0, 0, 0
    try:
        while True:
            if stop_evt.wait(POLL):      # receiver PUSHED a stop -> react immediately
                log(f"cast: stopped on the TV ({stop_why[0]}); shutting down")
                break
            if not fh.alive():           # ffmpeg/source ended -> stop
                log("cast: remux ended (source stopped); shutting down")
                break
            try:
                connected = bool(cc.socket_client and cc.socket_client.is_connected)
            except Exception:
                connected = True
            app = getattr(cc, "app_id", None)
            idle = fh.idle_secs()
            st = ""
            if app == _CAST_RECEIVER_APP:
                try:
                    mc.update_status()
                    st = mc.status.player_state if mc.status else ""
                except Exception:
                    st = ""
            if not played:
                if st == "PLAYING" or idle < 6:
                    log("cast: playing")
                    played = True
                elif idle > 12:
                    log(f"cast: failed to start ({int(idle)}s); shutting down")
                    break
                else:
                    startup += 1
                    if startup >= 24:
                        log("cast: never started; shutting down")
                        break
                continue
            # --- resilient steady state: a small buffer that drains, or a brief control-socket
            # blip, should recover -- tolerate it and re-LOAD up to MAX_RETRIES times before
            # tearing down, instead of cutting out on the first stall. A genuine stop (the user
            # exiting on the TV) leaves a distinct trail -> we still bail fast for those, so a
            # retry never resurrects a stream the user deliberately closed.
            if not connected:
                # the cast CONTROL socket dropped (the TV may still be pulling segments over HTTP).
                # reconnect a few times; don't re-LOAD here so we never interrupt a stream that's
                # actually still playing on a transient blip.
                trouble += 1
                if trouble >= STALL_POLLS:
                    if idle > 10:          # control lost AND the TV stopped pulling -> really gone
                        log(f"cast: connection lost and TV idle ({int(idle)}s); shutting down")
                        break
                    if retries >= MAX_RETRIES:
                        log(f"cast: not reconnected after {MAX_RETRIES} retries; shutting down")
                        break
                    retries += 1; trouble = 0
                    log(f"cast: reconnecting to the TV -- retry {retries}/{MAX_RETRIES}")
                    try:
                        cc.wait(timeout=8)
                    except Exception:
                        pass
                continue
            if app != _CAST_RECEIVER_APP:   # our player is no longer foreground -> user exited / another app
                log(f"cast: receiver no longer active on the TV (app={app}); shutting down")
                break
            if st == "PLAYING":
                trouble, retries = 0, 0      # healthy playback refreshes the retry budget
                continue
            if st == "IDLE":                # the receiver reports the media stopped -> user stopped
                log("cast: media stopped on the TV; shutting down")
                break
            if idle > 8:                    # not playing AND the TV stopped pulling -> it left the
                log(f"cast: TV stopped pulling ({int(idle)}s) and not playing; shutting down")
                break                       # stream (a buffer drain keeps pulling, so this isn't one)
            # connected, our player, still pulling, but BUFFERING/stalled (e.g. a small buffer
            # drained) -> give it a moment, then nudge it with a fresh LOAD before giving up.
            trouble += 1
            if trouble >= STALL_POLLS:
                if retries >= MAX_RETRIES:
                    log(f"cast: still buffering after {MAX_RETRIES} retries; shutting down")
                    break
                retries += 1; trouble = 0
                log(f"cast: buffering -- reload retry {retries}/{MAX_RETRIES}")
                try:
                    mc.play_media(m3u8, "application/vnd.apple.mpegurl", title=cast_title, stream_type="LIVE")
                    try:
                        mc.block_until_active(timeout=8)
                    except Exception:
                        pass
                    fh.touch()
                except Exception:
                    pass
    except KeyboardInterrupt:
        pass


def run_cast(args):
    if not args.managed:
        kill_previous_proxy()
    _arm_pidfile_cleanup()
    ip = lan_ip(args.tv)
    hdrs = [h for h in os.environ.get("SLC_HEADERS", "").split("\n") if h]
    hdrs += list(args.add_header or [])
    hdr_map = {}
    for h in hdrs:
        if "=" in h:
            k, v = h.split("=", 1)
            hdr_map[k] = v
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        log("cast: ffmpeg not found (needed to remux HLS for Chromecast). pip install static-ffmpeg")
        _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)
    # source HLS URL: the sniffed media URL directly, else resolve the page via streamlink.
    if args.media_url:
        src = args.media_url
    else:
        cfg = _write_header_config([f"{k}={v}" for k, v in hdr_map.items()])
        try:
            out = subprocess.run(_streamlink_cmd("--stream-url", *(["--config", cfg] if cfg else []),
                                                 "--", args.url, args.quality),
                                 capture_output=True, text=True, creationflags=NO_WINDOW)
        finally:
            _safe_unlink(cfg)
        src = (out.stdout or "").strip()
        if not src:
            log(f"cast: streamlink could not resolve {_redact_url(args.url)}")
            _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)
    fh = FfmpegHls(src, hdr_map, ffmpeg, buffer=args.buffer)
    atexit.register(fh.stop)           # backstop: SIGTERM (-> SystemExit) / normal exit kill ffmpeg
    for _ in range(50):                # wait up to ~25s for ffmpeg to produce a couple segments
        if fh.ready() or not fh.alive():
            break
        time.sleep(0.5)
    if not fh.ready():
        log("cast: ffmpeg produced no playable output (dead/offline source?)")
        fh.stop(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)
    httpd = ThreadingHTTPServer((ip, args.port), make_ffmpeg_hls_handler(fh, tv=args.tv))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    m3u8 = f"http://{ip}:{args.port}/live.m3u8"
    log(f"HLS (ffmpeg remux) at {m3u8} -> cast to {args.cast_name or args.tv}")
    if not _tcp_open(args.tv, args.cast_port):
        log(f"cast: {args.tv}:{args.cast_port} not reachable -- is the TV on (not in standby)?")
        httpd.shutdown(); fh.stop(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)
    try:
        cc = _cast_connect(args.tv, args.cast_port, args.cast_uuid, args.cast_model, args.cast_name)
    except Exception as e:
        log(f"cast connect failed: {type(e).__name__}: {str(e)[:80]}")
        httpd.shutdown(); fh.stop(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)

    def _quit():
        try:
            if getattr(cc, "app_id", None) == _CAST_RECEIVER_APP:   # only if OUR receiver is up
                cc.quit_app()
        except Exception:
            pass
        try:
            cc.disconnect()
        except Exception:
            pass
    atexit.register(_quit)
    mc = cc.media_controller
    # Listen for the receiver's PUSH notifications so we react INSTANTLY when the user stops on
    # the TV (idle_reason CANCELLED/FINISHED) instead of waiting for the slow poll-based checks.
    stop_evt = threading.Event()
    stop_why = [""]

    class _MediaListener:
        def new_media_status(self, status):
            ps = getattr(status, "player_state", "") or ""
            ir = getattr(status, "idle_reason", None)
            if ps == "IDLE" and ir in ("CANCELLED", "FINISHED", "INTERRUPTED"):
                stop_why[0] = f"media {ir}"
                stop_evt.set()

        def load_media_failed(self, item, error_code):
            stop_why[0] = "load failed"
            stop_evt.set()

    try:
        mc.register_status_listener(_MediaListener())
    except Exception:
        pass
    cast_title = args.title or args.cast_name or "Streaming LAN Cast"
    mc.play_media(m3u8, "application/vnd.apple.mpegurl", title=cast_title, stream_type="LIVE")
    try:
        mc.block_until_active(timeout=12)
    except Exception:
        pass
    log("cast: LOAD sent")
    fh.last_hit = time.time()   # start the "TV is pulling" clock NOW (not at ffmpeg launch, which
    #                             can take seconds to produce the first segment on slow sources)
    _cast_monitor(cc, mc, fh, m3u8, cast_title, stop_evt, stop_why)
    _quit()
    httpd.shutdown()
    fh.stop()
    _safe_unlink(PIDFILE)
    clear_cast_state()
    os._exit(0)


def run_proxy(args):
    control_url = discover_control_url(args.tv, DMR_PORT)
    log(f"TV AVTransport: {control_url}")

    if not args.managed:
        kill_previous_proxy()  # standalone re-run resyncs to live edge (control server does this itself)
    _arm_pidfile_cleanup()
    ip = lan_ip(args.tv)
    local_url = f"http://{ip}:{args.port}/live.ts"
    sl_flags = ["--hls-live-edge", str(args.live_edge),
                "--hls-segment-stream-data",
                "--stream-segment-threads", str(args.threads),
                "--hls-playlist-reload-time", "live-edge",
                "--ringbuffer-size", args.ringbuffer]
    # a sniffed direct media URL (from the extension) takes precedence over resolving the page;
    # prefix the protocol plugin so streamlink uses it regardless of the URL's extension/query.
    target = _proto_target(args.media_url) if args.media_url else args.url
    extra_sl = []
    # replay headers arrive via env (SLC_HEADERS, set by the control server) or --add-header
    # (standalone CLI). Route them through a private 0600 --config file so a Cookie never
    # sits in this process's or streamlink's argv (/proc/<pid>/cmdline is world-readable).
    hdrs = [h for h in os.environ.get("SLC_HEADERS", "").split("\n") if h]
    hdrs += list(args.add_header or [])
    cfg = _write_header_config(hdrs)
    if cfg:
        extra_sl += ["--config", cfg]
        atexit.register(lambda p=cfg: _safe_unlink(p))
    if args.insecure:
        extra_sl += ["--http-no-ssl-verify"]
    httpd = ThreadingHTTPServer((ip, args.port), make_handler(target, args.quality, sl_flags, extra_sl, tv=args.tv))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"live HTTP server at {local_url} (live-edge={args.live_edge}, quality={args.quality})")
    set_and_play(control_url, local_url, args.title or f"LIVE: {args.url}")
    log("pushed (live). Ctrl+C stops the server and playback.")
    played, gone, retries = False, 0, 0
    DLNA_MAX_RETRIES = 3
    try:
        while True:
            time.sleep(2)
            st = transport_state(control_url)
            if st in ("PLAYING", "TRANSITIONING"):
                played, gone, retries = True, 0, 0   # TV is active (playing or buffering)
            elif played:
                gone += 1                        # not playing after it started
                if gone >= 3:  # ~6s
                    if st in ("STOPPED", "NO_MEDIA"):
                        log("TV stopped externally; shutting down proxy")  # clean stop -> user stopped
                        break
                    # st unreadable/error -> renderer briefly unreachable (a connection blip, not a
                    # deliberate stop) -> re-push a few times before giving up, so it recovers.
                    if retries >= DLNA_MAX_RETRIES:
                        log(f"DLNA: renderer unreachable after {DLNA_MAX_RETRIES} retries; shutting down")
                        break
                    retries += 1; gone = 0
                    log(f"DLNA: renderer unreachable -- re-push retry {retries}/{DLNA_MAX_RETRIES}")
                    try:
                        set_and_play(control_url, local_url, args.title or f"LIVE: {args.url}")
                    except Exception:
                        pass
    except KeyboardInterrupt:
        try:
            soap(control_url, "Stop", "<InstanceID>0</InstanceID>")
        except Exception:
            pass
    httpd.shutdown()
    try:
        os.remove(PIDFILE)
    except OSError:
        pass
    clear_cast_state()   # clean auto-exit (TV stopped) -> drop the persisted cast so no phantom recovery
    os._exit(0)


def main():
    # frozen build acting as streamlink: `<exe> --__sl <streamlink args...>` -> run the bundled CLI
    if FROZEN and len(sys.argv) > 1 and sys.argv[1] == "--__sl":
        sys.argv = ["streamlink"] + sys.argv[2:]
        from streamlink_cli.main import main as _sl_main
        sys.exit(_sl_main())

    _install_safe_url_opener()   # http(s) only for every urllib fetch (no file://, no ftp://)
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="stream page URL (any site streamlink supports)")
    ap.add_argument("--tv", default=DEFAULT_TV, help="DLNA renderer IP (the extension sets this per device)")
    ap.add_argument("--quality", default="best", help="streamlink quality (best, 720p60, ...)")
    ap.add_argument("--proxy", action="store_true", help="serve as MPEG-TS via our live HTTP server")
    ap.add_argument("--port", type=int, default=8088, help="local proxy port")
    ap.add_argument("--live-edge", type=int, default=2, help="HLS segments behind live edge (1=lowest latency, default 2)")
    ap.add_argument("--threads", type=int, default=3, help="segment download threads")
    ap.add_argument("--ringbuffer", default="8M", help="streamlink ringbuffer size")
    ap.add_argument("--low-latency", action="store_true", help="aggressive: live-edge 1")
    ap.add_argument("--stop", action="store_true", help="just stop playback on the TV")
    ap.add_argument("--media-url", default="", help="direct media URL (HLS/DASH/file) to cast instead of resolving the page")
    ap.add_argument("--title", default="", help="title to show on the renderer (defaults to the URL)")
    ap.add_argument("--buffer", type=int, default=5, help="cast: seconds behind live to start (lower = less delay)")
    ap.add_argument("--add-header", action="append", default=[], metavar="NAME=VALUE",
                    help="extra HTTP header for streamlink, repeatable (e.g. Referer=..., Cookie=...)")
    ap.add_argument("--insecure", action="store_true", help="don't verify TLS for the media fetch")
    ap.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)   # launched by the control server (it owns kill + pidfile)
    ap.add_argument("--kill-pid", default="", help=argparse.SUPPRESS)           # --stop: kill exactly this proxy pid (managed stop)
    ap.add_argument("--cast", action="store_true", help=argparse.SUPPRESS)      # target a Google Cast device (HLS + pychromecast)
    ap.add_argument("--cast-port", type=int, default=8009, help=argparse.SUPPRESS)
    ap.add_argument("--cast-uuid", default="", help=argparse.SUPPRESS)
    ap.add_argument("--cast-name", default="", help=argparse.SUPPRESS)
    ap.add_argument("--cast-model", default="", help=argparse.SUPPRESS)
    ap.add_argument("--serve", action="store_true", help="run the local control server for the Firefox extension")
    ap.add_argument("--control-port", type=int, default=9988, help="control server port (localhost)")
    args = ap.parse_args()
    if args.low_latency:
        args.live_edge = 1

    if args.serve:
        serve_control(args.control_port)
    elif args.stop:
        run_stop(args)
    elif args.cast:
        run_cast(args)
    elif args.proxy:
        run_proxy(args)


if __name__ == "__main__":
    main()
