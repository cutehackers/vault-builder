"""vault-builder setup: one run from the precheck to doctor (spec 4.4).

Steps:

1. Precheck. Writes no file. Parse the options, read vault-map.json, audit every existing vault, run the install
   precheck of every vault that needs no upgrade (install --dry-run --with-hooks), check every --repo, and check
   that Claude Code is not running (decision 5). Any failure stops the run.
   A vault whose manifest schema is older, or whose audit plans create or replace actions, is upgraded in step 2.
   When any vault's audit has a review-required or invalid action, setup lists every such path of every vault and
   stops before writing, because only a person can decide those files.
2. For every vault, in order: apply its audit plan when it needs an upgrade (the same transaction as the apply
   command, with its rollback), then install --with-hooks. The environment is passed on unchanged, so install
   records a vg found through VAULT_GRAPH_BIN or PATH and records nothing when there is none (installing.py).
3. One link per repository: the shared vaults and that repository's project vault.
4. doctor without a path.

setup installs only vault-builder files and settings. It installs no other tool.

The vaults of a run are every vault in vault-map.json (install runs only the outside-vault steps for them),
then the new ones: a personal vault at $HOME/vault when the map has no vault and no new vault is personal,
the --vault vaults, and a project vault for each repository that is not linked yet
($HOME/vaults/<repository folder name>, decision 11). Running the same command again changes nothing.

Interactive mode: a terminal (/dev/tty) and no --yes. Questions 1 to 4 are asked only when no --vault, --repo
or --project-vault is given. The Claude Code process check and /dev/tty are reached only through Externals, so
tests replace them.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tools.vault_migration import installing, linking, map_settings, roles, uninstalling
from tools.vault_migration.applying import apply_plan
from tools.vault_migration.catalogs import load_catalog
from tools.vault_migration.manifests import load_manifest
from tools.vault_migration.models import MigrationAction, MigrationPlan
from tools.vault_migration.planning import _CURRENT_CATALOG_PATH, _REQUIRED_TOPOLOGY, audit_vault
from tools.wiki.lib.vault_manifest import MANIFEST_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[2]
PERSONAL_NAME = "personal"
EXIT_FAILED = 1
EXIT_MISSING_OPTION = 2
EXIT_CLAUDE_RUNNING = 3
_INSTALL_ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)
_INSTALL_SUMMARY = ("LLM Wiki Vault", "Vault name:", "MCP server:", "Hooks:", "Note:")
_COMMAND = "setup"
_UPGRADE_ACTIONS = frozenset({"create", "replace"})
_REVIEW_ACTIONS = frozenset({"review-required", "invalid"})
_SHOWN_REVIEW_PATHS = 10


class Tty(Protocol):
    def write(self, value: str, /) -> int: ...

    def flush(self) -> None: ...

    def readline(self) -> str: ...


def _run(
    argv: Sequence[str], env: Mapping[str, str], *, capture: bool, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an external tool. A missing program is exit 127 and a timeout is exit 124."""
    try:
        return subprocess.run(
            list(argv), env=dict(env), text=True, capture_output=capture, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{argv[0]}: command not found\n")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"{argv[0]}: timed out after {timeout} seconds\n")


def _claude_running(env: Mapping[str, str]) -> bool:
    """True when a process named claude runs (spec 7 risk 2: IDE and desktop processes are not checked)."""
    result = _run(["pgrep", "-x", "claude"], env, capture=True, timeout=10)
    return result.returncode == 0


def _open_tty() -> Tty | None:
    try:
        return open("/dev/tty", "r+", encoding="utf-8")  # noqa: SIM115 - kept open for the whole run
    except OSError:
        return None


@dataclass(frozen=True)
class Externals:
    claude_running: Callable[[Mapping[str, str]], bool] = _claude_running
    open_tty: Callable[[], Tty | None] = _open_tty


@dataclass(frozen=True)
class SetupOptions:
    vaults: tuple[str, ...] = ()  # NAME=PATH
    roles: tuple[str, ...] = ()  # NAME=ROLE
    repos: tuple[str, ...] = ()
    project_vault: str | None = None  # NAME=PATH
    default_write: str | None = None
    yes: bool = False
    dry_run: bool = False
    create_project_vault: bool = True  # False when the interactive answer to question 3 is no


def options_from_args(args: argparse.Namespace) -> SetupOptions:
    return SetupOptions(
        vaults=tuple(args.vault or ()),
        roles=tuple(args.role or ()),
        repos=tuple(args.repo or ()),
        project_vault=args.project_vault,
        default_write=args.default_write,
        yes=args.yes,
        dry_run=args.dry_run,
    )


def role_help() -> str:
    """The --role help: every known role from roles.py with its description."""
    known = "; ".join(f"{role.name}: {role.description}" for role in roles.ROLES)
    return (
        "NAME=ROLE, the role of one vault. Repeat for more vaults. Known roles: "
        f"{known}. Any other non-empty string is accepted and linked as a shared vault. "
        "Without --role a new vault is personal and a registered vault keeps its role."
    )


class _Stop(Exception):
    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Vault:
    name: str
    path: Path
    role: str | None  # None: a registered vault keeps its role
    registered: bool
    registered_role: str | None = None

    @property
    def final_role(self) -> str:
        if self.role is not None:
            return self.role
        return self.registered_role or installing._DEFAULT_ROLE


@dataclass
class _Repo:
    path: Path
    root: Path
    linked_project: str | None  # project id when vault-map.json already links this repository
    project_vaults: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Upgrade:
    """An existing vault that install would refuse until its audit plan is applied."""

    vault: _Vault
    plan: MigrationPlan
    schema_version: int

    def counts(self) -> str:
        created = sum(1 for action in self.plan.actions if action.action == "create")
        replaced = sum(1 for action in self.plan.actions if action.action == "replace")
        return f"{created} files to create, {replaced} to replace"


@dataclass
class _Plan:
    vaults: list[_Vault]
    repos: list[_Repo]

    def names(self) -> list[str]:
        return [vault.name for vault in self.vaults]

    def link_names(self, repo: _Repo) -> list[str]:
        shared = [vault.name for vault in self.vaults if roles.is_shared(vault.final_role)]
        return list(dict.fromkeys([*shared, *repo.project_vaults]))


def _pair(value: str, option: str, what: str) -> tuple[str, str]:
    name, sep, rest = value.partition("=")
    if not sep or not name.strip():
        raise _Stop(f"{option} needs NAME={what}, got {value!r}.", EXIT_MISSING_OPTION)
    return name.strip(), rest


def _path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


class _Prompter:
    def __init__(self, tty: Tty) -> None:
        self.tty = tty

    def say(self, line: str) -> None:
        self.tty.write(line + "\n")
        self.tty.flush()

    def ask(self, question: str, default: str = "") -> str:
        self.tty.write(f"{question} ")
        self.tty.flush()
        answer = self.tty.readline().strip()
        return answer or default

    def confirm(self, question: str, default: bool) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
        if answer.startswith("y"):
            return True
        if answer.startswith("n"):
            return False
        return default

    def wait_enter(self, message: str) -> bool:
        """False on end of input."""
        self.tty.write(f"{message} ")
        self.tty.flush()
        return self.tty.readline() != ""


def setup(
    options: SetupOptions,
    *,
    environ: Mapping[str, str] | None = None,
    externals: Externals | None = None,
    builder_root: Path = ROOT,
    cwd: Path | None = None,
) -> int:
    environment = dict(os.environ if environ is None else environ)
    tools = externals or Externals()
    try:
        return _setup(options, environment, tools, builder_root, Path.cwd() if cwd is None else cwd)
    except _Stop as exc:
        print(f"{_COMMAND}: {exc}", file=sys.stderr)
        return exc.code


def _setup(options: SetupOptions, env: dict[str, str], tools: Externals, builder_root: Path, cwd: Path) -> int:
    # Step 1: precheck. Nothing below writes until the plan is confirmed and Claude Code is closed.
    _step(1, "precheck (no file is written)")
    try:
        user = installing.resolve_user_paths(env)
    except installing.InstallError as exc:
        raise _Stop(str(exc)) from None
    data = _read_map(user.vault_map, env)
    prompter: _Prompter | None = None
    if not options.yes:
        tty = tools.open_tty()
        prompter = _Prompter(tty) if tty is not None else None
    if prompter is None and not options.yes and not options.dry_run:
        raise _Stop("No terminal for questions: give --yes to run without questions.", EXIT_MISSING_OPTION)

    plan_options = options
    if prompter is not None and not (options.vaults or options.repos or options.project_vault):
        plan_options = _ask(prompter, options, data, env, cwd)
    plan = _build_plan(plan_options, data, user.home)
    _print_plan(plan, options.dry_run)
    upgrades = _audit_vaults(plan.vaults, builder_root)
    _print_upgrades(upgrades)
    for vault in plan.vaults:
        _precheck_vault(vault, env, builder_root, upgrade_pending=vault.name in upgrades)
    if options.dry_run:
        if tools.claude_running(env):
            print(f"{_COMMAND}: note: Claude Code is running. Quit it before the real run.")
        print(f"{_COMMAND}: dry run, the precheck passed and nothing was written.")
        return 0
    if prompter is not None and not prompter.confirm("Proceed?", True):
        raise _Stop("Stopped before any change.")
    _require_claude_closed(tools, env, prompter)

    # Step 2: install every vault with hooks, in plan order.
    _step(2, "upgrade and install vaults with hooks")
    for vault in plan.vaults:
        upgrade = upgrades.get(vault.name)
        if upgrade is not None:
            _upgrade_vault(upgrade, builder_root)
        _install_vault(vault, env, builder_root)

    # Step 3: one link per repository, then the default write vault.
    _step(3, "link repositories")
    for repo in plan.repos:
        names = plan.link_names(repo)
        if not names:
            print(f"{_COMMAND}: no vault to link for {repo.root}.")
            continue
        if linking.link(names, repo=str(repo.path), environ=env) != 0:
            raise _Stop(
                f"link failed for {repo.root}. Fix the cause shown above, then run the same command again.",
            )
    _default_write(plan, options.default_write, env, prompter)

    failed: list[str] = []
    # Step 4: doctor.
    _step(4, "doctor")
    try:
        doctor_code = uninstalling.doctor(None, builder_root=builder_root, environ=env, out=sys.stdout)
    except _INSTALL_ERRORS as exc:
        print(f"doctor: {exc}", file=sys.stderr)
        doctor_code = 1
    if doctor_code != 0:
        failed.append("doctor found problems. Run the fix command on each Problem line above.")

    for message in failed:
        print(f"{_COMMAND}: {message}", file=sys.stderr)
    print("Restart Claude Code (every terminal, IDE and app session) so it loads the new MCP server and hooks.")
    print("Then run /mcp in Claude Code and check that vault-wiki is listed as connected.")
    print("To link another repository later: vault-builder link --repo <path>")
    return EXIT_FAILED if failed else 0


def _step(number: int, text: str) -> None:
    print(f"{_COMMAND}: [{number}/4] {text}")


def _read_map(path: Path, env: Mapping[str, str]) -> dict:
    if not path.exists():
        return {"version": 2, "vaults": {}, "projects": {}}
    try:
        _path_unused, data = linking._load(env)
    except linking._Refused as exc:
        raise _Stop(str(exc)) from None
    return data


def _ask(prompter: _Prompter, options: SetupOptions, data: dict, env: Mapping[str, str], cwd: Path) -> SetupOptions:
    """Questions 1 to 4 of spec 4.4. Returns options with the answers."""
    home = Path(env["HOME"])
    vaults: list[str] = []
    roles_given: list[str] = list(options.roles)
    registered = data.get("vaults", {})
    if registered:
        prompter.say("Registered vaults:")
        for name, entry in registered.items():
            prompter.say(f"  {name} (role {entry.get('role')}): {entry.get('path')}")
    else:
        path = prompter.ask(f"Personal vault path [{home / 'vault'}]", str(home / "vault"))
        vaults.append(f"{PERSONAL_NAME}={path}")
        roles_given.append(f"{PERSONAL_NAME}={installing._DEFAULT_ROLE}")
    root = linking._router().main_worktree_root(cwd)
    if root is not None:
        answer = prompter.ask(f"Repository to link [{root}]", str(root))
    else:
        answer = prompter.ask("Repository to link (Enter to skip)", "")
    repos = (answer,) if answer else ()
    project_vault: str | None = None
    create_project_vault = True
    if answer:
        repo_root = linking._router().main_worktree_root(_path(answer))
        linked = repo_root is not None and not isinstance(
            linking._router().find_project(_path(answer), data), linking._router().Stop
        )
        if repo_root is not None and not linked:
            default = home / "vaults" / repo_root.name
            if prompter.confirm(f"Create project vault {repo_root.name} at {default}?", True):
                project_vault = f"{repo_root.name}={default}"
            else:
                create_project_vault = False
    while prompter.confirm("Add another vault?", False):
        path = prompter.ask("Vault path:")
        if not path:
            continue
        name = prompter.ask(f"Vault name [{_path(path).name}]", _path(path).name)
        prompter.say("Known roles:")
        for role in roles.ROLES:
            prompter.say(f"  {role.name}: {role.description}")
        prompter.say("Another role name is accepted and linked as a shared vault.")
        role = prompter.ask(f"Role [{installing._DEFAULT_ROLE}]", installing._DEFAULT_ROLE)
        vaults.append(f"{name}={path}")
        roles_given.append(f"{name}={role}")
    return SetupOptions(
        vaults=tuple(vaults),
        roles=tuple(roles_given),
        repos=repos,
        project_vault=project_vault,
        default_write=options.default_write,
        yes=options.yes,
        dry_run=options.dry_run,
        create_project_vault=create_project_vault,
    )


def _build_plan(options: SetupOptions, data: dict, home: Path) -> _Plan:
    given_roles: dict[str, str] = {}
    for value in options.roles:
        name, role = _pair(value, "--role", "ROLE")
        given_roles[name] = role
    given: list[tuple[str, Path]] = []
    for value in options.vaults:
        name, raw = _pair(value, "--vault", "PATH")
        if not raw.strip():
            raise _Stop(f"--vault needs NAME=PATH, got {value!r}.", EXIT_MISSING_OPTION)
        given.append((name, _path(raw)))
    project_vault: tuple[str, Path] | None = None
    if options.project_vault is not None:
        name, raw = _pair(options.project_vault, "--project-vault", "PATH")
        if not raw.strip():
            raise _Stop(f"--project-vault needs NAME=PATH, got {options.project_vault!r}.", EXIT_MISSING_OPTION)
        if len(options.repos) != 1:
            raise _Stop("--project-vault needs exactly one --repo.", EXIT_MISSING_OPTION)
        project_vault = (name, _path(raw))

    vaults: dict[str, _Vault] = {}
    registered = data.get("vaults", {})
    for name, entry in registered.items():
        path = Path(os.path.expanduser(str(entry.get("path", ""))))
        if not path.is_absolute() or not path.is_dir():
            print(f"{_COMMAND}: note: vault {name} points at {path}, which is not a folder; it is skipped. "
                  "Run vault-builder doctor.")
            continue
        vaults[name] = _Vault(name, path, given_roles.get(name), True, entry.get("role"))

    new: list[_Vault] = []
    for name, path in given:
        if name in vaults and vaults[name] not in new:
            vaults[name].path = path
            continue
        if any(vault.name == name for vault in new):
            raise _Stop(f"Vault name {name} is given twice. Give each vault its own name with --vault NAME=PATH.")
        new.append(_Vault(name, path, given_roles.get(name), False))
    if not registered and not any(vault.final_role == installing._DEFAULT_ROLE for vault in new):
        if PERSONAL_NAME not in {vault.name for vault in new}:
            new.insert(0, _Vault(PERSONAL_NAME, home / "vault", given_roles.get(PERSONAL_NAME), False))

    router = linking._router()
    repos: list[_Repo] = []
    gives_project = any(vault.final_role == roles.PROJECT_ROLE for vault in new)
    for raw in options.repos:
        start = _path(raw)
        root = router.main_worktree_root(start)
        if root is None:
            raise _Stop(f"{start} is not inside a git repository.")
        match = router.find_project(start, data)
        if isinstance(match, router.Stop):
            if match.ids:
                raise _Stop(f"{match.reason} Fix the link config so one project matches.")
            if root.name in data.get("projects", {}):
                raise _Stop(
                    f"Project {root.name} already belongs to another repository "
                    f"({data['projects'][root.name]['root']}). Link this repository with "
                    "vault-builder link --repo <path> --project <id>."
                )
            repo = _Repo(start, root, None)
        else:
            repo = _Repo(start, root, match.project_id)
            linked = data["projects"][match.project_id]["vaults"]
            repo.project_vaults = [
                name for name in linked if registered.get(name, {}).get("role") == roles.PROJECT_ROLE and name in vaults
            ]
        if project_vault is not None:
            name, path = project_vault
            _add_project_vault(vaults, new, name, path, given_roles)
            repo.project_vaults.append(name)
        elif repo.linked_project is None and options.create_project_vault:
            if gives_project:
                repo.project_vaults.extend(vault.name for vault in new if vault.final_role == roles.PROJECT_ROLE)
            else:
                name = root.name
                _add_project_vault(vaults, new, name, home / "vaults" / name, given_roles)
                repo.project_vaults.append(name)
        repos.append(repo)

    plan = _Plan([*vaults.values(), *new], repos)
    names = plan.names()
    for name in given_roles:
        if name not in names:
            raise _Stop(f"--role {name}=...: no vault {name} in --vault, --project-vault or the link config.",
                        EXIT_MISSING_OPTION)
    seen: dict[Path, str] = {}
    for vault in plan.vaults:
        other = seen.setdefault(vault.path, vault.name)
        if other != vault.name:
            raise _Stop(
                f"Vaults {other} and {vault.name} have the same path {vault.path}. "
                "Give each vault its own folder with --vault NAME=PATH."
            )
    if options.default_write is not None and not any(
        options.default_write in plan.link_names(repo) for repo in plan.repos
    ):
        raise _Stop(
            f"--default-write {options.default_write}: that vault is not linked to any --repo of this run.",
            EXIT_MISSING_OPTION,
        )
    return plan


def _add_project_vault(
    vaults: dict[str, _Vault], new: list[_Vault], name: str, path: Path, given_roles: Mapping[str, str]
) -> None:
    """A registered vault of that name is reused. Otherwise a new vault with role project is planned."""
    if name in vaults or any(vault.name == name for vault in new):
        return
    new.append(_Vault(name, path, given_roles.get(name, roles.PROJECT_ROLE), False))


def _print_plan(plan: _Plan, dry_run: bool) -> None:
    print(f"{_COMMAND}: plan{' (dry run)' if dry_run else ''}:")
    for vault in plan.vaults:
        state = "registered" if vault.registered else "new"
        print(f"  vault {vault.name} ({state}, role {vault.final_role}): {vault.path}")
        if roles.known_role(vault.final_role) is None and vault.final_role.strip():
            print(f"    role {vault.final_role} is not a known role; it is linked as a shared vault.")
    for repo in plan.repos:
        print(f"  repository {repo.root}: link {', '.join(plan.link_names(repo)) or 'nothing'}")


def _audit_vaults(vaults: Sequence[_Vault], builder_root: Path) -> dict[str, _Upgrade]:
    """Audit every vault that has a manifest. Return the upgrades, or stop listing every path a person must handle.

    A folder without a manifest is a new vault or one install restores itself, so it is not audited here.
    Nothing in a stopped run is written.
    """
    catalog = load_catalog(_current_catalog_path(builder_root))
    managed = set(catalog.managed_files)
    current_contract = catalog.contract_version
    upgrades: dict[str, _Upgrade] = {}
    problems: list[str] = []
    for vault in vaults:
        if not vault.path.is_dir():
            continue
        where = f"vault {vault.name} ({vault.path})"
        try:
            manifest = load_manifest(vault.path)
        except ValueError as exc:
            problems.append(_manifest_problem(where, vault.path, exc, current_contract))
            continue
        if manifest is None:
            continue
        try:
            migration = audit_vault(builder_root, vault.path)
        except ValueError as exc:
            problems.append(_audit_failure(where, vault.path, exc, managed, builder_root))
            continue
        blocking = [action for action in migration.actions if action.action in _REVIEW_ACTIONS]
        if blocking:
            problems.append(_review_problem(where, vault.path, blocking, managed, builder_root))
            continue
        pending = any(action.action in _UPGRADE_ACTIONS for action in migration.actions)
        if pending or manifest.schema_version != MANIFEST_SCHEMA_VERSION:
            upgrades[vault.name] = _Upgrade(vault, migration, manifest.schema_version)
    if problems:
        raise _Stop(
            "setup cannot upgrade these vaults until the paths below are fixed. Nothing was written.\n"
            + "\n".join(f"  {problem}" for problem in problems)
            + "\n  Fix them, then run this command again."
        )
    return upgrades


def _audit_failure(where: str, path: Path, error: ValueError, managed: set[str], builder_root: Path) -> str:
    """A vault-builder file that audit needs is missing: name the copy that puts it back."""
    lines = [f"{where}: audit failed: {error}"]
    missing = [relative for relative in _REQUIRED_TOPOLOGY if relative in managed and not (path / relative).is_file()]
    if missing:
        lines.append("    Put back the vault-builder version:")
        lines += [f"      cp {_quote(builder_root / relative)} {_quote(path / relative)}" for relative in missing]
    return "\n".join(lines)


def _manifest_problem(where: str, path: Path, error: ValueError, current_contract: int) -> str:
    try:
        recorded = json.loads((path / ".vault-builder" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recorded = None
    version = recorded.get("contract_version") if isinstance(recorded, dict) else None
    if type(version) is int and version > current_contract:
        return (
            f"{where}: a newer vault-builder (contract {version}) manages this vault, and this vault-builder knows "
            f"contract {current_contract}. Update vault-builder with git -C <builder checkout> pull --ff-only, or "
            "run the install command again."
        )
    return f"{where}: its .vault-builder/manifest.json is not valid: {error} Restore it from git: git -C {_quote(path)} checkout -- .vault-builder/manifest.json"


def _review_problem(
    where: str, path: Path, blocking: Sequence[MigrationAction], managed: set[str], builder_root: Path
) -> str:
    """One block per vault: each path with the audit reason, then what to do for each kind of path."""
    lines = [f"{where}:"]
    shown = sorted(blocking, key=lambda action: action.path)[:_SHOWN_REVIEW_PATHS]
    for action in shown:
        lines.append(f"    {action.action} {action.path}: {action.reason}")
    if len(blocking) > len(shown):
        lines.append(f"    ... and {len(blocking) - len(shown)} more")
    kinds = {_review_kind(action, managed) for action in blocking}
    if "managed" in kinds:
        lines.append(
            "    A vault-builder file differs from every version vault-builder shipped. Copy it outside the vault "
            "if you want to keep your changes, then put back the vault-builder version:"
        )
        for action in sorted(blocking, key=lambda item: item.path):
            if _review_kind(action, managed) != "managed":
                continue
            target = path / action.path
            if action.action == "invalid":
                # A folder or a symlink in place of the file: move it away so cp writes a plain file there.
                lines.append(f"      mv {_quote(target)} {_quote(target.with_name(target.name + '.old'))}")
            lines.append(f"      cp {_quote(builder_root / action.path)} {_quote(target)}")
    if "manifest" in kinds:
        lines.append(
            "    The manifest was written by a newer vault-builder. Update vault-builder with "
            "git -C <builder checkout> pull --ff-only, or run the install command again."
        )
    if "page" in kinds:
        lines.append(
            "    A wiki page breaks the source page rules. Fix the page so it follows them; "
            f"cd {_quote(path)} && python3 tools/wiki/cli.py lint shows the details."
        )
    lines.append(f"    List every path: vault-builder audit --target {_quote(path)} --plan-out <plan.json>")
    return "\n".join(lines)


def _review_kind(action: MigrationAction, managed: set[str]) -> str:
    if action.path == ".vault-builder/manifest.json":
        return "manifest"
    if action.path in managed:
        return "managed"
    return "page"


def _current_catalog_path(builder_root: Path) -> Path:
    return builder_root / _CURRENT_CATALOG_PATH


def _quote(path: Path) -> str:
    return shlex.quote(str(path))


def _print_upgrades(upgrades: Mapping[str, _Upgrade]) -> None:
    for upgrade in upgrades.values():
        print(
            f"  upgrade vault {upgrade.vault.name}: manifest schema {upgrade.schema_version} to "
            f"{MANIFEST_SCHEMA_VERSION}, {upgrade.counts()}. Your wiki pages and raw sources are kept."
        )


def _upgrade_vault(upgrade: _Upgrade, builder_root: Path) -> None:
    vault = upgrade.vault
    print(f"  upgrading vault {vault.name} ({upgrade.counts()}); it runs the vault's release gate.")
    try:
        apply_plan(builder_root, vault.path, upgrade.plan)
    except _INSTALL_ERRORS as exc:
        raise _Stop(
            f"upgrade of vault {vault.name} ({vault.path}) failed: {exc} "
            "The upgrade restores the vault's files when it fails. When the message names a recover command, "
            "run it. Vaults before it stay installed. Fix the cause, then run the same command again."
        ) from None


def _install_kwargs(vault: _Vault, env: Mapping[str, str], builder_root: Path) -> dict[str, object]:
    return {
        "name": vault.name,
        "with_hooks": True,
        "role": vault.role,
        "builder_root": builder_root,
        "environ": env,
    }


def _precheck_vault(vault: _Vault, env: Mapping[str, str], builder_root: Path, *, upgrade_pending: bool = False) -> None:
    kwargs = _install_kwargs(vault, env, builder_root)
    if upgrade_pending:
        kwargs["upgrade_pending"] = True
    try:
        installing.install(vault.path, dry_run=True, out=io.StringIO(), **kwargs)  # type: ignore[arg-type]
    except _INSTALL_ERRORS as exc:
        raise _Stop(f"vault {vault.name} ({vault.path}): {exc}") from None


def _require_claude_closed(tools: Externals, env: Mapping[str, str], prompter: _Prompter | None) -> None:
    message = "Quit every Claude Code session (terminal, IDE and app) before setup changes its settings."
    while tools.claude_running(env):
        if prompter is None:
            raise _Stop(message, EXIT_CLAUDE_RUNNING)
        if not prompter.wait_enter(f"{message} Press Enter when Claude Code is closed."):
            raise _Stop(message, EXIT_CLAUDE_RUNNING)


def _install_vault(vault: _Vault, env: Mapping[str, str], builder_root: Path) -> None:
    output = io.StringIO()
    try:
        installing.install(vault.path, out=output, **_install_kwargs(vault, env, builder_root))  # type: ignore[arg-type]
    except _INSTALL_ERRORS as exc:
        raise _Stop(
            f"install of vault {vault.name} ({vault.path}) failed: {exc} "
            "Vaults before it stay installed. Fix the cause, then run the same command again."
        ) from None
    for line in output.getvalue().splitlines():
        if line.startswith(_INSTALL_SUMMARY):
            print(f"  {line}")


def _default_write(plan: _Plan, wanted: str | None, env: Mapping[str, str], prompter: _Prompter | None) -> None:
    """Spec 4.6: when link set no default write vault, use --default-write, ask, or print the command."""
    router = linking._router()
    for repo in plan.repos:
        _path_unused, data = linking._load(env)
        match = router.find_project(repo.path, data)
        if isinstance(match, router.Stop):
            continue
        project_id = match.project_id
        entry = data["projects"][project_id]
        if map_settings.DEFAULT_WRITE_FIELD in entry:
            continue
        linked = list(entry["vaults"])
        choice: str | None = wanted if wanted in linked else None
        if choice is None and prompter is not None and linked:
            answer = prompter.ask(f"Default write vault for project {project_id} ({', '.join(linked)}) [skip]:")
            choice = answer if answer in linked else None
        if choice is not None:
            if map_settings.set_default_write(project_id, vault=choice, environ=env) != 0:
                raise _Stop(f"set-default-write failed for project {project_id}.")
            continue
        print(
            f"{_COMMAND}: project {project_id} has no default write vault, so each write must choose a vault. "
            f"Set one with: vault-builder set-default-write --project {project_id} --vault <name>"
        )
