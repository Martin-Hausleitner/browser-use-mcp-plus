#!/usr/bin/env bash
set -euo pipefail

browser_use_resolve_session_id() {
  local session_id="${BROWSER_USE_SESSION_ID:-}"
  if [[ -n "${session_id}" ]]; then
    printf '%s' "${session_id}"
    return 0
  fi

  # If we're running under Claude Code, prefer binding to the Claude session UUID so
  # browser state can be stored alongside the programming session.
  browser_use_apply_claude_session_storage >/dev/null 2>&1 || true
  if [[ -n "${BROWSER_USE_CLAUDE_SESSION_ID:-}" ]]; then
    printf '%s' "main"
    return 0
  fi

  # Fallback: tie the "browser session" to the owning Claude process (stable across MCP servers).
  local owner_pid
  owner_pid="$(browser_use_resolve_owner_pid 2>/dev/null || true)"
  if [[ -z "${owner_pid}" ]]; then
    printf 'ppid-%s' "${PPID}"
    return 0
  fi
  local start_ticks
  start_ticks="$(browser_use_proc_start_ticks "${owner_pid}" 2>/dev/null || true)"
  if [[ -n "${start_ticks}" ]]; then
    printf 'owner-%s-%s' "${owner_pid}" "${start_ticks}"
    return 0
  fi
  printf 'owner-%s' "${owner_pid}"
}

browser_use_sanitize_session_id() {
  local raw="$1"
  # Keep it filesystem-safe and reasonably short.
  local safe
  safe="$(printf '%s' "${raw}" | tr -cs 'A-Za-z0-9._@+-' '_' | sed 's/^_\\+//; s/_\\+$//')"
  if [[ -z "${safe}" ]]; then
    safe="session"
  fi
  # Avoid path-length issues.
  printf '%.80s' "${safe}"
}

browser_use_state_root() {
  local explicit="${BROWSER_USE_MCP_STATE_DIR:-}"
  if [[ -n "${explicit}" ]]; then
    printf '%s' "${explicit}"
    return 0
  fi

  local xdg_state="${XDG_STATE_HOME:-${HOME}/.local/state}"
  printf '%s/browser-use-mcp-plus' "${xdg_state}"
}

browser_use_session_dir() {
  local session_id_safe="$1"
  local base="${BROWSER_USE_MCP_SESSIONS_DIR:-}"
  if [[ -z "${base}" ]]; then
    base="$(browser_use_state_root)/sessions"
  fi
  printf '%s/%s' "${base}" "${session_id_safe}"
}

browser_use_session_profile_dir() {
  local session_id_safe="$1"
  local base="${BROWSER_USE_CDP_PROFILE_BASE_DIR:-${HOME}/.config/browseruse/profiles/session-cdp}"
  printf '%s/%s' "${base}" "${session_id_safe}"
}

browser_use_proc_start_ticks() {
  local pid="$1"
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "${pid}" <<'PY'
import os
import sys

pid = sys.argv[1].strip()
if not pid.isdigit():
    sys.exit(1)

try:
    stat = open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace").read()
except Exception:
    sys.exit(1)

end = stat.rfind(")")
if end == -1:
    sys.exit(1)

rest = stat[end + 2 :].split()
if len(rest) < 20:
    sys.exit(1)

# /proc/<pid>/stat field 22 (starttime) is index 19 when rest starts at field 3.
print(rest[19])
PY
}

browser_use_pid_chain() {
  # Print ancestor PIDs from root -> leaf for the provided PID.
  local start_pid="${1:-${PPID}}"
  local max_depth="${2:-30}"
  command -v ps >/dev/null 2>&1 || return 1

  local pid="${start_pid}"
  local -a chain=()
  local i=0

  while [[ "${pid}" =~ ^[0-9]+$ ]] && [[ "${pid}" != "0" ]] && [[ "${pid}" != "1" ]] && [[ "${i}" -lt "${max_depth}" ]]; do
    chain+=("${pid}")
    local ppid
    ppid="$(ps -p "${pid}" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -z "${ppid}" ]] || [[ "${ppid}" == "${pid}" ]]; then
      break
    fi
    pid="${ppid}"
    ((i++))
  done

  local j
  for ((j=${#chain[@]}-1; j>=0; j--)); do
    printf '%s' "${chain[j]}"
    if (( j > 0 )); then
      printf ' '
    fi
  done
}

browser_use_find_ancestor_pid_by_comm() {
  local want_comm="$1"
  local start_pid="${2:-${PPID}}"
  local max_depth="${3:-30}"
  command -v ps >/dev/null 2>&1 || return 1

  local pid="${start_pid}"
  local i=0
  while [[ "${pid}" =~ ^[0-9]+$ ]] && [[ "${pid}" != "0" ]] && [[ "${pid}" != "1" ]] && [[ "${i}" -lt "${max_depth}" ]]; do
    local comm
    comm="$(ps -p "${pid}" -o comm= 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "${comm}" == "${want_comm}" ]]; then
      printf '%s' "${pid}"
      return 0
    fi
    local ppid
    ppid="$(ps -p "${pid}" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -z "${ppid}" ]] || [[ "${ppid}" == "${pid}" ]]; then
      break
    fi
    pid="${ppid}"
    ((i++))
  done
  return 1
}

browser_use_find_ancestor_pid_by_args_contains() {
  local needle="$1"
  local start_pid="${2:-${PPID}}"
  local max_depth="${3:-30}"
  command -v ps >/dev/null 2>&1 || return 1

  local pid="${start_pid}"
  local i=0
  while [[ "${pid}" =~ ^[0-9]+$ ]] && [[ "${pid}" != "0" ]] && [[ "${pid}" != "1" ]] && [[ "${i}" -lt "${max_depth}" ]]; do
    local args
    args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    if [[ -n "${args}" ]] && [[ "${args}" == *"${needle}"* ]]; then
      printf '%s' "${pid}"
      return 0
    fi
    local ppid
    ppid="$(ps -p "${pid}" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -z "${ppid}" ]] || [[ "${ppid}" == "${pid}" ]]; then
      break
    fi
    pid="${ppid}"
    ((i++))
  done
  return 1
}

browser_use_resolve_owner_pid() {
  # The owner PID is used for:
  # - stable session IDs across MCP servers in the same Claude Code session
  # - best-effort browser cleanup when the Claude session ends
  local current="${BROWSER_USE_OWNER_PID:-}"
  if [[ -n "${current}" ]] && kill -0 "${current}" >/dev/null 2>&1; then
    printf '%s' "${current}"
    return 0
  fi

  # Prefer the AGY/CCS orchestrator (it can outlive transient `claude` processes used for MCP calls).
  local ccs_pid
  ccs_pid="$(browser_use_find_ancestor_pid_by_args_contains "ccs agy" "${PPID}" 80 2>/dev/null || true)"
  if [[ -n "${ccs_pid}" ]]; then
    printf '%s' "${ccs_pid}"
    return 0
  fi

  local claude_pid
  claude_pid="$(browser_use_find_ancestor_pid_by_comm "claude" "${PPID}" 80 2>/dev/null || true)"
  if [[ -n "${claude_pid}" ]]; then
    printf '%s' "${claude_pid}"
    return 0
  fi

  # Fallback: immediate parent.
  printf '%s' "${PPID}"
}

browser_use_try_resolve_claude_session_id_from_debug() {
  # Args: one or more PIDs (as separate args). Output: "<uuid>\t<matched_pid>" or empty.
  python3 - "$@" <<'PY'
import glob
import os
import sys
import time
import re

pids = []
for arg in sys.argv[1:]:
    arg = (arg or "").strip()
    if arg.isdigit():
        pids.append(arg)

if not pids:
    sys.exit(0)

debug_dir = os.path.expanduser("~/.claude/debug")
if not os.path.isdir(debug_dir):
    sys.exit(0)

scan_limit = int(os.getenv("BROWSER_USE_CLAUDE_DEBUG_SCAN_LIMIT", "200") or "200")
max_age_s = int(os.getenv("BROWSER_USE_CLAUDE_DEBUG_MAX_AGE_SECONDS", str(7 * 24 * 3600)) or str(7 * 24 * 3600))

pid_alt = b"|".join(re.escape(pid.encode("utf-8")) for pid in pids)
regex = re.compile(rb"\.tmp\.(?:" + pid_alt + rb")\.")

paths = glob.glob(os.path.join(debug_dir, "*.txt"))
paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)

now = time.time()

for path in paths[: max(0, scan_limit)]:
    try:
        mtime = os.path.getmtime(path)
        if now - mtime > max_age_s:
            # Remaining files are older as the list is sorted by mtime.
            break
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                data = tail + chunk
                m = regex.search(data)
                if m:
                    matched_pid = m.group(0).split(b".")[2].decode("utf-8", errors="ignore")
                    claude_id = os.path.splitext(os.path.basename(path))[0]
                    print(f"{claude_id}\t{matched_pid}")
                    sys.exit(0)
                tail = data[-64:]
    except Exception:
        continue

sys.exit(0)
PY
}

browser_use_try_resolve_claude_session_id_from_pid_fds() {
  local pid="$1"
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "${pid}" <<'PY'
import os
import re
import sys

pid = (sys.argv[1] or "").strip()
if not pid.isdigit():
    sys.exit(0)

home = os.path.expanduser("~")
debug_dir = os.path.join(home, ".claude", "debug")
fd_dir = f"/proc/{pid}/fd"

uuid_re = re.compile(re.escape(debug_dir) + r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\\.txt(?: \\(deleted\\))?$")

try:
    fds = os.listdir(fd_dir)
except Exception:
    sys.exit(0)

for fd in fds:
    try:
        target = os.readlink(os.path.join(fd_dir, fd))
    except Exception:
        continue
    target = (target or "").split(" (deleted)")[0]
    m = uuid_re.search(target)
    if m:
        print(m.group(1))
        sys.exit(0)

sys.exit(0)
PY
}

browser_use_claude_project_key() {
  # Claude Code stores sessions under ~/.claude/projects/<project-key>/...
  # project-key appears to be the absolute cwd with "/" replaced by "-" and a leading "-".
  local cwd="${1:-${PWD:-}}"
  if [[ -z "${cwd}" ]]; then
    return 1
  fi
  local trimmed="${cwd#/}"
  trimmed="${trimmed%/}"
  if [[ -z "${trimmed}" ]]; then
    printf '%s' "-"
    return 0
  fi
  printf '%s' "-${trimmed//\//-}"
}

browser_use_find_claude_session_root() {
  local claude_id="$1"
  if [[ -z "${claude_id}" ]]; then
    return 1
  fi

  local base="${HOME}/.claude/projects"
  if [[ ! -d "${base}" ]]; then
    return 1
  fi

  # Prefer current working directory's project key if it matches.
  local project_key
  project_key="$(browser_use_claude_project_key "${PWD:-}")" || project_key=""
  if [[ -n "${project_key}" ]] && [[ -d "${base}/${project_key}/${claude_id}" ]]; then
    printf '%s' "${base}/${project_key}/${claude_id}"
    return 0
  fi

  # Otherwise, search across all projects for the session UUID.
  local match
  shopt -s nullglob
  for match in "${base}"/*/"${claude_id}"; do
    if [[ -d "${match}" ]]; then
      printf '%s' "${match}"
      shopt -u nullglob
      return 0
    fi
  done
  shopt -u nullglob
  return 1
}

browser_use_apply_claude_session_storage() {
  # Best-effort: when invoked by Claude Code, bind browser storage to the Claude session UUID.
  if [[ -n "${BROWSER_USE_CLAUDE_SESSION_ID:-}" ]]; then
    return 0
  fi

  if [[ -n "${CLAUDE_SESSION_ID:-}" ]]; then
    export BROWSER_USE_CLAUDE_SESSION_ID="${CLAUDE_SESSION_ID}"
  else
    local claude_pid
    claude_pid="$(browser_use_find_ancestor_pid_by_comm "claude" "${PPID}" 80 2>/dev/null || true)"
    if [[ -n "${claude_pid}" ]]; then
      local claude_id_from_fds
      claude_id_from_fds="$(browser_use_try_resolve_claude_session_id_from_pid_fds "${claude_pid}" 2>/dev/null || true)"
      if [[ -n "${claude_id_from_fds}" ]]; then
        export BROWSER_USE_CLAUDE_SESSION_ID="${claude_id_from_fds}"
      fi
    fi

    if [[ -z "${BROWSER_USE_CLAUDE_SESSION_ID:-}" ]]; then
      local chain
      chain="$(browser_use_pid_chain "${PPID}" 60 2>/dev/null || true)"
      if [[ -n "${chain}" ]]; then
        local info claude_id matched_pid
        # shellcheck disable=SC2086
        info="$(browser_use_try_resolve_claude_session_id_from_debug ${chain} 2>/dev/null || true)"
        if [[ -n "${info}" ]]; then
          IFS=$'\t' read -r claude_id matched_pid <<<"${info}"
          if [[ -n "${claude_id}" ]]; then
            export BROWSER_USE_CLAUDE_SESSION_ID="${claude_id}"
            if [[ -z "${BROWSER_USE_OWNER_PID:-}" ]] && [[ -n "${matched_pid}" ]]; then
              export BROWSER_USE_OWNER_PID="${matched_pid}"
            fi
          fi
        fi
      fi
    fi
  fi

  if [[ -z "${BROWSER_USE_CLAUDE_SESSION_ID:-}" ]]; then
    return 0
  fi

  local session_root
  session_root="$(browser_use_find_claude_session_root "${BROWSER_USE_CLAUDE_SESSION_ID}")" || return 0
  if [[ ! -d "${session_root}" ]]; then
    return 0
  fi

  local browser_root="${session_root}/browser-use"
  export BROWSER_USE_MCP_SESSIONS_DIR="${browser_root}/sessions"
  export BROWSER_USE_CDP_PROFILE_BASE_DIR="${browser_root}/profiles"
  mkdir -p "${BROWSER_USE_MCP_SESSIONS_DIR}" "${BROWSER_USE_CDP_PROFILE_BASE_DIR}"
}

browser_use_ensure_display_env() {
  # We want a real, visible browser. If DISPLAY isn't set (common for some MCP servers),
  # try to infer a usable X11 display from /tmp/.X11-unix.
  if [[ -n "${DISPLAY:-}" ]]; then
    return 0
  fi

  if [[ -S /tmp/.X11-unix/X10 ]]; then
    export DISPLAY=":10"
  elif [[ -S /tmp/.X11-unix/X0 ]]; then
    export DISPLAY=":0"
  fi

  if [[ -n "${DISPLAY:-}" && -z "${XAUTHORITY:-}" && -f "${HOME}/.Xauthority" ]]; then
    export XAUTHORITY="${HOME}/.Xauthority"
  fi
}
