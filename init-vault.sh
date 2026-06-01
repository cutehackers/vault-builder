#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=""
WITH_STENC=0

usage() {
  cat <<'EOF'
Usage: ./init-vault.sh [--with-stenc] [target-dir]

Create a ready-to-use LLM Wiki vault.

Options:
  --with-stenc  Include optional Stenc fixed-format spec/plan docs and tools.
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

require_stenc_bundle() {
  local missing=0
  for required_path in \
    "docs/stenc/content" \
    "tools/stenc/validate-stenc-doc.js" \
    "tools/stenc/setup-project.js" \
    "tools/stenc/check-rendered-pages.js"
  do
    if [[ ! -e "${SCRIPT_DIR}/${required_path}" ]]; then
      echo "Optional Stenc assets are not available in this vault template: ${required_path}" >&2
      missing=1
    fi
  done
  if [[ "${missing}" -ne 0 ]]; then
    echo "Create the vault without --with-stenc, or run --with-stenc from a template that includes Stenc." >&2
    exit 1
  fi
}

mkdir -p "${TARGET_DIR}/raw/sources" "${TARGET_DIR}/raw/assets" "${TARGET_DIR}/raw/imports"
mkdir -p "${TARGET_DIR}/scratch/drafts" "${TARGET_DIR}/scratch/reports" "${TARGET_DIR}/scratch/review"
mkdir -p "${TARGET_DIR}/docs"

copy_file ".gitignore"
copy_file "init-vault.sh"
copy_file "AGENTS.md"
copy_file "README.md"
copy_file "README-kr.md"
copy_file "docs/LLM-WIKI.md"
copy_file "docs/usage.md"
copy_file "docs/architecture.md"
copy_tree ".github"
copy_tree "agents"
copy_tree "docs/agent"
copy_tree "scripts"
copy_tree "tools/wiki"
copy_tree "tests"
copy_tree "wiki"
copy_tree "scratch/drafts"
copy_tree "scratch/review"

if [[ "${WITH_STENC}" -eq 1 ]]; then
  require_stenc_bundle
  copy_file "docs/stenc/.gitignore"
  copy_tree "docs/stenc/content"
  copy_tree "tools/stenc"
fi

touch "${TARGET_DIR}/raw/sources/.gitkeep"
touch "${TARGET_DIR}/raw/assets/.gitkeep"
touch "${TARGET_DIR}/raw/imports/.gitkeep"
touch "${TARGET_DIR}/scratch/reports/.gitkeep"

chmod +x "${TARGET_DIR}/init-vault.sh" 2>/dev/null || true
chmod +x "${TARGET_DIR}/scripts/bootstrap.sh" 2>/dev/null || true
chmod +x "${TARGET_DIR}/scripts/release_gate.sh" 2>/dev/null || true

echo "LLM Wiki Vault initialized: ${TARGET_DIR}"
if [[ "${WITH_STENC}" -eq 1 ]]; then
  echo "Optional Stenc docs and tools included."
else
  echo "Optional Stenc docs and tools skipped. Re-run with --with-stenc to include them."
fi
echo "Next:"
echo "  cd ${TARGET_DIR}"
echo "  python3 tools/wiki/cli.py lint --report"
echo "  python3 tools/wiki/cli.py health"
