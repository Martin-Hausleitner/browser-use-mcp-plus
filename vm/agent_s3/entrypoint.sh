#!/usr/bin/env bash
set -euo pipefail

MODE="${VM_MODE:-selftest}"
REPO_URL="${VM_REPO_URL:-}"
REPO_DIR="${VM_REPO_DIR:-/workspace/repo}"
TASK="${VM_TASK:-}"
STEPS="${VM_STEPS:-15}"
WORKDIR="${VM_WORKDIR:-${REPO_DIR}}"
XVFB_ARGS="${VM_XVFB_ARGS:--screen 0 1600x900x24}"
DRY_RUN="${VM_DRY_RUN:-}"
UNSAFE_EXEC="${VM_UNSAFE_EXEC:-}"

mkdir -p /workspace

if [[ -d "${REPO_DIR}" ]] && [[ -n "$(ls -A "${REPO_DIR}" 2>/dev/null || true)" ]]; then
  echo "Repo present: ${REPO_DIR}" >&2
else
  if [[ -n "${REPO_URL}" ]]; then
    echo "Cloning repo: ${REPO_URL} -> ${REPO_DIR}" >&2
    rm -rf -- "${REPO_DIR}" || true
    git clone --depth 1 "${REPO_URL}" "${REPO_DIR}"
  else
    mkdir -p -- "${REPO_DIR}"
  fi
fi

case "${MODE}" in
  selftest)
    dbus-run-session -- xvfb-run -a -s "${XVFB_ARGS}" python3 /app/selftest.py | sed -n '/^{/,$p'
    ;;
  task)
    if [[ -z "${TASK}" ]]; then
      echo "ERROR: VM_TASK is required for VM_MODE=task" >&2
      exit 2
    fi
    extra_args=()
    if [[ "${DRY_RUN}" == "1" || "${DRY_RUN,,}" == "true" || "${DRY_RUN,,}" == "yes" ]]; then
      extra_args+=("--dry-run")
    fi
    if [[ "${UNSAFE_EXEC}" == "1" || "${UNSAFE_EXEC,,}" == "true" || "${UNSAFE_EXEC,,}" == "yes" ]]; then
      extra_args+=("--unsafe-exec")
    fi

    dbus-run-session -- xvfb-run -a -s "${XVFB_ARGS}" python3 /app/run_task.py \
      --query "${TASK}" \
      --steps "${STEPS}" \
      --workdir "${WORKDIR}" \
      "${extra_args[@]}" | sed -n '/^{/,$p'
    ;;
  *)
    echo "ERROR: Unknown VM_MODE=${MODE}" >&2
    exit 2
    ;;
esac
