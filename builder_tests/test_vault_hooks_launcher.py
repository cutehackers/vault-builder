"""Tests for the hook launcher asset vault_hooks.py (vault-graph requests spec, contract 6, A-16).

Every test uses a temporary HOME, CLAUDE_CONFIG_DIR, and fake vg programs. The real $HOME is never read.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BUILDER_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = BUILDER_ROOT / "tools" / "vault_migration" / "assets" / "vault-hooks" / "vault_hooks.py"
ROUTER_SOURCE = BUILDER_ROOT / "tools" / "vault_migration" / "assets" / "llm-wiki-router" / "llm_wiki_router.py"

if str(BUILDER_ROOT) not in sys.path:
    sys.path.insert(0, str(BUILDER_ROOT))

from tools.wiki.lib import session_capture  # noqa: E402

# Hook input fields (risk 1). Source: the Claude Code hooks reference as recorded in
# tests/test_wiki_tools.py (transcript format notes, "Hook input" item), which session_capture.py was
# built against: every event sends session_id, transcript_path, cwd and hook_event_name. PreCompact adds
# trigger and custom_instructions. SessionEnd adds reason. SessionStart adds source.
HOOK_COMMON_FIELDS = ("session_id", "transcript_path", "cwd", "hook_event_name")
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def load_launcher():
    spec = importlib.util.spec_from_file_location("vault_hooks_under_test", LAUNCHER_SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


launcher = load_launcher()


def git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True)


def make_repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if origin is not None:
        git(path, "remote", "add", "origin", origin)
    return path.resolve()


def write_program(path: Path, body: str) -> Path:
    """Write an executable Python program. body runs with sys, os, json, time imported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys, os, json, time\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def make_vault(path: Path, contract: int | None = session_capture.MIN_CONTRACT_VERSION, tools: Path | None = None) -> Path:
    path.mkdir(parents=True)
    if contract is not None:
        manifest = path / ".vault-builder" / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"contract_version": contract}), encoding="utf-8")
    if tools is not None:
        (path / "tools").symlink_to(tools, target_is_directory=True)
    return path.resolve()


class LauncherCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.config = self.home / ".claude"
        (self.config / "vault-builder").mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.record = self.tmp / "record.json"
        self.repo = make_repo(self.tmp / "work" / "demo")
        self.env = {
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "PATH": os.pathsep.join([str(self.bin), "/usr/bin", "/bin"]),
        }

    def hook(self, event: str = "PreCompact", **extra: object) -> bytes:
        data = {
            "session_id": SESSION_ID,
            "transcript_path": str(self.tmp / "transcript.jsonl"),
            "cwd": str(self.repo),
            "hook_event_name": event,
        }
        data.update(extra)
        return json.dumps(data).encode("utf-8")

    def fake_vg(self, name: str = "vg", *, exit_code: int = 0, output: str = "", sleep: float = 0) -> Path:
        body = (
            f"data = sys.stdin.buffer.read()\n"
            f"json.dump({{'argv': sys.argv[1:], 'stdin': data.decode(), 'cwd': os.getcwd()}}, "
            f"open({str(self.record)!r}, 'w'))\n"
            f"time.sleep({sleep})\n"
            f"sys.stdout.write({output!r})\n"
            f"sys.exit({exit_code})"
        )
        return write_program(self.bin / name, body)

    def fake_recall(self, output: str = "RECALL-OUT\n") -> Path:
        recall = self.config / "skills" / "project-memory" / "scripts" / "recall.sh"
        recall.parent.mkdir(parents=True)
        recall.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\npwd > {self.tmp / 'recall-cwd'}\n", encoding="utf-8")
        return recall

    def write_map(self, vaults: dict[str, tuple[Path, str]], project_vaults: list[str], **project: object) -> None:
        entry = {"root": str(self.repo), "vaults": project_vaults}
        entry.update(project)
        data = {
            "version": 2,
            "vaults": {name: {"path": str(path), "role": role} for name, (path, role) in vaults.items()},
            "projects": {"demo": entry},
        }
        (self.config / "vault-builder" / "vault-map.json").write_text(json.dumps(data), encoding="utf-8")

    def run_in_process(self, argv: list[str], data: bytes) -> tuple[int, bytes, bytes]:
        out, err = io.BytesIO(), io.BytesIO()
        code = launcher.run(argv, data, self.env, out, err)
        return code, out.getvalue(), err.getvalue()

    def run_script(self, argv: list[str], data: bytes, timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-I", str(LAUNCHER_SOURCE), *argv],
            input=data,
            capture_output=True,
            env=self.env,
            cwd=self.tmp,
            timeout=timeout,
            check=False,
        )


class HookInputFieldsTests(unittest.TestCase):
    def test_session_capture_reads_the_documented_hook_fields(self) -> None:
        values = {"transcript_path": "/x/t.jsonl", "cwd": "/x", "session_id": SESSION_ID, "hook_event_name": "SessionEnd"}
        request = session_capture.read_capture_request({}, io.StringIO(json.dumps(values)))
        self.assertEqual(request.transcript_path, Path("/x/t.jsonl"))
        self.assertEqual(request.cwd, Path("/x"))
        self.assertEqual(request.session_id, SESSION_ID)
        self.assertEqual(request.trigger, "SessionEnd")
        self.assertEqual(set(values), set(HOOK_COMMON_FIELDS))


class FindVgTests(LauncherCase):
    def test_env_variable_comes_first(self) -> None:
        on_path = self.fake_vg()
        from_env = self.fake_vg("vg-env")
        recorded = self.fake_vg("vg-recorded")
        (self.config / "vault-builder" / "vault-hooks.json").write_text(json.dumps({"vg": str(recorded)}))
        self.assertEqual(launcher.find_vg({**self.env, "VAULT_GRAPH_BIN": str(from_env)}), from_env)
        self.assertEqual(launcher.find_vg(self.env), recorded)
        (self.config / "vault-builder" / "vault-hooks.json").unlink()
        self.assertEqual(launcher.find_vg(self.env), on_path)

    def test_unusable_entries_fall_through_and_none_means_missing(self) -> None:
        (self.config / "vault-builder" / "vault-hooks.json").write_text(json.dumps({"vg": "relative/vg"}))
        env = {**self.env, "VAULT_GRAPH_BIN": str(self.tmp / "missing-vg")}
        self.assertIsNone(launcher.find_vg(env))
        on_path = self.fake_vg()
        self.assertEqual(launcher.find_vg(env), on_path)

    def test_config_dir_defaults_to_home_claude(self) -> None:
        env = {"HOME": str(self.home), "PATH": ""}
        recorded = self.fake_vg("vg-recorded")
        (self.config / "vault-builder" / "vault-hooks.json").write_text(json.dumps({"vg": str(recorded)}))
        self.assertEqual(launcher.find_vg(env), recorded)


class SessionStartTests(LauncherCase):
    def test_vg_exit_zero_output_is_printed_as_is(self) -> None:
        self.fake_vg(output="VG-OUT\n")
        self.fake_recall()
        data = self.hook("SessionStart", source="startup")
        result = self.run_script(["session-start"], data)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"VG-OUT\n")
        record = json.loads(self.record.read_text())
        self.assertEqual(record["argv"], ["hook", "session-start"])
        self.assertEqual(record["stdin"], data.decode())
        self.assertEqual(Path(record["cwd"]).resolve(), self.repo)

    def test_vg_exit_zero_with_empty_output_stays_empty(self) -> None:
        self.fake_vg(output="")
        self.fake_recall()
        result = self.run_script(["session-start"], self.hook("SessionStart", source="resume"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_vg_failure_prints_recall_output(self) -> None:
        self.fake_vg(exit_code=3, output="partial")
        self.fake_recall()
        result = self.run_script(["session-start"], self.hook("SessionStart"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"RECALL-OUT\n")
        self.assertEqual(Path((self.tmp / "recall-cwd").read_text().strip()).resolve(), self.repo)

    def test_missing_vg_prints_recall_output(self) -> None:
        self.fake_recall()
        result = self.run_script(["session-start"], self.hook("SessionStart"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"RECALL-OUT\n")

    def test_vg_timeout_prints_recall_output(self) -> None:
        self.fake_vg(output="late", sleep=30)
        self.fake_recall()
        started = time.monotonic()
        with mock.patch.object(launcher, "VG_SESSION_START_TIMEOUT_S", 0.5):
            code, out, err = self.run_in_process(["session-start"], self.hook("SessionStart"))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, 0)
        self.assertEqual(out, b"RECALL-OUT\n")
        self.assertIn(b"timed out", err)

    def test_no_vg_and_no_recall_exits_zero_with_empty_output(self) -> None:
        result = self.run_script(["session-start"], self.hook("SessionStart"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")


class CaptureTests(LauncherCase):
    """capture falls back to capture --direct. A fake vault CLI records that it ran."""

    def setUp(self) -> None:
        super().setUp()
        self.vault = make_vault(self.tmp / "vaults" / "proj")
        self.cli_record = self.tmp / "cli-record.json"
        write_program(
            self.vault / "tools" / "wiki" / "cli.py",
            f"data = sys.stdin.buffer.read()\n"
            f"json.dump({{'argv': sys.argv[1:], 'stdin': data.decode(), 'cwd': os.getcwd()}}, "
            f"open({str(self.cli_record)!r}, 'w'))\n"
            f"print(json.dumps({{'status': 'written', 'vault': 'proj', 'path': 'raw/sessions/x.md'}}))",
        )
        self.write_map({"proj": (self.vault, "project")}, ["proj"])

    def assert_direct_ran(self, data: bytes) -> None:
        record = json.loads(self.cli_record.read_text())
        self.assertEqual(record["argv"], ["sessions", "capture", "--json"])
        self.assertEqual(record["stdin"], data.decode())
        self.assertEqual(Path(record["cwd"]).resolve(), self.vault)

    def test_vg_exit_zero_skips_direct_capture(self) -> None:
        self.fake_vg(output="vg captured\n")
        data = self.hook("SessionEnd", reason="exit")
        result = self.run_script(["capture"], data)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.record.read_text())["argv"], ["hook", "capture"])
        self.assertEqual(json.loads(self.record.read_text())["stdin"], data.decode())
        self.assertFalse(self.cli_record.exists())

    def test_vg_failure_runs_direct_capture(self) -> None:
        self.fake_vg(exit_code=3)
        data = self.hook("PreCompact", trigger="auto", custom_instructions="")
        result = self.run_script(["capture"], data)
        self.assertEqual(result.returncode, 0)
        self.assert_direct_ran(data)

    def test_missing_vg_runs_direct_capture(self) -> None:
        data = self.hook("SessionEnd", reason="exit")
        result = self.run_script(["capture"], data)
        self.assertEqual(result.returncode, 0)
        self.assert_direct_ran(data)

    def test_vg_timeout_runs_direct_capture(self) -> None:
        self.fake_vg(sleep=30)
        data = self.hook("SessionEnd", reason="exit")
        started = time.monotonic()
        with mock.patch.object(launcher, "VG_CAPTURE_TIMEOUT_S", 0.5):
            code, _, err = self.run_in_process(["capture"], data)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, 0)
        self.assertIn(b"timed out", err)
        self.assert_direct_ran(data)

    def test_direct_prints_the_vault_cli_result(self) -> None:
        result = self.run_script(["capture", "--direct"], self.hook())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "written")

    def test_direct_never_calls_vg(self) -> None:
        self.fake_vg()
        result = self.run_script(["capture", "--direct"], self.hook())
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.record.exists())

    def test_vault_cli_timeout_and_bad_input_still_exit_zero(self) -> None:
        write_program(self.vault / "tools" / "wiki" / "cli.py", "time.sleep(30)")
        with mock.patch.object(launcher, "DIRECT_CAPTURE_TIMEOUT_S", 0.5):
            code, out, _ = self.run_in_process(["capture", "--direct"], self.hook())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "skipped")
        for argv, data in ((["capture"], b"not json"), (["capture", "--direct"], b""), (["bogus"], b"{}"), ([], b"")):
            result = self.run_script(argv, data)
            self.assertEqual(result.returncode, 0, argv)

    def test_missing_router_still_exits_zero(self) -> None:
        installed = self.config / "vault-builder" / "vault_hooks.py"
        shutil.copyfile(LAUNCHER_SOURCE, installed)
        result = subprocess.run(
            [sys.executable, "-I", str(installed), "capture", "--direct"],
            input=self.hook(),
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "skipped")
        self.assertIn(b"llm_wiki_router.py", result.stderr)


class NormalizedCaptureTests(LauncherCase):
    """capture --direct --normalized FILE [--rerender] (vault-graph request S10)."""

    def setUp(self) -> None:
        super().setUp()
        self.vault = make_vault(self.tmp / "vaults" / "proj")
        self.cli_record = self.tmp / "cli-record.json"
        self.write_map({"proj": (self.vault, "project")}, ["proj"])
        self.normalized = self.tmp / "export" / "s.jsonl"
        self.normalized.parent.mkdir()
        session = {"schema": "vg-session-jsonl/1", "kind": "session", "session_id": SESSION_ID,
                   "agent": "claude-code", "cwd": str(self.repo)}
        self.normalized.write_text(json.dumps(session) + "\n", encoding="utf-8")

    def fake_cli(self, exit_code: int = 0) -> None:
        write_program(
            self.vault / "tools" / "wiki" / "cli.py",
            f"data = sys.stdin.buffer.read()\n"
            f"json.dump({{'argv': sys.argv[1:], 'stdin': data.decode(), 'cwd': os.getcwd()}}, "
            f"open({str(self.cli_record)!r}, 'w'))\n"
            f"print(json.dumps({{'status': 'written', 'schema': 'vault-session/v2'}}))\n"
            f"sys.exit({exit_code})",
        )

    def test_passes_the_absolute_normalized_path_and_hook_json(self) -> None:
        self.fake_cli()
        data = self.hook("SessionEnd", reason="exit")
        result = subprocess.run(
            [sys.executable, "-I", str(LAUNCHER_SOURCE), "capture", "--direct", "--normalized", "export/s.jsonl"],
            input=data, capture_output=True, env=self.env, cwd=self.tmp, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema"], "vault-session/v2")
        record = json.loads(self.cli_record.read_text())
        self.assertEqual(record["argv"], ["sessions", "capture", "--json", "--normalized", str(self.normalized)])
        self.assertEqual(record["stdin"], data.decode())
        self.assertEqual(Path(record["cwd"]).resolve(), self.vault)

    def test_rerender_is_passed_on(self) -> None:
        self.fake_cli()
        result = self.run_script(["capture", "--direct", "--normalized", str(self.normalized), "--rerender"], self.hook())
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.cli_record.read_text())
        self.assertEqual(record["argv"][-1], "--rerender")

    def test_vault_cli_that_does_not_know_normalized_exits_two(self) -> None:
        self.fake_cli(exit_code=2)
        result = self.run_script(["capture", "--direct", "--normalized", str(self.normalized)], self.hook())
        self.assertEqual(result.returncode, 2)

    def test_without_hook_cwd_the_normalized_cwd_picks_the_vault(self) -> None:
        self.fake_cli()
        result = self.run_script(["capture", "--direct", "--normalized", str(self.normalized)], b"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.cli_record.exists())

    def test_bad_normalized_arguments_print_usage_and_exit_two(self) -> None:
        for argv in (["capture", "--direct", "--normalized"], ["capture", "--direct", "--normalized", "x", "--bogus"],
                     ["capture", "--direct", "--rerender"]):
            with self.subTest(argv=argv):
                result = self.run_script(argv, self.hook())
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"usage", result.stderr)

    def test_end_to_end_writes_a_v2_part_with_real_vault_tools(self) -> None:
        vault = make_vault(self.tmp / "vaults" / "real", tools=BUILDER_ROOT / "tools")
        self.write_map({"real": (vault, "project")}, ["real"])
        lines = [
            {"schema": "vg-session-jsonl/1", "kind": "session", "session_id": SESSION_ID, "agent": "claude-code",
             "cwd": str(self.repo)},
            {"schema": "vg-session-jsonl/1", "kind": "exchange", "exchange_id": "x1", "seq": 1, "agent_scope": "main",
             "closed": True, "started_at": "2026-11-02T01:00:00.000Z", "ended_at": "2026-11-02T01:00:01.000Z",
             "tools": {}, "files_touched": [], "errors": 0, "outcome": "answered"},
            {"schema": "vg-session-jsonl/1", "kind": "turn", "turn_id": "x1", "exchange_id": "x1", "role": "user",
             "turn_kind": "human", "at": "2026-11-02T01:00:00.000Z", "text": "hello", "actions": []},
        ]
        self.normalized.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        result = self.run_script(["capture", "--direct", "--normalized", str(self.normalized)],
                                 self.hook("SessionEnd", reason="exit"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "written", result.stdout)
        files = list((vault / "raw" / "sessions").rglob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("schema: vault-session/v2", files[0].read_text(encoding="utf-8"))


class InstalledLayoutTests(LauncherCase):
    def test_installed_copy_loads_the_router_from_its_own_folder(self) -> None:
        folder = self.config / "vault-builder"
        shutil.copyfile(LAUNCHER_SOURCE, folder / "vault_hooks.py")
        shutil.copyfile(ROUTER_SOURCE, folder / "llm_wiki_router.py")
        vault = make_vault(self.tmp / "vaults" / "proj")
        write_program(vault / "tools" / "wiki" / "cli.py", "print(json.dumps({'status': 'written'}))")
        self.write_map({"proj": (vault, "project")}, ["proj"])
        result = subprocess.run(
            [sys.executable, "-I", str(folder / "vault_hooks.py"), "capture"],
            input=self.hook(),
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "written")


class TranscriptSavedWithoutVgTests(LauncherCase):
    """End to end: no vg, real vault tools (this checkout), the transcript lands in raw/sessions/."""

    def test_capture_without_vg_writes_the_session_file(self) -> None:
        vault = make_vault(self.tmp / "vaults" / "proj", tools=BUILDER_ROOT / "tools")
        self.write_map({"proj": (vault, "project")}, ["proj"])
        transcript = self.tmp / "transcript.jsonl"
        entries = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-11-02T01:00:00.000Z", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-11-02T01:00:01.000Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        result = self.run_script(["capture"], self.hook("SessionEnd", reason="exit"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "written", result.stdout)
        files = list((vault / "raw" / "sessions").rglob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("hello", files[0].read_text(encoding="utf-8"))


class VaultChoiceTableTests(LauncherCase):
    """Risk 7: launcher select_vault and session_capture.select_target_vault give the same vault on one table."""

    def choice_pair(self, cwd: Path) -> tuple[str | None, str | None]:
        request = session_capture.CaptureRequest(
            transcript_path=self.tmp / "transcript.jsonl",
            session_id=SESSION_ID,
            cwd=cwd,
            trigger="SessionEnd",
            vault=None,
            from_hook=True,
        )
        builder = session_capture.select_target_vault(request, self.env)
        builder_name = builder.name if isinstance(builder, session_capture.TargetVault) else None
        chosen = launcher.select_vault(cwd, self.env)
        launcher_name = chosen[0] if isinstance(chosen, tuple) else None
        self.assertEqual(launcher_name, builder_name)
        if isinstance(chosen, tuple):
            self.assertEqual(chosen[1].resolve(), builder.path.resolve())
        return builder_name, launcher_name

    def test_table(self) -> None:
        vaults = self.tmp / "vaults"
        project = make_vault(vaults / "proj")
        project2 = make_vault(vaults / "proj2")
        personal = make_vault(vaults / "me")
        team = make_vault(vaults / "team")
        no_manifest = make_vault(vaults / "bare", contract=None)
        old = make_vault(vaults / "old", contract=session_capture.MIN_CONTRACT_VERSION - 1)
        other_repo = make_repo(self.tmp / "work" / "other")
        outside = self.tmp / "not-git"
        outside.mkdir()
        named_repo = make_repo(self.tmp / "elsewhere" / "demo")
        remote_repo = make_repo(self.tmp / "clone" / "x", origin="git@example.com:me/demo.git")
        all_vaults = {
            "proj": (project, "project"),
            "proj2": (project2, "project"),
            "me": (personal, "personal"),
            "team": (team, "team"),
            "bare": (no_manifest, "project"),
            "old": (old, "project"),
        }
        map_file = self.config / "vault-builder" / "vault-map.json"
        # (case, project vaults or None for no map, extra project fields, cwd, expected vault)
        table = [
            ("one project vault", ["proj"], {}, self.repo, "proj"),
            ("project among personal and team", ["me", "team", "proj"], {}, self.repo, "proj"),
            ("only personal and team", ["me", "team"], {}, self.repo, None),
            ("two project vaults", ["proj", "proj2"], {}, self.repo, None),
            ("no manifest", ["bare"], {}, self.repo, None),
            ("old contract", ["old"], {}, self.repo, None),
            ("no vaults", [], {}, self.repo, None),
            ("cwd in a subfolder", ["proj"], {}, self.repo, "proj"),
            ("cwd outside git", ["proj"], {}, outside, None),
            ("repository not linked", ["proj"], {}, other_repo, None),
            ("found by folder name", ["proj"], {}, named_repo, "proj"),
            ("found by remote", ["proj"], {"remote": "https://EXAMPLE.com/me/demo"}, remote_repo, "proj"),
            ("unknown vault name", ["proj", "ghost"], {}, self.repo, None),
            ("no vault map", None, {}, self.repo, None),
        ]
        (self.repo / "sub").mkdir()
        for case, names, extra, cwd, expected in table:
            with self.subTest(case=case):
                if names is None:
                    map_file.unlink(missing_ok=True)
                else:
                    self.write_map(all_vaults, names, **extra)
                if case == "cwd in a subfolder":
                    cwd = self.repo / "sub"
                builder_name, launcher_name = self.choice_pair(cwd)
                self.assertEqual(launcher_name, builder_name)
                self.assertEqual(launcher_name, expected)

    def test_damaged_map_chooses_nothing_in_both(self) -> None:
        map_file = self.config / "vault-builder" / "vault-map.json"
        for text in ("{", json.dumps({"version": 9, "vaults": {}, "projects": {}}), json.dumps({"version": 2, "vaults": {"a": {"path": "/a"}}, "projects": {}})):
            with self.subTest(text=text):
                map_file.write_text(text, encoding="utf-8")
                self.assertEqual(self.choice_pair(self.repo), (None, None))


if __name__ == "__main__":
    unittest.main()
