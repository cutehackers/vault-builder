#!/usr/bin/env python3
"""Builder-only Vault migration utility."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - supported migrations are POSIX-only
    fcntl = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.vault_migration.applying import apply_plan, recover_migration
from tools.vault_migration.catalogs import build_catalog, write_catalog
from tools.vault_migration.manifests import create_manifest, load_manifest, write_manifest
from tools.vault_migration.models import MigrationPlan
from tools.vault_migration.planning import audit_vault, validate_external_output_path


_ACTION_ORDER = (
    "unchanged",
    "create",
    "replace",
    "preserve",
    "review-required",
    "rebuild-suggested",
    "invalid",
)


def render_plan_summary(
    target_root: Path,
    plan: MigrationPlan,
    *,
    target_writes: str = "none",
) -> str:
    """Render stable, useful audit information without exposing internals."""
    counts = Counter(action.action for action in plan.actions)
    current = "legacy" if plan.current_contract_version is None else str(plan.current_contract_version)
    lines = [
        f"Target: {Path(target_root).absolute()}",
        f"Current contract: {current}",
        f"Target contract: {plan.target_contract_version}",
        f"Migration ID: {plan.migration_id}",
        "Actions:",
        *(f"  {action}: {counts[action]}" for action in _ACTION_ORDER),
        f"Target writes: {target_writes}",
    ]
    return "\n".join(lines) + "\n"


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


@dataclass
class _CreatedDirectory:
    parent: int
    name: str


@dataclass
class _PreparedOutput:
    path: Path
    label: str
    parent: int
    temporary_name: str
    prior: "_OutputLeafState"
    candidate: "_OutputLeafState"


@dataclass(frozen=True)
class _OutputLeafState:
    contents: bytes | None
    mode: int | None
    device: int | None
    inode: int | None


_ABSENT_OUTPUT = _OutputLeafState(None, None, None, None)


def _canonical_safe_path(path_value: str, label: str) -> Path:
    """Reject raw traversal and user links before deriving a canonical path."""
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{label} must be a non-empty path.")
    pieces = path_value.split(os.sep)
    if path_value.startswith(os.sep):
        pieces = pieces[1:]
    if not pieces or any(piece in {"", ".", ".."} for piece in pieces):
        raise ValueError(f"{label} must not contain empty, dot, or traversal components.")
    raw = Path(path_value).absolute()
    _reject_user_symlink_components(raw, label)
    try:
        return raw.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {raw}") from exc


def _reject_user_symlink_components(path: Path, label: str) -> None:
    """Keep macOS system aliases compatible while rejecting user path links."""
    system_aliases = frozenset({Path("/etc"), Path("/tmp"), Path("/var")})
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError(f"{label} is unreadable: {current}") from exc
        if stat.S_ISLNK(state.st_mode) and current not in system_aliases:
            raise ValueError(f"{label} contains a symlink: {current}")


def _output_leaf_is_safe(path: Path, label: str) -> None:
    try:
        state = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError(f"{label} must be an absent or regular non-symlink file: {path}")


def _validate_report_output(target_root: Path, raw_path: str) -> tuple[Path, str]:
    report = _canonical_safe_path(raw_path, "Audit report")
    report_root = target_root / "scratch" / "reports"
    try:
        relative = report.relative_to(report_root)
    except ValueError as exc:
        raise ValueError("Audit report must stay beneath target scratch/reports/.") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Audit report must name a file beneath target scratch/reports/.")
    _output_leaf_is_safe(report, "Audit report")
    return report, relative.as_posix()


def _open_directory(path: Path, *, create: bool, created: list[_CreatedDirectory] | None = None) -> int:
    """Open an absolute directory by no-follow component walk."""
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("Output parent must be an absolute non-root directory.")
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    except OSError:
        raise ValueError(f"Could not open output root: {path.anchor}") from None
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(f"Could not create output parent: {part}") from exc
                else:
                    if created is not None:
                        created.append(_CreatedDirectory(os.dup(descriptor), part))
            try:
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(f"Output path contains an unsafe parent: {part}") from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_file(
    path: Path,
    label: str,
    *,
    before_leaf_open: Callable[[], None] | None = None,
) -> str:
    parent = _open_directory(path.parent, create=False)
    try:
        if before_leaf_open is not None:
            before_leaf_open()
        try:
            descriptor = os.open(path.name, _READ_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise ValueError(f"{label} must be a regular non-symlink file: {path}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"{label} must be a regular non-symlink file: {path}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _output_snapshot_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_output_prior(parent: int, path: Path, label: str) -> _OutputLeafState:
    try:
        try:
            state = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return _ABSENT_OUTPUT
        if not stat.S_ISREG(state.st_mode):
            raise ValueError(f"{label} must be an absent or regular non-symlink file: {path}")
        descriptor = os.open(path.name, _READ_FLAGS, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"{label} must be an absent or regular non-symlink file: {path}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            contents = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        leaf = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            _output_snapshot_identity(before)
            != _output_snapshot_identity(after)
            or not stat.S_ISREG(leaf.st_mode)
            or (leaf.st_dev, leaf.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ValueError(f"{label} changed while it was being prepared: {path}")
        return _OutputLeafState(
            contents,
            stat.S_IMODE(after.st_mode),
            after.st_dev,
            after.st_ino,
        )
    except OSError as exc:
        raise ValueError(f"Could not prepare {label.lower()}: {path}") from exc


def _reserve_temporary(parent: int, path: Path, label: str) -> tuple[str, int]:
    for _ in range(8):
        candidate = f".{path.name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError(f"Could not reserve temporary {label.lower()}: {path}") from exc
        return candidate, descriptor
    raise ValueError(f"Could not reserve temporary {label.lower()}: {path}")


def _write_temporary(descriptor: int, contents: bytes, mode: int) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Temporary output write was incomplete.")
        view = view[written:]
    os.fchmod(descriptor, mode)
    os.fsync(descriptor)


def _prepare_output(
    path: Path,
    contents: str,
    label: str,
    parent: int,
) -> _PreparedOutput:
    temporary_name: str | None = None
    descriptor: int | None = None
    try:
        prior = _read_output_prior(parent, path, label)
        temporary_name, descriptor = _reserve_temporary(parent, path, label)
        payload = contents.encode("utf-8")
        _write_temporary(descriptor, payload, 0o644)
        candidate_state = os.fstat(descriptor)
        candidate = _OutputLeafState(
            payload,
            stat.S_IMODE(candidate_state.st_mode),
            candidate_state.st_dev,
            candidate_state.st_ino,
        )
        os.close(descriptor)
        descriptor = None
        return _PreparedOutput(
            path,
            label,
            parent,
            temporary_name,
            prior,
            candidate,
        )
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        raise


def _discard_prepared(outputs: list[_PreparedOutput]) -> None:
    for output in outputs:
        if output.temporary_name:
            try:
                os.unlink(output.temporary_name, dir_fd=output.parent)
            except FileNotFoundError:
                pass


def _finish_created_directories(created: list[_CreatedDirectory], *, remove: bool) -> None:
    for directory in reversed(created):
        try:
            if remove:
                try:
                    os.rmdir(directory.name, dir_fd=directory.parent)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        finally:
            os.close(directory.parent)


def _fsync_parent(parent: int) -> None:
    os.fsync(parent)


def _validate_output_leaf(
    output: _PreparedOutput,
    expected: _OutputLeafState,
    operation: str,
) -> None:
    current = _read_output_prior(output.parent, output.path, output.label)
    if current != expected:
        raise ValueError(
            f"{output.label} changed before {operation}: {output.path}"
        )


def _restore_output(output: _PreparedOutput) -> None:
    if output.prior.contents is None:
        _validate_output_leaf(output, output.candidate, "rollback")
        try:
            os.unlink(output.path.name, dir_fd=output.parent)
        except FileNotFoundError:
            return
        _fsync_parent(output.parent)
        return
    temporary_name: str | None = None
    descriptor: int | None = None
    try:
        temporary_name, descriptor = _reserve_temporary(output.parent, output.path, output.label)
        try:
            _write_temporary(
                descriptor,
                output.prior.contents,
                output.prior.mode or 0o644,
            )
        finally:
            os.close(descriptor)
            descriptor = None
        _validate_output_leaf(output, output.candidate, "rollback")
        os.replace(temporary_name, output.path.name, src_dir_fd=output.parent, dst_dir_fd=output.parent)
        temporary_name = None
        _fsync_parent(output.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=output.parent)
            except FileNotFoundError:
                pass


def _rollback_outputs(committed: list[_PreparedOutput], original: BaseException) -> None:
    failures: list[BaseException] = []
    for output in reversed(committed):
        try:
            _restore_output(output)
        except BaseException as rollback_exc:
            failures.append(rollback_exc)
    if failures:
        details = "; ".join(str(failure) for failure in failures)
        raise ValueError(f"Output commit failed: {original}; rollback failed: {details}") from failures[0]


def _open_and_lock_output_parents(
    outputs: tuple[tuple[Path, str, str], ...],
    created: list[_CreatedDirectory],
) -> tuple[list[int], list[tuple[tuple[int, int], int]]]:
    if fcntl is None:
        raise ValueError("Audit output locking requires POSIX advisory locks.")
    parents_by_identity: dict[tuple[int, int], int] = {}
    output_parents: list[int] = []
    locked: list[tuple[tuple[int, int], int]] = []
    try:
        for path, _contents, _label in outputs:
            descriptor = _open_directory(
                path.parent,
                create=True,
                created=created,
            )
            state = os.fstat(descriptor)
            identity = (state.st_dev, state.st_ino)
            existing = parents_by_identity.get(identity)
            if existing is not None:
                os.close(descriptor)
                descriptor = existing
            else:
                parents_by_identity[identity] = descriptor
            output_parents.append(descriptor)
        for identity, descriptor in sorted(parents_by_identity.items()):
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked.append((identity, descriptor))
        return output_parents, locked
    except BaseException:
        for _identity, descriptor in reversed(locked):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        for descriptor in parents_by_identity.values():
            os.close(descriptor)
        raise


def _release_output_parents(
    locked: list[tuple[tuple[int, int], int]],
) -> None:
    if fcntl is None:  # pragma: no cover - paired acquisition already rejects
        return
    for _identity, descriptor in reversed(locked):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_outputs(
    outputs: tuple[tuple[Path, str, str], ...],
    *,
    before_replace: Callable[[int], None] | None = None,
) -> None:
    """Prepare every output, then atomically commit or restore them as a unit."""
    prepared: list[_PreparedOutput] = []
    created: list[_CreatedDirectory] = []
    committed: list[_PreparedOutput] = []
    locked: list[tuple[tuple[int, int], int]] = []
    successful = False
    try:
        output_parents, locked = _open_and_lock_output_parents(
            outputs,
            created,
        )
        for (path, contents, label), parent in zip(
            outputs,
            output_parents,
            strict=True,
        ):
            prepared.append(_prepare_output(path, contents, label, parent))
        for index, output in enumerate(prepared):
            if before_replace is not None:
                before_replace(index)
            _validate_output_leaf(output, output.prior, "publication")
            try:
                os.replace(output.temporary_name, output.path.name, src_dir_fd=output.parent, dst_dir_fd=output.parent)
            except OSError as exc:
                raise ValueError(f"Could not write {output.label.lower()}: {output.path}") from exc
            output.temporary_name = ""
            committed.append(output)
            _fsync_parent(output.parent)
        successful = True
    except BaseException as exc:
        _rollback_outputs(committed, exc)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Could not write requested outputs.") from exc
    finally:
        try:
            try:
                _discard_prepared(prepared)
            finally:
                _finish_created_directories(
                    created,
                    remove=not successful,
                )
        finally:
            _release_output_parents(locked)


def _write_atomic(
    path: Path,
    contents: str,
    label: str,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    hook = None if before_replace is None else lambda _index: before_replace()
    _write_outputs(((path, contents, label),), before_replace=hook)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Migration plan JSON contains duplicate key: {key}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Migration plan JSON contains unsupported numeric constant: {value}.")


def _load_plan(
    path_value: str,
    *,
    before_leaf_open: Callable[[], None] | None = None,
) -> MigrationPlan:
    path = _canonical_safe_path(path_value, "Migration plan")
    try:
        parsed = json.loads(
            _read_regular_file(path, "Migration plan", before_leaf_open=before_leaf_open),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
        return MigrationPlan.from_dict(parsed)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Migration plan is invalid: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault-migration")
    subcommands = parser.add_subparsers(dest="command", required=True)
    catalog = subcommands.add_parser("catalog")
    catalog.add_argument("--contract-version", required=True, type=int)
    catalog.add_argument("--revision", required=True)
    catalog.add_argument("--source-ref")
    catalog.add_argument("--output", required=True)
    manifest = subcommands.add_parser("manifest")
    manifest.add_argument("--builder-root", required=True)
    manifest.add_argument("--target", required=True)
    manifest.add_argument("--builder-revision", required=True)
    audit = subcommands.add_parser("audit")
    audit.add_argument("--target", required=True)
    audit.add_argument("--plan-out")
    audit.add_argument("--report")
    apply = subcommands.add_parser("apply")
    apply.add_argument("--target", required=True)
    apply.add_argument("--plan", required=True)
    recover = subcommands.add_parser("recover")
    recover.add_argument("--target", required=True)
    recover.add_argument("--migration-id", required=True)
    return parser


def _run_audit(args: argparse.Namespace) -> int:
    plan = audit_vault(ROOT, Path(args.target))
    target = Path(args.target).resolve()
    plan_output: Path | None = None
    report_output: Path | None = None
    report_relative: str | None = None
    if args.plan_out:
        plan_output = _canonical_safe_path(args.plan_out, "Migration plan output")
        validate_external_output_path(target, plan_output)
        _output_leaf_is_safe(plan_output, "Migration plan output")
    if args.report:
        report_output, report_relative = _validate_report_output(target, args.report)

    target_writes = "none" if report_relative is None else f"scratch/reports/{report_relative}"
    rendered = render_plan_summary(target, plan, target_writes=target_writes) + "\n" + plan.to_json()
    outputs: list[tuple[Path, str, str]] = []
    if plan_output is not None:
        outputs.append((plan_output, plan.to_json(), "Migration plan output"))
    if report_output is not None:
        outputs.append((report_output, rendered, "Audit report"))
    if outputs:
        _write_outputs(tuple(outputs))
    print(rendered, end="")
    return 2 if plan.has_blocking_actions else 0


def _run_apply(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    manifest = apply_plan(ROOT, Path(args.target), plan)
    changed = [action.path for action in plan.actions if action.action in {"create", "replace"}]
    print("Migration applied.")
    print("Changed paths:")
    if changed:
        for path in changed:
            print(f"  {path}")
    else:
        print("  none")
    print(f"Final contract: {manifest.contract_version}")
    return 0


def _run_recover(args: argparse.Namespace) -> int:
    outcome = recover_migration(Path(args.target), args.migration_id)
    manifest = load_manifest(Path(args.target))
    contract = "legacy" if manifest is None else str(manifest.contract_version)
    if outcome == "cleanup-completed":
        print("Migration committed; cleanup completed.")
    else:
        print("Migration rollback completed.")
    print(f"Final contract: {contract}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    if args.command == "catalog":
        try:
            result = build_catalog(ROOT, args.contract_version, args.revision, args.source_ref)
            output = Path(args.output)
            display_output = output if output.is_absolute() else ROOT / output
            write_catalog(output, result, output_root=ROOT)
        except (OSError, ValueError) as exc:
            print(f"catalog: {exc}", file=sys.stderr)
            return 1
        print(display_output)
        return 0
    if args.command == "manifest":
        try:
            result = create_manifest(Path(args.builder_root), Path(args.target), args.builder_revision)
            output = write_manifest(Path(args.target), result)
        except (OSError, ValueError) as exc:
            print(f"manifest: {exc}", file=sys.stderr)
            return 1
        print(f"{output} (contract v{result.contract_version})")
        return 0
    if args.command == "audit":
        try:
            return _run_audit(args)
        except (OSError, ValueError) as exc:
            print(f"audit: {exc}", file=sys.stderr)
            return 1
    if args.command == "apply":
        try:
            return _run_apply(args)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"apply: {exc}", file=sys.stderr)
            return 1
    if args.command == "recover":
        try:
            return _run_recover(args)
        except (OSError, ValueError, RuntimeError) as exc:
            message = str(exc)
            if message == "Migration recovery journal not found.":
                message = "Recovery journal not found."
            print(f"recover: {message}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
