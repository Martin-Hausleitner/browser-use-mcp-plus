#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../lib/mcp_common.sh"

browser_use_mcp_unset_proxy_env
browser_use_mcp_prepare_session

export UI_CDP_URL="${BROWSER_USE_CDP_URL}"

PYTHON_BIN="$(browser_use_mcp_resolve_python)"
ROOT_DIR="$(browser_use_mcp_repo_root)"
exec "${PYTHON_BIN}" "${ROOT_DIR}/servers/ui_describe_mcp_server.py"
