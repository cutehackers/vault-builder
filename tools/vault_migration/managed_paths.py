"""The exact builder-owned files eligible for migration."""
from __future__ import annotations

from pathlib import Path

from tools.wiki.lib.vault_manifest import (
    MANAGED_EXACT_PATH_SCOPE,
    MANAGED_TREE_ROOT_SCOPE,
)

MANAGED_EXACT_PATHS = tuple(sorted(MANAGED_EXACT_PATH_SCOPE))
MANAGED_TREE_ROOTS = tuple(
    root.removesuffix("/")
    for root in MANAGED_TREE_ROOT_SCOPE
)

_ROOT = Path(__file__).resolve().parents[2]
_EXCLUDED_PARTS = frozenset({"__pycache__"})


def _is_managed_tree_file(path: Path) -> bool:
    name = path.name
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in path.parts)
        and name != ".DS_Store"
        and not name.endswith((".pyc", ".tmp", "~"))
        and not name.startswith(".tmp")
    )


def _tree_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for tree_root in MANAGED_TREE_ROOTS:
        directory = root / tree_root
        if not directory.is_dir() or directory.is_symlink():
            continue
        paths.extend(
            path.relative_to(root).as_posix()
            for path in directory.rglob("*")
            if _is_managed_tree_file(path)
        )
    return tuple(sorted(paths))


MANAGED_PATHS = tuple(sorted((*MANAGED_EXACT_PATHS, *_tree_paths(_ROOT))))
