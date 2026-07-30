"""Apply approved Vault migration plans as recoverable filesystem transactions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
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
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_RELATIVE = ".vault-builder/manifest.json"
_DIRECTORY_MARKER_FILENAME = ".vault-builder-migration.json"


class MigrationApplyError(RuntimeError):
    """A post-start failure with explicit transaction recovery outcome."""

    def __init__(
        self,
        original_error: BaseException,
        *,
        target: Path,
        migration_id: str,
        rollback_completed: bool,
        rollback_error: BaseException | None = None,
    ) -> None:
        self.original_error = original_error
        self.rollback_error = rollback_error
        self.rollback_completed = rollback_completed
        self.failed_command = _failed_command(original_error)
        self.exit_code = (
            original_error.returncode
            if isinstance(original_error, subprocess.CalledProcessError)
            else None
        )
        self.audit_command = _audit_command(target) if rollback_completed else None
        self.recover_command = (
            None
            if rollback_completed
            else _recover_command(target, migration_id)
        )
        lines = ["Migration failed after transaction start."]
        if self.failed_command is not None:
            lines.append(f"Failed command: {self.failed_command}")
            lines.append(f"Exit code: {self.exit_code}")
        else:
            lines.append(f"Failure: {original_error}")
        if rollback_completed:
            lines.append("Automatic rollback: completed")
            lines.append(f"Run read-only audit again: {self.audit_command}")
        else:
            lines.append("Automatic rollback: failed")
            lines.append(f"Recover with: {self.recover_command}")
        super().__init__("\n".join(lines))


class _JournalPreparationFailure(Exception):
    def __init__(
        self,
        original_error: BaseException,
        cleanup_error: BaseException | None,
    ) -> None:
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        super().__init__(str(original_error))


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
    try:
        journal_root, journal = _prepare_journal(target, checked_plan, actions)
    except _JournalPreparationFailure as exc:
        raise MigrationApplyError(
            exc.original_error,
            target=target,
            migration_id=checked_plan.migration_id,
            rollback_completed=exc.cleanup_error is None,
            rollback_error=exc.cleanup_error,
        ) from (exc.cleanup_error or exc.original_error)
    gate = run_gate or _run_release_gate
    try:
        _write_journal_state(journal_root, journal, "applying")
        def record_directory(directory: str, lifecycle: str) -> None:
            records = _directory_records(journal)
            if lifecycle == "intent":
                records.append({"existed": False, "path": directory, "state": "intent"})
                records.sort(key=lambda item: str(item["path"]))
            else:
                record = next(item for item in records if item["path"] == directory)
                record["state"] = "created"
            journal["directories"] = records
            _write_journal_state(journal_root, journal, "applying")

        for action in actions:
            _replace_from_builder(
                builder,
                target,
                action,
                migration_id=checked_plan.migration_id,
                directory_nonce=str(journal["directory_nonce"]),
                record_directory=record_directory,
            )
        _write_journal_state(journal_root, journal, "validating")

        # Gate one deliberately runs before authority-v1 metadata exists.
        gate(target)
        _verify_candidate_files(builder, target, actions)

        candidate = replace(
            create_manifest(builder, target, checked_plan.builder_revision),
            last_migration_id=checked_plan.migration_id,
        )
        candidate_bytes = candidate.to_json().encode("utf-8")
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        _record_manifest_publication_intent(
            target,
            journal_root,
            journal,
            candidate_sha256,
        )
        _manifest_step("before-publication")
        write_manifest(target, candidate)
        _manifest_step("after-publication")
        if sha256_file(target / _MANIFEST_RELATIVE) != candidate_sha256:
            raise ValueError("Candidate Vault builder manifest verification failed.")
        manifest_state = _journal_manifest(journal, allow_none=False)
        assert manifest_state is not None
        manifest_state["publication_state"] = "published"
        journal["manifest"] = manifest_state
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
            raise MigrationApplyError(
                exc,
                target=target,
                migration_id=checked_plan.migration_id,
                rollback_completed=False,
                rollback_error=rollback_exc,
            ) from rollback_exc
        raise MigrationApplyError(
            exc,
            target=target,
            migration_id=checked_plan.migration_id,
            rollback_completed=True,
        ) from exc


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

    journal_root = _journal_root(target, plan.migration_id, create=False)
    try:
        journal_root = _journal_root(target, plan.migration_id, create=True)
        originals = _journal_originals(journal_root)
        entries: list[dict[str, object]] = []
        for action in actions:
            path = _safe_target_path(target, action.path, reject_leaf_symlink=False)
            existed = path.exists()
            entry: dict[str, object] = {
                "path": action.path,
                "existed": existed,
                "sha256": action.current_sha256,
                "mode": action.current_mode,
                "applied_sha256": action.desired_sha256,
                "applied_mode": action.desired_mode,
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
            "directory_nonce": secrets.token_hex(32),
            "directories": [],
            "state_directory_existed": state_existed,
            "recovery_directory_existed": recovery_existed,
        }
        _write_journal_state(journal_root, journal, "prepared")
        _journal_prepare_step("after-prepared")
        return journal_root, journal
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        try:
            if journal_root.exists() or journal_root.is_symlink():
                _remove_tree_no_symlinks(journal_root)
            if not recovery_existed:
                _rmdir_if_empty(recovery_directory)
            if not state_existed:
                _rmdir_if_empty(state_directory)
        except BaseException as cleanup_exc:
            cleanup_error = cleanup_exc
        raise _JournalPreparationFailure(exc, cleanup_error) from (cleanup_error or exc)


def _journal_prepare_step(_stage: str) -> None:
    """Failure-injection seam after durable journal preparation."""


def _record_manifest_publication_intent(
    target: Path,
    journal_root: Path,
    journal: dict[str, object],
    candidate_sha256: str,
) -> None:
    manifest_path = _safe_target_path(target, _MANIFEST_RELATIVE, reject_leaf_symlink=False)
    existed = manifest_path.exists()
    manifest: dict[str, object] = {
        "existed": existed,
        "sha256": None,
        "mode": None,
        "backup_state": "not-required",
        "candidate_sha256": candidate_sha256,
        "publication_state": "intent",
    }
    if existed:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Vault builder manifest must be a regular file.")
        contents = manifest_path.read_bytes()
        manifest["sha256"] = hashlib.sha256(contents).hexdigest()
        manifest["mode"] = _observed_mode(manifest_path)
        manifest["backup_state"] = "intent"
    journal["manifest"] = manifest
    _write_journal_state(journal_root, journal, "validating")
    if existed:
        _manifest_backup_step("after-intent")
        _write_original(_journal_originals(journal_root), _MANIFEST_RELATIVE, contents, str(manifest["mode"]))
        _manifest_backup_step("after-original")
        manifest["backup_state"] = "complete"
        journal["manifest"] = manifest
        _write_journal_state(journal_root, journal, "validating")


def _manifest_backup_step(_stage: str) -> None:
    """Hard-stop injection seam around prior-manifest backup completion."""


def _manifest_step(_stage: str) -> None:
    """Hard-stop injection seam around candidate manifest publication."""


def _replace_from_builder(
    builder: Path,
    target: Path,
    action: MigrationAction,
    *,
    migration_id: str,
    directory_nonce: str,
    record_directory: Callable[[str, str], None],
) -> None:
    source = _safe_relative_path(builder, action.path)
    destination = _safe_target_path(target, action.path, reject_leaf_symlink=False)
    _ensure_managed_parent(
        target,
        destination,
        action.path,
        migration_id=migration_id,
        directory_nonce=directory_nonce,
        record_directory=record_directory,
    )
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


def _ensure_managed_parent(
    target: Path,
    destination: Path,
    managed_path: str,
    *,
    migration_id: str,
    directory_nonce: str,
    record_directory: Callable[[str, str], None],
) -> None:
    relative_parent = destination.parent.relative_to(target)
    current = target
    for part in relative_parent.parts:
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError:
            relative = current.relative_to(target).as_posix()
            _directory_step("before-intent", relative)
            record_directory(relative, "intent")
            _directory_step("after-intent", relative)
            journal_root = _journal_root(target, migration_id, create=False)
            _prepare_directory_marker(
                journal_root,
                migration_id,
                directory_nonce,
                relative,
            )
            _mkdir_directory(current)
            _directory_step("after-mkdir", relative)
            _publish_directory_marker(journal_root, current, migration_id, directory_nonce, relative)
            _directory_step("after-marker", relative)
            record_directory(relative, "created")
            continue
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise ValueError(f"Migration target parent contains an unsafe path: {managed_path}")


def _mkdir_directory(path: Path) -> None:
    path.mkdir()
    _fsync_directory(path.parent)


def _directory_step(_stage: str, _relative: str) -> None:
    """Failure-injection seam for directory transaction crash windows."""


def _prepare_directory_marker(journal_root: Path, migration_id: str, nonce: str, relative: str) -> Path:
    marker_directory = _journal_path(journal_root, "directory-markers")
    try:
        directory_state = marker_directory.lstat()
    except FileNotFoundError:
        marker_directory.mkdir()
        _fsync_directory(journal_root)
    else:
        if stat.S_ISLNK(directory_state.st_mode) or not stat.S_ISDIR(directory_state.st_mode):
            raise ValueError("Migration recovery directory markers are invalid.")
    marker = _directory_marker_source(journal_root, relative)
    payload = _directory_marker_payload(migration_id, nonce, relative)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"Migration recovery directory marker already exists: {relative}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if handle.write(payload) != len(payload):
                raise OSError("Migration recovery directory marker write was incomplete.")
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
    except BaseException:
        marker.unlink(missing_ok=True)
        _fsync_directory(marker_directory)
        raise
    _fsync_directory(marker_directory)
    _validate_staged_directory_marker(journal_root, migration_id, nonce, relative)
    return marker


def _publish_directory_marker(journal_root: Path, directory: Path, migration_id: str, nonce: str, relative: str) -> None:
    prepared_marker = _validate_staged_directory_marker(journal_root, migration_id, nonce, relative)
    marker = directory / _DIRECTORY_MARKER_FILENAME
    try:
        os.link(prepared_marker, marker, follow_symlinks=False)
    except FileExistsError as exc:
        raise ValueError(f"Migration directory marker already exists: {relative}") from exc
    _fsync_directory(prepared_marker.parent)
    _fsync_directory(directory)
    _validate_directory_marker_identity(
        journal_root,
        directory,
        migration_id,
        nonce,
        relative,
        require_expected_contents=True,
    )


def _directory_marker(migration_id: str, nonce: str, relative: str) -> dict[str, object]:
    return {"migration_id": migration_id, "nonce": nonce, "path": relative, "schema_version": 1}


def _directory_marker_source(journal_root: Path, relative: str) -> Path:
    marker_directory = _journal_path(journal_root, "directory-markers")
    marker_name = f"{hashlib.sha256(relative.encode('utf-8')).hexdigest()}.json"
    return _journal_path(marker_directory, marker_name)


def _directory_marker_payload(migration_id: str, nonce: str, relative: str) -> bytes:
    return _canonical_json(_directory_marker(migration_id, nonce, relative)).encode("utf-8")


def _validate_staged_directory_marker(journal_root: Path, migration_id: str, nonce: str, relative: str) -> Path:
    try:
        marker = _directory_marker_source(journal_root, relative)
        marker_state = marker.lstat()
        payload = _directory_marker_payload(migration_id, nonce, relative)
        contents = marker.read_bytes()
    except (OSError, ValueError) as exc:
        raise ValueError(f"Migration recovery directory marker is invalid: {relative}") from exc
    if (
        stat.S_ISLNK(marker_state.st_mode)
        or not stat.S_ISREG(marker_state.st_mode)
        or f"{marker_state.st_mode & 0o7777:04o}" != "0644"
        or contents != payload
        or hashlib.sha256(contents).digest() != hashlib.sha256(payload).digest()
    ):
        raise ValueError(f"Migration recovery directory marker is invalid: {relative}")
    return marker


def _validate_directory_marker_identity(
    journal_root: Path,
    directory: Path,
    migration_id: str,
    nonce: str,
    relative: str,
    *,
    require_expected_contents: bool,
) -> Path:
    try:
        directory_state = directory.lstat()
        source = _directory_marker_source(journal_root, relative)
        source_state = source.lstat()
        marker = directory / _DIRECTORY_MARKER_FILENAME
        marker_state = marker.lstat()
    except (OSError, ValueError) as exc:
        raise ValueError(f"Migration directory marker identity is invalid: {relative}") from exc
    if (
        stat.S_ISLNK(directory_state.st_mode)
        or not stat.S_ISDIR(directory_state.st_mode)
        or stat.S_ISLNK(source_state.st_mode)
        or not stat.S_ISREG(source_state.st_mode)
        or stat.S_ISLNK(marker_state.st_mode)
        or not stat.S_ISREG(marker_state.st_mode)
        or (source_state.st_dev, source_state.st_ino) != (marker_state.st_dev, marker_state.st_ino)
    ):
        raise ValueError(f"Migration directory marker identity is invalid: {relative}")
    if require_expected_contents:
        payload = _directory_marker_payload(migration_id, nonce, relative)
        try:
            source_contents = source.read_bytes()
            marker_contents = marker.read_bytes()
        except OSError as exc:
            raise ValueError(f"Migration directory marker is invalid: {relative}") from exc
        if (
            f"{source_state.st_mode & 0o7777:04o}" != "0644"
            or f"{marker_state.st_mode & 0o7777:04o}" != "0644"
            or source_contents != payload
            or marker_contents != payload
            or hashlib.sha256(source_contents).digest() != hashlib.sha256(payload).digest()
            or hashlib.sha256(marker_contents).digest() != hashlib.sha256(payload).digest()
        ):
            raise ValueError(f"Migration directory marker is invalid: {relative}")
    return marker


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
    entries = _journal_entries(journal)
    originals = _journal_originals(journal_root, create=False)
    manifest = _journal_manifest(journal, allow_none=True)
    _validate_original_tree(journal_root, entries, manifest)
    _validate_entry_recovery_targets(target, entries)
    if manifest is not None:
        manifest_state = _validate_manifest_recovery_target(target, manifest)
        if manifest_state == "candidate" and manifest["existed"]:
            _require_original(
                _journal_path(originals, _MANIFEST_RELATIVE),
                str(manifest["sha256"]),
                str(manifest["mode"]),
            )
    directories = _validate_directory_records(
        target,
        journal_root,
        str(journal["migration_id"]),
        str(journal["directory_nonce"]),
        entries,
        journal.get("directories"),
    )
    _write_journal_state(journal_root, journal, "rolling-back")
    if manifest is not None:
        _restore_manifest(target, originals, manifest)
    for entry in reversed(entries):
        _restore_entry(target, originals, entry)
    for record in sorted(directories, key=lambda item: len(PurePosixPath(str(item["path"])).parts), reverse=True):
        _recover_directory(target, journal_root, str(journal["migration_id"]), str(journal["directory_nonce"]), record)
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


def _validate_entry_recovery_targets(
    target: Path,
    entries: tuple[dict[str, object], ...],
) -> None:
    for entry in entries:
        relative = str(entry["path"])
        destination = _safe_target_path(target, relative, reject_leaf_symlink=False)
        try:
            state = destination.lstat()
        except FileNotFoundError:
            if entry["existed"]:
                raise ValueError(f"Refusing to restore a missing managed recovery file: {relative}")
            continue
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
            raise ValueError(f"Refusing to restore an unsafe managed recovery file: {relative}")
        current_sha256 = sha256_file(destination)
        current_mode = _observed_mode(destination)
        prior_matches = (
            entry["existed"]
            and current_sha256 == entry["sha256"]
            and current_mode == entry["mode"]
        )
        applied_matches = (
            current_sha256 == entry.get("applied_sha256")
            and current_mode == entry.get("applied_mode")
        )
        if not prior_matches and not applied_matches:
            raise ValueError(f"Refusing to overwrite an unknown managed recovery file: {relative}")


def _restore_manifest(target: Path, originals: Path, manifest: dict[str, object]) -> None:
    current_state = _validate_manifest_recovery_target(target, manifest)
    destination = _safe_target_path(target, _MANIFEST_RELATIVE, reject_leaf_symlink=False)
    candidate_sha256 = str(manifest["candidate_sha256"])
    try:
        state = destination.lstat()
    except FileNotFoundError:
        state = None
    if manifest["existed"]:
        if current_state == "prior":
            return
        source = _journal_path(originals, _MANIFEST_RELATIVE)
        _require_original(source, str(manifest["sha256"]), str(manifest["mode"]))
        _ensure_regular_parent(target, destination)
        _atomic_write(destination, source.read_bytes(), str(manifest["mode"]))
        return
    if state is not None:
        _delete_transaction_file(destination, candidate_sha256)


def _validate_manifest_recovery_target(target: Path, manifest: dict[str, object]) -> str:
    destination = _safe_target_path(target, _MANIFEST_RELATIVE, reject_leaf_symlink=False)
    candidate_sha256 = str(manifest["candidate_sha256"])
    try:
        state = destination.lstat()
    except FileNotFoundError:
        state = None
    if state is not None:
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
            raise ValueError("Refusing to restore an unsafe Vault builder manifest.")
        current_sha256 = sha256_file(destination)
        current_mode = _observed_mode(destination)
        prior_matches = (
            manifest["existed"]
            and current_sha256 == manifest["sha256"]
            and current_mode == manifest["mode"]
        )
        candidate_matches = current_sha256 == candidate_sha256
        if prior_matches:
            return "prior"
        if candidate_matches:
            return "candidate"
        raise ValueError("Refusing to overwrite an unknown Vault builder manifest.")
    elif manifest["existed"]:
        raise ValueError("Refusing to restore a missing prior Vault builder manifest.")
    return "absent"


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
    nonce = data.get("directory_nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise ValueError("Migration recovery directory nonce is invalid.")
    _validate_directory_records(target, journal_root, migration_id, nonce, entries, data.get("directories"))
    for key in ("state_directory_existed", "recovery_directory_existed"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError("Migration recovery journal state is invalid.")
    _validate_original_tree(journal_root, entries, manifest)
    return data


def _journal_required_keys() -> frozenset[str]:
    return frozenset(
        {"schema_version", "migration_id", "target_contract_version", "state", "entries", "manifest", "directory_nonce", "directories"}
    )


def _journal_optional_keys() -> frozenset[str]:
    return _journal_required_keys() | frozenset({"state_directory_existed", "recovery_directory_existed"})


def _journal_entries(journal: dict[str, object]) -> tuple[dict[str, object], ...]:
    raw_entries = journal.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Migration recovery journal entries are invalid.")
    parsed: list[dict[str, object]] = []
    paths: set[str] = set()
    for raw in raw_entries:
        allowed_keys = {
            "path",
            "existed",
            "sha256",
            "mode",
            "applied_sha256",
            "applied_mode",
        }
        if not isinstance(raw, dict) or set(raw) - allowed_keys or not {"path", "existed", "sha256", "mode"} <= set(raw):
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
        applied_mode = raw.get("applied_mode")
        if applied_mode is not None and (not isinstance(applied_mode, str) or not _MODE_RE.fullmatch(applied_mode)):
            raise ValueError("Migration recovery journal applied mode is invalid.")
        if (applied is None) != (applied_mode is None):
            raise ValueError("Migration recovery journal applied state is invalid.")
        parsed.append(
            {
                "path": path,
                "existed": existed,
                "sha256": digest,
                "mode": mode,
                "applied_sha256": applied,
                "applied_mode": applied_mode,
            }
        )
    if tuple(item["path"] for item in parsed) != tuple(sorted(str(item["path"]) for item in parsed)):
        raise ValueError("Migration recovery journal entries must be sorted.")
    return tuple(parsed)


def _journal_manifest(journal: dict[str, object], *, allow_none: bool) -> dict[str, object] | None:
    raw = journal.get("manifest")
    if raw is None and allow_none:
        return None
    expected_keys = {
        "existed",
        "sha256",
        "mode",
        "backup_state",
        "candidate_sha256",
        "publication_state",
    }
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise ValueError("Migration recovery manifest state is invalid.")
    existed, digest, mode = raw.get("existed"), raw.get("sha256"), raw.get("mode")
    if not isinstance(existed, bool):
        raise ValueError("Migration recovery manifest state is invalid.")
    if existed:
        if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest) or not isinstance(mode, str) or not _MODE_RE.fullmatch(mode):
            raise ValueError("Migration recovery manifest hash or mode is invalid.")
    elif digest is not None or mode is not None:
        raise ValueError("Migration recovery manifest state is invalid.")
    candidate_sha256 = raw.get("candidate_sha256")
    backup_state = raw.get("backup_state")
    publication_state = raw.get("publication_state")
    if not isinstance(candidate_sha256, str) or not _HEX_RE.fullmatch(candidate_sha256):
        raise ValueError("Migration recovery manifest candidate hash is invalid.")
    if publication_state not in {"intent", "published"}:
        raise ValueError("Migration recovery manifest publication state is invalid.")
    if existed and backup_state not in {"intent", "complete"}:
        raise ValueError("Migration recovery manifest backup state is invalid.")
    if not existed and backup_state != "not-required":
        raise ValueError("Migration recovery manifest backup state is invalid.")
    return {
        "existed": existed,
        "sha256": digest,
        "mode": mode,
        "backup_state": backup_state,
        "candidate_sha256": candidate_sha256,
        "publication_state": publication_state,
    }


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
        manifest_original = _journal_path(originals, _MANIFEST_RELATIVE)
        if manifest["backup_state"] == "complete" or manifest_original.exists():
            expected.add(_MANIFEST_RELATIVE)
            _require_original(manifest_original, str(manifest["sha256"]), str(manifest["mode"]))
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
    if journal.get("state") == "complete":
        _remove_created_markers(
            target,
            journal_root,
            str(journal["migration_id"]),
            str(journal["directory_nonce"]),
            _directory_records(journal),
        )
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
    return shlex.join(
        [
            "python3",
            "tools/vault_migration/cli.py",
            "recover",
            "--target",
            str(target),
            "--migration-id",
            migration_id,
        ]
    )


def _audit_command(target: Path) -> str:
    return shlex.join(
        [
            "python3",
            "tools/vault_migration/cli.py",
            "audit",
            "--target",
            str(target),
        ]
    )


def _failed_command(error: BaseException) -> str | None:
    if not isinstance(error, subprocess.CalledProcessError):
        return None
    command = error.cmd
    if isinstance(command, str):
        return command
    try:
        return shlex.join(str(part) for part in command)
    except TypeError:
        return str(command)


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


def _directory_records(journal: dict[str, object]) -> list[dict[str, object]]:
    value = journal.get("directories")
    if not isinstance(value, list):
        raise ValueError("Migration recovery directory records are invalid.")
    return value  # type: ignore[return-value]


def _validate_directory_records(
    target: Path,
    journal_root: Path,
    migration_id: str,
    nonce: str,
    entries: tuple[dict[str, object], ...],
    value: object,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("Migration recovery directory records are invalid.")
    directories: list[dict[str, object]] = []
    paths: set[str] = set()
    allowed: set[str] = set()
    for entry in entries:
        if entry["existed"]:
            continue
        parent = PurePosixPath(str(entry["path"])).parent
        while parent.parts:
            allowed.add(parent.as_posix())
            parent = parent.parent
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"existed", "path", "state"}:
            raise ValueError("Migration recovery directory records are invalid.")
        relative, existed, lifecycle = raw.get("path"), raw.get("existed"), raw.get("state")
        if not isinstance(relative, str) or existed is not False or lifecycle not in {"intent", "created"}:
            raise ValueError("Migration recovery directory records are invalid.")
        if relative in paths or relative not in allowed:
            raise ValueError("Migration recovery directory records are invalid.")
        _validate_directory_relative(relative)
        paths.add(relative)
        directory = _safe_target_path(target, relative, reject_leaf_symlink=False)
        if lifecycle == "created":
            _validate_directory_marker_identity(
                journal_root,
                directory,
                migration_id,
                nonce,
                relative,
                require_expected_contents=False,
            )
        elif directory.exists() and _marker_exists(directory):
            _validate_directory_marker_identity(
                journal_root,
                directory,
                migration_id,
                nonce,
                relative,
                require_expected_contents=False,
            )
        directories.append({"existed": False, "path": relative, "state": lifecycle})
    if tuple(record["path"] for record in directories) != tuple(sorted(str(record["path"]) for record in directories)):
        raise ValueError("Migration recovery directory records must be sorted.")
    return tuple(directories)


def _remove_created_markers(
    target: Path,
    journal_root: Path,
    migration_id: str,
    nonce: str,
    directories: list[dict[str, object]],
) -> None:
    markers = [
        _validate_directory_marker_identity(
            journal_root,
            _safe_target_path(target, str(record["path"]), reject_leaf_symlink=False),
            migration_id,
            nonce,
            str(record["path"]),
            require_expected_contents=False,
        )
        for record in directories
        if record["state"] == "created"
    ]
    for marker in markers:
        marker.unlink()
        _fsync_directory(marker.parent)


def _recover_directory(
    target: Path,
    journal_root: Path,
    migration_id: str,
    nonce: str,
    record: dict[str, object],
) -> None:
    relative = str(record["path"])
    directory = _safe_target_path(target, relative, reject_leaf_symlink=False)
    if not directory.exists():
        if record["state"] == "created":
            raise ValueError(f"Migration directory marker is invalid: {relative}")
        return
    marker = (
        _validate_directory_marker_identity(
            journal_root,
            directory,
            migration_id,
            nonce,
            relative,
            require_expected_contents=False,
        )
        if _marker_exists(directory)
        else None
    )
    if record["state"] == "created" and marker is None:
        raise ValueError(f"Migration directory marker is invalid: {relative}")
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise ValueError(f"Migration directory is unreadable: {relative}") from exc
    if marker is None:
        if children:
            raise ValueError(f"Migration directory contains unexpected content: {relative}")
        directory.rmdir()
        _fsync_directory(directory.parent)
        return
    if children != [marker]:
        raise ValueError(f"Migration directory contains unexpected content: {relative}")
    marker.unlink()
    _fsync_directory(directory)
    directory.rmdir()
    _fsync_directory(directory.parent)


def _marker_exists(directory: Path) -> bool:
    marker = directory / _DIRECTORY_MARKER_FILENAME
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("Migration directory marker is invalid.") from exc
    return True


def _validate_directory_relative(relative: str) -> None:
    candidate = PurePosixPath(relative)
    if "\\" in relative or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Migration recovery directory records are invalid.")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result
