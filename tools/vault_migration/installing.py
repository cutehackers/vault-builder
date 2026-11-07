"""Install a Vault plus its user-level files: router, vault map, and the one shared MCP server entry
`mcpServers.vault-wiki`. No Vault gets a .mcp.json.
An entry of ours under the previous name `mcpServers.llm-wiki` is renamed in the same write.

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

Install no longer writes the project-memory skill and its recall script (vault-graph request S11). When
an earlier install left them and they are still builder output (a manifest records their hash, or they
equal a released asset), install deletes them, drops their manifest records, and removes the folders
they leave empty. A changed copy is kept and listed.

--with-hooks also copies the hook launcher vault_hooks.py next to the router, records the vg path in
<config folder>/vault-builder/vault-hooks.json, and adds the launcher hook entries to
the Claude Code settings file named in hooks.py. Without --with-hooks install does not open that file.
"""
from __future__ import annotations

import dataclasses
import hashlib
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

from tools.vault_migration.versioning import version_label
from tools.wiki.lib.vault_manifest import MANIFEST_SCHEMA_VERSION, normalized_json_sha256

from . import hooks as _hooks
from .applying import apply_plan
from .catalogs import load_catalog, sha256_file
from .durability import fsync_directory
from .manifests import create_manifest, load_manifest, write_manifest
from .models import MigrationPlan
from .planning import audit_vault


ROOT = Path(__file__).resolve().parents[2]
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MCP_SERVER_KEY = "vault-wiki"
# The name of the shared entry before vault-wiki. Install renames it when its args run this config folder's router.
LEGACY_MCP_SERVER_KEY = "llm-wiki"
# Manifest json-entry key paths install writes now or wrote before the rename.
SERVER_KEY_PATHS = (f"mcpServers.{MCP_SERVER_KEY}", f"mcpServers.{LEGACY_MCP_SERVER_KEY}")
# Entries named llm-wiki-<name> come from the previous builder version, which install refuses.
MCP_ENTRY_PREFIX = "llm-wiki-"
# The project-memory skill and recall script that install wrote before vault-graph request S11.
# Install and uninstall delete them only when they are builder output: a recorded hash or a released asset.
LEGACY_RECALL_PLACEHOLDER = "@VAULT_BUILDER_PYTHON@"
_LEGACY_SKILL_SHA256 = frozenset(
    {
        "07644af358af7f197655879eef103643cf56adeb05c48d819240f6f6f33ff3fc",
        "aa1c6e4a4724f0cd1a8b7e48c8f915198597bfa964450721f9062f14856bcf85",
    }
)
# Hook launcher assets of earlier builders (v3.0.0 to v4.1.0, then main before 5.0.0). Install replaces them.
_PREVIOUS_LAUNCHER_SHA256 = frozenset(
    {
        "53e8fe4f60ddb3b330ae583de42a5c219385b4401d3bd6a6836ddffe062a995d",
        "b3fad1d24b7dbb40652a340d5c973aa28715e6af517217ae3f74e37cd99984be",
    }
)
# recall.sh assets with the placeholder still on their single PYTHON= line. Install replaced only that line.
_LEGACY_RECALL_TEMPLATE_SHA256 = frozenset(
    {
        "f4a9ffa6fa83141c195ed3b1c78aeb870668623591565b6bb1a658a6e782cc0c",
        "cf68cf87c893dea8c2e53508eaa5785c811ae2e6f8590ebba978671738f6f5e6",
        "bcef1bc75f97bf67a2fc08bea662717741164b30dfcaf65f7c27b2c69677a50e",
        "39e2da33f4e1a8c4d1ff501cc887136533e071861dc075fd257903dadf710030",
        "7fecb92677fb30f31be89444a166384577b87a501138c340d5c834ebbe6d0f6f",
    }
)
_ROUTER_ASSET = Path("tools/vault_migration/assets/llm-wiki-router/llm_wiki_router.py")
_CATALOG_PATH = "tools/vault_migration/contracts/v6.json"
_REFUSED_EXISTING_ACTIONS = frozenset({"create", "replace", "review-required", "invalid"})
_REFUSED_RESTORE_ACTIONS = frozenset({"review-required", "invalid"})
_VAULT_MAP_VERSION = 2  # the version install writes
_DEFAULT_ROLE = "personal"  # role of a new vault entry when install has no --role
_VAULT_MAP_READ_VERSIONS = (1, 2)


def _vault_map_version_ok(version: object) -> bool:
    """Only a JSON integer 1 or 2, as the router and memory-scan read it (not true or 1.0)."""
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
    ".claude/skills/session-capture/SKILL.md",
    "scripts/release_gate.sh",
    ".github/workflows/qa.yml",
)
_COPY_TREES = (
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
    "raw/sessions",
    "scratch/drafts",
    "scratch/reports",
    "scratch/review",
    "docs",
)
_KEEP_FILES = (
    "raw/sources/.gitkeep",
    "raw/assets/.gitkeep",
    "raw/imports/.gitkeep",
    "raw/sessions/.gitkeep",
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
    def legacy_skill(self) -> Path:
        """The project-memory skill an earlier install wrote. Install now deletes it."""
        return self.config_dir / "skills" / "project-memory" / "SKILL.md"

    @property
    def legacy_recall(self) -> Path:
        """The recall script an earlier install wrote. Install now deletes it."""
        return self.config_dir / "skills" / "project-memory" / "scripts" / "recall.sh"

    @property
    def legacy_files(self) -> tuple[Path, Path]:
        return (self.legacy_skill, self.legacy_recall)

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
class _HooksPlan:
    """--with-hooks: the launcher copy, the recorded vg path and the Claude Code settings hook entries.

    These files are not manifest records: uninstall finds the hook entries by the launcher path.
    """

    launcher: _FilePlan
    config_path: Path
    config_contents: bytes | None  # None when vg was not found or the file already has these bytes
    settings_path: Path
    settings_action: str  # create | update | unchanged
    config_delete: bool = False  # True when no vg was found and the recorded vg no longer runs


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
    role: str
    mcp_entry: dict[str, object]
    mcp_entry_action: str
    notes: list[str] = field(default_factory=list)
    remove_legacy_entry: bool = False  # mcpServers.llm-wiki is ours and goes in the same write
    restore_plan: MigrationPlan | None = None  # set for a Vault without a manifest
    hooks: _HooksPlan | None = None  # set by --with-hooks
    legacy_deletes: list[Path] = field(default_factory=list)  # builder-owned project-memory files to delete

    @property
    def mcp_key(self) -> str:
        return MCP_SERVER_KEY


def install(
    target: Path,
    *,
    name: str | None = None,
    with_stenc: bool = False,
    with_hooks: bool = False,
    role: str | None = None,
    dry_run: bool = False,
    builder_root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    python: str | None = None,
    out: TextIO | None = None,
    upgrade_pending: bool = False,
) -> int:
    """Install or register a Vault. Raise InstallError after restoring the prior state.

    upgrade_pending (dry run only): setup applies this vault's audit plan before the real install, so the precheck
    skips the manifest schema and audit checks and runs every other check.
    """
    if upgrade_pending and not dry_run:
        raise ValueError("upgrade_pending is only for a dry run.")
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
        with_hooks=with_hooks,
        role=role,
        builder_root=builder_root,
        environ=environment,
        python=python,
        upgrade_pending=upgrade_pending,
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
    with_hooks: bool = False,
    role: str | None = None,
    builder_root: Path,
    environ: Mapping[str, str],
    python: str,
    upgrade_pending: bool = False,
) -> _InstallPlan:
    """role None keeps an existing vault's role and gives a new vault entry the role personal."""
    if not os.path.isabs(python):
        raise InstallError(f"Interpreter path must be absolute: {python}")
    if role is not None:
        role = role.strip()
        if not role:
            raise InstallError("--role must be a non-empty string, for example personal, project, or team.")
    user = resolve_user_paths(environ)
    target = _absolute_target(target)
    state, empty_dir = _target_state(target, builder_root, upgrade_pending=upgrade_pending)
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
    for path in (user.router, user.vault_map, user.mcp_config):
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
    remove_legacy = is_legacy_entry(servers.get(LEGACY_MCP_SERVER_KEY), user.router)
    existing_entry = servers.get(MCP_SERVER_KEY)
    if existing_entry is None:
        entry_action = "rename" if remove_legacy else "create"
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
    if remove_legacy:
        notes.append(
            f"The server is now named {MCP_SERVER_KEY}, so its tools are mcp__{MCP_SERVER_KEY}__*. Update permission "
            f"rules and skills that name mcp__{LEGACY_MCP_SERVER_KEY}__ by hand."
        )

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
        vault_role = role or _DEFAULT_ROLE
        vaults[vault_name] = {"path": str(target), "role": vault_role}
        map_action = "add"
    else:
        existing_path = existing_vault.get("path") if isinstance(existing_vault, dict) else None
        if not isinstance(existing_path, str):
            raise InstallError(
                f"Vault map {user.vault_map}: vault {vault_name} has no path. Fix it or choose --name."
            )
        old_role = existing_vault.get("role")
        vault_role = role or (old_role if isinstance(old_role, str) and old_role.strip() else _DEFAULT_ROLE)
        if _same_path(Path(existing_path).expanduser(), target):
            if old_role == vault_role:
                map_action = "unchanged"
            else:
                existing_vault["role"] = vault_role
                map_action = "update"
        elif not Path(existing_path).expanduser().exists():
            vaults[vault_name] = {"path": str(target), "role": vault_role}
            map_action = "replace"
            notes.append(f"Replacing vault map entry {vault_name}: its vault no longer exists ({existing_path}).")
        else:
            raise InstallError(
                f"Vault map {user.vault_map} already has vault {vault_name} at {existing_path}. "
                "Choose a different name with --name."
            )

    router_asset = (builder_root / _ROUTER_ASSET).read_bytes()
    # A router an earlier install wrote (a vault manifest records its hash) is replaced, so the new server name reaches it.
    router_hashes = _recorded_hashes(user, user.router, [target, *_vault_paths(vaults)])
    files = [
        _file_plan(
            user.router,
            router_asset,
            0o644,
            lambda current: current == router_asset or hashlib.sha256(current).hexdigest() in router_hashes,
        ),
    ]
    for item in files:
        if item.action == "keep":
            notes.append(f"Kept existing file with unknown content (not replaced): {item.path}")

    legacy_deletes = _legacy_plan(user, [target, *_vault_paths(vaults)], notes)

    hooks_plan = _hooks_precheck(user, builder_root, environ, python, notes) if with_hooks else None

    restore_plan = None
    if state == "existing" and not upgrade_pending:
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
        role=vault_role,
        mcp_entry=entry,
        mcp_entry_action=entry_action,
        notes=notes,
        remove_legacy_entry=remove_legacy,
        restore_plan=restore_plan,
        hooks=hooks_plan,
        legacy_deletes=legacy_deletes,
    )


def _hooks_precheck(
    user: UserPaths, builder_root: Path, environ: Mapping[str, str], python: str, notes: list[str]
) -> _HooksPlan:
    """Read the Claude Code settings file and plan the hook files. Only called with --with-hooks."""
    launcher = _hooks.launcher_path(user.config_dir)
    config_path = _hooks.hooks_config_path(user.config_dir)
    settings_path = _hooks.settings_path(user.config_dir)
    for path in (launcher, config_path, settings_path):
        _require_regular_or_absent(path)
    try:
        settings = _hooks.load_settings(settings_path)
    except _hooks.HooksError as exc:
        raise InstallError(f"{exc} Fix it, then run install --with-hooks again.") from exc
    _updated, changed = _hooks.add_launcher_hooks(settings, python, launcher)
    if not settings_path.exists():
        settings_action = "create"
    else:
        settings_action = "update" if changed else "unchanged"
    asset = (builder_root / _hooks.LAUNCHER_ASSET).read_bytes()
    launcher_plan = _file_plan(
        launcher,
        asset,
        0o644,
        lambda current: current == asset or hashlib.sha256(current).hexdigest() in _PREVIOUS_LAUNCHER_SHA256,
    )
    if launcher_plan.action == "keep":
        notes.append(f"Kept existing file with unknown content (not replaced): {launcher}")
    vg = _hooks.find_vg(environ)
    config_contents: bytes | None = None
    config_delete = False
    if vg is None:
        notes.append(
            "vg was not found (VAULT_GRAPH_BIN or PATH), so no vg path was recorded. Until vg is on PATH or "
            "install --with-hooks runs again with vg available, SessionStart adds nothing and the capture hooks "
            "use direct capture."
        )
        config_delete = _hooks.recorded_vg_is_gone(config_path)
    else:
        wanted = _hooks.hooks_config_bytes(vg)
        if not config_path.exists() or config_path.read_bytes() != wanted:
            config_contents = wanted
    return _HooksPlan(
        launcher=launcher_plan,
        config_path=config_path,
        config_contents=config_contents,
        settings_path=settings_path,
        settings_action=settings_action,
        config_delete=config_delete,
    )


def _absolute_target(target: Path) -> Path:
    absolute = Path(os.path.abspath(target))
    if absolute == Path(absolute.anchor):
        raise InstallError("Target must not be the filesystem root.")
    if absolute.is_symlink():
        raise InstallError(f"Target must not be a symlink: {absolute}")
    return absolute.parent.resolve() / absolute.name


def _target_state(target: Path, builder_root: Path, *, upgrade_pending: bool = False) -> tuple[str, bool]:
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
        if manifest.schema_version != MANIFEST_SCHEMA_VERSION and not upgrade_pending:
            raise InstallError(
                f"Target manifest is schema version {manifest.schema_version}. Run vault-builder setup, which "
                "upgrades it when no file needs review, or audit and apply it yourself: "
                f"vault-builder audit --target {shlex.quote(str(target))} --plan-out <plan.json>"
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
        user.relative(path) for path in (*user.legacy_files, user.router, user.vault_map, user.mcp_config)
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
        if record["kind"] == "json-entry" and key_path not in SERVER_KEY_PATHS:
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


def _vault_paths(vaults: Mapping[str, object]) -> list[Path]:
    paths = []
    for value in vaults.values():
        raw = value.get("path") if isinstance(value, dict) else None
        if isinstance(raw, str) and os.path.isabs(os.path.expanduser(raw)):
            paths.append(Path(os.path.expanduser(raw)))
    return paths


def _recorded_hashes(user: UserPaths, path: Path, vaults: list[Path]) -> set[str]:
    """The sha256 values that the manifests of these vaults record for the outside file path."""
    location = user.relative(path)
    hashes: set[str] = set()
    for vault in vaults:
        try:
            manifest = load_manifest(vault)
        except (OSError, ValueError):
            continue
        if manifest is None:
            continue
        for record in manifest.external_records:
            if record["kind"] == "file" and record["location"] == location and isinstance(record.get("sha256"), str):
                hashes.add(str(record["sha256"]))
    return hashes


def is_legacy_entry(entry: object, router: Path) -> bool:
    """True when an entry under the previous name is ours: its args are exactly ["-I", <router>]."""
    return isinstance(entry, dict) and entry.get("args") == ["-I", str(router)]


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


def _legacy_plan(user: UserPaths, vaults: list[Path], notes: list[str]) -> list[Path]:
    """Return the project-memory files to delete. A changed copy stays and gets a note."""
    deletes: list[Path] = []
    for path in user.legacy_files:
        if not path.exists() and not path.is_symlink():
            continue
        if is_legacy_builder_file(path, _recorded_hashes(user, path, vaults)):
            deletes.append(path)
        else:
            notes.append(
                f"Kept {path}: it is not a file an earlier install wrote. vault-builder no longer installs the "
                "project-memory skill; delete the file yourself when you no longer need it."
            )
    return deletes


def is_legacy_builder_file(path: Path, recorded: set[str]) -> bool:
    """True when the project-memory skill or recall script is a regular file an earlier install wrote."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
    except FileNotFoundError:
        return False
    current = path.read_bytes()
    if hashlib.sha256(current).hexdigest() in recorded:
        return True
    if path.name == "SKILL.md":
        return hashlib.sha256(current).hexdigest() in _LEGACY_SKILL_SHA256
    return _legacy_recall_template_sha256(current) in _LEGACY_RECALL_TEMPLATE_SHA256


def _legacy_recall_template_sha256(current: bytes) -> str | None:
    """Put the single PYTHON= line back to the asset placeholder and hash the result."""
    try:
        lines = current.decode("utf-8").split("\n")
    except UnicodeError:
        return None
    candidates = [index for index, line in enumerate(lines) if line.startswith("PYTHON=")]
    if len(candidates) != 1:
        return None
    lines[candidates[0]] = f"PYTHON={LEGACY_RECALL_PLACEHOLDER}"
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def remove_legacy_directories(user: UserPaths) -> None:
    """Remove the project-memory folders when deleting the legacy files left them empty."""
    project_memory = user.config_dir / "skills" / "project-memory"
    for directory in (project_memory / "scripts", project_memory):
        try:
            directory.rmdir()
        except OSError:
            continue
        fsync_directory(directory.parent)


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
            f"Existing vault needs migration first ({len(pending)} actions: {shown}). Run vault-builder setup, "
            "which upgrades it when no file needs review, or run "
            f"vault-builder audit --target {shlex.quote(str(target))} --plan-out <plan.json>, "
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
    remove_legacy_directories(plan.user)


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
        legacy_locations = {plan.user.relative(path) for path in plan.user.legacy_files}
        identities = {(str(record["location"]), str(record.get("key_path", ""))) for record in records}
        kept = [
            dict(record)
            for record in manifest.external_records
            if (str(record["location"]), str(record.get("key_path", ""))) not in identities
            # The server entry record moves to the new key path.
            and not (record["kind"] == "json-entry" and str(record.get("key_path", "")) in SERVER_KEY_PATHS)
            # Install no longer writes the project-memory files, so their records go.
            and str(record["location"]) not in legacy_locations
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
    remove_legacy_directories(plan.user)


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
    """Write router, vault map and server entry, delete legacy project-memory files. Return the manifest records.

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
    for path in plan.legacy_deletes:
        changes.delete(path)
    if plan.vault_map_write:
        changes.write(user.vault_map, _json_bytes(plan.vault_map, indent=2), 0o644)
    records.append({"kind": "config", "location": user.relative(user.vault_map), "shared": True})
    if plan.mcp_entry_action != "unchanged" or plan.remove_legacy_entry:
        # Re-read just before writing: Claude Code may have rewritten the file since the precheck.
        config = _load_json_object(user.mcp_config, "MCP settings file")
        current = dict(config.get("mcpServers", {}))  # type: ignore[arg-type]
        legacy = plan.remove_legacy_entry and is_legacy_entry(current.get(LEGACY_MCP_SERVER_KEY), user.router)
        servers: dict[str, object] = {}
        for key, value in current.items():
            if legacy and key == LEGACY_MCP_SERVER_KEY:
                if plan.mcp_key not in current:
                    servers[plan.mcp_key] = plan.mcp_entry  # the renamed entry keeps its place
                continue
            servers[key] = value
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
    if plan.hooks is not None:
        _apply_hooks(plan.hooks, plan.python, changes)
    return records


def _apply_hooks(hooks_plan: _HooksPlan, python: str, changes: _OutsideChanges) -> None:
    """Write the launcher and the vg record before the settings entries that run the launcher."""
    launcher = hooks_plan.launcher
    if launcher.action in {"create", "replace"}:
        changes.write(launcher.path, launcher.contents, launcher.mode)
    if hooks_plan.config_contents is not None:
        changes.write(hooks_plan.config_path, hooks_plan.config_contents, 0o644)
    elif hooks_plan.config_delete:
        changes.delete(hooks_plan.config_path)
    if hooks_plan.settings_action == "unchanged":
        return
    # Re-read just before writing: Claude Code may have rewritten the file since the precheck.
    path = hooks_plan.settings_path
    settings, changed = _hooks.add_launcher_hooks(_hooks.load_settings(path), python, launcher.path)
    if changed:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        changes.write(path, _hooks.settings_bytes(settings), mode)


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
    print(version_label(), file=stream)
    print(f"Vault name: {plan.name}", file=stream)
    print(f"Vault role: {plan.role}", file=stream)
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
    for path in plan.legacy_deletes:
        print(f"  delete {path}: vault-builder no longer installs the project-memory skill", file=stream)
    print(f"  {plan.vault_map_action} vault {plan.name} in {plan.user.vault_map}", file=stream)
    if plan.mcp_entry_action == "rename":
        print(
            f"  rename mcpServers.{LEGACY_MCP_SERVER_KEY} to mcpServers.{plan.mcp_key} in {plan.user.mcp_config}",
            file=stream,
        )
    else:
        print(f"  {plan.mcp_entry_action} mcpServers.{plan.mcp_key} in {plan.user.mcp_config}", file=stream)
        if plan.remove_legacy_entry:
            print(f"  remove mcpServers.{LEGACY_MCP_SERVER_KEY} from {plan.user.mcp_config}", file=stream)
    if plan.hooks is not None:
        print(f"  {plan.hooks.launcher.action} {plan.hooks.launcher.path}", file=stream)
        if plan.hooks.config_contents is not None:
            print(f"  write vg path to {plan.hooks.config_path}", file=stream)
        elif plan.hooks.config_delete:
            print(f"  delete {plan.hooks.config_path}: its vg is no longer an executable file", file=stream)
        print(
            f"  {plan.hooks.settings_action} hooks SessionStart, PreCompact, SessionEnd in {plan.hooks.settings_path}",
            file=stream,
        )
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
    print(version_label(), file=stream)
    print(f"Vault name: {plan.name}", file=stream)
    print(f"MCP server: mcpServers.{plan.mcp_key} ({plan.mcp_entry_action}) in {plan.user.mcp_config}", file=stream)
    print(f"Router: {plan.user.router}", file=stream)
    print(f"Vault map: {plan.user.vault_map}", file=stream)
    for path in plan.legacy_deletes:
        print(f"Deleted {path}: vault-builder no longer installs the project-memory skill.", file=stream)
    if plan.hooks is not None:
        print(
            f"Hooks: SessionStart, PreCompact, SessionEnd ({plan.hooks.settings_action}) in "
            f"{plan.hooks.settings_path}, launcher {plan.hooks.launcher.path}",
            file=stream,
        )
    for note in plan.notes:
        print(f"Note: {note}", file=stream)
    print(
        f"Restart Claude Code, check that /mcp lists one {MCP_SERVER_KEY} server, then run "
        f"vault-builder doctor {shlex.quote(str(plan.target))}.",
        file=stream,
    )
    print("Next:", file=stream)
    print(f"  cd {plan.target}", file=stream)
    print(f"  {python} tools/wiki/cli.py lint --report", file=stream)
    print(f"  {python} tools/wiki/cli.py health", file=stream)
