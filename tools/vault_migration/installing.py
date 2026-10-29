"""Install a Vault plus its user-level files: project-memory skill, recall script, router, vault map,
and the one shared MCP server entry `mcpServers.llm-wiki`. No Vault gets a .mcp.json.

New Vault: precheck, assemble and self-check in a temporary sibling folder, one rename, then the
changes outside the Vault, then the final manifest. A failure after the rename restores every
outside file from its backup and removes the new Vault folder, which holds only seed content.

Existing Vault (valid manifest): refuse unless a catalog v2 audit needs no changes, then run only the
outside-Vault steps. A failure restores only the outside files.

Vault without a manifest but with wiki/index.md (left by uninstall): audit it, refuse when the plan
holds review-required or invalid actions, restore managed files and the manifest with apply_plan,
then continue as an existing Vault. A failure after apply restores only the outside files and
leaves a valid existing Vault, so running install again finishes the job.

Outside files live beneath the config folder: CLAUDE_CONFIG_DIR when set, else $HOME/.claude.
The MCP settings file is $HOME/.claude.json, or <config folder>/.claude.json when CLAUDE_CONFIG_DIR
is set. Manifest records locate them relative to $HOME, so the config folder must be inside $HOME.
The server entry is rewritten only when it changes, so a second Vault usually leaves that file alone.
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

from .applying import apply_plan
from .catalogs import load_catalog, sha256_file
from .durability import fsync_directory
from .manifests import create_manifest, load_manifest, write_manifest
from .models import MigrationPlan
from .planning import audit_vault


ROOT = Path(__file__).resolve().parents[2]
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MCP_SERVER_KEY = "llm-wiki"
# Entries named llm-wiki-<name> come from the previous builder version, which install refuses.
MCP_ENTRY_PREFIX = "llm-wiki-"
RECALL_PLACEHOLDER = "@VAULT_BUILDER_PYTHON@"
_ASSET_ROOT = Path("tools/vault_migration/assets/project-memory")
_ROUTER_ASSET = Path("tools/vault_migration/assets/llm-wiki-router/llm_wiki_router.py")
_CATALOG_PATH = "tools/vault_migration/contracts/v2.json"
_REFUSED_EXISTING_ACTIONS = frozenset({"create", "replace", "review-required", "invalid"})
_REFUSED_RESTORE_ACTIONS = frozenset({"review-required", "invalid"})
_VAULT_MAP_VERSION = 2  # the version install writes
_VAULT_MAP_READ_VERSIONS = (1, 2)


def _vault_map_version_ok(version: object) -> bool:
    """Only a JSON integer 1 or 2, as the router, recall and memory-scan read it (not true or 1.0)."""
    return type(version) is int and version in _VAULT_MAP_READ_VERSIONS
_PATCH_VERSION = re.compile(r"\d+\.\d+\.\d+")
_PREVIOUS_VERSION_HINT = "Run uninstall with the previous vault-builder version, then run install again."

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

    @property
    def router(self) -> Path:
        return self.config_dir / "vault-builder" / "llm_wiki_router.py"

    def relative(self, path: Path) -> str:
        return path.relative_to(self.home).as_posix()


def choose_interpreter(executable: str, environ: Mapping[str, str]) -> tuple[str, str | None]:
    """Return (interpreter path, note). Prefer a path to the same real file without a patch version.

    Candidates in order: the absolute executable path, python<major>.<minor> on PATH, python3 on PATH.
    major.minor is the running interpreter's version. With no match, return the executable path and a
    note, because a path with a patch version stops working after a Python patch update.
    """
    original = os.path.abspath(executable)
    real = os.path.realpath(executable)
    search = environ.get("PATH", "")
    candidates = [original]
    for name in (f"python{sys.version_info[0]}.{sys.version_info[1]}", "python3"):
        found = shutil.which(name, path=search) if search else None
        if found:
            candidates.append(os.path.abspath(found))
    for candidate in candidates:
        if os.path.realpath(candidate) == real and not _PATCH_VERSION.search(candidate):
            return candidate, None
    return original, (
        f"Used a version-bound interpreter path ({original}). "
        "After you upgrade Python, run install again."
    )


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
    vault_map_write: bool
    mcp_entry: dict[str, object]
    mcp_entry_action: str
    notes: list[str] = field(default_factory=list)
    restore_plan: MigrationPlan | None = None  # set for a Vault without a manifest

    @property
    def mcp_key(self) -> str:
        return MCP_SERVER_KEY


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
    environment = os.environ if environ is None else environ
    notes: list[str] = []
    if python is None:
        python, note = choose_interpreter(sys.executable, environment)
        if note is not None:
            notes.append(note)
    plan = _precheck(
        Path(target),
        name=name,
        with_stenc=with_stenc,
        builder_root=builder_root,
        environ=environment,
        python=python,
    )
    plan.notes[:0] = notes
    if dry_run:
        _print_dry_run(plan, stream)
        return 0
    if plan.new_vault:
        _install_new_vault(plan, builder_root)
    elif plan.restore_plan is not None:
        _install_restored_vault(plan, builder_root)
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
    state, empty_dir = _target_state(target, builder_root)
    new_vault = state == "new"
    vault_name = _vault_name(target, name)
    if state == "existing":
        _require_current_records(target, user)
    if state != "new" and (target / ".mcp.json").exists():
        raise InstallError(
            f"Vault {target} has a .mcp.json from the previous vault-builder version. {_PREVIOUS_VERSION_HINT}"
        )
    if with_stenc and new_vault:
        missing = [path for path in _STENC_REQUIRED if not (builder_root / path).exists()]
        if missing:
            raise InstallError(
                "Optional Stenc assets are not available in this vault template: "
                + ", ".join(missing)
                + ". Create the vault without --with-stenc, or run --with-stenc from a template that includes Stenc."
            )
    for path in (user.skill, user.recall, user.router, user.vault_map, user.mcp_config):
        _require_regular_or_absent(path)
    entry: dict[str, object] = {"command": python, "args": ["-I", str(user.router)]}
    notes: list[str] = []

    claude_config = _load_json_object(user.mcp_config, "MCP settings file")
    servers = claude_config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(f"MCP settings file {user.mcp_config}: mcpServers must be a JSON object.")
    old_keys = sorted(
        key for key, value in servers.items() if key.startswith(MCP_ENTRY_PREFIX) and _entry_server(value) is not None
    )
    if old_keys:
        raise InstallError(
            f"MCP settings file {user.mcp_config} has entries from the previous vault-builder version: "
            + ", ".join(f"mcpServers.{key}" for key in old_keys)
            + f". {_PREVIOUS_VERSION_HINT}"
        )
    existing_entry = servers.get(MCP_SERVER_KEY)
    if existing_entry is None:
        entry_action = "create"
    elif not _runs_router(existing_entry, user.router):
        raise InstallError(
            f"MCP settings file {user.mcp_config} already has mcpServers.{MCP_SERVER_KEY} for another program "
            f"(its args do not include {user.router}). Remove or rename that entry, then run install again."
        )
    elif existing_entry == entry:
        entry_action = "unchanged"
    else:
        entry_action = "replace"
        old_command = existing_entry.get("command") if isinstance(existing_entry, dict) else None
        notes.append(f"Replacing mcpServers.{MCP_SERVER_KEY} interpreter {old_command} with {python}.")

    vault_map = _load_json_object(user.vault_map, "Vault map")
    if user.vault_map.exists():
        if not _vault_map_version_ok(vault_map.get("version")):
            versions = " or ".join(str(version) for version in _VAULT_MAP_READ_VERSIONS)
            raise InstallError(f"Vault map {user.vault_map}: version must be {versions}.")
        for section in ("vaults", "projects"):
            if not isinstance(vault_map.get(section, {}), dict):
                raise InstallError(f"Vault map {user.vault_map}: {section} must be a JSON object.")
        map_upgrade = vault_map.get("version") != _VAULT_MAP_VERSION
        vault_map["version"] = _VAULT_MAP_VERSION
    else:
        vault_map = {"version": _VAULT_MAP_VERSION, "vaults": {}, "projects": {}}
        map_upgrade = False
    vaults = vault_map.setdefault("vaults", {})
    vault_map.setdefault("projects", {})
    assert isinstance(vaults, dict)
    if state != "new":
        _require_one_vault_name(target, vault_name, vaults, user)
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
    router_asset = (builder_root / _ROUTER_ASSET).read_bytes()
    files = [
        _file_plan(user.skill, skill_asset, 0o644, lambda current: current == skill_asset),
        _file_plan(user.recall, _recall_bytes(builder_root, python), 0o755, _recall_is_known(builder_root)),
        _file_plan(user.router, router_asset, 0o644, lambda current: current == router_asset),
    ]
    for item in files:
        if item.action == "keep":
            notes.append(f"Kept existing file with unknown content (not replaced): {item.path}")

    restore_plan = None
    if state == "existing":
        _require_audit_clean(builder_root, target)
    elif state == "restore":
        restore_plan = _restore_audit(builder_root, target)

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
        vault_map_write=map_upgrade or map_action != "unchanged" or not user.vault_map.exists(),
        mcp_entry=entry,
        mcp_entry_action=entry_action,
        notes=notes,
        restore_plan=restore_plan,
    )


def _absolute_target(target: Path) -> Path:
    absolute = Path(os.path.abspath(target))
    if absolute == Path(absolute.anchor):
        raise InstallError("Target must not be the filesystem root.")
    if absolute.is_symlink():
        raise InstallError(f"Target must not be a symlink: {absolute}")
    return absolute.parent.resolve() / absolute.name


def _target_state(target: Path, builder_root: Path) -> tuple[str, bool]:
    """Return (state, target_is_empty_directory). state is new, existing, or restore (no manifest)."""
    if _same_path(target, builder_root) or _is_within(target, builder_root):
        raise InstallError(f"Target must be outside the builder checkout: {target}")
    if not target.exists():
        return "new", False
    if not target.is_dir():
        raise InstallError(f"Target exists and is not a directory: {target}")
    if not any(target.iterdir()):
        return "new", True
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
        return "existing", False
    if (target / "wiki" / "index.md").exists() or (target / "tools" / "wiki" / "cli.py").exists():
        return "restore", False
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


def _require_current_records(target: Path, user: UserPaths) -> None:
    """Refuse records from another config folder or another server entry: uninstall accepts only these."""
    manifest = load_manifest(target)
    assert manifest is not None
    expected = {
        user.relative(path) for path in (user.skill, user.recall, user.router, user.vault_map, user.mcp_config)
    }
    for record in manifest.external_records:
        key_path = str(record.get("key_path", ""))
        if record["kind"] == "json-entry" and key_path.startswith("mcpServers." + MCP_ENTRY_PREFIX):
            raise InstallError(
                f"Vault {target} was installed by the previous vault-builder version ({key_path}). "
                f"{_PREVIOUS_VERSION_HINT}"
            )
        if str(record["location"]) not in expected:
            raise InstallError(
                f"Vault {target} was installed with another HOME or CLAUDE_CONFIG_DIR "
                f"({record['location']} is not under the current config folder). "
                "Run uninstall with the original HOME and CLAUDE_CONFIG_DIR first."
            )
        if record["kind"] == "json-entry" and key_path != f"mcpServers.{MCP_SERVER_KEY}":
            raise InstallError(
                f"Vault {target} has an unexpected server entry record {key_path}; "
                f"install records only mcpServers.{MCP_SERVER_KEY}. Run uninstall first."
            )


def _require_one_vault_name(target: Path, name: str, vaults: dict[str, object], user: UserPaths) -> None:
    """Refuse a second vault map name for the same folder: uninstall removes only one."""
    for other, value in vaults.items():
        path = value.get("path") if isinstance(value, dict) else None
        if other != name and isinstance(path, str) and _same_path(Path(path).expanduser(), target):
            raise InstallError(
                f"Vault {target} is already registered as vault {other} in {user.vault_map}. "
                f"Run uninstall first to change its name, or install again with --name {other}."
            )


def _runs_router(entry: object, router: Path) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("args"), list):
        return False
    return any(isinstance(argument, str) and _same_path(Path(argument), router) for argument in entry["args"])


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


def _restore_audit(builder_root: Path, target: Path) -> MigrationPlan:
    """Audit a Vault without a manifest. Refuse when any file needs human review (decision 42)."""
    try:
        plan = audit_vault(builder_root, target)
    except ValueError as exc:
        raise InstallError(
            f"Target is a vault without a Vault builder manifest, and its audit failed: {exc} "
            f"Run tools/vault_migration/cli.py audit --target {shlex.quote(str(target))} --plan-out <plan.json> "
            "to inspect it."
        ) from exc
    blocked = sorted(f"{action.action} {action.path}" for action in plan.actions if action.action in _REFUSED_RESTORE_ACTIONS)
    if blocked:
        raise InstallError(
            f"Target is a vault without a Vault builder manifest, and {len(blocked)} paths need review before "
            f"install can restore its managed files: {'; '.join(blocked)}. Run "
            f"tools/vault_migration/cli.py audit --target {shlex.quote(str(target))} --plan-out <plan.json>, "
            "resolve those paths, apply the plan, then run install again."
        )
    return plan


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


def _install_restored_vault(plan: _InstallPlan, builder_root: Path) -> None:
    """Restore managed files and the manifest with apply, then continue as an existing Vault."""
    assert plan.restore_plan is not None
    try:
        apply_plan(builder_root, plan.target, plan.restore_plan)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise InstallError(
            "Restoring the vault's managed files and manifest failed; apply restored the vault and nothing "
            f"outside the vault changed: {exc}"
        ) from exc
    _install_existing_vault(
        plan,
        builder_root,
        failure=(
            "Install failed after the vault's managed files and manifest were restored; outside files were "
            "restored. The vault is now a valid existing vault, so run install again to finish"
        ),
    )


def _install_existing_vault(
    plan: _InstallPlan,
    builder_root: Path,
    *,
    failure: str = "Install failed; outside files were restored",
) -> None:
    manifest = load_manifest(plan.target)
    assert manifest is not None
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
            raise InstallError(f"{failure}: {exc}") from exc
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

    def delete(self, path: Path) -> None:
        """Remove a file after backing it up; rollback writes it back."""
        if not any(prior.path == path for prior in self.priors):
            self.priors.append(self._backup(path))
        _before_write(path)
        path.unlink()
        fsync_directory(path.parent)

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
    """Write skill, recall script, router, vault map and server entry. Return the manifest external records.

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
    if plan.vault_map_write:
        changes.write(user.vault_map, _json_bytes(plan.vault_map, indent=2), 0o644)
    records.append({"kind": "config", "location": user.relative(user.vault_map), "shared": True})
    if plan.mcp_entry_action != "unchanged":
        # Re-read just before writing: Claude Code may have rewritten the file since the precheck.
        config = _load_json_object(user.mcp_config, "MCP settings file")
        servers = dict(config.get("mcpServers", {}))  # type: ignore[arg-type]
        servers[plan.mcp_key] = plan.mcp_entry
        config["mcpServers"] = servers
        mode = stat.S_IMODE(user.mcp_config.stat().st_mode) if user.mcp_config.exists() else 0o600
        changes.write(user.mcp_config, _json_bytes(config, indent=2), mode)
    records.append(
        {
            "kind": "json-entry",
            "location": user.relative(user.mcp_config),
            "key_path": f"mcpServers.{plan.mcp_key}",
            "sha256": normalized_json_sha256(plan.mcp_entry),
            "shared": True,
        }
    )
    return records


def _json_bytes(value: object, *, indent: int) -> bytes:
    return (json.dumps(value, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")


# Output.


def _restore_changes(plan: MigrationPlan) -> list[str]:
    return sorted(f"{action.action} {action.path}" for action in plan.actions if action.action in {"create", "replace"})


def _print_dry_run(plan: _InstallPlan, stream: TextIO) -> None:
    if plan.new_vault:
        kind = "new vault"
    elif plan.restore_plan is not None:
        kind = "vault without a manifest"
    else:
        kind = "existing vault"
    print(f"Dry run: install {kind} {plan.target}", file=stream)
    print(f"Vault name: {plan.name}", file=stream)
    print(f"Interpreter: {plan.python}", file=stream)
    if plan.new_vault:
        print(f"  create vault {plan.target} (assembled in a temporary sibling folder, then renamed)", file=stream)
    if plan.restore_plan is not None:
        counts: dict[str, int] = {}
        for action in plan.restore_plan.actions:
            counts[action.action] = counts.get(action.action, 0) + 1
        summary = ", ".join(f"{action}: {counts[action]}" for action in sorted(counts))
        print(f"  audit plan {plan.restore_plan.migration_id} ({summary})", file=stream)
        for line in _restore_changes(plan.restore_plan):
            print(f"    {line}", file=stream)
        print("  apply the audit plan (release gate runs twice), which writes the manifest", file=stream)
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
    if plan.restore_plan is not None:
        print("Restored managed files and the manifest:", file=stream)
        for line in _restore_changes(plan.restore_plan) or ["none"]:
            print(f"  {line}", file=stream)
    print(f"Vault name: {plan.name}", file=stream)
    print(f"MCP server: mcpServers.{plan.mcp_key} ({plan.mcp_entry_action}) in {plan.user.mcp_config}", file=stream)
    print(f"Router: {plan.user.router}", file=stream)
    print(f"Vault map: {plan.user.vault_map}", file=stream)
    print(f"Skill: {plan.user.skill}", file=stream)
    for note in plan.notes:
        print(f"Note: {note}", file=stream)
    print(
        "Restart Claude Code, check that /mcp lists one llm-wiki server, then run "
        f"tools/vault_migration/cli.py doctor {shlex.quote(str(plan.target))} from the vault-builder checkout.",
        file=stream,
    )
    print("Next:", file=stream)
    print(f"  cd {plan.target}", file=stream)
    print(f"  {python} tools/wiki/cli.py lint --report", file=stream)
    print(f"  {python} tools/wiki/cli.py health", file=stream)
