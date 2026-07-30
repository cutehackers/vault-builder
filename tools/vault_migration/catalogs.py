"""Load and deterministically generate migration contract catalogs."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .managed_paths import MANAGED_EXACT_PATHS, MANAGED_TREE_ROOTS, _is_managed_tree_file
from .models import ContractCatalog, ManagedFile


CATALOG_SCHEMA_VERSION = 1


def _validate_root(root: Path, label: str) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{label} must be a regular directory.")
    return root.resolve()


def _safe_relative_path(root: Path, relative: str) -> Path:
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or any(part in {"", ".", ".."} for part in candidate_relative.parts):
        raise ValueError(f"Managed path must be a safe relative path: {relative}")
    candidate = root
    for part in candidate_relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"Managed path contains a symlink: {relative}")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Managed path escapes the builder root: {relative}") from exc
    return candidate


def _safe_output_path(output_root: Path, output: Path) -> Path:
    root = _validate_root(output_root, "Catalog output root")
    raw_root = output_root.absolute()
    candidate = output.absolute() if output.is_absolute() else raw_root / output
    try:
        relative = candidate.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("Catalog output must stay inside the output root.") from exc
    return _safe_relative_path(root, relative.as_posix())


def _reject_symlinked_ancestors(path: Path) -> None:
    current = path.absolute()
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(f"Catalog output contains a symlink: {path}")
        current = current.parent


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_catalog(path: Path) -> ContractCatalog:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Catalog must be a regular file: {path}")
    try:
        return ContractCatalog.from_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read catalog: {path}") from exc


def _catalog_paths(builder_root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for relative in MANAGED_EXACT_PATHS:
        _safe_relative_path(builder_root, relative)
        paths.append(relative)
    for tree_root in MANAGED_TREE_ROOTS:
        directory = _safe_relative_path(builder_root, tree_root)
        if not directory.is_dir():
            raise ValueError(f"Managed tree root is missing: {tree_root}")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(builder_root).as_posix()
            _safe_relative_path(builder_root, relative)
            if _is_managed_tree_file(path):
                paths.append(relative)
    return tuple(sorted(set(paths)))


def _mode_for_path(path: Path) -> str:
    return "0755" if path.stat().st_mode & 0o111 else "0644"


def _git_file(builder_root: Path, source_ref: str, relative: str) -> tuple[bytes, str] | None:
    shown = subprocess.run(
        ["git", "show", f"{source_ref}:{relative}"],
        cwd=builder_root,
        capture_output=True,
        check=False,
    )
    if shown.returncode:
        return None
    tree = subprocess.run(
        ["git", "ls-tree", source_ref, "--", relative],
        cwd=builder_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if tree.returncode or not tree.stdout.strip():
        raise ValueError(f"Could not read mode for {relative} at {source_ref}.")
    mode = tree.stdout.split(maxsplit=1)[0]
    if mode != "100644" and mode != "100755":
        raise ValueError(f"Managed Git entry must be a regular file: {relative}")
    return shown.stdout, "0755" if mode == "100755" else "0644"


def build_catalog(
    builder_root: Path,
    contract_version: int,
    revision: str,
    source_ref: str | None = None,
) -> ContractCatalog:
    root = _validate_root(builder_root, "Builder root")
    files: dict[str, ManagedFile] = {}
    for relative in _catalog_paths(root):
        path = root / relative
        if source_ref is None:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Managed path must be a regular file: {relative}")
            files[relative] = ManagedFile(
                path=relative,
                sha256=sha256_file(path),
                mode=_mode_for_path(path),
            )
            continue
        legacy = _git_file(root, source_ref, relative)
        if legacy is None:
            continue
        contents, mode = legacy
        files[relative] = ManagedFile(
            path=relative,
            sha256=hashlib.sha256(contents).hexdigest(),
            mode=mode,
        )
    return ContractCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        contract_version=contract_version,
        builder_revision=revision,
        managed_files=files,
    )


def write_catalog(path: Path, catalog: ContractCatalog, *, output_root: Path | None = None) -> None:
    if output_root is not None:
        output = _safe_output_path(output_root, path)
    else:
        output = path.absolute()
        _reject_symlinked_ancestors(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlinked_ancestors(output)
    if output.is_symlink():
        raise ValueError(f"Catalog output must not be a symlink: {output}")
    output.write_text(catalog.to_json(), encoding="utf-8")
