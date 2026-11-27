#!/usr/bin/env bash
# Start ArduCopter SITL plus MAVProxy for local testing.
#
#   ./scripts/sitl.sh                 run in the foreground, Ctrl+C stops everything
#   ./scripts/sitl.sh --background    start and return; logs and pids in .sitl/
#   ./scripts/sitl.sh --stop          stop a background run
#   ./scripts/sitl.sh --no-mavproxy   skip MAVProxy; connect to tcp:127.0.0.1:5760 instead
#
# Ports (with MAVProxy):
#   udp 14550  quad_controller (config target "sitl")
#   udp 14551  Mission Planner / QGroundControl, if you want to watch along
#
# Environment:
#   ARDUCOPTER_BIN   path to an arducopter SITL binary (skips the download)
#   SITL_VERSION     firmware.ardupilot.org channel: stable (default), beta, latest
#   SITL_HOME        lat,lon,alt,heading (default: ArduPilot's CMAC test field)
#   SITL_SPEEDUP     simulation speed multiplier (default 1)
#
# Works on Linux x86_64 and in WSL2 on Windows. See docs/setup/sitl.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$ROOT/.sitl"
PIDS="$WORK/pids"
SITL_VERSION="${SITL_VERSION:-stable}"
SITL_HOME="${SITL_HOME:--35.363261,149.165230,584,353}"
SITL_SPEEDUP="${SITL_SPEEDUP:-1}"
PARAMS_URL="https://raw.githubusercontent.com/ArduPilot/ardupilot/master/Tools/autotest/default_params/copter.parm"

background=0
use_mavproxy=1
for arg in "$@"; do
  case "$arg" in
    --background) background=1 ;;
    --no-mavproxy) use_mavproxy=0 ;;
    --stop)
      if [[ -f "$PIDS" ]]; then
        while read -r pid; do kill "$pid" 2>/dev/null || true; done < "$PIDS"
        rm -f "$PIDS"
        echo "SITL stopped"
      else
        echo "no background SITL running"
      fi
      exit 0 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$WORK"
cd "$WORK"

if [[ -f "$PIDS" ]]; then
  echo "SITL already running (pids in $PIDS). Use --stop first." >&2
  exit 1
fi

# 1. Get a SITL binary.
bin="${ARDUCOPTER_BIN:-$WORK/arducopter}"
if [[ ! -x "$bin" ]]; then
  arch="$(uname -m)"
  if [[ "$arch" != "x86_64" ]]; then
    echo "No prebuilt SITL for $arch. Build it from source and set ARDUCOPTER_BIN:" >&2
    echo "  https://ardupilot.org/dev/docs/building-setup-linux.html" >&2
    exit 1
  fi
  url="https://firmware.ardupilot.org/Copter/$SITL_VERSION/SITL_x86_64_linux_gnu/arducopter"
  echo "Downloading SITL ($SITL_VERSION) from $url"
  curl -fsSL -o "$WORK/arducopter.tmp" "$url"
  chmod +x "$WORK/arducopter.tmp"
  mv "$WORK/arducopter.tmp" "$WORK/arducopter"
  bin="$WORK/arducopter"
fi

# 2. Default copter params (frame, simulated sensors).
if [[ ! -f "$WORK/copter.parm" ]]; then
  echo "Downloading default copter params"
  curl -fsSL -o "$WORK/copter.parm" "$PARAMS_URL"
fi

if [[ $use_mavproxy -eq 1 ]] && ! command -v mavproxy.py >/dev/null; then
  echo "mavproxy.py not found. Install it with: pip install -e '.[sitl]'" >&2
  echo "or run with --no-mavproxy and connect to tcp:127.0.0.1:5760" >&2
  exit 1
fi

pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  rm -f "$PIDS"
}

# 3. Start SITL. It listens on tcp:5760 (SERIAL0) and waits for a client.
# SITL exits when its parent process goes away, so it runs under a small
# subshell that stays alive after this script returns in --background mode.
rm -f "$WORK/arducopter.pid"
(
  "$bin" --model + --speedup "$SITL_SPEEDUP" --defaults "$WORK/copter.parm" \
    --home "$SITL_HOME" -I0 > "$WORK/arducopter.log" 2>&1 &
  echo $! > "$WORK/arducopter.pid"
  wait
) &
pids+=($!)
for _ in $(seq 1 50); do [[ -s "$WORK/arducopter.pid" ]] && break; sleep 0.1; done
pids+=("$(cat "$WORK/arducopter.pid")")
echo "SITL started (pid ${pids[-1]}), log: $WORK/arducopter.log"

# Wait until SITL reports that it is listening on port 5760.
for _ in $(seq 1 100); do
  grep -q "5760" "$WORK/arducopter.log" 2>/dev/null && break
  if ! kill -0 "${pids[-1]}" 2>/dev/null; then
    echo "SITL exited early; see $WORK/arducopter.log" >&2
    cat "$WORK/arducopter.log" >&2
    exit 1
  fi
  sleep 0.1
done

# 4. MAVProxy fans SITL out to UDP ports.
if [[ $use_mavproxy -eq 1 ]]; then
  mavproxy.py --master tcp:127.0.0.1:5760 \
    --out udp:127.0.0.1:14550 --out udp:127.0.0.1:14551 \
    --non-interactive --state-basedir "$WORK" > "$WORK/mavproxy.log" 2>&1 &
  pids+=($!)
  echo "MAVProxy started (pid ${pids[-1]}): udp 14550 (quadctl), udp 14551 (GCS)"
  echo "Connect with: quadctl check --target sitl"
else
  echo "No MAVProxy. Connect with: quadctl check --connection tcp:127.0.0.1:5760"
fi

printf '%s\n' "${pids[@]}" > "$PIDS"

if [[ $background -eq 1 ]]; then
  echo "Running in the background. Stop with: ./scripts/sitl.sh --stop"
  exit 0
fi

trap cleanup EXIT INT TERM
echo "Press Ctrl+C to stop."
wait
