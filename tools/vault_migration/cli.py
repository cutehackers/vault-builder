#!/usr/bin/env python3
"""Builder-only Vault migration utility."""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

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


def _absolute(path: str) -> Path:
    return Path(path).absolute()


def _validate_regular_output(path: Path, label: str) -> None:
    """Require a safe existing parent and an absent or regular output leaf."""
    parent = path.parent
    try:
        parent_state = parent.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} parent must already exist: {parent}") from exc
    except OSError as exc:
        raise ValueError(f"{label} parent is unreadable: {parent}") from exc
    if stat.S_ISLNK(parent_state.st_mode) or not stat.S_ISDIR(parent_state.st_mode):
        raise ValueError(f"{label} parent must be a regular directory: {parent}")
    try:
        leaf_state = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if stat.S_ISLNK(leaf_state.st_mode) or not stat.S_ISREG(leaf_state.st_mode):
        raise ValueError(f"{label} must be an absent or regular non-symlink file: {path}")


def _validate_report_output(target_root: Path, raw_path: str) -> tuple[Path, str]:
    raw_report = _absolute(raw_path)
    _reject_symlinked_output_components(raw_report)
    try:
        raw_state = raw_report.lstat()
    except FileNotFoundError:
        raw_state = None
    except OSError as exc:
        raise ValueError(f"Audit report is unreadable: {raw_report}") from exc
    if raw_state is not None and stat.S_ISLNK(raw_state.st_mode):
        raise ValueError(f"Audit report must not be a symlink: {raw_report}")
    report = raw_report.resolve(strict=False)
    report_root = target_root / "scratch" / "reports"
    try:
        relative = report.relative_to(report_root)
    except ValueError as exc:
        raise ValueError("Audit report must stay beneath target scratch/reports/.") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Audit report must name a file beneath target scratch/reports/.")

    current = target_root
    for part in ("scratch", "reports", *relative.parts[:-1]):
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"Audit report parent must already exist: {current}") from exc
        except OSError as exc:
            raise ValueError(f"Audit report parent is unreadable: {current}") from exc
        if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
            raise ValueError(f"Audit report path contains an unsafe parent: {current}")
    _validate_regular_output(report, "Audit report")
    return report, relative.as_posix()


def _reject_symlinked_output_components(path: Path) -> None:
    """Reject report-path links while allowing macOS's system /var alias."""
    system_components = frozenset({Path("/etc"), Path("/tmp"), Path("/var")})
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            state = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError(f"Audit report path is unreadable: {current}") from exc
        if stat.S_ISLNK(state.st_mode) and current not in system_components:
            raise ValueError(f"Audit report path contains a symlink: {current}")


def _write_atomic(path: Path, contents: str, label: str) -> None:
    """Atomically write a validated text output with a predictable mode."""
    _validate_regular_output(path, label)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        _validate_regular_output(path, label)
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ValueError(f"Could not write {label.lower()}: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Migration plan JSON contains duplicate key: {key}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Migration plan JSON contains unsupported numeric constant: {value}.")


def _load_plan(path_value: str) -> MigrationPlan:
    path = _absolute(path_value)
    try:
        state = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"Migration plan must be a regular non-symlink file: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Migration plan is unreadable: {path}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError(f"Migration plan must be a regular non-symlink file: {path}")
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
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
        plan_output = _absolute(args.plan_out)
        validate_external_output_path(target, plan_output)
        _validate_regular_output(plan_output, "Migration plan output")
    if args.report:
        report_output, report_relative = _validate_report_output(target, args.report)

    target_writes = "none" if report_relative is None else f"scratch/reports/{report_relative}"
    rendered = render_plan_summary(target, plan, target_writes=target_writes) + "\n" + plan.to_json()
    if plan_output is not None:
        _write_atomic(plan_output, plan.to_json(), "Migration plan output")
    if report_output is not None:
        _write_atomic(report_output, rendered, "Audit report")
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
    recover_migration(Path(args.target), args.migration_id)
    manifest = load_manifest(Path(args.target))
    contract = "legacy" if manifest is None else str(manifest.contract_version)
    print("Migration recovery completed.")
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
