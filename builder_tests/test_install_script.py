"""scripts/install.sh: tool checks, the builder checkout, the vault-builder command file and --uninstall.

Spec 4.3, 4.8, decisions 2, 5, 10, 13, A-1, A-7, A-19, A-23, A-27, A-33. Every test runs install.sh with a
temporary HOME and a stub PATH (a fake pgrep in front of /usr/bin:/bin). The builder repository is a local git
copy of this working tree, so no test reaches the network or reads or writes the real HOME. Each run starts a
new session, so it has no /dev/tty and runs in non-interactive mode. install.sh installs no other tool: a test
gives a fake vg through VAULT_GRAPH_BIN only to check that the hooks record a vg the caller names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from builder_tests._tiers import full_tier_only
from tools.vault_migration import builder_command

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"
BASH = shutil.which("bash", path="/usr/bin:/bin") or "/bin/bash"
EVENTS = ("SessionStart", "PreCompact", "SessionEnd")
EXPORT_LINE = 'export PATH="$HOME/.local/bin:$PATH"'

FAKE_VG = """#!{python} -I
import sys
args = sys.argv[1:]
with open({log!r}, "a", encoding="utf-8") as log:
    log.write("vg " + " ".join(args) + "\\n")
if args == ["--help"]:
    raise SystemExit(0)
if args[:1] == ["hook"]:
    sys.stdin.read()
    print("VG-HOOK " + args[1])
    raise SystemExit(0)
raise SystemExit(2)
"""

FAKE_PGREP = """#!/bin/sh
if [ -e "{flag}" ]; then exit 0; fi
exit 1
"""

OLD_PYTHON = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "Python 3.9.6"; fi
exit 1
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=120
    )
    return result.stdout


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            result[str(path.relative_to(root))] = "other"
    return result


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    return match.group(0)


class _InstallScriptCase(unittest.TestCase):
    """One builder source repository for the class, a fresh HOME and stub folder for each test."""

    source: Path
    _class_tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls._class_tmp = tempfile.TemporaryDirectory()
        base = Path(cls._class_tmp.name).resolve()
        git_env = cls._git_env(base)
        cls.source = base / "vault-builder-source"
        files = _git("ls-files", "-co", "--exclude-standard", "-z", cwd=ROOT).split("\0")
        for name in filter(None, files):
            origin = ROOT / name
            if not origin.exists() and not origin.is_symlink():
                continue
            target = cls.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target, follow_symlinks=False)
        _git("init", "-q", "-b", "main", cwd=cls.source, env=git_env)
        _git("add", "-A", cwd=cls.source, env=git_env)
        _git("commit", "-q", "-m", "source", cwd=cls.source, env=git_env)
        _git("checkout", "-q", "-b", "other", cwd=cls.source, env=git_env)
        (cls.source / "OTHER-REF").write_text("other\n", encoding="utf-8")
        _git("add", "OTHER-REF", cwd=cls.source, env=git_env)
        _git("commit", "-q", "-m", "other", cwd=cls.source, env=git_env)
        _git("checkout", "-q", "main", cwd=cls.source, env=git_env)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._class_tmp.cleanup()

    @staticmethod
    def _git_env(home: Path) -> dict[str, str]:
        return {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.log = self.tmp / "stub.log"
        self.log.touch()
        self.claude_flag = self.tmp / "claude-running"
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self.sources = self.tmp / "stub-sources"  # files the tests name; never on PATH
        self.sources.mkdir()
        self.python = _write(self.stub / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        self.fake_vg = _write(self.sources / "vg", FAKE_VG.format(python=sys.executable, log=str(self.log)))
        _write(self.stub / "pgrep", FAKE_PGREP.format(flag=self.claude_flag))
        self.app = self.tmp / "app"
        self.app.mkdir()
        _git("init", "-q", str(self.app), env=self._git_env(self.tmp))

    # Paths.

    @property
    def checkout(self) -> Path:
        return self.home / ".local" / "share" / "vault-builder"

    @property
    def command(self) -> Path:
        return self.home / ".local" / "bin" / "vault-builder"

    @property
    def vault_map(self) -> Path:
        return self.home / ".claude" / "vault-builder" / "vault-map.json"

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    @property
    def settings(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def hooks_config(self) -> Path:
        return self.home / ".claude" / "vault-builder" / "vault-hooks.json"

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    # Running install.sh.

    def env(self, **extra: str) -> dict[str, str]:
        environment = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join([str(self.stub), "/usr/bin", "/bin"]),
            "PYTHON": str(self.python),
            "SHELL": "/bin/bash",
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "VAULT_BUILDER_REPO": str(self.source),
        }
        environment.update(extra)
        return environment

    def run_script(
        self, *args: str, pipe: bool = False, env: dict[str, str] | None = None, timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        """Run install.sh without /dev/tty. pipe=True runs it the README way: the script on stdin of bash."""
        if pipe:
            argv = [BASH, "-s", "--", *args]
            stdin_text: str | None = SCRIPT.read_text(encoding="utf-8")
        else:
            argv = [BASH, str(SCRIPT), *args]
            stdin_text = None
        return subprocess.run(
            argv,
            input=stdin_text,
            stdin=None if pipe else subprocess.DEVNULL,
            env=env or self.env(),
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
            check=False,
        )

    def assert_exit(self, code: int, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(code, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def quick(self, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """A run that writes only the checkout and the command file: setup --dry-run."""
        return self.run_script("--yes", "--dry-run", *extra, env=env)

    def expected_command(self, checkout: Path | None = None) -> bytes:
        return builder_command.command_text(checkout or self.checkout, str(self.python)).encode("utf-8")


class ToolCheckTests(_InstallScriptCase):
    def minimal_path(self, *tools: str) -> str:
        """A PATH folder with only the named tools (real git, the test Python as python3)."""
        folder = self.tmp / "minimal"
        folder.mkdir(exist_ok=True)
        for tool in tools:
            if tool == "git":
                os.symlink(shutil.which("git", path="/usr/bin:/bin") or "/usr/bin/git", folder / "git")
            elif tool == "python3":
                shutil.copy2(self.python, folder / "python3")
            else:
                shutil.copy2(self.stub / tool, folder / tool)
        return str(folder)

    def assert_stopped_without_writing(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assert_exit(1, result)
        self.assertEqual({}, _snapshot(self.home))
        self.assertIn("Nothing was written.", result.stderr)

    def test_missing_git_stops_with_exit_1_and_writes_nothing(self) -> None:
        env = self.env(PATH=self.minimal_path("python3"))
        env.pop("PYTHON")
        result = self.run_script("--yes", env=env)
        self.assert_stopped_without_writing(result)
        self.assertIn("Missing required command: git", result.stderr)

    def test_python_older_than_3_10_stops_with_exit_1_and_writes_nothing(self) -> None:
        path = self.minimal_path("git")
        _write(Path(path) / "python3", OLD_PYTHON)
        env = self.env(PATH=path)
        env.pop("PYTHON")
        result = self.run_script("--yes", env=env)
        self.assert_stopped_without_writing(result)
        self.assertIn("Python 3.10 or newer is required", result.stderr)
        self.assertIn("Found: Python 3.9.6", result.stderr)

    def test_every_missing_tool_is_named_in_one_run(self) -> None:
        env = self.env(PATH=self.minimal_path())
        env.pop("PYTHON")
        result = self.run_script("--yes", env=env)
        self.assert_stopped_without_writing(result)
        self.assertIn("Missing required command: git", result.stderr)
        self.assertIn("Python 3.10 or newer is required", result.stderr)

    def test_choose_python_is_the_bootstrap_rule(self) -> None:
        install = SCRIPT.read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
        for name in ("choose_python", "print_python_install_help"):
            self.assertEqual(_function(bootstrap, name), _function(install, name), name)

    def test_the_script_parses_and_reads_questions_only_from_dev_tty(self) -> None:
        subprocess.run([BASH, "-n", str(SCRIPT)], check=True, timeout=30)
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual('main "$@"', text.rstrip().splitlines()[-1])
        for line in text.splitlines():
            if re.search(r"\bread -r\b", line) and not line.lstrip().startswith("while "):
                self.assertIn("</dev/tty", line, line)


class CheckoutTests(_InstallScriptCase):
    def test_default_checkout_and_command_file(self) -> None:
        """A-33 (install part): the command file is builder_command.command_text for the checkout."""
        result = self.quick()
        self.assert_exit(0, result)
        self.assertTrue((self.checkout / ".git").exists())
        self.assertTrue((self.checkout / "tools" / "vault_migration" / "cli.py").is_file())
        self.assertEqual(self.expected_command(), self.command.read_bytes())
        self.assertTrue(os.access(self.command, os.X_OK))
        self.assertIn("(created)", result.stdout)
        self.assertIn("setup: dry run, the precheck passed", result.stdout)

    def test_vault_builder_home_and_ref(self) -> None:
        chosen = self.tmp / "my-builder"
        result = self.quick(env=self.env(VAULT_BUILDER_HOME=str(chosen), VAULT_BUILDER_REF="other"))
        self.assert_exit(0, result)
        self.assertTrue((chosen / "OTHER-REF").is_file())
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.expected_command(chosen), self.command.read_bytes())

    def test_a_folder_that_is_not_a_git_checkout_stops_with_exit_1(self) -> None:
        self.checkout.mkdir(parents=True)
        (self.checkout / "notes.txt").write_text("mine\n", encoding="utf-8")
        result = self.quick()
        self.assert_exit(1, result)
        self.assertIn(f"{self.checkout} exists and is not a vault-builder git checkout", result.stderr)
        self.assertIn("VAULT_BUILDER_HOME", result.stderr)
        self.assertEqual(["notes.txt"], [path.name for path in self.checkout.iterdir()])
        self.assertFalse(self.command.exists())

    def test_a_failed_pull_warns_and_continues_with_the_checkout(self) -> None:
        self.assert_exit(0, self.quick())
        _git("-C", str(self.checkout), "remote", "set-url", "origin", str(self.tmp / "gone"))
        result = self.quick()
        self.assert_exit(0, result)
        warnings = [line for line in result.stderr.splitlines() if "git pull --ff-only failed" in line]
        self.assertEqual(1, len(warnings), result.stderr)
        self.assertIn(str(self.checkout), warnings[0])
        self.assertIn("setup: dry run, the precheck passed", result.stdout)

    def _old_checkout(self) -> Path:
        """A checkout made before builder_command.py existed, whose origin has the newer commit to pull."""
        git_env = self._git_env(self.home)
        upstream = self.tmp / "upstream"
        _git("clone", "-q", str(self.source), str(upstream), env=git_env)
        _git("rm", "-q", "tools/vault_migration/builder_command.py", cwd=upstream, env=git_env)
        _git("commit", "-q", "-m", "old", cwd=upstream, env=git_env)
        self.checkout.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "-q", str(upstream), str(self.checkout), env=git_env)
        _git("revert", "--no-edit", "HEAD", cwd=upstream, env=git_env)
        return upstream

    def test_an_old_checkout_without_builder_command_is_updated_and_used(self) -> None:
        """spec 4.9: an existing checkout from before this change is pulled, then used."""
        self._old_checkout()
        result = self.quick()
        self.assert_exit(0, result)
        self.assertTrue((self.checkout / "tools" / "vault_migration" / "builder_command.py").is_file())
        self.assertEqual(self.expected_command(), self.command.read_bytes())

    def test_an_old_checkout_that_cannot_be_updated_stops_with_exit_1(self) -> None:
        self._old_checkout()
        _git("-C", str(self.checkout), "remote", "set-url", "origin", str(self.tmp / "gone"))
        result = self.quick()
        self.assert_exit(1, result)
        self.assertIn("is older than this install command", result.stderr)
        self.assertIn("git -C", result.stderr)
        self.assertFalse(self.command.exists())

    def test_a_rerun_with_the_same_content_leaves_the_file_alone(self) -> None:
        self.assert_exit(0, self.quick())
        before = self.command.stat()
        result = self.quick()
        self.assert_exit(0, result)
        after = self.command.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertIn("(unchanged)", result.stdout)

    def test_an_older_marked_file_is_replaced(self) -> None:
        self.command.parent.mkdir(parents=True)
        old = builder_command.command_text(self.tmp / "old-checkout", "/usr/bin/python3")
        _write(self.command, old)
        result = self.quick()
        self.assert_exit(0, result)
        self.assertEqual(self.expected_command(), self.command.read_bytes())
        self.assertIn("(replaced)", result.stdout)

    def test_a_file_without_the_marker_is_kept_and_stops_with_exit_1(self) -> None:
        """A-33: an unmarked vault-builder file is the user's."""
        self.command.parent.mkdir(parents=True)
        _write(self.command, "#!/bin/sh\necho mine\n")
        before = self.command.read_bytes()
        result = self.quick()
        self.assert_exit(1, result)
        self.assertEqual(before, self.command.read_bytes())
        self.assertIn("was not written by vault-builder", result.stderr)
        self.assertNotIn("setup:", result.stdout)


class PathHintTests(_InstallScriptCase):
    def test_the_last_line_is_the_export_line_when_local_bin_is_not_on_path(self) -> None:
        result = self.quick()
        self.assert_exit(0, result)
        lines = result.stdout.rstrip().splitlines()
        self.assertEqual(EXPORT_LINE, lines[-1])
        self.assertIn(str(self.home / ".bash"), lines[-2])

    def test_no_hint_when_local_bin_is_on_path(self) -> None:
        env = self.env()
        env["PATH"] = os.pathsep.join([env["PATH"], str(self.home / ".local" / "bin")])
        result = self.quick(env=env)
        self.assert_exit(0, result)
        self.assertNotIn("export PATH", result.stdout)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_the_readme_pipe_from_zsh(self) -> None:
        """A-27: a zsh user runs the README line; the hint names ~/.zshrc."""
        zsh = shutil.which("zsh")
        assert zsh is not None
        command = f"cat {SCRIPT} | {BASH} -s -- --yes --dry-run"
        result = subprocess.run(
            [zsh, "-f", "-c", command],
            env=self.env(SHELL=zsh),
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            start_new_session=True,
            check=False,
        )
        self.assert_exit(0, result)
        lines = result.stdout.rstrip().splitlines()
        self.assertEqual(EXPORT_LINE, lines[-1])
        self.assertIn(str(self.home / ".zshrc"), lines[-2])


class ClaudeRunningTests(_InstallScriptCase):
    def test_install_exits_3_and_leaves_claude_json_alone(self) -> None:
        """A-19 (shell path)."""
        self.claude_flag.touch()
        self.claude_json.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        before = self.claude_json.read_bytes()
        result = self.run_script("--repo", str(self.app), "--yes")
        self.assert_exit(3, result)
        self.assertEqual(before, self.claude_json.read_bytes())
        self.assertFalse(self.vault_map.exists())

    def test_uninstall_exits_3_before_removing_anything(self) -> None:
        self.assert_exit(0, self.quick())
        self.claude_flag.touch()
        before = _snapshot(self.home)
        result = self.run_script("--uninstall")
        self.assert_exit(3, result)
        self.assertEqual(before, _snapshot(self.home))
        self.assertIn("Claude Code is running", result.stderr)


class UninstallStopTests(_InstallScriptCase):
    def test_a_failed_uninstall_all_keeps_the_command_file(self) -> None:
        self.assert_exit(0, self.quick())
        self.vault_map.parent.mkdir(parents=True)
        self.vault_map.write_text("not json\n", encoding="utf-8")
        result = self.run_script("--uninstall")
        self.assert_exit(1, result)
        self.assertIn("stopped before deleting the command file and the checkout", result.stderr)
        self.assertEqual(self.expected_command(), self.command.read_bytes())

    def test_uninstall_rejects_setup_options(self) -> None:
        result = self.run_script("--uninstall", "--repo", str(self.app))
        self.assert_exit(2, result)


class FullFlowTests(_InstallScriptCase):
    def install(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """A-7: the README line in non-interactive mode, with no /dev/tty."""
        result = self.run_script("--repo", str(self.app), "--yes", pipe=True, env=env)
        self.assert_exit(0, result)
        return result

    @full_tier_only
    def test_one_line_installs_everything(self) -> None:
        """A-1 and A-7: A-2 to A-6 hold after one run of the install command. No other tool is installed."""
        result = self.install()
        # A-2
        data = self.read_json(self.vault_map)
        roles = {name: entry["role"] for name, entry in data["vaults"].items()}
        self.assertEqual({"personal": "personal", "app": "project"}, roles)
        project = data["projects"]["app"]
        self.assertEqual(["personal", "app"], project["vaults"])
        self.assertEqual("app", project["default_write_vault"])
        # A-3
        servers = self.read_json(self.claude_json)["mcpServers"]
        self.assertEqual(["vault-wiki"], list(servers))
        # A-4
        hooks = self.read_json(self.settings)["hooks"]
        for event in EVENTS:
            self.assertEqual(1, len(hooks[event]), event)
            self.assertIn("vault_hooks.py", hooks[event][0]["hooks"][0]["command"])
        self.assertFalse(self.hooks_config.exists())
        self.assertEqual([], self.calls())
        for text in ("uv tool", "vg setup", "vault-graph:"):
            self.assertNotIn(text, result.stdout + result.stderr)
        # A-6
        doctor = subprocess.run(
            [str(self.command), "doctor"],
            env=self.env(),
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            start_new_session=True,
            check=False,
        )
        self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)

    @full_tier_only
    def test_the_hooks_record_a_vg_the_caller_names(self) -> None:
        """A-5: a caller that installed vg passes it in VAULT_GRAPH_BIN. The hooks run it without PATH."""
        vg = self.fake_vg
        self.install(env=self.env(VAULT_GRAPH_BIN=str(vg)))
        self.assertEqual(str(vg), self.read_json(self.hooks_config)["vg"])
        launcher = self.home / ".claude" / "vault-builder" / "vault_hooks.py"
        session = subprocess.run(
            [sys.executable, "-I", str(launcher), "session-start"],
            input=json.dumps({"cwd": str(self.app), "hook_event_name": "SessionStart"}),
            env=self.env(PATH="/usr/bin:/bin"),
            cwd=self.app,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, session.returncode, session.stderr)
        self.assertIn("VG-HOOK session-start", session.stdout)
        doctor = subprocess.run(
            [str(self.command), "doctor"],
            env=self.env(),
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            start_new_session=True,
            check=False,
        )
        self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)

    @full_tier_only
    def test_uninstall_removes_everything_but_the_vaults(self) -> None:
        """A-23 and A-33 (removal part)."""
        self.install()
        result = self.run_script("--uninstall", pipe=True)
        self.assert_exit(0, result)
        servers = self.read_json(self.claude_json).get("mcpServers", {})
        self.assertNotIn("vault-wiki", servers)
        if self.settings.exists():
            self.assertNotIn("vault_hooks.py", self.settings.read_text(encoding="utf-8"))
        self.assertFalse(self.vault_map.exists())
        self.assertFalse(self.command.exists())
        self.assertFalse(self.checkout.exists())
        for vault in (self.home / "vault", self.home / "vaults" / "app"):
            self.assertTrue((vault / "raw").is_dir(), vault)
            self.assertTrue((vault / "wiki").is_dir(), vault)
            self.assertIn(f"vault: {vault}", result.stdout)


if __name__ == "__main__":
    unittest.main()
