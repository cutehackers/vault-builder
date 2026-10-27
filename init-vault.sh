#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=""
WITH_STENC=0
VAULT_NAME_ARG=""

usage() {
  cat <<'EOF'
Usage: ./init-vault.sh [--with-stenc] [--name NAME] [target-dir]

Create a ready-to-use LLM Wiki vault and register it for Claude Code
(project-memory skill, vault map, and the user MCP entry llm-wiki-NAME).

Options:
  --with-stenc  Include optional Stenc fixed-format spec/plan docs and tools.
  --name NAME   Vault name. Default: the target folder name.
  -h, --help    Show this help.

Examples:
  ./init-vault.sh
  ./init-vault.sh --with-stenc
  ./init-vault.sh ~/wiki/my-vault
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-stenc)
      WITH_STENC=1
      shift
      ;;
    --name)
      if [[ $# -lt 2 ]]; then
        echo "--name requires a value." >&2
        usage >&2
        exit 1
      fi
      VAULT_NAME_ARG="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "${TARGET_DIR}" ]]; then
        echo "Only one target directory may be provided." >&2
        usage >&2
        exit 1
      fi
      TARGET_DIR="$1"
      shift
      ;;
  esac
done

if [[ $# -gt 0 ]]; then
  if [[ -n "${TARGET_DIR}" || $# -gt 1 ]]; then
    echo "Only one target directory may be provided." >&2
    usage >&2
    exit 1
  fi
  TARGET_DIR="$1"
fi

TARGET_DIR="${TARGET_DIR:-vault}"

if [[ "${TARGET_DIR}" != /* ]]; then
  TARGET_DIR="$(pwd)/${TARGET_DIR}"
fi

if [[ -e "${TARGET_DIR}/wiki/index.md" || -e "${TARGET_DIR}/tools/wiki/cli.py" ]]; then
  echo "Refusing to overwrite an existing initialized vault: ${TARGET_DIR}" >&2
  echo "Choose an empty target directory." >&2
  exit 1
fi

print_python_install_help() {
  local found
  echo "Python 3.10 or newer is required, and no such interpreter was found on PATH." >&2
  for found in "${PYTHON:-}" python3 python; do
    if [[ -n "${found}" ]] && command -v "${found}" >/dev/null 2>&1; then
      echo "  Found: $("${found}" --version 2>&1) at $(command -v "${found}")" >&2
    fi
  done
  echo "Install Python 3.10 or newer, then run this command again:" >&2
  case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin)
      echo "  brew install python@3.12" >&2
      ;;
    Linux)
      echo "  sudo apt-get install python3.12   (Ubuntu 24.04 or newer; older Ubuntu needs ppa:deadsnakes/ppa)" >&2
      echo "  sudo dnf install python3.12       (Fedora, RHEL)" >&2
      ;;
  esac
  echo "  uv python install 3.12            (any OS, https://docs.astral.sh/uv/)" >&2
  echo "If Python 3.10 or newer is installed under another name, set PYTHON, for example PYTHON=python3.12." >&2
}

choose_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if [[ -n "${candidate}" ]] && command -v "${candidate}" >/dev/null 2>&1; then
      if "${candidate}" - <<'PY' >/dev/null 2>&1; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
        echo "${candidate}"
        return 0
      fi
    fi
  done
  print_python_install_help
  exit 1
}

PYTHON_BIN="$(choose_python)"

# install assembles the vault in a temporary sibling folder, renames it into place, installs the
# user-level project-memory files and MCP entry, and prints next steps that start with ${PYTHON_BIN}'s path.
install_args=(install "${TARGET_DIR}")
if [[ -n "${VAULT_NAME_ARG}" ]]; then
  install_args+=(--name "${VAULT_NAME_ARG}")
fi
if [[ "${WITH_STENC}" -eq 1 ]]; then
  install_args+=(--with-stenc)
fi
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/tools/vault_migration/cli.py" "${install_args[@]}"
