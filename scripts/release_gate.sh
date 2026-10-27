#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
gate_root=""

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

run_from_root() {
  (
    cd "$ROOT"
    "$@"
  )
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

code_fingerprint() {
  local node_version
  node_version="$(node --version 2>/dev/null || echo none)"
  "$PYTHON_BIN" - "$1" "$PYTHON_BIN" "$node_version" <<'PY'
from pathlib import Path
import hashlib
import sys

root = Path(sys.argv[1])
skip_prefixes = {("wiki",), ("raw",), ("scratch",), (".git",), (".worktrees",), (".obsidian",)}
if (root / "init-vault.sh").is_file():
    # The builder copies wiki/, scratch/drafts and scratch/review into new vaults and tests that output.
    skip_prefixes = {("raw",), ("scratch", "reports"), (".git",), (".worktrees",), (".obsidian",)}
skip_parts = {"__pycache__", "node_modules", ".DS_Store"}
digest = hashlib.sha256()
digest.update(f"{sys.argv[2]}\0{sys.version}\0{sys.argv[3]}\0".encode())
for path in sorted(root.rglob("*")):
    relative = path.relative_to(root)
    if any(relative.parts[: len(prefix)] == prefix for prefix in skip_prefixes):
        continue
    if any(part in skip_parts for part in relative.parts):
        continue
    if path.suffix == ".pyc" or path.is_symlink() or not path.is_file():
        continue
    digest.update(relative.as_posix().encode() + b"\0")
    digest.update(hashlib.sha256(path.read_bytes()).digest())
print(digest.hexdigest())
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

# Tool tests take most of the gate's time and depend on code and docs, not on vault knowledge.
# In the builder repository they also test the wiki/ and scratch/ files copied into new vaults,
# so those count as code there.
# A passing run records a fingerprint of everything outside the knowledge directories in the Git
# directory, so later runs that only changed wiki/, raw/ or scratch/ skip the tool tests. The cache
# never lives in the vault's own files. Vaults without Git and fresh CI checkouts run every test.
tests_cache=""
code_fingerprint=""
unit_tests_cached=0
if [ "$git_worktree" -eq 1 ]; then
  tests_cache="$(git -C "$ROOT" rev-parse --absolute-git-dir)/llm-wiki-gate-tests.cache"
fi

gate_root="$(mktemp -d "${TMPDIR:-/tmp}/llm-wiki-release-gate.XXXXXX")"
wiki_tests=(
  tests/test_wiki_quality_baseline.py
  tests/test_wiki_tools.py
)
if [ -f "builder_tests/test_vault_migration.py" ] && [ -d "tools/vault_migration" ]; then
  wiki_tests+=(builder_tests/test_vault_migration.py)
fi
rsync -a --exclude ".git" --exclude "__pycache__" "$ROOT/" "$gate_root/"
cd "$gate_root"

# Fingerprint the copy that the tests run against, so a file edited during the gate cannot be
# recorded as tested.
if [ -n "$tests_cache" ]; then
  code_fingerprint="$(code_fingerprint "$gate_root")"
  if [ "${WIKI_GATE_FULL:-0}" != "1" ] && [ -f "$tests_cache" ] && [ "$(cat "$tests_cache")" = "$code_fingerprint" ]; then
    unit_tests_cached=1
  fi
fi

if [ "$unit_tests_cached" -eq 1 ]; then
  run "$PYTHON_BIN -m unittest ${wiki_tests[*]:0:1}" \
    "$PYTHON_BIN" -m unittest "${wiki_tests[@]:0:1}"
  printf '\n[release-gate] Tool tests skipped: code and docs match the last passing run. Set WIKI_GATE_FULL=1 to run them.\n'
else
  run "$PYTHON_BIN -m unittest ${wiki_tests[*]:0:2}" \
    "$PYTHON_BIN" -m unittest "${wiki_tests[@]:0:2}"
  if [ "${#wiki_tests[@]}" -gt 2 ]; then
    run "$PYTHON_BIN -m unittest ${wiki_tests[*]:2}" \
      run_from_root "$PYTHON_BIN" -m unittest "${wiki_tests[@]:2}"
  fi
fi
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

if [ -n "$tests_cache" ] && [ "$unit_tests_cached" -eq 0 ]; then
  if ! printf '%s\n' "$code_fingerprint" 2>/dev/null > "$tests_cache"; then
    printf '[release-gate] Could not record the passing test fingerprint; the next run will run every test.\n' >&2
  fi
fi
