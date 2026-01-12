#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIB="${BROWSER_USE_MCP_SESSION_LIB:-${SCRIPT_DIR}/../lib/session_lib.sh}"
if [[ -f "${LIB}" ]]; then
  # shellcheck source=/dev/null
  source "${LIB}"
fi

resolve_python_bin() {
  local candidate="${BROWSER_USE_MCP_PYTHON:-}"
  if [[ -n "${candidate}" ]] && [[ -x "${candidate}" ]]; then
    printf '%s' "${candidate}"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  echo "ERROR: python not found (set BROWSER_USE_MCP_PYTHON=...)" >&2
  exit 1
}

PYTHON_BIN="$(resolve_python_bin)"

MODE="${BROWSER_USE_CHROME_MODE:-auto}" # auto|session|persistent
CDP_HOST="${CDP_HOST:-127.0.0.1}"
CHROME_BIN="${CHROME_BIN:-${BROWSER_USE_CHROME_BIN:-google-chrome}}"
WINDOW_WIDTH="${BROWSER_USE_WINDOW_WIDTH:-1600}"
WINDOW_HEIGHT="${BROWSER_USE_WINDOW_HEIGHT:-900}"
WINDOW_POS_X="${BROWSER_USE_WINDOW_POS_X:-0}"
WINDOW_POS_Y="${BROWSER_USE_WINDOW_POS_Y:-0}"

is_truthy() {
  local v="${1:-}"
  v="${v,,}"
  [[ -n "${v}" ]] && [[ "${v}" == "1" || "${v}" == "true" || "${v}" == "yes" || "${v}" == "y" || "${v}" == "on" ]]
}

should_keep_browser_open() {
  if is_truthy "${BROWSER_USE_KEEP_BROWSER_OPEN:-}"; then
    return 0
  fi

  if [[ -n "${REAPER_FILE:-}" ]]; then
    local marker
    marker="$(dirname "${REAPER_FILE}")/keep-browser-open.flag"
    [[ -f "${marker}" ]] && return 0
  fi

  return 1
}

browser_use_mcp_state_root() {
  if declare -F browser_use_state_root >/dev/null 2>&1; then
    browser_use_state_root
    return 0
  fi
  local xdg_state="${XDG_STATE_HOME:-${HOME}/.local/state}"
  printf '%s/browser-use-mcp-plus' "${xdg_state}"
}

legacy_setup() {
  CDP_PORT="${CDP_PORT:-9222}"
  CDP_URL="http://${CDP_HOST}:${CDP_PORT}"
  USER_DATA_DIR="${BROWSER_USE_PERSISTENT_USER_DATA_DIR:-${HOME}/.config/browseruse/profiles/persistent-cdp}"
  local state_root
  state_root="$(browser_use_mcp_state_root)"
  LOG_FILE="${BROWSER_USE_PERSISTENT_CHROME_LOG_FILE:-${state_root}/persistent-chrome.log}"
  PID_FILE="${BROWSER_USE_PERSISTENT_CHROME_PID_FILE:-${state_root}/persistent-chrome.pid}"
  LOCK_FILE="${BROWSER_USE_PERSISTENT_CHROME_LOCK_FILE:-${state_root}/persistent-chrome.lock}"

  mkdir -p "${state_root}"
  mkdir -p "${USER_DATA_DIR}"
}

session_setup() {
  if ! declare -F browser_use_resolve_session_id >/dev/null 2>&1; then
    echo "ERROR: session_lib.sh not available; cannot run in session mode" >&2
    exit 1
  fi

  local session_id_raw session_id_safe session_dir
  session_id_raw="$(browser_use_resolve_session_id)"
  session_id_safe="$(browser_use_sanitize_session_id "${session_id_raw}")"
  session_dir="$(browser_use_session_dir "${session_id_safe}")"

  mkdir -p "${session_dir}"

  STATE_FILE="${session_dir}/chrome.json"
  LOCK_FILE="${session_dir}/chrome.lock"
  PID_FILE="${session_dir}/chrome.pid"
  LOG_FILE="${session_dir}/chrome.log"
  REAPER_FILE="${session_dir}/chrome.reaper.pid"

  USER_DATA_DIR="$(browser_use_session_profile_dir "${session_id_safe}")"
  mkdir -p "${USER_DATA_DIR}"

  # If a previous port was chosen for this session, try to reuse it.
  if [[ -f "${STATE_FILE}" ]]; then
    CDP_PORT="$("${PYTHON_BIN}" - "${STATE_FILE}" <<'PY'
import json, sys
p = sys.argv[1]
try:
  obj = json.loads(open(p, "r", encoding="utf-8").read())
  port = int(obj.get("cdp_port") or 0)
  print(port if port > 0 else "")
except Exception:
  print("")
PY
)"
  fi

  if [[ -z "${CDP_PORT:-}" ]]; then
    CDP_PORT="$("${PYTHON_BIN}" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
  fi

  CDP_URL="http://${CDP_HOST}:${CDP_PORT}"
}

# Decide mode.
case "${MODE}" in
  persistent)
    legacy_setup
    ;;
  session)
    session_setup
    ;;
  auto)
    # Only enable per-session mode when the caller explicitly provides a session id.
    if [[ -n "${BROWSER_USE_SESSION_ID:-}" ]]; then
      session_setup
    else
      legacy_setup
    fi
    ;;
  *)
    echo "ERROR: Unknown BROWSER_USE_CHROME_MODE='${MODE}' (expected auto|session|persistent)" >&2
    exit 1
    ;;
esac

is_cdp_ready() {
  curl -fsS --max-time 0.3 "${CDP_URL}/json/version" >/dev/null 2>&1
}

is_cdp_ready_url() {
  local url="$1"
  curl -fsS --max-time 0.3 "${url}/json/version" >/dev/null 2>&1
}

is_pid_alive() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

find_existing_chrome_for_profile() {
  # Output: "<pid>\t<remote_debugging_port>" or empty.
  local user_data_dir="$1"
  "${PYTHON_BIN}" - "${user_data_dir}" <<'PY'
import os
import sys

user_data_dir = (sys.argv[1] or "").strip()
if not user_data_dir:
    sys.exit(0)

user_data_dir = os.path.abspath(os.path.expanduser(user_data_dir))

matches = []

for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    cmdline_path = f"/proc/{pid}/cmdline"
    try:
        raw = open(cmdline_path, "rb").read()
    except Exception:
        continue
    if not raw:
        continue
    parts = [p for p in raw.split(b"\0") if p]
    if not parts:
        continue

    try:
        args = [p.decode("utf-8", errors="ignore") for p in parts]
    except Exception:
        continue

    # Quick filter: only chrome-ish processes
    exe = os.path.basename(args[0])
    if "chrome" not in exe and "chromium" not in exe:
        continue

    has_profile = False
    port = ""
    typ = ""
    for a in args[1:]:
        if a.startswith("--user-data-dir="):
            val = a.split("=", 1)[1]
            try:
                val = os.path.abspath(os.path.expanduser(val))
            except Exception:
                pass
            if val == user_data_dir:
                has_profile = True
        elif a.startswith("--remote-debugging-port="):
            port = a.split("=", 1)[1]
        elif a.startswith("--type="):
            typ = a.split("=", 1)[1]

    if has_profile:
        matches.append((int(pid), typ, port))

# Prefer the main browser process (no --type), then lowest PID.
matches.sort(key=lambda t: (0 if t[1] == "" else 1, t[0]))

if matches:
    pid, typ, port = matches[0]
    print(f"{pid}\t{port}")
PY
}

read_pid_file() {
  if [[ -f "${PID_FILE}" ]]; then
    tr -dc '0-9' <"${PID_FILE}" | head -c 12 || true
  fi
}

write_state_file() {
  local chrome_pid="$1"
  if [[ -z "${STATE_FILE:-}" ]]; then
    return 0
  fi
  "${PYTHON_BIN}" - "${STATE_FILE}" <<PY
import json, os, sys, time

state_file = sys.argv[1]
obj = {
  "updated_at_unix": time.time(),
  "mode": os.getenv("BROWSER_USE_CHROME_MODE", "auto"),
  "session_id": os.getenv("BROWSER_USE_SESSION_ID") or None,
  "claude_session_id": os.getenv("BROWSER_USE_CLAUDE_SESSION_ID") or os.getenv("CLAUDE_SESSION_ID") or None,
  "cdp_host": "${CDP_HOST}",
  "cdp_port": int("${CDP_PORT}"),
  "cdp_url": "${CDP_URL}",
  "chrome_pid": int("${chrome_pid}"),
  "user_data_dir": "${USER_DATA_DIR}",
  "display": os.getenv("DISPLAY"),
}
os.makedirs(os.path.dirname(state_file), exist_ok=True)
with open(state_file, "w", encoding="utf-8") as f:
  json.dump(obj, f, ensure_ascii=False, indent=2)
  f.write("\\n")
PY
}

start_chrome() {
  if [[ -z "${DISPLAY:-}" ]]; then
    if [[ "${BROWSER_USE_ALLOW_HEADLESS_FALLBACK:-}" == "1" || "${BROWSER_USE_ALLOW_HEADLESS_FALLBACK:-}" == "true" ]]; then
      : # allow headless fallback below
    else
      echo "ERROR: DISPLAY is not set; refusing to start headless Chrome. Set DISPLAY/XAUTHORITY or BROWSER_USE_ALLOW_HEADLESS_FALLBACK=true." >&2
      exit 1
    fi
  fi

  local chrome_args=(
    "--remote-debugging-address=${CDP_HOST}"
    "--remote-debugging-port=${CDP_PORT}"
    "--user-data-dir=${USER_DATA_DIR}"
    "--no-first-run"
    "--no-default-browser-check"
    "--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}"
    "--window-position=${WINDOW_POS_X},${WINDOW_POS_Y}"
    "--new-window"
    "about:blank"
  )

  if [[ -z "${DISPLAY:-}" ]]; then
    chrome_args=(
      "--headless=new"
      "--disable-gpu"
      "${chrome_args[@]}"
    )
  fi

  (
    # Prevent inheriting the lock fd (held by this script) into Chrome.
    # Otherwise Chrome would keep the lock forever and future calls would block.
    exec 9>&- || true
    nohup "${CHROME_BIN}" "${chrome_args[@]}" >"${LOG_FILE}" 2>&1 &
    local chrome_pid="$!"
    echo "${chrome_pid}" >"${PID_FILE}"
    write_state_file "${chrome_pid}"

    # Best-effort cleanup when the owning Claude Code process ends.
    # We watch the owner PID (typically Claude Code's PID) rather than this script,
    # because multiple MCP servers should be able to share the same browser.
    if ! should_keep_browser_open && [[ -n "${BROWSER_USE_OWNER_PID:-}" ]] && [[ -n "${REAPER_FILE:-}" ]]; then
      local owner_pid="${BROWSER_USE_OWNER_PID}"
      # Avoid spawning duplicate reapers.
      if [[ -f "${REAPER_FILE}" ]]; then
        local existing
        existing="$(tr -dc '0-9' <"${REAPER_FILE}" | head -c 12 || true)"
        if is_pid_alive "${existing}"; then
          exit 0
        fi
      fi
      (
        while is_pid_alive "${owner_pid}"; do
          sleep 1
        done
        # Owner ended: terminate Chrome for this session.
        if is_pid_alive "${chrome_pid}"; then
          kill "${chrome_pid}" >/dev/null 2>&1 || true
          sleep 0.5
        fi
        if is_pid_alive "${chrome_pid}"; then
          kill -9 "${chrome_pid}" >/dev/null 2>&1 || true
        fi
      ) >/dev/null 2>&1 &
      echo "$!" >"${REAPER_FILE}"
    fi
  )
}

  (
    flock 9

    if is_cdp_ready; then
    # Ensure a session state file exists even if the browser was already running.
    if [[ -n "${STATE_FILE:-}" ]]; then
      existing_pid="$(read_pid_file || true)"
      if [[ -z "${existing_pid}" ]]; then
        existing_pid="0"
      fi
      write_state_file "${existing_pid}"
    fi
      exit 0
    fi

    # If Chrome is already running for this profile but our remembered CDP URL isn't ready,
    # try to discover the actual remote debugging port from the running process.
    if [[ -n "${USER_DATA_DIR:-}" ]]; then
      existing_info="$(find_existing_chrome_for_profile "${USER_DATA_DIR}" 2>/dev/null || true)"
      if [[ -n "${existing_info}" ]]; then
        IFS=$'\t' read -r existing_pid existing_port <<<"${existing_info}"
        if [[ "${existing_port}" =~ ^[0-9]+$ ]]; then
          existing_url="http://${CDP_HOST}:${existing_port}"
          if is_cdp_ready_url "${existing_url}"; then
            CDP_PORT="${existing_port}"
            CDP_URL="${existing_url}"
            echo "${existing_pid}" >"${PID_FILE}"
            write_state_file "${existing_pid}"
            exit 0
          fi
        fi
      fi
    fi

    # If we have a PID but CDP isn't ready, that process is likely dead/stuck.
    old_pid="$(read_pid_file || true)"
    if [[ -n "${old_pid}" ]] && is_pid_alive "${old_pid}" && [[ -n "${USER_DATA_DIR:-}" ]]; then
      # Chrome is alive but CDP is not ready. This can happen if we lost the port or Chrome got wedged.
      # If it's our profile, restart it cleanly so CDP becomes available again.
      if ps -p "${old_pid}" -o args= 2>/dev/null | grep -F -q -- "--user-data-dir=${USER_DATA_DIR}"; then
        kill "${old_pid}" >/dev/null 2>&1 || true
        sleep 0.5
        if is_pid_alive "${old_pid}"; then
          kill -9 "${old_pid}" >/dev/null 2>&1 || true
        fi
      fi
    elif [[ -n "${old_pid}" ]] && ! is_pid_alive "${old_pid}"; then
      rm -f "${PID_FILE}" >/dev/null 2>&1 || true
    fi

    start_chrome

    for _ in {1..200}; do
      if is_cdp_ready; then
        exit 0
      fi
      sleep 0.15
    done

    # One last attempt: Chrome may have re-used an existing browser instance for the same profile.
    if [[ -n "${USER_DATA_DIR:-}" ]]; then
      existing_info="$(find_existing_chrome_for_profile "${USER_DATA_DIR}" 2>/dev/null || true)"
      if [[ -n "${existing_info}" ]]; then
        IFS=$'\t' read -r existing_pid existing_port <<<"${existing_info}"
        if [[ "${existing_port}" =~ ^[0-9]+$ ]]; then
          existing_url="http://${CDP_HOST}:${existing_port}"
          if is_cdp_ready_url "${existing_url}"; then
            CDP_PORT="${existing_port}"
            CDP_URL="${existing_url}"
            echo "${existing_pid}" >"${PID_FILE}"
            write_state_file "${existing_pid}"
            exit 0
          fi
        fi
      fi
    fi

    # Common failure mode on some systems: inotify instance exhaustion.
    # Chromium logs this as inotify_init() failed: Too many open files (EMFILE).
    if [[ -n "${LOG_FILE:-}" ]] && [[ -f "${LOG_FILE}" ]]; then
      if grep -F -q "file_path_watcher_inotify.cc:339" "${LOG_FILE}" && grep -F -q "inotify_init() failed: Too many open files" "${LOG_FILE}"; then
        echo "HINT: Chrome failed to create inotify watcher (fs.inotify.max_user_instances). Increase it, e.g.: sudo sysctl -w fs.inotify.max_user_instances=512" >&2
      fi
      # Another common failure mode: X server refuses new clients (too many GUI clients / stale sessions).
      if grep -F -q "Maximum number of clients reached" "${LOG_FILE}" || grep -F -q "could not connect to display" "${LOG_FILE}"; then
        echo "HINT: Chrome could not connect to DISPLAY='${DISPLAY:-}'. Your X server may have reached its client limit (or DISPLAY/XAUTHORITY is wrong). Close old Chrome sessions (especially ones kept open via set_browser_keep_open), or restart the display session, then retry." >&2
      fi
    fi

    echo "ERROR: Chrome CDP endpoint not ready at ${CDP_URL}" >&2
    exit 1
  ) 9>"${LOCK_FILE}"
