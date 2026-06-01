#!/usr/bin/env bash
set -euo pipefail

REPO="${VAULT_BUILDER_REPO:-cutehackers/vault-builder}"
REF="${VAULT_BUILDER_REF:-main}"
TARGET_DIR="${1:-${VAULT_NAME:-llm-wiki}}"
VERIFY_MODE="${VAULT_BOOTSTRAP_VERIFY:-quick}"

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
  curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash -s -- ~/my-llm-wiki

Environment:
  VAULT_NAME=llm-wiki              Default target directory when no argument is passed.
  VAULT_BUILDER_REPO=owner/repo    Bootstrap repository. Defaults to cutehackers/vault-builder.
  VAULT_BUILDER_REF=main           Git ref to download. Defaults to main.
  VAULT_BUILDER_ARCHIVE_URL=url    Override the downloaded tarball URL.
  VAULT_BOOTSTRAP_VERIFY=quick     quick runs lint and health. full also runs scripts/release_gate.sh.
EOF
}

if [[ "${TARGET_DIR}" == "-h" || "${TARGET_DIR}" == "--help" ]]; then
  usage
  exit 0
fi

need_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    exit 1
  fi
}

need_command curl
need_command tar

choose_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.12 python3.11 python3.10 python3; do
    if [[ -n "${candidate}" ]] && command -v "${candidate}" >/dev/null 2>&1; then
      echo "${candidate}"
      return 0
    fi
  done
  echo "Missing required command: python3" >&2
  exit 1
}

PYTHON_BIN="$(choose_python)"

if [[ -e "${TARGET_DIR}/wiki/index.md" || -e "${TARGET_DIR}/tools/wiki/cli.py" ]]; then
  echo "Refusing to overwrite an existing initialized vault: ${TARGET_DIR}" >&2
  echo "Choose an empty target directory." >&2
  exit 1
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/vault-builder.XXXXXX")"
cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

archive_url="${VAULT_BUILDER_ARCHIVE_URL:-https://codeload.github.com/${REPO}/tar.gz/${REF}}"
echo "Downloading vault builder: ${REPO}@${REF}"
curl -fsSL "${archive_url}" | tar -xz -C "${tmp_dir}"

template_dir="$(find "${tmp_dir}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "${template_dir}" || ! -f "${template_dir}/init-vault.sh" ]]; then
  echo "Downloaded archive does not contain init-vault.sh" >&2
  exit 1
fi

bash "${template_dir}/init-vault.sh" "${TARGET_DIR}"

(
  cd "${TARGET_DIR}"
  echo
  echo "Running quick validation..."
  "${PYTHON_BIN}" tools/wiki/cli.py lint --report
  "${PYTHON_BIN}" tools/wiki/cli.py health

  if [[ "${VERIFY_MODE}" == "full" ]]; then
    echo
    echo "Running full release gate..."
    PYTHON="${PYTHON_BIN}" scripts/release_gate.sh
  elif [[ "${VERIFY_MODE}" != "quick" ]]; then
    echo "Unknown VAULT_BOOTSTRAP_VERIFY value: ${VERIFY_MODE}" >&2
    echo "Use quick or full." >&2
    exit 1
  fi
)

cat <<EOF

Ready:
  cd ${TARGET_DIR}
  ${PYTHON_BIN} tools/wiki/cli.py health
  ${PYTHON_BIN} tools/wiki/mcp_server.py
EOF
