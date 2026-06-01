#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
gate_root=""

choose_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.12 python3.11 python3.10 python3; do
    if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
        echo "$candidate"
        return 0
      fi
    fi
  done
  printf '[release-gate] Python 3.10 or newer is required.\n' >&2
  exit 1
}

PYTHON_BIN="$(choose_python)"

cleanup() {
  if [ -n "$gate_root" ]; then
    rm -rf "$gate_root"
  fi
}
trap cleanup EXIT

run() {
  local label="$1"
  shift
  printf '\n[release-gate] %s\n' "$label"
  "$@"
}

tracked_diff_fingerprint() {
  git -C "$ROOT" diff --no-ext-diff --binary -- . | shasum -a 256
}

is_vault_git_worktree() {
  local git_top
  git_top="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$git_top" ]; then
    return 1
  fi
  [ "$(cd "$git_top" && pwd -P)" = "$(cd "$ROOT" && pwd -P)" ]
}

check_workspace_whitespace() {
  "$PYTHON_BIN" - "$ROOT" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
skip_dirs = {".git", "__pycache__", "node_modules", ".venv"}
text_suffixes = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
issues = []

for path in sorted(root.rglob("*")):
    if not path.is_file():
        continue
    relative = path.relative_to(root)
    if any(part in skip_dirs for part in relative.parts):
        continue
    if path.suffix not in text_suffixes:
        continue
    try:
        lines = path.read_text().splitlines()
    except UnicodeDecodeError:
        continue
    for line_number, line in enumerate(lines, start=1):
        if line.rstrip(" \t") != line:
            issues.append(f"{relative}:{line_number}: trailing whitespace")

if issues:
    print("\n".join(issues), file=sys.stderr)
    raise SystemExit(1)
PY
}

stenc_checks_enabled() {
  case "${WIKI_ENABLE_STENC:-0}" in
    1|true|TRUE|yes|YES)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

git_worktree=0
tracked_diff_before=""
if is_vault_git_worktree; then
  git_worktree=1
  tracked_diff_before="$(tracked_diff_fingerprint)"
else
  printf '[release-gate] No Git worktree detected; tracked-diff guard will be skipped.\n'
fi

gate_root="$(mktemp -d "${TMPDIR:-/tmp}/llm-wiki-release-gate.XXXXXX")"
rsync -a --exclude ".git" --exclude "__pycache__" "$ROOT/" "$gate_root/"
cd "$gate_root"

run "$PYTHON_BIN -m unittest tests/test_wiki_quality_baseline.py tests/test_wiki_tools.py" \
  "$PYTHON_BIN" -m unittest tests/test_wiki_quality_baseline.py tests/test_wiki_tools.py
run "$PYTHON_BIN tools/wiki/cli.py lint --report" \
  "$PYTHON_BIN" tools/wiki/cli.py lint --report
run "$PYTHON_BIN tools/wiki/cli.py health" \
  "$PYTHON_BIN" tools/wiki/cli.py health
run "$PYTHON_BIN tools/wiki/cli.py maps build --check --report" \
  "$PYTHON_BIN" tools/wiki/cli.py maps build --check --report
run "$PYTHON_BIN tools/wiki/cli.py metrics --check --report" \
  "$PYTHON_BIN" tools/wiki/cli.py metrics --check --report

if stenc_checks_enabled; then
  if [ ! -d "docs/stenc/content" ]; then
    printf '[release-gate] Stenc checks enabled, but docs/stenc/content does not exist.\n' >&2
    exit 1
  fi

  stenc_validate="${STENC_VALIDATE:-tools/stenc/validate-stenc-doc.js}"
  stenc_setup="${STENC_SETUP:-tools/stenc/setup-project.js}"
  stenc_check="${STENC_CHECK_RENDERED:-tools/stenc/check-rendered-pages.js}"

  for stenc_script in "$stenc_validate" "$stenc_setup" "$stenc_check"; do
    if [ ! -f "$stenc_script" ]; then
      printf '[release-gate] missing Stenc script: %s\n' "$stenc_script" >&2
      exit 1
    fi
  done

  while IFS= read -r stenc_doc; do
    run "node validate-stenc-doc.js $stenc_doc" \
      node "$stenc_validate" "$stenc_doc"
  done < <(find docs/stenc/content -type f -name '*.json' | sort)

  run "node setup-project.js --project-root $gate_root --docs-dir docs/stenc" \
    node "$stenc_setup" --project-root "$gate_root" --docs-dir docs/stenc
  run "node check-rendered-pages.js docs/stenc" \
    node "$stenc_check" docs/stenc
else
  printf '\n[release-gate] Stenc checks skipped. Set WIKI_ENABLE_STENC=1 to validate optional Stenc docs.\n'
fi

cd "$ROOT"
if [ "$git_worktree" -eq 1 ]; then
  tracked_diff_after="$(tracked_diff_fingerprint)"
  if [ "$tracked_diff_before" != "$tracked_diff_after" ]; then
    printf '[release-gate] Release gate changed tracked output. Re-run or inspect generated artifacts.\n' >&2
    exit 1
  fi

  run "git diff --check" git -C "$ROOT" diff --check -- .
else
  run "check_workspace_whitespace" check_workspace_whitespace
fi
