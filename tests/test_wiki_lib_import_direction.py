"""Checks the import directions between the wiki modules added for vault-graph memory.

Each rule names one file and the modules it must not import directly, or the only project
modules it may import. A rule whose file does not exist yet is skipped and its name is printed,
so later tasks turn the rule on by creating the file.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

WIKI_CLI = "tools.wiki.cli"
LIB = "tools.wiki.lib"
MIGRATION = "tools.vault_migration"


def lib(name: str) -> str:
    return f"{LIB}.{name}"


def migration(name: str) -> str:
    return f"{MIGRATION}.{name}"


@dataclass(frozen=True)
class Rule:
    name: str
    module: str
    forbidden: frozenset[str] = field(default_factory=frozenset)
    # When set, the module may import only these modules from the project packages below.
    only: frozenset[str] | None = None


PROJECT_PACKAGES = ("tools.wiki", MIGRATION)

RULES: tuple[Rule, ...] = (
    Rule(
        "publish does not import review or memory_proposal",
        lib("publish"),
        forbidden=frozenset({lib("review"), lib("memory_proposal"), WIKI_CLI}),
    ),
    Rule(
        "review does not import memory_proposal or publish",
        lib("review"),
        forbidden=frozenset({lib("memory_proposal"), lib("publish"), WIKI_CLI}),
    ),
    Rule(
        "memory_proposal does not import cli",
        lib("memory_proposal"),
        forbidden=frozenset({WIKI_CLI}),
    ),
    Rule(
        "draft does not import publish or memory_proposal",
        lib("draft"),
        forbidden=frozenset({lib("publish"), lib("memory_proposal"), WIKI_CLI}),
    ),
    Rule(
        "lint does not import publish or memory_proposal",
        lib("lint"),
        forbidden=frozenset({lib("publish"), lib("memory_proposal"), WIKI_CLI}),
    ),
    Rule(
        "maps does not import publish or memory_proposal",
        lib("maps"),
        forbidden=frozenset({lib("publish"), lib("memory_proposal"), WIKI_CLI}),
    ),
    Rule(
        "memory_fields imports only schema and frontmatter",
        lib("memory_fields"),
        only=frozenset({lib("schema"), lib("frontmatter")}),
    ),
    Rule(
        "schema does not import memory_fields",
        lib("schema"),
        forbidden=frozenset({lib("memory_fields")}),
    ),
    Rule(
        "frontmatter does not import memory_fields",
        lib("frontmatter"),
        forbidden=frozenset({lib("memory_fields")}),
    ),
    Rule(
        "hooks does not import map_settings or migration cli",
        migration("hooks"),
        forbidden=frozenset({migration("map_settings"), migration("cli")}),
    ),
    Rule(
        "map_settings does not import hooks or migration cli",
        migration("map_settings"),
        forbidden=frozenset({migration("hooks"), migration("cli")}),
    ),
)

# New modules from spec 4.4. None of them may sit on an import cycle.
NEW_MODULES = (
    lib("publish"),
    lib("memory_proposal"),
    lib("memory_fields"),
    migration("hooks"),
    migration("map_settings"),
)


def module_path(root: Path, module: str) -> Path:
    package_init = root.joinpath(*module.split("."), "__init__.py")
    if package_init.is_file():
        return package_init
    return root.joinpath(*module.split(".")).with_suffix(".py")


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def direct_imports(root: Path, module: str) -> set[str]:
    """Return every module that `module` imports, including imports inside functions."""
    path = module_path(root, module)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_of(module, path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base_parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            # `from pkg import name` may import the submodule pkg.name.
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if module_path(root, candidate).is_file():
                    found.add(candidate)
    return found


def _project_imports(imports: set[str]) -> set[str]:
    return {
        name
        for name in imports
        if any(name == package or name.startswith(package + ".") for package in PROJECT_PACKAGES)
    }


def _is_package(root: Path, module: str) -> bool:
    return root.joinpath(*module.split("."), "__init__.py").is_file()


def check_rules(root: Path, rules: tuple[Rule, ...] = RULES) -> tuple[list[str], list[str]]:
    """Return (violations, skipped rule names) for `rules` under `root`."""
    violations: list[str] = []
    skipped: list[str] = []
    for rule in rules:
        if not module_path(root, rule.module).is_file():
            skipped.append(rule.name)
            continue
        imports = direct_imports(root, rule.module)
        for target in sorted(imports & rule.forbidden):
            violations.append(f"{rule.name}: {rule.module} imports {target}")
        if rule.only is not None:
            for target in sorted(_project_imports(imports)):
                if target in rule.only or _is_package(root, target):
                    continue
                violations.append(f"{rule.name}: {rule.module} imports {target}")
    return violations, skipped


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def import_graph(root: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for package in PROJECT_PACKAGES:
        package_dir = root.joinpath(*package.split("."))
        if not package_dir.is_dir():
            continue
        for path in sorted(package_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            graph[_module_name(root, path)] = set()
    for module in graph:
        graph[module] = {name for name in direct_imports(root, module) if name in graph and name != module}
    return graph


def cycles_through(graph: dict[str, set[str]], start: str) -> list[list[str]]:
    """Return one import path from `start` back to itself, if any."""
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    seen: set[str] = set()
    while stack:
        current, path = stack.pop()
        for target in sorted(graph.get(current, ())):
            if target == start:
                return [path + [start]]
            if target not in seen:
                seen.add(target)
                stack.append((target, path + [target]))
    return []


def check_cycles(root: Path, modules: tuple[str, ...] = NEW_MODULES) -> list[str]:
    graph = import_graph(root)
    found: list[str] = []
    for module in modules:
        if module not in graph:
            continue
        for cycle in cycles_through(graph, module):
            found.append(" -> ".join(cycle))
    return found


class WikiLibImportDirectionTests(unittest.TestCase):
    def test_spec_directions_hold(self):
        violations, skipped = check_rules(ROOT)
        for name in skipped:
            print(f"skipped import-direction rule (file missing): {name}", file=sys.stderr)
        self.assertEqual(violations, [])

    def test_new_modules_are_not_on_import_cycles(self):
        self.assertEqual(check_cycles(ROOT), [])


class CheckerSelfTests(unittest.TestCase):
    """The checker must catch the forbidden imports it claims to check."""

    def _tree(self, files: dict[str, str]) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for package in ("tools", "tools/wiki", "tools/wiki/lib", "tools/vault_migration"):
            (root / package).mkdir(parents=True, exist_ok=True)
            (root / package / "__init__.py").write_text("", encoding="utf-8")
        for relative, text in files.items():
            (root / relative).write_text(text, encoding="utf-8")
        return root

    def test_missing_files_are_skipped_by_name(self):
        root = self._tree({})
        violations, skipped = check_rules(root)
        self.assertEqual(violations, [])
        self.assertEqual(skipped, [rule.name for rule in RULES])

    def test_relative_absolute_and_function_level_imports_are_caught(self):
        root = self._tree(
            {
                "tools/wiki/lib/review.py": "",
                "tools/wiki/lib/memory_proposal.py": "",
                "tools/wiki/lib/publish.py": (
                    "from .review import x\n"
                    "def f():\n"
                    "    from tools.wiki.lib import memory_proposal\n"
                ),
            }
        )
        violations, _ = check_rules(root)
        self.assertIn(
            "publish does not import review or memory_proposal: tools.wiki.lib.publish imports tools.wiki.lib.review",
            violations,
        )
        self.assertIn(
            "publish does not import review or memory_proposal: "
            "tools.wiki.lib.publish imports tools.wiki.lib.memory_proposal",
            violations,
        )

    def test_review_importing_memory_proposal_is_caught(self):
        root = self._tree(
            {
                "tools/wiki/lib/memory_proposal.py": "",
                "tools/wiki/lib/review.py": "import tools.wiki.lib.memory_proposal\n",
            }
        )
        violations, _ = check_rules(root)
        self.assertTrue(any(v.startswith("review does not import") for v in violations), violations)

    def test_memory_fields_allow_list(self):
        root = self._tree(
            {
                "tools/wiki/lib/schema.py": "",
                "tools/wiki/lib/frontmatter.py": "",
                "tools/wiki/lib/lint.py": "",
                "tools/wiki/lib/memory_fields.py": (
                    "import re\nfrom .schema import Issue\nfrom .frontmatter import Page\nfrom .lint import x\n"
                ),
            }
        )
        violations, _ = check_rules(root)
        self.assertEqual(
            violations,
            [
                "memory_fields imports only schema and frontmatter: "
                "tools.wiki.lib.memory_fields imports tools.wiki.lib.lint"
            ],
        )

    def test_hooks_and_map_settings_must_not_import_each_other(self):
        root = self._tree(
            {
                "tools/vault_migration/hooks.py": "from .map_settings import y\n",
                "tools/vault_migration/map_settings.py": "from tools.vault_migration.hooks import z\n",
            }
        )
        violations, _ = check_rules(root)
        self.assertEqual(
            sorted(violations),
            [
                "hooks does not import map_settings or migration cli: "
                "tools.vault_migration.hooks imports tools.vault_migration.map_settings",
                "map_settings does not import hooks or migration cli: "
                "tools.vault_migration.map_settings imports tools.vault_migration.hooks",
            ],
        )

    def test_cycle_through_new_module_is_caught(self):
        root = self._tree(
            {
                "tools/wiki/lib/publish.py": "from .draft import a\n",
                "tools/wiki/lib/draft.py": "from .lint import b\n",
                "tools/wiki/lib/lint.py": "from .publish import c\n",
            }
        )
        self.assertEqual(
            check_cycles(root),
            ["tools.wiki.lib.publish -> tools.wiki.lib.draft -> tools.wiki.lib.lint -> tools.wiki.lib.publish"],
        )


if __name__ == "__main__":
    unittest.main()
