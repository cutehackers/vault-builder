"""vault-builder setup (spec 4.3, 4.4, 4.6 and the setup rows of 6).

Every test runs with a temporary HOME and without CLAUDE_CONFIG_DIR, so the real ~/.claude and ~/.claude.json
are never read or written. uv, vg, git ls-remote, the Claude Code process check and /dev/tty are fakes
(FakeExternals): no test runs the real uv or vg or reaches the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from tools.vault_migration import cli, installing, linking, onboarding, roles, uninstalling

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ("SessionStart", "PreCompact", "SessionEnd")
FAKE_VG = """#!/bin/sh
case "$1" in
  --help) exit 0 ;;
  hook) cat >/dev/null; echo "VG-HOOK $2"; exit 0 ;;
esac
exit 2
"""


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[str(path.relative_to(root)) + "/"] = "dir"
    return result


class FakeTty:
    """Answers questions from a list. Everything written to it is kept in text."""

    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = list(answers)
        self.text = ""

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        if not self.answers:
            return ""
        return self.answers.pop(0) + "\n"


class FakeExternals:
    """The external tools setup calls. events records the order of every external call."""

    def __init__(self, case: "_SetupCase") -> None:
        self.case = case
        self.events: list[str] = []
        self.calls: list[list[str]] = []
        self.ls_remote_code = 0
        self.uv_install_code = 0
        self.claude: list[bool] = []
        self.tty: FakeTty | None = None
        self.vg_setup_code = 0

    def run(
        self, argv: Sequence[str], env: Mapping[str, str], *, capture: bool, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            self.events.append("ls-remote")
            return subprocess.CompletedProcess(argv, self.ls_remote_code, "", "Permission denied (publickey).\n")
        if argv[:3] == ["uv", "tool", "install"]:
            self.events.append("uv-install")
            if self.uv_install_code == 0:
                self.case.uv_bin.mkdir(exist_ok=True)
                vg = self.case.uv_bin / "vg"
                vg.write_text(FAKE_VG, encoding="utf-8")
                vg.chmod(0o755)
                return subprocess.CompletedProcess(argv, 0, "Installed 1 executable: vg\n", "")
            return subprocess.CompletedProcess(argv, self.uv_install_code, "", "error: git clone failed\n")
        if argv[:4] == ["uv", "tool", "dir", "--bin"]:
            self.events.append("uv-dir")
            return subprocess.CompletedProcess(argv, 0, f"{self.case.uv_bin}\n", "")
        if argv[1:] == ["setup", "--agent", "claude", "--mcp"]:
            self.events.append("vg-setup")
            self.case.register_vault_graph(argv[0])
            return subprocess.CompletedProcess(argv, self.vg_setup_code, "", "")
        raise AssertionError(f"unexpected external command: {argv}")

    def claude_running(self, env: Mapping[str, str]) -> bool:
        self.events.append("claude-check")
        return self.claude.pop(0) if self.claude else False

    def open_tty(self) -> FakeTty | None:
        return self.tty

    def externals(self) -> onboarding.Externals:
        return onboarding.Externals(run=self.run, claude_running=self.claude_running, open_tty=self.open_tty)


class _SetupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.uv_bin = self.tmp / "uv-bin"
        self.config = self.home / ".claude"
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        for key in ("CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN", "VAULT_GRAPH_REPO"):
            os.environ.pop(key, None)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())
        self.fake = FakeExternals(self)

    def env(self, **extra: str) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN", "VAULT_GRAPH_REPO"}
        }
        environment["HOME"] = str(self.home)
        environment["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        environment.update(extra)
        return environment

    @property
    def vault_map(self) -> Path:
        return self.config / "vault-builder" / "vault-map.json"

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def hooks_config(self) -> Path:
        return self.config / "vault-builder" / "vault-hooks.json"

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def register_vault_graph(self, vg: str) -> None:
        """What `vg setup --agent claude --mcp` does to $HOME/.claude.json: write mcpServers.vault-graph."""
        data = self.read_json(self.claude_json) if self.claude_json.exists() else {}
        wanted = {"command": vg, "args": ["mcp", "serve"]}
        if data.get("mcpServers", {}).get("vault-graph") == wanted:
            return
        data.setdefault("mcpServers", {})["vault-graph"] = wanted
        self.claude_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def setup(self, *arguments: str, cwd: Path | None = None, **extra: str) -> tuple[int, str]:
        parsed = cli._build_parser().parse_args(["setup", *arguments])
        options = onboarding.options_from_args(parsed)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            code = onboarding.setup(
                options, environ=self.env(**extra), externals=self.fake.externals(), cwd=cwd or self.tmp
            )
        return code, stdout.getvalue()

    def assert_setup(self, *arguments: str, code: int = 0, **extra: str) -> str:
        result, output = self.setup(*arguments, **extra)
        self.assertEqual(code, result, output)
        return output

    def project(self, project_id: str = "app") -> dict:
        return self.read_json(self.vault_map)["projects"][project_id]

    def vaults(self) -> dict:
        return self.read_json(self.vault_map)["vaults"]


class FullSetupTests(_SetupCase):
    def test_one_run_installs_links_registers_and_checks(self) -> None:
        """A-2, A-3, A-4 and the step order: precheck, vg, install with hooks, link, vg setup, doctor."""
        events = self.fake.events
        real_install, real_link, real_doctor = installing.install, linking.link, uninstalling.doctor
        installs: list[dict] = []

        def install(target: Path, **kwargs: object) -> int:
            if not kwargs.get("dry_run"):
                events.append(f"install:{kwargs['name']}")
                installs.append({"target": target, **kwargs})
            return real_install(target, **kwargs)  # type: ignore[arg-type]

        def link(names: Sequence[str], **kwargs: object) -> int:
            events.append("link:" + ",".join(names))
            return real_link(names, **kwargs)  # type: ignore[arg-type]

        def doctor(target: Path | None = None, **kwargs: object) -> int:
            events.append(f"doctor:{target}")
            return real_doctor(target, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(onboarding.installing, "install", side_effect=install),
            mock.patch.object(onboarding.linking, "link", side_effect=link),
            mock.patch.object(onboarding.uninstalling, "doctor", side_effect=doctor),
        ):
            output = self.assert_setup("--repo", str(self.repo), "--yes")

        self.assertEqual(
            [
                "ls-remote",
                "claude-check",
                "uv-install",
                "uv-dir",
                "install:personal",
                "install:app",
                "link:personal,app",
                "vg-setup",
                "doctor:None",
            ],
            events,
        )
        vg = self.uv_bin / "vg"
        for call in installs:
            self.assertTrue(call["with_hooks"])
            self.assertEqual(str(vg), call["environ"]["VAULT_GRAPH_BIN"])
        self.assertIn(
            ["uv", "tool", "install", "--force", "git+https://github.com/cutehackers/vault-graph.git"], self.fake.calls
        )
        self.assertIn([str(vg), "setup", "--agent", "claude", "--mcp"], self.fake.calls)

        # A-2
        vaults = self.vaults()
        self.assertEqual({"personal": "personal", "app": "project"}, {k: v["role"] for k, v in vaults.items()})
        self.assertEqual(str(self.home / "vault"), vaults["personal"]["path"])
        self.assertEqual(str(self.home / "vaults" / "app"), vaults["app"]["path"])
        self.assertEqual(["personal", "app"], self.project()["vaults"])
        self.assertEqual("app", self.project()["default_write_vault"])
        # A-3
        servers = self.read_json(self.claude_json)["mcpServers"]
        self.assertIn("vault-wiki", servers)
        self.assertEqual(str(vg), servers["vault-graph"]["command"])
        # A-4
        hooks = self.read_json(self.settings)["hooks"]
        for event in EVENTS:
            self.assertEqual(1, len(hooks[event]), event)
            self.assertIn("vault_hooks.py", hooks[event][0]["hooks"][0]["command"])
        self.assertEqual(str(vg), self.read_json(self.hooks_config)["vg"])
        # last lines
        self.assertIn("Doctor found no problems", output)
        tail = output.splitlines()[-4:]
        self.assertTrue(any("Restart Claude Code" in line for line in tail), output)
        self.assertTrue(any("/mcp" in line and "vault-wiki" in line and "vault-graph" in line for line in tail), output)
        self.assertTrue(any("vault-builder link --repo <path>" in line for line in tail), output)

    def test_the_session_start_hook_runs_vg_without_the_uv_folder_on_path(self) -> None:
        """A-5: the recorded vg path makes the launcher run vg hook session-start."""
        self.assert_setup("--repo", str(self.repo), "--yes")
        launcher = self.config / "vault-builder" / "vault_hooks.py"
        env = self.env()
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run(
            [sys.executable, "-I", str(launcher), "session-start"],
            input=json.dumps({"cwd": str(self.repo), "hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            env=env,
            cwd=self.repo,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("VG-HOOK session-start", result.stdout)

    def test_a_second_run_changes_no_byte(self) -> None:
        """A-21."""
        self.assert_setup("--repo", str(self.repo), "--yes")
        files = (self.vault_map, self.claude_json, self.settings, self.hooks_config)
        before = {path: path.read_bytes() for path in files}
        output = self.assert_setup("--repo", str(self.repo), "--yes")
        self.assertEqual(before, {path: path.read_bytes() for path in files})
        self.assertIn("link config unchanged", output)

    def test_team_and_an_unknown_role_in_the_first_run(self) -> None:
        """A-11, A-12: one run installs and links personal, project, team and a client vault."""
        team, client = self.tmp / "team-vault", self.tmp / "client-vault"
        output = self.assert_setup(
            "--repo", str(self.repo),
            "--vault", f"team={team}", "--role", "team=team",
            "--vault", f"acme={client}", "--role", "acme=client",
            "--yes",
        )
        roles_by_name = {name: entry["role"] for name, entry in self.vaults().items()}
        self.assertEqual({"personal": "personal", "team": "team", "acme": "client", "app": "project"}, roles_by_name)
        self.assertEqual({"personal", "team", "acme", "app"}, set(self.project()["vaults"]))
        self.assertEqual("app", self.project()["default_write_vault"])
        self.assertIn("acme has role client, which is not a known role", output)

    def test_a_next_vault_is_added_with_the_same_command(self) -> None:
        """A-22: a later run adds a team vault, links it, and leaves the hook and MCP entries alone."""
        self.assert_setup("--repo", str(self.repo), "--yes")
        before = {path: path.read_bytes() for path in (self.claude_json, self.settings, self.hooks_config)}
        team = self.tmp / "team-vault"
        self.assert_setup("--vault", f"team={team}", "--role", "team=team", "--repo", str(self.repo), "--yes")
        self.assertEqual("team", self.vaults()["team"]["role"])
        self.assertEqual(["personal", "app", "team"], self.project()["vaults"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertIn(["git", "ls-remote", "https://github.com/cutehackers/vault-graph.git", "HEAD"], self.fake.calls)

    def test_a_failed_vg_setup_still_runs_doctor_and_exits_1(self) -> None:
        self.fake.vg_setup_code = 1
        result, output = self.setup("--repo", str(self.repo), "--yes")
        self.assertEqual(1, result, output)
        self.assertIn("Doctor found", output)
        self.assertIn("setup --vault-id <id>", output)

    def test_a_role_option_keeps_or_sets_roles(self) -> None:
        """No --role: a new vault is personal and a registered vault keeps its role."""
        mine = self.tmp / "mine"
        self.assert_setup("--vault", f"mine={mine}", "--yes")
        self.assertEqual({"mine": "personal"}, {k: v["role"] for k, v in self.vaults().items()})
        subprocess.run(
            [sys.executable, str(ROOT / "tools/vault_migration/cli.py"), "set-role", "--vault", "mine", "--role", "team"],
            env=self.env(), check=True, capture_output=True,
        )
        self.assert_setup("--vault", f"mine={mine}", "--yes")
        self.assertEqual("team", self.vaults()["mine"]["role"])

    def test_vault_graph_repo_can_be_changed(self) -> None:
        self.assert_setup("--vault", f"mine={self.tmp / 'mine'}", "--yes", VAULT_GRAPH_REPO="https://example.test/vg.git")
        self.assertIn(["git", "ls-remote", "https://example.test/vg.git", "HEAD"], self.fake.calls)
        self.assertIn(["uv", "tool", "install", "--force", "git+https://example.test/vg.git"], self.fake.calls)


class DefaultWriteTests(_SetupCase):
    def link_personal_and_team(self) -> None:
        self.assert_setup("--vault", f"team={self.tmp / 'team-vault'}", "--role", "team=team", "--yes")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, linking.link(["personal", "team"], repo=str(self.repo), environ=self.env()))

    def test_without_a_project_vault_setup_prints_the_set_default_write_command(self) -> None:
        """A-14: personal and team only: no automatic default; non-interactive mode prints one hint."""
        self.link_personal_and_team()
        output = self.assert_setup("--repo", str(self.repo), "--yes")
        self.assertNotIn("default_write_vault", self.project())
        self.assertNotIn("app", self.vaults(), "a linked repository gets no new project vault")
        self.assertIn("vault-builder set-default-write --project app --vault <name>", output)

    def test_default_write_option_is_recorded(self) -> None:
        self.link_personal_and_team()
        self.assert_setup("--repo", str(self.repo), "--default-write", "team", "--yes")
        self.assertEqual("team", self.project()["default_write_vault"])

    def test_interactive_mode_asks_for_the_default_write_vault(self) -> None:
        self.link_personal_and_team()
        self.fake.tty = FakeTty(["", "team"])  # proceed, then the default write vault
        self.assert_setup("--repo", str(self.repo))
        self.assertIn("Default write vault for project app", self.fake.tty.text)
        self.assertEqual("team", self.project()["default_write_vault"])

    def test_an_unknown_default_write_vault_stops_before_writing(self) -> None:
        before = _snapshot(self.home)
        output = self.assert_setup("--repo", str(self.repo), "--default-write", "nope", "--yes", code=2)
        self.assertIn("--default-write", output)
        self.assertEqual(before, _snapshot(self.home))


class PrecheckTests(_SetupCase):
    def assert_nothing_written(self, *arguments: str, code: int = 1) -> str:
        before = _snapshot(self.home)
        result, output = self.setup(*arguments)
        self.assertEqual(code, result, output)
        self.assertEqual(before, _snapshot(self.home))
        self.assertNotIn("uv-install", self.fake.events)
        return output

    def test_no_vault_graph_access_writes_nothing(self) -> None:
        """A-18 setup part."""
        self.fake.ls_remote_code = 128
        output = self.assert_nothing_written("--repo", str(self.repo), "--yes")
        self.assertIn("cannot reach the vault-graph repository", output)
        self.assertIn("https://github.com/cutehackers/vault-graph", output)
        self.assertNotIn("ssh -T", output)
        self.assertIn("--without-vault-graph", output)
        self.assertFalse((self.home / "vault").exists())

    def test_claude_code_running_exits_3_without_writing(self) -> None:
        """A-19."""
        self.claude_json.write_text('{"numStartups": 3}\n', encoding="utf-8")
        self.fake.claude = [True]
        output = self.assert_nothing_written("--repo", str(self.repo), "--yes", code=3)
        self.assertIn("Quit every Claude Code", output)
        self.assertEqual('{"numStartups": 3}\n', self.claude_json.read_text(encoding="utf-8"))

    def test_a_vault_path_with_other_content_stops(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        (other / "notes.txt").write_text("mine", encoding="utf-8")
        output = self.assert_nothing_written("--vault", f"x={other}", "--yes")
        self.assertIn("Target is not empty", output)

    def test_a_repo_that_is_not_git_stops(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        output = self.assert_nothing_written("--repo", str(plain), "--yes")
        self.assertIn("is not inside a git repository", output)

    def test_settings_that_is_not_json_stops(self) -> None:
        self.config.mkdir()
        self.settings.write_text("{", encoding="utf-8")
        output = self.assert_nothing_written("--repo", str(self.repo), "--yes")
        self.assertIn("is not valid JSON", output)

    def test_an_empty_role_stops(self) -> None:
        output = self.assert_nothing_written("--vault", f"x={self.tmp / 'x'}", "--role", "x=", "--yes")
        self.assertIn("--role must be a non-empty string", output)

    def test_a_duplicate_vault_name_stops(self) -> None:
        output = self.assert_nothing_written(
            "--vault", f"x={self.tmp / 'x1'}", "--vault", f"x={self.tmp / 'x2'}", "--yes"
        )
        self.assertIn("--vault NAME=PATH", output)

    def test_a_bad_vault_name_stops(self) -> None:
        self.assert_nothing_written("--vault", f"b@d={self.tmp / 'x'}", "--yes")

    def test_a_malformed_option_exits_2(self) -> None:
        output = self.assert_nothing_written("--vault", "nopath", "--yes", code=2)
        self.assertIn("--vault", output)

    def test_a_role_for_an_unknown_vault_exits_2(self) -> None:
        output = self.assert_nothing_written("--role", "ghost=team", "--yes", code=2)
        self.assertIn("ghost", output)

    def test_project_vault_needs_one_repo(self) -> None:
        other = self.tmp / "other-repo"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True, env=self.env())
        output = self.assert_nothing_written(
            "--repo", str(self.repo), "--repo", str(other), "--project-vault", f"p={self.tmp / 'p'}", "--yes", code=2
        )
        self.assertIn("--project-vault", output)

    def test_without_a_tty_and_without_yes_exits_2(self) -> None:
        output = self.assert_nothing_written("--repo", str(self.repo), code=2)
        self.assertIn("--yes", output)

    def test_dry_run_writes_nothing_and_shows_the_plan(self) -> None:
        output = self.assert_nothing_written("--repo", str(self.repo), "--dry-run", code=0)
        self.assertIn(str(self.home / "vaults" / "app"), output)
        self.assertIn("dry run", output)

    def test_a_failed_uv_install_stops_before_install(self) -> None:
        self.fake.uv_install_code = 1
        result, output = self.setup("--repo", str(self.repo), "--yes")
        self.assertEqual(1, result, output)
        self.assertIn("error: git clone failed", output)
        self.assertIn("uv tool install --force git+https://github.com/cutehackers/vault-graph.git", output)
        self.assertFalse(self.vault_map.exists())


class RecoveryTests(_SetupCase):
    def test_a_failed_second_install_finishes_on_the_next_run(self) -> None:
        """A-20."""
        real_install = installing.install

        def failing(target: Path, **kwargs: object) -> int:
            if kwargs.get("name") == "app" and not kwargs.get("dry_run"):
                raise installing.InstallError("disk full")
            return real_install(target, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(onboarding.installing, "install", side_effect=failing):
            result, output = self.setup("--repo", str(self.repo), "--yes")
        self.assertEqual(1, result, output)
        self.assertIn("app", output)
        self.assertIn("disk full", output)
        self.assertIn("run the same command again", output)
        self.assertEqual({"personal"}, set(self.vaults()))

        output = self.assert_setup("--repo", str(self.repo), "--yes")
        self.assertEqual({"personal": "personal", "app": "project"}, {k: v["role"] for k, v in self.vaults().items()})
        self.assertEqual("app", self.project()["default_write_vault"])
        self.assertIn("Doctor found no problems", output)

    def test_an_existing_install_without_hooks_gets_them(self) -> None:
        """A-25: vaults, links and the default write vault stay; hooks are added; doctor passes."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for name, role in (("mine", "personal"), ("appvault", "project")):
                installing.install(
                    self.tmp / name, name=name, role=role, environ=self.env(), python=sys.executable, out=io.StringIO()
                )
            linking.link(["mine", "appvault"], repo=str(self.repo), environ=self.env())
        self.assertFalse(self.settings.exists())
        before = self.read_json(self.vault_map)

        output = self.assert_setup("--repo", str(self.repo), "--yes")
        after = self.read_json(self.vault_map)
        self.assertEqual(before["vaults"], after["vaults"])
        self.assertEqual(before["projects"], after["projects"])
        self.assertEqual("appvault", after["projects"]["app"]["default_write_vault"])
        hooks = self.read_json(self.settings)["hooks"]
        self.assertEqual(set(EVENTS), set(hooks))
        self.assertIn("Doctor found no problems", output)


class WithoutVaultGraphTests(_SetupCase):
    def test_vault_builder_only_then_add_vault_graph(self) -> None:
        """A-31, A-32."""
        output = self.assert_setup("--repo", str(self.repo), "--yes", "--without-vault-graph")
        self.assertEqual([], [call for call in self.fake.calls if call[0] in {"uv", "git"} or "setup" in call])
        self.assertFalse(self.uv_bin.exists())
        servers = self.read_json(self.claude_json)["mcpServers"]
        self.assertIn("vault-wiki", servers)
        self.assertNotIn("vault-graph", servers)
        self.assertEqual(set(EVENTS), set(self.read_json(self.settings)["hooks"]))
        self.assertFalse(self.hooks_config.exists())
        self.assertEqual(["personal", "app"], self.project()["vaults"])
        self.assertIn("recall.sh", output)
        self.assertIn("Doctor found no problems", output)
        mcp_line = [line for line in output.splitlines() if "/mcp" in line][-1]
        self.assertNotIn("vault-graph", mcp_line)

        self.assert_setup("--repo", str(self.repo), "--yes")
        vg = str(self.uv_bin / "vg")
        self.assertEqual(vg, self.read_json(self.hooks_config)["vg"])
        self.assertEqual(vg, self.read_json(self.claude_json)["mcpServers"]["vault-graph"]["command"])


class InteractiveTests(_SetupCase):
    def test_enter_on_every_question_gives_the_baseline_result(self) -> None:
        """Questions 1 to 5 and their defaults (spec 4.4); the Claude Code check waits for Enter."""
        self.fake.claude = [True, False]
        # personal path, repo, project vault, more vaults, vault-graph, proceed, wait for Claude Code
        self.fake.tty = FakeTty([""] * 7)
        self.assert_setup(cwd=self.repo)
        text = self.fake.tty.text
        self.assertIn(f"Personal vault path [{self.home / 'vault'}]", text)
        self.assertIn(f"Repository to link [{self.repo}]", text)
        self.assertIn(f"Create project vault app at {self.home / 'vaults' / 'app'}? [Y/n]", text)
        self.assertIn("Add another vault? [y/N]", text)
        self.assertIn("Also install vault-graph", text)
        self.assertIn("Proceed? [Y/n]", text)
        self.assertIn("Quit every Claude Code", text)
        self.assertEqual({"personal": "personal", "app": "project"}, {k: v["role"] for k, v in self.vaults().items()})
        self.assertEqual("app", self.project()["default_write_vault"])

    def test_registered_vaults_are_listed_instead_of_question_1(self) -> None:
        self.assert_setup("--vault", f"mine={self.tmp / 'mine'}", "--yes")
        self.fake.tty = FakeTty(["", "", "", "", ""])  # repo (skip), more vaults, vault-graph, proceed
        self.assert_setup("--dry-run", cwd=self.tmp)
        self.assertNotIn("Personal vault path", self.fake.tty.text)
        self.assertIn("Registered vaults:", self.fake.tty.text)
        self.assertIn("mine", self.fake.tty.text)

    def test_answer_no_stops_without_writing(self) -> None:
        self.fake.tty = FakeTty(["", "", "", "", "", "n"])
        before = _snapshot(self.home)
        result, _output = self.setup(cwd=self.repo)
        self.assertEqual(1, result)
        self.assertEqual(before, _snapshot(self.home))

    def test_an_added_role_shows_in_the_role_question_and_the_role_help(self) -> None:
        """A-13: one line in roles.py is enough."""
        added = roles.Role("client", "one client's shared notes", per_repo=False)
        with mock.patch.object(roles, "ROLES", (*roles.ROLES, added)):
            self.fake.tty = FakeTty(["", "", "y", str(self.tmp / "c"), "c", "client", "", "", ""])
            self.assert_setup("--dry-run", cwd=self.tmp)
            help_out = io.StringIO()
            with contextlib.redirect_stdout(help_out):
                self.assertEqual(0, cli.main(["setup", "--help"]))
        text = self.fake.tty.text
        for role in roles.ROLES:
            self.assertIn(f"{role.name}: {role.description}", text)
        self.assertIn("client: one client's shared notes", text)
        self.assertIn("client", help_out.getvalue())
        self.assertIn("one client's shared notes", " ".join(help_out.getvalue().split()))


class CliTests(unittest.TestCase):
    def test_setup_signature(self) -> None:
        parsed = cli._build_parser().parse_args(
            [
                "setup", "--vault", "a=/x", "--vault", "b=/y", "--role", "a=team", "--role", "b=client",
                "--repo", "/r1", "--repo", "/r2", "--project-vault", "p=/p", "--default-write", "a",
                "--yes", "--dry-run", "--without-vault-graph",
            ]
        )
        options = onboarding.options_from_args(parsed)
        self.assertEqual(("a=/x", "b=/y"), options.vaults)
        self.assertEqual(("a=team", "b=client"), options.roles)
        self.assertEqual(("/r1", "/r2"), options.repos)
        self.assertEqual("p=/p", options.project_vault)
        self.assertEqual("a", options.default_write)
        self.assertTrue(options.yes and options.dry_run and options.without_vault_graph)

    def test_install_keeps_hooks_off_by_default(self) -> None:
        """A-26."""
        self.assertFalse(cli._build_parser().parse_args(["install", "/x"]).with_hooks)

    def test_role_help_lists_every_known_role(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["setup", "--help"]))
        for name in roles.role_names():
            self.assertIn(name, out.getvalue())


if __name__ == "__main__":
    unittest.main()
