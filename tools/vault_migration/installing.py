"""Install a Vault plus its user-level files: project-memory skill, recall script, vault map, MCP entry.

New Vault: precheck, assemble and self-check in a temporary sibling folder, one rename, then the
changes outside the Vault, then the final manifest. A failure after the rename restores every
outside file from its backup and removes the new Vault folder, which holds only seed content.

Existing Vault (valid manifest): refuse unless a catalog v2 audit needs no changes, then regenerate
.mcp.json and run only the outside-Vault steps. A failure restores only the outside files.

Outside files live beneath the config folder: CLAUDE_CONFIG_DIR when set, else $HOME/.claude.
The MCP settings file is $HOME/.claude.json, or <config folder>/.claude.json when CLAUDE_CONFIG_DIR
is set. Manifest records locate them relative to $HOME, so the config folder must be inside $HOME.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from tools.wiki.lib.vault_manifest import normalized_json_sha256

from .catalogs import load_catalog, sha256_file
from .durability import fsync_directory
from .manifests import create_manifest, load_manifest, write_manifest
from .planning import audit_vault


ROOT = Path(__file__).resolve().parents[2]
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MCP_ENTRY_PREFIX = "llm-wiki-"
RECALL_PLACEHOLDER = "@VAULT_BUILDER_PYTHON@"
_ASSET_ROOT = Path("tools/vault_migration/assets/project-memory")
_CATALOG_PATH = "tools/vault_migration/contracts/v2.json"
_REFUSED_EXISTING_ACTIONS = frozenset({"create", "replace", "review-required", "invalid"})
_VAULT_MAP_VERSION = 1

# The same file set init-vault.sh copied before it became a wrapper.
_COPY_FILES = (
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "README-kr.md",
    "docs/LLM-WIKI.md",
    "docs/usage.md",
    "docs/USER_PROMPTS.md",
    "docs/VAULT_PROMPTS.md",
    ".claude/skills/wiki-ingest/SKILL.md",
    ".claude/skills/wiki-update/SKILL.md",
    ".claude/skills/wiki-query/SKILL.md",
    ".claude/skills/memory-cleanup/SKILL.md",
    "scripts/release_gate.sh",
)
_COPY_TREES = (
    ".github",
    ".agents",
    "docs/agent",
    "tools/wiki",
    "tests",
    "wiki",
    "scratch/drafts",
    "scratch/review",
)
_STENC_REQUIRED = (
    "docs/stenc/content",
    "tools/stenc/validate-stenc-doc.js",
    "tools/stenc/setup-project.js",
    "tools/stenc/check-rendered-pages.js",
)
_STENC_FILES = ("docs/stenc/.gitignore",)
_STENC_TREES = ("docs/stenc/content", "tools/stenc")
_EMPTY_DIRECTORIES = (
    "raw/sources",
    "raw/assets",
    "raw/imports",
    "scratch/drafts",
    "scratch/reports",
    "scratch/review",
    "docs",
)
_KEEP_FILES = (
    "raw/sources/.gitkeep",
    "raw/assets/.gitkeep",
    "raw/imports/.gitkeep",
    "scratch/reports/.gitkeep",
)


class InstallError(ValueError):
    """Install refused or failed; the message says why and what to do."""


def require_python(version: tuple[int, ...]) -> None:
    if tuple(version[:2]) < (3, 10):
        found = ".".join(str(part) for part in version[:3])
        raise InstallError(
            f"Python 3.10 or newer is required (running {found}). "
            "Run install with Python 3.10 or newer, for example python3.12."
        )


@dataclass(frozen=True)
class UserPaths:
    home: Path
    config_dir: Path
    mcp_config: Path

    @property
    def skill(self) -> Path:
        return self.config_dir / "skills" / "project-memory" / "SKILL.md"

    @property
    def recall(self) -> Path:
        return self.config_dir / "skills" / "project-memory" / "scripts" / "recall.sh"

    @property
    def vault_map(self) -> Path:
        return self.config_dir / "vault-builder" / "vault-map.json"

    def relative(self, path: Path) -> str:
        return path.relative_to(self.home).as_posix()


def resolve_user_paths(environ: Mapping[str, str]) -> UserPaths:
    raw_home = environ.get("HOME", "")
    if not raw_home or not os.path.isabs(raw_home):
        raise InstallError("HOME must be set to an absolute path.")
    home = Path(raw_home).resolve()
    raw_config = environ.get("CLAUDE_CONFIG_DIR", "")
    if raw_config:
        config_dir = Path(os.path.expanduser(raw_config)).absolute().resolve()
        mcp_config = config_dir / ".claude.json"
    else:
        config_dir = home / ".claude"
        mcp_config = home / ".claude.json"
    try:
        config_dir.relative_to(home)
    except ValueError:
        raise InstallError(
            f"CLAUDE_CONFIG_DIR must be inside $HOME ({home}): the manifest records outside files "
            f"relative to $HOME. Current value: {config_dir}"
        ) from None
    if config_dir == home:
        raise InstallError("CLAUDE_CONFIG_DIR must be a folder beneath $HOME, not $HOME itself.")
    return UserPaths(home=home, config_dir=config_dir, mcp_config=mcp_config)


@dataclass
class _FilePlan:
    path: Path
    contents: bytes
    mode: int
    action: str  # create | replace | unchanged | keep


@dataclass
class _InstallPlan:
    target: Path
    name: str
    python: str
    new_vault: bool
    target_was_empty_dir: bool
    with_stenc: bool
    user: UserPaths
    files: list[_FilePlan]
    vault_map: dict[str, object]
    vault_map_action: str
    mcp_entry: dict[str, object]
    mcp_entry_action: str
    notes: list[str] = field(default_factory=list)

    @property
    def mcp_key(self) -> str:
        return MCP_ENTRY_PREFIX + self.name


def install(
    target: Path,
    *,
    name: str | None = None,
    with_stenc: bool = False,
    dry_run: bool = False,
    builder_root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    python: str | None = None,
    out: TextIO | None = None,
) -> int:
    """Install or register a Vault. Raise InstallError after restoring the prior state."""
    stream = out if out is not None else sys.stdout
    require_python(tuple(sys.version_info))
    plan = _precheck(
        Path(target),
        name=name,
        with_stenc=with_stenc,
        builder_root=builder_root,
        environ=os.environ if environ is None else environ,
        python=python or os.path.abspath(sys.executable),
    )
    if dry_run:
        _print_dry_run(plan, stream)
        return 0
    if plan.new_vault:
        _install_new_vault(plan, builder_root)
    else:
        _install_existing_vault(plan, builder_root)
    _print_result(plan, stream)
    return 0


# Precheck: read only. Any failure here leaves every file untouched.


def _precheck(
    target: Path,
    *,
    name: str | None,
    with_stenc: bool,
    builder_root: Path,
    environ: Mapping[str, str],
    python: str,
) -> _InstallPlan:
    if not os.path.isabs(python):
        raise InstallError(f"Interpreter path must be absolute: {python}")
    user = resolve_user_paths(environ)
    target = _absolute_target(target)
    new_vault, empty_dir = _target_state(target, builder_root)
    vault_name = _vault_name(target, name)
    if with_stenc and new_vault:
        missing = [path for path in _STENC_REQUIRED if not (builder_root / path).exists()]
        if missing:
            raise InstallError(
                "Optional Stenc assets are not available in this vault template: "
                + ", ".join(missing)
                + ". Create the vault without --with-stenc, or run --with-stenc from a template that includes Stenc."
            )
    for path in (user.skill, user.recall, user.vault_map, user.mcp_config):
        _require_regular_or_absent(path)
    server = target / "tools" / "wiki" / "mcp_server.py"
    entry: dict[str, object] = {"command": python, "args": [str(server)]}
    notes: list[str] = []

    claude_config = _load_json_object(user.mcp_config, "MCP settings file")
    servers = claude_config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(f"MCP settings file {user.mcp_config}: mcpServers must be a JSON object.")
    key = MCP_ENTRY_PREFIX + vault_name
    existing_entry = servers.get(key)
    if existing_entry is None:
        entry_action = "create"
    else:
        owner = _entry_server(existing_entry)
        if owner is not None and _same_path(owner, server):
            entry_action = "unchanged" if existing_entry == entry else "replace"
        elif owner is not None and not owner.exists():
            entry_action = "replace"
            notes.append(f"Replacing mcpServers.{key}: its vault no longer exists ({owner}).")
        else:
            raise InstallError(
                f"MCP settings file {user.mcp_config} already has mcpServers.{key} for another vault. "
                "Choose a different name with --name."
            )

    vault_map = _load_json_object(user.vault_map, "Vault map")
    if vault_map:
        if vault_map.get("version") != _VAULT_MAP_VERSION:
            raise InstallError(f"Vault map {user.vault_map}: version must be {_VAULT_MAP_VERSION}.")
        for section in ("vaults", "projects"):
            if not isinstance(vault_map.get(section, {}), dict):
                raise InstallError(f"Vault map {user.vault_map}: {section} must be a JSON object.")
    else:
        vault_map = {"version": _VAULT_MAP_VERSION, "vaults": {}, "projects": {}}
    vaults = vault_map.setdefault("vaults", {})
    vault_map.setdefault("projects", {})
    assert isinstance(vaults, dict)
    existing_vault = vaults.get(vault_name)
    if existing_vault is None:
        vaults[vault_name] = {"path": str(target), "role": "personal"}
        map_action = "add"
    else:
        existing_path = existing_vault.get("path") if isinstance(existing_vault, dict) else None
        if not isinstance(existing_path, str):
            raise InstallError(
                f"Vault map {user.vault_map}: vault {vault_name} has no path. Fix it or choose --name."
            )
        if _same_path(Path(existing_path).expanduser(), target):
            map_action = "unchanged"
        elif not Path(existing_path).expanduser().exists():
            vaults[vault_name] = {"path": str(target), "role": "personal"}
            map_action = "replace"
            notes.append(f"Replacing vault map entry {vault_name}: its vault no longer exists ({existing_path}).")
        else:
            raise InstallError(
                f"Vault map {user.vault_map} already has vault {vault_name} at {existing_path}. "
                "Choose a different name with --name."
            )

    skill_asset = (builder_root / _ASSET_ROOT / "SKILL.md").read_bytes()
    files = [
        _file_plan(user.skill, skill_asset, 0o644, lambda current: current == skill_asset),
        _file_plan(user.recall, _recall_bytes(builder_root, python), 0o755, _recall_is_known(builder_root)),
    ]
    for item in files:
        if item.action == "keep":
            notes.append(f"Kept existing file with unknown content (not replaced): {item.path}")

    if not new_vault:
        _require_audit_clean(builder_root, target)

    return _InstallPlan(
        target=target,
        name=vault_name,
        python=python,
        new_vault=new_vault,
        target_was_empty_dir=empty_dir,
        with_stenc=with_stenc,
        user=user,
        files=files,
        vault_map=vault_map,
        vault_map_action=map_action,
        mcp_entry=entry,
        mcp_entry_action=entry_action,
        notes=notes,
    )


def _absolute_target(target: Path) -> Path:
    absolute = Path(os.path.abspath(target))
    if absolute == Path(absolute.anchor):
        raise InstallError("Target must not be the filesystem root.")
    if absolute.is_symlink():
        raise InstallError(f"Target must not be a symlink: {absolute}")
    return absolute.parent.resolve() / absolute.name


def _target_state(target: Path, builder_root: Path) -> tuple[bool, bool]:
    """Return (new_vault, target_is_empty_directory)."""
    if _same_path(target, builder_root) or _is_within(target, builder_root):
        raise InstallError(f"Target must be outside the builder checkout: {target}")
    if not target.exists():
        return True, False
    if not target.is_dir():
        raise InstallError(f"Target exists and is not a directory: {target}")
    if not any(target.iterdir()):
        return True, True
    try:
        manifest = load_manifest(target)
    except ValueError as exc:
        raise InstallError(f"Target has an invalid Vault builder manifest: {target}. {exc}") from exc
    if manifest is not None:
        if manifest.schema_version != 2:
            raise InstallError(
                f"Target manifest is schema version {manifest.schema_version}. Run audit and apply first: "
                f"tools/vault_migration/cli.py audit --target {shlex.quote(str(target))} --plan-out <plan.json>"
            )
        return False, False
    if (target / "wiki" / "index.md").exists() or (target / "tools" / "wiki" / "cli.py").exists():
        raise InstallError(
            f"Target is an initialized vault without a Vault builder manifest: {target}. Run audit and apply first."
        )
    raise InstallError(f"Target is not empty: {target}. Choose an empty or new directory.")


def _vault_name(target: Path, name: str | None) -> str:
    if name is not None:
        if not NAME_PATTERN.fullmatch(name):
            raise InstallError(
                f"--name {name!r} is invalid: use letters, digits, '.', '_' or '-', starting with a letter or digit."
            )
        return name
    if NAME_PATTERN.fullmatch(target.name):
        return target.name
    raise InstallError(
        f"Folder name {target.name!r} cannot be a vault name. Pass --name NAME "
        "(letters, digits, '.', '_' or '-', starting with a letter or digit)."
    )


def _require_regular_or_absent(path: Path) -> None:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise InstallError(f"Install only replaces regular files; this is a symlink or not a file: {path}")


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{label} {path} is not valid JSON: {exc}. Fix it, then run install again.") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{label} {path} must contain a JSON object.")
    return value


def _entry_server(entry: object) -> Path | None:
    if not isinstance(entry, dict):
        return None
    arguments = entry.get("args")
    if not isinstance(arguments, list):
        return None
    for argument in arguments:
        if isinstance(argument, str) and argument.endswith("tools/wiki/mcp_server.py"):
            return Path(argument)
    return None


def _same_path(left: Path, right: Path) -> bool:
    return os.path.abspath(left) == os.path.abspath(right) or left.resolve() == right.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _recall_bytes(builder_root: Path, python: str) -> bytes:
    text = (builder_root / _ASSET_ROOT / "scripts" / "recall.sh").read_text(encoding="utf-8")
    if text.count(RECALL_PLACEHOLDER) != 1:
        raise InstallError("recall.sh asset must contain exactly one interpreter placeholder.")
    return text.replace(RECALL_PLACEHOLDER, shlex.quote(python)).encode("utf-8")


def _recall_is_known(builder_root: Path) -> Callable[[bytes], bool]:
    template = (builder_root / _ASSET_ROOT / "scripts" / "recall.sh").read_text(encoding="utf-8")
    placeholder_line = f"PYTHON={RECALL_PLACEHOLDER}"

    def known(current: bytes) -> bool:
        # A builder recall.sh differs from the asset only in the interpreter on its PYTHON= line.
        try:
            lines = current.decode("utf-8").split("\n")
        except UnicodeError:
            return False
        candidates = [index for index, line in enumerate(lines) if line.startswith("PYTHON=")]
        if len(candidates) != 1:
            return False
        lines[candidates[0]] = placeholder_line
        return "\n".join(lines) == template

    return known


def _file_plan(path: Path, contents: bytes, mode: int, is_known: Callable[[bytes], bool]) -> _FilePlan:
    if not path.exists():
        return _FilePlan(path, contents, mode, "create")
    current = path.read_bytes()
    if current == contents and stat.S_IMODE(path.stat().st_mode) == mode:
        return _FilePlan(path, contents, mode, "unchanged")
    if current == contents or is_known(current):
        return _FilePlan(path, contents, mode, "replace")
    return _FilePlan(path, contents, mode, "keep")


def _require_audit_clean(builder_root: Path, target: Path) -> None:
    try:
        plan = audit_vault(builder_root, target)
    except ValueError as exc:
        raise InstallError(f"Audit of the existing vault failed: {exc}") from exc
    pending = sorted(
        f"{action.action} {action.path}" for action in plan.actions if action.action in _REFUSED_EXISTING_ACTIONS
    )
    if pending:
        shown = "; ".join(pending[:5]) + ("; ..." if len(pending) > 5 else "")
        raise InstallError(
            f"Existing vault needs migration first ({len(pending)} actions: {shown}). Run "
            f"tools/vault_migration/cli.py audit --target {shlex.quote(str(target))} --plan-out <plan.json>, "
            "then apply, then install again."
        )


# New Vault assembly. Only the staging folder is written; failure removes it.


def _install_new_vault(plan: _InstallPlan, builder_root: Path) -> None:
    parent = plan.target.parent
    created_parents = _make_parents(parent)
    staging: Path | None = None
    renamed = False
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{plan.target.name}.install-", dir=parent))
        os.chmod(staging, 0o777 & ~_current_umask())
        _assemble(staging, builder_root, plan.with_stenc)
        draft = create_manifest(builder_root, staging, "")
        write_manifest(staging, draft)
        _self_check(staging, builder_root, plan.python)
        _write_mcp_json(staging, plan)  # final paths, so it is valid only after the rename
        if plan.target_was_empty_dir:
            os.rmdir(plan.target)
        os.rename(staging, plan.target)
        renamed = True
        staging = None
        fsync_directory(parent)
    except BaseException as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if not renamed:
            if plan.target_was_empty_dir and not plan.target.exists():
                plan.target.mkdir()
            _remove_created(created_parents)
        if isinstance(exc, InstallError):
            raise
        if isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
            raise InstallError(f"Vault assembly failed; nothing was installed: {exc}") from exc
        raise

    changes = _OutsideChanges()
    try:
        records = _apply_outside_changes(plan, changes)
        manifest = dataclasses.replace(draft, external_records=tuple(records))
        _before_write(plan.target / ".vault-builder" / "manifest.json")
        write_manifest(plan.target, manifest)
    except BaseException as exc:
        changes.rollback()
        shutil.rmtree(plan.target, ignore_errors=True)
        if plan.target_was_empty_dir:
            plan.target.mkdir()
        _remove_created(created_parents)
        if isinstance(exc, (OSError, ValueError)):
            raise InstallError(f"Install failed; outside files were restored and the new vault removed: {exc}") from exc
        raise
    finally:
        changes.finish()


def _install_existing_vault(plan: _InstallPlan, builder_root: Path) -> None:
    manifest = load_manifest(plan.target)
    assert manifest is not None
    _write_mcp_json(plan.target, plan)
    changes = _OutsideChanges()
    try:
        records = _apply_outside_changes(plan, changes)
        identities = {(str(record["location"]), str(record.get("key_path", ""))) for record in records}
        kept = [
            dict(record)
            for record in manifest.external_records
            if (str(record["location"]), str(record.get("key_path", ""))) not in identities
        ]
        updated = dataclasses.replace(manifest, external_records=tuple([*kept, *records]))
        _before_write(plan.target / ".vault-builder" / "manifest.json")
        write_manifest(plan.target, updated)
    except BaseException as exc:
        changes.rollback()
        if isinstance(exc, (OSError, ValueError)):
            raise InstallError(f"Install failed; outside files were restored: {exc}") from exc
        raise
    finally:
        changes.finish()


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _make_parents(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
    return missing  # deepest first, the removal order


def _remove_created(directories: list[Path]) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _copy_file(builder_root: Path, staging: Path, relative: str) -> None:
    source = builder_root / relative
    if not source.is_file():
        return
    destination = staging / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, stat.S_IMODE(source.stat().st_mode) & ~_current_umask())


def _copy_tree(builder_root: Path, staging: Path, relative: str) -> None:
    root = builder_root / relative
    if not root.is_dir():
        return
    for source in sorted(root.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        parts = source.relative_to(builder_root).parts
        if "__pycache__" in parts or source.name.endswith(".pyc") or source.name == ".DS_Store":
            continue
        _copy_file(builder_root, staging, source.relative_to(builder_root).as_posix())


def _assemble(staging: Path, builder_root: Path, with_stenc: bool) -> None:
    for relative in _EMPTY_DIRECTORIES:
        (staging / relative).mkdir(parents=True, exist_ok=True)
    for relative in _COPY_FILES:
        _copy_file(builder_root, staging, relative)
    for relative in _COPY_TREES:
        _copy_tree(builder_root, staging, relative)
    if with_stenc:
        for relative in _STENC_FILES:
            _copy_file(builder_root, staging, relative)
        for relative in _STENC_TREES:
            _copy_tree(builder_root, staging, relative)
    for relative in _KEEP_FILES:
        (staging / relative).touch()
    gate = staging / "scripts" / "release_gate.sh"
    if gate.is_file():
        gate.chmod(0o755)


def _self_check(staging: Path, builder_root: Path, python: str) -> None:
    """Run lint and verify the draft manifest records every catalog file with matching bytes."""
    manifest = load_manifest(staging)
    if manifest is None:
        raise InstallError("Self-check failed: the draft manifest is missing.")
    catalog = load_catalog(builder_root / _CATALOG_PATH)
    missing = sorted(set(catalog.managed_files) - set(manifest.managed_files))
    if missing:
        raise InstallError("Self-check failed: managed files were not copied: " + ", ".join(missing))
    for relative, managed in manifest.managed_files.items():
        if sha256_file(staging / relative) != managed.sha256:
            raise InstallError(f"Self-check failed: managed file bytes differ: {relative}")
    lint = subprocess.run(
        [python, "-B", "tools/wiki/cli.py", "lint"],
        cwd=staging,
        text=True,
        capture_output=True,
        check=False,
    )
    if lint.returncode != 0:
        raise InstallError("Self-check failed: lint reported errors.\n" + (lint.stdout + lint.stderr).strip())


def _write_mcp_json(directory: Path, plan: _InstallPlan) -> None:
    # .mcp.json is machine-specific (interpreter, absolute path), so it is not a managed file.
    config = {"mcpServers": {"llm-wiki": plan.mcp_entry}}
    _atomic_write(directory / ".mcp.json", (json.dumps(config, indent=2) + "\n").encode("utf-8"), 0o644)


# Changes outside the Vault: backup, atomic write, restore on failure.


def _before_write(_path: Path) -> None:
    """Test seam called before each write outside the staging folder."""


def _atomic_write(path: Path, contents: bytes, mode: int) -> None:
    """Write through a temporary sibling, fsync, and os.replace, as manifests.write_manifest does."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            view = memoryview(contents)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(f"Write was incomplete: {path}")
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        temporary = ""
        fsync_directory(path.parent)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@dataclass
class _Prior:
    path: Path
    contents: bytes | None
    mode: int | None
    backup: Path | None


class _OutsideChanges:
    """Track outside writes so a failure can restore each file from its backup."""

    def __init__(self) -> None:
        self.priors: list[_Prior] = []
        self.created_directories: list[Path] = []

    def write(self, path: Path, contents: bytes, mode: int) -> None:
        missing = [directory for directory in _missing_directories(path.parent)]
        for directory in reversed(missing):
            directory.mkdir()
            self.created_directories.append(directory)
        if not any(prior.path == path for prior in self.priors):
            self.priors.append(self._backup(path))
        _before_write(path)
        _atomic_write(path, contents, mode)

    def _backup(self, path: Path) -> _Prior:
        if not path.exists():
            return _Prior(path, None, None, None)
        contents = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        hidden = path.name if path.name.startswith(".") else f".{path.name}"
        backup = path.with_name(f"{hidden}.{secrets.token_hex(4)}.vault-builder-backup")
        _atomic_write(backup, contents, mode)
        return _Prior(path, contents, mode, backup)

    def rollback(self) -> None:
        for prior in reversed(self.priors):
            try:
                if prior.contents is None:
                    prior.path.unlink(missing_ok=True)
                else:
                    assert prior.mode is not None
                    _atomic_write(prior.path, prior.contents, prior.mode)
            except OSError:
                if prior.backup is not None:
                    print(f"install: could not restore {prior.path}; its backup is {prior.backup}", file=sys.stderr)
                    prior.backup = None
        for directory in reversed(self.created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        self.created_directories = []

    def finish(self) -> None:
        for prior in self.priors:
            if prior.backup is not None:
                prior.backup.unlink(missing_ok=True)
        self.priors = []


def _missing_directories(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    return missing


def _apply_outside_changes(plan: _InstallPlan, changes: _OutsideChanges) -> list[dict[str, object]]:
    """Write skill, recall script, vault map and MCP entry. Return the manifest external records.

    The caller rolls back `changes` on any failure, including failures after this returns.
    """
    user = plan.user
    records: list[dict[str, object]] = []
    for item in plan.files:
        if item.action in {"create", "replace"}:
            changes.write(item.path, item.contents, item.mode)
        if item.action != "keep":
            records.append(
                {"kind": "file", "location": user.relative(item.path), "sha256": sha256_file(item.path), "shared": True}
            )
    if plan.vault_map_action != "unchanged" or not user.vault_map.exists():
        changes.write(user.vault_map, _json_bytes(plan.vault_map, indent=2), 0o644)
    records.append({"kind": "config", "location": user.relative(user.vault_map), "shared": True})
    # Re-read just before writing: Claude Code may have rewritten the file since the precheck.
    config = _load_json_object(user.mcp_config, "MCP settings file")
    servers = dict(config.get("mcpServers", {}))  # type: ignore[arg-type]
    servers[plan.mcp_key] = plan.mcp_entry
    config["mcpServers"] = servers
    if plan.mcp_entry_action != "unchanged" or not user.mcp_config.exists():
        mode = stat.S_IMODE(user.mcp_config.stat().st_mode) if user.mcp_config.exists() else 0o600
        changes.write(user.mcp_config, _json_bytes(config, indent=2), mode)
    records.append(
        {
            "kind": "json-entry",
            "location": user.relative(user.mcp_config),
            "key_path": f"mcpServers.{plan.mcp_key}",
            "sha256": normalized_json_sha256(plan.mcp_entry),
            "shared": False,
        }
    )
    return records


def _json_bytes(value: object, *, indent: int) -> bytes:
    return (json.dumps(value, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")


# Output.


def _print_dry_run(plan: _InstallPlan, stream: TextIO) -> None:
    print(f"Dry run: install {'new vault' if plan.new_vault else 'existing vault'} {plan.target}", file=stream)
    print(f"Vault name: {plan.name}", file=stream)
    print(f"Interpreter: {plan.python}", file=stream)
    if plan.new_vault:
        print(f"  create vault {plan.target} (assembled in a temporary sibling folder, then renamed)", file=stream)
    print(f"  write {plan.target / '.mcp.json'}", file=stream)
    for item in plan.files:
        print(f"  {item.action} {item.path}", file=stream)
    print(f"  {plan.vault_map_action} vault {plan.name} in {plan.user.vault_map}", file=stream)
    print(f"  {plan.mcp_entry_action} mcpServers.{plan.mcp_key} in {plan.user.mcp_config}", file=stream)
    print(f"  write {plan.target / '.vault-builder' / 'manifest.json'} with outside records", file=stream)
    for note in plan.notes:
        print(f"Note: {note}", file=stream)
    print("Nothing was written.", file=stream)


def _print_result(plan: _InstallPlan, stream: TextIO) -> None:
    python = shlex.quote(plan.python)
    if plan.new_vault:
        print(f"LLM Wiki Vault initialized: {plan.target}", file=stream)
        if plan.with_stenc:
            print("Optional Stenc docs and tools included.", file=stream)
        else:
            print("Optional Stenc docs and tools skipped. Re-run with --with-stenc to include them.", file=stream)
    else:
        print(f"LLM Wiki Vault registered: {plan.target}", file=stream)
    print(f"Vault name: {plan.name}", file=stream)
    print(f"MCP server: mcpServers.{plan.mcp_key} in {plan.user.mcp_config}", file=stream)
    print(f"Vault map: {plan.user.vault_map}", file=stream)
    print(f"Skill: {plan.user.skill}", file=stream)
    for note in plan.notes:
        print(f"Note: {note}", file=stream)
    print("Restart Claude Code to load the new MCP server and the project-memory skill.", file=stream)
    print("Next:", file=stream)
    print(f"  cd {plan.target}", file=stream)
    print(f"  {python} tools/wiki/cli.py lint --report", file=stream)
    print(f"  {python} tools/wiki/cli.py health", file=stream)
