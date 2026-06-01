from __future__ import annotations

from pathlib import Path


def resolve_report_path(root: Path, path: Path | str) -> Path:
    return _resolve_under(root, path, "scratch/reports", "Report")


def resolve_draft_artifact_path(root: Path, path: Path | str) -> Path:
    return _resolve_under(root, path, "scratch/drafts", "Draft")


def _resolve_under(root: Path, path: Path | str, relative_root: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    allowed_root = root.resolve() / relative_root
    try:
        candidate.resolve(strict=False).relative_to(allowed_root)
    except ValueError:
        raise ValueError(f"{label} path must stay under {relative_root}/.") from None
    if candidate.exists() and candidate.is_dir():
        raise ValueError(f"{label} path must be a file, not a directory.")
    if candidate.parent.exists() and not candidate.parent.is_dir():
        raise ValueError(f"{label} parent path must be a directory.")
    return candidate
