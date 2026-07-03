#!/usr/bin/env bash
# Streaming LAN Cast: Linux + macOS uninstaller (per-user).
# Stops and removes the autostart service (systemd on Linux, launchd on macOS),
# the venv, and the per-install token.

set -euo pipefail

SERVICE="streaming-lan-cast"
LABEL="com.fabriziosantidev.streaming-lan-cast"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/streaming-lan-cast"
UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${SERVICE}.service"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TOKEN_DIR="$HOME/.streaming-lan-cast"
OS="$(uname -s)"

if [ -t 1 ]; then BOLD=$'\e[1m'; GRN=$'\e[32m'; RST=$'\e[0m'; else BOLD=""; GRN=""; RST=""; fi
say() { printf '%s\n' "$*"; }

say "${BOLD}Streaming LAN Cast: uninstaller${RST}"

# Stop + remove the autostart service (ignore if it was never installed).
if [ "$OS" = "Darwin" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
else
  systemctl --user disable --now "${SERVICE}.service" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  systemctl --user daemon-reload 2>/dev/null || true
fi
say "  removed autostart service"

# Reap any still-running cast proxy before deleting the venv it runs from. The proxy
# survives the stop above (Linux KillMode=process / launchd only signals the main job)
# and is a session leader, so kill the whole GROUP to take its streamlink/ffmpeg
# children down too, else the TV keeps streaming after uninstall. The pid lives in a
# temp dir derived from tempfile.gettempdir(), so honor $TMPDIR/$TMP and the /tmp fallback.
TMP="$(python3 -c 'import tempfile; print(tempfile.gettempdir())' 2>/dev/null || echo /tmp)"
for d in "${TMPDIR:-}" "$TMP" /tmp; do
  [ -n "$d" ] || continue
  pidf="$d/streaming-lan-cast-proxy.pid"
  if [ -f "$pidf" ]; then
    pid="$(cat "$pidf" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) ;;   # empty / non-numeric -> skip (don't pass garbage to kill)
      *)
        # Confirm it's still our helper (guards against PID reuse). /proc on Linux,
        # `ps` on macOS/BSD (no /proc there).
        ours=0
        if [ -r "/proc/$pid/cmdline" ]; then
          grep -qa 'streaming-lan-cast' "/proc/$pid/cmdline" 2>/dev/null && ours=1
        elif ps -ww -p "$pid" -o command= 2>/dev/null | grep -q 'streaming-lan-cast'; then
          ours=1
        fi
        if [ "$ours" = 1 ]; then
          kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
          sleep 1
          kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
        fi
        ;;
    esac
  fi
  rm -f "$pidf" "$d/streaming-lan-cast-state.json" "$d/streaming-lan-cast.log" 2>/dev/null || true
done

# Remove the install (venv + helper) and the token directory.
rm -rf "$DATA_DIR"
say "  removed $DATA_DIR"
rm -rf "$TOKEN_DIR"
say "  removed $TOKEN_DIR"

say "${GRN}Done. You can remove the browser extension from your browser separately.${RST}"
