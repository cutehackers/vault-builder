"""Create and safely persist generated-Vault contract manifests."""
from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .catalogs import _safe_relative_path, _validate_root, load_catalog, sha256_file
from .durability import fsync_directory as _fsync_directory
from .models import VaultManifest


_CATALOG_PATH = "tools/vault_migration/contracts/v1.json"
_MANIFEST_DIRECTORY = ".vault-builder"
_MANIFEST_FILENAME = "manifest.json"


def _manifest_paths(target_root: Path) -> tuple[Path, Path, Path]:
    root = _validate_root(target_root, "Vault target root")
    state_directory = _safe_relative_path(root, _MANIFEST_DIRECTORY)
    manifest_path = _safe_relative_path(root, f"{_MANIFEST_DIRECTORY}/{_MANIFEST_FILENAME}")
    if state_directory.exists() and (state_directory.is_symlink() or not state_directory.is_dir()):
        raise ValueError("Vault builder state directory must be a regular directory.")
    if manifest_path.exists() and (manifest_path.is_symlink() or not manifest_path.is_file()):
        raise ValueError("Vault builder manifest must be a regular file.")
    return root, state_directory, manifest_path


def _builder_revision(builder_root: Path, explicit_revision: str) -> str:
    if explicit_revision:
        return explicit_revision
    environment_revision = os.environ.get("VAULT_BUILDER_REVISION", "")
    if environment_revision:
        return environment_revision
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=builder_root,
        text=True,
        capture_output=True,
        check=False,
    )
    revision = result.stdout.strip() if result.returncode == 0 else ""
    return revision or "contract-v1"


def _utc_timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def create_manifest(
    builder_root: Path,
    target_root: Path,
    builder_revision: str,
    generated_at: datetime | None = None,
) -> VaultManifest:
    """Return the v1 manifest for catalog files copied unchanged into a target."""
    builder = _validate_root(builder_root, "Builder root")
    target, _, _ = _manifest_paths(target_root)
    catalog_path = _safe_relative_path(builder, _CATALOG_PATH)
    catalog = load_catalog(catalog_path)
    if catalog.contract_version != 1:
        raise ValueError("Version 1 manifest requires a version 1 catalog.")

    managed_files = {}
    for relative, managed in catalog.managed_files.items():
        candidate = _safe_relative_path(target, relative)
        if not candidate.exists() or candidate.is_symlink() or not candidate.is_file():
            continue
        if sha256_file(candidate) != managed.sha256:
            continue
        observed_mode = f"{candidate.stat().st_mode & 0o7777:04o}"
        if observed_mode != managed.mode:
            raise ValueError(
                f"Managed file mode does not match the catalog: {relative}"
            )
        managed_files[relative] = managed

    return VaultManifest(
        schema_version=catalog.schema_version,
        contract_version=catalog.contract_version,
        builder_revision=_builder_revision(builder, builder_revision),
        generated_at=_utc_timestamp(generated_at),
        managed_files=managed_files,
    )


def load_manifest(target_root: Path) -> VaultManifest | None:
    """Load a regular manifest, distinguishing absence from malformed state."""
    _, _, manifest_path = _manifest_paths(target_root)
    if not manifest_path.exists():
        return None
    try:
        return VaultManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Vault builder manifest is invalid.") from exc


def write_manifest(target_root: Path, manifest: VaultManifest) -> Path:
    """Atomically replace the target manifest without following symlinks."""
    root, state_directory, manifest_path = _manifest_paths(target_root)
    state_created = False
    if not state_directory.exists():
        state_directory.mkdir()
        state_created = True
        try:
            _fsync_directory(root)
        except OSError as exc:
            try:
                state_directory.rmdir()
            except OSError:
                pass
            raise ValueError("Could not write Vault builder manifest.") from exc
    _, state_directory, manifest_path = _manifest_paths(target_root)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=state_directory,
            prefix=f".{_MANIFEST_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            payload = manifest.to_json().encode("utf-8")
            if handle.write(payload) != len(payload):
                raise OSError("Vault builder manifest write was incomplete.")
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary_path, manifest_path)
        temporary_path = None
        _fsync_directory(state_directory)
    except OSError as exc:
        raise ValueError("Could not write Vault builder manifest.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
                _fsync_directory(state_directory)
            except OSError:
                pass
        if state_created and not manifest_path.exists():
            try:
                state_directory.rmdir()
                _fsync_directory(root)
            except OSError:
                pass
    return manifest_path
