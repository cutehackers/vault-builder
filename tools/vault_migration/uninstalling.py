"""Uninstall a Vault's builder files and check an installed Vault with doctor.

uninstall never deletes knowledge (raw/, wiki/, scratch/, .git) or files the manifest does not
list, and never commits. Inside the Vault it deletes managed files whose bytes still match the
manifest, except .gitignore, then .mcp.json and the manifest. Outside the Vault (schema 2 only) it
removes this Vault's MCP entry when its normalized hash matches the record, removes the Vault from
the vault map, and, when no Vault remains, deletes the vault map and the shared files it owns.

A shared `file` record (skill, recall script) counts as builder-owned when the file matches the
recorded hash or the current builder asset form that install also accepts: SKILL.md equal to the
asset, recall.sh equal to the asset except its single PYTHON= line. A later install from another
interpreter rewrites the shared recall.sh, so the first Vault's recorded hash can go stale.

doctor reports problems and exits 1 when it finds any. The only file it writes is .mcp.json, and
only when that file is missing, invalid, points to another Vault, or names a Python older than 3.10.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from tools.wiki.lib.vault_manifest import normalized_json_sha256

from .catalogs import _safe_relative_path, sha256_file
from .durability import fsync_directory
from .installing import (
    MCP_ENTRY_PREFIX,
    NAME_PATTERN,
    ROOT,
    InstallError,
    UserPaths,
    _ASSET_ROOT,
    _OutsideChanges,
    _absolute_target,
    _atomic_write,
    _entry_server,
    _json_bytes,
    _load_json_object,
    _recall_is_known,
    _same_path,
    _VAULT_MAP_VERSION,
    require_python,
    resolve_user_paths,
)
from .manifests import load_manifest
from .models import VaultManifest


_KEEP_INSIDE = frozenset({".gitignore"})
_MINIMUM_GIT = (2, 31)
_PYTHON_CHECK = "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"


class UninstallError(InstallError):
    """Uninstall refused before writing anything; the message says why."""


# Shared helpers.


def _records(manifest: VaultManifest, kind: str) -> list[Mapping[str, object]]:
    return [record for record in manifest.external_records if record["kind"] == kind]


def _record_path(home: Path, record: Mapping[str, object]) -> Path:
    return home / str(record["location"])


def _entry_key(record: Mapping[str, object]) -> str:
    return str(record["key_path"]).split(".", 1)[1]


def _record_problems(manifest: VaultManifest, user: UserPaths) -> list[str]:
    """Describe each outside record whose location or key is not one install writes."""
    problems = [_record_problem(record, user) for record in manifest.external_records]
    return [problem for problem in problems if problem]


def _record_problem(record: Mapping[str, object], user: UserPaths) -> str:
    """Return why a record is unexpected, or "" when install writes exactly this location.

    The manifest is tracked by git and editable, so uninstall and doctor accept only the paths
    install resolves from HOME and CLAUDE_CONFIG_DIR: the skill, the recall script, the vault map,
    and mcpServers.llm-wiki-<name> in the MCP settings file.
    """
    kind, location = str(record["kind"]), str(record["location"])
    if kind == "file":
        files = sorted({user.relative(user.skill), user.relative(user.recall)})
        if location not in files:
            return f"Unexpected file record {location}: expected {' or '.join(files)}."
    elif kind == "config":
        vault_map = user.relative(user.vault_map)
        if location != vault_map:
            return f"Unexpected config record {location}: expected {vault_map}."
    elif kind == "json-entry":
        mcp_config = user.relative(user.mcp_config)
        key_path = str(record["key_path"])
        prefix = "mcpServers." + MCP_ENTRY_PREFIX
        name = key_path[len(prefix):] if key_path.startswith(prefix) else ""
        if location != mcp_config or not NAME_PATTERN.fullmatch(name):
            return (
                f"Unexpected json-entry record {location} {key_path}: "
                f"expected {mcp_config} mcpServers.{MCP_ENTRY_PREFIX}<name>."
            )
    else:
        return f"Unexpected {kind} record {location}."
    return ""


def _is_skill(path: Path) -> bool:
    return path.name == "SKILL.md" and path.parent.name == "project-memory"


def _is_recall(path: Path) -> bool:
    return path.name == "recall.sh" and path.parent.name == "scripts"


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
    return False


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
    entry_path: Path | None = None
    entries: list[tuple[str, str]] = field(default_factory=list)  # (key, remove | keep | absent)
    vault_map_path: Path | None = None
    vault_map: dict[str, object] | None = None
    vault_map_action: str = ""  # update | delete | absent
    removed_vaults: list[str] = field(default_factory=list)
    remaining_vaults: list[str] = field(default_factory=list)
    delete_outside: list[Path] = field(default_factory=list)
    kept_outside: list[Path] = field(default_factory=list)
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
    plan.delete_mcp_json = mcp_json.is_file() and not mcp_json.is_symlink()

    if manifest.schema_version < 2:
        plan.notes.append(
            f"Manifest schema {manifest.schema_version} has no outside records: "
            "only files inside the vault were processed; files outside the vault were not touched."
        )
        return plan
    user = resolve_user_paths(environ)
    problems = _record_problems(manifest, user)
    if problems:
        raise UninstallError(
            "The manifest lists files outside the vault that install does not write; nothing was written. "
            + " ".join(problems)
            + " Check HOME and CLAUDE_CONFIG_DIR match the install, or restore the manifest from git."
        )
    _plan_outside(plan, manifest, builder_root, user.home)
    return plan


def _plan_outside(plan: _UninstallPlan, manifest: VaultManifest, builder_root: Path, home: Path) -> None:
    # Records were checked by _record_problems: every json-entry is in the same MCP settings file.
    names: set[str] = set()
    for record in _records(manifest, "json-entry"):
        key = _entry_key(record)
        names.add(key[len(MCP_ENTRY_PREFIX):])
        path = _record_path(home, record)
        plan.entry_path = path
        config = _load_json_object(path, "MCP settings file")
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise UninstallError(f"MCP settings file {path}: mcpServers must be a JSON object.")
        if key not in servers:
            action = "absent"
        elif normalized_json_sha256(servers[key]) == record["sha256"]:
            action = "remove"
        else:
            action = "keep"
        plan.entries.append((key, action))

    for record in _records(manifest, "config"):
        path = _record_path(home, record)
        plan.vault_map_path = path
        if not path.exists():
            plan.vault_map_action = "absent"
            continue
        vault_map = _load_json_object(path, "Vault map")
        vaults = vault_map.get("vaults")
        projects = vault_map.get("projects", {})
        if vault_map.get("version") != _VAULT_MAP_VERSION or not isinstance(vaults, dict):
            raise UninstallError(
                f"Vault map {path} must be a JSON object with version {_VAULT_MAP_VERSION} and a vaults object; "
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
            elif vault_name in names:
                plan.notes.append(f"Vault map entry {vault_name} points to another vault ({vault_path}); left as is.")
        dropped = set(removed) | {vault_name for vault_name in names if vault_name not in vaults}
        for project in projects.values():
            if isinstance(project, dict) and isinstance(project.get("vaults"), list):
                project["vaults"] = [item for item in project["vaults"] if item not in dropped]
        plan.vault_map = vault_map
        plan.removed_vaults = removed
        plan.remaining_vaults = list(vaults)
        plan.vault_map_action = "update" if vaults else "delete"

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
    if plan.last_vault:
        directories: list[Path] = []
        for path in [*plan.delete_outside, *plan.kept_outside]:
            if _is_recall(path):
                directories += [path.parent, path.parent.parent]
            elif _is_skill(path):
                directories.append(path.parent)
        if plan.vault_map_action == "delete" and plan.vault_map_path is not None:
            directories.append(plan.vault_map_path.parent)
        ordered = sorted(set(directories), key=lambda directory: len(directory.parts), reverse=True)
        _remove_empty_directories(ordered)


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
        print(f"  {verb} {plan.target / '.mcp.json'}", file=stream)
    print(f"  {verb} {plan.target / '.vault-builder' / 'manifest.json'}", file=stream)
    print("  kept .gitignore, raw/, wiki/, scratch/, .git and files the manifest does not list", file=stream)
    for relative in plan.kept_modified:
        print(f"Kept modified file: {plan.target / relative}", file=stream)
    for entry_key, action in plan.entries:
        key = f"mcpServers.{entry_key}"
        if action == "remove":
            print(f"  {'remove' if dry_run else 'removed'} {key} from {plan.entry_path}", file=stream)
        elif action == "keep":
            print(f"Kept changed MCP entry (differs from the manifest record): {key} in {plan.entry_path}", file=stream)
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


# doctor.


@dataclass
class _DoctorReport:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)


def doctor(
    target: Path,
    *,
    builder_root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    python: str | None = None,
    out: TextIO | None = None,
) -> int:
    stream = out if out is not None else sys.stdout
    require_python(tuple(sys.version_info))
    environment = os.environ if environ is None else environ
    interpreter = python or os.path.abspath(sys.executable)
    target = _absolute_target(Path(target))
    if not target.is_dir():
        raise InstallError(f"Target is not a directory: {target}")
    report = _DoctorReport()
    user = resolve_user_paths(environment)
    home = user.home

    manifest: VaultManifest | None = None
    try:
        manifest = load_manifest(target)
    except ValueError as exc:
        report.problems.append(f"Manifest is invalid: {exc}")
    if manifest is None and not report.problems:
        report.problems.append(f"No Vault builder manifest in {target}. Run install first.")
    elif manifest is not None and manifest.schema_version != 2:
        report.problems.append(
            f"Manifest schema is {manifest.schema_version}, not 2. Run audit and apply, then install."
        )

    skill, recall, vault_map_path, mcp_config = user.skill, user.recall, user.vault_map, user.mcp_config
    recorded: dict[Path, str] = {}
    name: str | None = None
    if manifest is not None:
        modified = sorted(
            relative
            for relative, managed in manifest.managed_files.items()
            if not (target / relative).is_file() or sha256_file(target / relative) != managed.sha256
        )
        if modified:
            report.problems.append(
                f"{len(modified)} managed files differ from the manifest: " + ", ".join(modified)
            )
        # An unexpected record is a problem and its location is not used.
        report.problems += _record_problems(manifest, user)
        trusted = [record for record in manifest.external_records if not _record_problem(record, user)]
        for record in (record for record in trusted if record["kind"] == "file"):
            path = _record_path(home, record)
            recorded[path] = str(record["sha256"])
            if _is_skill(path):
                skill = path
            elif _is_recall(path):
                recall = path
        for record in (record for record in trusted if record["kind"] == "config"):
            vault_map_path = _record_path(home, record)
        for record in (record for record in trusted if record["kind"] == "json-entry"):
            mcp_config = _record_path(home, record)
            key = _entry_key(record)
            if key.startswith(MCP_ENTRY_PREFIX):
                name = key[len(MCP_ENTRY_PREFIX):]

    vault_map = _doctor_vault_map(report, vault_map_path, target)
    if name is None and vault_map is not None:
        vaults = vault_map.get("vaults", {})
        for vault_name, value in vaults.items() if isinstance(vaults, dict) else ():
            if isinstance(value, dict) and isinstance(value.get("path"), str):
                if _same_path(Path(value["path"]).expanduser(), target):
                    name = vault_name
    if vault_map is not None and name is not None:
        vaults = vault_map.get("vaults", {})
        entry = vaults.get(name) if isinstance(vaults, dict) else None
        entry_path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(entry_path, str) or not _same_path(Path(entry_path).expanduser(), target):
            report.problems.append(f"Vault map {vault_map_path} does not include this vault as {name}.")
    elif vault_map is not None:
        report.problems.append(f"Vault map {vault_map_path} does not include this vault.")

    for path in (skill, recall):
        if not path.exists():
            report.problems.append(f"Missing {path}. Run install again.")
        elif not _builder_owned(path, recorded.get(path), builder_root):
            report.problems.append(f"{path} differs from the manifest record and the builder asset.")
    if recall.is_file():
        _doctor_recall(report, recall, environment)

    key = MCP_ENTRY_PREFIX + (name or target.name)
    _doctor_mcp_entry(report, mcp_config, key, target)
    if environment.get("CLAUDE_CONFIG_DIR"):
        _doctor_claude_mcp_list(report, key, mcp_config, environment)
    _doctor_git(report)
    _doctor_mcp_json(report, target, interpreter)

    for line in report.fixed:
        print(line, file=stream)
    for note in report.notes:
        print(f"Note: {note}", file=stream)
    for problem in report.problems:
        print(f"Problem: {problem}", file=stream)
    if report.problems:
        print(f"Doctor found {len(report.problems)} problems in {target}.", file=stream)
        return 1
    print(f"Doctor found no problems in {target}.", file=stream)
    return 0


def _doctor_vault_map(report: _DoctorReport, path: Path, target: Path) -> dict[str, object] | None:
    if not path.exists():
        report.problems.append(f"Missing vault map {path}. Run install again.")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report.problems.append(f"Vault map {path} is not valid JSON: {exc}")
        return None
    if not isinstance(value, dict) or not isinstance(value.get("vaults", {}), dict):
        report.problems.append(f"Vault map {path} must be a JSON object with a vaults object.")
        return None
    return value


def _doctor_recall(report: _DoctorReport, recall: Path, environment: Mapping[str, str]) -> None:
    if not os.access(recall, os.X_OK):
        report.problems.append(f"{recall} is not executable.")
        return
    lines = [line for line in recall.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("PYTHON=")]
    try:
        words = shlex.split(lines[0][len("PYTHON="):]) if len(lines) == 1 else []
    except ValueError:
        words = []
    if len(words) != 1 or not _python_ok(words[0]):
        report.problems.append(f"The interpreter in {recall} does not run Python 3.10 or newer. Run install again.")
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
            report.problems.append(f"{recall} failed outside a git repository: {exc}")
            return
    if result.returncode != 0:
        report.problems.append(
            f"{recall} exited {result.returncode} outside a git repository: {(result.stdout + result.stderr).strip()}"
        )


def _doctor_mcp_entry(report: _DoctorReport, path: Path, key: str, target: Path) -> None:
    try:
        config = _load_json_object(path, "MCP settings file")
    except InstallError as exc:
        report.problems.append(str(exc))
        return
    servers = config.get("mcpServers", {})
    entry = servers.get(key) if isinstance(servers, dict) else None
    if entry is None:
        report.problems.append(f"mcpServers.{key} is missing from {path}. Run install again.")
        return
    server = _entry_server(entry)
    if server is None or not _same_path(server, target / "tools" / "wiki" / "mcp_server.py"):
        report.problems.append(f"mcpServers.{key} in {path} does not point to {target / 'tools/wiki/mcp_server.py'}.")


def _doctor_claude_mcp_list(report: _DoctorReport, key: str, path: Path, environment: Mapping[str, str]) -> None:
    # CLAUDE_CONFIG_DIR may or may not move .claude.json (spec risk 14); ask Claude Code itself.
    claude = shutil.which("claude", path=environment.get("PATH"))
    if claude is None:
        report.notes.append(
            f"CLAUDE_CONFIG_DIR is set and claude is not on PATH: could not confirm that Claude Code reads {path}."
        )
        return
    try:
        result = subprocess.run(
            [claude, "mcp", "list"], env=dict(environment), capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.notes.append(f"claude mcp list failed: {exc}")
        return
    if not re.search(rf"(^|\s){re.escape(key)}:", result.stdout, flags=re.MULTILINE):
        report.problems.append(
            f"claude mcp list does not show {key}. With CLAUDE_CONFIG_DIR set, Claude Code may read "
            f"a different settings file than {path}."
        )


def _doctor_git(report: _DoctorReport) -> None:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        report.problems.append("git is not available; recall.sh needs git 2.31 or newer.")
        return
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if result.returncode != 0 or match is None:
        report.problems.append("Could not read the git version; recall.sh needs git 2.31 or newer.")
    elif (int(match.group(1)), int(match.group(2))) < _MINIMUM_GIT:
        report.problems.append(f"git {match.group(0)} is older than 2.31; recall.sh needs git 2.31 or newer.")


def _doctor_mcp_json(report: _DoctorReport, target: Path, interpreter: str) -> None:
    server = target / "tools" / "wiki" / "mcp_server.py"
    if not server.is_file():
        return
    path = target / ".mcp.json"
    config: dict[str, object] = {}
    reason = ""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        report.problems.append(f"{path} is not a regular file; it was not regenerated.")
        return
    if not path.exists():
        reason = "it was missing"
    else:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
            reason = "it was not valid JSON"
        else:
            config = value
            entry = value.get("mcpServers", {}).get("llm-wiki")
            owner = _entry_server(entry)
            command = entry.get("command") if isinstance(entry, dict) else None
            if owner is None or not _same_path(owner, server):
                reason = "it pointed to another vault"
            elif not isinstance(command, str) or not _python_ok(command):
                reason = "its interpreter is missing or older than Python 3.10"
    if not reason:
        return
    servers = dict(config.get("mcpServers", {}))  # type: ignore[arg-type]
    servers["llm-wiki"] = {"command": interpreter, "args": [str(server)]}
    config["mcpServers"] = servers
    _atomic_write(path, (json.dumps(config, indent=2) + "\n").encode("utf-8"), 0o644)
    report.fixed.append(f"Regenerated {path}: {reason}.")
