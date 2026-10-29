from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
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


# Router tests run with a temporary HOME and without CLAUDE_CONFIG_DIR, so the link config the router
# reads is <temporary HOME>/.claude/vault-builder/vault-map.json and the real one is never touched.
_SAVED_ENVIRONMENT: dict[str, str | None] = {}
_MODULE_TMP: list[Path] = []

LOG_LINE = r"^llm-wiki tool=\S+ vault=\S+ exit=\S+ ms=\d+$"
VALID_DRAFT = {
    "version": 1,
    "operation": "upsert-page",
    "path": "wiki/concepts/router-test.md",
    "frontmatter": {
        "title": "Router Test",
        "type": "concept",
        "status": "active",
        "created": "2026-10-29",
        "updated": "2026-10-29",
        "owner": "agent",
        "summary": "A page published through the llm-wiki router.",
        "source_count": 0,
        "tags": ["test"],
        "related": ["index"],
        "confidence": "high",
        "quality": {"provenance": "none", "links": "unchecked", "contradictions": "none", "review_required": False},
    },
    "body": "# Router Test\n\nPublished through the router.\n",
    "index": {
        "section": "Concepts",
        "target": "concepts/router-test",
        "summary": "A page published through the llm-wiki router.",
    },
    "log": {"event_type": "repair", "title": "Router Test", "items": ["Added [[concepts/router-test]]."]},
}


def setUpModule() -> None:
    for key in ("HOME", "CLAUDE_CONFIG_DIR"):
        _SAVED_ENVIRONMENT[key] = os.environ.get(key)
    tmp = Path(tempfile.mkdtemp()).resolve()
    _MODULE_TMP.append(tmp)
    (tmp / "home").mkdir()
    os.environ["HOME"] = str(tmp / "home")
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    _build_template_vault(tmp / "template")


def tearDownModule() -> None:
    for key, value in _SAVED_ENVIRONMENT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for tmp in _MODULE_TMP:
        shutil.rmtree(tmp, ignore_errors=True)


def _build_template_vault(target: Path) -> None:
    """A small real vault: this checkout's wiki tools and wiki pages, committed in its own git repository."""
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(BUILDER_ROOT / "tools" / "wiki", target / "tools" / "wiki", ignore=ignore)
    shutil.copytree(BUILDER_ROOT / "wiki", target / "wiki", ignore=ignore)
    shutil.copy2(BUILDER_ROOT / ".gitignore", target / ".gitignore")
    for folder in ("raw/sources", "scratch/reports", "scratch/review"):
        (target / folder).mkdir(parents=True)
        (target / folder / ".gitkeep").write_text("", encoding="utf-8")
    (target / "scratch" / "drafts").mkdir(parents=True)
    (target / "scratch" / "drafts" / "valid.json").write_text(json.dumps(VALID_DRAFT, indent=2), encoding="utf-8")
    invalid = json.loads(json.dumps(VALID_DRAFT))
    invalid["path"] = "wiki/concepts/router-invalid.md"
    del invalid["frontmatter"]["summary"]
    (target / "scratch" / "drafts" / "invalid.json").write_text(json.dumps(invalid, indent=2), encoding="utf-8")
    git(target, "init", "-q")
    git(target, "add", "-A")
    git(target, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")


def template_vault() -> Path:
    return _MODULE_TMP[0] / "template"


def map_path() -> Path:
    return Path(os.environ["HOME"]) / ".claude" / "vault-builder" / "vault-map.json"


def write_map(data: dict) -> None:
    map_path().parent.mkdir(parents=True, exist_ok=True)
    map_path().write_text(json.dumps(data), encoding="utf-8")


def tree_state(root: Path) -> dict[str, str]:
    """File list and content hash of everything outside .git, including any __pycache__ folders."""
    state = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            state[str(relative)] = "link:" + os.readlink(path)
        elif path.is_file():
            state[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            state[str(relative)] = "dir"
    return state


def git_status(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout


def call_request(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class RouterCase(unittest.TestCase):
    """Vaults p and q (personal) and t (team). Repository r is linked to p only. The router cwd is r."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(lambda: map_path().unlink(missing_ok=True))
        self.vaults = {}
        for name in ("p", "q", "t"):
            self.vaults[name] = self.tmp / "vaults" / name
            shutil.copytree(template_vault(), self.vaults[name], symlinks=True)
        self.repo = make_repo(self.tmp / "r")
        self.map = {
            "version": 2,
            "vaults": {
                "p": {"path": str(self.vaults["p"]), "role": "personal"},
                "q": {"path": str(self.vaults["q"]), "role": "personal"},
                "t": {"path": str(self.vaults["t"]), "role": "team"},
            },
            "projects": {"r": {"root": str(self.repo), "vaults": ["p"]}},
        }
        write_map(self.map)
        self.chdir(self.repo)

    def chdir(self, folder: Path) -> None:
        previous = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, previous)

    def call(self, name: str, arguments: dict) -> tuple[dict, str, int]:
        """Run one tools/call in this process. Returns the result, the stderr text, and the CLI start count."""
        stderr = io.StringIO()
        with (
            mock.patch.object(router, "start_vault_cli", wraps=router.start_vault_cli) as starts,
            contextlib.redirect_stderr(stderr),
        ):
            response = router.handle_request(call_request(name, arguments))
        self.assertEqual(response["id"], 7)
        self.assertIn("result", response, response)
        return response["result"], stderr.getvalue(), starts.call_count

    @staticmethod
    def text(result: dict) -> str:
        return "\n".join(item["text"] for item in result["content"])

    def assertRuns(self, name: str, arguments: dict) -> str:
        result, _, starts = self.call(name, arguments)
        self.assertFalse(result["isError"], self.text(result))
        self.assertGreaterEqual(starts, 1)
        return self.text(result)

    def assertRefused(self, name: str, arguments: dict) -> str:
        result, stderr, starts = self.call(name, arguments)
        self.assertTrue(result["isError"], self.text(result))
        self.assertEqual(starts, 0)
        self.assertIn("exit=refused", stderr)
        return self.text(result)


class RouterProtocolTests(RouterCase):
    def run_process(self, lines: list[dict], cwd: Path) -> tuple[list[dict], str]:
        completed = subprocess.run(
            [sys.executable, "-I", str(ROUTER_SOURCE)],
            input="".join(json.dumps(line) + "\n" for line in lines),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return [json.loads(line) for line in completed.stdout.splitlines()], completed.stderr

    def fixed_requests(self) -> list[dict]:
        return [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "server/discover"},
            {"jsonrpc": "2.0", "id": 4, "method": "no/such"},
        ]

    def test_router_process_answers_fixed_requests_without_link_config(self) -> None:
        map_path().unlink()
        responses, _ = self.run_process(self.fixed_requests(), self.tmp)
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        initialize, tools, discover, unknown = (response.get("result") for response in responses)
        self.assertEqual(initialize["serverInfo"]["name"], "llm-wiki")
        self.assertEqual(len(tools["tools"]), 24)
        self.assertEqual(discover["serverInfo"], initialize["serverInfo"])
        self.assertEqual(discover["capabilities"], initialize["capabilities"])
        self.assertEqual(discover["tools"], tools["tools"])
        self.assertIsNone(unknown)
        self.assertEqual(responses[3]["error"]["code"], -32601)

    def test_router_fixed_responses_are_identical_across_calls_cwd_and_config(self) -> None:
        def fixed() -> list[str]:
            return [
                json.dumps(router.handle_request(request)["result"])
                for request in self.fixed_requests()
                if "id" in request and request["method"] != "no/such"
            ]

        before = fixed()
        self.call("wiki_search", {"query": "wiki"})
        self.call("wiki_health", {})
        self.assertEqual(fixed(), before)
        self.chdir(self.tmp)
        self.map["projects"] = {}
        write_map(self.map)
        self.assertEqual(fixed(), before)
        map_path().unlink()
        self.assertEqual(fixed(), before)
        process_responses, _ = self.run_process(self.fixed_requests(), self.vaults["q"])
        self.assertEqual([json.dumps(response["result"]) for response in process_responses[:3]], before)

    def test_router_notification_gets_no_response(self) -> None:
        self.assertIsNone(router.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_router_process_runs_tool_call_and_logs_one_line(self) -> None:
        responses, stderr = self.run_process([call_request("wiki_read_page", {"path": "wiki/overview.md"})], self.repo)
        self.assertFalse(responses[0]["result"]["isError"], responses[0])
        self.assertRegex(stderr.strip(), LOG_LINE)
        self.assertIn("tool=wiki_read_page vault=p exit=0", stderr)

    def test_router_missing_or_damaged_config_is_a_tool_error_with_path_and_install(self) -> None:
        map_path().unlink()
        text = self.assertRefused("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertIn(str(map_path()), text)
        self.assertIn("install", text)
        map_path().write_text("{", encoding="utf-8")
        text = self.assertRefused("wiki_health", {"vault": "p"})
        self.assertIn(str(map_path()), text)
        self.assertIn("install", text)


class RouterToolCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if str(BUILDER_ROOT) not in sys.path:
            sys.path.insert(0, str(BUILDER_ROOT))
        from tools.wiki import mcp_server

        cls.mcp_server = mcp_server

    def server_argv(self, name: str, arguments: dict) -> list[str]:
        captured = []

        def fake_run(argv: list[str]) -> tuple[int, str]:
            captured.append(argv)
            return 0, ""

        with mock.patch.object(self.mcp_server, "_run_cli", fake_run):
            self.mcp_server._call_tool(name, arguments)
        return captured[0]

    @staticmethod
    def sample(schema: dict, required_only: bool) -> dict:
        values = {"string": "wiki/x.md", "array": ["wiki/a.md", "wiki/b.md"], "boolean": True, "integer": 3}
        properties = schema["properties"]
        names = schema.get("required", []) if required_only else list(properties)
        return {name: values[properties[name]["type"]] for name in names}

    def test_router_tool_names_match_mcp_server(self) -> None:
        names = [tool["name"] for tool in router.tool_list()]
        self.assertEqual(len(names), 24)
        self.assertEqual(names, [tool["name"] for tool in self.mcp_server.TOOLS])

    def test_router_schemas_add_vault_and_cwd_only(self) -> None:
        server = {tool["name"]: tool for tool in self.mcp_server.TOOLS}
        for tool in router.tool_list():
            with self.subTest(tool["name"]):
                original = server[tool["name"]]["inputSchema"]
                schema = tool["inputSchema"]
                self.assertEqual(schema["properties"]["vault"]["type"], "string")
                self.assertEqual(schema["properties"]["cwd"]["type"], "string")
                properties = {key: value for key, value in schema["properties"].items() if key not in ("vault", "cwd")}
                self.assertEqual(properties, original["properties"])
                required = [key for key in schema.get("required", []) if key != "vault"]
                self.assertEqual(required, original.get("required", []))
                self.assertEqual("vault" in schema.get("required", []), tool["name"] in router.ALWAYS_WRITE_TOOLS)

    def test_router_cli_arguments_match_mcp_server(self) -> None:
        for tool in self.mcp_server.TOOLS:
            for required_only in (True, False):
                arguments = self.sample(tool["inputSchema"], required_only)
                with self.subTest(tool["name"], required_only=required_only):
                    expected = self.server_argv(tool["name"], dict(arguments))
                    routed = dict(arguments, vault="p", cwd="/somewhere")
                    self.assertEqual(router.cli_arguments(tool["name"], routed), expected)

    def test_router_classification_table(self) -> None:
        reads = [
            ("wiki_search", {}),
            ("wiki_read_page", {}),
            ("wiki_validate_page", {}),
            ("wiki_hash_source", {}),
            ("wiki_review_list", {"status": "pending"}),
            ("wiki_lint", {}),
            ("wiki_source_registry", {}),
            ("wiki_apply_draft", {}),
            ("wiki_merge_scan", {}),
            ("wiki_maps_build", {"dry_run": True}),
            ("wiki_maps_build", {"check": True}),
        ]
        writes = [
            ("wiki_lint", {"report": "scratch/reports/l.md"}),
            ("wiki_source_registry", {"report": "scratch/reports/s.md"}),
            ("wiki_apply_draft", {"report": "scratch/reports/a.md"}),
            ("wiki_merge_scan", {"report": "scratch/reports/m.md"}),
            ("wiki_merge_scan", {"create_review": True}),
            ("wiki_maps_build", {}),
            ("wiki_maps_build", {"dry_run": False, "check": False}),
            # maps build --dry-run --report writes the report file, so a report makes it a write.
            ("wiki_maps_build", {"dry_run": True, "report": "scratch/reports/m.md"}),
            ("wiki_maps_build", {"check": True, "report": "scratch/reports/m.md"}),
            ("wiki_unknown_tool", {}),
        ] + [(name, {}) for name in sorted(router.ALWAYS_WRITE_TOOLS)]
        self.assertEqual(len(router.ALWAYS_WRITE_TOOLS), 14)
        for name, arguments in reads:
            with self.subTest(name, arguments=arguments):
                self.assertFalse(router.is_write_call(name, arguments))
        for name, arguments in writes:
            with self.subTest(name, arguments=arguments):
                self.assertTrue(router.is_write_call(name, arguments))


class RouterWriteTests(RouterCase):
    def publish(self, vault: str | None, draft: str = "scratch/drafts/valid.json") -> tuple[dict, str, int]:
        arguments = {"path": draft}
        if vault is not None:
            arguments["vault"] = vault
        return self.call("wiki_publish_draft", arguments)

    def test_router_write_to_session_linked_vault(self) -> None:
        result, _, starts = self.publish("p")
        self.assertFalse(result["isError"], self.text(result))
        self.assertEqual(starts, 1)
        self.assertTrue((self.vaults["p"] / "wiki" / "concepts" / "router-test.md").is_file())

    def test_router_write_to_unreachable_vault_runs(self) -> None:
        result, _, _ = self.publish("q")
        self.assertFalse(result["isError"], self.text(result))
        self.assertTrue((self.vaults["q"] / "wiki" / "concepts" / "router-test.md").is_file())

    def test_router_write_to_team_vault_runs(self) -> None:
        result, stderr, _ = self.publish("t")
        self.assertFalse(result["isError"], self.text(result))
        self.assertTrue((self.vaults["t"] / "wiki" / "concepts" / "router-test.md").is_file())
        self.assertIn("vault=t exit=0", stderr)

    def test_router_write_without_vault_is_refused(self) -> None:
        before = {name: tree_state(path) for name, path in self.vaults.items()}
        result, stderr, starts = self.publish(None)
        self.assertTrue(result["isError"])
        self.assertEqual(starts, 0)
        self.assertIn("p, q, t", self.text(result))
        self.assertIn("vault=- exit=refused", stderr)
        self.assertEqual({name: tree_state(path) for name, path in self.vaults.items()}, before)

    def test_router_write_with_unknown_vault_is_refused(self) -> None:
        text = self.assertRefused("wiki_health", {"vault": "nope"})
        self.assertIn("p, q, t", text)

    def test_router_write_works_outside_git_and_cwd_argument_is_ignored(self) -> None:
        self.chdir(self.tmp)
        self.assertRuns("wiki_health", {"vault": "q"})
        self.assertRuns("wiki_health", {"vault": "q", "cwd": str(self.tmp)})

    def test_router_invalid_draft_is_rejected_by_vault_validation(self) -> None:
        before = tree_state(self.vaults["q"] / "wiki")
        result, _, starts = self.publish("q", "scratch/drafts/invalid.json")
        self.assertTrue(result["isError"])
        self.assertEqual(starts, 1)
        self.assertEqual(tree_state(self.vaults["q"] / "wiki"), before)

    def test_router_path_arguments_outside_the_vault_are_refused(self) -> None:
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps(VALID_DRAFT), encoding="utf-8")
        (self.vaults["p"] / "scratch" / "drafts" / "escape.json").symlink_to(outside)
        (self.vaults["p"] / "escape-dir").symlink_to(self.tmp)
        before = tree_state(self.vaults["p"])
        cases = [
            ("wiki_publish_draft", {"path": "../x.json"}, "path"),
            ("wiki_publish_draft", {"path": "scratch/../../x.json"}, "path"),
            ("wiki_publish_draft", {"path": str(outside)}, "path"),
            ("wiki_publish_draft", {"path": "scratch/drafts/escape.json"}, "path"),
            ("wiki_publish_draft", {"path": "scratch/drafts/valid.json", "report": "escape-dir/r.md"}, "report"),
            ("wiki_bulk_ingest", {"paths": ["raw/sources/a.md", "/etc/hosts"]}, "paths"),
            ("wiki_workflow_ingest", {"source": "../raw.md"}, "source"),
            ("wiki_workflow_update_publish", {"target": "wiki/overview.md", "draft": "/tmp/d.json"}, "draft"),
            ("wiki_workflow_update_preflight", {"target": "../wiki/overview.md"}, "target"),
            ("wiki_read_page", {"path": "../q/wiki/overview.md"}, "path"),
            ("wiki_lint", {"report": "/tmp/lint.md"}, "report"),
        ]
        for name, arguments, argument in cases:
            with self.subTest(name, argument=argument):
                text = self.assertRefused(name, dict(arguments, vault="p"))
                self.assertIn(argument, text)
        self.assertEqual(tree_state(self.vaults["p"]), before)

    def test_router_relative_path_inside_vault_is_allowed(self) -> None:
        self.assertRuns("wiki_read_page", {"vault": "p", "path": "wiki/../wiki/overview.md"})


class RouterReadScopeTests(RouterCase):
    def test_router_read_reaches_linked_vault_only(self) -> None:
        self.assertRuns("wiki_read_page", {"vault": "p", "path": "wiki/overview.md"})
        self.assertRuns("wiki_search", {"vault": "p", "query": "wiki"})
        for name, arguments in (
            ("wiki_read_page", {"path": "wiki/overview.md"}),
            ("wiki_search", {"query": "wiki"}),
        ):
            for vault in ("q", "t"):
                with self.subTest(name, vault=vault):
                    text = self.assertRefused(name, dict(arguments, vault=vault))
                    self.assertIn("p", text.split("Reachable vaults:")[1].splitlines()[0])
                    self.assertIn("link", text)

    def test_router_read_from_vault_folder_reaches_that_vault_only(self) -> None:
        self.chdir(self.vaults["q"] / "wiki")
        self.assertRuns("wiki_read_page", {"vault": "q", "path": "wiki/overview.md"})
        self.assertRefused("wiki_read_page", {"vault": "p", "path": "wiki/overview.md"})
        text = self.assertRuns("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertNotIn("Run link again", text)

    def test_router_cwd_argument_sets_the_session(self) -> None:
        self.assertRuns("wiki_read_page", {"vault": "q", "path": "wiki/overview.md", "cwd": str(self.vaults["q"])})
        self.assertRefused("wiki_read_page", {"vault": "p", "path": "wiki/overview.md", "cwd": str(self.vaults["q"])})

    def test_router_relative_cwd_argument_is_refused(self) -> None:
        text = self.assertRefused("wiki_read_page", {"path": "wiki/overview.md", "cwd": "vaults/q"})
        self.assertIn("cwd", text)

    def test_router_write_ignores_read_scope(self) -> None:
        self.assertRuns("wiki_health", {"vault": "q"})

    def test_router_read_without_vault_uses_the_single_reachable_vault(self) -> None:
        result, stderr, _ = self.call("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertFalse(result["isError"], self.text(result))
        self.assertIn("vault=p exit=0", stderr)

    def test_router_read_without_vault_and_two_reachable_vaults_is_refused(self) -> None:
        self.map["projects"]["r"]["vaults"] = ["p", "q"]
        write_map(self.map)
        text = self.assertRefused("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertIn("p, q", text)

    def test_router_read_outside_git_is_refused_with_names_and_link(self) -> None:
        self.chdir(self.tmp)
        text = self.assertRefused("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertIn("p, q, t", text)
        self.assertIn("link", text)
        self.assertRuns("wiki_read_page", {"path": "wiki/overview.md", "cwd": str(self.repo)})

    def test_router_ambiguous_project_leaves_nothing_reachable(self) -> None:
        self.map["projects"]["s"] = {"root": str(self.repo) + "/", "vaults": ["q"]}
        write_map(self.map)
        text = self.assertRefused("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertIn("r, s", text)

    def test_router_moved_repository_adds_link_again_line(self) -> None:
        moved = make_repo(self.tmp / "moved" / "r")
        self.chdir(moved)
        text = self.assertRuns("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertIn("Run link again", text)

    def test_router_missing_vault_folder_is_an_error(self) -> None:
        shutil.rmtree(self.vaults["p"])
        text = self.assertRefused("wiki_read_page", {"path": "wiki/overview.md"})
        self.assertIn(str(self.vaults["p"]), text)
        self.assertRefused("wiki_health", {"vault": "p"})


class RouterSearchTests(RouterCase):
    def setUp(self) -> None:
        super().setUp()
        self.map["projects"]["r"]["vaults"] = ["q", "p"]
        write_map(self.map)

    def test_router_search_covers_every_reachable_vault_in_name_order(self) -> None:
        result, stderr, starts = self.call("wiki_search", {"query": "wiki", "limit": 2})
        text = self.text(result)
        self.assertFalse(result["isError"], text)
        self.assertEqual(starts, 2)
        self.assertLess(text.index("# vault p (personal)"), text.index("# vault q (personal)"))
        self.assertEqual(len(stderr.splitlines()), 1)

    def test_router_search_limit_applies_per_vault(self) -> None:
        with (
            mock.patch.object(router, "start_vault_cli", wraps=router.start_vault_cli) as starts,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            router.handle_request(call_request("wiki_search", {"query": "wiki", "limit": 2}))
        for call in starts.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index("--limit") + 1], "2")

    def test_router_search_keeps_going_when_one_vault_fails(self) -> None:
        shutil.rmtree(self.vaults["q"])
        result, _, starts = self.call("wiki_search", {"query": "wiki"})
        text = self.text(result)
        self.assertFalse(result["isError"], text)
        self.assertEqual(starts, 1)
        self.assertIn("# vault p (personal)", text)
        after_q = text.split("# vault q (personal)")[1].strip().splitlines()
        self.assertEqual(len(after_q), 1)
        self.assertIn("Error", after_q[0])

    def test_router_search_is_error_when_every_vault_fails(self) -> None:
        shutil.rmtree(self.vaults["p"])
        shutil.rmtree(self.vaults["q"])
        result, _, _ = self.call("wiki_search", {"query": "wiki"})
        self.assertTrue(result["isError"])

    def test_router_search_from_vault_folder_covers_that_vault_only(self) -> None:
        self.chdir(self.vaults["p"])
        text = self.assertRuns("wiki_search", {"query": "wiki"})
        self.assertIn("# vault p (personal)", text)
        self.assertNotIn("# vault q", text)

    def test_router_search_from_unlinked_repository_is_refused(self) -> None:
        self.chdir(make_repo(self.tmp / "other"))
        text = self.assertRefused("wiki_search", {"query": "wiki"})
        self.assertIn("p, q, t", text)
        self.assertIn("link", text)


class RouterStatelessTests(RouterCase):
    def test_router_link_change_applies_to_the_next_call(self) -> None:
        self.assertNotIn("# vault q", self.assertRuns("wiki_search", {"query": "wiki"}))
        self.map["projects"]["r"]["vaults"] = ["p", "q"]
        write_map(self.map)
        self.assertIn("# vault q (personal)", self.assertRuns("wiki_search", {"query": "wiki"}))

    def test_router_unlink_and_link_again(self) -> None:
        arguments = {"vault": "p", "path": "wiki/overview.md"}
        self.assertRuns("wiki_read_page", arguments)
        self.map["projects"] = {}
        write_map(self.map)
        self.assertIn("link", self.assertRefused("wiki_read_page", arguments))
        self.map["projects"] = {"r": {"root": str(self.repo), "vaults": ["p"]}}
        write_map(self.map)
        self.assertRuns("wiki_read_page", arguments)

    def test_router_removed_config_and_restored_config(self) -> None:
        arguments = {"vault": "p", "path": "wiki/overview.md"}
        map_path().unlink()
        self.assertRefused("wiki_read_page", arguments)
        write_map(self.map)
        self.assertRuns("wiki_read_page", arguments)

    def test_router_starts_one_cli_per_call_and_none_when_refused(self) -> None:
        calls = [
            ("wiki_read_page", {"path": "wiki/overview.md"}, 1),
            ("wiki_health", {"vault": "q"}, 1),
            ("wiki_hash_source", {"vault": "p", "path": "wiki/overview.md"}, 1),
            ("wiki_health", {}, 0),
            ("wiki_read_page", {"vault": "q", "path": "wiki/overview.md"}, 0),
            ("wiki_read_page", {"vault": "p", "path": "/etc/hosts"}, 0),
        ]
        for name, arguments, expected in calls:
            with self.subTest(name, arguments=arguments):
                _, _, starts = self.call(name, arguments)
                self.assertEqual(starts, expected)

    def test_router_logs_one_line_per_call_without_argument_values(self) -> None:
        marker = "MARKER-7f3a"
        for name, arguments in (
            ("wiki_search", {"query": marker}),
            ("wiki_health", {}),
            ("wiki_read_page", {"vault": "q", "path": marker}),
            ("wiki_health", {"vault": marker}),
            ("wiki_no_such_tool", {"query": marker}),
        ):
            with self.subTest(name, arguments=arguments):
                _, stderr, _ = self.call(name, arguments)
                lines = stderr.splitlines()
                self.assertEqual(len(lines), 1, stderr)
                self.assertRegex(lines[0], LOG_LINE)
                self.assertNotIn(marker, stderr)


class RouterExecutionTests(RouterCase):
    def fake_vault(self, name: str, source: str) -> Path:
        root = self.tmp / "fake" / name
        (root / "tools" / "wiki").mkdir(parents=True)
        (root / "tools" / "wiki" / "cli.py").write_text(source, encoding="utf-8")
        self.map["vaults"][name] = {"path": str(root), "role": "personal"}
        write_map(self.map)
        return root

    def test_router_runs_vault_cli_with_isolated_interpreter_in_vault(self) -> None:
        with (
            mock.patch.object(router, "start_vault_cli", wraps=router.start_vault_cli) as starts,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            router.handle_request(call_request("wiki_health", {"vault": "q"}))
        command, cwd, timeout = starts.call_args.args
        self.assertEqual(command, [sys.executable, "-I", "-B", str(self.vaults["q"] / "tools" / "wiki" / "cli.py"), "health"])
        self.assertEqual(Path(cwd), self.vaults["q"])
        self.assertEqual(timeout, 120)
        self.assertEqual(router.CLI_TIMEOUT_SECONDS, 120)

    def test_router_old_vault_tools_get_audit_and_apply_hint(self) -> None:
        self.fake_vault(
            "old",
            "import argparse\n"
            "parser = argparse.ArgumentParser(prog='wiki')\n"
            "sub = parser.add_subparsers(dest='command', required=True)\n"
            "sub.add_parser('search')\n"
            "parser.parse_args()\n",
        )
        result, stderr, starts = self.call("wiki_health", {"vault": "old"})
        text = self.text(result)
        self.assertTrue(result["isError"])
        self.assertEqual(starts, 1)
        self.assertIn("invalid choice", text)
        self.assertIn("audit", text)
        self.assertIn("apply", text)
        self.assertIn("vault=old exit=2", stderr)

    def test_router_failed_cli_returns_its_output(self) -> None:
        self.fake_vault("broken", "import sys\nprint('broken output')\nsys.exit(3)\n")
        result, _, _ = self.call("wiki_health", {"vault": "broken"})
        self.assertTrue(result["isError"])
        self.assertIn("broken output", self.text(result))
        self.assertNotIn("audit", self.text(result))

    def test_router_timeout_stops_the_cli_and_next_call_works(self) -> None:
        self.fake_vault("slow", "import time\ntime.sleep(30)\n")
        with mock.patch.object(router, "CLI_TIMEOUT_SECONDS", 1):
            result, stderr, _ = self.call("wiki_health", {"vault": "slow"})
        self.assertTrue(result["isError"])
        self.assertIn("timed out", self.text(result))
        self.assertIn("exit=timeout", stderr)
        self.assertRuns("wiki_health", {"vault": "q"})


class RouterReadOnlyTests(RouterCase):
    def test_router_read_calls_do_not_change_the_vault(self) -> None:
        reads = [
            ("wiki_search", {"query": "wiki"}),
            ("wiki_read_page", {"path": "wiki/overview.md", "section": "## Purpose"}),
            ("wiki_validate_page", {"path": "wiki/overview.md"}),
            ("wiki_hash_source", {"path": "wiki/overview.md"}),
            ("wiki_review_list", {}),
            ("wiki_lint", {}),
            ("wiki_source_registry", {}),
            ("wiki_apply_draft", {"path": "scratch/drafts/valid.json"}),
            ("wiki_merge_scan", {}),
            ("wiki_maps_build", {"dry_run": True}),
            ("wiki_maps_build", {"check": True}),
        ]
        vault = self.vaults["p"]
        for name, arguments in reads:
            with self.subTest(name, arguments=arguments):
                self.assertFalse(router.is_write_call(name, arguments))
                before = (tree_state(vault), git_status(vault))
                result, _, starts = self.call(name, dict(arguments, vault="p"))
                self.assertEqual(starts, 1)
                self.assertEqual((tree_state(vault), git_status(vault)), before, self.text(result))


if __name__ == "__main__":
    unittest.main()
