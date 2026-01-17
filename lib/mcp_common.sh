#!/usr/bin/env bash
set -euo pipefail

browser_use_mcp_unset_proxy_env() {
  # Avoid leaking global SOCKS/HTTP proxy settings into MCP server processes.
  unset ALL_PROXY all_proxy HTTPS_PROXY https_proxy HTTP_PROXY http_proxy
}

browser_use_mcp_lib_dir() {
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
}

browser_use_mcp_repo_root() {
  local lib_dir
  lib_dir="$(browser_use_mcp_lib_dir)"
  cd -- "${lib_dir}/.." && pwd
}

browser_use_mcp_bin_dir() {
  local root_dir
  root_dir="$(browser_use_mcp_repo_root)"
  printf '%s/bin' "${root_dir}"
}

browser_use_mcp_load_session_lib() {
  local root_dir lib
  root_dir="$(browser_use_mcp_repo_root)"
  lib="${root_dir}/lib/session_lib.sh"
  if [[ ! -f "${lib}" ]]; then
    echo "ERROR: Missing ${lib} (cannot resolve per-session browser config)" >&2
    exit 1
  fi
  # shellcheck source=/dev/null
  source "${lib}"
}

browser_use_mcp_resolve_python() {
  local candidate

  candidate="${BROWSER_USE_MCP_PYTHON:-}"
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

  echo "ERROR: No Python interpreter found (set BROWSER_USE_MCP_PYTHON=...)" >&2
  exit 1
}

browser_use_mcp_prepare_session() {
  browser_use_mcp_load_session_lib
  local python_bin
  python_bin="$(browser_use_mcp_resolve_python)"

  # Default to per-session Chrome (isolated profile + unique CDP port),
  # but allow callers (e.g. MCP client env) to override.
  export BROWSER_USE_CHROME_MODE="${BROWSER_USE_CHROME_MODE:-session}"
  browser_use_ensure_display_env

  # Best-effort: bind storage to Claude session folder if detectable.
  browser_use_apply_claude_session_storage >/dev/null 2>&1 || true

  local session_id session_id_safe session_dir
  session_id="$(browser_use_resolve_session_id)"
  export BROWSER_USE_SESSION_ID="${session_id}"
  session_id_safe="$(browser_use_sanitize_session_id "${session_id}")"
  session_dir="$(browser_use_session_dir "${session_id_safe}")"

  # Tie browser lifetime to the owning process (shared by all MCP servers in the session).
  export BROWSER_USE_OWNER_PID
  BROWSER_USE_OWNER_PID="$(browser_use_resolve_owner_pid 2>/dev/null || echo "${PPID}")"

  local bin_dir
  bin_dir="$(browser_use_mcp_bin_dir)"
  "${bin_dir}/ensure_cdp_chrome.sh"

  local mode cdp_url user_data_dir
  mode="${BROWSER_USE_CHROME_MODE,,}"
  cdp_url=""
  user_data_dir=""

  if [[ "${mode}" == "persistent" ]]; then
    local cdp_host cdp_port
    cdp_host="${CDP_HOST:-127.0.0.1}"
    cdp_port="${CDP_PORT:-9222}"
    cdp_url="http://${cdp_host}:${cdp_port}"
    user_data_dir="${BROWSER_USE_PERSISTENT_USER_DATA_DIR:-${HOME}/.config/browseruse/profiles/persistent-cdp}"
  else
    local state_file
    state_file="${session_dir}/chrome.json"
    if [[ ! -f "${state_file}" ]]; then
      echo "ERROR: Missing Chrome session state file: ${state_file}" >&2
      exit 1
    fi

    cdp_url="$("${python_bin}" - "${state_file}" <<'PY'
import json, sys
obj = json.loads(open(sys.argv[1], "r", encoding="utf-8").read())
print((obj.get("cdp_url") or "").strip())
PY
)"
    user_data_dir="$("${python_bin}" - "${state_file}" <<'PY'
import json, sys
obj = json.loads(open(sys.argv[1], "r", encoding="utf-8").read())
print((obj.get("user_data_dir") or "").strip())
PY
)"
  fi

  if [[ -z "${cdp_url}" ]]; then
    echo "ERROR: Could not determine CDP URL (mode=${mode})" >&2
    exit 1
  fi

  export BROWSER_USE_CDP_URL="${cdp_url}"
  export BROWSER_USE_USER_DATA_DIR="${user_data_dir}"
  export BROWSER_USE_MCP_SHARED_STATE_PATH="${session_dir}/shared_state.json"
}

browser_use_mcp_prepare_browser_use_config() {
  # browser_use.mcp reads its own config.json; by default it launches a separate browser
  # and won't share the CDP Chrome started by ensure_cdp_chrome.sh. To keep all MCP
  # servers on the same Chrome session, write a minimal browser-use config that pins
  # the BrowserProfile to our session's CDP URL + user-data-dir.
  #
  # Opt-out: set BROWSER_USE_MCP_DISABLE_BROWSER_USE_CONFIG=1
  if [[ -n "${BROWSER_USE_MCP_DISABLE_BROWSER_USE_CONFIG:-}" ]]; then
    return 0
  fi

  # Respect explicit config path if caller already set one.
  if [[ -n "${BROWSER_USE_CONFIG_PATH:-}" ]]; then
    return 0
  fi

  local shared_state_path session_dir config_path python_bin
  shared_state_path="${BROWSER_USE_MCP_SHARED_STATE_PATH:-}"
  if [[ -z "${shared_state_path}" ]]; then
    return 0
  fi
  session_dir="$(dirname "${shared_state_path}")"
  config_path="${session_dir}/browser_use.config.json"

  export BROWSER_USE_CONFIG_PATH="${config_path}"
  python_bin="$(browser_use_mcp_resolve_python)"

  "${python_bin}" - "${config_path}" <<'PY'
import json
import os
import sys
import uuid
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
cdp_url = (os.environ.get("BROWSER_USE_CDP_URL") or "").strip()
user_data_dir = (os.environ.get("BROWSER_USE_USER_DATA_DIR") or "").strip()

if not cdp_url:
    raise SystemExit(0)

config_path.parent.mkdir(parents=True, exist_ok=True)
profile_id = str(uuid.uuid4())
data = {
    "browser_profile": {
        profile_id: {
            "id": profile_id,
            "default": True,
            "cdp_url": cdp_url,
            "user_data_dir": user_data_dir or None,
            "keep_alive": True,
        }
    },
    # Keep empty by default so browser-use doesn't assume an LLM is configured.
    "llm": {},
    "agent": {},
}
config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}
