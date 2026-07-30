"""Apply approved Vault migration plans as recoverable filesystem transactions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Callable

from .catalogs import _safe_relative_path, _validate_root, load_catalog, sha256_file
from .managed_paths import MANAGED_PATHS
from .manifests import create_manifest, write_manifest
from .models import MigrationAction, MigrationPlan, VaultManifest
from .planning import _target_root, audit_vault, target_fingerprint


_V1_CATALOG_PATH = "tools/vault_migration/contracts/v1.json"
_MIGRATION_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_JOURNAL_STATES = frozenset({"prepared", "applying", "validating", "rolling-back", "rollback-failed", "complete"})
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MODE_RE = re.compile(r"^[0-7]{4}$")
_MANIFEST_RELATIVE = ".vault-builder/manifest.json"


def _safe_target_path(target: Path, relative: str, *, reject_leaf_symlink: bool = True) -> Path:
    """Return the exact safe target leaf, including absent parent components."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Vault path must be a safe relative POSIX path.")
    candidate_relative = PurePosixPath(relative)
    if candidate_relative.is_absolute() or any(part in {"", ".", ".."} for part in candidate_relative.parts):
        raise ValueError("Vault path must be a safe relative POSIX path.")
    candidate = target
    for index, part in enumerate(candidate_relative.parts):
        candidate = candidate / part
        try:
            state = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"Vault path is unreadable: {relative}") from exc
        if stat.S_ISLNK(state.st_mode) and (reject_leaf_symlink or index < len(candidate_relative.parts) - 1):
            raise ValueError(f"Vault path contains a symlink: {relative}")
        if index < len(candidate_relative.parts) - 1 and not stat.S_ISDIR(state.st_mode):
            raise ValueError(f"Vault path has a non-directory parent: {relative}")
    try:
        candidate.parent.resolve(strict=False).relative_to(target)
    except ValueError as exc:
        raise ValueError(f"Vault path escapes the target root: {relative}") from exc
    return candidate


def apply_plan(
    builder_root: Path,
    target_root: Path,
    plan: MigrationPlan,
    run_gate: Callable[[Path], None] | None = None,
) -> VaultManifest:
    """Apply one audit plan, restoring exact prior state if any later step fails."""
    builder = _validate_root(builder_root, "Builder root")
    target = _target_root(target_root)
    checked_plan, actions = _verify_preconditions(builder, target, plan)
    journal_root, journal = _prepare_journal(target, checked_plan, actions)
    gate = run_gate or _run_release_gate
    try:
        _write_journal_state(journal_root, journal, "applying")
        created_directories: list[str] = []

        def record_created(directories: list[str]) -> None:
            created_directories.extend(directories)
            journal["created_directories"] = sorted(set(created_directories))
            _write_journal_state(journal_root, journal, "applying")

        for action in actions:
            _replace_from_builder(builder, target, action, record_created=record_created)
        _write_journal_state(journal_root, journal, "validating")

        # Gate one deliberately runs before authority-v1 metadata exists.
        gate(target)
        _verify_candidate_files(builder, target, actions)

        _record_prior_manifest(target, journal_root, journal)
        candidate = replace(
            create_manifest(builder, target, checked_plan.builder_revision),
            last_migration_id=checked_plan.migration_id,
        )
        write_manifest(target, candidate)
        journal["manifest"]["applied_sha256"] = sha256_file(target / _MANIFEST_RELATIVE)
        _write_journal_state(journal_root, journal, "validating")

        # Gate two intentionally sees authority-v1 metadata.
        gate(target)
        _verify_candidate_files(builder, target, actions)
        _write_journal_state(journal_root, journal, "complete")
        _remove_journal(target, journal_root, journal)
        return candidate
    except BaseException as exc:
        try:
            _rollback(target, journal_root, journal)
        except BaseException as rollback_exc:
            _mark_rollback_failed(journal_root, journal)
            command = _recover_command(target, checked_plan.migration_id)
            raise RuntimeError(f"Migration rollback failed; run `{command}`.") from rollback_exc
        raise exc


def recover_migration(target_root: Path, migration_id: str) -> None:
    """Restore an interrupted transaction without consulting builder desired bytes."""
    if not isinstance(migration_id, str) or not _MIGRATION_ID_RE.fullmatch(migration_id):
        raise ValueError("migration_id must be 16 lowercase hexadecimal characters.")
    target = _target_root(target_root)
    journal_root = _journal_root(target, migration_id, create=False)
    journal = _load_journal(target, journal_root, migration_id)
    if journal["state"] == "complete":
        raise ValueError("Migration recovery journal is complete and cannot be recovered.")
    _rollback(target, journal_root, journal)


def _verify_preconditions(builder: Path, target: Path, plan: MigrationPlan) -> tuple[MigrationPlan, tuple[MigrationAction, ...]]:
    # Reparse the immutable model so programmatic callers cannot bypass JSON boundaries.
    checked = MigrationPlan.from_json(plan.to_json())
    if checked.schema_version != 1 or checked.target_contract_version != 1:
        raise ValueError("Migration plan must target contract version 1.")
    if checked.has_blocking_actions:
        raise ValueError("Migration plan requires human review before apply.")
    paths = tuple(action.path for action in checked.actions)
    if len(paths) != len(set(paths)):
        raise ValueError("Migration plan actions must be unique.")
    if target_fingerprint(target, paths) != checked.target_fingerprint:
        raise ValueError("Migration target changed after audit; run audit again.")
    if audit_vault(builder, target) != checked:
        raise ValueError("Migration target changed after audit; run audit again.")

    catalog = load_catalog(_safe_relative_path(builder, _V1_CATALOG_PATH))
    if catalog.schema_version != checked.schema_version or catalog.contract_version != checked.target_contract_version:
        raise ValueError("Migration plan does not match the current builder contract.")
    if catalog.builder_revision != checked.builder_revision:
        raise ValueError("Migration plan does not match the current builder revision.")
    for relative, desired in catalog.managed_files.items():
        builder_file = _safe_relative_path(builder, relative)
        if sha256_file(builder_file) != desired.sha256 or _observed_mode(builder_file) != desired.mode:
            raise ValueError("Migration plan does not match desired builder files.")

    actions: list[MigrationAction] = []
    for action in checked.actions:
        if action.action not in {"create", "replace", "unchanged", "preserve", "rebuild-suggested"}:
            raise ValueError("Migration plan contains an unsupported action.")
        _safe_target_path(target, action.path, reject_leaf_symlink=False)
        if action.action not in {"create", "replace"}:
            continue
        desired = catalog.managed_files.get(action.path)
        if desired is None or action.desired_sha256 != desired.sha256 or action.desired_mode != desired.mode:
            raise ValueError("Migration plan does not match desired builder files.")
        builder_file = _safe_relative_path(builder, action.path)
        if sha256_file(builder_file) != action.desired_sha256 or _observed_mode(builder_file) != action.desired_mode:
            raise ValueError("Migration plan does not match desired builder files.")
        _verify_current_action(target, action)
        actions.append(action)

    journal_root = _journal_root(target, checked.migration_id, create=False)
    if journal_root.exists() or journal_root.is_symlink():
        raise ValueError("Migration recovery journal already exists for this migration ID.")
    return checked, tuple(sorted(actions, key=lambda action: action.path))


def _verify_candidate_files(builder: Path, target: Path, actions: tuple[MigrationAction, ...]) -> None:
    for action in actions:
        path = _safe_target_path(target, action.path, reject_leaf_symlink=False)
        if path.is_symlink() or not path.is_file() or sha256_file(path) != action.desired_sha256 or _observed_mode(path) != action.desired_mode:
            raise ValueError(f"Migration validation failed: {action.path}")
    catalog = load_catalog(_safe_relative_path(builder, _V1_CATALOG_PATH))
    for relative, desired in catalog.managed_files.items():
        path = _safe_target_path(target, relative, reject_leaf_symlink=False)
        if path.is_symlink() or not path.is_file() or sha256_file(path) != desired.sha256 or _observed_mode(path) != desired.mode:
            raise ValueError(f"Migration validation failed: {relative}")


def _verify_current_action(target: Path, action: MigrationAction) -> None:
    path = _safe_target_path(target, action.path, reject_leaf_symlink=False)
    try:
        state = path.lstat()
    except FileNotFoundError:
        state = None
    if action.action == "create":
        if state is not None or action.current_sha256 is not None or action.current_mode is not None:
            raise ValueError("Migration target changed after audit; run audit again.")
        return
    if state is None or stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError("Migration target changed after audit; run audit again.")
    if sha256_file(path) != action.current_sha256 or _observed_mode(path) != action.current_mode:
        raise ValueError("Migration target changed after audit; run audit again.")


def _prepare_journal(target: Path, plan: MigrationPlan, actions: tuple[MigrationAction, ...]) -> tuple[Path, dict[str, object]]:
    state_directory = _safe_target_path(target, ".vault-builder", reject_leaf_symlink=False)
    state_existed = state_directory.exists()
    if state_existed and (state_directory.is_symlink() or not state_directory.is_dir()):
        raise ValueError("Vault builder state directory must be a regular directory.")
    recovery_directory = _safe_target_path(target, ".vault-builder/recovery", reject_leaf_symlink=False)
    recovery_existed = recovery_directory.exists()
    if recovery_existed and (recovery_directory.is_symlink() or not recovery_directory.is_dir()):
        raise ValueError("Vault recovery directory must be a regular directory.")

    journal_root = _journal_root(target, plan.migration_id, create=True)
    originals = _journal_originals(journal_root)
    entries: list[dict[str, object]] = []
    try:
        for action in actions:
            path = _safe_target_path(target, action.path, reject_leaf_symlink=False)
            existed = path.exists()
            entry: dict[str, object] = {
                "path": action.path,
                "existed": existed,
                "sha256": action.current_sha256,
                "mode": action.current_mode,
                "applied_sha256": action.desired_sha256,
            }
            if existed:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Migration target changed after audit; run audit again.")
                _write_original(originals, action.path, path.read_bytes(), action.current_mode)
            entries.append(entry)
        journal: dict[str, object] = {
            "schema_version": 1,
            "migration_id": plan.migration_id,
            "target_contract_version": plan.target_contract_version,
            "state": "prepared",
            "entries": entries,
            "manifest": None,
            "created_directories": [],
            "state_directory_existed": state_existed,
            "recovery_directory_existed": recovery_existed,
        }
        _write_journal_state(journal_root, journal, "prepared")
        return journal_root, journal
    except BaseException:
        _remove_tree_no_symlinks(journal_root)
        if not recovery_existed:
            _rmdir_if_empty(recovery_directory)
        if not state_existed:
            _rmdir_if_empty(state_directory)
        raise


def _record_prior_manifest(target: Path, journal_root: Path, journal: dict[str, object]) -> None:
    manifest_path = _safe_target_path(target, _MANIFEST_RELATIVE, reject_leaf_symlink=False)
    existed = manifest_path.exists()
    manifest: dict[str, object] = {"existed": existed, "sha256": None, "mode": None, "applied_sha256": None}
    if existed:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Vault builder manifest must be a regular file.")
        contents = manifest_path.read_bytes()
        manifest["sha256"] = hashlib.sha256(contents).hexdigest()
        manifest["mode"] = _observed_mode(manifest_path)
        _write_original(_journal_originals(journal_root), _MANIFEST_RELATIVE, contents, str(manifest["mode"]))
    journal["manifest"] = manifest
    _write_journal_state(journal_root, journal, "validating")


def _replace_from_builder(
    builder: Path,
    target: Path,
    action: MigrationAction,
    *,
    record_created: Callable[[list[str]], None],
) -> None:
    source = _safe_relative_path(builder, action.path)
    destination = _safe_target_path(target, action.path, reject_leaf_symlink=False)
    record_created(_ensure_regular_parent(target, destination))
    _atomic_write(destination, source.read_bytes(), action.desired_mode or "0644")
    if sha256_file(destination) != action.desired_sha256 or _observed_mode(destination) != action.desired_mode:
        raise ValueError(f"Atomic replacement verification failed: {action.path}")


def _ensure_regular_parent(target: Path, destination: Path) -> list[str]:
    relative_parent = destination.parent.relative_to(target)
    current = target
    created: list[str] = []
    for part in relative_parent.parts:
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            created.append(current.relative_to(target).as_posix())
            continue
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise ValueError(f"Migration target parent contains an unsafe path: {destination.relative_to(target)}")
    return created


def _atomic_write(destination: Path, contents: bytes, mode: str) -> None:
    if destination.exists() and (destination.is_symlink() or not destination.is_file()):
        raise ValueError(f"Migration target must be a regular file: {destination}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, int(mode, 8))
        if sha256_file(temporary) != hashlib.sha256(contents).hexdigest():
            raise ValueError(f"Temporary replacement verification failed: {destination}")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_original(originals: Path, relative: str, contents: bytes, mode: str) -> None:
    destination = _journal_path(originals, relative)
    _ensure_regular_parent(originals, destination)
    _atomic_write(destination, contents, mode)


def _write_journal_state(journal_root: Path, journal: dict[str, object], state: str) -> None:
    if state not in _JOURNAL_STATES:
        raise ValueError("Migration journal state is invalid.")
    journal["state"] = state
    payload = _canonical_json(journal).encode("utf-8")
    destination = _journal_path(journal_root, "journal.json")
    _atomic_write(destination, payload, "0644")


def _rollback(target: Path, journal_root: Path, journal: dict[str, object]) -> None:
    _write_journal_state(journal_root, journal, "rolling-back")
    entries = _journal_entries(journal)
    originals = _journal_originals(journal_root, create=False)
    for entry in reversed(entries):
        _restore_entry(target, originals, entry)
    manifest = _journal_manifest(journal, allow_none=True)
    if manifest is not None:
        _restore_manifest(target, originals, manifest)
    created_directories = _validate_created_directories(entries, journal.get("created_directories"))
    for relative in sorted(created_directories, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        _rmdir_if_empty(_safe_target_path(target, relative, reject_leaf_symlink=False))
    _verify_restored(target, originals, entries, manifest)
    _remove_journal(target, journal_root, journal)


def _restore_entry(target: Path, originals: Path, entry: dict[str, object]) -> None:
    relative = str(entry["path"])
    destination = _safe_target_path(target, relative, reject_leaf_symlink=False)
    if entry["existed"]:
        source = _journal_path(originals, relative)
        _require_original(source, str(entry["sha256"]), str(entry["mode"]))
        _ensure_regular_parent(target, destination)
        _atomic_write(destination, source.read_bytes(), str(entry["mode"]))
        return
    _delete_transaction_file(destination, entry.get("applied_sha256"))


def _restore_manifest(target: Path, originals: Path, manifest: dict[str, object]) -> None:
    destination = _safe_target_path(target, _MANIFEST_RELATIVE, reject_leaf_symlink=False)
    if manifest["existed"]:
        source = _journal_path(originals, _MANIFEST_RELATIVE)
        _require_original(source, str(manifest["sha256"]), str(manifest["mode"]))
        _ensure_regular_parent(target, destination)
        _atomic_write(destination, source.read_bytes(), str(manifest["mode"]))
        return
    _delete_transaction_file(destination, manifest.get("applied_sha256"))


def _delete_transaction_file(path: Path, applied_sha256: object) -> None:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError(f"Refusing to delete unsafe recovery path: {path}")
    if not isinstance(applied_sha256, str) or not _HEX_RE.fullmatch(applied_sha256) or sha256_file(path) != applied_sha256:
        raise ValueError(f"Refusing to delete a non-transaction recovery file: {path}")
    path.unlink()
    _fsync_directory(path.parent)


def _verify_restored(
    target: Path,
    originals: Path,
    entries: tuple[dict[str, object], ...],
    manifest: dict[str, object] | None,
) -> None:
    for entry in entries:
        path = _safe_target_path(target, str(entry["path"]), reject_leaf_symlink=False)
        if entry["existed"]:
            if path.is_symlink() or not path.is_file() or sha256_file(path) != entry["sha256"] or _observed_mode(path) != entry["mode"]:
                raise ValueError(f"Recovery verification failed: {entry['path']}")
        elif path.exists():
            raise ValueError(f"Recovery verification failed: {entry['path']}")
    if manifest is None:
        return
    path = _safe_target_path(target, _MANIFEST_RELATIVE, reject_leaf_symlink=False)
    if manifest["existed"]:
        if path.is_symlink() or not path.is_file() or sha256_file(path) != manifest["sha256"] or _observed_mode(path) != manifest["mode"]:
            raise ValueError("Recovery verification failed: manifest")
    elif path.exists():
        raise ValueError("Recovery verification failed: manifest")


def _load_journal(target: Path, journal_root: Path, migration_id: str) -> dict[str, object]:
    if journal_root.is_symlink() or not journal_root.is_dir():
        raise ValueError("Migration recovery journal not found.")
    journal_path = _journal_path(journal_root, "journal.json")
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError("Migration recovery journal is invalid.")
    try:
        data = json.loads(
            journal_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Migration recovery journal is invalid.") from exc
    if not isinstance(data, dict) or set(data) - _journal_optional_keys() or not _journal_required_keys() <= set(data):
        raise ValueError("Migration recovery journal is invalid.")
    if type(data.get("schema_version")) is not int or type(data.get("target_contract_version")) is not int:
        raise ValueError("Migration recovery journal is invalid.")
    if data.get("schema_version") != 1 or data.get("migration_id") != migration_id or data.get("target_contract_version") != 1:
        raise ValueError("Migration recovery journal is invalid.")
    if not isinstance(data.get("state"), str) or data["state"] not in _JOURNAL_STATES:
        raise ValueError("Migration recovery journal has an invalid state.")
    entries = _journal_entries(data)
    manifest = _journal_manifest(data, allow_none=True)
    _validate_created_directories(entries, data.get("created_directories"))
    for key in ("state_directory_existed", "recovery_directory_existed"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError("Migration recovery journal state is invalid.")
    _validate_original_tree(journal_root, entries, manifest)
    return data


def _journal_required_keys() -> frozenset[str]:
    return frozenset({"schema_version", "migration_id", "target_contract_version", "state", "entries", "manifest"})


def _journal_optional_keys() -> frozenset[str]:
    return _journal_required_keys() | frozenset({"created_directories", "state_directory_existed", "recovery_directory_existed"})


def _journal_entries(journal: dict[str, object]) -> tuple[dict[str, object], ...]:
    raw_entries = journal.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Migration recovery journal entries are invalid.")
    parsed: list[dict[str, object]] = []
    paths: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) - {"path", "existed", "sha256", "mode", "applied_sha256"} or not {"path", "existed", "sha256", "mode"} <= set(raw):
            raise ValueError("Migration recovery journal entries are invalid.")
        path = raw.get("path")
        if not isinstance(path, str):
            raise ValueError("Migration recovery journal path is invalid.")
        _validate_managed_relative(path)
        if path in paths:
            raise ValueError("Migration recovery journal contains duplicate paths.")
        paths.add(path)
        existed = raw.get("existed")
        if not isinstance(existed, bool):
            raise ValueError("Migration recovery journal entry existence is invalid.")
        digest, mode = raw.get("sha256"), raw.get("mode")
        if existed:
            if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest) or not isinstance(mode, str) or not _MODE_RE.fullmatch(mode):
                raise ValueError("Migration recovery journal entry hash or mode is invalid.")
        elif digest is not None or mode is not None:
            raise ValueError("Migration recovery journal entry state is invalid.")
        applied = raw.get("applied_sha256")
        if applied is not None and (not isinstance(applied, str) or not _HEX_RE.fullmatch(applied)):
            raise ValueError("Migration recovery journal applied hash is invalid.")
        parsed.append({"path": path, "existed": existed, "sha256": digest, "mode": mode, "applied_sha256": applied})
    if tuple(item["path"] for item in parsed) != tuple(sorted(str(item["path"]) for item in parsed)):
        raise ValueError("Migration recovery journal entries must be sorted.")
    return tuple(parsed)


def _journal_manifest(journal: dict[str, object], *, allow_none: bool) -> dict[str, object] | None:
    raw = journal.get("manifest")
    if raw is None and allow_none:
        return None
    if not isinstance(raw, dict) or set(raw) - {"existed", "sha256", "mode", "applied_sha256"} or not {"existed", "sha256", "mode"} <= set(raw):
        raise ValueError("Migration recovery manifest state is invalid.")
    existed, digest, mode = raw.get("existed"), raw.get("sha256"), raw.get("mode")
    if not isinstance(existed, bool):
        raise ValueError("Migration recovery manifest state is invalid.")
    if existed:
        if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest) or not isinstance(mode, str) or not _MODE_RE.fullmatch(mode):
            raise ValueError("Migration recovery manifest hash or mode is invalid.")
    elif digest is not None or mode is not None:
        raise ValueError("Migration recovery manifest state is invalid.")
    applied = raw.get("applied_sha256")
    if applied is not None and (not isinstance(applied, str) or not _HEX_RE.fullmatch(applied)):
        raise ValueError("Migration recovery manifest applied hash is invalid.")
    return {"existed": existed, "sha256": digest, "mode": mode, "applied_sha256": applied}


def _validate_original_tree(journal_root: Path, entries: tuple[dict[str, object], ...], manifest: dict[str, object] | None) -> None:
    originals = _journal_originals(journal_root, create=False)
    if originals.is_symlink() or not originals.is_dir():
        raise ValueError("Migration recovery originals are invalid.")
    expected: set[str] = set()
    for entry in entries:
        if entry["existed"]:
            relative = str(entry["path"])
            expected.add(relative)
            _require_original(_journal_path(originals, relative), str(entry["sha256"]), str(entry["mode"]))
    if manifest is not None and manifest["existed"]:
        expected.add(_MANIFEST_RELATIVE)
        _require_original(_journal_path(originals, _MANIFEST_RELATIVE), str(manifest["sha256"]), str(manifest["mode"]))
    actual: set[str] = set()
    for path in originals.rglob("*"):
        relative = path.relative_to(originals).as_posix()
        state = path.lstat()
        if stat.S_ISLNK(state.st_mode) or not (stat.S_ISREG(state.st_mode) or stat.S_ISDIR(state.st_mode)):
            raise ValueError("Migration recovery originals contain an unsafe path.")
        if stat.S_ISREG(state.st_mode):
            actual.add(relative)
    if actual != expected:
        raise ValueError("Migration recovery originals do not match the journal.")


def _require_original(path: Path, digest: str, mode: str) -> None:
    if path.is_symlink() or not path.is_file() or sha256_file(path) != digest or _observed_mode(path) != mode:
        raise ValueError("Migration recovery original is invalid.")


def _validate_managed_relative(relative: str) -> None:
    candidate = PurePosixPath(relative)
    if "\\" in relative or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Migration recovery path must be a safe relative path.")
    if relative not in MANAGED_PATHS:
        raise ValueError("Migration recovery path is not a managed file.")


def _journal_root(target: Path, migration_id: str, *, create: bool) -> Path:
    relative = f".vault-builder/recovery/{migration_id}"
    root = _safe_target_path(target, relative, reject_leaf_symlink=False)
    if create:
        _ensure_regular_parent(target, root)
        if root.exists() or root.is_symlink():
            raise ValueError("Migration recovery journal already exists for this migration ID.")
        root.mkdir()
    return root


def _journal_originals(journal_root: Path, *, create: bool = True) -> Path:
    root = _journal_path(journal_root, "originals")
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Migration recovery originals are invalid.")
    elif create:
        root.mkdir()
    else:
        raise ValueError("Migration recovery originals are invalid.")
    return root


def _journal_path(root: Path, relative: str) -> Path:
    candidate = _safe_relative_path(root, relative)
    return candidate


def _remove_journal(target: Path, journal_root: Path, journal: dict[str, object]) -> None:
    _remove_tree_no_symlinks(journal_root)
    recovery = journal_root.parent
    state_directory = recovery.parent
    if not bool(journal.get("recovery_directory_existed", True)):
        _rmdir_if_empty(recovery)
    if not bool(journal.get("state_directory_existed", True)):
        _rmdir_if_empty(state_directory)


def _remove_tree_no_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Migration recovery journal is invalid.")
    for path in sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True):
        state = path.lstat()
        if stat.S_ISLNK(state.st_mode) or not (stat.S_ISREG(state.st_mode) or stat.S_ISDIR(state.st_mode)):
            raise ValueError("Migration recovery journal contains an unsafe path.")
        if stat.S_ISREG(state.st_mode):
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def _mark_rollback_failed(journal_root: Path, journal: dict[str, object]) -> None:
    try:
        _write_journal_state(journal_root, journal, "rollback-failed")
    except BaseException:
        pass


def _run_release_gate(target_root: Path) -> None:
    subprocess.run(["bash", "scripts/release_gate.sh"], cwd=target_root, text=True, check=True)


def _recover_command(target: Path, migration_id: str) -> str:
    return f"python3 tools/vault_migration/cli.py recover --target {target} --migration-id {migration_id}"


def _observed_mode(path: Path) -> str:
    return f"{path.stat().st_mode & 0o7777:04o}"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Invalid JSON constant: {value}")


def _string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Migration recovery created directories are invalid.")
    for item in value:
        candidate = PurePosixPath(item)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("Migration recovery created directories are invalid.")
    return tuple(value)


def _validate_created_directories(entries: tuple[dict[str, object], ...], value: object) -> tuple[str, ...]:
    directories = _string_list(value)
    if directories != tuple(sorted(set(directories))):
        raise ValueError("Migration recovery created directories are invalid.")
    allowed: set[str] = set()
    for entry in entries:
        if entry["existed"]:
            continue
        parent = PurePosixPath(str(entry["path"])).parent
        while parent.parts:
            allowed.add(parent.as_posix())
            parent = parent.parent
    if not set(directories) <= allowed:
        raise ValueError("Migration recovery created directories are invalid.")
    return directories


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result
