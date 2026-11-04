"""Uninstall a Vault's builder files and check an installed Vault with doctor.

uninstall never deletes knowledge (raw/, wiki/, scratch/, .git) or files the manifest does not
list, and never commits. Inside the Vault it deletes managed files whose bytes still match the
manifest, except .gitignore, then a .mcp.json of the previous builder form and the manifest.
Outside the Vault (schema 2 only) it removes the Vault from the vault map (and every project
whose vault list becomes empty). When no Vault remains it also deletes the vault map, the shared
files (skill, recall script, router), the mcpServers.vault-wiki server entry (and an mcpServers.llm-wiki
entry of ours left from before the rename), and the folders
those files leave empty. The mcpServers key stays.

With the last Vault it also undoes install --with-hooks: from the Claude Code settings file (hooks.py) it removes only
hook items whose command contains the launcher path (hooks.py), and it deletes the launcher when it equals
the asset and vault-hooks.json when it has the form install writes. A settings file that is not valid JSON
is left unchanged with a note.

uninstall --all runs uninstall for every Vault in the vault map, then deletes the marked vault-builder
command file and the default builder checkout it records (builder_command.py).

A shared `file` record counts as builder-owned when the file matches the recorded hash or the
current builder asset form that install also accepts: SKILL.md and the router equal to the asset,
recall.sh equal to the asset except its single PYTHON= line. The server entry counts as
builder-owned when its normalized hash matches the record or it has the form install writes
(an absolute command and args ["-I", <router>]). A later install from another interpreter
rewrites recall.sh and the server entry, so the first Vault's recorded hashes can go stale.

doctor reports problems and exits 1 when it finds any. It writes no file. Each problem line ends with
one fix command. Without a path it checks every vault in the vault map and the shared items once. It
starts the server entry and checks the initialize and tools/list answers, and it checks the hook
entries, the launcher, the recorded vg (vg --help), mcpServers.vault-graph and the vault-builder command.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from tools.vault_migration.versioning import read_version, version_label
from tools.wiki.lib.vault_manifest import MANIFEST_SCHEMA_VERSION, normalized_json_sha256

from . import builder_command as _builder_command
from . import hooks as _hooks
from .catalogs import _safe_relative_path, sha256_file
from .durability import fsync_directory
from .installing import (
    LEGACY_MCP_SERVER_KEY,
    MCP_ENTRY_PREFIX,
    MCP_SERVER_KEY,
    ROOT,
    SERVER_KEY_PATHS,
    InstallError,
    UserPaths,
    _ASSET_ROOT,
    _OutsideChanges,
    _PATCH_VERSION,
    _PREVIOUS_VERSION_HINT,
    _ROUTER_ASSET,
    _VAULT_MAP_READ_VERSIONS,
    _VAULT_MAP_VERSION,
    _vault_map_version_ok,
    _absolute_target,
    _entry_server,
    _json_bytes,
    _load_json_object,
    _recall_is_known,
    _same_path,
    choose_interpreter,
    is_legacy_entry,
    require_python,
    resolve_user_paths,
)
from .manifests import load_manifest
from .models import VaultManifest


_KEEP_INSIDE = frozenset({".gitignore"})
_MINIMUM_GIT = (2, 31)
_PYTHON_CHECK = "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
_SERVER_KEY_PATH = f"mcpServers.{MCP_SERVER_KEY}"
_START_TIMEOUT_SECONDS = 30


class UninstallError(InstallError):
    """Uninstall refused before writing anything; the message says why."""


# Shared helpers.


def _records(manifest: VaultManifest, kind: str) -> list[Mapping[str, object]]:
    return [record for record in manifest.external_records if record["kind"] == kind]


def _record_path(home: Path, record: Mapping[str, object]) -> Path:
    return home / str(record["location"])


def _record_problems(manifest: VaultManifest, user: UserPaths) -> list[str]:
    """Describe each outside record whose location or key is not one install writes."""
    problems = [_record_problem(record, user) for record in manifest.external_records]
    return [problem for problem in problems if problem]


def _old_entry_records(manifest: VaultManifest) -> list[str]:
    """Return the key paths of mcpServers.llm-wiki-<name> records written by the previous version."""
    return [
        str(record["key_path"])
        for record in manifest.external_records
        if record["kind"] == "json-entry" and str(record["key_path"]).startswith("mcpServers." + MCP_ENTRY_PREFIX)
    ]


def _record_problem(record: Mapping[str, object], user: UserPaths) -> str:
    """Return why a record is unexpected, or "" when install writes exactly this location.

    The manifest is tracked by git and editable, so uninstall and doctor accept only the paths
    install resolves from HOME and CLAUDE_CONFIG_DIR: the skill, the recall script, the router,
    the vault map, and the shared mcpServers.vault-wiki entry in the MCP settings file. A record of the
    previous name mcpServers.llm-wiki is accepted: install renames it on its next run.
    """
    kind, location = str(record["kind"]), str(record["location"])
    if kind == "file":
        files = sorted({user.relative(user.skill), user.relative(user.recall), user.relative(user.router)})
        if location not in files:
            return f"Unexpected file record {location}: expected {' or '.join(files)}."
    elif kind == "config":
        vault_map = user.relative(user.vault_map)
        if location != vault_map:
            return f"Unexpected config record {location}: expected {vault_map}."
    elif kind == "json-entry":
        mcp_config = user.relative(user.mcp_config)
        key_path = str(record["key_path"])
        if key_path.startswith("mcpServers." + MCP_ENTRY_PREFIX):
            return f"The vault was installed by the previous vault-builder version ({key_path}). {_PREVIOUS_VERSION_HINT}"
        if location != mcp_config or key_path not in SERVER_KEY_PATHS or record.get("shared") is not True:
            return (
                f"Unexpected json-entry record {location} {key_path}: "
                f"expected {mcp_config} {_SERVER_KEY_PATH} with shared true."
            )
    else:
        return f"Unexpected {kind} record {location}."
    return ""


def _is_skill(path: Path) -> bool:
    return path.name == "SKILL.md" and path.parent.name == "project-memory"


def _is_recall(path: Path) -> bool:
    return path.name == "recall.sh" and path.parent.name == "scripts"


def _is_router(path: Path) -> bool:
    return path.name == _ROUTER_ASSET.name and path.parent.name == "vault-builder"


def _builder_owned(path: Path, recorded_sha256: str | None, builder_root: Path) -> bool:
    """True when a regular file matches its record or the current builder asset form."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
    except FileNotFoundError:
        return False
    if recorded_sha256 is not None and sha256_file(path) == recorded_sha256:
        return True
    current = path.read_bytes()
    if _is_skill(path):
        return current == (builder_root / _ASSET_ROOT / "SKILL.md").read_bytes()
    if _is_recall(path):
        return _recall_is_known(builder_root)(current)
    if _is_router(path):
        return current == (builder_root / _ROUTER_ASSET).read_bytes()
    return False


def _install_form(entry: object, router: Path) -> bool:
    """True when the server entry has exactly the form install writes."""
    return (
        isinstance(entry, dict)
        and set(entry) == {"command", "args"}
        and isinstance(entry["command"], str)
        and os.path.isabs(entry["command"])
        and entry["args"] == ["-I", str(router)]
    )


def _old_mcp_json(path: Path, target: Path) -> bool:
    """True when .mcp.json is the previous builder form: one llm-wiki server for this vault."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    if not isinstance(servers, dict) or list(servers) != [LEGACY_MCP_SERVER_KEY]:
        return False
    server = _entry_server(servers[LEGACY_MCP_SERVER_KEY])
    return server is not None and _same_path(server, target / "tools" / "wiki" / "mcp_server.py")


def _python_ok(command: str) -> bool:
    try:
        result = subprocess.run(
            [command, "-c", _PYTHON_CHECK], capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _remove_empty_directories(directories: list[Path]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue
        fsync_directory(directory.parent)


# uninstall.


@dataclass
class _UninstallPlan:
    target: Path
    schema_version: int
    delete_inside: list[str] = field(default_factory=list)
    kept_modified: list[str] = field(default_factory=list)
    missing_inside: list[str] = field(default_factory=list)
    delete_mcp_json: bool = False
    kept_mcp_json: bool = False
    config_dir: Path | None = None
    entry_path: Path | None = None
    entries: list[tuple[str, str]] = field(default_factory=list)  # (key, remove | keep | absent | shared)
    vault_map_path: Path | None = None
    vault_map: dict[str, object] | None = None
    vault_map_action: str = ""  # update | delete | absent
    removed_vaults: list[str] = field(default_factory=list)
    remaining_vaults: list[str] = field(default_factory=list)
    delete_outside: list[Path] = field(default_factory=list)
    kept_outside: list[Path] = field(default_factory=list)
    hooks_settings_path: Path | None = None  # set when the settings file has launcher hook items to remove
    hooks_launcher: Path | None = None
    hooks_removed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def last_vault(self) -> bool:
        return not self.remaining_vaults


def uninstall(
    target: Path,
    *,
    dry_run: bool = False,
    builder_root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    stream = out if out is not None else sys.stdout
    plan = _plan_uninstall(Path(target), builder_root, os.environ if environ is None else environ)
    if dry_run:
        _print_uninstall(plan, stream, dry_run=True)
        return 0
    if plan.schema_version >= 2:
        _uninstall_outside(plan)
    _uninstall_inside(plan)
    _print_uninstall(plan, stream, dry_run=False)
    return 0


def _plan_uninstall(target: Path, builder_root: Path, environ: Mapping[str, str]) -> _UninstallPlan:
    target = _absolute_target(target)
    if not target.is_dir():
        raise UninstallError(f"Target is not a directory: {target}")
    try:
        manifest = load_manifest(target)
    except ValueError as exc:
        raise UninstallError(f"Target has an invalid Vault builder manifest: {target}. {exc}") from exc
    if manifest is None:
        raise UninstallError(f"Target has no Vault builder manifest: {target}. Nothing to uninstall.")
    plan = _UninstallPlan(target=target, schema_version=manifest.schema_version)

    for relative, managed in sorted(manifest.managed_files.items()):
        if relative in _KEEP_INSIDE:
            continue
        try:
            path = _safe_relative_path(target, relative)
        except ValueError:
            plan.kept_modified.append(relative)
            continue
        if not path.exists():
            plan.missing_inside.append(relative)
        elif path.is_file() and sha256_file(path) == managed.sha256:
            plan.delete_inside.append(relative)
        else:
            plan.kept_modified.append(relative)
    mcp_json = target / ".mcp.json"
    if _old_mcp_json(mcp_json, target):
        plan.delete_mcp_json = True
    elif mcp_json.exists() or mcp_json.is_symlink():
        plan.kept_mcp_json = True

    if manifest.schema_version < 2:
        plan.notes.append(
            f"Manifest schema {manifest.schema_version} has no outside records: "
            "only files inside the vault were processed; files outside the vault were not touched."
        )
        return plan
    old_records = _old_entry_records(manifest)
    if old_records:
        raise UninstallError(
            f"Vault {target} was installed by the previous vault-builder version ({', '.join(old_records)}); "
            "nothing was written. Run uninstall with the previous vault-builder version."
        )
    user = resolve_user_paths(environ)
    problems = _record_problems(manifest, user)
    if problems:
        raise UninstallError(
            "The manifest lists files outside the vault that install does not write; nothing was written. "
            + " ".join(problems)
            + " Check HOME and CLAUDE_CONFIG_DIR match the install, or restore the manifest from git."
        )
    plan.config_dir = user.config_dir
    _plan_outside(plan, manifest, builder_root, user)
    return plan


def _plan_outside(plan: _UninstallPlan, manifest: VaultManifest, builder_root: Path, user: UserPaths) -> None:
    # Records were checked by _record_problems: the only json-entry is the shared server entry.
    home = user.home
    for record in _records(manifest, "config"):
        path = _record_path(home, record)
        plan.vault_map_path = path
        if not path.exists():
            plan.vault_map_action = "absent"
            continue
        vault_map = _load_json_object(path, "Vault map")
        vaults = vault_map.get("vaults")
        projects = vault_map.get("projects", {})
        if not _vault_map_version_ok(vault_map.get("version")) or not isinstance(vaults, dict):
            versions = " or ".join(str(version) for version in _VAULT_MAP_READ_VERSIONS)
            raise UninstallError(
                f"Vault map {path} must be a JSON object with version {versions} and a vaults object; "
                "nothing was written."
            )
        if not isinstance(projects, dict):
            raise UninstallError(f"Vault map {path}: projects must be a JSON object.")
        removed = []
        for vault_name, value in list(vaults.items()):
            vault_path = value.get("path") if isinstance(value, dict) else None
            if isinstance(vault_path, str) and _same_path(Path(vault_path).expanduser(), plan.target):
                removed.append(vault_name)
                del vaults[vault_name]
        for project_id, project in list(projects.items()):
            if not isinstance(project, dict) or not isinstance(project.get("vaults"), list):
                continue
            kept = [item for item in project["vaults"] if item not in removed]
            if len(kept) == len(project["vaults"]):
                continue
            if kept:
                project["vaults"] = kept
            else:
                del projects[project_id]  # decision 35: an empty project would still match a repository
        vault_map["version"] = _VAULT_MAP_VERSION
        plan.vault_map = vault_map
        plan.removed_vaults = removed
        plan.remaining_vaults = list(vaults)
        plan.vault_map_action = "update" if vaults else "delete"

    for record in _records(manifest, "json-entry"):
        path = _record_path(home, record)
        plan.entry_path = path
        config = _load_json_object(path, "MCP settings file")
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise UninstallError(f"MCP settings file {path}: mcpServers must be a JSON object.")
        if not plan.last_vault:
            action = "shared"
        elif MCP_SERVER_KEY not in servers:
            action = "absent"
        elif normalized_json_sha256(servers[MCP_SERVER_KEY]) == record["sha256"] or _install_form(
            servers[MCP_SERVER_KEY], user.router
        ):
            action = "remove"
        else:
            action = "keep"
        if action == "absent" and is_legacy_entry(servers.get(LEGACY_MCP_SERVER_KEY), user.router):
            plan.entries.append((LEGACY_MCP_SERVER_KEY, "remove"))  # installed before the rename
            continue
        plan.entries.append((MCP_SERVER_KEY, action))
        if plan.last_vault and is_legacy_entry(servers.get(LEGACY_MCP_SERVER_KEY), user.router):
            plan.entries.append((LEGACY_MCP_SERVER_KEY, "remove"))  # left from before the rename

    for record in _records(manifest, "file"):
        path = _record_path(home, record)
        if record["shared"] and not plan.last_vault:
            continue
        if not path.exists() and not path.is_symlink():
            plan.notes.append(f"Already absent: {path}")
        elif _builder_owned(path, str(record["sha256"]), builder_root):
            plan.delete_outside.append(path)
        else:
            plan.kept_outside.append(path)

    if plan.last_vault:
        _plan_hooks(plan, builder_root, user)


def _plan_hooks(plan: _UninstallPlan, builder_root: Path, user: UserPaths) -> None:
    """Plan the removal of what install --with-hooks wrote. The hooks are shared, so only the last Vault does it."""
    launcher = _hooks.launcher_path(user.config_dir)
    settings_path = _hooks.settings_path(user.config_dir)
    if settings_path.is_symlink() or (settings_path.exists() and not settings_path.is_file()):
        plan.notes.append(f"{settings_path} is not a regular file; hook entries that run {launcher} were not removed.")
    elif settings_path.exists():
        try:
            settings = _hooks.load_settings(settings_path)
        except _hooks.HooksError as exc:
            plan.notes.append(f"{exc} Hook entries that run {launcher} were not removed; remove them by hand.")
        else:
            _updated, removed = _hooks.remove_launcher_hooks(settings, launcher)
            if removed:
                plan.hooks_settings_path = settings_path
                plan.hooks_launcher = launcher
                plan.hooks_removed = removed
    if launcher.is_file() and not launcher.is_symlink():
        if launcher.read_bytes() == (builder_root / _hooks.LAUNCHER_ASSET).read_bytes():
            plan.delete_outside.append(launcher)
        else:
            plan.kept_outside.append(launcher)
    config = _hooks.hooks_config_path(user.config_dir)
    if config.is_file() and not config.is_symlink():
        if _hooks.is_hooks_config(config.read_bytes()):
            plan.delete_outside.append(config)
        else:
            plan.kept_outside.append(config)


def _uninstall_outside(plan: _UninstallPlan) -> None:
    changes = _OutsideChanges()
    try:
        removed = [key for key, action in plan.entries if action == "remove"]
        if removed and plan.entry_path is not None:
            # Re-read just before writing: Claude Code may have rewritten the file since planning.
            config = _load_json_object(plan.entry_path, "MCP settings file")
            servers = dict(config.get("mcpServers", {}))  # type: ignore[arg-type]
            for key in removed:
                servers.pop(key, None)
            config["mcpServers"] = servers
            mode = stat.S_IMODE(plan.entry_path.stat().st_mode)
            changes.write(plan.entry_path, _json_bytes(config, indent=2), mode)
        if plan.hooks_settings_path is not None and plan.hooks_launcher is not None:
            # Re-read just before writing, then remove only the launcher items (before the launcher goes).
            settings, removed_hooks = _hooks.remove_launcher_hooks(
                _hooks.load_settings(plan.hooks_settings_path), plan.hooks_launcher
            )
            if removed_hooks:
                mode = stat.S_IMODE(plan.hooks_settings_path.stat().st_mode)
                changes.write(plan.hooks_settings_path, _hooks.settings_bytes(settings), mode)
        if plan.vault_map_path is not None and plan.vault_map is not None:
            if plan.vault_map_action == "update":
                mode = stat.S_IMODE(plan.vault_map_path.stat().st_mode)
                changes.write(plan.vault_map_path, _json_bytes(plan.vault_map, indent=2), mode)
            elif plan.vault_map_action == "delete":
                changes.delete(plan.vault_map_path)
        for path in plan.delete_outside:
            changes.delete(path)
    except BaseException as exc:
        changes.rollback()
        if isinstance(exc, (OSError, ValueError)):
            raise UninstallError(f"Uninstall failed; outside files were restored and the vault was not changed: {exc}") from exc
        raise
    finally:
        changes.finish()
    if plan.last_vault and plan.config_dir is not None:
        # Spec 4.6: these folders hold only builder files; each goes only when it is empty.
        project_memory = plan.config_dir / "skills" / "project-memory"
        _remove_empty_directories([project_memory / "scripts", project_memory, plan.config_dir / "vault-builder"])


def _uninstall_inside(plan: _UninstallPlan) -> None:
    parents: set[Path] = set()
    for relative in plan.delete_inside:
        path = plan.target / relative
        path.unlink()
        parents.add(path.parent)
    if plan.delete_mcp_json:
        (plan.target / ".mcp.json").unlink()
    manifest_path = plan.target / ".vault-builder" / "manifest.json"
    manifest_path.unlink()  # last, so an interrupted run can be repeated
    parents.add(manifest_path.parent)
    fsync_directory(plan.target)
    directories: set[Path] = set()
    for parent in parents:
        current = parent
        while current != plan.target and plan.target in current.parents:
            directories.add(current)
            current = current.parent
    ordered = sorted(directories, key=lambda directory: len(directory.parts), reverse=True)
    _remove_empty_directories(ordered)


def _print_uninstall(plan: _UninstallPlan, stream: TextIO, *, dry_run: bool) -> None:
    verb = "delete" if dry_run else "deleted"
    print(f"{'Dry run: uninstall' if dry_run else 'Uninstalled'} {plan.target}", file=stream)
    print(f"  {verb} {len(plan.delete_inside)} unmodified managed files", file=stream)
    if plan.delete_mcp_json:
        print(f"  {verb} {plan.target / '.mcp.json'} (previous builder form)", file=stream)
    print(f"  {verb} {plan.target / '.vault-builder' / 'manifest.json'}", file=stream)
    if plan.kept_mcp_json:
        print(f"Kept .mcp.json (not the previous builder form): {plan.target / '.mcp.json'}", file=stream)
    print("  kept .gitignore, raw/, wiki/, scratch/, .git and files the manifest does not list", file=stream)
    for relative in plan.kept_modified:
        print(f"Kept modified file: {plan.target / relative}", file=stream)
    for entry_key, action in plan.entries:
        key = f"mcpServers.{entry_key}"
        if action == "remove":
            print(f"  {'remove' if dry_run else 'removed'} {key} from {plan.entry_path}", file=stream)
        elif action == "keep":
            print(
                f"Kept changed MCP entry (differs from the manifest record and the install form): {key} in {plan.entry_path}",
                file=stream,
            )
        elif action == "shared":
            print(f"  kept {key} in {plan.entry_path}: vaults remain", file=stream)
        else:
            print(f"Note: {key} was not found in {plan.entry_path}", file=stream)
    if plan.vault_map_path is not None:
        if plan.vault_map_action == "absent":
            print(f"Note: vault map {plan.vault_map_path} was not found", file=stream)
        else:
            removed = ", ".join(plan.removed_vaults) or "(none)"
            print(f"  {'remove' if dry_run else 'removed'} vault {removed} from {plan.vault_map_path}", file=stream)
            if plan.vault_map_action == "delete":
                print(f"  {verb} {plan.vault_map_path} (no vault remains)", file=stream)
            else:
                print(f"  kept shared files: vaults remain ({', '.join(plan.remaining_vaults)})", file=stream)
    if plan.hooks_settings_path is not None:
        print(
            f"  {'remove' if dry_run else 'removed'} {plan.hooks_removed} hook entries that run "
            f"{plan.hooks_launcher} from {plan.hooks_settings_path}",
            file=stream,
        )
    for path in plan.delete_outside:
        print(f"  {verb} {path}", file=stream)
    for path in plan.kept_outside:
        print(f"Kept file with unknown content: {path}", file=stream)
    for note in plan.notes:
        print(f"Note: {note}", file=stream)
    if dry_run:
        print("Nothing was written.", file=stream)
    else:
        print("Nothing was committed. Review the vault with git status.", file=stream)


# uninstall --all.


def uninstall_all(
    *,
    dry_run: bool = False,
    builder_root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    """Uninstall every Vault in the vault map, then delete the vault-builder command file and the builder checkout.

    Spec 4.8: each Vault goes through uninstall, so the last one deletes the shared files and the hooks, and
    raw/ and wiki/ stay. The first Vault that fails stops the run with UninstallError naming that Vault and
    the error; the command file and the checkout then stay. Spec 4.3 item 3: only a command file with the
    marker line is deleted, and only the checkout it records at the default place. A checkout the user chose
    with VAULT_BUILDER_HOME and any other path are kept. The default checkout is deleted even when this code
    runs from it, so that deletion is the very last action.
    """
    stream = out if out is not None else sys.stdout
    environment = os.environ if environ is None else environ
    user = resolve_user_paths(environment)
    for name, target in _all_vaults(user.vault_map, stream):
        try:
            uninstall(target, dry_run=dry_run, builder_root=builder_root, environ=environment, out=stream)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            raise UninstallError(
                f"Vault {name} ({target}) failed to uninstall; stopped before the remaining vaults, "
                f"the vault-builder command and the builder checkout: {exc}"
            ) from exc
    checkout = _remove_builder_command(user, environment, stream, dry_run=dry_run)
    if dry_run:
        if checkout is not None:
            print(f"  delete {checkout}", file=stream)
        print("Dry run: nothing was written.", file=stream)
        return 0
    if checkout is not None:
        _delete_checkout(checkout, stream)
    return 0


def _delete_checkout(checkout: Path, stream: TextIO) -> None:
    """The last action of uninstall --all.

    The checkout may be the one this code runs from, so every other line is printed and flushed first,
    and nothing is imported after rmtree starts: shutil, shlex and fsync_directory are loaded with this
    module. A failure keeps the checkout and is not an error (exit 0).
    """
    print(f"  deleting {checkout} (last step)", file=stream)
    stream.flush()
    try:
        shutil.rmtree(checkout)
        fsync_directory(checkout.parent)
    except OSError as exc:
        print(
            f"Kept the builder checkout {checkout}: {exc}. Remove it with: rm -rf {shlex.quote(str(checkout))}",
            file=stream,
        )
        stream.flush()


def _all_vaults(path: Path, stream: TextIO) -> list[tuple[str, Path]]:
    """The (name, path) of every vault in the vault map, once per path, in map order."""
    if not path.exists() and not path.is_symlink():
        print(f"Note: vault map {path} was not found; no vault to uninstall.", file=stream)
        return []
    vault_map = _load_json_object(path, "Vault map")
    vaults = vault_map.get("vaults")
    if not isinstance(vaults, dict):
        raise UninstallError(f"Vault map {path} must have a vaults object; nothing was written.")
    found: list[tuple[str, Path]] = []
    for name, value in vaults.items():
        raw = value.get("path") if isinstance(value, dict) else None
        if not isinstance(raw, str) or not os.path.isabs(os.path.expanduser(raw)):
            raise UninstallError(f"Vault {name} in {path} has no absolute path; nothing more was written.")
        target = Path(os.path.expanduser(raw))
        if not any(_same_path(target, seen) for _name, seen in found):
            found.append((name, target))
    return found


def _remove_builder_command(
    user: UserPaths, environment: Mapping[str, str], stream: TextIO, *, dry_run: bool
) -> Path | None:
    """Delete the marked command file. Return the recorded checkout when it may be deleted, else None."""
    verb = "delete" if dry_run else "deleted"
    path = _builder_command.command_path(user.home)
    if not path.exists() and not path.is_symlink():
        print(f"Note: {path} was not found; the vault-builder command is not installed.", file=stream)
        return None
    contents = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    if not _builder_command.has_marker(contents):
        print(f"Kept {path}: it has no vault-builder marker line, so it is not the vault-builder command.", file=stream)
        return None
    checkout = _builder_command.read_checkout(contents)
    if not dry_run:
        path.unlink()
        fsync_directory(path.parent)
    print(f"  {verb} {path}", file=stream)
    if checkout is None:
        print(f"Note: {path} recorded no builder checkout; no checkout was deleted.", file=stream)
        return None
    reason = _checkout_kept_reason(checkout, user.home, environment)
    if reason:
        print(f"Kept the builder checkout {checkout}: {reason}", file=stream)
        return None
    return checkout


def _checkout_kept_reason(checkout: Path, home: Path, environment: Mapping[str, str]) -> str:
    """Why the recorded checkout stays, or "" when it is the default checkout and may be deleted.

    The checkout this code runs from is not a reason to keep it (spec 4.3 item 3); _delete_checkout runs last.
    """
    chosen = environment.get("VAULT_BUILDER_HOME", "")
    if chosen and _same_path(Path(os.path.expanduser(chosen)), checkout):
        return "you chose it with VAULT_BUILDER_HOME."
    if checkout.is_symlink() or not _same_path(checkout, _builder_command.default_checkout(home)):
        return f"it is not the default checkout {_builder_command.default_checkout(home)}."
    if not (checkout / _builder_command.CLI_RELATIVE).is_file() or not (checkout / ".git").exists():
        return f"it is not a vault-builder git checkout (no {_builder_command.CLI_RELATIVE} or .git)."
    return ""


# doctor.

_SETUP = "vault-builder setup"
_VAULT_GRAPH_KEY = "vault-graph"
_VG_HELP_TIMEOUT_SECONDS: float = 10


@dataclass
class _DoctorReport:
    """Problems and notes. Each problem line ends with one fix command (spec 4.7 item 6)."""

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    prefix: str = ""  # names the vault while doctor checks one vault of several

    def problem(self, message: str, fix: str = _SETUP) -> None:
        self.problems.append(f"{self.prefix}{message} Fix: {fix}")

    def note(self, message: str) -> None:
        self.notes.append(f"{self.prefix}{message}")


def doctor(
    target: Path | None = None,
    *,
    builder_root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    """Report problems of one installed Vault, or of every Vault in the vault map when target is None.

    Shared items (skill, recall script, router, MCP entries, hooks, vg, the vault-builder command)
    are checked once. Writes no file; exits 1 when it finds a problem.
    """
    stream = out if out is not None else sys.stdout
    require_python(tuple(sys.version_info))
    environment = os.environ if environ is None else environ
    if target is not None:
        target = _absolute_target(Path(target))
        if not target.is_dir():
            raise InstallError(f"Target is not a directory: {target}")
    report = _DoctorReport()
    user = resolve_user_paths(environment)

    vault_map = _doctor_vault_map(report, user.vault_map)
    if target is not None:
        targets = [("", target)]
    else:
        targets = _doctor_targets(report, user.vault_map, vault_map)
    recorded: dict[Path, set[str]] = {}
    for name, vault in targets:
        report.prefix = f"{name} ({vault}): " if target is None else ""
        _doctor_vault(report, vault, user, builder_root, recorded)
        if target is not None and vault_map is not None and not _vault_map_names(vault_map, vault):
            report.problem(
                f"Vault map {user.vault_map} does not include this vault.", fix=f"vault-builder install {shlex.quote(str(vault))}"
            )
    report.prefix = ""

    for path in (user.skill, user.recall):
        if not path.exists():
            report.problem(f"Missing {path}.")
        elif not _owned_by_any(path, recorded.get(path, set()), builder_root):
            report.problem(f"{path} differs from the manifest record and the builder asset.")
    if user.recall.is_file():
        _doctor_recall(report, user.recall, environment)
    if not user.router.exists():
        report.problem(f"Missing router {user.router}.")
    elif not _owned_by_any(user.router, recorded.get(user.router, set()), builder_root):
        report.problem(f"Router {user.router} differs from the manifest record and the builder asset.")

    entry = _doctor_mcp_entry(report, user, environment)
    if entry is not None:
        _doctor_server(report, entry, targets[0][1] if targets else user.home, builder_root, environment)
    if environment.get("CLAUDE_CONFIG_DIR"):
        _doctor_claude_mcp_list(report, MCP_SERVER_KEY, user.mcp_config, environment)
    _doctor_git(report)
    _doctor_hooks(report, user, builder_root, environment)
    _doctor_vault_graph_entry(report, user, environment)
    _doctor_builder_command(report, user, builder_root)

    if target is not None:
        scope = str(target)
    else:
        scope = f"{len(targets)} vaults ({', '.join(name for name, _vault in targets) or 'none'})"
    print(version_label(builder_root), file=stream)
    for note in report.notes:
        print(f"Note: {note}", file=stream)
    for problem in report.problems:
        print(f"Problem: {problem}", file=stream)
    if report.problems:
        print(f"Doctor found {len(report.problems)} problems in {scope}.", file=stream)
        return 1
    print(f"Doctor found no problems in {scope}.", file=stream)
    return 0


def _doctor_vault(
    report: _DoctorReport, target: Path, user: UserPaths, builder_root: Path, recorded: dict[Path, set[str]]
) -> None:
    """The checks of one vault. Adds the recorded hashes of its shared file records to recorded."""
    quoted = shlex.quote(str(target))
    restore_manifest = f"git -C {quoted} checkout -- .vault-builder/manifest.json"
    manifest: VaultManifest | None = None
    try:
        manifest = load_manifest(target)
    except ValueError as exc:
        report.problem(f"Manifest is invalid: {exc}", fix=restore_manifest)
    else:
        if manifest is None:
            report.problem(f"No Vault builder manifest in {target}.", fix=f"vault-builder install {quoted}")
    builder_version = read_version(builder_root)
    if manifest is not None and manifest.builder_revision != builder_version:
        report.note(
            f"This vault was installed or migrated by builder revision {manifest.builder_revision}; "
            f"this checkout is {builder_version}. Run audit and apply to bring the vault up to date."
        )
    elif manifest is not None and manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        report.problem(
            f"Manifest schema is {manifest.schema_version}, not {MANIFEST_SCHEMA_VERSION}. "
            "Run audit and apply, then install.",
            fix=f"vault-builder audit --target {quoted}",
        )
    if manifest is not None:
        modified = sorted(
            relative
            for relative, managed in manifest.managed_files.items()
            if not (target / relative).is_file() or sha256_file(target / relative) != managed.sha256
        )
        if modified:
            report.problem(
                f"{len(modified)} managed files differ from the manifest: " + ", ".join(modified),
                fix=f"vault-builder audit --target {quoted}",
            )
        # An unexpected record is a problem and its location is not used.
        for problem in _record_problems(manifest, user):
            report.problem(problem, fix=_SETUP if _PREVIOUS_VERSION_HINT in problem else restore_manifest)
        for record in manifest.external_records:
            if record["kind"] == "file" and not _record_problem(record, user):
                recorded.setdefault(_record_path(user.home, record), set()).add(str(record["sha256"]))
    mcp_json = target / ".mcp.json"
    if mcp_json.exists() or mcp_json.is_symlink():
        report.problem(
            f"{mcp_json} is left from the previous vault-builder version; the server is the user-scope "
            f"mcpServers.{MCP_SERVER_KEY} entry now. Run uninstall with the previous vault-builder version, "
            "or delete the file if you do not use it.",
            fix=f"rm {shlex.quote(str(mcp_json))}",
        )


def _owned_by_any(path: Path, recorded: set[str], builder_root: Path) -> bool:
    """True when the file matches one vault's record or the current builder asset form."""
    return any(_builder_owned(path, sha256, builder_root) for sha256 in recorded) or _builder_owned(
        path, None, builder_root
    )


def _vault_map_names(vault_map: Mapping[str, object], target: Path) -> list[str]:
    vaults = vault_map.get("vaults", {})
    return [
        vault_name
        for vault_name, value in (vaults.items() if isinstance(vaults, dict) else ())
        if isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and _same_path(Path(value["path"]).expanduser(), target)
    ]


def _doctor_vault_map(report: _DoctorReport, path: Path) -> dict[str, object] | None:
    if not path.exists():
        report.problem(f"Missing vault map {path}.")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report.problem(f"Vault map {path} is not valid JSON: {exc}", fix=f"python3 -m json.tool {shlex.quote(str(path))}")
        return None
    if not isinstance(value, dict) or not isinstance(value.get("vaults", {}), dict):
        report.problem(f"Vault map {path} must be a JSON object with a vaults object.")
        return None
    return value


def _doctor_targets(
    report: _DoctorReport, path: Path, vault_map: Mapping[str, object] | None
) -> list[tuple[str, Path]]:
    """The (name, path) of every vault in the vault map whose path is a directory."""
    if vault_map is None:
        return []
    vaults = vault_map.get("vaults", {})
    assert isinstance(vaults, dict)  # checked by _doctor_vault_map
    targets: list[tuple[str, Path]] = []
    for name, value in vaults.items():
        raw = value.get("path") if isinstance(value, dict) else None
        if not isinstance(raw, str) or not os.path.isabs(os.path.expanduser(raw)):
            report.problem(f"Vault {name} in {path} has no absolute path.", fix=f"vault-builder install <PATH> --name {name}")
            continue
        vault = Path(os.path.expanduser(raw))
        if not vault.is_dir():
            report.problem(
                f"Vault {name} in {path} points at {vault}, which is not a directory.",
                fix=f"vault-builder install {shlex.quote(str(vault))} --name {name}",
            )
            continue
        targets.append((name, _absolute_target(vault)))
    if not vaults:
        report.problem(f"Vault map {path} has no vaults.")
    return targets


def _doctor_recall(report: _DoctorReport, recall: Path, environment: Mapping[str, str]) -> None:
    if not os.access(recall, os.X_OK):
        report.problem(f"{recall} is not executable.")
        return
    lines = [line for line in recall.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("PYTHON=")]
    try:
        words = shlex.split(lines[0][len("PYTHON="):]) if len(lines) == 1 else []
    except ValueError:
        words = []
    if len(words) != 1 or not _python_ok(words[0]):
        report.problem(f"The interpreter in {recall} does not run Python 3.10 or newer.")
        return
    with tempfile.TemporaryDirectory(prefix="vault-doctor-") as scratch:
        outside = Path(scratch).resolve()
        try:
            result = subprocess.run(
                [str(recall)],
                cwd=outside,
                env={**environment, "GIT_CEILING_DIRECTORIES": str(outside.parent)},
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            report.problem(f"{recall} failed outside a git repository: {exc}")
            return
    if result.returncode != 0:
        report.problem(
            f"{recall} exited {result.returncode} outside a git repository: {(result.stdout + result.stderr).strip()}"
        )


def _doctor_mcp_entry(
    report: _DoctorReport, user: UserPaths, environment: Mapping[str, str]
) -> dict[str, object] | None:
    """Check the mcpServers.vault-wiki entry. Return the server entry when it can be started."""
    path = user.mcp_config
    try:
        config = _load_json_object(path, "MCP settings file")
    except InstallError as exc:
        report.problem(str(exc), fix=f"python3 -m json.tool {shlex.quote(str(path))}")
        return None
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        report.problem(f"MCP settings file {path}: mcpServers must be a JSON object.")
        return None
    old_keys = sorted(key for key in servers if key.startswith(MCP_ENTRY_PREFIX))
    if old_keys:
        report.problem(
            f"MCP settings file {path} has entries from the previous vault-builder version: "
            + ", ".join(f"mcpServers.{key}" for key in old_keys)
            + f". {_PREVIOUS_VERSION_HINT}"
        )
    if is_legacy_entry(servers.get(LEGACY_MCP_SERVER_KEY), user.router):
        report.problem(
            f"mcpServers.{LEGACY_MCP_SERVER_KEY} in {path} runs this router under the name used before "
            f"{MCP_SERVER_KEY}; install renames it."
        )
    entry = servers.get(MCP_SERVER_KEY)
    expected_args = ["-I", str(user.router)]
    if entry is None:
        report.problem(f"{_SERVER_KEY_PATH} is missing from {path}.")
        return None
    if not isinstance(entry, dict) or entry.get("args") != expected_args:
        report.problem(f"{_SERVER_KEY_PATH} in {path} does not have args {json.dumps(expected_args)}.")
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not os.path.isabs(command) or not _python_ok(command):
        report.problem(f"{_SERVER_KEY_PATH} command {command!r} in {path} does not run Python 3.10 or newer.")
        return None
    if _PATCH_VERSION.search(command):
        chosen, note = choose_interpreter(command, environment)
        if note is None and chosen != command:
            report.note(
                f"{_SERVER_KEY_PATH} command {command} names a Python patch version; {chosen} runs the same "
                "Python without one. Run install again to use it."
            )
    return entry


def _doctor_server(
    report: _DoctorReport, entry: Mapping[str, object], cwd: Path, builder_root: Path, environment: Mapping[str, str]
) -> None:
    """Start the server entry and check its initialize and tools/list answers."""
    command = [str(entry["command"]), *[str(argument) for argument in entry["args"]]]  # type: ignore[union-attr]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "vault-builder-doctor", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(request) + "\n" for request in requests)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        report.problem(f"Could not start {_SERVER_KEY_PATH} ({shlex.join(command)}): {exc}")
        return
    try:
        stdout, stderr = process.communicate(stdin, timeout=_START_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop(process)
        report.problem(
            f"{_SERVER_KEY_PATH} ({shlex.join(command)}) did not answer initialize and tools/list "
            f"within {_START_TIMEOUT_SECONDS} seconds; it was stopped."
        )
        return
    answers: dict[object, object] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and "id" in message:
            answers[message["id"]] = message.get("result")
    first_error = next((line for line in stderr.splitlines() if line.strip()), "")
    suffix = f" First stderr line: {first_error}" if first_error else ""
    initialize = answers.get(1)
    server_info = initialize.get("serverInfo") if isinstance(initialize, dict) else None
    name = server_info.get("name") if isinstance(server_info, dict) else None
    if name != MCP_SERVER_KEY:
        report.problem(
            f"{_SERVER_KEY_PATH} did not answer initialize with serverInfo.name {MCP_SERVER_KEY} "
            f"(got {name!r}, exit {process.returncode}).{suffix}"
        )
        return
    listing = answers.get(2)
    tools = listing.get("tools") if isinstance(listing, dict) else None
    found = sorted(tool.get("name") for tool in tools if isinstance(tool, dict)) if isinstance(tools, list) else None
    expected = sorted(tool["name"] for tool in _router_module(builder_root).tool_list())
    if found != expected:
        report.problem(
            f"{_SERVER_KEY_PATH} tools/list did not return the {len(expected)} router tools "
            f"(got {len(found) if found is not None else 'no list'}).{suffix}"
        )


def _stop(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _router_module(builder_root: Path):  # noqa: ANN202 - a module loaded from the router asset
    spec = importlib.util.spec_from_file_location("vault_builder_doctor_router", builder_root / _ROUTER_ASSET)
    if spec is None or spec.loader is None:
        raise InstallError(f"Cannot load the router asset {builder_root / _ROUTER_ASSET}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doctor_claude_mcp_list(report: _DoctorReport, key: str, path: Path, environment: Mapping[str, str]) -> None:
    # CLAUDE_CONFIG_DIR may or may not move .claude.json (spec risk 14); ask Claude Code itself.
    claude = shutil.which("claude", path=environment.get("PATH"))
    if claude is None:
        report.note(
            f"CLAUDE_CONFIG_DIR is set and claude is not on PATH: could not confirm that Claude Code reads {path}."
        )
        return
    try:
        result = subprocess.run(
            [claude, "mcp", "list"], env=dict(environment), capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.note(f"claude mcp list failed: {exc}")
        return
    if not re.search(rf"(^|\s){re.escape(key)}:", result.stdout, flags=re.MULTILINE):
        report.problem(
            f"claude mcp list does not show {key}. With CLAUDE_CONFIG_DIR set, Claude Code may read "
            f"a different settings file than {path}.",
            fix="claude mcp list",
        )


def _doctor_git(report: _DoctorReport) -> None:
    fix = "install git 2.31 or newer (macOS: brew install git)"
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        report.problem("git is not available; recall.sh needs git 2.31 or newer.", fix=fix)
        return
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if result.returncode != 0 or match is None:
        report.problem("Could not read the git version; recall.sh needs git 2.31 or newer.", fix=fix)
    elif (int(match.group(1)), int(match.group(2))) < _MINIMUM_GIT:
        report.problem(f"git {match.group(0)} is older than 2.31; recall.sh needs git 2.31 or newer.", fix=fix)


def _doctor_hooks(report: _DoctorReport, user: UserPaths, builder_root: Path, environment: Mapping[str, str]) -> None:
    """Spec 4.7 items 1-4: the launcher hook entries, the launcher file, and the recorded vg."""
    launcher = _hooks.launcher_path(user.config_dir)
    settings_path = _hooks.settings_path(user.config_dir)
    config_path = _hooks.hooks_config_path(user.config_dir)
    counts: dict[str, int] | None = None
    try:
        settings = _hooks.load_settings(settings_path)
    except _hooks.HooksError as exc:
        report.problem(str(exc), fix=f"python3 -m json.tool {shlex.quote(str(settings_path))}")
    else:
        hooks = settings.get("hooks", {})
        assert isinstance(hooks, dict)  # checked by load_settings
        counts = {
            event: sum(
                1
                for group in hooks.get(event, [])
                for item in (group.get("hooks") if isinstance(group, dict) and isinstance(group.get("hooks"), list) else [])
                if _hooks._is_launcher_item(item, launcher)
            )
            for event, _subcommand, _timeout in _hooks.EVENTS
        }
    installed = counts is None or any(counts.values()) or launcher.exists() or config_path.exists()
    if not installed:
        report.note(
            f"The SessionStart, PreCompact and SessionEnd hooks are not installed ({settings_path} has no entry "
            f"that runs {launcher}). To add them, run {_SETUP}."
        )
        return
    for event, count in (counts or {}).items():
        if count == 0:
            report.problem(f"{settings_path} has no hooks.{event} entry that runs {launcher}.")
        elif count > 1:
            report.problem(f"{settings_path} has {count} hooks.{event} entries that run {launcher}; expected 1.")
    asset = builder_root / _hooks.LAUNCHER_ASSET
    if not launcher.is_file():
        report.problem(f"Missing hook launcher {launcher}.")
    elif launcher.read_bytes() != asset.read_bytes():
        report.problem(f"Hook launcher {launcher} differs from the builder asset {asset}.")
    _doctor_hooks_vg(report, config_path, environment)


def _doctor_hooks_vg(report: _DoctorReport, config_path: Path, environment: Mapping[str, str]) -> None:
    without_vg = (
        f"{config_path} records no vg path, so the hooks use recall.sh and direct capture "
        f"(an install without vault-graph). To use vault-graph, run {_SETUP}."
    )
    if not config_path.exists():
        report.note(without_vg)
        return
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report.problem(f"{config_path} is not valid JSON: {exc}")
        return
    if not isinstance(value, dict):
        report.problem(f"{config_path} must contain a JSON object.")
        return
    vg = value.get("vg")
    if vg is None:
        report.note(without_vg)
    elif not isinstance(vg, str) or not os.path.isabs(vg):
        report.problem(f"vg in {config_path} is {vg!r}, not an absolute path.")
    elif not (Path(vg).is_file() and os.access(vg, os.X_OK)):
        report.problem(f"vg in {config_path} is {vg}, which is not an executable file.")
    else:
        _doctor_vg_help(report, vg, environment)


def _doctor_vg_help(report: _DoctorReport, vg: str, environment: Mapping[str, str]) -> None:
    """Spec 4.7 item 4: vg has no --version, so --help must exit 0 in time."""
    try:
        process = subprocess.Popen(
            [vg, "--help"],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        report.problem(f"Could not run {vg} --help: {exc}")
        return
    try:
        _stdout, stderr = process.communicate(timeout=_VG_HELP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop(process)
        report.problem(f"{vg} --help did not finish within {_VG_HELP_TIMEOUT_SECONDS} seconds; it was stopped.")
        return
    if process.returncode != 0:
        first_error = next((line for line in stderr.splitlines() if line.strip()), "")
        suffix = f" First stderr line: {first_error}" if first_error else ""
        report.problem(f"{vg} --help exited {process.returncode}.{suffix}")


def _doctor_vault_graph_entry(report: _DoctorReport, user: UserPaths, environment: Mapping[str, str]) -> None:
    """Spec 4.7 item 5. A missing entry is a note: vault-builder works without vault-graph."""
    path = user.mcp_config
    try:
        config = _load_json_object(path, "MCP settings file")
    except InstallError:
        return  # _doctor_mcp_entry reported it
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        return  # _doctor_mcp_entry reported it
    key_path = f"mcpServers.{_VAULT_GRAPH_KEY}"
    entry = servers.get(_VAULT_GRAPH_KEY)
    if entry is None:
        report.note(f"{key_path} is not in {path}: vault-graph is not set up. To add it, run {_SETUP}.")
        return
    command = entry.get("command") if isinstance(entry, dict) else None
    found: str | None = None
    if isinstance(command, str) and command:
        found = command if os.path.isabs(command) else shutil.which(command, path=environment.get("PATH"))
    if found is None or not (Path(found).is_file() and os.access(found, os.X_OK)):
        report.problem(f"{key_path} command {command!r} in {path} is not an executable file.")


def _doctor_builder_command(report: _DoctorReport, user: UserPaths, builder_root: Path) -> None:
    """Spec 4.3 item 4: the vault-builder command file exists and points at its recorded checkout."""
    path = _builder_command.command_path(user.home)
    install = _builder_command.INSTALL_COMMAND
    if not path.exists() and not path.is_symlink():
        report.note(
            f"{path} is absent, so the vault-builder command is not installed; "
            f"tools/vault_migration/cli.py in {builder_root} still works. To install the command, run: {install}"
        )
        return
    try:
        contents = path.read_bytes()
    except OSError as exc:
        report.problem(f"Cannot read {path}: {exc}", fix=install)
        return
    if not _builder_command.has_marker(contents):
        moved = shlex.quote(f"{path}.mine")
        report.problem(
            f"{path} was not written by vault-builder (no marker line); the install command will not replace it.",
            fix=f"mv {shlex.quote(str(path))} {moved} && {install}",
        )
        return
    checkout = _builder_command.read_checkout(contents)
    if checkout is None:
        report.problem(f"{path} has no single absolute {_builder_command.CHECKOUT_VARIABLE} line.", fix=install)
    elif not (checkout / _builder_command.CLI_RELATIVE).is_file():
        report.problem(
            f"{path} points at {checkout}, which is not a vault-builder checkout "
            f"(no {_builder_command.CLI_RELATIVE}).",
            fix=install,
        )
    elif not os.access(path, os.X_OK):
        report.problem(f"{path} is not executable.", fix=f"chmod +x {shlex.quote(str(path))}")
    elif not _same_path(checkout, builder_root):
        report.note(f"{path} runs the checkout {checkout}; this doctor runs from {builder_root}.")
