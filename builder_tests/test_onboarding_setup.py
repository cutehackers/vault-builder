"""vault-builder setup (spec 4.3, 4.4, 4.6 and the setup rows of 6).

Every test runs with a temporary HOME and without CLAUDE_CONFIG_DIR, so the real ~/.claude and ~/.claude.json
are never read or written. The Claude Code process check and /dev/tty are fakes (FakeExternals). setup installs
no other tool: a test names a fake vg through VAULT_GRAPH_BIN only to check that the hooks record it.
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
    """The external checks setup makes. events records their order."""

    def __init__(self, case: "_SetupCase") -> None:
        self.case = case
        self.events: list[str] = []
        self.claude: list[bool] = []
        self.tty: FakeTty | None = None

    def claude_running(self, env: Mapping[str, str]) -> bool:
        self.events.append("claude-check")
        return self.claude.pop(0) if self.claude else False

    def open_tty(self) -> FakeTty | None:
        return self.tty

    def externals(self) -> onboarding.Externals:
        return onboarding.Externals(claude_running=self.claude_running, open_tty=self.open_tty)


class _SetupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.vg = self.tmp / "vg-bin" / "vg"
        self.vg.parent.mkdir()
        self.vg.write_text(FAKE_VG, encoding="utf-8")
        self.vg.chmod(0o755)
        self.config = self.home / ".claude"
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        for key in ("CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN"):
            os.environ.pop(key, None)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())
        self.fake = FakeExternals(self)

    def env(self, **extra: str) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN"}
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
        """A-2, A-3, A-4 and the step order: precheck, install with hooks, link, doctor. No other tool."""
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
            ["claude-check", "install:personal", "install:app", "link:personal,app", "doctor:None"], events
        )
        for call in installs:
            self.assertTrue(call["with_hooks"])
            self.assertNotIn("VAULT_GRAPH_BIN", call["environ"])

        # A-2
        vaults = self.vaults()
        self.assertEqual({"personal": "personal", "app": "project"}, {k: v["role"] for k, v in vaults.items()})
        self.assertEqual(str(self.home / "vault"), vaults["personal"]["path"])
        self.assertEqual(str(self.home / "vaults" / "app"), vaults["app"]["path"])
        self.assertEqual(["personal", "app"], self.project()["vaults"])
        self.assertEqual("app", self.project()["default_write_vault"])
        # A-3
        self.assertEqual(["vault-wiki"], list(self.read_json(self.claude_json)["mcpServers"]))
        # A-4
        hooks = self.read_json(self.settings)["hooks"]
        for event in EVENTS:
            self.assertEqual(1, len(hooks[event]), event)
            self.assertIn("vault_hooks.py", hooks[event][0]["hooks"][0]["command"])
        self.assertFalse(self.hooks_config.exists())
        self.assertIn("recall.sh", output)
        for text in ("uv tool", "vg setup", "vault-graph:"):
            self.assertNotIn(text, output)
        # last lines
        self.assertIn("Doctor found no problems", output)
        tail = output.splitlines()[-4:]
        self.assertTrue(any("Restart Claude Code" in line for line in tail), output)
        self.assertTrue(any("/mcp" in line and "vault-wiki" in line for line in tail), output)
        self.assertTrue(any("vault-builder link --repo <path>" in line for line in tail), output)

    def test_the_hooks_record_a_vg_the_caller_names(self) -> None:
        """A-5: a caller that installed vg names it in VAULT_GRAPH_BIN. The launcher runs it without PATH."""
        output = self.assert_setup("--repo", str(self.repo), "--yes", VAULT_GRAPH_BIN=str(self.vg))
        self.assertEqual(str(self.vg), self.read_json(self.hooks_config)["vg"])
        self.assertIn("Doctor found no problems", output)
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
        files = (self.vault_map, self.claude_json, self.settings)
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
        before = {path: path.read_bytes() for path in (self.claude_json, self.settings)}
        team = self.tmp / "team-vault"
        self.assert_setup("--vault", f"team={team}", "--role", "team=team", "--repo", str(self.repo), "--yes")
        self.assertEqual("team", self.vaults()["team"]["role"])
        self.assertEqual(["personal", "app", "team"], self.project()["vaults"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})

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
        return output

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


class UpgradeTests(_SetupCase):
    """An existing vault that install would refuse is upgraded by setup, unless a person must decide a file."""

    def setUp(self) -> None:
        super().setUp()
        self.assert_setup("--repo", str(self.repo), "--yes")
        self.personal = self.home / "vault"
        self.app = self.home / "vaults" / "app"

    def test_a_vault_with_a_missing_managed_file_is_upgraded_and_installed(self) -> None:
        readme = self.personal / "README.md"
        expected = readme.read_bytes()
        readme.unlink()

        output = self.assert_setup("--repo", str(self.repo), "--yes")

        self.assertIn("upgrade vault personal", output)
        self.assertIn("1 files to create, 0 to replace", output)
        self.assertEqual(expected, readme.read_bytes())
        self.assertNotIn("upgrade vault app", output)
        self.assertIn("Doctor found no problems", output)

    def test_dry_run_shows_the_upgrade_and_writes_nothing(self) -> None:
        (self.personal / "README.md").unlink()
        before = _snapshot(self.home)

        output = self.assert_setup("--repo", str(self.repo), "--dry-run")

        self.assertIn("upgrade vault personal", output)
        self.assertEqual(before, _snapshot(self.home))

    def test_a_file_a_person_edited_stops_before_any_write_and_lists_every_vault(self) -> None:
        for vault in (self.personal, self.app):
            with (vault / "README.md").open("a", encoding="utf-8") as stream:
                stream.write("\nmy own note\n")
        (self.personal / "AGENTS.md").unlink()
        before = _snapshot(self.home)

        output = self.assert_setup("--repo", str(self.repo), "--yes", code=1)

        self.assertIn("Nothing was written", output)
        self.assertIn("then delete it from the vault. setup puts back the vault-builder version", output)
        self.assertIn("audit failed: Vault topology is missing required file: AGENTS.md", output)
        self.assertIn(f"git -C {self.personal} checkout -- <path>", output)
        self.assertIn("review-required README.md", output)
        self.assertIn(f"vault personal ({self.personal})", output)
        self.assertIn(f"vault app ({self.app})", output)
        self.assertIn(f"vault-builder audit --target {self.personal}", output)
        self.assertEqual(before, _snapshot(self.home))

    def test_an_install_problem_of_a_vault_to_upgrade_stops_before_the_upgrade(self) -> None:
        (self.personal / "README.md").unlink()
        self.settings.write_text("{not json", encoding="utf-8")
        before = _snapshot(self.home)

        result, output = self.setup("--repo", str(self.repo), "--yes")

        self.assertEqual(1, result, output)
        self.assertEqual(before, _snapshot(self.home))

    def test_a_failed_upgrade_stops_and_names_the_vault(self) -> None:
        (self.personal / "README.md").unlink()

        with mock.patch.object(onboarding, "apply_plan", side_effect=RuntimeError("gate failed")):
            result, output = self.setup("--repo", str(self.repo), "--yes")

        self.assertEqual(1, result, output)
        self.assertIn("upgrade of vault personal", output)
        self.assertIn("gate failed", output)
        self.assertIn("run the same command again", output)


class InteractiveTests(_SetupCase):
    def test_enter_on_every_question_gives_the_baseline_result(self) -> None:
        """Questions 1 to 5 and their defaults (spec 4.4); the Claude Code check waits for Enter."""
        self.fake.claude = [True, False]
        # personal path, repo, project vault, more vaults, proceed, wait for Claude Code
        self.fake.tty = FakeTty([""] * 6)
        self.assert_setup(cwd=self.repo)
        text = self.fake.tty.text
        self.assertIn(f"Personal vault path [{self.home / 'vault'}]", text)
        self.assertIn(f"Repository to link [{self.repo}]", text)
        self.assertIn(f"Create project vault app at {self.home / 'vaults' / 'app'}? [Y/n]", text)
        self.assertIn("Add another vault? [y/N]", text)
        self.assertNotIn("vault-graph", text)
        self.assertIn("Proceed? [Y/n]", text)
        self.assertIn("Quit every Claude Code", text)
        self.assertEqual({"personal": "personal", "app": "project"}, {k: v["role"] for k, v in self.vaults().items()})
        self.assertEqual("app", self.project()["default_write_vault"])

    def test_registered_vaults_are_listed_instead_of_question_1(self) -> None:
        self.assert_setup("--vault", f"mine={self.tmp / 'mine'}", "--yes")
        self.fake.tty = FakeTty(["", ""])  # repo (skip), more vaults
        self.assert_setup("--dry-run", cwd=self.tmp)
        self.assertNotIn("Personal vault path", self.fake.tty.text)
        self.assertIn("Registered vaults:", self.fake.tty.text)
        self.assertIn("mine", self.fake.tty.text)

    def test_answer_no_stops_without_writing(self) -> None:
        self.fake.tty = FakeTty(["", "", "", "", "n"])  # personal, repo, project vault, more vaults, proceed
        before = _snapshot(self.home)
        result, _output = self.setup(cwd=self.repo)
        self.assertEqual(1, result)
        self.assertEqual(before, _snapshot(self.home))

    def test_an_added_role_shows_in_the_role_question_and_the_role_help(self) -> None:
        """A-13: one line in roles.py is enough."""
        added = roles.Role("client", "one client's shared notes", per_repo=False)
        with mock.patch.object(roles, "ROLES", (*roles.ROLES, added)):
            self.fake.tty = FakeTty(["", "", "y", str(self.tmp / "c"), "c", "client", ""])
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
                "--yes", "--dry-run",
            ]
        )
        options = onboarding.options_from_args(parsed)
        self.assertEqual(("a=/x", "b=/y"), options.vaults)
        self.assertEqual(("a=team", "b=client"), options.roles)
        self.assertEqual(("/r1", "/r2"), options.repos)
        self.assertEqual("p=/p", options.project_vault)
        self.assertEqual("a", options.default_write)
        self.assertTrue(options.yes and options.dry_run)

    def test_install_keeps_hooks_off_by_default(self) -> None:
        """A-26."""
        self.assertFalse(cli._build_parser().parse_args(["install", "/x"]).with_hooks)

    def test_role_help_lists_every_known_role(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["setup", "--help"]))
        for name in roles.role_names():
            self.assertIn(name, out.getvalue())


class UpgradeResolutionTests(_SetupCase):
    def test_deleting_an_edited_file_lets_the_next_run_upgrade(self) -> None:
        self.assert_setup("--repo", str(self.repo), "--yes")
        readme = self.home / "vault" / "README.md"
        expected = readme.read_bytes()
        readme.write_text("my own readme\n", encoding="utf-8")
        self.assert_setup("--repo", str(self.repo), "--yes", code=1)

        readme.unlink()
        output = self.assert_setup("--repo", str(self.repo), "--yes")

        self.assertEqual(expected, readme.read_bytes())
        self.assertIn("Doctor found no problems", output)

    def test_a_wiki_page_that_breaks_the_source_rules_is_not_deleted_but_fixed(self) -> None:
        self.assert_setup("--repo", str(self.repo), "--yes")
        page = self.home / "vault" / "wiki" / "concepts" / "x.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text('---\ntitle: "X"\ntype: source\nstatus: seed\n---\n# X\n\nbody\n', encoding="utf-8")
        before = _snapshot(self.home)

        output = self.assert_setup("--repo", str(self.repo), "--yes", code=1)

        self.assertIn("review-required wiki/concepts/x.md", output)
        self.assertIn("A wiki page breaks the source page rules", output)
        self.assertIn("tools/wiki/cli.py lint", output)
        self.assertNotIn("delete it from the vault", output)
        self.assertEqual(before, _snapshot(self.home))

    def test_a_manifest_from_a_newer_builder_asks_for_an_update(self) -> None:
        self.assert_setup("--repo", str(self.repo), "--yes")
        manifest_path = self.home / "vault" / ".vault-builder" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["contract_version"] = 99
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        result, output = self.setup("--repo", str(self.repo), "--yes")

        self.assertEqual(1, result, output)
        self.assertIn("a newer vault-builder (contract 99) manages this vault", output)
        self.assertNotIn("delete it from the vault", output)


if __name__ == "__main__":
    unittest.main()
