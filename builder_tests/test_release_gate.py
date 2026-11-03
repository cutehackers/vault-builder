"""Behavior tests for scripts/release_gate.sh (ci-qa-redesign spec 6, A-10, A-11).

Folder tests copy the real gate into a temp folder with a stub tools/wiki/cli.py, so each run stays fast.
They stay in unit, not full_tier_only (spec 4.4 rule 3): stub tools, no real vault gate, under a second each.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "release_gate.sh"
WRITE_GUARD_MESSAGE = "Release gate changed tracked output"

STUB_CLI = textwrap.dedent(
    """\
    import os
    import sys
    from pathlib import Path

    if os.environ.get("FAKE_STEP_WRITES") == "1" and sys.argv[1:2] == ["lint"]:
        (Path(__file__).resolve().parents[2] / "created-by-gate.txt").write_text("new\\n")
    raise SystemExit(0)
    """
)

PASSING_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAILING_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_fails(self):\n        self.fail('tests must not run')\n"


def _gate_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.pop("WIKI_GATE_TIER", None)
    env.pop("WIKI_ENABLE_STENC", None)
    env["PYTHON"] = sys.executable
    return env


def _run(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = _gate_env()
    env.update(extra_env or {})
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def _make_folder(root: Path, *, builder: bool, tests_body: str = PASSING_TEST) -> Path:
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(GATE, root / "scripts" / "release_gate.sh")
    (root / "tools" / "wiki").mkdir(parents=True)
    (root / "tools" / "wiki" / "cli.py").write_text(STUB_CLI)
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_stub.py").write_text(tests_body)
    if builder:
        (root / "init-vault.sh").write_text("#!/usr/bin/env bash\n")
        (root / "tools" / "vault_migration").mkdir()
        (root / "builder_tests").mkdir()
        (root / "builder_tests" / "__init__.py").write_text("")
        (root / "builder_tests" / "test_stub.py").write_text(PASSING_TEST)
    return root


def _git_commit_all(root: Path) -> None:
    git = ["git", "-c", "user.name=gate-test", "-c", "user.email=gate-test@example.invalid"]
    env = _gate_env()
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run([*git, *args], cwd=root, env=env, check=True, capture_output=True, timeout=30)


class ReleaseGateArgumentTests(unittest.TestCase):
    """Bad arguments exit before any step runs, so these use the repository's own gate."""

    def _assert_usage_exit(self, *args: str) -> None:
        result = _run(["bash", str(GATE), *args], cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage: scripts/release_gate.sh", result.stderr)
        self.assertNotIn("[release-gate] tier:", result.stdout)

    def test_unknown_tier_value_exits_2(self) -> None:
        self._assert_usage_exit("--tier", "nope")

    def test_unknown_argument_exits_2(self) -> None:
        self._assert_usage_exit("--bogus")

    def test_tier_without_value_exits_2(self) -> None:
        self._assert_usage_exit("--tier")


class ReleaseGateFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _gate(self, root: Path, *args: str, extra_env: dict[str, str] | None = None):
        return _run(["bash", str(root / "scripts" / "release_gate.sh"), *args], cwd=root, extra_env=extra_env)

    def test_vault_folder_without_arguments_runs_check_and_skips_tests(self) -> None:
        root = _make_folder(self.tmp / "vault", builder=False, tests_body=FAILING_TEST)
        result = self._gate(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[release-gate] tier: check\n", result.stdout)
        self.assertNotIn("unittest discover", result.stdout)

    def test_builder_folder_without_arguments_runs_unit(self) -> None:
        root = _make_folder(self.tmp / "builder", builder=True)
        result = self._gate(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[release-gate] tier: unit\n", result.stdout)
        self.assertIn("WIKI_GATE_TIER=unit", result.stdout)
        self.assertIn("discover -s builder_tests", result.stdout)

    def test_clean_git_vault_passes_the_write_guard(self) -> None:
        root = _make_folder(self.tmp / "vault", builder=False)
        _git_commit_all(root)
        result = self._gate(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(WRITE_GUARD_MESSAGE, result.stderr)

    def test_step_that_creates_untracked_file_fails_the_write_guard(self) -> None:
        root = _make_folder(self.tmp / "vault", builder=False)
        _git_commit_all(root)
        result = self._gate(root, extra_env={"FAKE_STEP_WRITES": "1"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(WRITE_GUARD_MESSAGE, result.stderr)
        self.assertIn("created-by-gate.txt", result.stderr)


if __name__ == "__main__":
    unittest.main()
