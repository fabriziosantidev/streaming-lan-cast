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

Google Cast (--cast, Chromecast / Android TV): an authenticating HLS reverse-proxy
serves the source to a branded Cast receiver, whose native player handles playback,
driven over the Cast protocol via pychromecast.

The control server (--serve) is what the browser extension talks to: it discovers
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

HELPER_VERSION = "0.5.0"   # reported to the extension via /ping; bump with manifest.json + the .iss
DEFAULT_URL = ""           # the extension passes the stream URL per cast
DEFAULT_TV = ""            # the extension passes the chosen renderer's IP per cast
DMR_PORT = 9197
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
PIDFILE = os.path.join(tempfile.gettempdir(), "streaming-lan-cast-proxy.pid")
STATEFILE = os.path.join(tempfile.gettempdir(), "streaming-lan-cast-state.json")  # survives --serve restarts
PROXY_LOG = os.path.join(tempfile.gettempdir(), "streaming-lan-cast-cast.log")  # last cast proxy's output (debug)
PROXY_ERR_FILE = os.path.join(tempfile.gettempdir(), "streaming-lan-cast-proxy-error.json")  # proxy->control: source expired (410/403)
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
# dies by any means, even SIGKILL or a crash, which run no Python cleanup. Without this an ffmpeg
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
              # that its segments need. Without this the segments 404/403 (the cast HLS proxy
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
    it spawns the proxy (under its state lock), so proxy_alive() is true immediately, closing
    the window where state says 'casting' but the pidfile is still empty (a /status poll there
    would read 'stopped' and tear the fresh cast down)."""
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        pass


def _remove_pidfile():
    """Remove PIDFILE, but only if it still names THIS process, so a proxy exiting late can't
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
    """True only if `pid` is alive AND is one of OUR cast proxies, not merely a process that
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
_CAST_SNAP_FIELDS = ("url", "device", "name", "title", "quality")


def _empty_cast_state():
    return {"url": "", "device": "", "name": "", "title": "", "quality": "best",
            "media": "", "headers": [], "epoch": 0, "kind": "dlna", "cast": {}}


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


def _yt_codec_family(vc):
    vc = vc or ""
    if vc.startswith("av01"):
        return "AV1"
    if vc.startswith(("vp9", "vp09")):
        return "VP9"
    if vc.startswith("avc"):
        return "H.264"
    return (vc.split(".")[0] or "?")[:6]


def _youtube_meta(url):
    """{title, author, qualities, matrix} for a YouTube VOD via yt-dlp (streamlink only sees 360p there).
    'qualities' is the flat height ladder the classic dropdown consumes; 'matrix' is the full
    resolution -> fps -> [codec/range/itag/bitrate] tree the cascading quality menu consumes. Returns
    None for a live stream or on failure, so stream_meta falls back to streamlink (live's own ladder)."""
    cmd = _ytdlp_cmd()
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd + ["-J", "--no-warnings", "--no-playlist", "--", url],
                             capture_output=True, text=True, timeout=45)
        info = json.loads(out.stdout or "")
    except Exception:
        return None
    if info.get("is_live"):
        return None
    tree = {}
    for f in info.get("formats") or []:
        if f.get("vcodec") in (None, "none") or f.get("acodec") not in (None, "none"):
            continue
        if not (f.get("url") and f.get("protocol") == "https" and f.get("height")):
            continue
        rng = "HDR" if "HDR" in (f.get("dynamic_range") or "").upper() else "SDR"
        tree.setdefault(f["height"], {}).setdefault(int(f.get("fps") or 0), []).append(
            {"codec": _yt_codec_family(f.get("vcodec")), "range": rng,
             "itag": f["format_id"], "tbr": round((f.get("tbr") or 0) / 1000, 1)})
    cr = {"AV1": 2, "H.264": 1, "VP9": 0}
    matrix = []
    for h in sorted(tree, reverse=True):
        fpss = [{"fps": fp, "opts": sorted(tree[h][fp], key=lambda o: (-cr.get(o["codec"], 0), o["range"] != "SDR", -o["tbr"]))}
                for fp in sorted(tree[h], reverse=True)]
        matrix.append({"res": h, "fps": fpss})
    return {"title": (info.get("title") or "").strip(),
            "author": (info.get("uploader") or "").strip(),
            "qualities": [f"{r['res']}p" for r in matrix],
            "matrix": matrix}


def stream_meta(url):
    """One `streamlink --json` call: returns {title, author, qualities[]}.
    Cached per-url (~90s) so /qualities and the /cast title worker reuse it.
    Qualities are real renditions (best/worst aliases dropped), highest first.
    YouTube VODs are resolved with yt-dlp instead (streamlink caps them at 360p)."""
    now = time.time()
    with _meta_lock:
        c = _meta_cache.get(url)
        if c and now - c[0] < 90:
            return c[1]
    if "youtube.com" in url or "youtu.be" in url:
        ym = _youtube_meta(url)
        if ym is not None:
            with _meta_lock:
                _meta_cache[url] = (now, ym)
                _meta_cache.move_to_end(url)
                while len(_meta_cache) > _META_CACHE_MAX:
                    _meta_cache.popitem(last=False)
            return ym
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


def _source_reachable(url, headers):
    """True if a small authenticated GET of url succeeds, i.e. the cast's own reverse-proxy could serve
    it. Used to override a streamlink 'unplayable' verdict for an already-resolved CDN playlist that
    streamlink's resolver rejects but the proxy fetches fine (some hosts only hand their fragmented-MP4
    media playlist to a plain GET). headers is the replay list ('Name=Value'); a manifest needs only
    Referer/Origin/User-Agent, so the Cookie is not sent. A media playlist that declares SAMPLE-AES key
    delivery is reported unreachable so the DRM rejection stands."""
    try:
        h = {}
        for x in (headers or []):
            name, sep, value = x.partition("=")
            if sep and name.lower() in ("referer", "origin", "user-agent"):
                h[name] = value
        h.setdefault("User-Agent", "Mozilla/5.0")
        h["Range"] = "bytes=0-65535"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=6) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            body = r.read(200_000)
    except Exception:
        return False
    if body[:64].lstrip().startswith(b"#EXTM3U"):
        return b"SAMPLE-AES" not in body          # plain/AES-128 HLS is castable; SAMPLE-AES is DRM
    return "mpegurl" in ct or ct.startswith("video/")


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
_ITAG_RE = re.compile(r"^itag:[A-Za-z0-9]+$")               # the quality menu picks an exact yt-dlp format id


def _safe_quality(value):
    """Quality selector, validated. streamlink takes it as a trailing POSITIONAL, so an
    unvalidated value like '--http-proxy=...' could masquerade as a streamlink option; restrict
    it to a conservative charset and fall back to 'best'. 'itag:<id>' is also allowed (the quality menu
    picks one); run_cast intercepts it before streamlink, so it never reaches streamlink as an arg."""
    value = (value or "").strip()
    return value if (value and (_QUALITY_RE.match(value) or _ITAG_RE.match(value))) else "best"


def _safe_unlink(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _write_header_config(headers):
    """Write replay headers to a PRIVATE (0600) streamlink --config file and return its path
    ('' if none). Credentials (Cookie) then reach streamlink via the config file instead of argv:
    argv is world-readable via /proc/<pid>/cmdline, a config file is owner-only."""
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
    """A URL with its query string stripped, for logging. Signed/tokenised query params don't
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
def make_handler(target, quality, sl_flags, extra_sl=(), tv="", hold=0.0, hold_ref=None):
    # hold_ref, if given, is a 1-element list whose value the adaptive monitor mutates at runtime;
    # the writer reads it each iteration so the hold-delay depth can grow/shrink live. Falls back
    # to the fixed `hold` when absent (e.g. the DLNA path).
    def _hold():
        return hold_ref[0] if hold_ref is not None else hold
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
            # the TS-mode Cast receiver pulls this with fetch() from its https origin, which
            # enforces CORS (DLNA players don't care; the header is inert for them)
            self.send_header("Access-Control-Allow-Origin", "*")
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
            log(f"TV connected ({self.client_address[0]}) -> streamlink target: {_redact_url(target)} "
                f"(continuous live, hold={_hold():.0f}s)")
            self._send_live_headers()
            cmd = _streamlink_cmd(*sl_flags, *extra_sl, "--stdout", "--", target, quality)
            # DLNA-faithful robustness, in two parts:
            #  (1) ONE HTTP response kept open indefinitely; a READER thread RESPAWNS streamlink
            #      across its restarts instead of ending the response when the source hiccups.
            #      (Ending it made the player rebuild from scratch on every blip:
            #      currentTime reset to 0 and stuck on "loading".)
            #  (2) a server-side HOLD buffer (the set-top box's reservoir): the WRITER releases
            #      only bytes that arrived >= `hold` seconds ago, so during a source outage it
            #      keeps feeding the client from the reserve and the receiver never underruns.
            #      hold=0 -> release immediately.
            import collections
            Q = collections.deque()          # (arrival_monotonic, chunk)
            qlock = threading.Lock()
            done = threading.Event()          # set when the source is dead OR the client left
            MAXQ = 64 * 1024 * 1024
            state = {"grand": 0, "gen": 0, "reader_bytes": 0}

            def reader():
                duds = 0
                while not done.is_set():
                    state["gen"] += 1
                    errf = tempfile.TemporaryFile()
                    try:
                        sl = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                              creationflags=NO_WINDOW)
                    except Exception as e:
                        log(f"proxy: streamlink spawn failed: {type(e).__name__}: {str(e)[:80]}")
                        break
                    seg = 0
                    try:
                        while not done.is_set():
                            chunk = sl.stdout.read(188 * 350)
                            if not chunk:
                                break                     # streamlink ended -> respawn at live edge
                            with qlock:
                                Q.append((time.monotonic(), chunk))
                                state["reader_bytes"] += len(chunk)
                                over = state["reader_bytes"]  # trim if the queue balloons
                                while Q and over > MAXQ:
                                    over -= len(Q.popleft()[1])
                                state["reader_bytes"] = over
                            seg += len(chunk)
                    finally:
                        try:
                            sl.terminate()
                            try:
                                sl.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                sl.kill(); sl.wait()
                        except Exception:
                            pass
                        try:
                            if sl.stdout:
                                sl.stdout.close()
                        except Exception:
                            pass
                        if seg < 100000:
                            try:
                                errf.seek(0)
                                err = errf.read().decode("utf-8", "replace").strip()[-400:]
                                log(f"proxy: streamlink run {state['gen']} produced {seg}B (source hiccup) {err[-160:]}")
                            except Exception:
                                pass
                        try:
                            errf.close()
                        except Exception:
                            pass
                    if done.is_set():
                        break
                    duds = duds + 1 if seg < 100000 else 0
                    if duds >= 40:            # source dead for a long stretch -> stop
                        log("proxy: source not delivering; ending stream")
                        break
                    time.sleep(0.25 if duds else 0.05)    # brief pause, then respawn at live edge
                done.set()

            rt = threading.Thread(target=reader, daemon=True)
            rt.start()
            # HOLD-DELAY (DVR-style time-shift): release only bytes that arrived >= `hold` seconds
            # ago. In steady state this just delivers the stream `hold` seconds late (so the
            # receiver sits ~hold behind the true live edge, reading already-settled content like a
            # PC player does). The payoff is during a SOURCE STALL: no new bytes arrive, but the
            # "arrived >= hold ago" cutoff keeps advancing through the reserve already in Q, so the
            # server KEEPS FEEDING the receiver and it never underruns for stalls shorter than hold.
            # No receiver-side seeking needed (that fought mpegts and caused reload storms).
            try:
                while True:
                    cutoff = time.monotonic() - _hold()
                    out = []
                    with qlock:
                        while Q and (Q[0][0] <= cutoff or done.is_set()):
                            out.append(Q.popleft()[1])
                            if len(out) >= 64:
                                break
                    if out:
                        for c in out:
                            self.wfile.write(c)
                            state["grand"] += len(c)
                    elif done.is_set():
                        break                 # source dead and reserve drained
                    else:
                        time.sleep(0.03)      # nothing aged past the hold window yet
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                          # the receiver/TV closed the connection
            finally:
                done.set()                    # stop the reader
            log(f"proxy: stream ended (sent {state['grand']} bytes over {state['gen']} streamlink runs)")

    return LiveHandler


def _proxy_playlist_note(text):
    """Secret-free one-line description of a playlist the proxy served, to diagnose why a receiver
    rejects a load (media vs master, low-latency parts, fragmented-MP4, codecs, encryption). No URIs are
    included, so no signed query strings reach the log."""
    bits = ["master" if "#EXT-X-STREAM-INF" in text else "media"]
    if bits[0] == "master":
        codecs = re.findall(r'CODECS="([^"]+)"', text)
        if codecs:
            bits.append("codecs=" + ",".join(sorted({c.strip() for cs in codecs for c in cs.split(",")}))[:48])
    else:
        bits.append(f"segs={text.count('#EXTINF')}")
    if "#EXT-X-PART" in text or "#EXT-X-PRELOAD-HINT" in text:
        bits.append("LL")
    if "#EXT-X-MAP" in text:
        bits.append("fMP4")
    km = re.search(r"#EXT-X-KEY:[^\n]*METHOD=([A-Z0-9-]+)", text)
    if km:
        bits.append("enc=" + km.group(1))
    if "#EXT-X-ENDLIST" in text:
        bits.append("VOD")
    return " ".join(bits)


def _ll_fetch_url(source, req_path):
    """Build the origin URL for a low-latency playlist request: keep the source's own query (token) but
    drop any stale _HLS_msn/_HLS_part/_HLS_skip baked into it, and append only the receiver's current ones
    (from req_path). Duplicate _HLS_msn makes the origin replay the sniffed sequence and the stream stalls."""
    base, _, sq = source.partition("?")
    keep = "&".join(kv for kv in sq.split("&") if kv and not kv.split("=", 1)[0].startswith("_HLS_"))
    fwd = ""
    if "?" in req_path:
        fwd = "&".join(kv for kv in req_path.split("?", 1)[1].split("&") if kv.split("=", 1)[0].startswith("_HLS_"))
    q = "&".join(x for x in (keep, fwd) if x)
    return base + ("?" + q if q else "")


def make_hls_proxy(source_url, hdr_map, tv="", media_kind="hls"):
    """Authenticating reverse-proxy for the receiver. Injects the sniffed headers/token the CDN
    requires and adds Access-Control-Allow-Origin so the https receiver can fetch this http LAN
    endpoint. media_kind 'hls' rewrites every playlist URL to route back through here (so segments and
    nested playlists inherit those headers + CORS); 'file' streams a direct media file at /live.<ext>
    and forwards byte-range requests so the receiver can seek."""
    HDRS = dict(hdr_map or {})
    HDRS.setdefault("User-Agent", "Mozilla/5.0")
    # Mimic a browser's in-player fetch. Some CDNs hotlink-protect their segments by requiring the
    # Sec-Fetch-* headers a real player request carries (they 403/decoy anything that looks like a
    # non-browser fetch), even with no token or cookie. Sec-Fetch-Site must MATCH what a browser sends:
    # same-origin when the player and the media share a host, else cross-site (a mismatched value can trip
    # the check). Derive it from the sniffed Referer vs the media host; default same-origin when there's no
    # Referer (an in-player same-origin fetch, e.g. the player embedded on its own CDN). setdefault -> a
    # sniffed value still wins.
    HDRS.setdefault("Accept", "*/*")
    _ref = next((v for k, v in HDRS.items() if k.lower() == "referer"), "")
    _mh = urllib.parse.urlsplit(source_url).netloc
    HDRS.setdefault("Sec-Fetch-Site",
                    "cross-site" if (_ref and urllib.parse.urlsplit(_ref).netloc != _mh) else "same-origin")
    HDRS.setdefault("Sec-Fetch-Mode", "cors")
    HDRS.setdefault("Sec-Fetch-Dest", "empty")

    # Keep-alive session: reuse CDN connections so each segment doesn't pay a fresh TLS handshake
    # (per-segment handshakes add latency that can stall playback). Falls back to urllib if the
    # requests library is unavailable.
    class _UpstreamError(Exception):
        def __init__(self, code): self.code = code
    try:
        import requests
        _sess = requests.Session()
        # curl_cffi fetches with a real browser's TLS/HTTP2 fingerprint, which a CDN bot-check that 403s a
        # plain HTTP client accepts. Optional dependency: when it's absent the 403 stands (no retry).
        try:
            from curl_cffi import requests as _cffi_requests
            _cffi_sess = _cffi_requests.Session()
        except Exception:
            _cffi_sess = None

        class _Resp:
            # .read() over iter_content (curl_cffi exposes the streamed body only there, not via .content):
            # read(-1) drains the whole body, read(n>=0) yields the next chunk.
            def __init__(self, r): self.headers = r.headers; self._it = r.iter_content(chunk_size=65536)
            def read(self, n=-1):
                if n is None or n < 0:
                    return b"".join(self._it)
                try:
                    return next(self._it)
                except StopIteration:
                    return b""

        # Headers the browser impersonation sets itself to match its fingerprint - our sniffed values would
        # desync it, so on the retry keep only the request-specific ones (Referer/Origin/Cookie/...).
        _IMPERSONATE_OWNS = ("user-agent", "accept", "accept-encoding", "accept-language",
                             "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")

        def _fetch(url, timeout=10):
            r = _sess.get(url, headers=HDRS, stream=True, timeout=timeout)
            if r.status_code == 403 and _cffi_sess is not None:
                r.close()
                hdrs = {k: v for k, v in HDRS.items() if k.lower() not in _IMPERSONATE_OWNS}
                r = _cffi_sess.get(url, headers=hdrs, stream=True, timeout=timeout, impersonate="chrome")
            if r.status_code >= 400:
                code = r.status_code; r.close(); raise _UpstreamError(code)
            return _Resp(r)

        def _fetch_ranged(url, range_hdr, timeout=15):
            h = dict(HDRS)
            if range_hdr:
                h["Range"] = range_hdr
            r = _sess.get(url, headers=h, stream=True, timeout=timeout)
            if r.status_code == 403 and _cffi_sess is not None:
                r.close()
                ch = {k: v for k, v in h.items() if k.lower() not in _IMPERSONATE_OWNS}
                r = _cffi_sess.get(url, headers=ch, stream=True, timeout=timeout, impersonate="chrome")
            return r.status_code, r.headers, r.iter_content(65536)
    except ImportError:
        def _fetch(url, timeout=10):
            try:
                return urllib.request.urlopen(urllib.request.Request(url, headers=HDRS), timeout=timeout)
            except urllib.error.HTTPError as e:
                raise _UpstreamError(e.code)

        def _fetch_ranged(url, range_hdr, timeout=15):
            h = dict(HDRS)
            if range_hdr:
                h["Range"] = range_hdr
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
                status = r.getcode() or 200
            except urllib.error.HTTPError as e:
                status, r = e.code, e
            def _it():
                while True:
                    c = r.read(65536)
                    if not c:
                        break
                    yield c
            return status, r.headers, _it()

    def _rewrite(text, base):
        # point every URL (segment, variant, key, map) at /p?u=<absolute> so it stays proxied
        out = []
        for ln in text.splitlines():
            s = ln.rstrip("\r")
            if not s.strip():
                out.append(s); continue
            if s.startswith("#"):
                m = re.search(r'URI="([^"]+)"', s)
                if m:
                    au = urllib.parse.urljoin(base, m.group(1))
                    s = s[:m.start(1)] + "/p?u=" + urllib.parse.quote(au, safe="") + s[m.end(1):]
                out.append(s); continue
            au = urllib.parse.urljoin(base, s.strip())
            out.append("/p?u=" + urllib.parse.quote(au, safe=""))
        return "\n".join(out) + "\n"

    def _qparam(path, key):
        """Every value for a query key, unquoted without parse_qs's +->space rule - a proxied segment url
        can carry a literal '+' in a base64 signature that '+'->space would corrupt into a 404."""
        qs = path.split("?", 1)[1] if "?" in path else ""
        return [urllib.parse.unquote(kv[len(key) + 1:]) for kv in qs.split("&") if kv.startswith(key + "=")]

    dbg = {"m3u8": False, "seg": False, "refused": False, "err": 0}   # log the first of each event only once

    class HlsProxy(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _foreign(self):
            return self.client_address[0] not in (tv, "127.0.0.1")
        def _cors(self, extra=()):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
        def do_OPTIONS(self):
            self.send_response(204); self._cors(); self.send_header("Content-Length", "0"); self.end_headers()
        def _serve_m3u8(self, text, base):
            body = _rewrite(text, base).encode()
            self.send_response(200); self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self._cors([("Content-Length", str(len(body))), ("Connection", "close")]); self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self._foreign():
                if not dbg["refused"]:
                    dbg["refused"] = True
                    log(f"proxy: REFUSED {self.client_address[0]} (cast target is {tv}): client can't reach the stream")
                self.send_error(403); return
            p = self.path.split("?", 1)[0]
            res_tail = p                      # what we're serving, for an upstream-error log
            try:
                if media_kind == "file":
                    # Direct media file: forward the receiver's Range so it can seek; relay the ranging
                    # headers (status 206 + Content-Range) the player needs.
                    status, uh, body_it = _fetch_ranged(source_url, self.headers.get("Range"))
                    ct = uh.get("Content-Type") or ("video/webm" if p.endswith(".webm") else "video/mp4")
                    extra = [("Accept-Ranges", "bytes")]
                    crange, clen = uh.get("Content-Range"), uh.get("Content-Length")
                    if crange:
                        extra.append(("Content-Range", crange))
                    if clen is not None:
                        extra.append(("Content-Length", clen))
                    else:
                        extra.append(("Connection", "close"))
                    if not dbg["seg"]:
                        dbg["seg"] = True
                        log(f"proxy: receiver fetching direct file (status {status}, client {self.client_address[0]})")
                    self.send_response(status)
                    self.send_header("Content-Type", ct)
                    self._cors(extra); self.end_headers()
                    for chunk in body_it:
                        if chunk:
                            self.wfile.write(chunk)
                    return
                if p == "/live.m3u8":
                    # Forward only the receiver's blocking-reload params, dropping the source's stale ones
                    # (see _ll_fetch_url). The rewrite base stays source_url.
                    r = _fetch(_ll_fetch_url(source_url, self.path))
                    text = r.read().decode("utf-8", "replace")
                    if not dbg["m3u8"]:
                        dbg["m3u8"] = True
                        log(f"proxy: receiver fetched /live.m3u8 [{_proxy_playlist_note(text)}] (client {self.client_address[0]})")
                    self._serve_m3u8(text, source_url)
                    return
                if p == "/p":
                    if not dbg["seg"]:
                        dbg["seg"] = True
                        log("proxy: receiver fetched first segment/sub-playlist")
                    u = (_qparam(self.path, "u") or [""])[0]
                    if not u:
                        self.send_error(400); return
                    res_tail = _redact_url(u).rsplit("/", 1)[-1] or p
                    is_pl = u.split("?")[0].endswith(".m3u8")
                    r = _fetch(_ll_fetch_url(u, self.path) if is_pl else u)   # forward blocking-reload for a nested LL chunklist
                    ctype = r.headers.get("Content-Type", "")
                    if is_pl or "mpegurl" in ctype:
                        self._serve_m3u8(r.read().decode("utf-8", "replace"), u)
                        return
                    first = r.read(65536)
                    # A segment hidden behind a non-media Content-Type (some sites serve fMP4/TS as
                    # text/html to dodge adblock/CDN filters) -> label it from its own bytes so the
                    # receiver's player accepts it (fMP4 starts with an MP4 box; TS with a 0x47 sync byte).
                    ct = ctype
                    if (not ct) or "text/html" in ct or "octet-stream" in ct:
                        if first[4:8] in (b"ftyp", b"styp", b"moof", b"moov", b"sidx", b"mdat"):
                            ct = "video/mp4"
                        elif first[:1] == b"\x47":
                            ct = "video/mp2t"
                        else:
                            ct = ct or "video/mp2t"
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self._cors([("Connection", "close")]); self.end_headers()
                    if first:
                        self.wfile.write(first)
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                    return
                self.send_error(404)
            except _UpstreamError as e:
                if dbg["err"] < 4:               # the CDN rejected a segment/playlist -> the receiver can't play it
                    dbg["err"] += 1
                    log(f"proxy: upstream {e.code} for {res_tail}")
                if p == "/live.m3u8" and e.code in (403, 404, 410):
                    # the MAIN source is gone (a signed url that expired) - flag it so the control server's
                    # /status can tell the popup the stream expired and to reload the page. A per-segment
                    # error (the /p path) isn't fatal, so only flag /live.m3u8.
                    try:
                        with open(PROXY_ERR_FILE, "w", encoding="utf-8") as _ef:
                            json.dump({"code": e.code, "ts": time.time()}, _ef)
                    except Exception:
                        pass
                try: self.send_error(e.code)     # propagate the CDN's 404/etc so the player retries correctly
                except Exception: pass
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                try:
                    log(f"proxy: HLS fetch failed ({p}): {type(e).__name__}: {str(e)[:80]}")
                    self.send_error(502)
                except Exception:
                    pass
    return HlsProxy


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # TVs/Chromecasts reset connections constantly (closing keep-alives, seeking); those are
        # benign, so don't spew a traceback for them. Real handler errors are logged via log().
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# --- Google Cast (Chromecast / Android TV) -----------------------------------
# Cast speaks a TLS+protobuf protocol (not DLNA/SOAP) and its receiver plays HLS, not raw
# MPEG-TS, so cast targets get the HLS proxy below instead of the live.ts server. pychromecast
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


CAST_HOSTS_FILE = os.path.join(TOKEN_DIR, "cast-hosts.json")   # hosts confirmed as Cast devices (persisted)
_known_cast_hosts = None


def _cast_hosts():
    """Hosts known to be Cast devices. A TV's Cast port sleeps in standby, so once a host is seen answering
    Cast it's remembered (persisted across restarts) and kept on the Cast path rather than demoted to the
    DLNA player, which can't run the branded receiver."""
    global _known_cast_hosts
    if _known_cast_hosts is None:
        try:
            with open(CAST_HOSTS_FILE, encoding="utf-8") as f:
                _known_cast_hosts = set(json.load(f))
        except Exception:
            _known_cast_hosts = set()
    return _known_cast_hosts


def _remember_cast_host(host):
    hosts = _cast_hosts()
    if host and host not in hosts:
        hosts.add(host)
        try:
            os.makedirs(TOKEN_DIR, exist_ok=True)
            with open(CAST_HOSTS_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(hosts), f)
        except Exception:
            pass


def discover_all_devices():
    """DLNA (SSDP) + Cast (mDNS) renderers, discovered in parallel and merged. If one physical
    device exposes BOTH protocols (e.g. an Android TV or a modern TV with Chromecast built-in), keep
    the Cast entry: it drives the branded receiver (own title, tolerant playback, remote exit) over a
    plain HLS proxy. Set SLC_PREFER_DLNA=1 to keep the DLNA (MPEG-TS/SOAP) entry instead."""
    from concurrent.futures import ThreadPoolExecutor
    devs = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(discover_dlna_renderers), ex.submit(discover_cast_devices)]
        for f in futs:
            try:
                devs += f.result()
            except Exception:
                pass
    prefer_dlna = os.environ.get("SLC_PREFER_DLNA") == "1"   # opt back into the plain DLNA player
    by_host, extras = {}, []
    for d in devs:
        h = d.get("host")
        if not h:
            extras.append(d)
            continue
        cur = by_host.get(h)
        # When a host exposes both protocols, prefer Cast: it drives the branded receiver instead of
        # the plain DLNA player. SLC_PREFER_DLNA=1 keeps DLNA (e.g. a source Cast plays back poorly).
        if prefer_dlna:
            take = cur is None or (cur.get("kind") == "cast" and d.get("kind") == "dlna")
        else:
            take = cur is None or (cur.get("kind") == "dlna" and d.get("kind") == "cast")
        if take:
            by_host[h] = d
    devs = list(by_host.values()) + extras
    # A TV with Chromecast built-in advertises Cast (mDNS) unreliably and its Cast port sleeps in standby,
    # so a Cast device can surface over DLNA only. Drive any host known to be a Cast device over Cast (the
    # branded receiver) regardless - a port that answers now confirms and remembers it; DLNA can't run the
    # branded receiver, and when the port is asleep the cast reports "turn the TV on" instead of failing
    # silently on DLNA. pychromecast connects by host:port and fetches the uuid/model itself.
    if not prefer_dlna:
        for d in devs:
            if d.get("kind") == "cast" and d.get("host"):
                _remember_cast_host(d["host"])
        known = _cast_hosts()
        for d in devs:
            if d.get("kind") == "dlna" and d.get("host") and \
               (d["host"] in known or _tcp_open(d["host"], 8009, timeout=1.5)):
                log(f"discover: {d.get('name', d['host'])} is a Cast device -> using Cast over DLNA")
                d["kind"] = "cast"; d["port"] = 8009
                _remember_cast_host(d["host"])
    devs.sort(key=lambda d: d["name"].lower())
    return devs


def _tcp_open(host, port, timeout=4):
    """True if a TCP connect to host:port succeeds. The Cast service (:8009) stops listening when
    the TV is in standby/off (it still answers ping), so we check this before connecting. Otherwise
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


# The Cast receiver app id: our branded receiver by default, overridable via SLC_CAST_APP_ID (e.g.
# CC1AD845 for Google's Default Media Receiver). Quitting it returns the device to its home screen,
# BUT calling quit on a device that's already idle spuriously RELAUNCHES it (showing the receiver
# splash), so every quit below is guarded by checking this is still the running app.
_CAST_RECEIVER_APP = os.environ.get("SLC_CAST_APP_ID", "C4B6F8FF")


def cast_quit(host, port, uuid, model, name):
    """Stop playback on a cast device (back to home). Only quits if OUR media receiver is still
    the running app. Quitting an already-idle device just relaunches its splash screen."""
    try:
        cc = _cast_connect(host, port, uuid, model, name, timeout=8)
        if getattr(cc, "app_id", None) == _CAST_RECEIVER_APP:
            cc.quit_app()
            time.sleep(0.5)
        cc.disconnect()
    except Exception as e:
        log(f"cast quit failed: {type(e).__name__}: {str(e)[:80]}")


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

    def build_cast_args(u, d, qy, media, kind="dlna", cast=None, title="",
                        src_kind="", src_vod=False, src_ll=False):
        """(url, device, quality, media, kind) -> proxy CLI argv. kind 'cast' targets a Chromecast
        (HLS + pychromecast); 'dlna' targets a UPnP renderer (MPEG-TS + SOAP). Replay headers are
        NOT here. They ride in the env (see launch). --managed = control server owns kill+pidfile."""
        extra = [u, ("--cast" if kind == "cast" else "--proxy"), "--low-latency", "--managed"]
        if d:
            extra += ["--tv", d]
        if qy and qy != "best":
            extra += ["--quality", qy]
        if media:
            extra += ["--media-url", media]
        if src_kind:                          # classifier's verdict -> proxy skips its own source probe
            extra += ["--src-kind", src_kind]
            if src_vod:
                extra += ["--src-vod"]
            if src_ll:
                extra += ["--src-ll"]
        if title:
            extra += ["--title", title]      # shown on the renderer instead of the raw URL
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
        try:
            out = open(PROXY_LOG, "w")   # capture the proxy's [cast] logs (retries, restarts, stop reason)
        except OSError:
            out = subprocess.DEVNULL
        kw = dict(stdin=subprocess.DEVNULL, stdout=out,
                  stderr=subprocess.STDOUT, close_fds=True)
        if headers:
            env = os.environ.copy()
            env["SLC_HEADERS"] = "\n".join(headers)
            kw["env"] = env
        if IS_WIN:
            kw["creationflags"] = 0x00000008 | NO_WINDOW   # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True                 # detach on macOS/Linux
        proc = subprocess.Popen(cmd, **kw)
        if out is not subprocess.DEVNULL:
            out.close()                  # child keeps its own dup; we don't need ours
        return proc

    def _spawn_proxy(extra, headers):
        """Stop the current proxy and start a new one, recording its pid SYNCHRONOUSLY so
        proxy_alive() is true the instant the cast is registered (no startup teardown race)."""
        kill_previous_proxy()
        proc = launch(extra, headers)
        _write_pidfile(proc.pid)
        return proc.pid

    def relaunch_current(u, d, qy, media, headers, kind="dlna", cast=None, title=""):
        """Re-cast the given stream at quality qy (snapshot args, not live state reads)."""
        return _spawn_proxy(build_cast_args(u, d, qy, media, kind, cast, title), headers)

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
            elif u.path == "/qualities":       # POST so the page's Cookie (for reading its ladder) stays out of the URL
                self._qualities(q)
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def _cast(self, q):
            url = (q.get("url", [""])[0]).strip()
            device = (q.get("device", [""])[0]).strip()   # chosen TV host/IP
            name = (q.get("name", [""])[0]).strip()        # friendly name (for status)
            title = (q.get("title", [""])[0]).strip()      # stream title (for status)
            quality = _safe_quality(q.get("quality", [""])[0])
            media = (q.get("media", [""])[0]).strip()      # a single sniffed media URL (fallback)
            medias_raw = (q.get("medias", [""])[0]).strip()  # all sniffed HLS URLs (JSON): understand + cast the master
            headers = parse_replay_headers(q.get("headers", [""])[0])
            media_probed = False
            src_kind, src_vod, src_ll = "", False, False   # the classifier's verdict -> the proxy skips re-probing
            if not (url.startswith("http://") or url.startswith("https://")):
                self._json({"ok": False, "error": "invalid URL: " + url[:60]})
                return
            if media and not (media.startswith("http://") or media.startswith("https://")):
                media = ""    # ignore a bogus media URL -> fall back to resolving the page
            # Cast a rendition by its OWN url from the page's ladder (the extension reads it - inline, or
            # resolved in-page - so it's freshly minted). A numeric pick -> that height; "best"/no pick ->
            # the highest. This also avoids casting the sniffed VARIANT playlist, whose signed url some hosts
            # make single-use (a classify probe plus the proxy fetch would then 410) - see probe_media=False.
            # Empty (no ladder, e.g. a live cam) -> the sniffed path below still runs.
            picked_url = ""
            try:
                _lad = {int(k): v for k, v in (json.loads(q.get("ladder", ["{}"])[0] or "{}")).items()}
            except Exception:
                _lad = {}
            if _lad:
                _qh = re.match(r"(\d+)", quality or "")
                _k = int(_qh.group(1)) if _qh else max(_lad)
                _cand = _lad.get(_k, "")
                if isinstance(_cand, str) and _cand.startswith(("http://", "https://")):
                    picked_url = _cand
            if medias_raw or picked_url:
                # General path: understand the sniffed HLS stream from its content and cast a media
                # (variant) playlist. When a quality was picked, try that quality's own url first, and fall
                # back to the sniffed (playing) quality if it can't be cast - so a stale/unavailable pick
                # never breaks casting.
                try:
                    medias = json.loads(medias_raw) if medias_raw else []
                except Exception:
                    medias = []
                medias = [m for m in medias if isinstance(m, str)] if isinstance(medias, list) else []
                hmap = _headers_list_to_map(headers)
                model = None
                if picked_url:
                    cand = _classify_stream([picked_url], hmap, probe_media=False)   # picked quality's own stream
                    if cand["source"] and cand["castable"]:
                        model = cand
                        log(f"control: quality {quality} -> its own stream")
                if model is None and medias:
                    model = _classify_stream(medias, hmap)          # fallback: the sniffed (playing) quality
                if model is not None:
                    if not model["source"] or not model["castable"]:
                        reason = model["reason"] or "unplayable"
                        log(f"control: cast rejected ({reason}) {_redact_url(model['source'])[:70]}")
                        _emsg = {"obfuscated": "this server hides the video inside images - try another server",
                                 "drm": "this stream is DRM-protected"}
                        self._json({"ok": False, "reason": reason,
                                    "error": _emsg.get(reason, f"stream not castable ({reason})")})
                        return
                    # Cast the MEDIA (variant) playlist, not the master. The receiver plays a bare media
                    # playlist reliably but fails to load a proxied master, even a single-variant one. The
                    # signed URLs are TTL-based (re-fetchable within the window), so casting the variant
                    # directly is safe. Pick the requested quality (or the highest); a live stream keeps its
                    # fresh chunklist below.
                    vs = model["variants"]
                    pick = next((v for v in vs if v["quality"] == quality), None) or (vs[0] if vs else None)
                    media = pick["url"] if pick else model["source"]
                    # A rendition taller than the TV's H.264 decoder cap (_H264_MAXH, ~1088) won't decode -
                    # a portrait / very-tall video hits this. Drop to the tallest rendition that fits. The
                    # page ladder is keyed by height, so pick the highest entry <= the cap and cast it.
                    if pick and pick.get("height", 0) > _H264_MAXH and pick.get("vcodec", "") in ("h264", ""):
                        try:
                            _lad = {int(k): u for k, u in (json.loads(q.get("ladder", ["{}"])[0] or "{}")).items()}
                        except Exception:
                            _lad = {}
                        _fit = max((k for k in _lad if k <= _H264_MAXH and isinstance(_lad[k], str)
                                    and _lad[k].startswith(("http://", "https://"))), default=None)
                        if _fit is not None:
                            _alt = _classify_stream([_lad[_fit]], hmap, probe_media=False)
                            if _alt.get("source") and _alt.get("castable") and _alt.get("variants"):
                                model, pick = _alt, _alt["variants"][0]
                                media = pick["url"]
                                log(f"control: rendition too tall for the decoder -> {pick.get('height', 0)}p")
                    media_probed = True
                    # a LIVE stream: cast the fresh media the classifier chose, not a stale/wrong/AV1 variant
                    if model["live"] and model["source"]:
                        media = model["source"]
                    # The classifier already fetched + understood the source (HLS, live/VOD, low-latency),
                    # so hand the verdict to the proxy: it can skip re-fetching to re-probe (a slow, and on
                    # single-use hosts wasteful, second look at the same URL before the TV even loads).
                    src_kind = "hls"
                    src_vod = not model["live"]
                    src_ll = model["low_latency"]
            # ignore an in-flight proxy during a Stop window: it's being torn down, so a re-cast
            # should start a fresh one rather than be told it's "already" casting (empty target).
            if proxy_alive() and time.time() >= stopping["until"]:
                with _state_lock:
                    snap = {k: state[k] for k in _CAST_SNAP_FIELDS}
                self._json({"ok": True, "already": True, **snap})
                return
            if media and not media_probed:
                # single sniffed URL (older extension / a direct file): pre-flight it so a DRM/offline
                # stream fails fast with a clear message instead of leaving the TV black. Headers (Cookie)
                # reach the probe via a private 0600 --config file, not argv.
                cfg = _write_header_config(headers)
                try:
                    ok, reason, detail = probe_stream(_proto_target(media), cfg)
                finally:
                    _safe_unlink(cfg)
                if not ok and reason == "unplayable" and _source_reachable(media, headers):
                    # streamlink's resolver can't open some CDN media playlists (fragmented-MP4, a
                    # .mp4/ path) that the cast's own reverse-proxy serves fine -> trust the proxy.
                    ok, reason, detail = True, "", ""
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
            extra = build_cast_args(url, device, quality, media, kind, cinfo, title,
                                    src_kind, src_vod, src_ll)
            already, this_epoch = None, 0
            with _state_lock:
                if proxy_alive() and time.time() >= stopping["until"]:   # re-check atomically
                    already = {k: state[k] for k in _CAST_SNAP_FIELDS}
                else:
                    stopping["until"] = 0  # a fresh cast cancels any pending stop-suppression
                    _safe_unlink(PROXY_ERR_FILE)         # clear a stale expired-source flag from a prior cast
                    _spawn_proxy(extra, headers)        # kill old (if any) + launch + record pid
                    epoch["n"] += 1
                    this_epoch = epoch["n"]
                    grace["until"] = time.time() + 10   # cover the child's startup window
                    state.update(url=url, device=device, name=name, title=title, quality=quality,
                                 media=media, headers=headers, epoch=this_epoch, kind=kind, cast=cinfo)
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
            raw_h = (q.get("h", [""])[0]).strip()          # sniffed source: list the master playlist's variants
            raw_urls = (q.get("urls", [""])[0]).strip()    # all HLS URLs the tab sniffed (the master is often one)
            if raw_h or raw_urls:
                try:
                    hmap = json.loads(raw_h) if raw_h else {}
                except Exception:
                    hmap = {}
                if not isinstance(hmap, dict):
                    hmap = {}
                try:
                    urls = json.loads(raw_urls) if raw_urls else []
                except Exception:
                    urls = []
                urls = [u for u in urls if isinstance(u, str)] if isinstance(urls, list) else []
                if not urls and qurl:
                    urls = [qurl]
                log("qualities: names=" + ", ".join(u.split("?")[0].rsplit("/", 1)[-1][:44] for u in urls[:24]))
                # A watch page can inline its whole rendition ladder ({height:url}); only the playing quality
                # is ever sniffed, so when the page supplied that ladder it is the authoritative list. Prefer
                # it and skip probing for a master: guessing/fetching master URLs the host doesn't serve is
                # wasted, slow work once the page has already told us every quality. A live cam room inlines
                # no ladder, so it still resolves the sniffed master for its variants.
                raw_lad = (q.get("ladder", [""])[0]).strip()
                try:
                    ladder = {int(k): v for k, v in (json.loads(raw_lad) or {}).items()} if raw_lad else {}
                except Exception:
                    ladder = {}
                by_h, vs = {}, []                          # height -> label, richest label kept
                if not ladder:
                    _mu, vs, _t = _resolve_sniffed_master(urls, hmap)
                    for v in vs:
                        mm = re.match(r"(\d+)", v["quality"])
                        if mm:
                            by_h[int(mm.group(1))] = v["quality"]
                for hgt in ladder:
                    by_h.setdefault(hgt, f"{hgt}p")
                quals = [by_h[hgt] for hgt in sorted(by_h, reverse=True)] or [v["quality"] for v in vs]
                log(f"qualities: sniffed n={len(urls)} hdrs={list(hmap)} -> {len(vs)}"
                    + (f" +page{sorted(ladder, reverse=True)}" if ladder else "")
                    + f" [ldiag={(q.get('ldiag', [''])[0])[:130]}]")
                self._json({"ok": True, "qualities": quals, "matrix": []})
                return
            m = stream_meta(qurl) if qurl else {"qualities": []}
            self._json({"ok": True, "qualities": m.get("qualities", []), "matrix": m.get("matrix", [])})

        def _relaunch_locked(self, **changes):
            """Apply state changes and re-cast the current stream (caller holds _state_lock).
            Returns the response dict. Used by /quality."""
            if not state["url"]:
                state.update(changes)
                return {"ok": True, "casting": False}
            state.update(changes)
            u2, d2 = state["url"], state["device"]
            m2, h2 = state["media"], state["headers"]
            k2, c2 = state.get("kind", "dlna"), state.get("cast") or {}
            t2 = state.get("title", "")
            epoch["n"] += 1
            state["epoch"] = epoch["n"]
            grace["until"] = time.time() + 10        # tolerate the brief proxy gap during relaunch
            save_cast_state(state, grace["until"])
            relaunch_current(u2, d2, state["quality"], m2, h2, k2, c2, t2)
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

        def _stop(self, q):
            _safe_unlink(PROXY_ERR_FILE)   # the cast is over: clear any expired-source flag so /status stops reporting it
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
            # a fatal proxy error (the source url expired: 410/403) written by the running proxy: surface it
            # so the popup can tell the user the stream expired and to reload the page.
            perror = None
            try:
                with open(PROXY_ERR_FILE, encoding="utf-8") as _ef:
                    _pe = json.load(_ef)
                if time.time() - _pe.get("ts", 0) < 90:
                    perror = _pe.get("code", 410)
            except Exception:
                pass
            self._json({"ok": True, "casting": alive, **snap, **({"perror": perror} if perror else {})})

        def _ping(self, q):
            self._json({"ok": True, "pong": True, "version": HELPER_VERSION})

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
    # pre-quit here. The proxy's own (guarded) atexit quits the receiver when we kill it;
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


def _resolve_hls_url(page_url, quality, hdr_map):
    """Resolve a page URL to the source's own HLS playlist URL with streamlink, so the native Shaka
    player (which needs HLS, not a TS pipe) can play sites that aren't a fetchable .m3u8 (Twitch, Kick,
    YouTube, etc.). Twitch resolves to its low-latency HLS. Returns '' if streamlink can't resolve the
    stream. Only asks for the URL (--stream-url); it does not stream or transcode."""
    opts = ["--stream-url"]
    if "twitch.tv" in page_url:
        opts += ["--twitch-low-latency"]          # Twitch's native LL-HLS = the low latency we want
    cfg = _write_header_config([f"{k}={v}" for k, v in (hdr_map or {}).items()])
    if cfg:
        opts += ["--config", cfg]
    try:
        out = subprocess.run(_streamlink_cmd(*opts, "--", page_url, quality or "best"),
                             capture_output=True, text=True, timeout=25)
        url = ""
        for ln in (out.stdout or "").splitlines():   # --stream-url prints the resolved URL on stdout
            ln = ln.strip()
            if ln.startswith("http"):
                url = ln
        if not url:
            log(f"cast: streamlink could not resolve a stream ({(out.stderr or '').strip()[-120:]})")
        return url
    except Exception as e:
        log(f"cast: streamlink resolve error: {type(e).__name__}: {str(e)[:80]}")
        return ""
    finally:
        if cfg:
            _safe_unlink(cfg)


def _hls_is_vod(url, hdr_map):
    """True if the HLS at url is a finite VOD (it carries #EXT-X-ENDLIST or #EXT-X-PLAYLIST-TYPE:VOD),
    so it can be cast as a seekable BUFFERED stream instead of LIVE. Follows one master -> variant hop
    to reach the media playlist. Returns False on any error or when it can't tell, so a live stream is
    never wrongly marked seekable."""
    def _peek(u):
        h = dict(hdr_map or {}); h.setdefault("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=6) as r:
            return r.read(3_000_000).decode("utf-8", "replace")   # keep original case: signed URLs are case-sensitive
    def _has_endlist(text):
        t = text.lower()
        return "#ext-x-endlist" in t or "#ext-x-playlist-type:vod" in t
    try:
        text = _peek(url)
        if _has_endlist(text):
            return True
        if "#ext-x-stream-inf" in text.lower():        # master playlist: the ENDLIST is in the variant
            for ln in text.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    return _has_endlist(_peek(urllib.parse.urljoin(url, ln)))   # ln keeps its original case
        return False
    except Exception:
        return False


def _fetch_playlist(url, hdr_map, timeout=8):
    """GET an .m3u8 with the sniffed headers; return its body text, or None on any error (logging the
    HTTP status so a wrong guessed name (404) reads differently from a signature-locked one (403))."""
    try:
        h = dict(hdr_map or {}); h.setdefault("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except Exception as e:
        code = getattr(e, "code", None)
        log(f"variants: fetch {url.split('?')[0][-52:]} -> {('HTTP ' + str(code)) if code else type(e).__name__}")
        return None


def _page_hls_ladder(page_url, hdr_map):
    """Some players inline the whole quality ladder in the watch-page HTML - a JSON list of {quality, url}
    HLS renditions - before the player fetches any single one, so the sniffer only ever sees the playing
    quality's playlist and the menu looks empty/thin. Fetch the page with the page's own headers and read
    those already-signed per-quality master URLs so the menu is complete. Returns {height:int -> url},
    header-authenticated and bounded; empty on any failure (never raises)."""
    if not (page_url or "").startswith(("http://", "https://")):
        return {}
    try:
        h = dict(hdr_map or {}); h.setdefault("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(urllib.request.Request(page_url, headers=h), timeout=6) as r:
            html = r.read(2_000_000).decode("utf-8", "replace")
    except Exception:
        return {}
    if ".m3u8" not in html:
        return {}
    ladder = {}
    for o in re.findall(r'\{(?:[^{}]|"[^"]*")*\}', html):        # each flat JSON object in the page
        if ".m3u8" not in o:
            continue
        qm = re.search(r'"(?:quality|label|res|height)"\s*:\s*"?(\d{3,4})p?"?', o)
        um = re.search(r'"(?:videoUrl|url|src|file|manifest)"\s*:\s*"([^"]*\.m3u8[^"]*)"', o)
        if not (qm and um):
            continue
        hgt = int(qm.group(1))
        if not (100 <= hgt <= 4320):
            continue
        try:
            u = json.loads('"' + um.group(1) + '"')             # unescape the JS string (\/ etc.) safely
        except Exception:
            u = um.group(1).replace("\\/", "/")
        ladder[hgt] = u
    return ladder


def _codec_family(codecs):
    """Coarse video-codec family from an HLS/DASH CODECS string, for compatibility decisions
    ('' if none/audio-only)."""
    c = (codecs or "").lower()
    if "avc1" in c or "avc3" in c or "h264" in c:
        return "h264"
    if "hvc1" in c or "hev1" in c or "hevc" in c or "h265" in c:
        return "hevc"
    if "vp09" in c or "vp9" in c:
        return "vp9"
    if "av01" in c:
        return "av1"
    if "vp08" in c or "vp8" in c:
        return "vp8"
    return "other" if any(t and not t.startswith("mp4a") and not t.startswith("ec-3") and not t.startswith("ac-3")
                          for t in c.split(",")) else ""


# H.264 first: the one video codec every Cast receiver decodes. When a master offers a resolution in
# several codecs, the most broadly castable is picked so the default plays anywhere; a receiver that
# reports wider support (see stream capabilities) can override this later.
_VCODEC_ORDER = {"h264": 4, "av1": 3, "hevc": 2, "vp9": 1, "vp8": 1}

# Max frame height a TV's H.264 decoder handles. ~1088 (1920x1088) is the near-universal cap regardless of
# panel size - 4K/8K TVs still decode H.264 only to ~1080p (they use HEVC/AV1 above that). A taller H.264
# rendition (a portrait / very-tall video) is auto-dropped to the tallest that fits. Overridable for the
# rare TV whose H.264 decoder genuinely does 4K (SLC_H264_MAXH=2160).
try:
    _H264_MAXH = int(os.environ.get("SLC_H264_MAXH", "1088") or 1088)
except ValueError:
    _H264_MAXH = 1088


def _parse_master_variants(url, text):
    """Video variants ([{quality, url, height, bw, codecs, vcodec}] highest-first, one per resolution) of
    an HLS master playlist body. [] if it carries no #EXT-X-STREAM-INF with a RESOLUTION (i.e. it is a
    media playlist, not a master). When a resolution is offered in several codecs the most broadly
    castable (H.264) is kept. Variant URIs are resolved against url."""
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        mres = re.search(r"RESOLUTION=\d+x(\d+)", ln)
        if not mres:
            continue                                     # audio-only / no resolution -> not a pickable quality
        mfps = re.search(r"FRAME-RATE=([\d.]+)", ln)
        mbw = re.search(r"BANDWIDTH=(\d+)", ln)
        mcod = re.search(r'CODECS="([^"]+)"', ln)
        uri = next((s.strip() for s in lines[i + 1:] if s.strip() and not s.strip().startswith("#")), "")
        if not uri:
            continue
        height = int(mres.group(1)); fps = int(round(float(mfps.group(1)))) if mfps else 0
        codecs = mcod.group(1) if mcod else ""
        out.append({"quality": f"{height}p" + (str(fps) if fps > 30 else ""),
                    "url": urllib.parse.urljoin(url, uri), "height": height,
                    "bw": int(mbw.group(1)) if mbw else 0,
                    "codecs": codecs, "vcodec": _codec_family(codecs)})
    best = {}
    for v in out:
        cur = best.get(v["quality"])
        key = (_VCODEC_ORDER.get(v["vcodec"], 0), v["bw"])
        if cur is None or key > (_VCODEC_ORDER.get(cur["vcodec"], 0), cur["bw"]):
            best[v["quality"]] = v
    return sorted(best.values(), key=lambda v: (v["height"], v["bw"]), reverse=True)


_MASTER_NAMES = ("index.m3u8", "master.m3u8", "playlist.m3u8")


def _master_url_candidates(url):
    """Sibling- and parent-directory master-playlist URLs to try when a sniffed HLS URL is a media
    (variant) playlist. A player fetches the master once then rides a variant, so the sniffer usually
    captures the variant while the master sits beside it or one directory up (some hosts give each
    rendition its own subdirectory under a shared master). Renditions are commonly named by suffixing the
    master stem with '-'-joined tags (e.g. index-f3-v1-a1, hls-1080p-<hash>), so progressively dropping
    trailing '-' groups recovers stem candidates; the conventional index/master/playlist names are also
    tried, in the same directory first and then the parent. The query string is preserved (hosts sign the
    whole URL), the sniffed URL itself is excluded, and the list is bounded. Highest-priority first."""
    sp = urllib.parse.urlsplit(url)
    # Keep the auth token the host signs into the query, but drop the low-latency blocking-reload params
    # (_HLS_msn/_HLS_part/_HLS_skip): a master playlist has no parts, so a chunklist's _HLS_ query makes
    # the host reject the derived master URL.
    cq = "&".join(kv for kv in sp.query.split("&") if kv and not kv.split("=", 1)[0].lower().startswith("_hls_"))
    base, _, fname = sp.path.rpartition("/")
    stem = fname[:-5] if fname.endswith(".m3u8") else fname
    parts = stem.split("-")
    stems = [f"{'-'.join(parts[:i])}.m3u8" for i in range(1, len(parts))][:3]   # drop trailing '-' groups
    mc = re.match(r"chunklist_.*_([a-z]+)$", stem)   # chunklist_N_video_<n>_llhls -> llhls.m3u8 (LL master)
    if mc:
        stems.insert(0, mc.group(1) + ".m3u8")
    dirs = [(base, stems + list(_MASTER_NAMES))]
    parent = base.rpartition("/")[0]
    if parent and parent != base:
        # a rendition can live in its own subdirectory: try the shared master by the conventional names
        # and by the sniffed file's own name one level up (some packagers reuse the manifest filename).
        dirs.append((parent, [fname] + [n for n in _MASTER_NAMES if n != fname]))
    out, seen = [], set()
    for d, names in dirs:
        for nm in names:
            p = f"{d}/{nm}"
            if p == sp.path or p in seen:                # skip the sniffed URL itself / duplicates
                continue
            seen.add(p)
            out.append(urllib.parse.urlunsplit((sp.scheme, sp.netloc, p, cq, "")))
    return out[:8]


def _playlist_shape(text):
    """A short, secret-free descriptor of a playlist that didn't parse as a master, for diagnosing a new
    host from the log. Only #EXT-X-STREAM-INF attribute lines are surfaced; segment/variant URI lines
    (which carry signed query strings) are never logged."""
    if not text:
        return ""
    if "#EXTM3U" not in text[:64]:
        return " [not m3u8]"
    infs = [ln.strip()[18:][:80] for ln in text.splitlines() if ln.startswith("#EXT-X-STREAM-INF")]
    if infs:
        return f" [STREAM-INF x{len(infs)}: {' | '.join(infs[:3])}]"
    if "#EXTINF" in text:
        return " [media playlist]"
    return " [no STREAM-INF]"


def _headers_list_to_map(headers):
    """['Referer=...', 'Cookie=...'] (the replay list) -> {'Referer': '...', ...} for a fetch. The first
    '=' splits, so a value that itself contains '=' (a signed URL) is preserved."""
    out = {}
    for h in (headers or []):
        name, sep, value = h.partition("=")
        if sep and name:
            out[name] = value
    return out


def _resolve_sniffed_master(urls, hdr_map):
    """From the HLS URLs a tab sniffed (newest-first), return (master_url, variants): the URL that is (or
    leads to) the multi-quality master and its video variants, highest-first. A player fetches the master
    once then rides a variant, and separate audio/video tracks or low-latency live playback only work when
    the *master* is cast (so the receiver pairs the tracks and adapts) - yet the sniffer usually reports a
    variant. So this tries each sniffed URL as a master, then falls back to guessing sibling/parent master
    URLs from the newest one (see _master_url_candidates), keeping the richest. Returns (newest_url, []) if
    nothing lists variants. Fetches are time-bounded so an unresponsive host can't stall the caller. Uses
    the sniffed Referer/Origin the host requires. Returns (master_url, variants, playlist_text): the body
    is the master's (or the newest sniffed body when no master was found) so a caller can classify it
    without re-fetching."""
    urls = [u for u in (urls or []) if u and u.startswith(("http://", "https://"))]
    if not urls:
        return "", [], None
    seen, best_url, best, best_text, first_text = set(), urls[0], [], None, None
    merged = {}                                          # quality -> variant, unioned across every master
    deadline = time.monotonic() + 7
    # A live stream sniffs a dozen media chunklists but usually no master. A chunklist/segment is very
    # unlikely to BE a master, so probing them first only burns the time budget before the derived
    # sibling/parent guesses (where a live master like llhls.m3u8 actually is) are reached. Probe the
    # plausible masters (non-chunklist sniffed URLs) and the derived guesses first, but DEFER the
    # chunklist-named URLs to the end rather than dropping them, so an unconventionally chunklist-named
    # master is still tried within the deadline when nothing better was found.
    def _worth_probing(u):
        f = u.split("?")[0].rsplit("/", 1)[-1].lower()
        return not (f.startswith("chunklist") or f.endswith((".ts", ".m4s", ".aac", ".mp4")))
    likely = [u for u in urls if _worth_probing(u)]
    deferred = [u for u in urls if not _worth_probing(u)]

    def _probe(seq):
        nonlocal best_url, best, best_text, first_text
        for u in seq:
            if time.monotonic() >= deadline:
                return True                              # out of the time budget -> stop
            if u in seen:
                continue
            seen.add(u)
            text = _fetch_playlist(u, hdr_map, timeout=3)
            if u == urls[0]:
                first_text = text
            if text is None:
                continue
            vs = _parse_master_variants(u, text)
            for v in vs:                                 # some hosts split qualities across per-quality masters
                cur = merged.get(v["quality"])
                if cur is None or (_VCODEC_ORDER.get(v["vcodec"], 0), v["bw"]) > (_VCODEC_ORDER.get(cur["vcodec"], 0), cur["bw"]):
                    merged[v["quality"]] = v
            if len(vs) > len(best):
                best_url, best, best_text = u, vs, text   # the single richest master = the adaptive cast source
            if len(best) >= 2:                            # one master already lists the full ladder; stop probing
                return True
        return False

    # Probe the sniffed URLs first. Only derive+probe sibling/parent master candidates when NONE of the
    # sniffed URLs is itself a master: guessing master URLs the host doesn't serve is a slow 404 walk that
    # burns the time budget (and delays the cast) once we already hold the master. A live stream that
    # sniffed only chunklists finds no master here, so it still falls through to the guesses + deferred.
    done = _probe(likely)
    if not done and not best:
        _probe(_master_url_candidates(urls[0]) + deferred)
    variants = sorted(merged.values(), key=lambda v: (v["height"], v["bw"]), reverse=True)
    if variants:
        log(f"master: {best_url.split('?')[0][-52:]} -> {len(variants)} ({', '.join(v['quality'] for v in variants)})")
    else:
        log(f"master: none for {urls[0].split('?')[0][-52:]}{_playlist_shape(first_text)}")
    return best_url, variants, (best_text if best else first_text)


def _hls_variants(url, hdr_map):
    """Video variants (highest-first, one per resolution) of the master behind one sniffed URL, or [].
    Convenience wrapper over _resolve_sniffed_master for callers that hold a single URL."""
    return _resolve_sniffed_master([url], hdr_map)[1]


def _prefer_h264_codec(url):
    """Request H.264 in place of AV1 when a URL pins the video codec through a query param (e.g. a
    ...codec...=av1 selector). Cast targets decode H.264 universally but AV1 only spottily, so normalise
    to the broadly-decodable baseline rather than cast a codec the TV can't decode."""
    return re.sub(r'([?&][^=&]*codec[^=&]*=)av1(?=&|$)', r'\g<1>h264', url, flags=re.IGNORECASE)


def _classify_stream(urls, hdr_map, probe_media=True):
    """Understand a sniffed HLS stream from its content, not its URL shape. Picks the media playlist to cast
    (resolving a master to its variants first) and describes the structure the cast path and the user need.
    Returns:
      {source, role: 'master'|'media', variants, video_codec, container: 'ts'|'fmp4'|'?',
       live: bool, low_latency: bool, separate_av: bool, encryption: ''|'aes-128'|'drm',
       castable: bool, reason: ''|'drm'}
    Reads at most one extra media playlist (for container/liveness/encryption). Header-authenticated,
    bounded. This is the general model every delivery type is reduced to."""
    urls = [_prefer_h264_codec(u) for u in urls]   # cast the broadly-decodable H.264 rendition, not AV1
    master_url, variants, master_text = _resolve_sniffed_master(urls, hdr_map)
    # With a real master use it; with no master cast a bare media playlist - but prefer a video/muxed one
    # over a separate audio-only rendition (a live stream splits video and audio into their own chunklists,
    # and the newest sniffed one is often the audio track, which alone plays as sound with no picture).
    # (_resolve_sniffed_master returns urls[0] as master_url even when it found no master, so key on variants.)
    if variants:
        src = master_url
    elif urls:
        src = next((u for u in urls
                    if "audio" not in u.split("?")[0].rsplit("/", 1)[-1].lower()), urls[0])
    else:
        src = ""
    model = {"source": src, "role": "master" if variants else "media", "variants": variants,
             "video_codec": variants[0]["vcodec"] if variants else "", "container": "?",
             "live": True, "low_latency": False, "separate_av": False, "encryption": "",
             "castable": bool(src), "reason": ""}
    # A resolved master tells us the delivery shape: whether audio is a separate rendition and whether it
    # is low-latency.
    if master_text and variants:
        model["separate_av"] = "#EXT-X-MEDIA:TYPE=AUDIO" in master_text
        if "#EXT-X-PART" in master_text or "PRELOAD-HINT" in master_text:
            model["low_latency"] = True
    # The player rides a specific media chunklist - the freshest sniffed non-audio one, distinct from the
    # master. Probe (and cast) that rather than a master variant: a variant URL parsed from the master can
    # lack params the host requires on the real chunklist, making it read as VOD/unavailable when it's live.
    fresh = ""
    if variants:
        fresh = next((u for u in urls
                      if u != master_url
                      and u.split("?")[0].endswith(".m3u8")
                      and "audio" not in u.split("?")[0].rsplit("/", 1)[-1].lower()), "")
    # read one media playlist: the sniffed chunklist the player rides (or the top master variant, or the
    # source itself when it's already media). Reuse a carried-over body only when it's the media we need;
    # otherwise fetch it (the sniffed-master resolver skips chunklists, so a bare-media source's body isn't
    # carried through - without this the container and low-latency flags stay unknown and a live LL stream
    # is cast without live-edge handling). Drop stale blocking-reload params so the probe fetch can't block.
    if probe_media:
        probe = fresh or (variants[0]["url"] if variants else src)
        probe = re.sub(r"&_HLS_[^&]*", "", probe) if probe else probe
        mt = master_text if (master_text and not variants) else (_fetch_playlist(probe, hdr_map, timeout=3) if probe else None)
    else:
        # Don't fetch the media just to classify it: its signed URL can be single-use (some hosts' per-quality
        # variant playlist 410s on the 2nd fetch), and the proxy fetches it exactly once when it serves - so a
        # classify probe here would burn the token and leave the proxy with a 410. This path is only for a
        # quality picked from a video page's ladder, so it is VOD.
        mt = master_text if (master_text and not variants) else None
        model["live"] = False
    if mt:
        model["container"] = "fmp4" if ("#EXT-X-MAP" in mt or ".m4s" in mt) else ("ts" if ".ts" in mt else "?")
        model["live"] = not ("#EXT-X-ENDLIST" in mt or "PLAYLIST-TYPE:VOD" in mt)
        if "#EXT-X-PART" in mt or "PRELOAD-HINT" in mt:
            model["low_latency"] = True
        km = re.search(r"#EXT-X-KEY:[^\n]*METHOD=([A-Z0-9-]+)", mt)
        if km:
            meth = km.group(1).upper()
            model["encryption"] = "drm" if meth.startswith("SAMPLE-AES") else meth.lower()
        # Segments that are IMAGES (.image/.png/.jpg...) mean the video is steganographically hidden inside
        # them (an anti-cast trick: the player de-embeds it in obfuscated JS). The receiver can't decode a
        # picture, so reject with a clear reason. Normal HLS segments are .ts/.m4s/.mp4/.aac, so this never
        # trips a real stream.
        _segs = [ln.strip().split("?")[0].lower() for ln in mt.splitlines() if ln.strip() and not ln.startswith("#")]
        if _segs and sum(s.endswith((".image", ".png", ".jpg", ".jpeg", ".webp", ".gif")) for s in _segs[:5]) >= min(2, len(_segs)):
            model["castable"], model["reason"] = False, "obfuscated"
    # For a live master stream, cast the fresh sniffed chunklist probed above rather than a master variant,
    # which can resolve to a stale or wrong sub-stream (rolled off, or a codec the receiver can't play).
    # fresh is only set when a master resolved, so the override applies to master streams only.
    if model["live"] and fresh:
        model["source"] = fresh
    if model["encryption"] == "drm":
        model["castable"], model["reason"] = False, "drm"
    log(f"stream: {model['role']} {model['container']} {'live' if model['live'] else 'vod'}"
        f"{' LL' if model['low_latency'] else ''}{' split-av' if model['separate_av'] else ''}"
        f" vcodec={model['video_codec'] or '?'}{(' enc=' + model['encryption']) if model['encryption'] else ''}"
        f" src={model['source'].split('?')[0].rsplit('/', 1)[-1][:44]}"
        f" -> {'cast' if model['castable'] else 'reject:' + model['reason']}")
    return model


def _classify_source(url, hdr_map):
    """Probe the cast source once. Returns (kind, is_vod, container, low_latency): kind 'file' for a direct
    media file (served with byte-range seeking) or 'hls' for a playlist (reverse-proxied); container is the
    extension to expose for a file ('mp4'/'webm'); low_latency marks an LL-HLS media playlist. A direct
    file is always seekable; an HLS with #EXT-X-ENDLIST is too; a live HLS is not. Falls back to
    ('hls', False, 'mp4', False) on any error, i.e. the current live behavior."""
    try:
        h = dict(hdr_map or {}); h.setdefault("User-Agent", "Mozilla/5.0")
        h["Range"] = "bytes=0-8191"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=6) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            head = r.read(8192)
    except Exception:
        return ("hls", False, "mp4", False)
    u = url.split("?")[0].lower()
    if "mpegurl" in ct or u.endswith(".m3u8") or head[:7] == b"#EXTM3U":
        # a low-latency media playlist (blocking reload / partial segments) has a short live window, so
        # the cast must ride near the edge with a shallow buffer or Shaka can't find enough to start.
        ll = b"CAN-BLOCK-RELOAD" in head or b"#EXT-X-PART" in head or b"PRELOAD-HINT" in head
        return ("hls", _hls_is_vod(url, hdr_map), "mp4", ll)
    if ct.startswith(("video/", "audio/")) or u.endswith((".mp4", ".webm", ".m4v", ".mov", ".mkv")):
        return ("file", True, "webm" if ("webm" in ct or u.endswith(".webm")) else "mp4", False)
    return ("hls", False, "mp4", False)


def _ytdlp_cmd():
    """argv prefix to run yt-dlp (SLC_YTDLP override, else on PATH, else next to the helper/exe), or
    None if it isn't installed. Used only for YouTube VODs, where streamlink caps at 360p."""
    cand = os.environ.get("SLC_YTDLP") or shutil.which("yt-dlp")
    if not cand:
        for d in (os.path.dirname(sys.executable or ""), os.path.dirname(os.path.abspath(__file__))):
            for name in ("yt-dlp", "yt-dlp.exe"):   # .exe covers the bundled Windows binary
                p = os.path.join(d, name)
                if d and os.path.exists(p):
                    cand = p
                    break
            if cand:
                break
    return [cand] if cand else None


def _ffmpeg_bin():
    """Path to ffmpeg (SLC_FFMPEG override, else on PATH, else the binary bundled next to the
    helper/exe, else /usr/bin/ffmpeg), or None."""
    cand = os.environ.get("SLC_FFMPEG") or shutil.which("ffmpeg")
    if not cand:
        for d in (os.path.dirname(sys.executable or ""), os.path.dirname(os.path.abspath(__file__))):
            for name in ("ffmpeg", "ffmpeg.exe"):   # .exe covers the bundled Windows binary
                p = os.path.join(d, name)
                if d and os.path.exists(p):
                    cand = p
                    break
            if cand:
                break
    if not cand and os.path.exists("/usr/bin/ffmpeg"):
        cand = "/usr/bin/ffmpeg"
    return cand or None


def _resolve_youtube(page_url, max_h=2160, itag=None):
    """yt-dlp resolve for a YouTube URL. Returns {'kind':'vod', 'video':url, 'audio':url, 'ua':ua,
    'height':h} for a recorded video (streamlink only exposes 360p for those; yt-dlp reaches the DASH
    ladder), {'kind':'live'} for a live stream (kept on streamlink, which gives its 1080p HLS), or None
    when yt-dlp is unavailable / extraction fails. With itag, that exact video format is used (the
    quality menu picks one); otherwise the best direct-URL (non-SABR) format up to max_h is chosen."""
    cmd = _ytdlp_cmd()
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd + ["-J", "--no-warnings", "--no-playlist", "--", page_url],
                             capture_output=True, text=True, timeout=45)
        info = json.loads(out.stdout or "")
    except Exception as e:
        log(f"cast: yt-dlp resolve failed: {type(e).__name__}: {str(e)[:80]}")
        return None
    if info.get("is_live"):
        return {"kind": "live"}
    fmts = info.get("formats") or []
    def _direct(f):
        return f.get("url") and f.get("protocol") == "https"
    auds = [f for f in fmts if _direct(f) and f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none")]
    if not auds:
        return None
    if itag:
        vids = [f for f in fmts if str(f.get("format_id")) == str(itag) and _direct(f)
                and f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")]
        if not vids:
            return None
        v = vids[0]
    else:
        vids = [f for f in fmts if _direct(f) and f.get("vcodec") not in (None, "none")
                and f.get("acodec") in (None, "none") and 0 < (f.get("height") or 0) <= max_h]
        if not vids:
            return None
        def _vrank(f):
            vc = f.get("vcodec") or ""
            codec_pref = 2 if vc.startswith("av01") else (1 if vc.startswith("avc") else 0)   # av1 > h264 > vp9
            sdr = 1 if (f.get("dynamic_range") or "SDR") == "SDR" else 0   # prefer SDR (lighter to render than HDR)
            return (f.get("height") or 0, f.get("fps") or 0, sdr, codec_pref, f.get("tbr") or 0)
        v = max(vids, key=_vrank)
    a = max(auds, key=lambda f: f.get("tbr") or 0)
    ua = (v.get("http_headers") or {}).get("User-Agent") or "Mozilla/5.0"
    return {"kind": "vod", "video": v["url"], "audio": a["url"], "ua": ua, "height": v.get("height")}


def _start_youtube_remux(yt, tmpdir):
    """Remux the YouTube video+audio streams into a growing HLS (fMP4) playlist in tmpdir: video is
    copied (no re-encode, so ~free) and audio -> AAC for broad HLS compatibility. Returns the ffmpeg
    Popen, or None if ffmpeg isn't installed. The child is killed with the helper (PDEATHSIG)."""
    ff = _ffmpeg_bin()
    if not ff:
        return None
    ua = yt["ua"]
    # -hls_playlist_type event (not vod): ffmpeg rewrites index.m3u8 after every segment, so playback
    # can start while the remux is still running. vod only writes the playlist at the very end, which
    # would stall the cast. All segments are kept (no sliding window), so seeking works; ffmpeg adds
    # #EXT-X-ENDLIST when it finishes, turning it into a fully seekable VOD.
    cmd = [ff, "-nostdin", "-loglevel", "error", "-y",
           "-user_agent", ua, "-i", yt["video"],
           "-user_agent", ua, "-i", yt["audio"],
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
           "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event", "-hls_segment_type", "fmp4",
           "-hls_flags", "independent_segments",
           "-hls_segment_filename", os.path.join(tmpdir, "seg%05d.m4s"),
           os.path.join(tmpdir, "index.m3u8")]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=_PDEATHSIG)


def _make_dir_server(root, tv=""):
    """CORS-enabled static file server rooted at root (the ffmpeg HLS output dir), reachable only by the
    cast target + loopback. SimpleHTTPRequestHandler already answers Range requests, so the receiver can
    seek within what ffmpeg has written."""
    class _DirHandler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def __init__(self, *a, **k):
            super().__init__(*a, directory=root, **k)
        def log_message(self, *a):
            pass
        def guess_type(self, path):
            if path.endswith(".m3u8"):
                return "application/vnd.apple.mpegurl"
            if path.endswith((".m4s", ".mp4")):
                return "video/mp4"
            return super().guess_type(path)
        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()
        def do_GET(self):
            if self.client_address[0] not in (tv, "127.0.0.1"):
                self.send_error(403)
                return
            super().do_GET()
    return _DirHandler


def run_cast(args):
    """Cast an HLS stream (live or VOD) to the branded receiver. The helper runs an authenticating HLS
    reverse-proxy (make_hls_proxy: injects the sniffed headers/token + CORS, rewrites playlist URLs
    to stay proxied); the receiver's native player handles playback: adaptive sync, gap-jumping,
    fragment/playlist retries and discontinuity handling. Sources that aren't a direct playlist
    (Twitch, etc.) are resolved to their HLS with streamlink first. A finite VOD is cast seekable."""
    import pychromecast
    from pychromecast.controllers import BaseController

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

    source_url = args.media_url or args.url
    if not source_url:
        log("cast: no source URL to proxy")
        _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)

    # Sites like Twitch/Kick/YouTube aren't a fetchable .m3u8: their stream URL is resolved fresh from
    # the page each time (a sniffed Twitch playlist URL expires quickly). Resolve those with streamlink
    # (Twitch gives its low-latency HLS); a direct .m3u8 is proxied directly. If a listed site can't be
    # resolved, fall back to proxying the given URL.
    _page = args.url or ""
    _low_latency = False
    _yt_dir = None      # temp dir + ffmpeg proc, set when a YouTube VOD is remuxed locally
    _yt_ff = None
    if ("youtube.com" in _page) or ("youtu.be" in _page):
        # streamlink only exposes 360p for a YouTube VOD; resolve those with yt-dlp and remux the DASH
        # video+audio to HLS with ffmpeg (up to SLC_YT_MAXH, default 2160p). A live YouTube stays on
        # streamlink below (it gives the 1080p HLS ladder).
        _qsel = getattr(args, "quality", "best") or "best"
        _im = re.match(r"itag:(\w+)$", _qsel)
        if _im:
            yt = _resolve_youtube(_page, itag=_im.group(1))          # exact format the quality menu picked
        else:
            _qm = re.match(r"(\d+)p", _qsel)
            _maxh = int(_qm.group(1)) if _qm else int(os.environ.get("SLC_YT_MAXH", "2160") or 2160)
            yt = _resolve_youtube(_page, _maxh)
        if yt and yt.get("kind") == "vod":
            _yt_dir = tempfile.mkdtemp(prefix="slc-yt-")
            _yt_ff = _start_youtube_remux(yt, _yt_dir)
            if _yt_ff:
                log(f"cast: youtube VOD -> yt-dlp + ffmpeg HLS remux ({yt.get('height')}p)")
            else:
                shutil.rmtree(_yt_dir, ignore_errors=True)
                _yt_dir = None
                log("cast: ffmpeg not found; falling back to streamlink (360p)")
    if _yt_dir is None and any(site in _page for site in ("twitch.tv", "kick.com", "youtube.com", "youtu.be")):
        resolved = _resolve_hls_url(_page, getattr(args, "quality", None), hdr_map)
        if resolved:
            log(f"cast: streamlink resolved {_page.split('//')[-1][:34]} -> HLS (native player)")
            source_url = resolved
            _low_latency = True   # these sources are stable enough to ride closer to the live edge
        else:
            log(f"cast: could not resolve {_page[:48]} via streamlink; proxying {source_url[:36]} as-is")

    # A sniffed HLS master + a resolution picked in the menu -> serve just that variant (no adaptive).
    # Skip this when the control server already resolved the exact media to cast (--src-kind): re-resolving
    # would refetch the source and walk sibling-master guesses again, delaying the cast for no gain.
    _vq = getattr(args, "quality", "best") or "best"
    if _yt_dir is None and args.media_url and re.match(r"\d+p", _vq) and not getattr(args, "src_kind", ""):
        for v in _hls_variants(source_url, hdr_map):
            if v["quality"] == _vq:
                source_url = v["url"]; log(f"cast: sniffed HLS -> variant {_vq}"); break

    if _yt_dir is not None:
        # YouTube VOD: serve ffmpeg's growing HLS output dir; wait for the first segment before LOAD.
        httpd = ThreadingHTTPServer((ip, args.port), _make_dir_server(_yt_dir, tv=args.tv))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _is_vod, _stream_type = True, "BUFFERED"
        hls_url = f"http://{ip}:{args.port}/index.m3u8?vod=1"
        _ready = False
        for _ in range(60):     # up to ~30s for ffmpeg to lay down the init + first segment
            if os.path.exists(os.path.join(_yt_dir, "index.m3u8")) and \
               any(fn.endswith(".m4s") for fn in os.listdir(_yt_dir)):
                _ready = True
                break
            if _yt_ff.poll() is not None:     # ffmpeg exited before producing a segment
                break
            time.sleep(0.5)
        if not _ready:
            log("cast: youtube remux produced no output (ffmpeg failed); aborting cast")
            try:
                _yt_ff.terminate()
            except Exception:
                pass
            shutil.rmtree(_yt_dir, ignore_errors=True)
            httpd.shutdown(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)
        log(f"youtube HLS remux at {hls_url} -> cast (VOD/seekable) to {args.cast_name or args.tv}")
    else:
        # Classify the source once: a direct media file (mp4/webm) is served with byte-range seeking; an
        # HLS playlist keeps the reverse-proxy path. A finite VOD (a file, or an HLS with #EXT-X-ENDLIST)
        # is cast as a seekable BUFFERED stream; a live source stays LIVE. ?vod=1 / ?ll=1 carry that to
        # the receiver (ll=1 also rides closer to the live edge). The proxy ignores the query.
        # The control server passes its own classification (--src-kind) for a sniffed HLS source, so reuse
        # it instead of refetching the source to re-probe (faster start; and it doesn't spend a single-use
        # token). Fall back to probing here for the single-URL path that carried no classification.
        if getattr(args, "src_kind", ""):
            _kind, _is_vod, _container, _ll = args.src_kind, args.src_vod, "mp4", args.src_ll
        else:
            _kind, _is_vod, _container, _ll = _classify_source(source_url, hdr_map)
        if _ll and not _is_vod:
            _low_latency = True   # LL-HLS live: ride near the edge with a shallow buffer so it can start
        httpd = ThreadingHTTPServer((ip, args.port),
                                    make_hls_proxy(source_url, hdr_map, tv=args.tv, media_kind=_kind))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _stream_type = "BUFFERED" if _is_vod else "LIVE"
        _marks = []
        if _low_latency and _kind != "file":
            _marks.append("ll=1")
        if _is_vod:
            _marks.append("vod=1")
        _path = f"/live.{_container}" if _kind == "file" else "/live.m3u8"
        hls_url = f"http://{ip}:{args.port}{_path}" + ("?" + "&".join(_marks) if _marks else "")
        log(f"{_kind} proxy at {hls_url} -> cast ({'VOD/seekable' if _is_vod else 'live'}) to {args.cast_name or args.tv}")

    if not _tcp_open(args.tv, args.cast_port):
        log(f"cast: {args.tv}:{args.cast_port} not reachable. Is the TV on (not in standby)?")
        httpd.shutdown(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)
    try:
        cc = _cast_connect(args.tv, args.cast_port, args.cast_uuid, args.cast_model, args.cast_name)
    except Exception as e:
        log(f"cast connect failed: {type(e).__name__}: {str(e)[:80]}")
        httpd.shutdown(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)

    class _SlcChannel(BaseController):
        """Private receiver channel: sends play/stop, receives {type:'status'} pushes."""
        def __init__(self):
            super().__init__("urn:x-cast:slc")
            self.last = {}
            self.last_at = 0.0

        def receive_message(self, _message, data):
            if isinstance(data, dict) and data.get("type") == "status":
                self.last = data
                self.last_at = time.time()
                return True
            return False

    slc = _SlcChannel()
    try:
        cc.register_handler(slc)
    except AttributeError:
        cc.socket_client.register_handler(slc)

    def _quit():
        try:
            if getattr(cc, "app_id", None) == _CAST_RECEIVER_APP:
                try:
                    slc.send_message({"type": "stop"})
                except Exception:
                    pass
                cc.quit_app()
        except Exception:
            pass
        try:
            cc.disconnect()
        except Exception:
            pass
    atexit.register(_quit)

    # Fetch the stream's OWN title (streamlink metadata) in the background while the receiver app
    # launches below, so it reaches the LOAD with no added latency. Falls back to the sender's tab
    # title when streamlink has no title for the source (e.g. a raw HLS URL).
    _stitle = {"t": ""}
    _title_src = args.url or source_url
    _title_thr = None
    if _title_src:
        _title_thr = threading.Thread(
            target=lambda: _stitle.__setitem__("t", fetch_stream_title(_title_src) or ""), daemon=True)
        _title_thr.start()

    # launch the branded receiver and wait for it to come to the foreground
    if getattr(cc, "app_id", None) != _CAST_RECEIVER_APP:
        try:
            cc.start_app(_CAST_RECEIVER_APP)
        except Exception as e:
            # a slow cold launch can outlast the ~10s start_app timeout; the app is still coming up,
            # so wait for it below
            log(f"cast: start_app slow ({type(e).__name__}); waiting for the receiver to foreground")
    for _ in range(40):
        if getattr(cc, "app_id", None) == _CAST_RECEIVER_APP:
            break
        time.sleep(0.5)
    if getattr(cc, "app_id", None) != _CAST_RECEIVER_APP:
        log("cast: receiver app did not launch")
        httpd.shutdown(); _safe_unlink(PIDFILE); clear_cast_state(); os._exit(1)

    # title for the receiver's native UI: the stream's own title (fetched above) if streamlink found
    # one, else the sender's tab title, else a generic label (never the raw CDN host)
    if _title_thr:
        _title_thr.join(timeout=4)   # usually already finished (it overlapped the app launch above)
    _title = _stitle["t"] or (getattr(args, "title", "") or "").strip() or "En vivo"
    # Standard CAF media LOAD -> the receiver's native player (Shaka) plays the proxied HLS, so the
    # native UI + TV-remote/phone controls + BACK/exit all work. The receiver's shakaConfig keeps
    # playback tolerant of segment gaps and stalls (retries / stall-skip / gap-jump / deep buffer).
    mc = cc.media_controller
    try:
        mc.play_media(hls_url, "application/x-mpegurl", title=_title, stream_type=_stream_type)
        try:
            mc.block_until_active(timeout=10)
        except Exception:
            pass
        log("cast: LOAD sent (native Shaka player, tolerant config)")
    except Exception as e:
        log(f"cast: LOAD failed: {type(e).__name__}: {str(e)[:80]}")

    # ---- steady state, DLNA-simple: the receiver self-heals (reconnect loop); we only tear down
    # when the user leaves (app change / stop) or the control connection is truly gone. ----
    POLL = 1.5
    played = False
    reconnect_tries = 0
    tele = 0                       # telemetry heartbeat (log latency/buffer every ~10s)
    last_stalls = 0
    last_err = None
    load_attempts = 1              # the initial play_media above is attempt 1
    last_load_at = time.monotonic()
    gave_up = False
    MAX_LOAD_ATTEMPTS = 5          # if the receiver reports a load error before playback starts, the
                                   # session just sits idle; re-send the LOAD ourselves up to this many
                                   # times (a source that's slow to start often succeeds on a later try)
    while True:
        time.sleep(POLL)
        app = getattr(cc, "app_id", None)
        if app != _CAST_RECEIVER_APP:
            log(f"cast: receiver no longer active on the TV (app={app}); shutting down")
            break
        try:
            connected = bool(cc.socket_client and cc.socket_client.is_connected)
        except Exception:
            connected = True
        if not connected:
            reconnect_tries += 1
            if reconnect_tries > 5:
                log("cast: control connection lost; shutting down")
                break
            log(f"cast: reconnecting to the TV ({reconnect_tries}/5)")
            try:
                cc.wait(timeout=8)
            except Exception:
                pass
            continue
        reconnect_tries = 0
        st = slc.last.get("state") if slc.last else None
        if st == "playing" and not played:
            played = True
            log(f"cast: playing (receiver rx={slc.last.get('ver')})")
        # surface the receiver's err/state changes in the log as they happen
        err = slc.last.get("err") if slc.last else None
        if err and err != last_err:
            log(f"cast: receiver err -> {err}")
            last_err = err
        # Auto-retry a failed initial LOAD: if the receiver reports a load error before playback starts,
        # re-send the LOAD. It hits the same warm proxy for a fresh live playlist, so a source that's
        # slow to start usually catches on a later attempt. Bounded and cooled down so a lingering error
        # (or a genuinely dead source) can't storm.
        if not played and err and err.startswith("shaka/"):
            if load_attempts < MAX_LOAD_ATTEMPTS and (time.monotonic() - last_load_at) > 4:
                load_attempts += 1
                log(f"cast: load failed ({err}); auto-retry {load_attempts}/{MAX_LOAD_ATTEMPTS}")
                try:
                    mc.play_media(hls_url, "application/x-mpegurl", title=_title, stream_type=_stream_type)
                    last_load_at = time.monotonic()
                except Exception as e:
                    log(f"cast: auto-retry LOAD send failed: {type(e).__name__}: {str(e)[:60]}")
            elif load_attempts >= MAX_LOAD_ATTEMPTS and not gave_up:
                gave_up = True
                log(f"cast: still failing after {MAX_LOAD_ATTEMPTS} loads ({err}); source may be down "
                    f"or too slow to start; re-cast to retry")
        if slc.last:
            stalls = int(slc.last.get("stalls") or 0)
            if stalls > last_stalls:
                log(f"cast: receiver stall (#{stalls}, state {st})")
                last_stalls = stalls
            tele += 1
            if tele >= 7:          # ~ every 10s
                tele = 0
                log(f"cast: telemetry rx={slc.last.get('ver')} state={st} buf={slc.last.get('buf')}s "
                    f"t={slc.last.get('t')} stalls={stalls} err={slc.last.get('err')}")

    _quit()
    httpd.shutdown()
    if _yt_ff:
        try:
            _yt_ff.terminate()
        except Exception:
            pass
    if _yt_dir:
        shutil.rmtree(_yt_dir, ignore_errors=True)
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
                    log(f"DLNA: renderer unreachable (re-push retry {retries}/{DLNA_MAX_RETRIES})")
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
    ap.add_argument("--src-kind", default="", help=argparse.SUPPRESS)       # control's source classification (hls) -> skip the proxy re-probe
    ap.add_argument("--src-vod", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--src-ll", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--title", default="", help="title to show on the renderer (defaults to the URL)")
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
    ap.add_argument("--serve", action="store_true", help="run the local control server for the browser extension")
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
