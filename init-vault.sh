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
copy_file "AGENTS.md"
copy_file "CLAUDE.md"
copy_file "README.md"
copy_file "README-kr.md"
copy_file "docs/LLM-WIKI.md"
copy_file "docs/usage.md"
copy_file "docs/USER_PROMPTS.md"
copy_file "docs/VAULT_PROMPTS.md"
copy_tree ".github"
copy_tree ".agents"
copy_file ".claude/skills/wiki-ingest/SKILL.md"
copy_file ".claude/skills/wiki-update/SKILL.md"
copy_file ".claude/skills/wiki-query/SKILL.md"
copy_tree "docs/agent"
copy_file "scripts/release_gate.sh"
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

chmod 0755 "${TARGET_DIR}/scripts/release_gate.sh" 2>/dev/null || true

# .mcp.json is machine-specific (chosen interpreter, absolute path), so it is not a managed file.
# Record the absolute interpreter path: Claude Code may start MCP servers with a PATH that lacks it.
PYTHON_ABS="$(command -v "${PYTHON_BIN}")"
"${PYTHON_BIN}" - "${PYTHON_ABS}" "${TARGET_DIR}" <<'PY'
import json
import sys
from pathlib import Path

python_bin, target_dir = str(Path(sys.argv[1]).absolute()), Path(sys.argv[2]).resolve()
config = {
    "mcpServers": {
        "llm-wiki": {
            "command": python_bin,
            "args": [str(target_dir / "tools" / "wiki" / "mcp_server.py")],
        }
    }
}
(target_dir / ".mcp.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

"${PYTHON_BIN}" "${SCRIPT_DIR}/tools/vault_migration/cli.py" manifest \
  --builder-root "${SCRIPT_DIR}" \
  --target "${TARGET_DIR}" \
  --builder-revision "${VAULT_BUILDER_REVISION:-contract-v1}"

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
