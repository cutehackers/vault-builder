#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-.}"

usage() {
  cat <<'EOF'
Usage: ./init-vault.sh [target-dir]

Create a ready-to-use LLM Wiki vault.

Examples:
  ./init-vault.sh
  ./init-vault.sh ~/wiki/my-llm-wiki
EOF
}

if [[ "${TARGET_DIR}" == "-h" || "${TARGET_DIR}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${TARGET_DIR}" != /* ]]; then
  TARGET_DIR="$(pwd)/${TARGET_DIR}"
fi

if [[ -e "${TARGET_DIR}/wiki/index.md" || -e "${TARGET_DIR}/tools/wiki/cli.py" ]]; then
  echo "Refusing to overwrite an existing initialized vault: ${TARGET_DIR}" >&2
  echo "Choose an empty target directory." >&2
  exit 1
fi

mkdir -p "${TARGET_DIR}"

copy_file() {
  local relative_path="$1"
  local source_path="${SCRIPT_DIR}/${relative_path}"
  local target_path="${TARGET_DIR}/${relative_path}"
  if [[ ! -f "${source_path}" ]]; then
    return
  fi
  mkdir -p "$(dirname "${target_path}")"
  cp "${source_path}" "${target_path}"
}

copy_tree() {
  local relative_path="$1"
  local source_root="${SCRIPT_DIR}/${relative_path}"
  if [[ ! -d "${source_root}" ]]; then
    return
  fi
  while IFS= read -r -d '' source_path; do
    local relative_file="${source_path#"${source_root}/"}"
    local target_path="${TARGET_DIR}/${relative_path}/${relative_file}"
    mkdir -p "$(dirname "${target_path}")"
    cp "${source_path}" "${target_path}"
  done < <(
    find "${source_root}" -type f \
      ! -path '*/__pycache__/*' \
      ! -name '*.pyc' \
      ! -name '.DS_Store' \
      -print0
  )
}

mkdir -p "${TARGET_DIR}/raw/sources" "${TARGET_DIR}/raw/assets" "${TARGET_DIR}/raw/imports"
mkdir -p "${TARGET_DIR}/scratch/drafts" "${TARGET_DIR}/scratch/reports" "${TARGET_DIR}/scratch/review"
mkdir -p "${TARGET_DIR}/docs"

copy_file ".gitignore"
copy_file "init-vault.sh"
copy_file "AGENTS.md"
copy_file "README.md"
copy_file "docs/LLM-WIKI.md"
copy_file "docs/usage.md"
copy_file "docs/architecture.md"
copy_tree ".github"
copy_tree "agents"
copy_tree "docs/agent"
copy_file "docs/stenc/.gitignore"
copy_tree "docs/stenc/content"
copy_tree "scripts"
copy_tree "tools/stenc"
copy_tree "tools/wiki"
copy_tree "tests"
copy_tree "wiki"
copy_tree "scratch/drafts"
copy_tree "scratch/review"

touch "${TARGET_DIR}/raw/sources/.gitkeep"
touch "${TARGET_DIR}/raw/assets/.gitkeep"
touch "${TARGET_DIR}/raw/imports/.gitkeep"
touch "${TARGET_DIR}/scratch/reports/.gitkeep"

chmod +x "${TARGET_DIR}/init-vault.sh" 2>/dev/null || true
chmod +x "${TARGET_DIR}/scripts/bootstrap.sh" 2>/dev/null || true
chmod +x "${TARGET_DIR}/scripts/release_gate.sh" 2>/dev/null || true

echo "LLM Wiki Vault initialized: ${TARGET_DIR}"
echo "Next:"
echo "  cd ${TARGET_DIR}"
echo "  python3 tools/wiki/cli.py lint --report"
echo "  python3 tools/wiki/cli.py health"
