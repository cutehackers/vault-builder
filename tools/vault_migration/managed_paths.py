"""The exact builder-owned files eligible for migration."""
from __future__ import annotations

from pathlib import Path


MANAGED_EXACT_PATHS = (
    ".agents/skills/wiki-ingest/SKILL.md",
    ".agents/skills/wiki-query/SKILL.md",
    ".agents/skills/wiki-update/SKILL.md",
    ".github/workflows/release-gate.yml",
    ".gitignore",
    "AGENTS.md",
    "README-kr.md",
    "README.md",
    "docs/LLM-WIKI.md",
    "docs/USER_PROMPTS.md",
    "docs/VAULT_PROMPTS.md",
    "docs/agent/DRAFTS.md",
    "docs/agent/OPERATING-SCHEMA.md",
    "docs/usage.md",
    "scripts/release_gate.sh",
    "tests/__init__.py",
    "tests/test_wiki_quality_baseline.py",
    "tests/test_wiki_tools.py",
)

MANAGED_TREE_ROOTS = ("tools/wiki",)

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
