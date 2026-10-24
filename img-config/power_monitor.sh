#!/usr/bin/env bash
# Purpose: Power-loss monitor. On a verified power cut it runs the hooks in
# HOOK_DIR (e.g. to checkpoint a print), syncs and powers off while the
# super-capacitor keeps the board alive (~6 s on a Neptune 4 Max).
#
# The power-sense pins depend on the board, so they come from a profile picked
# from the model in /boot/.OpenNept4une.txt (override with PROFILE=...):
#   plus-max  Neptune 4 Plus / Max. One line, gpiochip2 line 3 (GPIO2_A3,
#             sysfs gpio67): inactive = power OK, active = lost. This is the
#             pin Elegoo's own Plus/Max firmware (APP 1.2.3.4) watches.
#   dual      Neptune 4 / 4 Pro. PWRLOSS gpiochip1 line 10 (rising = loss) and
#             PWRGOOD line 19 (falling = loss), as in Elegoo's N4 Pro firmware.
#             On the Max, line 10 toggles constantly and line 19 never changes.

set -euo pipefail

# ===== Configuration =====
DEBUG="${DEBUG:-false}"       # no GPIO access at all, simulated events
DRY_RUN="${DRY_RUN:-false}"   # real GPIO monitoring, but never hooks or poweroff
LOG_TAG="${LOG_TAG:-power_monitor}"
MODEL_FILE="${MODEL_FILE:-/boot/.OpenNept4une.txt}"
CHIP_SUPERCAP="${CHIP_SUPERCAP:-gpiochip1}"
LINE_SUPERCAP="${LINE_SUPERCAP:-21}"
VERIFY_SAMPLES="${VERIFY_SAMPLES:-5}"
VERIFY_INTERVAL_MS="${VERIFY_INTERVAL_MS:-40}"
# plus-max: the loss line bounces for ~2 ms when the supply fails, then stays
# active. With libgpiod v2 the kernel debounces it (an edge is only reported once
# the line has been stable this long); v1 has no debounce, so there a rising
# edge must not be followed by a falling one within CONFIRM_SECONDS instead.
DEBOUNCE_MS="${DEBOUNCE_MS:-50}"
CONFIRM_SECONDS="${CONFIRM_SECONDS:-0.1}"
# Executables in this directory run (in name order) after a verified power cut,
# before poweroff, e.g. to checkpoint a running print. They share HOOK_TIMEOUT,
# which has to leave time for sync within the super-capacitor's hold-up.
HOOK_DIR="${HOOK_DIR:-/etc/power_monitor.d}"
HOOK_TIMEOUT="${HOOK_TIMEOUT:-2}"
# Optional extra log file, synced after every line so it survives the power loss
LOG_FILE="${LOG_FILE:-}"

for flag in DEBUG DRY_RUN; do
  case "${!flag}" in true|false) ;; *) echo "$flag must be true or false" >&2; exit 1 ;; esac
done

# ===== Helpers =====
log() {
  echo "$1" | systemd-cat -t "$LOG_TAG" -p info; echo "$1"
  if [ -n "$LOG_FILE" ]; then
    echo "$(awk '{printf "%.2f", $1}' /proc/uptime) $1" >> "$LOG_FILE"
    sync "$LOG_FILE"
  fi
}
die() { log "ERROR: $*"; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Required command '$1' not found"; }
ms_sleep() { python3 - <<PY || sleep 0
import time; time.sleep(${1}/1000.0)
PY
}

detect_profile() {
  if [ -n "${PROFILE:-}" ]; then
    echo "$PROFILE"
    return
  fi
  case "$(grep -m1 -iE '^n4' "$MODEL_FILE" 2>/dev/null | tr '[:upper:]' '[:lower:]')" in
    n4plus*|n4max*) echo "plus-max" ;;
    *) echo "dual" ;;
  esac
}

# ===== Pre-flight =====
need_cmd gpioinfo; need_cmd gpioget; need_cmd gpioset; need_cmd gpiomon; need_cmd systemctl; need_cmd stdbuf
[ "$(id -u)" -eq 0 ] || die "This script must be run as root"

log "Detected libgpiod: $(gpioinfo --version 2>&1 | head -n1)"
$DEBUG && log "===== DEBUG MODE (no GPIO access, no destructive ops) ====="
$DRY_RUN && log "===== DRY RUN (real GPIO monitoring, no hooks or poweroff) ====="

# Detect v2 tool syntax
USE_V2=false
if gpioget --help 2>&1 | grep -q '\-c, \-\-chip'; then
  USE_V2=true; log "Using libgpiod v2 tool syntax"
else
  USE_V2=false; log "Using libgpiod v1 tool syntax"
fi

PROFILE="$(detect_profile)"
case "$PROFILE" in
  plus-max)
    CHIP_LOSS="${CHIP_LOSS:-gpiochip2}"
    LINE_LOSS="${LINE_LOSS:-3}"
    log "Profile plus-max: power loss = ${CHIP_LOSS} line ${LINE_LOSS} active (debounce ${DEBOUNCE_MS} ms)"
    ;;
  dual)
    CHIP="${CHIP:-gpiochip1}"
    LINE_PWRLOSS="${LINE_PWRLOSS:-10}"   # rising = loss
    LINE_PWRGOOD="${LINE_PWRGOOD:-19}"   # falling = loss
    log "Profile dual: power loss = ${CHIP} line ${LINE_PWRLOSS} rising or line ${LINE_PWRGOOD} falling"
    ;;
  *) die "Unknown profile '$PROFILE'" ;;
esac

# Track background processes for cleanup
MON_PIDS=()
SUPERCAP_PID=""
MON_COPROC_PID=""

cleanup() {
  for p in "${MON_PIDS[@]:-}" "$MON_COPROC_PID"; do
    [ -n "$p" ] && kill "$p" >/dev/null 2>&1 || true
  done
  if [ -n "${SUPERCAP_PID}" ] && kill -0 "$SUPERCAP_PID" >/dev/null 2>&1; then
    kill "$SUPERCAP_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ===== Supercap enable =====
enable_supercap() {
  if $DEBUG; then
    log "[DEBUG] gpioset ${CHIP_SUPERCAP} ${LINE_SUPERCAP}=1 (no-op)"
    return
  fi

  if $USE_V2; then
    # v2: daemonize mode keeps the line held
    gpioset -c "$CHIP_SUPERCAP" --daemonize "${LINE_SUPERCAP}=1"
    sleep 0.2  # Give daemon time to fork
    SUPERCAP_PID="$(pgrep -n -x gpioset || true)"
    log "Super-capacitor charging enabled (${CHIP_SUPERCAP} line ${LINE_SUPERCAP} HIGH) [v2 daemonized]"
  else
    # v1: run gpioset in the background to hold the line
    gpioset "$CHIP_SUPERCAP" "${LINE_SUPERCAP}=1" &
    SUPERCAP_PID=$!
    log "Super-capacitor charging enabled (${CHIP_SUPERCAP} line ${LINE_SUPERCAP} HIGH) [v1, pid=${SUPERCAP_PID}]"
  fi
}

run_hooks() {
  [ -d "$HOOK_DIR" ] || return 0
  local hook
  for hook in "$HOOK_DIR"/*; do
    [ -f "$hook" ] && [ -x "$hook" ] || continue
    if $DEBUG || $DRY_RUN; then
      log "[DRY RUN] Would run hook: $hook"
      continue
    fi
    log "Running hook: $hook"
    timeout "$HOOK_TIMEOUT" "$hook" || log "WARNING: hook $hook failed or timed out"
  done
}

handle_power_cut() {
  log "Power loss verified. Initiating safe shutdown..."
  run_hooks
  if $DEBUG || $DRY_RUN; then
    log "[DRY RUN] Would execute: sync; systemctl poweroff"
    return
  fi
  sync
  systemctl poweroff
  # Keep holding the supercap line until systemd stops us late in shutdown: the
  # EXIT trap would release it, and with it the power the shutdown runs on.
  trap - EXIT
  while true; do sleep 60; done
}

# Turn a gpiomon event line into "rising" or "falling" (v1 and v2 formats)
edge_of() {
  case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
    *rising*) echo rising ;;
    *falling*) echo falling ;;
    *) echo unknown ;;
  esac
}

line_active() {  # $1=chip $2=line -> exit 0 if the line reads active/1
  local out
  if $USE_V2; then
    out="$(gpioget -c "$1" "$2" 2>&1)"
    case "$out" in *=active*) return 0 ;; *) return 1 ;; esac
  else
    out="$(gpioget "$1" "$2" 2>&1)"
    [ "$out" = "1" ]
  fi
}

# ===== plus-max: one active-high loss line =====
monitor_plus_max() {
  if $DEBUG; then
    log "[DEBUG] simulate waiting 30s, then a glitch"; sleep 30; return
  fi

  # Never shut down because the line already reads "lost" at start (wrong pin,
  # odd boot); wait for it to report power OK first.
  while line_active "$CHIP_LOSS" "$LINE_LOSS"; do
    log "WARNING: ${CHIP_LOSS} line ${LINE_LOSS} reads power lost at startup. Waiting for it to clear."
    sleep 5
  done
  log "Power OK (${CHIP_LOSS} line ${LINE_LOSS} inactive). Monitoring."

  # One gpiomon reports every edge, so the line is never re-requested while held.
  # stdbuf: gpiomon writes to a pipe here and would otherwise buffer its events.
  if $USE_V2; then
    coproc MON { exec stdbuf -oL gpiomon -c "$CHIP_LOSS" --edges=both -p "${DEBOUNCE_MS}ms" "$LINE_LOSS"; }
  else
    coproc MON { exec stdbuf -oL gpiomon --rising-edge --falling-edge "$CHIP_LOSS" "$LINE_LOSS"; }
  fi
  MON_COPROC_PID="$MON_PID"
  local fd="${MON[0]}" event next
  while read -r -u "$fd" event; do
    if [ "$(edge_of "$event")" = falling ]; then
      log "Power OK again (${CHIP_LOSS} line ${LINE_LOSS} inactive)."
      continue
    fi
    [ "$(edge_of "$event")" = rising ] || continue
    log "Power loss signalled (${CHIP_LOSS} line ${LINE_LOSS} active)."
    # v2 edges are already debounced. On v1, lost power has to stay lost for
    # CONFIRM_SECONDS: a falling edge inside the window means it was a glitch.
    if ! $USE_V2 && read -r -t "$CONFIRM_SECONDS" -u "$fd" next && [ "$(edge_of "$next")" = falling ]; then
      log "Glitch ignored (line ${LINE_LOSS} returned to power OK within ${CONFIRM_SECONDS}s)."
      continue
    fi
    if $DEBUG || $DRY_RUN; then
      handle_power_cut
      log "[DRY RUN] Continuing to monitor."
      continue
    fi
    kill "$MON_COPROC_PID" >/dev/null 2>&1 || true
    wait "$MON_COPROC_PID" 2>/dev/null || true
    MON_COPROC_PID=""
    handle_power_cut
    return
  done
  die "gpiomon exited unexpectedly"
}

# ===== dual: PWRLOSS rising / PWRGOOD falling =====
read_states() {
  local out
  if $DEBUG; then
    if $USE_V2; then out="\"${LINE_PWRLOSS}\"=inactive \"${LINE_PWRGOOD}\"=active"
    else out="0 1"; fi
  else
    if $USE_V2; then out="$(gpioget -c "$CHIP" "$LINE_PWRLOSS" "$LINE_PWRGOOD" 2>&1)"
    else out="$(gpioget "$CHIP" "$LINE_PWRLOSS" "$LINE_PWRGOOD")"; fi
  fi

  local pl pg
  if $USE_V2; then
    # Handle both "LINE"=state and chip LINE=state formats
    if echo "$out" | grep -Eq "(\"${LINE_PWRLOSS}\"|${CHIP}[[:space:]]+${LINE_PWRLOSS})=active"; then pl=1
    elif echo "$out" | grep -Eq "(\"${LINE_PWRLOSS}\"|${CHIP}[[:space:]]+${LINE_PWRLOSS})=inactive"; then pl=0
    else
      log "WARNING: Could not parse PWRLOSS from: $out"
      pl=2
    fi

    if echo "$out" | grep -Eq "(\"${LINE_PWRGOOD}\"|${CHIP}[[:space:]]+${LINE_PWRGOOD})=active"; then pg=1
    elif echo "$out" | grep -Eq "(\"${LINE_PWRGOOD}\"|${CHIP}[[:space:]]+${LINE_PWRGOOD})=inactive"; then pg=0
    else
      log "WARNING: Could not parse PWRGOOD from: $out"
      pg=2
    fi
  else
    read -r pl pg <<<"$out"
  fi

  echo "$pl $pg"
}

start_monitors() {
  MON_PIDS=()
  if $DEBUG; then
    log "[DEBUG] would start: gpiomon PWRLOSS(rising), PWRGOOD(falling)"
    return
  fi

  if $USE_V2; then
    gpiomon -c "$CHIP" --edges=rising -n 1 "$LINE_PWRLOSS" &
    local pid1=$!
    MON_PIDS+=("$pid1")

    gpiomon -c "$CHIP" --edges=falling -n 1 "$LINE_PWRGOOD" &
    local pid2=$!
    MON_PIDS+=("$pid2")

    # A monitor that is already gone saw its edge right away (each exits after
    # one event); that is an event to verify, not a failure to start.
    sleep 0.1
    if ! kill -0 "$pid1" 2>/dev/null || ! kill -0 "$pid2" 2>/dev/null; then
      log "A monitor exited immediately (edge already seen). Verifying."
      return
    fi

    log "Monitors started: PWRLOSS(${LINE_PWRLOSS}↑ pid=$pid1) PWRGOOD(${LINE_PWRGOOD}↓ pid=$pid2)"
  else
    gpiomon --num-events=1 --rising-edge "$CHIP" "$LINE_PWRLOSS" & MON_PIDS+=("$!")
    gpiomon --num-events=1 --falling-edge "$CHIP" "$LINE_PWRGOOD" & MON_PIDS+=("$!")
    log "Monitors started: PWRLOSS(${LINE_PWRLOSS}↑) and PWRGOOD(${LINE_PWRGOOD}↓), PIDs: ${MON_PIDS[*]}"
  fi
}

wait_any_event() {
  if $DEBUG; then
    log "[DEBUG] simulate waiting 30s"; sleep 30; return 0
  fi

  # Wait for ANY monitor to finish
  if [ "${BASH_VERSINFO[0]}" -ge 5 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 3 ]; }; then
    wait -n "${MON_PIDS[@]}"
  else
    wait
  fi
}

stop_monitors() {
  # The monitor that did not fire still holds its line, and gpioget on a held line
  # fails with "Device or resource busy", so stop both before sampling the pins.
  for p in "${MON_PIDS[@]:-}"; do
    kill "$p" >/dev/null 2>&1 || true
  done
  for p in "${MON_PIDS[@]:-}"; do
    wait "$p" 2>/dev/null || true
  done
  MON_PIDS=()
}

verify_loss() {
  local ok=0 cnt=0
  log "Verifying power loss condition..."

  while [ "$cnt" -lt "$VERIFY_SAMPLES" ]; do
    read -r pl pg < <(read_states)
    # Loss if PWRLOSS==1 or PWRGOOD==0
    if [ "$pl" = "1" ] || [ "$pg" = "0" ]; then
      ok=$((ok+1))
    fi
    cnt=$((cnt+1))
    [ "$cnt" -lt "$VERIFY_SAMPLES" ] && ms_sleep "$VERIFY_INTERVAL_MS"
  done

  log "Verification: ${ok}/${VERIFY_SAMPLES} samples confirmed loss"
  # Require majority true
  [ "$ok" -ge $(( (VERIFY_SAMPLES+1)/2 )) ]
}

monitor_dual() {
  read -r pl pg < <(read_states)
  log "Initial states — PowerLoss(${LINE_PWRLOSS})=${pl}, PowerGood(${LINE_PWRGOOD})=${pg}"

  if [ "$pl" = "0" ] && [ "$pg" = "1" ]; then
    log "Pins in expected stable state. Starting monitors."
  else
    log "WARNING: Unexpected initial state (PWRLOSS=${pl}, PWRGOOD=${pg}). Continuing anyway."
  fi

  # Glitches loop here rather than re-exec'ing: exec skips the EXIT trap, so the
  # supercap holder and the monitor that did not fire would survive and block
  # the new instance from requesting the lines.
  while true; do
    start_monitors
    wait_any_event || true
    stop_monitors

    if verify_loss; then
      handle_power_cut
      return
    fi
    log "Glitch detected and ignored. Restarting monitors."
    sleep 0.5
  done
}

# ===== Main =====
enable_supercap

# Give supercap a moment to stabilize
sleep 0.5

if [ "$PROFILE" = "plus-max" ]; then
  monitor_plus_max
else
  monitor_dual
fi
