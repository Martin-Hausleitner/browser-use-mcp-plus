#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  bin/mcp_plus.sh example
  bin/mcp_plus.sh test
  bin/mcp_plus.sh test-live
  bin/mcp_plus.sh test-live-suite

Env:
  BROWSER_USE_MCP_PYTHON   Python used by MCP server wrappers
  PYTHON                  Python used to run this script (default: python3)
EOF
}

cmd="${1:-}"
shift || true

PYTHON_BIN="${PYTHON:-python3}"

case "${cmd}" in
  example)
    cd -- "${ROOT_DIR}"
    exec "${PYTHON_BIN}" -m scripts.example "$@"
    ;;
  test)
    cd -- "${ROOT_DIR}"
    exec "${PYTHON_BIN}" -m tests "$@"
    ;;
  test-live)
    cd -- "${ROOT_DIR}"
    exec "${PYTHON_BIN}" -m scripts.live_llm_e2e "$@"
    ;;
  test-live-suite)
    cd -- "${ROOT_DIR}"
    exec "${PYTHON_BIN}" -m scripts.live_llm_suite "$@"
    ;;
  -h|--help|help|"")
    usage
    exit 0
    ;;
  *)
    echo "ERROR: Unknown command: ${cmd}" >&2
    usage >&2
    exit 2
    ;;
esac
