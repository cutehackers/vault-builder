#!/usr/bin/env python3
"""User-scope Claude Code hook launcher for vault-builder (vault-graph requests spec, contract 6).

Usage: vault_hooks.py session-start | capture | capture --direct

Every subcommand reads the Claude Code hook JSON from stdin, passes it on unchanged, and exits 0.
A hook that fails or runs long must never block the Claude Code session.

- session-start: run `vg hook session-start`. On exit 0 print its stdout as is (empty output too).
  Otherwise print the output of the installed project-memory recall.sh.
- capture: run `vg hook capture`. On exit 0 stop. Otherwise do what `capture --direct` does.
- capture --direct: pick the one role project vault of the hook cwd project and pass the hook JSON
  to that vault's `tools/wiki/cli.py sessions capture --json`. The vault choice must stay the same
  as select_target_vault in tools/wiki/lib/session_capture.py. builder_tests/test_vault_hooks_launcher.py
  runs both on one table of cases.

vg lookup order: VAULT_GRAPH_BIN, the "vg" absolute path in <config dir>/vault-builder/vault-hooks.json,
then vg on PATH. <config dir> is CLAUDE_CONFIG_DIR, else $HOME/.claude.

The project lookup rule (root, origin remote, folder name) is not copied here. It is loaded from the
llm-wiki router file, which install puts in the same folder as this file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Mapping

PREFIX = "vault-hooks: "
USAGE = "usage: vault_hooks.py session-start | capture [--direct]"

# Claude Code gives the capture hook entries 60 seconds (contract 6). vg and the direct capture share it.
VG_SESSION_START_TIMEOUT_S = 20.0
RECALL_TIMEOUT_S = 20.0
VG_CAPTURE_TIMEOUT_S = 25.0
DIRECT_CAPTURE_TIMEOUT_S = 30.0

PROJECT_ROLE = "project"
MIN_CONTRACT_VERSION = 3
MANIFEST_RELATIVE = Path(".vault-builder") / "manifest.json"
HOOKS_CONFIG_NAME = "vault-hooks.json"
ROUTER_FILE = "llm_wiki_router.py"
ROUTER_MODULE = "vault_builder_llm_wiki_router"


def config_dir(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    home = environ.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_vg(environ: Mapping[str, str]) -> Path | None:
    """Return the first usable vg: VAULT_GRAPH_BIN, vault-hooks.json "vg", PATH. None when none is usable."""
    configured = environ.get("VAULT_GRAPH_BIN")
    if configured and _executable(Path(configured).expanduser()):
        return Path(configured).expanduser()
    try:
        recorded = json.loads((config_dir(environ) / "vault-builder" / HOOKS_CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        recorded = None
    value = recorded.get("vg") if isinstance(recorded, dict) else None
    if isinstance(value, str) and os.path.isabs(value) and _executable(Path(value)):
        return Path(value)
    found = shutil.which("vg", path=environ.get("PATH", ""))
    return Path(found) if found else None


def run_process(
    command: list[str], data: bytes, cwd: Path, timeout: float, environ: Mapping[str, str]
) -> tuple[int | None, bytes, bytes]:
    """Run one process with data on stdin. Returns (exit code or None on timeout or start failure, stdout, stderr).

    The process runs in its own process group so a timeout stops its children too.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return None, b"", f"{PREFIX}cannot start {command[0]}: {exc.strerror or exc}\n".encode()
    try:
        stdout, stderr = process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.communicate()
        return None, b"", f"{PREFIX}{command[0]} timed out after {timeout:g} seconds and was stopped.\n".encode()
    return process.returncode, stdout, stderr


def hook_cwd(data: bytes) -> Path:
    """The hook JSON cwd when it is a non-empty string, else this process's folder."""
    try:
        hook = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        hook = None
    value = hook.get("cwd") if isinstance(hook, dict) else None
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return Path.cwd()


def process_cwd(data: bytes) -> Path:
    """The folder child processes start in: the hook cwd when it exists, else this process's folder."""
    cwd = hook_cwd(data)
    return cwd if cwd.is_dir() else Path.cwd()


def session_start(data: bytes, environ: Mapping[str, str], out, err) -> None:
    cwd = process_cwd(data)
    vg = find_vg(environ)
    if vg is not None:
        code, stdout, stderr = run_process(
            [str(vg), "hook", "session-start"], data, cwd, VG_SESSION_START_TIMEOUT_S, environ
        )
        err.write(stderr)
        if code == 0:
            out.write(stdout)
            return
        err.write(f"{PREFIX}vg hook session-start ended with {code}; using recall.sh.\n".encode())
    recall = config_dir(environ) / "skills" / "project-memory" / "scripts" / "recall.sh"
    if not recall.is_file():
        err.write(f"{PREFIX}no recall.sh at {recall}; nothing to recall.\n".encode())
        return
    code, stdout, stderr = run_process(["/bin/sh", str(recall)], b"", cwd, RECALL_TIMEOUT_S, environ)
    err.write(stderr)
    out.write(stdout)


def capture(data: bytes, environ: Mapping[str, str], out, err) -> None:
    vg = find_vg(environ)
    if vg is not None:
        code, stdout, stderr = run_process(
            [str(vg), "hook", "capture"], data, process_cwd(data), VG_CAPTURE_TIMEOUT_S, environ
        )
        err.write(stdout)
        err.write(stderr)
        if code == 0:
            return
        err.write(f"{PREFIX}vg hook capture ended with {code}; capturing directly.\n".encode())
    capture_direct(data, environ, out, err)


def load_router(here: Path | None = None) -> ModuleType:
    """Load the llm-wiki router from this folder (installed) or the sibling asset folder (source checkout)."""
    loaded = sys.modules.get(ROUTER_MODULE)
    if loaded is not None:
        return loaded
    here = here or Path(__file__).resolve().parent
    candidates = [here / ROUTER_FILE, here.parent / "llm-wiki-router" / ROUTER_FILE]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError(f"no {ROUTER_FILE} next to {here}; run install again.")
    spec = importlib.util.spec_from_file_location(ROUTER_MODULE, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {source}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[ROUTER_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(ROUTER_MODULE, None)
        raise
    return module


def contract_problem(vault_root: Path) -> str | None:
    """Same check as _contract_problem in session_capture.py: the manifest contract_version, read as plain JSON."""
    manifest = vault_root / MANIFEST_RELATIVE
    if not manifest.is_file():
        return f"has no {MANIFEST_RELATIVE.as_posix()}"
    try:
        data = json.loads(manifest.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return f"has an unreadable {MANIFEST_RELATIVE.as_posix()}"
    version = data.get("contract_version") if isinstance(data, dict) else None
    if type(version) is not int:
        return "manifest has no integer contract_version"
    if version < MIN_CONTRACT_VERSION:
        return f"manifest is contract {version}"
    return None


def select_vault(cwd: Path, environ: Mapping[str, str]) -> tuple[str, Path] | str:
    """Return (vault name, vault folder) or a one-line reason nothing is chosen.

    Same rule as select_target_vault in session_capture.py with no --vault: the one role project
    vault linked to the cwd project, with contract MIN_CONTRACT_VERSION or later.
    """
    router = load_router()
    map_path = config_dir(environ) / "vault-builder" / "vault-map.json"
    if not map_path.is_file():
        return f"No vault map at {map_path}; nothing written."
    try:
        data = router.read_vault_map(map_path)
    except router.VaultMapError as exc:
        return f"{exc} Nothing written."
    vaults = data["vaults"]
    for project_id, entry in data["projects"].items():
        unknown = [name for name in entry["vaults"] if name not in vaults]
        if unknown:
            return f"Vault map {map_path}: project {project_id} links unknown vaults: {', '.join(unknown)}."
    found = router.find_project(cwd, data)
    if isinstance(found, router.Stop):
        return f"{found.reason} Nothing written."
    names = [name for name in data["projects"][found.project_id]["vaults"] if vaults[name]["role"] == PROJECT_ROLE]
    if not names:
        return f"Project {found.project_id} has no role project vault; nothing written."
    if len(names) > 1:
        return f"Project {found.project_id} has {len(names)} role project vaults ({', '.join(names)}); nothing written."
    name = names[0]
    path = Path(vaults[name]["path"]).expanduser()
    problem = contract_problem(path)
    if problem is not None:
        return f"Vault {name} {problem}; run the contract migration for that vault first. Nothing written."
    return name, path


def _skipped_json(message: str) -> bytes:
    result = {
        "status": "skipped",
        "message": message,
        "vault": None,
        "path": None,
        "turn_count": 0,
        "redactions": 0,
        "warnings": [],
    }
    return (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")


def capture_direct(data: bytes, environ: Mapping[str, str], out, err) -> None:
    """Run the chosen vault's sessions capture with the hook JSON. stdout gets one result JSON line."""
    try:
        chosen = select_vault(hook_cwd(data), environ)
    except Exception as exc:  # The hook must still exit 0 when the router cannot load.
        chosen = f"vault choice failed: {type(exc).__name__}: {exc}"
    if isinstance(chosen, str):
        out.write(_skipped_json(chosen))
        err.write(f"{PREFIX}{chosen}\n".encode())
        return
    name, root = chosen
    cli = root / "tools" / "wiki" / "cli.py"
    if not cli.is_file():
        message = f"Vault {name} has no tools/wiki/cli.py; nothing written."
        out.write(_skipped_json(message))
        err.write(f"{PREFIX}{message}\n".encode())
        return
    command = [sys.executable, "-I", "-B", str(cli), "sessions", "capture", "--json"]
    code, stdout, stderr = run_process(command, data, root, DIRECT_CAPTURE_TIMEOUT_S, environ)
    err.write(stderr)
    if code is None or not stdout.strip():
        message = f"Vault {name} sessions capture ended with {code} and no result."
        out.write(_skipped_json(message))
        err.write(f"{PREFIX}{message}\n".encode())
        return
    out.write(stdout)


def run(argv: list[str], data: bytes, environ: Mapping[str, str], out, err) -> int:
    """Run one subcommand. Always returns 0."""
    try:
        if argv == ["session-start"]:
            session_start(data, environ, out, err)
        elif argv == ["capture"]:
            capture(data, environ, out, err)
        elif argv == ["capture", "--direct"]:
            capture_direct(data, environ, out, err)
        else:
            err.write(f"{USAGE}\n".encode())
    except Exception as exc:  # A hook must never block the Claude Code session.
        err.write(f"{PREFIX}{argv[0] if argv else 'run'} failed: {type(exc).__name__}: {exc}\n".encode())
    return 0


def main() -> int:
    try:
        data = sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        data = b""
    run(sys.argv[1:], data, os.environ, sys.stdout.buffer, sys.stderr.buffer)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
