#!/usr/bin/env bash
# vault-builder install command (spec 4.3, 4.8).
#
#   curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash
#
# Checks git, curl and Python 3.10 or newer, handles a missing uv (decision 4), clones or updates the
# builder checkout, writes the vault-builder command file, then runs `vault-builder setup` with the
# arguments this script does not use itself. --uninstall removes everything again.
#
# The whole script is one function called on the last line, so `curl | bash` reads all of it before
# anything runs. Questions are read from /dev/tty because stdin is the script.
set -euo pipefail

REPO="${VAULT_BUILDER_REPO:-cutehackers/vault-builder}"
REF="${VAULT_BUILDER_REF:-main}"
UV_INSTALL_COMMAND="curl -LsSf https://astral.sh/uv/install.sh | sh"
INTERACTIVE=0

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash -s -- --repo /path/app --yes
  curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash -s -- --uninstall

Options of this script:
  --install-uv      Install uv without asking when it is missing.
  --uninstall       Remove the vaults' outside files, the hooks, both MCP entries, vg, the vault-builder
                    command and the builder checkout. Vault folders, ~/.vault-graph and the
                    embedding model folder (~/llm/models) stay.
Every other option goes to vault-builder setup (see vault-builder setup --help), for example
--repo PATH, --vault NAME=PATH, --role NAME=ROLE, --yes, --dry-run, --without-vault-graph.

Environment:
  VAULT_BUILDER_REPO=owner/repo    Builder repository: owner/repo on GitHub, a git URL or a local path.
                                   Defaults to cutehackers/vault-builder.
  VAULT_BUILDER_REF=main           Branch or tag to clone. Defaults to main.
  VAULT_BUILDER_HOME=path          Builder checkout to use. Defaults to ~/.local/share/vault-builder.
  PYTHON=python3.12                Python 3.10 or newer to use.
EOF
}

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

# Same rule as scripts/bootstrap.sh; builder_tests/test_install_script.py checks that the text matches.
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

print_tool_install_help() {
  local tool="$1"
  echo "Missing required command: ${tool}" >&2
  case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin)
      if [[ "${tool}" == "git" ]]; then
        echo "  xcode-select --install   (or: brew install git)" >&2
      else
        echo "  brew install ${tool}" >&2
      fi
      ;;
    Linux)
      echo "  sudo apt-get install ${tool}   (Debian, Ubuntu)" >&2
      echo "  sudo dnf install ${tool}       (Fedora, RHEL)" >&2
      ;;
  esac
}

# Sets PYTHON_BIN. Stops with exit 1 before writing anything when a tool is missing.
check_tools() {
  local missing=0 tool
  for tool in "$@"; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      print_tool_install_help "${tool}"
      missing=1
    fi
  done
  if ! PYTHON_BIN="$(choose_python)"; then
    missing=1
  fi
  if [[ "${missing}" -ne 0 ]]; then
    echo "Install the missing tools, then run this command again. Nothing was written." >&2
    exit 1
  fi
}

# Questions need /dev/tty. --yes or no terminal means non-interactive mode.
detect_interactive() {
  local yes="$1"
  INTERACTIVE=0
  if [[ "${yes}" -eq 0 ]] && (: </dev/tty) 2>/dev/null; then
    INTERACTIVE=1
  fi
}

ask_tty() {
  local question="$1" answer=""
  printf '%s ' "${question}" >/dev/tty
  if ! IFS= read -r answer </dev/tty; then
    return 1
  fi
  printf '%s' "${answer}"
}

install_uv() {
  echo "Installing uv: ${UV_INSTALL_COMMAND}"
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    echo "The uv installer failed. Run it yourself to see why: ${UV_INSTALL_COMMAND}" >&2
    exit 1
  fi
  hash -r
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv was installed but is not on PATH. Open a new terminal, then run this command again." >&2
    exit 1
  fi
}

# Decision 4.
ensure_uv() {
  local install_uv_flag="$1" answer
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  if [[ "${install_uv_flag}" -eq 1 ]]; then
    install_uv
    return 0
  fi
  if [[ "${INTERACTIVE}" -eq 1 ]]; then
    printf 'uv is not installed. vault-builder setup uses it to install vault-graph.\n' >/dev/tty
    printf 'The uv install command is: %s\n' "${UV_INSTALL_COMMAND}" >/dev/tty
    answer="$(ask_tty "Install uv now? [Y/n]")" || answer="n"
    case "${answer}" in
      ""|y|Y|yes|YES|Yes)
        install_uv
        return 0
        ;;
    esac
    echo "uv is required. Install it with: ${UV_INSTALL_COMMAND}" >&2
    exit 1
  fi
  echo "uv is required. Install it with: ${UV_INSTALL_COMMAND}" >&2
  echo "Or run this command again with --install-uv to install uv automatically." >&2
  exit 1
}

repo_url() {
  if [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && ! -e "${REPO}" ]]; then
    echo "https://github.com/${REPO}.git"
  else
    echo "${REPO}"
  fi
}

is_builder_checkout() {
  local dir="$1" top
  [[ -d "${dir}" && ! -L "${dir}" ]] || return 1
  [[ -f "${dir}/tools/vault_migration/cli.py" && -f "${dir}/tools/vault_migration/builder_command.py" ]] || return 1
  top="$(git -C "${dir}" rev-parse --show-toplevel 2>/dev/null)" || return 1
  [[ "$(cd "${top}" && pwd -P)" == "$(cd "${dir}" && pwd -P)" ]]
}

checkout_path() {
  local path="${VAULT_BUILDER_HOME:-${HOME}/.local/share/vault-builder}"
  case "${path}" in
    /*) ;;
    *) path="${PWD}/${path}" ;;
  esac
  echo "${path%/}"
}

# Sets CHECKOUT.
prepare_checkout() {
  CHECKOUT="$(checkout_path)"
  if [[ -e "${CHECKOUT}" || -L "${CHECKOUT}" ]]; then
    if ! is_builder_checkout "${CHECKOUT}"; then
      echo "${CHECKOUT} exists and is not a vault-builder git checkout. Nothing there was changed." >&2
      echo "Move it away, or choose another place: VAULT_BUILDER_HOME=<path>" >&2
      exit 1
    fi
    if GIT_TERMINAL_PROMPT=0 git -C "${CHECKOUT}" pull --ff-only --quiet </dev/null >/dev/null 2>&1; then
      echo "Builder checkout: ${CHECKOUT} (updated)"
    else
      echo "warning: git pull --ff-only failed in ${CHECKOUT}; using that checkout as it is." >&2
    fi
    return 0
  fi
  local url
  url="$(repo_url)"
  echo "Cloning vault-builder ${url} (${REF}) into ${CHECKOUT}"
  mkdir -p "$(dirname "${CHECKOUT}")"
  if ! GIT_TERMINAL_PROMPT=0 git clone --quiet --branch "${REF}" "${url}" "${CHECKOUT}" </dev/null; then
    echo "git clone ${url} failed. Check VAULT_BUILDER_REPO and VAULT_BUILDER_REF." >&2
    exit 1
  fi
}

# Writes ~/.local/bin/vault-builder with builder_command.command_text, so the text has one source.
write_command_file() {
  local python_path
  python_path="$(command -v "${PYTHON_BIN}")"
  "${PYTHON_BIN}" -I - "${CHECKOUT}" "${python_path}" "${HOME}" <<'PY'
import os
import sys
from pathlib import Path

checkout, python, home = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, str(checkout))
from tools.vault_migration import builder_command  # noqa: E402

path = builder_command.command_path(home)
text = builder_command.command_text(checkout, python).encode("utf-8")
verb = "created"
if path.exists() or path.is_symlink():
    current = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    if current == text:
        if not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
        print(f"vault-builder command: {path} (unchanged)")
        raise SystemExit(0)
    if not builder_command.has_marker(current):
        print(
            f"{path} exists and was not written by vault-builder (no marker line); it was not changed. "
            f"Move it away, for example: mv {path} {path}.mine, then run this command again.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    verb = "replaced"
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(text)
os.chmod(temporary, 0o755)
os.replace(temporary, path)
print(f"vault-builder command: {path} ({verb})")
PY
}

print_path_hint() {
  local rc
  case "${SHELL##*/}" in
    zsh) rc="${HOME}/.zshrc" ;;
    bash)
      if [[ "$(uname -s 2>/dev/null || echo unknown)" == "Darwin" ]]; then
        rc="${HOME}/.bash_profile"
      else
        rc="${HOME}/.bashrc"
      fi
      ;;
    *) rc="your shell startup file" ;;
  esac
  echo
  echo "${HOME}/.local/bin is not on PATH. To run vault-builder by name, add this line to ${rc} and open a new terminal:"
  # shellcheck disable=SC2016 # the line is printed for the user to paste, not expanded here
  echo 'export PATH="$HOME/.local/bin:$PATH"'
}

claude_running() {
  command -v pgrep >/dev/null 2>&1 && pgrep -x claude >/dev/null 2>&1
}

# Decision 5, for --uninstall. setup checks it again itself.
require_claude_closed() {
  while claude_running; do
    if [[ "${INTERACTIVE}" -eq 1 ]]; then
      if ! ask_tty "Claude Code is running. Quit every Claude Code session (terminal, IDE and app), then press Enter." >/dev/null; then
        echo "Claude Code is still running; nothing was removed." >&2
        exit 3
      fi
    else
      echo "Claude Code is running. Quit every Claude Code session (terminal, IDE and app), then run this command again." >&2
      echo "Nothing was removed." >&2
      exit 3
    fi
  done
}

# Prints a builder_command value (MARKER) or the vault paths of the vault map, from the checkout.
checkout_python() {
  local what="$1"
  "${PYTHON_BIN}" -I - "${CHECKOUT}" "${what}" <<'PY'
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
if sys.argv[2] == "marker":
    from tools.vault_migration import builder_command

    print(builder_command.MARKER)
    raise SystemExit(0)
from tools.vault_migration import installing

try:
    path = installing.resolve_user_paths(os.environ).vault_map
    vaults = json.loads(Path(path).read_text(encoding="utf-8")).get("vaults", {})
except (OSError, ValueError, AttributeError, installing.InstallError):
    raise SystemExit(0)
for value in vaults.values() if isinstance(vaults, dict) else ():
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        print(value["path"])
PY
}

has_marker() {
  local file="$1" marker="$2"
  [[ -n "${marker}" && -f "${file}" && ! -L "${file}" ]] && grep -Fqx -- "${marker}" "${file}"
}

# Spec 4.8.
run_uninstall() {
  local command_file="${HOME}/.local/bin/vault-builder" default_checkout="${HOME}/.local/share/vault-builder"
  local marker="" vault_paths="" have_checkout=0 vg="" uv_bin="" line
  require_claude_closed
  CHECKOUT="$(checkout_path)"
  if is_builder_checkout "${CHECKOUT}"; then
    have_checkout=1
    marker="$(checkout_python marker)"
    vault_paths="$(checkout_python vaults)"
  fi

  # 1. vault-builder uninstall --all. A failure stops here: the vg MCP entry and vg stay.
  local status=0
  if has_marker "${command_file}" "${marker}"; then
    "${command_file}" uninstall --all || status=$?
  elif [[ "${have_checkout}" -eq 1 ]]; then
    "${PYTHON_BIN}" "${CHECKOUT}/tools/vault_migration/cli.py" uninstall --all || status=$?
  elif [[ -e "${command_file}" || -L "${command_file}" ]]; then
    echo "No vault-builder checkout at ${CHECKOUT}, so ${command_file} could not be checked." >&2
    echo "Give the checkout it uses with VAULT_BUILDER_HOME=<path>, then run this command again. Nothing was removed." >&2
    exit 1
  else
    echo "Note: no vault-builder checkout and no ${command_file}; skipped vault-builder uninstall --all."
  fi
  if [[ "${status}" -ne 0 ]]; then
    echo "vault-builder uninstall --all failed (exit ${status}); stopped before vg mcp unregister and uv tool uninstall vault-graph." >&2
    echo "Fix the error above, then run this command again." >&2
    exit 1
  fi

  # 2. vg mcp unregister --agent claude.
  if command -v uv >/dev/null 2>&1; then
    uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
  fi
  if [[ -n "${uv_bin}" && -x "${uv_bin}/vg" ]]; then
    vg="${uv_bin}/vg"
  elif command -v vg >/dev/null 2>&1; then
    vg="$(command -v vg)"
  fi
  if [[ -n "${vg}" ]]; then
    if ! "${vg}" mcp unregister --agent claude; then
      echo "${vg} mcp unregister --agent claude failed; stopped before uv tool uninstall vault-graph." >&2
      exit 1
    fi
  else
    echo "Note: vg was not found; skipped vg mcp unregister --agent claude."
  fi

  # 3. uv tool uninstall vault-graph.
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^vault-graph '; then
    if ! uv tool uninstall vault-graph; then
      echo "uv tool uninstall vault-graph failed." >&2
      exit 1
    fi
  else
    echo "Note: vault-graph is not installed with uv; skipped uv tool uninstall vault-graph."
  fi

  # 4. What uninstall --all left: a marked command file and the default checkout.
  if has_marker "${command_file}" "${marker}"; then
    rm -f "${command_file}"
    echo "Deleted ${command_file}"
  fi
  if [[ "${CHECKOUT}" == "${default_checkout}" && -z "${VAULT_BUILDER_HOME:-}" ]] && is_builder_checkout "${CHECKOUT}"; then
    rm -rf "${CHECKOUT}"
    echo "Deleted ${CHECKOUT}"
  fi

  # 5. What stays.
  echo
  echo "Kept (delete them yourself if you no longer need them):"
  if [[ -n "${vault_paths}" ]]; then
    while IFS= read -r line; do
      [[ -n "${line}" ]] && echo "  vault: ${line}"
    done <<EOF
${vault_paths}
EOF
  fi
  echo "  vault-graph data: ${HOME}/.vault-graph"
  # The embedding model folder is ~/llm/models unless vg embedding use saved another one.
  local models_dir="${HOME}/llm/models" saved_dir=""
  local embedding_settings="${HOME}/.vault-graph/configs/embedding.json"
  if [[ -f "${embedding_settings}" ]] && command -v python3 >/dev/null 2>&1; then
    saved_dir="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("models_dir") or "")' \
      "${embedding_settings}" 2>/dev/null || true)"
    [[ -n "${saved_dir}" ]] && models_dir="${saved_dir}"
  fi
  echo "  embedding model folder: ${models_dir}"
}

main() {
  local install_uv_flag=0 uninstall=0 yes=0 without_vault_graph=0 arg
  local setup_args=()
  for arg in "$@"; do
    case "${arg}" in
      --install-uv) install_uv_flag=1 ;;
      --uninstall) uninstall=1 ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        case "${arg}" in
          --yes) yes=1 ;;
          --without-vault-graph) without_vault_graph=1 ;;
        esac
        setup_args+=("${arg}")
        ;;
    esac
  done
  detect_interactive "${yes}"

  local path_hint=0
  case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *)
      path_hint=1
      PATH="${PATH}:${HOME}/.local/bin"
      export PATH
      ;;
  esac

  if [[ "${uninstall}" -eq 1 ]]; then
    local extra
    for extra in ${setup_args[@]+"${setup_args[@]}"}; do
      if [[ "${extra}" != "--yes" ]]; then
        echo "--uninstall takes no other option than --yes, got ${extra}." >&2
        exit 2
      fi
    done
    check_tools
    run_uninstall
    return 0
  fi

  check_tools git curl
  if [[ "${without_vault_graph}" -eq 0 ]]; then
    ensure_uv "${install_uv_flag}"
  fi
  prepare_checkout
  write_command_file

  local status=0
  "${HOME}/.local/bin/vault-builder" setup ${setup_args[@]+"${setup_args[@]}"} || status=$?
  if [[ "${path_hint}" -eq 1 ]]; then
    print_path_hint
  fi
  return "${status}"
}

main "$@"
