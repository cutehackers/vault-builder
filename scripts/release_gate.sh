#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
  echo "usage: scripts/release_gate.sh [--tier check|unit|full]" >&2
  exit 2
}

is_builder_repo() {
  [ -f "$ROOT/init-vault.sh" ] && [ -d "$ROOT/tools/vault_migration" ]
}

# check: vault content checks only. unit: check plus the tests not marked full_tier_only.
# full: check plus every test. Without --tier the builder runs unit and a vault runs check.
tier=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tier)
      [ "$#" -ge 2 ] || usage
      tier="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done
if [ -z "$tier" ]; then
  if is_builder_repo; then
    tier="unit"
  else
    tier="check"
  fi
fi
case "$tier" in
  check|unit|full) ;;
  *) usage ;;
esac
printf '[release-gate] tier: %s\n' "$tier"

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
  print_python_install_help
  exit 1
}

PYTHON_BIN="$(choose_python)"
printf '[release-gate] python: %s (%s)\n' \
  "$(command -v "$PYTHON_BIN")" \
  "$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')"

run() {
  local label="$1"
  shift
  printf '\n[release-gate] %s\n' "$label"
  "$@"
}

tracked_diff_fingerprint() {
  git -C "$ROOT" diff --no-ext-diff --binary -- . | shasum -a 256
}

worktree_status() {
  git -C "$ROOT" status --porcelain --untracked-files=all -- .
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

# Every step runs in $ROOT and must not write tracked or untracked (non-ignored) files.
# The write guard compares the tracked diff and the porcelain status before and after.
git_worktree=0
tracked_diff_before=""
status_before=""
if is_vault_git_worktree; then
  git_worktree=1
  tracked_diff_before="$(tracked_diff_fingerprint)"
  status_before="$(worktree_status)"
else
  printf '[release-gate] No Git worktree detected; write guard will be skipped.\n'
fi

if [ "$tier" != "check" ]; then
  run "WIKI_GATE_TIER=$tier $PYTHON_BIN -m unittest discover -s tests -t . -p 'test_*.py'" \
    env WIKI_GATE_TIER="$tier" "$PYTHON_BIN" -m unittest discover -s tests -t . -p 'test_*.py'
  if is_builder_repo && [ -d "builder_tests" ]; then
    run "WIKI_GATE_TIER=$tier $PYTHON_BIN -m unittest discover -s builder_tests -t . -p 'test_*.py'" \
      env WIKI_GATE_TIER="$tier" "$PYTHON_BIN" -m unittest discover -s builder_tests -t . -p 'test_*.py'
  fi
fi

run "$PYTHON_BIN tools/wiki/cli.py lint" \
  "$PYTHON_BIN" tools/wiki/cli.py lint
run "$PYTHON_BIN tools/wiki/cli.py maps build --check" \
  "$PYTHON_BIN" tools/wiki/cli.py maps build --check
run "$PYTHON_BIN tools/wiki/cli.py metrics --check" \
  "$PYTHON_BIN" tools/wiki/cli.py metrics --check
run "$PYTHON_BIN tools/wiki/cli.py sessions git-check" \
  "$PYTHON_BIN" tools/wiki/cli.py sessions git-check

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

  run "node setup-project.js --project-root $ROOT --docs-dir docs/stenc" \
    node "$stenc_setup" --project-root "$ROOT" --docs-dir docs/stenc
  run "node check-rendered-pages.js docs/stenc" \
    node "$stenc_check" docs/stenc
else
  printf '\n[release-gate] Stenc checks skipped. Set WIKI_ENABLE_STENC=1 to validate optional Stenc docs.\n'
fi

cd "$ROOT"
if [ "$git_worktree" -eq 1 ]; then
  tracked_diff_after="$(tracked_diff_fingerprint)"
  status_after="$(worktree_status)"
  if [ "$tracked_diff_before" != "$tracked_diff_after" ] || [ "$status_before" != "$status_after" ]; then
    changed_paths="$(comm -13 <(printf '%s\n' "$status_before" | sort) <(printf '%s\n' "$status_after" | sort) | sed '/^$/d')"
    if [ -n "$changed_paths" ]; then
      printf '[release-gate] Paths changed or created during the gate:\n%s\n' "$changed_paths" >&2
    fi
    printf '[release-gate] Release gate changed tracked output. Re-run or inspect generated artifacts.\n' >&2
    exit 1
  fi

  run "git diff --check" git -C "$ROOT" diff --check -- .
else
  run "check_workspace_whitespace" check_workspace_whitespace
fi
