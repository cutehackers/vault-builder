from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BUILDER_ROOT = Path(__file__).resolve().parents[1]
ROUTER_SOURCE = BUILDER_ROOT / "tools" / "vault_migration" / "assets" / "llm-wiki-router" / "llm_wiki_router.py"


def load_router():
    spec = importlib.util.spec_from_file_location("llm_wiki_router_under_test", ROUTER_SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


router = load_router()


def git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True)


def make_repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if origin is not None:
        git(path, "remote", "add", "origin", origin)
    return path.resolve()


def vault_map(projects: dict, version: int = 2) -> dict:
    return {
        "version": version,
        "vaults": {"a": {"path": "/vaults/a", "role": "personal"}},
        "projects": projects,
    }


class FindProjectSourceRulesTests(unittest.TestCase):
    def test_find_project_source_uses_stdlib_and_constant_module_state(self) -> None:
        tree = ast.parse(ROUTER_SOURCE.read_text(encoding="utf-8"))
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        immutable = (ast.Constant, ast.Tuple)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    self.assertIn(name.split(".")[0], stdlib, name)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    self.assertIsInstance(target, ast.Name)
                    self.assertTrue(target.id.isupper(), target.id)
                value = node.value
                is_frozen = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in {"frozenset", "tuple"}
                )
                self.assertTrue(isinstance(value, immutable) or is_frozen, ast.dump(node))
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, (ast.Global, ast.Nonlocal))


class FindProjectReadVaultMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = self.tmp / "vault-map.json"

    def write(self, data: object) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_find_project_reads_versions_1_and_2(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                data = vault_map({"r": {"root": "/x/r", "vaults": ["a"], "remote": "git@h:o/r.git"}}, version)
                if version == 1:
                    del data["projects"]["r"]["remote"]
                self.write(data)
                self.assertEqual(router.read_vault_map(self.path), data)

    def test_find_project_rejects_other_versions_as_damaged(self) -> None:
        for version in (0, 3, "2", True, None):
            with self.subTest(version=version):
                self.write(vault_map({}, version))
                with self.assertRaises(router.VaultMapError) as caught:
                    router.read_vault_map(self.path)
                self.assertIn("damaged", str(caught.exception))
                self.assertIn(str(self.path), str(caught.exception))

    def test_find_project_rejects_damaged_shapes(self) -> None:
        cases = {
            "not json": "{",
            "not object": [],
            "vaults list": {"version": 2, "vaults": [], "projects": {}},
            "vault no path": {"version": 2, "vaults": {"a": {"role": "personal"}}, "projects": {}},
            "project no root": {"version": 2, "vaults": {}, "projects": {"r": {"vaults": []}}},
            "project vaults not list": {"version": 2, "vaults": {}, "projects": {"r": {"root": "/r", "vaults": "a"}}},
            "remote not string": {
                "version": 2,
                "vaults": {},
                "projects": {"r": {"root": "/r", "vaults": [], "remote": 1}},
            },
        }
        for label, data in cases.items():
            with self.subTest(label):
                if isinstance(data, str):
                    self.path.write_text(data, encoding="utf-8")
                else:
                    self.write(data)
                with self.assertRaises(router.VaultMapError):
                    router.read_vault_map(self.path)

    def test_find_project_missing_map_is_an_error(self) -> None:
        with self.assertRaises(router.VaultMapError) as caught:
            router.read_vault_map(self.path)
        self.assertIn(str(self.path), str(caught.exception))


class FindProjectNormalizeRemoteTests(unittest.TestCase):
    def test_find_project_normalizes_host_forms(self) -> None:
        expected = "github.com/Owner/Repo"
        for url in (
            "git@github.com:Owner/Repo.git",
            "  git@GitHub.com:Owner/Repo  ",
            "github.com:Owner/Repo.git/",
            "https://github.com/Owner/Repo.git",
            "https://user@GITHUB.COM:443/Owner/Repo/",
            "ssh://git@github.com:22/Owner/Repo.git",
            "git://github.com/Owner/Repo",
        ):
            with self.subTest(url=url):
                self.assertEqual(router.normalize_remote(url, Path("/base")), expected)

    def test_find_project_normalizes_local_paths(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "up.git").mkdir()
        expected = str((tmp / "up.git").resolve())
        for url in (f"file://{tmp}/up.git", f"{tmp}/up.git/", "../up.git"):
            with self.subTest(url=url):
                self.assertEqual(router.normalize_remote(url, tmp / "repo"), expected)


class FindProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)

    def assertMatch(self, result, project_id: str, stage: str) -> None:
        self.assertIsInstance(result, router.ProjectMatch, result)
        self.assertEqual((result.project_id, result.stage), (project_id, stage))

    def test_find_project_by_root_from_subfolder(self) -> None:
        repo = make_repo(self.tmp / "work" / "r", "git@github.com:o/r.git")
        (repo / "sub").mkdir()
        mapping = vault_map(
            {
                "other": {"root": str(self.tmp / "elsewhere"), "vaults": ["a"], "remote": "git@github.com:o/r.git"},
                "mine": {"root": str(repo), "vaults": ["a"]},
            }
        )
        result = router.find_project(repo / "sub", mapping)
        self.assertMatch(result, "mine", "root")
        self.assertEqual(result.root, str(repo))
        self.assertIsNone(result.note)

    def test_find_project_by_root_expands_user(self) -> None:
        repo = make_repo(self.tmp / "home" / "r")
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}):
            result = router.find_project(repo, vault_map({"x": {"root": "~/r", "vaults": ["a"]}}))
        self.assertMatch(result, "x", "root")

    def test_find_project_uses_main_worktree_root(self) -> None:
        repo = make_repo(self.tmp / "r")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        worktree = self.tmp / "wt"
        git(repo, "worktree", "add", "-q", str(worktree))
        result = router.find_project(worktree, vault_map({"r": {"root": str(repo), "vaults": ["a"]}}))
        self.assertMatch(result, "r", "root")

    def test_find_project_moved_repo_by_remote(self) -> None:
        for label, stored, actual in (
            ("scp", "git@github.com:o/r.git", "https://github.com/o/r"),
            ("https", "https://github.com/o/r.git", "git@github.com:o/r.git"),
        ):
            with self.subTest(label):
                repo = make_repo(self.tmp / label / "renamed", actual)
                mapping = vault_map({"r": {"root": str(self.tmp / "old" / "r"), "vaults": ["a"], "remote": stored}})
                result = router.find_project(repo, mapping)
                self.assertMatch(result, "r", "remote")
                self.assertEqual(result.root, str(self.tmp / "old" / "r"))
                self.assertIn("link", result.note)

    def test_find_project_by_name_without_origin(self) -> None:
        repo = make_repo(self.tmp / "new" / "r")
        mapping = vault_map({"r": {"root": str(self.tmp / "old" / "r"), "vaults": ["a"], "remote": "git@h:o/r"}})
        result = router.find_project(repo, mapping)
        self.assertMatch(result, "r", "name")
        self.assertIn("link", result.note)

    def test_find_project_by_name_when_entry_has_no_remote(self) -> None:
        repo = make_repo(self.tmp / "new" / "r", "git@h:o/r")
        result = router.find_project(repo, vault_map({"r": {"root": str(self.tmp / "old" / "r"), "vaults": ["a"]}}))
        self.assertMatch(result, "r", "name")

    def test_find_project_name_with_different_remote_is_none(self) -> None:
        repo = make_repo(self.tmp / "r", "git@github.com:someone/r.git")
        mapping = vault_map(
            {"r": {"root": str(self.tmp / "old" / "r"), "vaults": ["a"], "remote": "git@github.com:other/r.git"}}
        )
        result = router.find_project(repo, mapping)
        self.assertIsInstance(result, router.Stop)
        self.assertEqual(result.ids, ())

    def test_find_project_two_projects_same_remote_stops(self) -> None:
        repo = make_repo(self.tmp / "r", "git@github.com:o/r.git")
        mapping = vault_map(
            {
                "b": {"root": "/x/b", "vaults": ["a"], "remote": "https://github.com/o/r"},
                "a": {"root": "/x/a", "vaults": ["a"], "remote": "git@github.com:o/r"},
            }
        )
        result = router.find_project(repo, mapping)
        self.assertIsInstance(result, router.Stop)
        self.assertEqual(result.ids, ("a", "b"))
        self.assertIn("a, b", result.reason)

    def test_find_project_two_projects_same_root_stops(self) -> None:
        repo = make_repo(self.tmp / "r")
        mapping = vault_map(
            {"p": {"root": str(repo), "vaults": ["a"]}, "q": {"root": str(repo) + "/", "vaults": ["a"]}}
        )
        result = router.find_project(repo, mapping)
        self.assertIsInstance(result, router.Stop)
        self.assertEqual(result.ids, ("p", "q"))

    def test_find_project_root_stage_wins_over_remote(self) -> None:
        repo = make_repo(self.tmp / "r", "git@h:o/r")
        mapping = vault_map(
            {
                "by-root": {"root": str(repo), "vaults": ["a"]},
                "by-remote": {"root": "/x", "vaults": ["a"], "remote": "git@h:o/r"},
            }
        )
        self.assertMatch(router.find_project(repo, mapping), "by-root", "root")

    def test_find_project_remote_match_with_same_root_has_no_note(self) -> None:
        repo = make_repo(self.tmp / "r", "git@h:o/r")
        mapping = vault_map({"x": {"root": str(repo), "vaults": ["a"], "remote": "git@h:o/r"}})
        self.assertMatch(router.find_project(repo, mapping), "x", "root")

    def test_find_project_version_1_by_name(self) -> None:
        repo = make_repo(self.tmp / "r", "git@h:o/r")
        mapping = vault_map({"r": {"root": str(repo), "vaults": ["a"]}}, version=1)
        self.assertMatch(router.find_project(repo, mapping), "r", "root")
        moved = make_repo(self.tmp / "moved" / "r")
        result = router.find_project(moved, mapping)
        self.assertMatch(result, "r", "name")
        self.assertIn("link", result.note)

    def test_find_project_outside_git_is_none(self) -> None:
        folder = self.tmp / "plain"
        folder.mkdir()
        result = router.find_project(folder, vault_map({"plain": {"root": str(folder), "vaults": ["a"]}}))
        self.assertIsInstance(result, router.Stop)
        self.assertEqual(result.ids, ())
        self.assertIn("git", result.reason)

    def test_find_project_unlinked_repo_is_none(self) -> None:
        repo = make_repo(self.tmp / "r")
        result = router.find_project(repo, vault_map({}))
        self.assertIsInstance(result, router.Stop)
        self.assertEqual(result.ids, ())


if __name__ == "__main__":
    unittest.main()
