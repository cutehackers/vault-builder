"""Apply approved Vault migration plans as recoverable filesystem transactions."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

try:
    import fcntl
except ImportError:  # pragma: no cover - supported migrations are POSIX-only
    fcntl = None  # type: ignore[assignment]

from .catalogs import _safe_relative_path, _validate_root, load_catalog, sha256_file
from .durability import (
    exchange_directory_entries as _exchange_directory_entries,
    fsync_directory as _strict_fsync_directory,
    move_directory_entry_no_replace as _move_directory_entry_no_replace,
)
from .managed_paths import MANAGED_PATHS
from .manifests import create_manifest, load_manifest
from .models import MigrationAction, MigrationPlan, VaultManifest
from .planning import _target_root, audit_vault, target_fingerprint


_V1_CATALOG_PATH = "tools/vault_migration/contracts/v1.json"
_MIGRATION_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_RELEASE_GATE_COMMAND = ("bash", "scripts/release_gate.sh")
_GATE_OUTPUT_TAIL_LINES = 20
_JOURNAL_STATES = frozenset(
    {
        "preparing",
        "preparation-cleanup-pending",
        "prepared",
        "applying",
        "validating",
        "rolling-back",
        "rollback-failed",
        "rollback-cleanup-pending",
        "cleanup-pending",
        "complete",
    }
)
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MODE_RE = re.compile(r"^[0-7]{4}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_RELATIVE = ".vault-builder/manifest.json"
_MANIFEST_MODE = "0644"
_MANIFEST_BACKUP_STAGE = "staged/prior-manifest.json"
_DIRECTORY_MARKER_FILENAME = ".vault-builder-migration.json"
_PREPARING_PREFIX = ".preparing-"
_CLEANUP_PREFIX = ".cleanup-"
_ROLLBACK_CLEANUP_PREFIX = ".rollback-cleanup-"
_PREPARING_ARTIFACT_RE = re.compile(
    r"^\.preparing-([0-9a-f]{16})-(?:keep|remove)-recovery-(?:keep|remove)-state$"
)
_CLEANUP_ARTIFACT_RE = re.compile(
    r"^\.cleanup-([0-9a-f]{16})-(?:keep|remove)-recovery$"
)
_ROLLBACK_CLEANUP_ARTIFACT_RE = re.compile(
    r"^\.rollback-cleanup-([0-9a-f]{16})-(?:keep|remove)-recovery-(?:keep|remove)-state$"
)
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DIRECTORY_FSYNC_UNSUPPORTED = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)
_SYSTEM_SYMLINK_COMPONENTS = frozenset(
    {Path("/etc"), Path("/tmp"), Path("/var")}
)


@dataclass(frozen=True)
class _ExpectedDestinationState:
    existed: bool
    sha256: str | None
    mode: str | None
    manifest: VaultManifest | None = None
    last_migration_id: str | None = None


_ABSENT_DESTINATION = _ExpectedDestinationState(False, None, None)


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
        rollback_cleanup_pending: bool = False,
    ) -> None:
        self.original_error = original_error
        self.rollback_error = rollback_error
        self.rollback_completed = rollback_completed
        self.rollback_cleanup_pending = rollback_cleanup_pending
        self.failed_command = _failed_command(original_error)
        self.exit_code = (
            original_error.returncode
            if isinstance(original_error, subprocess.CalledProcessError)
            else None
        )
        self.audit_command = (
            _audit_command(target)
            if rollback_completed and not rollback_cleanup_pending
            else None
        )
        self.recover_command = (
            _recover_command(target, migration_id)
            if rollback_cleanup_pending or not rollback_completed
            else None
        )
        lines = ["Migration failed after transaction start."]
        if self.failed_command is not None:
            lines.append(f"Failed command: {self.failed_command}")
            lines.append(f"Exit code: {self.exit_code}")
            lines.extend(_output_tail_lines(original_error))
        else:
            lines.append(f"Failure: {original_error}")
        if rollback_cleanup_pending:
            lines.append("Automatic rollback: completed; cleanup pending")
            lines.append(f"Finish cleanup with: {self.recover_command}")
        elif rollback_completed:
            lines.append("Automatic rollback: completed")
            lines.append(f"Run read-only audit again: {self.audit_command}")
        else:
            lines.append("Automatic rollback: failed")
            lines.append(f"Recover with: {self.recover_command}")
        super().__init__("\n".join(lines))


class MigrationCleanupPendingError(RuntimeError):
    """A committed migration whose cleanup must be resumed explicitly."""

    def __init__(
        self,
        cleanup_error: BaseException,
        *,
        target: Path,
        migration_id: str,
    ) -> None:
        self.cleanup_error = cleanup_error
        self.migration_id = migration_id
        self.recover_command = _recover_command(target, migration_id)
        super().__init__(
            "\n".join(
                [
                    "Migration committed, cleanup pending.",
                    f"Cleanup failure: {cleanup_error}",
                    f"Finish cleanup with: {self.recover_command}",
                ]
            )
        )


class _JournalPreparationFailure(Exception):
    def __init__(
        self,
        original_error: BaseException,
        cleanup_error: BaseException | None,
    ) -> None:
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        super().__init__(str(original_error))


class _RollbackCleanupPending(Exception):
    """A completed rollback whose journal cleanup must be resumed."""

    def __init__(self, cleanup_error: BaseException) -> None:
        self.cleanup_error = cleanup_error
        super().__init__(f"Rollback completed, cleanup pending: {cleanup_error}")


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
    lock_descriptor = _acquire_migration_lock(target)
    try:
        return _apply_plan_locked(builder, target, plan, run_gate)
    finally:
        _release_migration_lock(lock_descriptor)


def _apply_plan_locked(
    builder: Path,
    target: Path,
    plan: MigrationPlan,
    run_gate: Callable[[Path], None] | None,
) -> VaultManifest:
    _require_no_outstanding_migrations(target)
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
    try:
        for action in actions:
            _stage_managed_candidate(builder, journal_root, action)
        _verify_current_actions(target, actions)
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        try:
            _cleanup_preparing(journal_root, journal)
        except BaseException as cleanup_exc:
            cleanup_error = cleanup_exc
        raise MigrationApplyError(
            exc,
            target=target,
            migration_id=checked_plan.migration_id,
            rollback_completed=cleanup_error is None,
            rollback_error=cleanup_error,
        ) from (cleanup_error or exc)
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
                staged_ready=True,
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
        _publish_candidate_manifest(
            target,
            journal_root,
            journal,
            candidate_bytes,
            candidate_sha256,
        )
        manifest_path = target / _MANIFEST_RELATIVE
        if (
            sha256_file(manifest_path) != candidate_sha256
            or _observed_mode(manifest_path) != _MANIFEST_MODE
        ):
            raise ValueError("Candidate Vault builder manifest verification failed.")
        manifest_state = _journal_manifest(journal, allow_none=False)
        assert manifest_state is not None
        manifest_state["publication_state"] = "published"
        journal["manifest"] = manifest_state
        _write_journal_state(journal_root, journal, "validating")

        # Gate two intentionally sees authority-v1 metadata.
        gate(target)
        _verify_candidate_files(builder, target, actions)
        _write_journal_state(journal_root, journal, "cleanup-pending")
    except BaseException as exc:
        try:
            _rollback(target, journal_root, journal)
        except _RollbackCleanupPending as cleanup_exc:
            raise MigrationApplyError(
                exc,
                target=target,
                migration_id=checked_plan.migration_id,
                rollback_completed=True,
                rollback_error=cleanup_exc.__cause__ or cleanup_exc,
                rollback_cleanup_pending=True,
            ) from cleanup_exc
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
    try:
        _cleanup_committed(target, journal_root, journal)
    except BaseException as cleanup_exc:
        raise MigrationCleanupPendingError(
            cleanup_exc,
            target=target,
            migration_id=checked_plan.migration_id,
        ) from cleanup_exc
    return candidate


def recover_migration(
    target_root: Path,
    migration_id: str,
) -> Literal["rolled-back", "cleanup-completed"]:
    """Restore an interrupted transaction without consulting builder desired bytes."""
    if not isinstance(migration_id, str) or not _MIGRATION_ID_RE.fullmatch(migration_id):
        raise ValueError("migration_id must be 16 lowercase hexadecimal characters.")
    target = _target_root(target_root)
    lock_descriptor = _acquire_migration_lock(target)
    try:
        return _recover_migration_locked(target, migration_id)
    finally:
        _release_migration_lock(lock_descriptor)


def _recover_migration_locked(
    target: Path,
    migration_id: str,
) -> Literal["rolled-back", "cleanup-completed"]:
    journal_root = _journal_root(target, migration_id, create=False)
    preparing_roots = tuple(
        path
        for path in _preparing_journal_root_candidates(target, migration_id)
        if path.exists() or path.is_symlink()
    )
    if len(preparing_roots) > 1:
        raise ValueError("Multiple migration preparation roots exist.")
    if preparing_roots:
        _recover_preparing_root(
            target,
            journal_root,
            preparing_roots[0],
            migration_id,
        )
        return "rolled-back"
    tombstones = tuple(
        path
        for path in _cleanup_tombstone_candidates(journal_root)
        if path.exists() or path.is_symlink()
    )
    if len(tombstones) > 1:
        raise ValueError("Multiple migration cleanup tombstones exist.")
    if tombstones:
        rolled_back = _resume_cleanup_tombstone(target, tombstones[0], migration_id)
        return "rolled-back" if rolled_back else "cleanup-completed"
    if not journal_root.exists() and not journal_root.is_symlink():
        manifest = load_manifest(target)
        if manifest is not None and manifest.last_migration_id == migration_id:
            return "cleanup-completed"
        raise ValueError("Migration recovery journal not found.")
    journal = _load_journal(target, journal_root, migration_id)
    if journal["state"] == "complete":
        raise ValueError("Migration recovery journal is complete and cannot be recovered.")
    _validate_recovery_manifest_fence(target, journal)
    if journal["state"] == "preparing":
        _cleanup_preparing(journal_root, journal)
        return "rolled-back"
    if journal["state"] == "preparation-cleanup-pending":
        _cleanup_rolled_back(journal_root, journal)
        return "rolled-back"
    if journal["state"] == "cleanup-pending":
        _cleanup_committed(target, journal_root, journal)
        return "cleanup-completed"
    if journal["state"] == "rollback-cleanup-pending":
        _verify_restored(
            target,
            _journal_originals(journal_root, create=False),
            _journal_entries(journal),
            _journal_manifest(journal, allow_none=True),
        )
        _cleanup_rolled_back(journal_root, journal)
        return "rolled-back"
    _rollback(target, journal_root, journal)
    return "rolled-back"


def _acquire_migration_lock(target: Path) -> int:
    if fcntl is None:
        raise RuntimeError(
            "Vault migration locking is unavailable on this platform."
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise ValueError("Vault target root is unreadable.") from exc
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise ValueError(
                "Another Vault migration operation is active; retry after "
                "it completes."
            ) from exc
        raise RuntimeError("Vault migration locking failed.") from exc
    held = os.fstat(descriptor)
    current = target.stat()
    if (
        not stat.S_ISDIR(held.st_mode)
        or held.st_dev != current.st_dev
        or held.st_ino != current.st_ino
    ):
        _release_migration_lock(descriptor)
        raise ValueError("Vault target root changed while acquiring its lock.")
    return descriptor


def _release_migration_lock(descriptor: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


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
    if any(
        path.exists() or path.is_symlink()
        for path in _preparing_journal_root_candidates(
            target,
            checked.migration_id,
        )
    ):
        raise ValueError("Migration preparation root already exists for this migration ID.")
    return checked, tuple(sorted(actions, key=lambda action: action.path))


def _require_no_outstanding_migrations(target: Path) -> None:
    migration_ids = _outstanding_migration_ids(target)
    if not migration_ids:
        return
    heading = (
        "Multiple outstanding migrations block apply; recover all of them:"
        if len(migration_ids) > 1
        else "An outstanding migration blocks apply; recover it:"
    )
    commands = [
        f"- {_recover_command(target, migration_id)}"
        for migration_id in migration_ids
    ]
    raise ValueError("\n".join([heading, *commands]))


def _outstanding_migration_ids(target: Path) -> tuple[str, ...]:
    migration_ids: set[str] = set()
    recovery = _safe_target_path(
        target,
        ".vault-builder/recovery",
        reject_leaf_symlink=False,
    )
    try:
        recovery_state = recovery.lstat()
    except FileNotFoundError:
        recovery_state = None
    if recovery_state is not None:
        if stat.S_ISLNK(recovery_state.st_mode) or not stat.S_ISDIR(
            recovery_state.st_mode
        ):
            raise ValueError("Vault recovery directory must be a regular directory.")
        for artifact in recovery.iterdir():
            if _MIGRATION_ID_RE.fullmatch(artifact.name):
                migration_ids.add(artifact.name)
                continue
            cleanup_match = _CLEANUP_ARTIFACT_RE.fullmatch(artifact.name)
            if cleanup_match is not None:
                migration_ids.add(cleanup_match.group(1))

    for artifact in target.iterdir():
        for pattern in (
            _PREPARING_ARTIFACT_RE,
            _ROLLBACK_CLEANUP_ARTIFACT_RE,
        ):
            match = pattern.fullmatch(artifact.name)
            if match is not None:
                migration_ids.add(match.group(1))
                break
    return tuple(sorted(migration_ids))


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

    entries: list[dict[str, object]] = []
    backups: list[tuple[str, bytes, str]] = []
    for action in actions:
        existed, contents = _snapshot_current_action(target, action)
        entry: dict[str, object] = {
            "path": action.path,
            "existed": existed,
            "sha256": action.current_sha256,
            "mode": action.current_mode,
            "applied_sha256": action.desired_sha256,
            "applied_mode": action.desired_mode,
        }
        if existed:
            assert contents is not None
            backups.append(
                (action.path, contents, str(action.current_mode))
            )
        entries.append(entry)
    journal: dict[str, object] = {
        "schema_version": 1,
        "migration_id": plan.migration_id,
        "target_contract_version": plan.target_contract_version,
        "state": "preparing",
        "entries": entries,
        "manifest": None,
        "manifest_precondition": _manifest_precondition(target),
        "directory_nonce": secrets.token_hex(32),
        "directories": [],
        "state_directory_existed": state_existed,
        "recovery_directory_existed": recovery_existed,
    }
    journal_root = _journal_root(target, plan.migration_id, create=False)
    preparing_root = _preparing_journal_root(
        target,
        plan.migration_id,
        recovery_existed=recovery_existed,
        state_existed=state_existed,
        create=False,
    )
    preparing_durable = False
    try:
        preparing_root = _preparing_journal_root(
            target,
            plan.migration_id,
            recovery_existed=recovery_existed,
            state_existed=state_existed,
            create=True,
        )
        _journal_bootstrap_step("after-root")
        _write_journal_state(preparing_root, journal, "preparing")
        preparing_durable = True
        _ensure_regular_parent(target, journal_root)
        source_parent = preparing_root.parent
        os.replace(preparing_root, journal_root)
        _fsync_directory(journal_root.parent)
        if source_parent != journal_root.parent:
            _fsync_directory(source_parent)
        _journal_bootstrap_step("after-publication")
        originals = _journal_originals(journal_root)
        for relative, contents, mode in backups:
            _write_original(originals, relative, contents, mode)
        _write_journal_state(journal_root, journal, "prepared")
        _journal_prepare_step("after-prepared")
        return journal_root, journal
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        try:
            if preparing_root.exists() or preparing_root.is_symlink():
                _recover_preparing_root(
                    target,
                    journal_root,
                    preparing_root,
                    plan.migration_id,
                )
            elif preparing_durable and (
                journal_root.exists() or journal_root.is_symlink()
            ):
                _cleanup_preparing(journal_root, journal)
            else:
                if journal_root.exists() or journal_root.is_symlink():
                    _remove_tree_no_symlinks(journal_root)
                if not recovery_existed:
                    _rmdir_if_empty(recovery_directory)
                if not state_existed:
                    _rmdir_if_empty(state_directory)
        except BaseException as cleanup_exc:
            cleanup_error = cleanup_exc
        raise _JournalPreparationFailure(exc, cleanup_error) from (cleanup_error or exc)


def _snapshot_current_action(
    target: Path,
    action: MigrationAction,
) -> tuple[bool, bytes | None]:
    path = _safe_target_path(
        target,
        action.path,
        reject_leaf_symlink=False,
    )
    if action.current_sha256 is None or action.current_mode is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            raise ValueError(
                "Migration target changed after audit; run audit again."
            ) from exc
        raise ValueError(
            "Migration target changed after audit; run audit again."
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(
            "Migration target changed after audit; run audit again."
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                "Migration target changed after audit; run audit again."
            )
        _snapshot_step("after-open", action.path)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        contents = b"".join(chunks)
        _snapshot_step("after-read", action.path)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError(
            "Migration target changed after audit; run audit again."
        ) from exc
    finally:
        os.close(descriptor)

    if _snapshot_identity(before) != _snapshot_identity(after):
        raise ValueError(
            "Migration target changed after audit; run audit again."
        )
    try:
        leaf = path.lstat()
    except OSError as exc:
        raise ValueError(
            "Migration target changed after audit; run audit again."
        ) from exc
    if (
        not stat.S_ISREG(leaf.st_mode)
        or leaf.st_dev != after.st_dev
        or leaf.st_ino != after.st_ino
    ):
        raise ValueError(
            "Migration target changed after audit; run audit again."
        )
    digest = hashlib.sha256(contents).hexdigest()
    mode = f"{after.st_mode & 0o7777:04o}"
    if digest != action.current_sha256 or mode != action.current_mode:
        raise ValueError(
            "Migration target changed after audit; run audit again."
        )
    return True, contents


def _snapshot_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _snapshot_step(_stage: str, _relative: str) -> None:
    """Failure and concurrent-change injection seam for stable snapshots."""


def _journal_prepare_step(_stage: str) -> None:
    """Failure-injection seam after durable journal preparation."""


def _journal_bootstrap_step(_stage: str) -> None:
    """Hard-stop injection seam around preparation-root publication."""


def _record_manifest_publication_intent(
    target: Path,
    journal_root: Path,
    journal: dict[str, object],
    candidate_sha256: str,
) -> None:
    current, contents = _validate_manifest_precondition(target, journal)
    existed = bool(current["existed"])
    manifest: dict[str, object] = {
        "existed": existed,
        "sha256": None,
        "mode": None,
        "backup_state": "not-required",
        "backup_staged_path": None,
        "candidate_sha256": candidate_sha256,
        "publication_state": "intent",
    }
    if existed:
        assert contents is not None
        manifest["sha256"] = current["sha256"]
        manifest["mode"] = current["mode"]
        manifest["backup_state"] = "intent"
        manifest["backup_staged_path"] = _MANIFEST_BACKUP_STAGE
    journal["manifest"] = manifest
    _write_journal_state(journal_root, journal, "validating")
    if existed:
        _manifest_backup_step("after-intent")
        _stage_prior_manifest_backup(
            journal_root,
            contents,
            str(manifest["sha256"]),
            str(manifest["mode"]),
        )
        _manifest_backup_step("after-original")
        manifest["backup_state"] = "complete"
        journal["manifest"] = manifest
        _write_journal_state(journal_root, journal, "validating")


def _manifest_backup_step(_stage: str) -> None:
    """Hard-stop injection seam around prior-manifest backup completion."""


def _stage_prior_manifest_backup(
    journal_root: Path,
    contents: bytes,
    expected_sha256: str,
    expected_mode: str,
) -> None:
    staged = _journal_path(journal_root, _MANIFEST_BACKUP_STAGE)
    _create_staged_file(
        journal_root,
        staged,
        contents,
        expected_mode,
        replace_partial=True,
    )
    _manifest_backup_step("after-backup-stage")
    destination = _journal_path(
        _journal_originals(journal_root),
        _MANIFEST_RELATIVE,
    )
    _ensure_regular_parent(journal_root, destination)
    _manifest_backup_step("before-backup-publication")
    _publish_staged_file(
        staged,
        destination,
        expected_sha256,
        expected_mode,
        _ABSENT_DESTINATION,
    )
    _manifest_backup_step("after-backup-publication")


def _manifest_step(_stage: str) -> None:
    """Hard-stop injection seam around candidate manifest publication."""


def _publish_candidate_manifest(
    target: Path,
    journal_root: Path,
    journal: dict[str, object],
    contents: bytes,
    expected_sha256: str,
) -> None:
    destination = _safe_target_path(
        target,
        _MANIFEST_RELATIVE,
        reject_leaf_symlink=False,
    )
    staged = _journal_path(journal_root, "staged/candidate-manifest.json")
    _create_staged_file(journal_root, staged, contents, _MANIFEST_MODE)
    _manifest_step("after-stage")
    _manifest_step("before-publication")
    current, current_contents = _validate_manifest_precondition(target, journal)
    current_manifest = (
        None
        if current_contents is None
        else VaultManifest.from_json(current_contents.decode("utf-8"))
    )
    _publish_staged_file(
        staged,
        destination,
        expected_sha256,
        _MANIFEST_MODE,
        _ExpectedDestinationState(
            bool(current["existed"]),
            current["sha256"] if isinstance(current["sha256"], str) else None,
            current["mode"] if isinstance(current["mode"], str) else None,
            current_manifest,
            (
                current["last_migration_id"]
                if isinstance(current["last_migration_id"], str)
                else None
            ),
        ),
    )
    _manifest_step("after-publication")


def _replace_from_builder(
    builder: Path,
    target: Path,
    action: MigrationAction,
    *,
    migration_id: str,
    directory_nonce: str,
    record_directory: Callable[[str, str], None],
    staged_ready: bool = False,
) -> None:
    destination = _safe_target_path(target, action.path, reject_leaf_symlink=False)
    journal_root = _journal_root(target, migration_id, create=False)
    staged = _journal_path(journal_root, f"staged/managed/{action.path}")
    if not staged_ready:
        _stage_managed_candidate(builder, journal_root, action)
    _ensure_managed_parent(
        target,
        destination,
        action.path,
        migration_id=migration_id,
        directory_nonce=directory_nonce,
        record_directory=record_directory,
    )
    _snapshot_current_action(target, action)
    _publish_staged_file(
        staged,
        destination,
        str(action.desired_sha256),
        action.desired_mode or "0644",
        _ExpectedDestinationState(
            action.action != "create",
            action.current_sha256,
            action.current_mode,
        ),
    )
    if sha256_file(destination) != action.desired_sha256 or _observed_mode(destination) != action.desired_mode:
        raise ValueError(f"Atomic replacement verification failed: {action.path}")


def _stage_managed_candidate(
    builder: Path,
    journal_root: Path,
    action: MigrationAction,
) -> None:
    source = _safe_relative_path(builder, action.path)
    staged = _journal_path(
        journal_root,
        f"staged/managed/{action.path}",
    )
    _create_staged_file(
        journal_root,
        staged,
        source.read_bytes(),
        action.desired_mode or "0644",
    )
    _managed_step("after-stage", action.path)


def _verify_current_actions(
    target: Path,
    actions: tuple[MigrationAction, ...],
) -> None:
    for action in actions:
        _snapshot_current_action(target, action)


def _managed_step(_stage: str, _relative: str) -> None:
    """Hard-stop injection seam after durable managed-file staging."""


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
            _fsync_directory(current.parent)
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


def _write_all(descriptor: int, contents: bytes) -> None:
    remaining = memoryview(contents)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Migration staged-file write was incomplete.")
        remaining = remaining[written:]


def _create_staged_file(
    journal_root: Path,
    destination: Path,
    contents: bytes,
    mode: str,
    *,
    replace_partial: bool = False,
) -> None:
    try:
        destination.relative_to(journal_root)
    except ValueError as exc:
        raise ValueError("Migration staged file must stay inside the journal.") from exc
    _ensure_regular_parent(journal_root, destination)
    if destination.exists() or destination.is_symlink():
        if not replace_partial:
            raise ValueError(f"Migration staged file already exists: {destination}")
        state = destination.lstat()
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
            raise ValueError(f"Migration staged file is unsafe: {destination}")
        destination.unlink()
        _fsync_directory(destination.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all(descriptor, contents)
        os.fchmod(descriptor, int(mode, 8))
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(destination.parent)
    if (
        sha256_file(destination) != hashlib.sha256(contents).hexdigest()
        or _observed_mode(destination) != mode
    ):
        raise ValueError(f"Migration staged-file verification failed: {destination}")


def _publish_staged_file(
    staged: Path,
    destination: Path,
    expected_sha256: str,
    expected_mode: str,
    expected_destination: _ExpectedDestinationState,
) -> None:
    source_parent = _open_directory_descriptor(staged.parent)
    try:
        destination_parent = _open_directory_descriptor(destination.parent)
    except BaseException:
        os.close(source_parent)
        raise
    try:
        staged_state = _capture_destination_state_at(
            source_parent,
            staged.name,
            staged,
        )
        if (
            not staged_state.existed
            or staged_state.sha256 != expected_sha256
            or staged_state.mode != expected_mode
        ):
            raise ValueError(f"Migration staged file is invalid: {staged}")

        _publication_step("before-destination-validation", destination)
        current = _capture_destination_state_at(
            destination_parent,
            destination.name,
            destination,
            parse_manifest=expected_destination.manifest is not None,
        )
        if current != expected_destination:
            raise ValueError(
                f"Migration destination changed before publication: {destination}"
            )
        _publication_step("after-destination-validation", destination)

        if expected_destination.existed:
            _exchange_directory_entries(
                source_parent,
                staged.name,
                destination_parent,
                destination.name,
            )
            _fsync_publication_parent(destination_parent, destination.parent)
            if _descriptor_identity(source_parent) != _descriptor_identity(
                destination_parent
            ):
                _fsync_publication_parent(source_parent, staged.parent)
            _publication_step("after-exchange", destination)
            displaced = _capture_destination_state_at(
                source_parent,
                staged.name,
                staged,
                parse_manifest=expected_destination.manifest is not None,
            )
            if displaced != expected_destination:
                try:
                    _exchange_directory_entries(
                        source_parent,
                        staged.name,
                        destination_parent,
                        destination.name,
                    )
                    _fsync_publication_parent(
                        destination_parent,
                        destination.parent,
                    )
                    if _descriptor_identity(
                        source_parent
                    ) != _descriptor_identity(destination_parent):
                        _fsync_publication_parent(source_parent, staged.parent)
                except BaseException as restore_exc:
                    raise ValueError(
                        "Migration destination changed at publication boundary "
                        f"and could not be restored: {destination}"
                    ) from restore_exc
                raise ValueError(
                    "Migration destination changed at publication boundary: "
                    f"{destination}"
                )
        else:
            try:
                os.link(
                    staged.name,
                    destination.name,
                    src_dir_fd=source_parent,
                    dst_dir_fd=destination_parent,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ValueError(
                    "Migration destination changed at publication boundary: "
                    f"{destination}"
                ) from exc
            except OSError as exc:
                raise ValueError(
                    f"Migration destination could not be published: {destination}"
                ) from exc
            _fsync_publication_parent(destination_parent, destination.parent)

        candidate = _capture_destination_state_at(
            destination_parent,
            destination.name,
            destination,
        )
        if (
            candidate.sha256 != expected_sha256
            or candidate.mode != expected_mode
        ):
            raise ValueError(
                f"Migration candidate verification failed: {destination}"
            )
        os.unlink(staged.name, dir_fd=source_parent)
        _fsync_publication_parent(source_parent, staged.parent)
    finally:
        os.close(destination_parent)
        os.close(source_parent)


def _open_directory_descriptor(path: Path) -> int:
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError(f"Migration parent path is invalid: {path}")
    raw_path = path
    path = _expand_system_directory_alias(path)
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise ValueError(
            f"Migration parent path is unreadable: {raw_path}"
        ) from exc
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            _directory_open_step(current)
            next_descriptor = os.open(
                part,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ValueError(
            f"Migration parent path contains an unsafe directory: {raw_path}"
        ) from exc


def _expand_system_directory_alias(path: Path) -> Path:
    parts = path.parts
    if len(parts) < 2:
        return path
    alias = Path(path.anchor) / parts[1]
    if alias not in _SYSTEM_SYMLINK_COMPONENTS:
        return path
    try:
        state = alias.lstat()
    except OSError as exc:
        raise ValueError(f"Migration parent path is unreadable: {path}") from exc
    if not stat.S_ISLNK(state.st_mode):
        return path
    try:
        target = Path(os.readlink(alias))
    except OSError as exc:
        raise ValueError(f"Migration parent path is unreadable: {path}") from exc
    if not target.is_absolute():
        target = Path(path.anchor) / target
    if any(part == ".." for part in target.parts):
        raise ValueError(f"Migration parent path contains an unsafe directory: {path}")
    return target.joinpath(*parts[2:])


def _directory_open_step(_path: Path) -> None:
    """Race-injection seam before opening one no-follow directory component."""


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    state = os.fstat(descriptor)
    return state.st_dev, state.st_ino


def _fsync_publication_parent(descriptor: int, path: Path) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _DIRECTORY_FSYNC_UNSUPPORTED:
            raise
    try:
        current = path.stat()
    except OSError:
        return
    if (current.st_dev, current.st_ino) == _descriptor_identity(descriptor):
        _fsync_directory(path)


def _publication_step(_stage: str, _destination: Path) -> None:
    """Concurrent-change injection seam immediately before final validation."""


def _validate_publication_destination(
    destination: Path,
    expected: _ExpectedDestinationState,
) -> None:
    current = _capture_destination_state(
        destination,
        parse_manifest=expected.manifest is not None,
    )
    if current != expected:
        raise ValueError(
            f"Migration destination changed before publication: {destination}"
        )


def _capture_destination_state(
    destination: Path,
    *,
    parse_manifest: bool = False,
) -> _ExpectedDestinationState:
    parent = _open_directory_descriptor(destination.parent)
    try:
        return _capture_destination_state_at(
            parent,
            destination.name,
            destination,
            parse_manifest=parse_manifest,
        )
    finally:
        os.close(parent)


def _capture_destination_state_at(
    parent: int,
    name: str,
    display_path: Path,
    *,
    parse_manifest: bool = False,
) -> _ExpectedDestinationState:
    try:
        leaf_before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return _ABSENT_DESTINATION
    except OSError as exc:
        raise ValueError(
            f"Migration destination is unreadable: {display_path}"
        ) from exc
    if stat.S_ISLNK(leaf_before.st_mode) or not stat.S_ISREG(leaf_before.st_mode):
        raise ValueError(
            f"Migration destination must be a regular file: {display_path}"
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        raise ValueError(
            f"Migration destination is unreadable: {display_path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                f"Migration destination must be a regular file: {display_path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        contents = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError(
            f"Migration destination is unreadable: {display_path}"
        ) from exc
    finally:
        os.close(descriptor)
    try:
        leaf_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(
            f"Migration destination changed during validation: {display_path}"
        ) from exc
    if (
        _snapshot_identity(before) != _snapshot_identity(after)
        or not stat.S_ISREG(leaf_after.st_mode)
        or (leaf_after.st_dev, leaf_after.st_ino)
        != (after.st_dev, after.st_ino)
    ):
        raise ValueError(
            f"Migration destination changed during validation: {display_path}"
        )
    manifest: VaultManifest | None = None
    last_migration_id: str | None = None
    if parse_manifest:
        try:
            manifest = VaultManifest.from_json(contents.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(
                f"Migration destination manifest is invalid: {display_path}"
            ) from exc
        last_migration_id = manifest.last_migration_id
    return _ExpectedDestinationState(
        True,
        hashlib.sha256(contents).hexdigest(),
        f"{after.st_mode & 0o7777:04o}",
        manifest,
        last_migration_id,
    )


def _write_original(originals: Path, relative: str, contents: bytes, mode: str) -> None:
    destination = _journal_path(originals, relative)
    _create_staged_file(originals, destination, contents, mode)


def _write_journal_state(journal_root: Path, journal: dict[str, object], state: str) -> None:
    if state not in _JOURNAL_STATES:
        raise ValueError("Migration journal state is invalid.")
    journal["state"] = state
    payload = _canonical_json(journal).encode("utf-8")
    destination = _journal_path(journal_root, "journal.json")
    temporary = _journal_path(journal_root, ".journal.json.next")
    expected_destination = _capture_destination_state(destination)
    _create_staged_file(
        journal_root,
        temporary,
        payload,
        "0644",
        replace_partial=True,
    )
    _publish_staged_file(
        temporary,
        destination,
        hashlib.sha256(payload).hexdigest(),
        "0644",
        expected_destination,
    )


def _rollback(target: Path, journal_root: Path, journal: dict[str, object]) -> None:
    entries = _journal_entries(journal)
    originals = _journal_originals(journal_root, create=False)
    manifest = _journal_manifest(journal, allow_none=True)
    _validate_recovery_manifest_fence(target, journal)
    _validate_original_tree(journal_root, entries, manifest)
    _reconcile_interrupted_publications(
        target,
        journal_root,
        entries,
        manifest,
    )
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
        _restore_manifest(target, journal_root, originals, manifest)
    for entry in reversed(entries):
        _restore_entry(target, journal_root, originals, entry)

    def record_directory_state(relative: str, state: str) -> None:
        record = next(
            item
            for item in _directory_records(journal)
            if item["path"] == relative
        )
        record["state"] = state
        _write_journal_state(journal_root, journal, "rolling-back")

    for record in sorted(directories, key=lambda item: len(PurePosixPath(str(item["path"])).parts), reverse=True):
        _recover_directory(
            target,
            journal_root,
            str(journal["migration_id"]),
            str(journal["directory_nonce"]),
            entries,
            record,
            record_directory_state,
        )
    _verify_restored(target, originals, entries, manifest)
    _write_journal_state(journal_root, journal, "rollback-cleanup-pending")
    try:
        _cleanup_rolled_back(journal_root, journal)
    except BaseException as cleanup_exc:
        raise _RollbackCleanupPending(cleanup_exc) from cleanup_exc


def _reconcile_interrupted_publications(
    target: Path,
    journal_root: Path,
    entries: tuple[dict[str, object], ...],
    manifest: dict[str, object] | None,
) -> None:
    for entry in entries:
        _reconcile_managed_apply_publication(
            target,
            journal_root,
            entry,
        )
    if manifest is not None:
        _reconcile_manifest_apply_publication(
            target,
            journal_root,
            manifest,
        )
    for entry in entries:
        _reconcile_managed_rollback_publication(
            target,
            journal_root,
            entry,
        )
    if manifest is not None:
        _reconcile_manifest_rollback_publication(
            target,
            journal_root,
            manifest,
        )


def _reconcile_managed_apply_publication(
    target: Path,
    journal_root: Path,
    entry: dict[str, object],
) -> None:
    relative = str(entry["path"])
    staged = _journal_path(journal_root, f"staged/managed/{relative}")
    staged_state = _capture_optional_journal_stage(staged)
    if not staged_state.existed:
        return
    applied = _ExpectedDestinationState(
        True,
        str(entry["applied_sha256"]),
        str(entry["applied_mode"]),
    )
    if staged_state == applied:
        return
    destination = _safe_target_path(
        target,
        relative,
        reject_leaf_symlink=False,
    )
    current = _capture_destination_state(destination)
    if current != applied:
        raise ValueError(
            f"Interrupted publication retained unknown managed content: {relative}"
        )
    _publish_captured_stage(staged, destination, staged_state, applied)
    prior = (
        _ExpectedDestinationState(
            True,
            str(entry["sha256"]),
            str(entry["mode"]),
        )
        if entry["existed"]
        else _ABSENT_DESTINATION
    )
    if staged_state != prior:
        raise ValueError(
            f"Interrupted publication restored external managed content: {relative}"
        )


def _reconcile_manifest_apply_publication(
    target: Path,
    journal_root: Path,
    manifest: dict[str, object],
) -> None:
    staged = _journal_path(journal_root, "staged/candidate-manifest.json")
    staged_state = _capture_optional_journal_stage(staged)
    if not staged_state.existed:
        return
    candidate = _ExpectedDestinationState(
        True,
        str(manifest["candidate_sha256"]),
        _MANIFEST_MODE,
    )
    if staged_state == candidate:
        return
    destination = _safe_target_path(
        target,
        _MANIFEST_RELATIVE,
        reject_leaf_symlink=False,
    )
    current = _capture_destination_state(destination)
    if current != candidate:
        raise ValueError(
            "Interrupted publication retained unknown manifest content."
        )
    _publish_captured_stage(staged, destination, staged_state, candidate)
    prior = (
        _ExpectedDestinationState(
            True,
            str(manifest["sha256"]),
            str(manifest["mode"]),
        )
        if manifest["existed"]
        else _ABSENT_DESTINATION
    )
    if staged_state != prior:
        raise ValueError(
            "Interrupted publication restored external manifest content."
        )


def _reconcile_managed_rollback_publication(
    target: Path,
    journal_root: Path,
    entry: dict[str, object],
) -> None:
    if not entry["existed"]:
        return
    relative = str(entry["path"])
    staged = _journal_path(
        journal_root,
        f"staged/rollback-managed/{relative}",
    )
    staged_state = _capture_optional_journal_stage(staged)
    if not staged_state.existed:
        return
    prior = _ExpectedDestinationState(
        True,
        str(entry["sha256"]),
        str(entry["mode"]),
    )
    if staged_state == prior:
        return
    applied = _ExpectedDestinationState(
        True,
        str(entry["applied_sha256"]),
        str(entry["applied_mode"]),
    )
    if staged_state == applied:
        return
    destination = _safe_target_path(
        target,
        relative,
        reject_leaf_symlink=False,
    )
    current = _capture_destination_state(destination)
    if current != prior:
        raise ValueError(
            f"Interrupted rollback retained unknown managed content: {relative}"
        )
    _publish_captured_stage(staged, destination, staged_state, prior)
    raise ValueError(
        f"Interrupted rollback restored external managed content: {relative}"
    )


def _reconcile_manifest_rollback_publication(
    target: Path,
    journal_root: Path,
    manifest: dict[str, object],
) -> None:
    if not manifest["existed"]:
        return
    staged = _journal_path(journal_root, "staged/rollback-manifest.json")
    staged_state = _capture_optional_journal_stage(staged)
    if not staged_state.existed:
        return
    prior = _ExpectedDestinationState(
        True,
        str(manifest["sha256"]),
        str(manifest["mode"]),
    )
    if staged_state == prior:
        return
    candidate = _ExpectedDestinationState(
        True,
        str(manifest["candidate_sha256"]),
        _MANIFEST_MODE,
    )
    if staged_state == candidate:
        return
    destination = _safe_target_path(
        target,
        _MANIFEST_RELATIVE,
        reject_leaf_symlink=False,
    )
    current = _capture_destination_state(destination)
    if current != prior:
        raise ValueError(
            "Interrupted rollback retained unknown manifest content."
        )
    _publish_captured_stage(staged, destination, staged_state, prior)
    raise ValueError(
        "Interrupted rollback restored external manifest content."
    )


def _publish_captured_stage(
    staged: Path,
    destination: Path,
    staged_state: _ExpectedDestinationState,
    expected_destination: _ExpectedDestinationState,
) -> None:
    if (
        not staged_state.existed
        or staged_state.sha256 is None
        or staged_state.mode is None
    ):
        raise ValueError(f"Interrupted publication stage is invalid: {staged}")
    _publish_staged_file(
        staged,
        destination,
        staged_state.sha256,
        staged_state.mode,
        expected_destination,
    )


def _capture_optional_journal_stage(
    staged: Path,
) -> _ExpectedDestinationState:
    try:
        staged.lstat()
    except FileNotFoundError:
        return _ABSENT_DESTINATION
    except OSError as exc:
        raise ValueError(
            f"Interrupted publication stage is unreadable: {staged}"
        ) from exc
    return _capture_destination_state(staged)


def _restore_entry(
    target: Path,
    journal_root: Path,
    originals: Path,
    entry: dict[str, object],
) -> None:
    relative = str(entry["path"])
    destination = _safe_target_path(target, relative, reject_leaf_symlink=False)
    if entry["existed"]:
        prior_destination = _ExpectedDestinationState(
            True,
            str(entry["sha256"]),
            str(entry["mode"]),
        )
        applied_destination = _ExpectedDestinationState(
            True,
            str(entry["applied_sha256"]),
            str(entry["applied_mode"]),
        )
        current_destination = _capture_destination_state(destination)
        if current_destination == prior_destination:
            return
        if current_destination != applied_destination:
            raise ValueError(
                f"Refusing to overwrite an unknown managed recovery file: {relative}"
            )
        source = _journal_path(originals, relative)
        _require_original(source, str(entry["sha256"]), str(entry["mode"]))
        _ensure_regular_parent(target, destination)
        staged = _journal_path(journal_root, f"staged/rollback-managed/{relative}")
        _create_staged_file(
            journal_root,
            staged,
            source.read_bytes(),
            str(entry["mode"]),
            replace_partial=True,
        )
        _publish_staged_file(
            staged,
            destination,
            str(entry["sha256"]),
            str(entry["mode"]),
            applied_destination,
        )
        return
    _delete_transaction_file(
        destination,
        journal_root,
        _journal_path(
            journal_root,
            f"staged/deleted-managed/{relative}",
        ),
        _ExpectedDestinationState(
            True,
            str(entry["applied_sha256"]),
            str(entry["applied_mode"]),
        ),
    )


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


def _restore_manifest(
    target: Path,
    journal_root: Path,
    originals: Path,
    manifest: dict[str, object],
) -> None:
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
        expected_candidate = _capture_destination_state(
            destination,
            parse_manifest=True,
        )
        if (
            expected_candidate.sha256 != candidate_sha256
            or expected_candidate.mode != _MANIFEST_MODE
        ):
            raise ValueError(
                "Refusing to overwrite an unknown Vault builder manifest."
            )
        source = _journal_path(originals, _MANIFEST_RELATIVE)
        _require_original(source, str(manifest["sha256"]), str(manifest["mode"]))
        _ensure_regular_parent(target, destination)
        staged = _journal_path(journal_root, "staged/rollback-manifest.json")
        _create_staged_file(
            journal_root,
            staged,
            source.read_bytes(),
            str(manifest["mode"]),
            replace_partial=True,
        )
        _publish_staged_file(
            staged,
            destination,
            str(manifest["sha256"]),
            str(manifest["mode"]),
            expected_candidate,
        )
        return
    _delete_transaction_file(
        destination,
        journal_root,
        _journal_path(
            journal_root,
            "staged/deleted-manifest.json",
        ),
        _ExpectedDestinationState(
            True,
            candidate_sha256,
            _MANIFEST_MODE,
        ),
    )


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
        candidate_matches = (
            current_sha256 == candidate_sha256
            and current_mode == _MANIFEST_MODE
        )
        if prior_matches:
            return "prior"
        if candidate_matches:
            return "candidate"
        raise ValueError("Refusing to overwrite an unknown Vault builder manifest.")
    elif manifest["existed"]:
        raise ValueError("Refusing to restore a missing prior Vault builder manifest.")
    return "absent"


def _delete_transaction_file(
    path: Path,
    journal_root: Path,
    quarantine: Path,
    expected: _ExpectedDestinationState,
) -> None:
    if (
        not expected.existed
        or expected.sha256 is None
        or expected.mode is None
    ):
        raise ValueError(f"Recovery deletion expectation is invalid: {path}")
    try:
        path.lstat()
        destination_exists = True
    except FileNotFoundError:
        destination_exists = False
    try:
        quarantine.lstat()
        quarantine_exists = True
    except FileNotFoundError:
        quarantine_exists = False
    if not destination_exists and not quarantine_exists:
        return
    _ensure_regular_parent(journal_root, quarantine)
    if quarantine_exists and not destination_exists:
        quarantined = _capture_destination_state(quarantine)
        if quarantined == expected:
            return
        try:
            _restore_quarantined_content(path, quarantine)
        except BaseException as exc:
            raise ValueError(
                f"Rollback retained external quarantined content: {quarantine}"
            ) from exc
        raise ValueError(
            f"Rollback restored external quarantined content: {path}"
        )
    source_parent = _open_directory_descriptor(path.parent)
    try:
        quarantine_parent = _open_directory_descriptor(quarantine.parent)
    except BaseException:
        os.close(source_parent)
        raise
    try:
        quarantined = _capture_destination_state_at(
            quarantine_parent,
            quarantine.name,
            quarantine,
        )
        current = _capture_destination_state_at(
            source_parent,
            path.name,
            path,
        )
        if quarantined.existed:
            if quarantined == expected and not current.existed:
                return
            if quarantined != expected and not current.existed:
                _move_directory_entry_no_replace(
                    quarantine_parent,
                    quarantine.name,
                    source_parent,
                    path.name,
                )
                _fsync_publication_parent(source_parent, path.parent)
                if _descriptor_identity(source_parent) != _descriptor_identity(
                    quarantine_parent
                ):
                    _fsync_publication_parent(
                        quarantine_parent,
                        quarantine.parent,
                    )
                raise ValueError(
                    f"Rollback restored external content before deletion: {path}"
                )
            raise ValueError(
                f"Rollback retained unknown quarantined content: {quarantine}"
            )
        if not current.existed:
            return
        if current != expected:
            raise ValueError(
                f"Refusing to delete a non-transaction recovery file: {path}"
            )
        _deletion_step(path)
        _move_directory_entry_no_replace(
            source_parent,
            path.name,
            quarantine_parent,
            quarantine.name,
        )
        _fsync_publication_parent(quarantine_parent, quarantine.parent)
        if _descriptor_identity(source_parent) != _descriptor_identity(
            quarantine_parent
        ):
            _fsync_publication_parent(source_parent, path.parent)
        captured = _capture_destination_state_at(
            quarantine_parent,
            quarantine.name,
            quarantine,
        )
        if captured == expected:
            return
        destination_after = _capture_destination_state_at(
            source_parent,
            path.name,
            path,
        )
        if not destination_after.existed:
            _move_directory_entry_no_replace(
                quarantine_parent,
                quarantine.name,
                source_parent,
                path.name,
            )
            _fsync_publication_parent(source_parent, path.parent)
            if _descriptor_identity(source_parent) != _descriptor_identity(
                quarantine_parent
            ):
                _fsync_publication_parent(
                    quarantine_parent,
                    quarantine.parent,
                )
            raise ValueError(
                f"Rollback restored external content after deletion race: {path}"
            )
        raise ValueError(
            f"Rollback preserved competing content at {path} and {quarantine}"
        )
    finally:
        os.close(quarantine_parent)
        os.close(source_parent)


def _deletion_step(_path: Path) -> None:
    """Race-injection seam after final rollback deletion validation."""


def _restore_quarantined_content(
    destination: Path,
    quarantine: Path,
) -> None:
    quarantine_parent = _open_directory_descriptor(quarantine.parent)
    try:
        destination_parent = _open_directory_descriptor(destination.parent)
    except BaseException:
        os.close(quarantine_parent)
        raise
    try:
        _move_directory_entry_no_replace(
            quarantine_parent,
            quarantine.name,
            destination_parent,
            destination.name,
        )
        _fsync_publication_parent(destination_parent, destination.parent)
        if _descriptor_identity(quarantine_parent) != _descriptor_identity(
            destination_parent
        ):
            _fsync_publication_parent(
                quarantine_parent,
                quarantine.parent,
            )
    finally:
        os.close(destination_parent)
        os.close(quarantine_parent)


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
    _validate_directory_records(
        target,
        journal_root,
        migration_id,
        nonce,
        entries,
        data.get("directories"),
        cleanup_pending=data["state"] == "cleanup-pending",
        rollback_complete=data["state"] == "rollback-cleanup-pending",
    )
    for key in ("state_directory_existed", "recovery_directory_existed"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError("Migration recovery journal state is invalid.")
    if data["state"] not in {
        "preparing",
        "preparation-cleanup-pending",
    }:
        _validate_original_tree(journal_root, entries, manifest)
    return data


def _journal_required_keys() -> frozenset[str]:
    return frozenset(
        {"schema_version", "migration_id", "target_contract_version", "state", "entries", "manifest", "directory_nonce", "directories"}
    )


def _journal_optional_keys() -> frozenset[str]:
    return _journal_required_keys() | frozenset(
        {
            "manifest_precondition",
            "state_directory_existed",
            "recovery_directory_existed",
        }
    )


def _manifest_precondition(target: Path) -> dict[str, object]:
    precondition, _ = _stable_manifest_snapshot(target)
    return precondition


def _stable_manifest_snapshot(
    target: Path,
) -> tuple[dict[str, object], bytes | None]:
    manifest_path = _safe_target_path(
        target,
        _MANIFEST_RELATIVE,
        reject_leaf_symlink=False,
    )
    try:
        state = manifest_path.lstat()
    except FileNotFoundError:
        return (
            {
                "existed": False,
                "last_migration_id": None,
                "mode": None,
                "sha256": None,
            },
            None,
        )
    except OSError as exc:
        raise ValueError("Vault builder manifest is unreadable.") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError("Vault builder manifest must be a regular file.")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(manifest_path, flags)
    except OSError as exc:
        raise ValueError("Vault builder manifest is unreadable.") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Vault builder manifest must be a regular file.")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        contents = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError("Vault builder manifest is unreadable.") from exc
    finally:
        os.close(descriptor)
    if _snapshot_identity(before) != _snapshot_identity(after):
        raise ValueError("Vault builder manifest changed during snapshot.")
    try:
        leaf = manifest_path.lstat()
    except OSError as exc:
        raise ValueError("Vault builder manifest changed during snapshot.") from exc
    if (
        not stat.S_ISREG(leaf.st_mode)
        or leaf.st_dev != after.st_dev
        or leaf.st_ino != after.st_ino
    ):
        raise ValueError("Vault builder manifest changed during snapshot.")
    try:
        manifest = VaultManifest.from_json(contents.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Vault builder manifest is invalid.") from exc
    return (
        {
            "existed": True,
            "last_migration_id": manifest.last_migration_id,
            "mode": f"{after.st_mode & 0o7777:04o}",
            "sha256": hashlib.sha256(contents).hexdigest(),
        },
        contents,
    )


def _validate_manifest_precondition(
    target: Path,
    journal: dict[str, object],
) -> tuple[dict[str, object], bytes | None]:
    expected = _journal_manifest_precondition(journal)
    if expected is None:
        raise ValueError(
            "Migration recovery manifest precondition is missing."
        )
    current, contents = _stable_manifest_snapshot(target)
    if current != expected:
        raise ValueError(
            "Vault builder manifest changed after migration preparation."
        )
    return current, contents


def _journal_manifest_precondition(
    journal: dict[str, object],
) -> dict[str, object] | None:
    raw = journal.get("manifest_precondition")
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"existed", "last_migration_id", "mode", "sha256"}
        or not isinstance(raw.get("existed"), bool)
    ):
        raise ValueError("Migration recovery manifest precondition is invalid.")
    existed = bool(raw["existed"])
    digest = raw.get("sha256")
    mode = raw.get("mode")
    last_migration_id = raw.get("last_migration_id")
    if existed:
        if (
            not isinstance(digest, str)
            or not _HEX_RE.fullmatch(digest)
            or not isinstance(mode, str)
            or not _MODE_RE.fullmatch(mode)
        ):
            raise ValueError(
                "Migration recovery manifest precondition is invalid."
            )
    elif digest is not None or mode is not None or last_migration_id is not None:
        raise ValueError("Migration recovery manifest precondition is invalid.")
    if last_migration_id is not None and (
        not isinstance(last_migration_id, str)
        or not _MIGRATION_ID_RE.fullmatch(last_migration_id)
    ):
        raise ValueError("Migration recovery manifest precondition is invalid.")
    return {
        "existed": existed,
        "last_migration_id": last_migration_id,
        "mode": mode,
        "sha256": digest,
    }


def _validate_recovery_manifest_fence(
    target: Path,
    journal: dict[str, object],
) -> None:
    precondition = _journal_manifest_precondition(journal)
    transaction_manifest = _journal_manifest(journal, allow_none=True)
    manifest_path = _safe_target_path(
        target,
        _MANIFEST_RELATIVE,
        reject_leaf_symlink=False,
    )
    try:
        state = manifest_path.lstat()
    except FileNotFoundError:
        state = None
    if state is None:
        prior_absent = (
            precondition is not None and not precondition["existed"]
        ) or (
            precondition is None
            and (
                transaction_manifest is None
                or not transaction_manifest["existed"]
            )
        )
        if prior_absent:
            return
        raise ValueError(
            "Stale migration recovery is fenced by the current manifest state."
        )
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError(
            "Stale migration recovery is fenced by an unsafe manifest state."
        )
    try:
        current = load_manifest(target)
    except ValueError as exc:
        raise ValueError(
            "Refusing to overwrite an unknown Vault builder manifest."
        ) from exc
    if current is None:
        raise ValueError(
            "Stale migration recovery is fenced by the current manifest state."
        )
    digest = sha256_file(manifest_path)
    mode = _observed_mode(manifest_path)
    prior_matches = (
        precondition is not None
        and precondition["existed"]
        and digest == precondition["sha256"]
        and mode == precondition["mode"]
        and current.last_migration_id == precondition["last_migration_id"]
    )
    if precondition is None and transaction_manifest is not None:
        prior_matches = (
            transaction_manifest["existed"]
            and digest == transaction_manifest["sha256"]
            and mode == transaction_manifest["mode"]
        )
    candidate_matches = (
        transaction_manifest is not None
        and digest == transaction_manifest["candidate_sha256"]
        and mode == _MANIFEST_MODE
        and current.last_migration_id == journal["migration_id"]
    )
    known_digest_with_wrong_mode = (
        precondition is not None
        and precondition["existed"]
        and digest == precondition["sha256"]
        and mode != precondition["mode"]
    ) or (
        transaction_manifest is not None
        and (
            (
                transaction_manifest["existed"]
                and digest == transaction_manifest["sha256"]
                and mode != transaction_manifest["mode"]
            )
            or (
                digest == transaction_manifest["candidate_sha256"]
                and mode != _MANIFEST_MODE
            )
        )
    )
    if known_digest_with_wrong_mode:
        raise ValueError(
            "Refusing to overwrite an unknown Vault builder manifest."
        )
    if not prior_matches and not candidate_matches:
        raise ValueError(
            "Stale migration recovery is fenced by a later Vault builder manifest."
        )


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
        "backup_staged_path",
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
    backup_staged_path = raw.get("backup_staged_path")
    publication_state = raw.get("publication_state")
    if not isinstance(candidate_sha256, str) or not _HEX_RE.fullmatch(candidate_sha256):
        raise ValueError("Migration recovery manifest candidate hash is invalid.")
    if publication_state not in {"intent", "published"}:
        raise ValueError("Migration recovery manifest publication state is invalid.")
    if existed and backup_state not in {"intent", "complete"}:
        raise ValueError("Migration recovery manifest backup state is invalid.")
    if not existed and backup_state != "not-required":
        raise ValueError("Migration recovery manifest backup state is invalid.")
    if (
        (existed and backup_staged_path != _MANIFEST_BACKUP_STAGE)
        or (not existed and backup_staged_path is not None)
    ):
        raise ValueError("Migration recovery manifest backup stage is invalid.")
    return {
        "existed": existed,
        "sha256": digest,
        "mode": mode,
        "backup_state": backup_state,
        "backup_staged_path": backup_staged_path,
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
        _fsync_directory(root.parent)
    return root


def _preparing_journal_root(
    target: Path,
    migration_id: str,
    *,
    recovery_existed: bool,
    state_existed: bool,
    create: bool,
) -> Path:
    recovery_disposition = "keep" if recovery_existed else "remove"
    state_disposition = "keep" if state_existed else "remove"
    relative = (
        f"{_PREPARING_PREFIX}{migration_id}-"
        f"{recovery_disposition}-recovery-{state_disposition}-state"
    )
    root = _safe_target_path(target, relative, reject_leaf_symlink=False)
    if create:
        _ensure_regular_parent(target, root)
        if root.exists() or root.is_symlink():
            raise ValueError(
                "Migration preparation root already exists for this migration ID."
            )
        root.mkdir()
        _fsync_directory(root.parent)
    return root


def _preparing_journal_root_candidates(
    target: Path,
    migration_id: str,
) -> tuple[Path, ...]:
    return tuple(
        _preparing_journal_root(
            target,
            migration_id,
            recovery_existed=recovery_existed,
            state_existed=state_existed,
            create=False,
        )
        for recovery_existed, state_existed in (
            (True, True),
            (True, False),
            (False, True),
            (False, False),
        )
    )


def _preparing_root_metadata(
    root: Path,
    migration_id: str,
) -> tuple[bool, bool]:
    for recovery_existed, recovery_disposition in (
        (True, "keep"),
        (False, "remove"),
    ):
        for state_existed, state_disposition in (
            (True, "keep"),
            (False, "remove"),
        ):
            if root.name == (
                f"{_PREPARING_PREFIX}{migration_id}-"
                f"{recovery_disposition}-recovery-"
                f"{state_disposition}-state"
            ):
                return recovery_existed, state_existed
    raise ValueError("Migration preparation root is invalid.")


def _recover_preparing_root(
    target: Path,
    journal_root: Path,
    preparing_root: Path,
    migration_id: str,
) -> None:
    recovery_existed, state_existed = _preparing_root_metadata(
        preparing_root,
        migration_id,
    )
    if (
        preparing_root.is_symlink()
        or not preparing_root.is_dir()
        or journal_root.exists()
        or journal_root.is_symlink()
    ):
        raise ValueError("Migration preparation root is invalid.")
    journal_path = preparing_root / "journal.json"
    if journal_path.exists() or journal_path.is_symlink():
        journal = _load_journal(
            target,
            preparing_root,
            migration_id,
        )
        if journal["state"] not in {
            "preparing",
            "preparation-cleanup-pending",
        }:
            raise ValueError("Migration preparation root is invalid.")
        if (
            bool(journal.get("recovery_directory_existed", True))
            != recovery_existed
            or bool(journal.get("state_directory_existed", True))
            != state_existed
        ):
            raise ValueError("Migration preparation root is invalid.")
        _validate_recovery_manifest_fence(target, journal)
    else:
        allowed = {".journal.json.next"}
        for path in preparing_root.iterdir():
            if (
                path.name not in allowed
                or path.is_symlink()
                or not path.is_file()
            ):
                raise ValueError("Migration preparation root is invalid.")
        journal = {
            "schema_version": 1,
            "migration_id": migration_id,
            "target_contract_version": 1,
            "state": "preparation-cleanup-pending",
            "entries": [],
            "manifest": None,
            "directory_nonce": secrets.token_hex(32),
            "directories": [],
            "state_directory_existed": state_existed,
            "recovery_directory_existed": recovery_existed,
        }
        _write_journal_state(
            preparing_root,
            journal,
            "preparation-cleanup-pending",
        )
    _ensure_regular_parent(target, journal_root)
    source_parent = preparing_root.parent
    os.replace(preparing_root, journal_root)
    _fsync_directory(journal_root.parent)
    if source_parent != journal_root.parent:
        _fsync_directory(source_parent)
    if journal["state"] == "preparing":
        _cleanup_preparing(journal_root, journal)
    else:
        _cleanup_rolled_back(journal_root, journal)


def _journal_originals(journal_root: Path, *, create: bool = True) -> Path:
    root = _journal_path(journal_root, "originals")
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Migration recovery originals are invalid.")
    elif create:
        root.mkdir()
        _fsync_directory(root.parent)
    else:
        raise ValueError("Migration recovery originals are invalid.")
    return root


def _journal_path(root: Path, relative: str) -> Path:
    candidate = _safe_relative_path(root, relative)
    return candidate


def _cleanup_tombstone(
    journal_root: Path,
    *,
    recovery_existed: bool,
    rolled_back: bool = False,
    state_existed: bool = True,
) -> Path:
    migration_id = journal_root.name
    disposition = "keep" if recovery_existed else "remove"
    if not rolled_back:
        return journal_root.parent / (
            f"{_CLEANUP_PREFIX}{migration_id}-{disposition}-recovery"
        )
    state_disposition = "keep" if state_existed else "remove"
    target = journal_root.parent.parent.parent
    return target / (
        f"{_ROLLBACK_CLEANUP_PREFIX}{migration_id}-{disposition}-recovery-"
        f"{state_disposition}-state"
    )


def _cleanup_tombstone_candidates(journal_root: Path) -> tuple[Path, ...]:
    return (
        _cleanup_tombstone(journal_root, recovery_existed=True),
        _cleanup_tombstone(journal_root, recovery_existed=False),
        _cleanup_tombstone(
            journal_root,
            recovery_existed=True,
            rolled_back=True,
            state_existed=True,
        ),
        _cleanup_tombstone(
            journal_root,
            recovery_existed=True,
            rolled_back=True,
            state_existed=False,
        ),
        _cleanup_tombstone(
            journal_root,
            recovery_existed=False,
            rolled_back=True,
            state_existed=True,
        ),
        _cleanup_tombstone(
            journal_root,
            recovery_existed=False,
            rolled_back=True,
            state_existed=False,
        ),
    )


def _tombstone_metadata(
    tombstone: Path,
    migration_id: str,
) -> tuple[bool, bool, bool]:
    for remove_recovery, disposition in ((True, "remove"), (False, "keep")):
        if tombstone.name == (
            f"{_CLEANUP_PREFIX}{migration_id}-{disposition}-recovery"
        ):
            return remove_recovery, False, False
        for remove_state, state_disposition in ((True, "remove"), (False, "keep")):
            if tombstone.name == (
                f"{_ROLLBACK_CLEANUP_PREFIX}{migration_id}-{disposition}-"
                f"recovery-{state_disposition}-state"
            ):
                return remove_recovery, True, remove_state
    raise ValueError("Migration cleanup tombstone is invalid.")


def _cleanup_committed(
    target: Path,
    journal_root: Path,
    journal: dict[str, object],
) -> None:
    if journal.get("state") != "cleanup-pending":
        raise ValueError("Migration cleanup requires durable cleanup-pending state.")
    _remove_created_markers(
        target,
        journal_root,
        str(journal["migration_id"]),
        str(journal["directory_nonce"]),
        _directory_records(journal),
        allow_missing=True,
    )
    recovery_existed = bool(journal.get("recovery_directory_existed", True))
    tombstone = _cleanup_tombstone(
        journal_root,
        recovery_existed=recovery_existed,
    )
    if tombstone.exists() or tombstone.is_symlink():
        raise ValueError("Migration cleanup tombstone already exists.")
    os.replace(journal_root, tombstone)
    _fsync_directory(tombstone.parent)
    _cleanup_step("after-journal-rename")
    _delete_cleanup_tombstone(tombstone)
    if not recovery_existed:
        _rmdir_if_empty(tombstone.parent)


def _cleanup_rolled_back(
    journal_root: Path,
    journal: dict[str, object],
) -> None:
    if journal.get("state") not in {
        "preparation-cleanup-pending",
        "rollback-cleanup-pending",
    }:
        raise ValueError("Rollback cleanup requires durable cleanup-pending state.")
    recovery_existed = bool(journal.get("recovery_directory_existed", True))
    state_existed = bool(journal.get("state_directory_existed", True))
    tombstone = _cleanup_tombstone(
        journal_root,
        recovery_existed=recovery_existed,
        rolled_back=True,
        state_existed=state_existed,
    )
    if tombstone.exists() or tombstone.is_symlink():
        raise ValueError("Migration cleanup tombstone already exists.")
    source_parent = journal_root.parent
    os.replace(journal_root, tombstone)
    _fsync_directory(tombstone.parent)
    _fsync_directory(source_parent)
    _cleanup_step("after-rollback-journal-rename")
    _finish_rollback_ancestry(
        tombstone.parent,
        remove_recovery=not recovery_existed,
        remove_state=not state_existed,
    )
    _delete_cleanup_tombstone(tombstone)


def _cleanup_preparing(
    journal_root: Path,
    journal: dict[str, object],
) -> None:
    if journal.get("state") not in {"preparing", "prepared"}:
        raise ValueError("Preparation cleanup requires a preparation journal.")
    _write_journal_state(
        journal_root,
        journal,
        "preparation-cleanup-pending",
    )
    _cleanup_rolled_back(journal_root, journal)


def _resume_cleanup_tombstone(
    target: Path,
    tombstone: Path,
    migration_id: str,
) -> bool:
    remove_recovery, rolled_back, remove_state = _tombstone_metadata(
        tombstone,
        migration_id,
    )
    journal_path = tombstone / "journal.json"
    if not journal_path.exists():
        manifest = load_manifest(target)
        if (
            (not rolled_back and (
                manifest is None
                or manifest.last_migration_id != migration_id
            ))
            or tombstone.is_symlink()
            or not tombstone.is_dir()
            or any(tombstone.iterdir())
        ):
            raise ValueError("Migration cleanup tombstone is invalid.")
        if rolled_back:
            _finish_rollback_ancestry(
                target,
                remove_recovery=remove_recovery,
                remove_state=remove_state,
            )
        tombstone.rmdir()
        _fsync_directory(tombstone.parent)
        if not rolled_back and remove_recovery:
            _rmdir_if_empty(tombstone.parent)
        return rolled_back
    journal = _load_cleanup_tombstone(
        target,
        tombstone,
        migration_id,
        remove_recovery=remove_recovery,
        rolled_back=rolled_back,
        remove_state=remove_state,
    )
    _validate_recovery_manifest_fence(target, journal)
    if rolled_back:
        _finish_rollback_ancestry(
            target,
            remove_recovery=not bool(
                journal.get("recovery_directory_existed", True)
            ),
            remove_state=not bool(journal.get("state_directory_existed", True)),
        )
    _delete_cleanup_tombstone(tombstone)
    if not rolled_back and not bool(
        journal.get("recovery_directory_existed", True)
    ):
        _rmdir_if_empty(tombstone.parent)
    return rolled_back


def _finish_rollback_ancestry(
    target: Path,
    *,
    remove_recovery: bool,
    remove_state: bool,
) -> None:
    state_directory = target / ".vault-builder"
    recovery = state_directory / "recovery"
    if remove_recovery:
        _rmdir_if_empty(recovery)
    if remove_state:
        _rmdir_if_empty(state_directory)


def _load_cleanup_tombstone(
    target: Path,
    tombstone: Path,
    migration_id: str,
    *,
    remove_recovery: bool,
    rolled_back: bool,
    remove_state: bool,
) -> dict[str, object]:
    if tombstone.is_symlink() or not tombstone.is_dir():
        raise ValueError("Migration cleanup tombstone is invalid.")
    journal_path = _journal_path(tombstone, "journal.json")
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError("Migration cleanup tombstone is invalid.")
    try:
        data = json.loads(
            journal_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Migration cleanup tombstone is invalid.") from exc
    if (
        not isinstance(data, dict)
        or set(data) - _journal_optional_keys()
        or not _journal_required_keys() <= set(data)
        or type(data.get("schema_version")) is not int
        or type(data.get("target_contract_version")) is not int
        or data.get("schema_version") != 1
        or data.get("target_contract_version") != 1
        or data.get("migration_id") != migration_id
        or (
            rolled_back
            and data.get("state") not in {
                "preparation-cleanup-pending",
                "rollback-cleanup-pending",
            }
        )
        or (not rolled_back and data.get("state") != "cleanup-pending")
    ):
        raise ValueError("Migration cleanup tombstone is invalid.")
    entries = _journal_entries(data)
    _journal_manifest(data, allow_none=True)
    nonce = data.get("directory_nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise ValueError("Migration cleanup tombstone is invalid.")
    for key in ("state_directory_existed", "recovery_directory_existed"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError("Migration cleanup tombstone is invalid.")
    _validate_directory_records(
        target,
        tombstone,
        migration_id,
        nonce,
        entries,
        data.get("directories"),
        cleanup_pending=not rolled_back,
        rollback_complete=rolled_back,
    )
    recovery_existed = bool(data.get("recovery_directory_existed", True))
    state_existed = bool(data.get("state_directory_existed", True))
    if (
        remove_recovery == recovery_existed
        or (rolled_back and remove_state == state_existed)
        or (not rolled_back and remove_state)
    ):
        raise ValueError("Migration cleanup tombstone is invalid.")
    return data


def _delete_cleanup_tombstone(tombstone: Path) -> None:
    if tombstone.is_symlink() or not tombstone.is_dir():
        raise ValueError("Migration cleanup tombstone is invalid.")
    journal_path = tombstone / "journal.json"
    paths = sorted(
        (
            path
            for path in tombstone.rglob("*")
            if path != journal_path
        ),
        key=lambda candidate: (len(candidate.parts), candidate.as_posix()),
        reverse=True,
    )
    for path in paths:
        state = path.lstat()
        if stat.S_ISLNK(state.st_mode) or not (
            stat.S_ISREG(state.st_mode) or stat.S_ISDIR(state.st_mode)
        ):
            raise ValueError("Migration cleanup tombstone contains an unsafe path.")
        if stat.S_ISREG(state.st_mode):
            path.unlink()
            _fsync_directory(path.parent)
            _cleanup_step("during-tombstone-delete")
        else:
            path.rmdir()
            _fsync_directory(path.parent)
            _cleanup_step("during-tombstone-delete")
    if not journal_path.is_file() or journal_path.is_symlink():
        raise ValueError("Migration cleanup tombstone is invalid.")
    journal_path.unlink()
    _fsync_directory(tombstone)
    _cleanup_step("during-tombstone-delete")
    tombstone.rmdir()
    _fsync_directory(tombstone.parent)


def _cleanup_step(_stage: str) -> None:
    """Hard-stop injection seam for committed cleanup recovery."""


def _remove_tree_no_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Migration recovery journal is invalid.")
    for path in sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True):
        state = path.lstat()
        if stat.S_ISLNK(state.st_mode) or not (stat.S_ISREG(state.st_mode) or stat.S_ISDIR(state.st_mode)):
            raise ValueError("Migration recovery journal contains an unsafe path.")
        if stat.S_ISREG(state.st_mode):
            path.unlink()
            _fsync_directory(path.parent)
        else:
            path.rmdir()
            _fsync_directory(path.parent)
    root.rmdir()
    _fsync_directory(root.parent)


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return
        raise
    _fsync_directory(path.parent)


def _mark_rollback_failed(journal_root: Path, journal: dict[str, object]) -> None:
    try:
        _write_journal_state(journal_root, journal, "rollback-failed")
    except BaseException:
        pass


def _run_release_gate(target_root: Path) -> None:
    """Run the target's release gate with captured output.

    stdout and stderr are merged and kept on the raised CalledProcessError so a
    failure message can show the tail without leaking nested test output.
    """
    environment = dict(os.environ)
    environment.setdefault("PYTHON", sys.executable)
    subprocess.run(
        list(_RELEASE_GATE_COMMAND),
        cwd=target_root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )


def _output_tail_lines(error: subprocess.CalledProcessError) -> list[str]:
    output = error.output
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    if not isinstance(output, str) or not output.strip():
        return []
    tail = output.rstrip("\n").splitlines()[-_GATE_OUTPUT_TAIL_LINES:]
    return [f"Output (last {len(tail)} lines):", *(f"  {line}" for line in tail)]


def _recover_command(target: Path, migration_id: str) -> str:
    return shlex.join(
        [
            sys.executable,
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
            sys.executable,
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
    _strict_fsync_directory(path)


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
    *,
    cleanup_pending: bool = False,
    rollback_complete: bool = False,
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
        if (
            not isinstance(relative, str)
            or existed is not False
            or lifecycle
            not in {"intent", "created", "marker-removed", "removed"}
        ):
            raise ValueError("Migration recovery directory records are invalid.")
        if relative in paths or relative not in allowed:
            raise ValueError("Migration recovery directory records are invalid.")
        _validate_directory_relative(relative)
        paths.add(relative)
        directory = _safe_target_path(target, relative, reject_leaf_symlink=False)
        if rollback_complete:
            if directory.exists() or directory.is_symlink():
                raise ValueError(
                    f"Recovery verification failed: directory {relative}"
                )
        elif cleanup_pending:
            if lifecycle not in {"intent", "created"}:
                raise ValueError(
                    "Migration recovery directory records are invalid."
                )
            if lifecycle == "created" and not _regular_directory_without_marker(
                directory
            ):
                _validate_directory_marker_identity(
                    journal_root,
                    directory,
                    migration_id,
                    nonce,
                    relative,
                    require_expected_contents=False,
                )
        elif lifecycle in {"created", "marker-removed", "removed"}:
            _validate_staged_directory_marker(
                journal_root,
                migration_id,
                nonce,
                relative,
            )
            if lifecycle == "removed":
                if directory.exists() or directory.is_symlink():
                    raise ValueError(
                        f"Recovery verification failed: directory {relative}"
                    )
            elif directory.exists() or directory.is_symlink():
                if _marker_exists(directory):
                    _validate_directory_marker_identity(
                        journal_root,
                        directory,
                        migration_id,
                        nonce,
                        relative,
                        require_expected_contents=False,
                    )
                elif not (
                    _directory_children_restored(target, entries, relative)
                    and _regular_empty_directory(directory)
                ):
                    raise ValueError(
                        f"Migration directory contains unexpected content: {relative}"
                    )
            elif not _directory_children_restored(target, entries, relative):
                raise ValueError(
                    f"Recovery verification failed: directory {relative}"
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
    *,
    allow_missing: bool = False,
) -> None:
    markers: list[Path] = []
    for record in directories:
        if record["state"] != "created":
            continue
        directory = _safe_target_path(
            target,
            str(record["path"]),
            reject_leaf_symlink=False,
        )
        if allow_missing and _regular_directory_without_marker(directory):
            continue
        markers.append(
            _validate_directory_marker_identity(
                journal_root,
                directory,
                migration_id,
                nonce,
                str(record["path"]),
                require_expected_contents=False,
            )
        )
    for marker in markers:
        marker.unlink()
        _fsync_directory(marker.parent)
        if allow_missing:
            _cleanup_step("after-marker-removal")


def _recover_directory(
    target: Path,
    journal_root: Path,
    migration_id: str,
    nonce: str,
    entries: tuple[dict[str, object], ...],
    record: dict[str, object],
    record_state: Callable[[str, str], None],
) -> None:
    relative = str(record["path"])
    lifecycle = str(record["state"])
    directory = _safe_target_path(target, relative, reject_leaf_symlink=False)
    children_restored = _directory_children_restored(
        target,
        entries,
        relative,
    )
    if not directory.exists():
        if lifecycle in {"created", "marker-removed", "removed"}:
            _validate_staged_directory_marker(
                journal_root,
                migration_id,
                nonce,
                relative,
            )
            if not children_restored:
                raise ValueError(
                    f"Recovery verification failed: directory {relative}"
                )
            if lifecycle != "removed":
                record_state(relative, "removed")
        return
    if lifecycle == "removed":
        raise ValueError(f"Recovery verification failed: directory {relative}")
    if not children_restored:
        raise ValueError(
            f"Recovery verification failed: directory {relative}"
        )
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
    if lifecycle in {"created", "marker-removed"} and marker is None:
        _validate_staged_directory_marker(
            journal_root,
            migration_id,
            nonce,
            relative,
        )
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise ValueError(f"Migration directory is unreadable: {relative}") from exc
    if marker is None:
        if children:
            raise ValueError(f"Migration directory contains unexpected content: {relative}")
        directory.rmdir()
        _fsync_directory(directory.parent)
        _directory_step("after-rollback-directory-removal", relative)
        record_state(relative, "removed")
        return
    if children != [marker]:
        raise ValueError(f"Migration directory contains unexpected content: {relative}")
    marker.unlink()
    _fsync_directory(directory)
    _directory_step("after-rollback-marker-removal", relative)
    record_state(relative, "marker-removed")
    directory.rmdir()
    _fsync_directory(directory.parent)
    _directory_step("after-rollback-directory-removal", relative)
    record_state(relative, "removed")


def _directory_children_restored(
    target: Path,
    entries: tuple[dict[str, object], ...],
    directory: str,
) -> bool:
    prefix = PurePosixPath(directory)
    matched = False
    for entry in entries:
        relative = PurePosixPath(str(entry["path"]))
        try:
            relative.relative_to(prefix)
        except ValueError:
            continue
        matched = True
        path = _safe_target_path(
            target,
            relative.as_posix(),
            reject_leaf_symlink=False,
        )
        try:
            state = path.lstat()
        except FileNotFoundError:
            if entry["existed"]:
                return False
            continue
        if (
            not entry["existed"]
            or stat.S_ISLNK(state.st_mode)
            or not stat.S_ISREG(state.st_mode)
            or sha256_file(path) != entry["sha256"]
            or _observed_mode(path) != entry["mode"]
        ):
            return False
    return matched


def _regular_empty_directory(directory: Path) -> bool:
    try:
        state = directory.lstat()
        return (
            stat.S_ISDIR(state.st_mode)
            and not stat.S_ISLNK(state.st_mode)
            and not any(directory.iterdir())
        )
    except (FileNotFoundError, OSError):
        return False


def _marker_exists(directory: Path) -> bool:
    marker = directory / _DIRECTORY_MARKER_FILENAME
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("Migration directory marker is invalid.") from exc
    return True


def _regular_directory_without_marker(directory: Path) -> bool:
    try:
        state = directory.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("Migration directory marker is invalid.") from exc
    return stat.S_ISDIR(state.st_mode) and not _marker_exists(directory)


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
