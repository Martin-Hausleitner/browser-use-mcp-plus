#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/../lib/mcp_common.sh"

browser_use_mcp_unset_proxy_env
browser_use_mcp_prepare_session

PYTHON_BIN="$(browser_use_mcp_resolve_python)"
exec "${PYTHON_BIN}" -m browser_use.mcp
