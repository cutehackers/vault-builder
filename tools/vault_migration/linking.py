"""link, unlink, and list: edit which vaults a repository reaches (spec 4.4).

The project lookup rule lives in the router source file. This module loads
that file by path and calls its find_project, so the builder, the router, and
the installed copy use one rule. Only the link config is written. The MCP
settings file is never read or written here.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from tools.vault_migration.installing import _atomic_write, _json_bytes, resolve_user_paths

_ROUTER_PATH = Path(__file__).resolve().parent / "assets" / "llm-wiki-router" / "llm_wiki_router.py"
_ROUTER_MODULE = "vault_builder_llm_wiki_router"
_VAULT_MAP_VERSION = 2


def _router() -> ModuleType:
    loaded = sys.modules.get(_ROUTER_MODULE)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_ROUTER_MODULE, _ROUTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load the router source {_ROUTER_PATH}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ROUTER_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_ROUTER_MODULE, None)
        raise
    return module


class _Refused(Exception):
    """The command stops with exit 1. The message is one line for stderr."""


def _load(environ: Mapping[str, str]) -> tuple[Path, dict]:
    path = resolve_user_paths(environ).vault_map
    router = _router()
    try:
        return path, router.read_vault_map(path)
    except router.VaultMapError as exc:
        raise _Refused(str(exc)) from None


def _repo(repo: str | None) -> Path:
    return Path(repo).expanduser().absolute() if repo else Path.cwd()


def _write(path: Path, data: dict, dry_run: bool, command: str) -> None:
    """Write the link config as version 2 when its bytes change."""
    data["version"] = _VAULT_MAP_VERSION
    contents = _json_bytes(data, indent=2)
    if path.read_bytes() == contents:
        print(f"{command}: link config unchanged: {path}")
        return
    if dry_run:
        print(f"{command}: dry run, would write {path}")
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    _atomic_write(path, contents, mode)
    print(f"{command}: wrote {path}")


def link(
    vault: str,
    *,
    repo: str | None = None,
    project: str | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] = os.environ,
) -> int:
    try:
        path, data = _load(environ)
        router = _router()
        vaults = data["vaults"]
        if vault not in vaults:
            names = ", ".join(sorted(vaults)) or "none"
            raise _Refused(f"Vault {vault} is not in the link config. Vaults: {names}.")
        start = _repo(repo)
        root = router.main_worktree_root(start)
        if root is None:
            raise _Refused(f"{start} is not inside a git repository.")
        origin = router.origin_url(root)
        projects = data["projects"]
        match = router.find_project(start, data)
        if isinstance(match, router.Stop):
            if match.ids:
                raise _Refused(f"{match.reason} Fix the link config so one project matches.")
            project_id = project or root.name
            if project_id in projects:
                raise _Refused(
                    f"Project {project_id} already belongs to another repository ({projects[project_id]['root']}). "
                    "Choose another id with --project."
                )
            entry: dict[str, object] = {"root": str(root), "vaults": [vault]}
            if origin is not None:
                entry["remote"] = origin
            projects[project_id] = entry
            action = "created"
        else:
            project_id = match.project_id
            if project is not None and project != project_id:
                raise _Refused(
                    f"This repository is already linked as project {project_id} (found by {match.stage}). "
                    f"Drop --project or use --project {project_id}."
                )
            entry = projects[project_id]
            if vault not in entry["vaults"]:
                entry["vaults"].append(vault)
            entry["root"] = str(root)
            if origin is not None:
                entry["remote"] = origin
            action = f"found by {match.stage}"
        print(f"link: project {project_id} ({action}) vaults: {', '.join(entry['vaults'])}")
        _write(path, data, dry_run, "link")
        return 0
    except _Refused as exc:
        print(f"link: {exc}", file=sys.stderr)
        return 1


def unlink(
    vault: str,
    *,
    repo: str | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] = os.environ,
) -> int:
    try:
        path, data = _load(environ)
        router = _router()
        match = router.find_project(_repo(repo), data)
        if isinstance(match, router.Stop):
            if match.ids:
                raise _Refused(f"{match.reason} Fix the link config so one project matches.")
            print(f"unlink: {match.reason} Nothing to do.")
            return 0
        entry = data["projects"][match.project_id]
        if vault not in entry["vaults"]:
            print(f"unlink: project {match.project_id} is not linked to vault {vault}. Nothing to do.")
            return 0
        entry["vaults"] = [name for name in entry["vaults"] if name != vault]
        if entry["vaults"]:
            print(f"unlink: project {match.project_id} vaults: {', '.join(entry['vaults'])}")
        else:
            del data["projects"][match.project_id]
            print(f"unlink: project {match.project_id} has no vaults left and was removed.")
        _write(path, data, dry_run, "unlink")
        return 0
    except _Refused as exc:
        print(f"unlink: {exc}", file=sys.stderr)
        return 1


def list_links(*, repo: str | None = None, environ: Mapping[str, str] = os.environ) -> int:
    try:
        path, data = _load(environ)
    except _Refused as exc:
        print(f"list: {exc}", file=sys.stderr)
        return 1
    router = _router()
    print(f"Link config: {path} (version {data['version']})")
    print("Vaults:")
    for name, entry in sorted(data["vaults"].items()):
        exists = "exists" if Path(entry["path"]).expanduser().is_dir() else "missing"
        print(f"  {name}  role={entry['role']}  path={entry['path']}  ({exists})")
    print("Projects:")
    for project_id, entry in sorted(data["projects"].items()):
        remote = entry.get("remote", "-")
        print(f"  {project_id}  root={entry['root']}  remote={remote}  vaults={', '.join(entry['vaults']) or '-'}")
    start = _repo(repo)
    match = router.find_project(start, data)
    if isinstance(match, router.Stop) and match.ids:
        print(f"Repository {start}: no single project. {match.reason}")
    elif isinstance(match, router.Stop):
        print(f"Repository {start}: not linked. {match.reason}")
    else:
        print(f"Repository {start}: linked to project {match.project_id} by {match.stage}.")
        if match.note:
            print(match.note)
    return 0
