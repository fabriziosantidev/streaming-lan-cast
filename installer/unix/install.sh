#!/usr/bin/env bash
# Streaming LAN Cast: Linux + macOS installer (per-user, no sudo).
#
# Installs the local helper into an isolated Python venv and autostarts it:
#   - Linux: a systemd --user service
#   - macOS: a launchd LaunchAgent
# The helper is a loopback control server the browser extension talks to; pair
# them with the token printed at the end.
#
# Usage:  ./install.sh        (run as your normal user, never with sudo)

set -euo pipefail

APP_NAME="Streaming LAN Cast"
SERVICE="streaming-lan-cast"
LABEL="com.fabriziosantidev.streaming-lan-cast"   # launchd job label (macOS)
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/streaming-lan-cast"
TOKEN_FILE="$HOME/.streaming-lan-cast/token"
VENV="$DATA_DIR/venv"
HELPER_DST="$DATA_DIR/streaming-lan-cast-helper.py"

OS="$(uname -s)"   # Linux or Darwin
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"   # Linux
UNIT_FILE="$UNIT_DIR/${SERVICE}.service"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"          # macOS

# Resolve our own location when run from a file, so sibling dirs hold. `readlink -f`
# is GNU-only (missing on macOS), so use cd + `pwd -P` instead. When piped
# (curl ... | bash) BASH_SOURCE is empty, so leave the paths blank and let the
# download branches take over.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  HELPER_SRC="$SCRIPT_DIR/../../helper/streaming-lan-cast-helper.py"
else
  SCRIPT_DIR=""
  HELPER_SRC=""   # piped install, forces the fetch (download) branches
fi

# Plain output, with color only on a real terminal.
if [ -t 1 ]; then BOLD=$'\e[1m'; DIM=$'\e[2m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RED=$'\e[31m'; RST=$'\e[0m'
else BOLD=""; DIM=""; GRN=""; YEL=""; RED=""; RST=""; fi
say()  { printf '%s\n' "$*"; }
step() { printf '%s\n' "${BOLD}$*${RST}"; }
warn() { printf '%s\n' "${YEL}! $*${RST}" >&2; }
die()  { printf '%s\n' "${RED}error: $*${RST}" >&2; exit 1; }

# Yes/no prompt (default yes) that works even under `curl ... | bash`: stdin is the piped
# script there, so read the user's terminal via /dev/tty. The /dev/tty node can be
# "readable" yet fail to open (ENXIO) when there's no controlling terminal, so probe it
# quietly first and bail to "no" when non-interactive.
ask_yn() {
  local ans
  { : < /dev/tty; } 2>/dev/null || return 1
  printf '%s [Y/n] ' "$1" > /dev/tty 2>/dev/null
  read -r ans < /dev/tty 2>/dev/null || return 1
  case "$ans" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# Where to fetch files when running WITHOUT a local checkout (i.e. `curl ... | bash`).
# Override via SLC_REPO_RAW=... to install from a fork.
REPO_RAW="${SLC_REPO_RAW:-https://raw.githubusercontent.com/fabriziosantidev/streaming-lan-cast/main}"
HELPER_URL="${SLC_HELPER_URL:-$REPO_RAW/helper/streaming-lan-cast-helper.py}"
UNINSTALL_URL="${SLC_UNINSTALL_URL:-$REPO_RAW/installer/unix/uninstall.sh}"

# Download URL $1 to file $2 over HTTPS (curl or wget). Atomic: writes a temp file and
# only moves it into place on success, so a failed transfer never leaves a partial/0-byte
# target (wget -O truncates up front). Nonzero if neither tool is present or it fails.
fetch() {
  local url="$1" dst="$2" tmp="$2.part.$$"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 30 "$url" -o "$tmp" || { rm -f "$tmp"; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q --timeout=30 --tries=3 -O "$tmp" "$url" || { rm -f "$tmp"; return 1; }
  else
    return 1
  fi
  mv -f "$tmp" "$dst"
}

# Bootstrap pip into a venv created without it (systems lacking ensurepip, e.g.
# Debian/Ubuntu without python3-venv) using the official get-pip.py. Keeps it sudo-free.
bootstrap_pip() {
  local venv="$1" getpip="$DATA_DIR/get-pip.py"
  local hint="Or install your distro's python venv package and retry (Debian/Ubuntu: python3-venv; Fedora/Arch/macOS: ships with python)."
  fetch "https://bootstrap.pypa.io/get-pip.py" "$getpip" \
    || die "could not download get-pip.py (network/proxy/TLS, or no curl/wget?). $hint"
  "$venv/bin/python" "$getpip" --quiet \
    || { rm -f "$getpip"; die "get-pip.py failed to install pip. $hint"; }
  rm -f "$getpip"
}

if [ "$(id -u)" = "0" ]; then
  die "do not run as root: this is a per-user install. Run it as yourself."
fi

say "${BOLD}$APP_NAME installer${RST}"
say ""

# --- prerequisites -----------------------------------------------------------
case "$OS" in
  Linux|Darwin) ;;
  *) die "unsupported OS '$OS'. This installer is for Linux and macOS; on Windows use the Inno Setup installer." ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then
    if ask_yn "python3 not found. Install Apple's Command Line Tools now (they include Python)?"; then
      xcode-select --install 2>/dev/null || true
      say ""
      warn "A Command Line Tools installer opened in a separate window."
      say "Finish it, then re-run this installer."
      say "(If its Python turns out older than 3.10, install a newer one with: brew install python.)"
      exit 1
    fi
    die "python3 not found. Install it with Homebrew (brew install python) or 'xcode-select --install', then re-run."
  else
    die "python3 not found. Install Python 3 (Debian/Ubuntu: python3 python3-venv; Fedora: python3; Arch: python)."
  fi
fi
# streamlink requires Python 3.10+, so fail fast rather than partway through the install.
python3 - <<'PY' || die "Python 3.10+ is required (streamlink needs it)."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
# The venv module itself must be importable; pip is handled later (with a get-pip.py
# fallback when ensurepip is missing, so no sudo is required).
python3 -c "import venv" 2>/dev/null || \
  die "the 'venv' module is missing. Install your distro's python venv package (Debian/Ubuntu: python3-venv; Fedora/Arch/macOS: ships with python)."

if [ "$OS" = "Linux" ]; then
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found: the Linux installer needs a systemd user session for autostart."
  systemctl --user show-environment >/dev/null 2>&1 || \
    die "no systemd user session. Log in to a normal graphical/SSH session (not a bare root shell) and retry."
else
  command -v launchctl >/dev/null 2>&1 || die "launchctl not found: this does not look like macOS."
fi

# ffmpeg is optional: only streams with separate audio+video tracks need it.
if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "ffmpeg not found. Most live streams cast fine without it, but a few (separate audio/video) need it."
  if [ "$OS" = "Darwin" ]; then warn "  install later if a stream fails: brew install ffmpeg."
  else warn "  install later if a stream fails (Debian/Ubuntu: sudo apt install ffmpeg; Fedora: sudo dnf install ffmpeg; Arch: sudo pacman -S ffmpeg)."; fi
fi

# --- venv + streamlink -------------------------------------------------------
step "[1/4] Python environment"
mkdir -p "$DATA_DIR"
pyver() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }
sys_pyver="$(pyver python3)"
venv_pyver=""
[ -x "$VENV/bin/python" ] && venv_pyver="$(pyver "$VENV/bin/python")"
if [ -n "$venv_pyver" ] && [ "$venv_pyver" = "$sys_pyver" ]; then
  say "  reusing existing venv"
  "$VENV/bin/python" -m pip --version >/dev/null 2>&1 || bootstrap_pip "$VENV"
else
  # Absent, or stale after a Python minor-version upgrade (the old venv points at a
  # removed interpreter): rebuild from scratch and drop the orphaned tree.
  [ -e "$VENV" ] && rm -rf "$VENV"
  if python3 -c "import ensurepip" 2>/dev/null; then
    say "  creating venv at $VENV"
    python3 -m venv "$VENV"
  else
    say "  creating venv at $VENV (bootstrapping pip without ensurepip)"
    python3 -m venv --without-pip "$VENV"
    bootstrap_pip "$VENV"
  fi
fi
say "  installing streamlink + pychromecast + ffmpeg (this downloads a few MB)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
# streamlink resolves the stream; pychromecast drives Google Cast / Android TV targets;
# static-ffmpeg provides a bundled ffmpeg used to remux HLS into a Cast-friendly low-latency feed
# (a system ffmpeg, if present, is preferred). ffmpeg is only needed for casting to Cast devices.
# Canonical runtime list lives in requirements.txt; keep this in sync (installed inline so the
# curl | bash one-liner needs no checkout).
"$VENV/bin/python" -m pip install --quiet --upgrade streamlink pychromecast static-ffmpeg
# pre-fetch the static ffmpeg binary so the first cast isn't delayed by a download
"$VENV/bin/python" -c "import static_ffmpeg.run as r; r.get_or_fetch_platform_executables_else_raise()" >/dev/null 2>&1 || true
say "  ${DIM}$("$VENV/bin/python" -m streamlink --version 2>/dev/null || echo streamlink)${RST}"

# --- helper ------------------------------------------------------------------
step "[2/4] Helper"
if [ -f "$HELPER_SRC" ]; then
  install -m 0644 "$HELPER_SRC" "$HELPER_DST"           # local checkout
  say "  installed $HELPER_DST"
else
  say "  downloading helper"                              # curl|bash install (no checkout)
  fetch "$HELPER_URL" "$HELPER_DST" || die "could not download the helper from $HELPER_URL"
  chmod 0644 "$HELPER_DST"
  say "  installed $HELPER_DST"
fi
# Drop a self-contained uninstaller into the data dir so the install can be removed
# later WITHOUT the repo (e.g. after a curl|bash install). It detects the OS itself.
UNINSTALL_DST="$DATA_DIR/uninstall.sh"
if [ -f "$SCRIPT_DIR/uninstall.sh" ]; then
  install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$UNINSTALL_DST"
elif fetch "$UNINSTALL_URL" "$UNINSTALL_DST"; then
  chmod 0755 "$UNINSTALL_DST"
else
  UNINSTALL_DST=""   # couldn't place one; fall back to documenting the manual steps
  warn "could not install the uninstaller; see the manual steps in the final message."
fi

# --- autostart ---------------------------------------------------------------
if [ "$OS" = "Darwin" ]; then
  step "[3/4] Autostart (launchd LaunchAgent)"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$HELPER_DST</string>
    <string>--serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
</dict>
</plist>
EOF
  say "  wrote $PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true          # idempotent: ignore "not loaded"
  if launchctl load -w "$PLIST" 2>/dev/null; then
    say "  LaunchAgent loaded and started"
  else
    warn "could not load the LaunchAgent automatically. Run: launchctl load -w \"$PLIST\""
  fi
else
  step "[3/4] Autostart (systemd user service)"
  mkdir -p "$UNIT_DIR"
  # Generated with absolute paths so it works regardless of where this repo lives.
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=$APP_NAME helper (local control server for the browser extension)

[Service]
Type=simple
ExecStart="$VENV/bin/python" "$HELPER_DST" --serve
# Signal only the main --serve process on stop/restart, leaving a detached cast
# proxy running so the documented "restart after update" doesn't cut the TV; the
# helper detects the survivor via its pidfile and recovers instead of relaunching.
KillMode=process
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  say "  wrote $UNIT_FILE"
  systemctl --user daemon-reload || warn "daemon-reload returned nonzero; continuing"
  if systemctl --user enable --now "${SERVICE}.service"; then
    say "  service enabled and started"
  else
    warn "could not start the service automatically (no active user session bus, or it failed to launch)."
    warn "  after logging into your desktop session, run: systemctl --user enable --now ${SERVICE}.service"
    warn "  for headless/SSH autostart, also run:          loginctl enable-linger \"$USER\""
  fi
fi

# --- token -------------------------------------------------------------------
step "[4/4] Pairing token"
token=""
for _ in $(seq 1 30); do
  if [ -s "$TOKEN_FILE" ]; then token="$(tr -d '[:space:]' < "$TOKEN_FILE")"; fi
  [ -n "$token" ] && break
  sleep 0.3
done

# Is the helper actually running? `launchctl list LABEL` exits 0 whenever the job is
# merely loaded (it always is, right after `load -w`), even if the helper crashed and
# launchd is throttling its respawn. The dict only has a "PID" key while the process is
# alive, so key off that to match the Linux is-active semantics. Poll briefly to settle.
running=1
if [ "$OS" = "Darwin" ]; then
  running=0
  for _ in 1 2 3 4 5; do
    if launchctl list "$LABEL" 2>/dev/null | grep -q '"PID"'; then running=1; break; fi
    sleep 0.3
  done
else
  systemctl --user is-active --quiet "${SERVICE}.service" || running=0
fi

say ""
if [ "$running" = 1 ]; then
  say "${GRN}${BOLD}Installation complete. The helper is running and starts automatically at login.${RST}"
elif [ "$OS" = "Darwin" ]; then
  warn "the helper did not stay running. Check it with:  launchctl list | grep ${SERVICE}"
else
  warn "the service did not stay active. Check it with:  systemctl --user status ${SERVICE}.service"
fi
say ""

if [ -n "$token" ]; then
  line="$(printf '%*s' 52 '' | tr ' ' '-')"
  say "  Paste this token into the extension (Options):"
  say "  +$line+"
  printf '  | %-50s |\n' "$token"
  say "  +$line+"
  # Deliberately NOT auto-copied to the clipboard: clipboard-history managers
  # persist entries to disk, which would leak this long-lived secret.
else
  warn "could not read the token yet. It will appear at: $TOKEN_FILE"
fi
say ""

say "${DIM}Manage it with:${RST}"
if [ "$OS" = "Darwin" ]; then
  say "  launchctl list | grep ${SERVICE}                         # is it running?"
  say "  launchctl unload \"$PLIST\" && launchctl load -w \"$PLIST\"   # restart after an update"
  say "  launchctl unload \"$PLIST\"                               # stop + remove autostart"
else
  say "  systemctl --user status  ${SERVICE}.service     # is it running?"
  say "  systemctl --user restart ${SERVICE}.service     # after an update"
  say "  systemctl --user disable --now ${SERVICE}.service   # stop + remove autostart"
fi
if [ -n "$UNINSTALL_DST" ]; then
  say "  ${UNINSTALL_DST}   # full uninstall (service + venv + token)"
fi
