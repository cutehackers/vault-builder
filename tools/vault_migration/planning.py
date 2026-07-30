"""Read-only inspection and deterministic planning for legacy Vault migration."""
from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from tools.wiki.lib.authority import scan_authority_issues
from tools.wiki.lib.frontmatter import Page, parse_page
from tools.wiki.lib.schema import Issue

from .catalogs import load_catalog
from .manifests import load_manifest
from .models import ContractCatalog, ManagedFile, MigrationAction, MigrationPlan


_V1_CATALOG_PATH = "tools/vault_migration/contracts/v1.json"
_V0_CATALOG_PATH = "tools/vault_migration/contracts/v0.json"
_REQUIRED_TOPOLOGY = (
    "AGENTS.md",
    "wiki/index.md",
    "wiki/log.md",
    "tools/wiki/cli.py",
    "scripts/release_gate.sh",
)
_LEGACY_BUILDER_COPIES = ("init-vault.sh", "scripts/bootstrap.sh")


def audit_vault(builder_root: Path, target_root: Path) -> MigrationPlan:
    """Inspect a Vault and return a plan without changing the target tree."""
    builder = _regular_directory(builder_root, "Builder root")
    target = _target_root(target_root)
    _validate_topology(target)
    v0_catalog = _load_catalog(builder, _V0_CATALOG_PATH)
    v1_catalog = _load_catalog(builder, _V1_CATALOG_PATH)
    manifest = load_manifest(target)

    catalog_actions = _catalog_actions(target, v0_catalog, v1_catalog, manifest)
    role_actions = _role_actions(target, set(catalog_actions))
    pages = _wiki_pages(target)
    authority_issues = _issue_dicts(target, scan_authority_issues(target, pages))
    _apply_authority_reviews(target, role_actions, authority_issues)

    actions = tuple(catalog_actions[path] for path in sorted(catalog_actions)) + tuple(
        role_actions[path] for path in sorted(role_actions)
    )
    fingerprint = target_fingerprint(target, (action.path for action in actions))
    current_version = manifest.contract_version if manifest is not None else None
    migration_id = _migration_id(
        current_version=current_version,
        target_version=v1_catalog.contract_version,
        builder_revision=v1_catalog.builder_revision,
        fingerprint=fingerprint,
        actions=actions,
        authority_issues=authority_issues,
    )
    return MigrationPlan(
        schema_version=v1_catalog.schema_version,
        migration_id=migration_id,
        current_contract_version=current_version,
        target_contract_version=v1_catalog.contract_version,
        builder_revision=v1_catalog.builder_revision,
        target_fingerprint=fingerprint,
        actions=actions,
        authority_issues=tuple(authority_issues),
    )


def target_fingerprint(target_root: Path, paths: Iterable[str]) -> str:
    """Hash observed target state for sorted Vault-relative paths only."""
    target = _target_root(target_root)
    entries = [_fingerprint_entry(target, relative) for relative in sorted(set(paths))]
    return hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()


def render_plan_summary(target_root: Path, plan: MigrationPlan) -> str:
    """Render a small deterministic, human-readable audit summary."""
    counts = Counter(action.action for action in plan.actions)
    lines = [
        "# Vault Migration Audit",
        "",
        f"- Target: `{Path(target_root).absolute()}`",
        f"- Migration ID: `{plan.migration_id}`",
        f"- Current contract version: `{plan.current_contract_version}`",
        f"- Target contract version: `{plan.target_contract_version}`",
        f"- Target fingerprint: `{plan.target_fingerprint}`",
        f"- Authority issues: {len(plan.authority_issues)}",
        "",
        "## Actions",
        "",
    ]
    if not counts:
        lines.append("- None.")
    else:
        lines.extend(f"- {action}: {counts[action]}" for action in sorted(counts))
    return "\n".join(lines) + "\n"


def validate_external_output_path(target_root: Path, output_path: Path) -> None:
    """Reject target-contained or symlinked future plan/report destinations."""
    target = _target_root(target_root).resolve()
    output = Path(output_path).absolute()
    _reject_symlinked_output_parents(output)
    resolved = output.resolve(strict=False)
    try:
        resolved.relative_to(target)
    except ValueError:
        return
    raise ValueError("Migration plan output must stay outside the target Vault.")


def _regular_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"{label} must be a regular directory.")
    try:
        return candidate.resolve()
    except OSError as exc:
        raise ValueError(f"{label} is unreadable.") from exc


def _target_root(target_root: Path) -> Path:
    target = Path(target_root)
    if target.is_symlink():
        raise ValueError("Vault target root must not be a symlink.")
    if not target.is_dir():
        raise ValueError("Vault target root must be a regular directory.")
    try:
        return target.resolve()
    except OSError as exc:
        raise ValueError("Vault target root is unreadable.") from exc


def _safe_target_path(target: Path, relative: str, *, reject_leaf_symlink: bool = True) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("Vault path must be a safe relative POSIX path.")
    candidate_relative = PurePosixPath(relative)
    if candidate_relative.is_absolute() or any(part in {"", ".", ".."} for part in candidate_relative.parts):
        raise ValueError("Vault path must be a safe relative POSIX path.")

    candidate = target
    parts = candidate_relative.parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            state = candidate.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError(f"Vault path is unreadable: {relative}") from exc
        if stat.S_ISLNK(state.st_mode) and (reject_leaf_symlink or index < len(parts) - 1):
            raise ValueError(f"Vault path contains a symlink: {relative}")

    parent = candidate.parent.resolve(strict=False)
    try:
        parent.relative_to(target)
    except ValueError as exc:
        raise ValueError(f"Vault path escapes the target root: {relative}") from exc
    return candidate


def _validate_topology(target: Path) -> None:
    for relative in _REQUIRED_TOPOLOGY:
        path = _safe_target_path(target, relative)
        try:
            state = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"Vault topology is missing required file: {relative}") from exc
        except OSError as exc:
            raise ValueError(f"Vault topology is unreadable: {relative}") from exc
        if not stat.S_ISREG(state.st_mode):
            raise ValueError(f"Vault topology requires a regular file: {relative}")
        _read_file(path, relative)


def _load_catalog(builder: Path, relative: str) -> ContractCatalog:
    path = _safe_builder_path(builder, relative)
    return load_catalog(path)


def _safe_builder_path(builder: Path, relative: str) -> Path:
    candidate_relative = PurePosixPath(relative)
    if candidate_relative.is_absolute() or any(part in {"", ".", ".."} for part in candidate_relative.parts):
        raise ValueError("Catalog path must be safe.")
    candidate = builder.joinpath(*candidate_relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"Catalog must be a regular file: {relative}")
    return candidate


def _catalog_actions(
    target: Path,
    v0_catalog: ContractCatalog,
    v1_catalog: ContractCatalog,
    manifest: object,
) -> dict[str, MigrationAction]:
    manifest_files = getattr(manifest, "managed_files", {})
    actions: dict[str, MigrationAction] = {}
    for relative, desired in v1_catalog.managed_files.items():
        state = _file_state(target, relative)
        if state["kind"] == "missing":
            actions[relative] = _action(relative, "create", state, desired, "Managed file is missing from the Vault.")
            continue
        if state["kind"] != "file":
            actions[relative] = _action(
                relative,
                "invalid",
                state,
                desired,
                "Managed path must be a readable regular file inside the Vault.",
            )
            continue
        if state["sha256"] == desired.sha256:
            actions[relative] = _action(relative, "unchanged", state, desired, "Already matches the target contract.")
            continue
        known_hashes = _known_hashes(relative, v0_catalog, manifest_files)
        if state["sha256"] in known_hashes:
            actions[relative] = _action(
                relative,
                "replace",
                state,
                desired,
                "Matches a known prior managed-file hash.",
            )
            continue
        actions[relative] = _action(
            relative,
            "review-required",
            state,
            desired,
            "Managed file differs from all known builder hashes.",
        )
    return actions


def _known_hashes(relative: str, v0_catalog: ContractCatalog, manifest_files: object) -> set[str]:
    hashes: set[str] = set()
    if isinstance(manifest_files, dict) or hasattr(manifest_files, "get"):
        managed = manifest_files.get(relative)
        if isinstance(managed, ManagedFile):
            hashes.add(managed.sha256)
    legacy = v0_catalog.managed_files.get(relative)
    if legacy is not None:
        hashes.add(legacy.sha256)
    return hashes


def _role_actions(target: Path, catalog_paths: set[str]) -> dict[str, MigrationAction]:
    actions: dict[str, MigrationAction] = {}
    for relative in _regular_files_below(target, "raw"):
        _add_role_action(actions, target, relative, "preserve", "Raw evidence is never changed by migration.", catalog_paths)
    for relative in _regular_files_below(target, "wiki"):
        if relative == "wiki/index.md" or relative.startswith("wiki/maps/"):
            action, reason = "rebuild-suggested", "Generated wiki views require an explicit rebuild."
        elif relative == "wiki/log.md":
            action, reason = "preserve", "Operational history is preserved byte-for-byte."
        else:
            action, reason = "preserve", "User-owned wiki knowledge is never rewritten by migration."
        _add_role_action(actions, target, relative, action, reason, catalog_paths)
    for relative in _regular_files_below(target, "scratch"):
        _add_role_action(actions, target, relative, "preserve", "Scratch and report evidence is preserved byte-for-byte.", catalog_paths)
    for relative in _LEGACY_BUILDER_COPIES:
        state = _file_state(target, relative)
        if state["kind"] == "file" and relative not in catalog_paths:
            actions[relative] = _action(
                relative,
                "preserve",
                state,
                None,
                "Legacy builder copy is preserved; creation is now builder-only.",
            )
    return actions


def _add_role_action(
    actions: dict[str, MigrationAction],
    target: Path,
    relative: str,
    action: str,
    reason: str,
    catalog_paths: set[str],
) -> None:
    if relative in catalog_paths:
        return
    actions[relative] = _action(relative, action, _file_state(target, relative), None, reason)


def _regular_files_below(target: Path, directory: str) -> tuple[str, ...]:
    root = _safe_target_path(target, directory)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ValueError(f"Vault role root must be a directory: {directory}")
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(target).as_posix()
        safe_path = _safe_target_path(target, relative)
        state = safe_path.lstat()
        if stat.S_ISREG(state.st_mode):
            _read_file(safe_path, relative)
            paths.append(relative)
        elif not stat.S_ISDIR(state.st_mode):
            raise ValueError(f"Vault role path must be a regular file or directory: {relative}")
    return tuple(paths)


def _wiki_pages(target: Path) -> list[Page]:
    pages: list[Page] = []
    for relative in _regular_files_below(target, "wiki"):
        if not relative.endswith(".md"):
            continue
        path = _safe_target_path(target, relative)
        try:
            pages.append(parse_page(path))
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Vault wiki page is unreadable: {relative}") from exc
    return pages


def _issue_dicts(target: Path, issues: list[Issue]) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for issue in issues:
        path = Path(issue.path)
        try:
            relative = path.resolve().relative_to(target).as_posix()
        except ValueError as exc:
            raise ValueError(f"Authority issue path escapes the target Vault: {issue.path}") from exc
        values.append(
            {
                "code": issue.code,
                "line": issue.line,
                "message": issue.message,
                "path": relative,
                "severity": issue.severity,
            }
        )
    return sorted(values, key=lambda issue: (str(issue["path"]), str(issue["code"]), str(issue["message"])))


def _apply_authority_reviews(
    target: Path,
    role_actions: dict[str, MigrationAction],
    authority_issues: list[dict[str, object]],
) -> None:
    by_path: dict[str, list[str]] = {}
    for issue in authority_issues:
        path = str(issue["path"])
        by_path.setdefault(path, []).append(str(issue["code"]))
    for relative, codes in by_path.items():
        state = _file_state(target, relative)
        if state["kind"] != "file":
            continue
        role_actions[relative] = _action(
            relative,
            "review-required",
            state,
            None,
            f"Authority review required: {', '.join(sorted(set(codes)))}.",
        )


def _file_state(target: Path, relative: str) -> dict[str, str | None]:
    try:
        path = _safe_target_path(target, relative, reject_leaf_symlink=False)
    except ValueError:
        return {"kind": "invalid", "sha256": None, "mode": None}
    try:
        state = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "sha256": None, "mode": None}
    except OSError:
        return {"kind": "invalid", "sha256": None, "mode": None}
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        return {"kind": "invalid", "sha256": None, "mode": None}
    try:
        digest = hashlib.sha256(_read_file(path, relative)).hexdigest()
    except ValueError:
        return {"kind": "invalid", "sha256": None, "mode": None}
    return {"kind": "file", "sha256": digest, "mode": _observed_mode(state.st_mode)}


def _action(
    relative: str,
    action: str,
    state: dict[str, str | None],
    desired: ManagedFile | None,
    reason: str,
) -> MigrationAction:
    return MigrationAction(
        path=relative,
        action=action,  # type: ignore[arg-type]
        current_sha256=state["sha256"],
        desired_sha256=desired.sha256 if desired is not None else None,
        current_mode=state["mode"],
        desired_mode=desired.mode if desired is not None else None,
        reason=reason,
    )


def _fingerprint_entry(target: Path, relative: str) -> dict[str, str | None]:
    state = _file_state(target, relative)
    return {"mode": state["mode"], "path": relative, "sha256": state["sha256"], "type": state["kind"]}


def _migration_id(
    *,
    current_version: int | None,
    target_version: int,
    builder_revision: str,
    fingerprint: str,
    actions: tuple[MigrationAction, ...],
    authority_issues: list[dict[str, object]],
) -> str:
    value = {
        "actions": [action.to_dict() for action in actions],
        "authority_issues": authority_issues,
        "builder_revision": builder_revision,
        "current_contract_version": current_version,
        "target_contract_version": target_version,
        "target_fingerprint": fingerprint,
    }
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _read_file(path: Path, relative: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Vault file is unreadable: {relative}") from exc


def _observed_mode(mode: int) -> str:
    return f"{mode & 0o7777:04o}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _reject_symlinked_output_parents(path: Path) -> None:
    current = path.parent
    while not current.exists() and not current.is_symlink() and current != current.parent:
        current = current.parent
    if current.is_symlink():
        raise ValueError(f"Migration plan output parent contains a symlink: {current}")
