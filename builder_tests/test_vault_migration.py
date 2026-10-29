from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Callable
from unittest import mock

from tools.vault_migration import cli
from tools.vault_migration import applying
from tools.vault_migration import installing
from tools.vault_migration.catalogs import build_catalog, load_catalog, sha256_file, write_catalog
from tools.vault_migration.applying import apply_plan, recover_migration
from tools.vault_migration.manifests import create_manifest, load_manifest, write_manifest
from tools.vault_migration.managed_paths import MANAGED_PATHS
from tools.vault_migration.models import ContractCatalog, ManagedFile, MigrationAction, MigrationPlan, VaultManifest
from tools.vault_migration.planning import (
    audit_vault,
    render_plan_summary,
    target_fingerprint,
    validate_external_output_path,
)
from tools.wiki.lib.maps import (
    PROJECT_MAP_MAX_TOKENS,
    PROJECT_SECTION_HEADINGS,
    PROJECT_SECTION_MAX_TOKENS,
    _render_project_summary,
    build_navigation_maps,
    estimate_tokens,
)
from tools.wiki.lib.vault_manifest import (
    normalized_json_sha256,
    parse_manifest_json,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
_PYTHON = shlex.quote(sys.executable)

# init-vault.sh and install write beneath HOME and CLAUDE_CONFIG_DIR. Every test in this module
# runs with a temporary HOME and without CLAUDE_CONFIG_DIR, so the real ~/.claude, ~/.claude.json
# and ~/.claude/settings.json are never touched (memory-stage2 spec A-26).
_MODULE_HOME: tempfile.TemporaryDirectory[str] | None = None
_SAVED_USER_ENVIRONMENT: dict[str, str | None] = {}


def setUpModule() -> None:
    global _MODULE_HOME
    _MODULE_HOME = tempfile.TemporaryDirectory()
    for key in ("HOME", "CLAUDE_CONFIG_DIR"):
        _SAVED_USER_ENVIRONMENT[key] = os.environ.get(key)
    os.environ["HOME"] = _MODULE_HOME.name
    os.environ.pop("CLAUDE_CONFIG_DIR", None)


def tearDownModule() -> None:
    for key, value in _SAVED_USER_ENVIRONMENT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if _MODULE_HOME is not None:
        _MODULE_HOME.cleanup()


def _run(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "tools/vault_migration/cli.py", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_concurrent_output_writes(
    first_write: Callable[[Callable[[], None]], None],
    second_write: Callable[[], None],
    *,
    timeout: float,
) -> tuple[bool, int, int]:
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    started_read, started_write = os.pipe()
    done_read, done_write = os.pipe()

    first = os.fork()
    if first == 0:
        os.close(ready_read)
        os.close(release_write)
        os.close(started_read)
        os.close(started_write)
        os.close(done_read)
        os.close(done_write)

        def pause() -> None:
            os.write(ready_write, b"1")
            os.read(release_read, 1)

        try:
            first_write(pause)
        except BaseException:
            os._exit(71)
        os._exit(0)

    os.close(ready_write)
    os.close(release_read)
    if os.read(ready_read, 1) != b"1":
        raise AssertionError("First output writer did not reach its pause.")

    second = os.fork()
    if second == 0:
        os.close(ready_read)
        os.close(release_write)
        os.close(started_read)
        os.close(done_read)
        try:
            os.write(started_write, b"1")
            second_write()
            os.write(done_write, b"1")
        except BaseException:
            os._exit(72)
        os._exit(0)

    os.close(started_write)
    os.close(done_write)
    if os.read(started_read, 1) != b"1":
        raise AssertionError("Second output writer did not start.")
    second_finished_while_first_paused = bool(
        select.select([done_read], [], [], timeout)[0]
    )
    os.write(release_write, b"1")
    _, first_status = os.waitpid(first, 0)
    _, second_status = os.waitpid(second, 0)
    os.close(ready_read)
    os.close(release_write)
    os.close(started_read)
    os.close(done_read)
    return (
        second_finished_while_first_paused,
        first_status,
        second_status,
    )


def _extract_git_archive(repository: Path, revision: str, destination: Path) -> Path:
    archive = subprocess.run(
        ["git", "archive", revision],
        cwd=repository,
        capture_output=True,
        check=True,
    ).stdout
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        if sys.version_info >= (3, 12):
            contents.extractall(destination, filter="data")
        else:
            contents.extractall(destination)
    return destination


def _normalize_legacy_modes(target: Path) -> None:
    legacy_catalog = load_catalog(ROOT / "tools" / "vault_migration" / "contracts" / "v0.json")
    for relative, managed in legacy_catalog.managed_files.items():
        path = target / relative
        if path.is_file() and not path.is_symlink():
            path.chmod(int(managed.mode, 8))


def _copy_legacy_fixture(target: Path) -> None:
    _extract_git_archive(ROOT, "47cbf83", target)
    _normalize_legacy_modes(target)
    (target / "raw" / "sources" / "input.md").write_text("Legacy input.\n")
    (target / "wiki" / "concepts" / "topic.md").write_text(
        "---\ntitle: Topic\ntype: concept\nstatus: active\ncreated: 2026-07-30\n"
        "updated: 2026-07-30\nowner: human\nsummary: Topic knowledge.\nsource_count: 0\n"
        "tags: []\nrelated: []\nconfidence: high\nquality:\n  provenance: none\n"
        "  links: valid\n  contradictions: none\n  review_required: false\n---\n\n# Topic\n\nOwned knowledge.\n"
    )
    (target / "scratch" / "reports" / "audit.md").write_text("Prior audit evidence.\n")


def _initialize_legacy_vault(root: Path) -> Path:
    legacy_builder = _extract_git_archive(ROOT, "47cbf83", root / "legacy-builder")
    target = root / "vault"
    _run(["bash", "init-vault.sh", str(target)], legacy_builder)
    _normalize_legacy_modes(target)
    return target


# Contract v1 is frozen at this commit on main (memory-stage2 spec decision 8). PR #2 was
# rebase-merged, so the branch commit 3e75f46 became 5fa66d4 with the same tree.
V1_BASELINE_COMMIT = "5fa66d4"


def _initialize_v1_vault(root: Path) -> Path:
    v1_builder = _extract_git_archive(ROOT, V1_BASELINE_COMMIT, root / "v1-builder")
    target = root / "vault"
    _run(["bash", "init-vault.sh", str(target)], v1_builder)
    return target


def _add_user_role_content(target: Path) -> dict[str, str]:
    raw = target / "raw" / "sources" / "user-evidence.md"
    raw.write_text("Representative user-owned raw evidence.\n")
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()

    source_manifest = target / "wiki" / "sources" / "user-evidence.md"
    source_manifest.write_text(
        "---\n"
        "title: User Evidence\n"
        "type: source\n"
        "status: active\n"
        "created: 2026-07-30\n"
        "updated: 2026-07-30\n"
        "owner: human\n"
        "summary: Representative user-owned raw evidence.\n"
        "source_count: 1\n"
        "tags: []\n"
        "related: []\n"
        "confidence: high\n"
        "quality:\n"
        "  provenance: section\n"
        "  links: unchecked\n"
        "  contradictions: none\n"
        "  review_required: false\n"
        "canonical_source: raw/sources/user-evidence.md\n"
        f"raw_sha256: {raw_hash}\n"
        "source_kind: markdown\n"
        "---\n\n"
        "# User Evidence\n\n"
        "## Summary\n\n"
        "Representative user-owned raw evidence.\n\n"
        "Source: `raw/sources/user-evidence.md`\n\n"
        "## Registration\n\n"
        "- Canonical source: `raw/sources/user-evidence.md`\n"
        f"- SHA-256: `{raw_hash}`\n"
        "- Source kind: `markdown`\n\n"
        "## Durable Knowledge Links\n\n"
        "- None.\n"
    )

    canonical = target / "wiki" / "concepts" / "compiled-knowledge.md"
    canonical.write_text(
        canonical.read_text()
        + "\n## Human-Owned Migration Note\n\n"
        "This durable note must remain byte-for-byte unchanged during migration.\n\n"
        "Source: Explicit human instruction for Task 8 acceptance.\n"
    )

    log = target / "wiki" / "log.md"
    log.write_text(
        log.read_text()
        + "\n## [2026-07-30] manual-note | Preserve Migration Acceptance Content\n\n"
        "- Recorded representative user-owned content before migration.\n"
        "- Contradictions found: none.\n"
        "- Follow-ups: preserve these bytes and modes through migration.\n"
    )

    report = target / "scratch" / "reports" / "user-audit.md"
    report.write_text("# User Audit Evidence\n\nThis report is user-owned and immutable during migration.\n")

    generated_view = target / "wiki" / "maps" / "README.md"
    generated_view.write_text(
        generated_view.read_text()
        + "\n## Human Navigation Note\n\n"
        "This navigation note must remain byte-for-byte unchanged during migration.\n\n"
        "Source: Explicit human instruction for Task 8 acceptance.\n"
    )

    modes = {
        raw.relative_to(target).as_posix(): "0600",
        source_manifest.relative_to(target).as_posix(): "0640",
        canonical.relative_to(target).as_posix(): "0640",
        log.relative_to(target).as_posix(): "0640",
        report.relative_to(target).as_posix(): "0600",
        generated_view.relative_to(target).as_posix(): "0640",
    }
    for relative, mode in modes.items():
        (target / relative).chmod(int(mode, 8))
    return modes


def _tree_digest(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        mode = f"{status.st_mode & 0o7777:04o}"
        if path.is_symlink():
            kind = "symlink"
            content = os.readlink(path).encode()
        elif path.is_file():
            kind = "file"
            content = path.read_bytes()
        elif path.is_dir():
            kind = "directory"
            content = b""
        else:
            kind = "other"
            content = b""
        entries.append(
            {
                "path": relative,
                "type": kind,
                "mode": mode,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _protected_hashes(root: Path) -> dict[str, tuple[str, str]]:
    values: dict[str, tuple[str, str]] = {}
    for directory in ("raw", "wiki", "scratch"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and not path.is_symlink():
                values[path.relative_to(root).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    f"{path.stat().st_mode & 0o7777:04o}",
                )
    return values


def _commit_all(target: Path) -> None:
    _run(["git", "init", "-q"], target)
    _run(["git", "add", "-A"], target)
    _run(
        [
            "git",
            "-c",
            "user.name=Vault Test",
            "-c",
            "user.email=vault-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        target,
    )


def _capture_inherited_output(action: Callable[[], None]) -> bytes:
    """Return bytes written to process file descriptors 1 and 2 during action."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    with tempfile.TemporaryFile() as sink:
        try:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            action()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
        sink.seek(0)
        return sink.read()


class VaultManifestTest(unittest.TestCase):
    def test_manifest_generated_at_requires_a_real_utc_second_timestamp(self):
        manifest = VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {})

        self.assertEqual(manifest, VaultManifest.from_json(manifest.to_json()))
        for generated_at in (
            "2026-07-30T00:00:00+00:00",
            "2026-07-30T00:00:00.000Z",
            "2026-02-30T00:00:00Z",
            "2026-07-30T24:00:00Z",
            "2026-07-30T00:00:00Z ",
        ):
            with self.assertRaisesRegex(ValueError, "generated_at"):
                VaultManifest(1, 1, "test", generated_at, {})

        for generated_at in (True, None, 1):
            with self.assertRaisesRegex(ValueError, "generated_at"):
                VaultManifest.from_dict(
                    {
                        "schema_version": 1,
                        "contract_version": 1,
                        "builder_revision": "test",
                        "generated_at": generated_at,
                        "managed_files": {},
                        "last_migration_id": None,
                    }
                )

    def test_manifest_records_only_copied_managed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "VAULT_BUILDER_REVISION": "test-revision"},
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            manifest = load_manifest(target)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(2, manifest.contract_version)
            self.assertEqual("test-revision", manifest.builder_revision)
            self.assertNotIn("wiki/overview.md", manifest.managed_files)
            self.assertNotIn("tools/vault_migration/cli.py", manifest.managed_files)
            for relative, managed in manifest.managed_files.items():
                self.assertEqual(managed.sha256, sha256_file(target / relative), relative)

    def test_manifest_creation_rejects_managed_file_mode_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            managed = target / "AGENTS.md"
            managed.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "mode"):
                create_manifest(ROOT, target, "test")

    def test_init_rejects_old_python_before_writing_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stub_bin = root / "bin"
            stub_bin.mkdir()
            for command in ("dirname", "mkdir", "cp", "find", "chmod", "touch"):
                resolved = shutil.which(command)
                assert resolved is not None, command
                (stub_bin / command).symlink_to(resolved)
            stub_python = stub_bin / "python3"
            stub_python.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then echo "Python 3.9.18"; exit 0; fi\n'
                "cat >/dev/null\n"
                "exit 1\n",
                encoding="utf-8",
            )
            stub_python.chmod(0o755)
            bash = shutil.which("bash")
            assert bash is not None
            env = {"HOME": str(root), "PATH": str(stub_bin)}

            missing_target = root / "missing"
            empty_target = root / "empty"
            empty_target.mkdir()
            for target in (missing_target, empty_target):
                with self.subTest(target=target.name):
                    result = subprocess.run(
                        [bash, str(ROOT / "init-vault.sh"), str(target)],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=False,
                        env=env,
                    )
                    self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                    self.assertIn("Python 3.10 or newer", result.stderr)
                    self.assertIn("Found: Python 3.9.18", result.stderr)
                    self.assertIn("uv python install 3.12", result.stderr)
            self.assertFalse(missing_target.exists())
            self.assertEqual([], list(empty_target.iterdir()))

    def test_load_manifest_rejects_malformed_or_symlinked_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            target.mkdir()
            self.assertIsNone(load_manifest(target))

            manifest_path = target / ".vault-builder" / "manifest.json"
            manifest_path.parent.mkdir()
            manifest_path.write_text("{not-json\n")
            with self.assertRaisesRegex(ValueError, "manifest"):
                load_manifest(target)

            manifest_path.write_text(
                VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {}).to_json().replace(
                    "2026-07-30T00:00:00Z", "2026-02-30T00:00:00Z"
                )
            )
            with self.assertRaisesRegex(ValueError, "manifest"):
                load_manifest(target)

            manifest_path.unlink()
            outside = Path(tmpdir) / "outside.json"
            outside.write_text("{}\n")
            manifest_path.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                load_manifest(target)

    def test_write_manifest_rejects_symlinked_state_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            outside = Path(tmpdir) / "outside"
            target.mkdir()
            outside.mkdir()
            (target / ".vault-builder").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                write_manifest(
                    target,
                    VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {}),
                )

    def test_manifest_revision_prefers_explicit_then_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            target.mkdir()
            with mock.patch.dict(os.environ, {"VAULT_BUILDER_REVISION": "environment"}):
                self.assertEqual("explicit", create_manifest(ROOT, target, "explicit").builder_revision)
                self.assertEqual("environment", create_manifest(ROOT, target, "").builder_revision)

    def test_manifest_cli_reports_invalid_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "not-a-directory"
            target.write_text("not a vault directory\n")

            self.assertEqual(
                1,
                cli.main(
                    [
                        "manifest",
                        "--builder-root",
                        str(ROOT),
                        "--target",
                        str(target),
                        "--builder-revision",
                        "test",
                    ]
                ),
            )

    def test_manifest_write_cleans_temporary_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            target.mkdir()
            manifest = VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {})

            with mock.patch("tools.vault_migration.manifests.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(ValueError, "Could not write"):
                    write_manifest(target, manifest)

            self.assertEqual([], list((target / ".vault-builder").glob(".manifest.json.*.tmp")))

    def test_manifest_write_reports_parent_fsync_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            target.mkdir()
            manifest = VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {})

            with mock.patch(
                "tools.vault_migration.manifests._fsync_directory",
                side_effect=OSError(errno.EIO, "injected manifest parent fsync"),
            ):
                with self.assertRaisesRegex(ValueError, "Could not write"):
                    write_manifest(target, manifest)

            self.assertEqual([], list((target / ".vault-builder").glob(".manifest.json.*.tmp")))


def _external_records() -> list[dict[str, object]]:
    return [
        {
            "kind": "file",
            "location": ".claude/skills/project-memory/SKILL.md",
            "sha256": "a" * 64,
            "shared": True,
        },
        {
            "kind": "config",
            "location": ".claude/vault-builder/vault-map.json",
            "shared": True,
        },
        {
            "kind": "json-entry",
            "location": ".claude.json",
            "key_path": "mcpServers.llm-wiki-personal",
            "sha256": "b" * 64,
            "shared": False,
        },
    ]


def _valid_v2_manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "contract_version": 2,
        "builder_revision": "test",
        "generated_at": "2026-10-27T00:00:00Z",
        "managed_files": {
            "AGENTS.md": {
                "path": "AGENTS.md",
                "sha256": "c" * 64,
                "mode": "0644",
                "policy": "replace-if-unmodified",
            }
        },
        "external_records": _external_records(),
        "last_migration_id": None,
    }


class VaultManifestV2SchemaTest(unittest.TestCase):
    def test_manifest_parser_reads_v1_and_v2(self):
        v1 = {
            "schema_version": 1,
            "contract_version": 1,
            "builder_revision": "test",
            "generated_at": "2026-07-30T00:00:00Z",
            "managed_files": {},
        }
        parsed_v1 = parse_manifest_json(json.dumps(v1))
        self.assertEqual(1, parsed_v1["schema_version"])
        self.assertNotIn("external_records", parsed_v1)

        parsed_v2 = parse_manifest_json(json.dumps(_valid_v2_manifest()))
        self.assertEqual(2, parsed_v2["schema_version"])
        self.assertEqual(2, parsed_v2["contract_version"])
        self.assertEqual(
            sorted(_external_records(), key=lambda item: (item["location"], item.get("key_path", ""))),
            parsed_v2["external_records"],
        )

    def test_vault_manifest_model_round_trips_external_records(self):
        manifest = VaultManifest.from_dict(_valid_v2_manifest())

        self.assertEqual(3, len(manifest.external_records))
        self.assertEqual(manifest, VaultManifest.from_json(manifest.to_json()))
        self.assertIn('"external_records"', manifest.to_json())
        self.assertNotIn(
            "external_records",
            VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {}).to_json(),
        )
        with self.assertRaisesRegex(ValueError, "external_records"):
            VaultManifest(
                1,
                1,
                "test",
                "2026-07-30T00:00:00Z",
                {},
                external_records=tuple(_external_records()),
            )

    def test_empty_external_records_are_valid_for_v2(self):
        value = _valid_v2_manifest()
        value["external_records"] = []

        self.assertEqual([], validate_manifest(value)["external_records"])

    def test_normalized_json_sha256_ignores_key_order_and_whitespace(self):
        first = normalized_json_sha256({"command": "python3", "args": ["a", "b"]})
        second = normalized_json_sha256(json.loads('{ "args": ["a","b"],  "command":"python3" }'))

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, normalized_json_sha256({"command": "python3", "args": ["b", "a"]}))

    def test_manifest_validator_rejects_invalid_versions_and_external_records(self):
        cases: dict[str, Callable[[dict[str, object]], None]] = {}

        def record(index: int, key: str, item: object) -> Callable[[dict[str, object]], None]:
            def change(value: dict[str, object]) -> None:
                records = value["external_records"]
                assert isinstance(records, list)
                records[index][key] = item

            return change

        def drop(index: int, key: str) -> Callable[[dict[str, object]], None]:
            def change(value: dict[str, object]) -> None:
                records = value["external_records"]
                assert isinstance(records, list)
                records[index].pop(key)

            return change

        def duplicate(index: int) -> Callable[[dict[str, object]], None]:
            def change(value: dict[str, object]) -> None:
                records = value["external_records"]
                assert isinstance(records, list)
                records.append(dict(records[index]))

            return change

        def file_location_reused_by_entry(value: dict[str, object]) -> None:
            records = value["external_records"]
            assert isinstance(records, list)
            records[2]["location"] = records[0]["location"]

        cases["v2-missing-external-records"] = lambda value: value.pop("external_records")
        cases["v1-with-external-records"] = lambda value: value.update(schema_version=1, contract_version=1)
        cases["schema-2-contract-1"] = lambda value: value.__setitem__("contract_version", 1)
        cases["schema-3"] = lambda value: value.update(schema_version=3, contract_version=3)
        cases["records-not-list"] = lambda value: value.__setitem__("external_records", {})
        cases["record-not-object"] = lambda value: value.__setitem__("external_records", ["x"])
        cases["unknown-kind"] = record(0, "kind", "directory")
        cases["absolute-location"] = record(0, "location", "/Users/someone/.claude/skills/project-memory/SKILL.md")
        cases["parent-location"] = record(0, "location", "../.claude/SKILL.md")
        cases["tilde-location"] = record(0, "location", "~/.claude/SKILL.md")
        cases["empty-location"] = record(0, "location", "")
        cases["file-missing-sha256"] = drop(0, "sha256")
        cases["file-bad-sha256"] = record(0, "sha256", "A" * 64)
        cases["file-key-path"] = record(0, "key_path", "mcpServers.x")
        cases["config-with-sha256"] = record(1, "sha256", "a" * 64)
        cases["shared-not-bool"] = record(1, "shared", 1)
        cases["shared-missing"] = drop(1, "shared")
        cases["unknown-record-field"] = record(1, "extra", True)
        cases["entry-missing-key-path"] = drop(2, "key_path")
        cases["entry-other-root-key"] = record(2, "key_path", "projects.llm-wiki-personal")
        cases["entry-empty-name"] = record(2, "key_path", "mcpServers.")
        cases["entry-nested-name"] = record(2, "key_path", "mcpServers.a/b")
        cases["entry-missing-sha256"] = drop(2, "sha256")
        cases["entry-shared"] = record(2, "shared", True)
        cases["duplicate-file"] = duplicate(0)
        cases["duplicate-entry"] = duplicate(2)
        cases["file-location-reused"] = file_location_reused_by_entry

        for name, change in cases.items():
            value = json.loads(json.dumps(_valid_v2_manifest()))
            change(value)
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_manifest(value)
            with self.subTest(name=f"{name}-json"), self.assertRaises(ValueError):
                parse_manifest_json(json.dumps(value))


class ContractCatalogTest(unittest.TestCase):
    def test_catalog_rejects_symlinked_ancestor_for_an_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "builder"
            outside = Path(tmpdir) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "file.py").write_text("outside\n")
            (root / "nested").symlink_to(outside, target_is_directory=True)

            with mock.patch("tools.vault_migration.catalogs.MANAGED_EXACT_PATHS", ("nested/file.py",)), mock.patch(
                "tools.vault_migration.catalogs.MANAGED_TREE_ROOTS", ()
            ):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    build_catalog(root, 1, "test")

    def test_catalog_rejects_symlinked_ancestor_during_tree_enumeration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "builder"
            outside = Path(tmpdir) / "outside"
            (root / "tools" / "wiki").mkdir(parents=True)
            outside.mkdir()
            (outside / "cli.py").write_text("outside\n")
            (root / "tools" / "wiki" / "linked").symlink_to(outside, target_is_directory=True)

            with mock.patch("tools.vault_migration.catalogs.MANAGED_EXACT_PATHS", ()), mock.patch(
                "tools.vault_migration.catalogs.MANAGED_TREE_ROOTS", ("tools/wiki",)
            ):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    build_catalog(root, 1, "test")

    def test_catalog_write_rejects_symlinked_output_ancestor_and_cli_outside_root(self):
        catalog = ContractCatalog(1, 1, "test", {})
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "builder"
            outside = Path(tmpdir) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "output").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                write_catalog(root / "output" / "v1.json", catalog, output_root=root)

            external = Path(tmpdir) / "outside.json"
            self.assertEqual(
                1,
                cli.main(
                    [
                        "catalog",
                        "--contract-version",
                        "1",
                        "--revision",
                        "test",
                        "--output",
                        str(external),
                    ]
                ),
            )
            self.assertFalse(external.exists())

    def test_legacy_catalog_reads_git_bytes_and_mode(self):
        catalog = build_catalog(ROOT, 0, "47cbf83", source_ref="47cbf83")
        managed = catalog.managed_files["tools/wiki/cli.py"]
        contents = subprocess.run(
            ["git", "show", "47cbf83:tools/wiki/cli.py"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout

        self.assertEqual(hashlib.sha256(contents).hexdigest(), managed.sha256)
        self.assertEqual("0644", managed.mode)

    def test_plan_deeply_freezes_authority_issues(self):
        plan = MigrationPlan(
            schema_version=1,
            migration_id="0123456789abcdef",
            current_contract_version=None,
            target_contract_version=1,
            builder_revision="test",
            target_fingerprint="c" * 64,
            actions=(),
            authority_issues=({"context": {"paths": ["wiki/concepts/topic.md"]}},),
        )

        with self.assertRaises(TypeError):
            plan.authority_issues[0]["context"] = {}
        with self.assertRaises(AttributeError):
            plan.authority_issues[0]["context"]["paths"].append("wiki/concepts/other.md")
        self.assertEqual(
            plan,
            MigrationPlan.from_json(plan.to_json()),
        )

    def test_plan_rejects_non_finite_authority_issue_floats(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(ValueError, "finite"):
                MigrationPlan(
                    schema_version=1,
                    migration_id="0123456789abcdef",
                    current_contract_version=None,
                    target_contract_version=1,
                    builder_revision="test",
                    target_fingerprint="c" * 64,
                    actions=(),
                    authority_issues=({"score": value},),
                )
            with self.assertRaisesRegex(ValueError, "finite"):
                MigrationPlan.from_dict(
                    {
                        "schema_version": 1,
                        "migration_id": "0123456789abcdef",
                        "current_contract_version": None,
                        "target_contract_version": 1,
                        "builder_revision": "test",
                        "target_fingerprint": "c" * 64,
                        "actions": [],
                        "authority_issues": [{"score": value}],
                    }
                )

    def test_plan_round_trips_finite_authority_issue_floats(self):
        plan = MigrationPlan(
            schema_version=1,
            migration_id="0123456789abcdef",
            current_contract_version=None,
            target_contract_version=1,
            builder_revision="test",
            target_fingerprint="c" * 64,
            actions=(),
            authority_issues=({"score": 1.25},),
        )

        self.assertEqual(plan, MigrationPlan.from_json(plan.to_json()))
        self.assertIn('"score": 1.25', plan.to_json())

    def test_parse_boundaries_reject_representative_invalid_nested_values(self):
        with self.assertRaisesRegex(ValueError, "path"):
            ManagedFile.from_dict(
                {
                    "path": "/tools/wiki/cli.py",
                    "sha256": "a" * 64,
                    "mode": "0644",
                    "policy": "replace-if-unmodified",
                }
            )
        with self.assertRaisesRegex(ValueError, "integer"):
            ContractCatalog.from_dict(
                {
                    "schema_version": True,
                    "contract_version": 1,
                    "builder_revision": "test",
                    "managed_files": {},
                }
            )
        with self.assertRaisesRegex(ValueError, "unknown"):
            ContractCatalog.from_dict(
                {
                    "schema_version": 1,
                    "contract_version": 1,
                    "builder_revision": "test",
                    "managed_files": {
                        "tools/wiki/cli.py": {
                            "path": "tools/wiki/cli.py",
                            "sha256": "a" * 64,
                            "mode": "0644",
                            "policy": "replace-if-unmodified",
                            "unexpected": True,
                        }
                    },
                }
            )
        for field, value in (("sha256", "A" * 64), ("mode", "0600"), ("policy", "unknown")):
            data = {
                "path": "tools/wiki/cli.py",
                "sha256": "a" * 64,
                "mode": "0644",
                "policy": "replace-if-unmodified",
            }
            data[field] = value
            with self.assertRaises(ValueError):
                ManagedFile.from_dict(data)
        with self.assertRaisesRegex(ValueError, "action"):
            MigrationAction.from_dict(
                {
                    "path": "tools/wiki/cli.py",
                    "action": "overwrite",
                    "current_sha256": None,
                    "desired_sha256": "b" * 64,
                    "current_mode": None,
                    "desired_mode": "0644",
                    "reason": "unsupported action",
                }
            )

    def test_model_parsers_reject_unsafe_or_unknown_values(self):
        with self.assertRaisesRegex(ValueError, "path"):
            ManagedFile.from_dict(
                {
                    "path": "../tools/wiki/cli.py",
                    "sha256": "a" * 64,
                    "mode": "0644",
                    "policy": "replace-if-unmodified",
                }
            )
        with self.assertRaisesRegex(ValueError, "unknown"):
            ContractCatalog.from_dict(
                {
                    "schema_version": 1,
                    "contract_version": 1,
                    "builder_revision": "test",
                    "managed_files": {},
                    "extra": True,
                }
            )
        with self.assertRaisesRegex(ValueError, "migration_id"):
            MigrationPlan.from_dict(
                {
                    "schema_version": 1,
                    "migration_id": "not-a-valid-id",
                    "current_contract_version": None,
                    "target_contract_version": 1,
                    "builder_revision": "test",
                    "target_fingerprint": "c" * 64,
                    "actions": [],
                    "authority_issues": [],
                }
            )

    def test_v1_baseline_commit_is_in_the_checked_out_history(self):
        # A rebase or squash merge rewrites commit ids; the baseline must stay reachable from HEAD.
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", V1_BASELINE_COMMIT, "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_v1_catalog_bytes_are_fixed_at_v1_baseline_commit(self):
        relative = "tools/vault_migration/contracts/v1.json"
        frozen = subprocess.run(
            ["git", "show", f"{V1_BASELINE_COMMIT}:{relative}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout

        self.assertEqual(frozen, (ROOT / relative).read_bytes())

    def test_v2_catalog_matches_current_managed_files(self):
        catalog = load_catalog(ROOT / "tools" / "vault_migration" / "contracts" / "v2.json")

        self.assertEqual(2, catalog.contract_version)
        self.assertEqual("contract-v2", catalog.builder_revision)
        self.assertIn(".gitignore", catalog.managed_files)
        for relative in (
            ".agents/skills/memory-cleanup/SKILL.md",
            ".claude/skills/memory-cleanup/SKILL.md",
            "tools/wiki/lib/memory_scan.py",
        ):
            self.assertIn(relative, catalog.managed_files)
        self.assertFalse(
            any(path.startswith("tools/vault_migration/assets/") for path in catalog.managed_files)
        )
        self.assertEqual(set(MANAGED_PATHS), set(catalog.managed_files))
        for relative, managed in catalog.managed_files.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertFalse(path.is_symlink(), relative)
            self.assertEqual(managed.sha256, sha256_file(path), relative)
            observed_mode = "0755" if path.stat().st_mode & 0o111 else "0644"
            self.assertEqual(managed.mode, observed_mode, relative)

    def test_plan_json_round_trip_is_canonical(self):
        action = MigrationAction(
            path="tools/wiki/cli.py",
            action="replace",
            current_sha256="a" * 64,
            desired_sha256="b" * 64,
            current_mode="0644",
            desired_mode="0644",
            reason="known version 0 managed file",
        )
        plan = MigrationPlan(
            schema_version=1,
            migration_id="0123456789abcdef",
            current_contract_version=None,
            target_contract_version=1,
            builder_revision="test",
            target_fingerprint="c" * 64,
            actions=(action,),
            authority_issues=(),
        )

        rendered = plan.to_json()

        self.assertEqual(plan, MigrationPlan.from_json(rendered))
        self.assertEqual(rendered, json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n")

    def test_plan_round_trip_preserves_observed_permission_drift(self):
        for current_mode in ("0000", "0600", "0640", "0777"):
            with self.subTest(current_mode=current_mode):
                action = MigrationAction(
                    path="AGENTS.md",
                    action="replace",
                    current_sha256="a" * 64,
                    desired_sha256="a" * 64,
                    current_mode=current_mode,
                    desired_mode="0644",
                    reason="mode drift",
                )
                plan = MigrationPlan(
                    schema_version=1,
                    migration_id="0123456789abcdef",
                    current_contract_version=1,
                    target_contract_version=1,
                    builder_revision="test",
                    target_fingerprint="c" * 64,
                    actions=(action,),
                    authority_issues=(),
                )

                self.assertEqual(plan, MigrationPlan.from_json(plan.to_json()))


class MigrationPlanningTest(unittest.TestCase):
    def test_audit_is_read_only_and_classifies_known_custom_and_owned_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")
            before = _tree_digest(target)

            plan = audit_vault(ROOT, target)

            self.assertEqual(before, _tree_digest(target))
            actions = {action.path: action for action in plan.actions}
            self.assertEqual("review-required", actions["tools/wiki/cli.py"].action)
            self.assertEqual("preserve", actions["raw/sources/input.md"].action)
            self.assertEqual("preserve", actions["wiki/concepts/topic.md"].action)
            self.assertEqual("preserve", actions["wiki/log.md"].action)
            self.assertNotIn("scratch/reports/audit.md", actions)
            self.assertEqual("rebuild-suggested", actions["wiki/maps/topic-map.md"].action)
            self.assertEqual("preserve", actions["init-vault.sh"].action)
            self.assertEqual("preserve", actions["scripts/bootstrap.sh"].action)

    def test_audit_reports_are_not_migration_relevant_fingerprint_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            first = audit_vault(ROOT, target)
            report = target / "scratch" / "reports" / "new-audit.md"
            report.write_text("new audit output\n")

            second = audit_vault(ROOT, target)

            self.assertEqual(first, second)
            self.assertNotIn(
                report.relative_to(target).as_posix(),
                {action.path for action in second.actions},
            )

    def test_supported_manifest_is_migration_relevant_fingerprint_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            (target / "AGENTS.md").chmod(0o600)
            first = audit_vault(ROOT, target)
            current = load_manifest(target)
            assert current is not None
            later = VaultManifest(
                current.schema_version,
                current.contract_version,
                current.builder_revision,
                current.generated_at,
                current.managed_files,
                "f" * 16,
            )
            write_manifest(target, later)

            second = audit_vault(ROOT, target)

            self.assertNotEqual(first.target_fingerprint, second.target_fingerprint)
            self.assertNotEqual(first.migration_id, second.migration_id)
            with self.assertRaisesRegex(
                ValueError,
                "target changed after audit",
            ):
                apply_plan(ROOT, target, first, run_gate=lambda _: None)
            self.assertEqual(later, load_manifest(target))

    def test_audit_is_deterministic_and_uses_known_legacy_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)

            first = audit_vault(ROOT, target)
            second = audit_vault(ROOT, target)

            actions = {action.path: action for action in first.actions}
            self.assertEqual("replace", actions["tools/wiki/cli.py"].action)
            self.assertEqual("create", actions["tools/wiki/lib/authority.py"].action)
            self.assertEqual(first, second)
            self.assertEqual(first.to_json(), second.to_json())
            self.assertEqual(first.target_fingerprint, target_fingerprint(target, (action.path for action in first.actions)))
            self.assertIn(first.migration_id, render_plan_summary(target, first))

    def test_audit_uses_manifest_hash_as_safe_replacement_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            managed_path = target / "tools" / "wiki" / "cli.py"
            managed_path.write_text("known generated variant\n")
            manifest_path = target / ".vault-builder" / "manifest.json"
            manifest_path.parent.mkdir()
            manifest_path.write_text(
                VaultManifest(
                    1,
                    1,
                    "test",
                    "2026-07-30T00:00:00Z",
                    {
                        "tools/wiki/cli.py": ManagedFile(
                            "tools/wiki/cli.py",
                            hashlib.sha256(managed_path.read_bytes()).hexdigest(),
                            "0644",
                        )
                    },
                ).to_json()
            )

            plan = audit_vault(ROOT, target)

            actions = {action.path: action for action in plan.actions}
            self.assertEqual(1, plan.current_contract_version)
            self.assertEqual("replace", actions["tools/wiki/cli.py"].action)

    def test_audit_classifies_mode_only_drift_as_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            managed = target / "AGENTS.md"
            managed.chmod(0o600)
            before = _tree_digest(target)

            plan = audit_vault(ROOT, target)

            self.assertEqual(before, _tree_digest(target))
            action = next(
                action for action in plan.actions if action.path == "AGENTS.md"
            )
            self.assertEqual("replace", action.action)
            self.assertEqual(action.current_sha256, action.desired_sha256)
            self.assertEqual("0600", action.current_mode)
            self.assertEqual("0644", action.desired_mode)

    def test_audit_rejects_unknown_manifest_contract_without_trusting_its_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            managed_path = target / "tools" / "wiki" / "cli.py"
            managed_path.write_text("unknown contract generated variant\n")
            manifest_path = target / ".vault-builder" / "manifest.json"
            manifest_path.parent.mkdir()
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "contract_version": 2,
                        "builder_revision": "unknown-contract",
                        "generated_at": "2026-07-30T00:00:00Z",
                        "managed_files": {
                        "tools/wiki/cli.py": ManagedFile(
                            "tools/wiki/cli.py",
                            hashlib.sha256(managed_path.read_bytes()).hexdigest(),
                            "0644",
                        ).to_dict()
                        },
                        "last_migration_id": None,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "manifest"):
                audit_vault(ROOT, target)

            self.assertEqual(before, _tree_digest(target))

    def test_audit_marks_non_topology_catalog_leaf_as_invalid_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            invalid = target / "tools" / "wiki" / "lib" / "authority.py"
            invalid.mkdir()
            before = _tree_digest(target)

            plan = audit_vault(ROOT, target)

            self.assertEqual(before, _tree_digest(target))
            actions = {action.path: action for action in plan.actions}
            self.assertEqual("invalid", actions["tools/wiki/lib/authority.py"].action)

    def test_audit_forces_authority_scan_and_reports_duplicate_canonical_bodies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            duplicate = target / "wiki" / "concepts" / "duplicate.md"
            duplicate.write_text((target / "wiki" / "concepts" / "topic.md").read_text())
            source = target / "wiki" / "sources" / "legacy.md"
            source.parent.mkdir(exist_ok=True)
            source.write_text("---\ntitle: Legacy\ntype: source\n---\n\n# Legacy\n\n## Key Takeaways\n\nCopied body.\n")

            plan = audit_vault(ROOT, target)

            actions = {action.path: action for action in plan.actions}
            self.assertEqual("review-required", actions["wiki/concepts/topic.md"].action)
            self.assertEqual("review-required", actions["wiki/sources/legacy.md"].action)
            self.assertIn("duplicate-canonical-body", {issue["code"] for issue in plan.authority_issues})
            self.assertIn("source-manifest-semantic-body", {issue["code"] for issue in plan.authority_issues})

    def test_audit_rejects_target_symlink_and_unsafe_catalog_leaf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            alias = root / "vault-link"
            _copy_legacy_fixture(target)
            alias.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "target root must not be a symlink"):
                audit_vault(ROOT, alias)

            (target / "tools" / "wiki" / "cli.py").unlink()
            (target / "tools" / "wiki" / "cli.py").symlink_to(root / "outside.py")
            with self.assertRaisesRegex(ValueError, "symlink"):
                audit_vault(ROOT, target)

    def test_audit_rejects_user_symlink_in_target_ancestry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            real_parent = root / "real"
            real_parent.mkdir()
            target = real_parent / "vault"
            _copy_legacy_fixture(target)
            alias = root / "alias"
            alias.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "target path contains a symlink"):
                audit_vault(ROOT, alias / "vault")

    def test_external_output_stays_outside_target_and_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            outside = root / "outside"
            _copy_legacy_fixture(target)
            outside.mkdir()
            (root / "linked-output").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must stay outside the target Vault"):
                validate_external_output_path(target, target / "migration-plan.json")
            with self.assertRaisesRegex(ValueError, "symlink"):
                validate_external_output_path(target, root / "linked-output" / "migration-plan.json")
            validate_external_output_path(target, outside / "migration-plan.json")

    def test_external_output_rejects_symlink_before_existing_descendant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            outside = root / "outside"
            _copy_legacy_fixture(target)
            (outside / "nested").mkdir(parents=True)
            (root / "linked-output").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                validate_external_output_path(target, root / "linked-output" / "nested" / "migration-plan.json")

    def test_audit_rejects_role_directory_without_read_and_search_permission(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            blocked = target / "raw" / "blocked"
            blocked.mkdir()
            original_mode = blocked.stat().st_mode
            blocked.chmod(0)
            try:
                with self.assertRaisesRegex(ValueError, "unreadable"):
                    audit_vault(ROOT, target)
            finally:
                blocked.chmod(original_mode)


class MigrationApplyTest(unittest.TestCase):
    def _prepare_missing_agents_transaction(
        self,
        target: Path,
    ) -> tuple[
        str,
        MigrationPlan,
        Path,
        Path,
        dict[str, object],
        MigrationAction,
        Callable[[str, str], None],
    ]:
        _copy_legacy_fixture(target)
        shutil.rmtree(target / ".agents")
        before = _tree_digest(target)
        plan = audit_vault(ROOT, target)
        resolved_target = applying._target_root(target)
        checked, actions = applying._verify_preconditions(ROOT, resolved_target, plan)
        journal_root, journal = applying._prepare_journal(resolved_target, checked, actions)
        action = next(item for item in actions if item.path == ".agents/skills/wiki-ingest/SKILL.md")

        def record_directory(relative: str, lifecycle: str) -> None:
            records = journal["directories"]
            assert isinstance(records, list)
            if lifecycle == "intent":
                records.append({"existed": False, "path": relative, "state": "intent"})
                records.sort(key=lambda item: item["path"])
            else:
                next(item for item in records if item["path"] == relative)["state"] = "created"
            applying._write_journal_state(journal_root, journal, "applying")

        return before, plan, resolved_target, journal_root, journal, action, record_directory

    def test_apply_replaces_only_approved_managed_files_and_runs_two_gates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            protected_before = _protected_hashes(target)
            plan = audit_vault(ROOT, target)
            gate_versions: list[int | None] = []

            def gate(root: Path) -> None:
                manifest = load_manifest(root)
                if manifest is None:
                    journal = root / ".vault-builder" / "recovery" / plan.migration_id
                    self.assertTrue((journal / "journal.json").is_file())
                    self.assertTrue((journal / "originals").is_dir())
                gate_versions.append(None if manifest is None else manifest.contract_version)

            manifest = apply_plan(ROOT, target, plan, run_gate=gate)

            self.assertEqual(2, manifest.contract_version)
            self.assertEqual(plan.migration_id, manifest.last_migration_id)
            persisted = load_manifest(target)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(plan.migration_id, persisted.last_migration_id)
            self.assertEqual([None, 2], gate_versions)
            self.assertEqual(protected_before, _protected_hashes(target))
            self.assertFalse((target / ".vault-builder" / "recovery" / plan.migration_id).exists())

    def test_v1_manifest_vault_audit_replaces_v1_hashes_and_apply_writes_v2_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = _initialize_v1_vault(Path(tmpdir))
            before = load_manifest(target)
            assert before is not None
            self.assertEqual((1, 1), (before.schema_version, before.contract_version))
            v1 = load_catalog(ROOT / "tools" / "vault_migration" / "contracts" / "v1.json")
            v2 = load_catalog(ROOT / "tools" / "vault_migration" / "contracts" / "v2.json")
            changed = sorted(
                path
                for path, desired in v2.managed_files.items()
                if path in v1.managed_files and v1.managed_files[path].sha256 != desired.sha256
            )

            plan = audit_vault(ROOT, target)

            actions = {action.path: action for action in plan.actions}
            self.assertEqual(1, plan.current_contract_version)
            self.assertEqual(2, plan.target_contract_version)
            self.assertIn(".gitignore", changed)
            for relative in changed:
                self.assertEqual("replace", actions[relative].action, relative)
                self.assertEqual(v1.managed_files[relative].sha256, actions[relative].current_sha256, relative)
            self.assertFalse(plan.has_blocking_actions)

            manifest = apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            persisted = load_manifest(target)
            assert persisted is not None
            for result in (manifest, persisted):
                self.assertEqual(2, result.schema_version)
                self.assertEqual(2, result.contract_version)
                self.assertEqual((), result.external_records)
            stored = json.loads((target / ".vault-builder" / "manifest.json").read_text())
            self.assertEqual([], stored["external_records"])
            self.assertEqual(2, stored["schema_version"])
            for relative, desired in v2.managed_files.items():
                self.assertEqual(desired.sha256, sha256_file(target / relative), relative)

    def test_audit_uses_v1_catalog_hash_as_known_prior_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            v1_gitignore = subprocess.run(
                ["git", "show", f"{V1_BASELINE_COMMIT}:.gitignore"],
                cwd=ROOT,
                capture_output=True,
                check=True,
            ).stdout
            (target / ".gitignore").write_bytes(v1_gitignore)

            plan = audit_vault(ROOT, target)

            action = next(action for action in plan.actions if action.path == ".gitignore")
            self.assertEqual("replace", action.action)
            self.assertEqual(hashlib.sha256(v1_gitignore).hexdigest(), action.current_sha256)

    def test_apply_preserves_existing_v2_external_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            current = load_manifest(target)
            assert current is not None
            records = VaultManifest.from_dict(_valid_v2_manifest()).external_records
            write_manifest(target, replace(current, external_records=records))
            (target / "AGENTS.md").chmod(0o600)
            plan = audit_vault(ROOT, target)

            manifest = apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(records, manifest.external_records)
            self.assertEqual(records, load_manifest(target).external_records)

    def test_apply_creates_missing_managed_parents_and_rollback_removes_them(self):
        for fail_gate in (False, True):
            with self.subTest(fail_gate=fail_gate), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                shutil.rmtree(target / ".agents")
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)

                def gate(_: Path) -> None:
                    if fail_gate:
                        raise subprocess.CalledProcessError(1, ["python3", "tools/wiki/cli.py", "lint", "--report"])
                    marker = target / ".agents" / ".vault-builder-migration.json"
                    self.assertTrue(marker.is_file())
                    lint = subprocess.run(
                        [sys.executable, "tools/wiki/cli.py", "lint"],
                        cwd=target,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(1, lint.returncode, lint.stdout + lint.stderr)
                    self.assertIn("orphan-page", lint.stdout)
                    self.assertNotIn(".vault-builder-migration", lint.stdout)

                if fail_gate:
                    with self.assertRaises(applying.MigrationApplyError):
                        apply_plan(ROOT, target, plan, run_gate=gate)
                    self.assertEqual(before, _tree_digest(target))
                    self.assertFalse((target / ".agents").exists())
                else:
                    manifest = apply_plan(ROOT, target, plan, run_gate=gate)
                    self.assertTrue((target / ".agents" / "skills" / "wiki-ingest" / "SKILL.md").is_file())
                    self.assertEqual(manifest.last_migration_id, load_manifest(target).last_migration_id)
                    self.assertEqual([], list((target / ".agents").rglob(".vault-builder-migration.json")))

    def test_apply_rolls_back_parents_when_create_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            original_stage = applying._create_staged_file

            def fail_created_file(
                journal_root: Path,
                destination: Path,
                contents: bytes,
                mode: str,
                *,
                replace_partial: bool = False,
            ) -> None:
                if destination.as_posix().endswith(
                    "/staged/managed/.agents/skills/wiki-ingest/SKILL.md"
                ):
                    raise OSError("injected create failure")
                original_stage(
                    journal_root,
                    destination,
                    contents,
                    mode,
                    replace_partial=replace_partial,
                )

            with mock.patch(
                "tools.vault_migration.applying._create_staged_file",
                side_effect=fail_created_file,
            ):
                with self.assertRaisesRegex(applying.MigrationApplyError, "injected create failure"):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".agents").exists())

    def test_apply_rolls_back_parent_created_before_later_mkdir_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            original_mkdir = applying._mkdir_directory

            def fail_second_parent(path: Path) -> None:
                if path.as_posix().endswith("/.agents/skills"):
                    raise OSError("injected parent mkdir failure")
                original_mkdir(path)

            with mock.patch("tools.vault_migration.applying._mkdir_directory", side_effect=fail_second_parent):
                with self.assertRaisesRegex(applying.MigrationApplyError, "injected parent mkdir failure"):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".agents").exists())

    def test_apply_aborts_before_writes_for_blocking_or_changed_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")
            blocked_plan = audit_vault(ROOT, target)
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "requires human review"):
                apply_plan(ROOT, target, blocked_plan, run_gate=lambda _: None)
            self.assertEqual(before, _tree_digest(target))

            target = Path(tmpdir) / "changed-vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            (target / "tools" / "wiki" / "cli.py").write_text("changed after audit\n")
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "changed after audit"):
                apply_plan(ROOT, target, plan, run_gate=lambda _: None)
            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".vault-builder" / "recovery").exists())

    def test_outstanding_rollback_blocks_every_apply_until_recovered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            original = _tree_digest(target)
            first_plan = audit_vault(ROOT, target)

            def fail_gate(_: Path) -> None:
                raise subprocess.CalledProcessError(9, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with mock.patch.object(applying, "_rollback", side_effect=OSError("rollback exploded")):
                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(ROOT, target, first_plan, run_gate=fail_gate)

            first_journal = (
                target / ".vault-builder" / "recovery" / first_plan.migration_id
            )
            self.assertEqual(
                "rollback-failed",
                json.loads((first_journal / "journal.json").read_text())["state"],
            )
            second_plan = audit_vault(ROOT, target)
            self.assertNotEqual(first_plan.migration_id, second_plan.migration_id)
            blocked = _tree_digest(target)
            expected_command = (
                f"{_PYTHON} tools/vault_migration/cli.py recover "
                f"--target {target.resolve()} --migration-id {first_plan.migration_id}"
            )

            for candidate in (first_plan, second_plan):
                with self.subTest(candidate=candidate.migration_id):
                    with self.assertRaisesRegex(
                        ValueError,
                        re.escape(expected_command),
                    ):
                        apply_plan(ROOT, target, candidate, run_gate=lambda _: None)
                    self.assertEqual(blocked, _tree_digest(target))

            self.assertEqual(
                "rolled-back",
                recover_migration(target, first_plan.migration_id),
            )
            self.assertEqual(original, _tree_digest(target))

            fresh_plan = audit_vault(ROOT, target)
            manifest = apply_plan(
                ROOT,
                target,
                fresh_plan,
                run_gate=lambda _: None,
            )

            self.assertEqual(fresh_plan.migration_id, manifest.last_migration_id)

    def test_concurrent_apply_is_serialized_before_journal_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            _copy_legacy_fixture(target)
            first_plan = audit_vault(ROOT, target)
            second_builder = root / "second-builder"
            shutil.copytree(
                ROOT,
                second_builder,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".superpowers",
                    "__pycache__",
                    "scratch",
                ),
            )
            second_catalog_path = (
                second_builder
                / "tools"
                / "vault_migration"
                / "contracts"
                / "v2.json"
            )
            second_catalog = json.loads(second_catalog_path.read_text())
            second_catalog["builder_revision"] = "concurrent-builder"
            second_catalog_path.write_text(
                json.dumps(second_catalog, indent=2, sort_keys=True) + "\n"
            )
            second_plan = audit_vault(second_builder, target)
            self.assertNotEqual(first_plan.migration_id, second_plan.migration_id)
            ready_read, ready_write = os.pipe()
            release_read, release_write = os.pipe()
            error_read, error_write = os.pipe()
            original_check = applying._require_no_outstanding_migrations
            child = os.fork()
            if child == 0:
                os.close(ready_read)
                os.close(release_write)
                os.close(error_read)

                def pause_before_journal(resolved_target: Path) -> None:
                    original_check(resolved_target)
                    os.write(ready_write, b"1")
                    os.read(release_read, 1)

                try:
                    with mock.patch.object(
                        applying,
                        "_require_no_outstanding_migrations",
                        side_effect=pause_before_journal,
                    ):
                        apply_plan(
                            ROOT,
                            target,
                            first_plan,
                            run_gate=lambda _: None,
                        )
                except BaseException as exc:
                    os.write(error_write, repr(exc).encode("utf-8"))
                    os._exit(31)
                os._exit(0)

            os.close(ready_write)
            os.close(release_read)
            os.close(error_write)
            self.assertEqual(b"1", os.read(ready_read, 1))
            before = _tree_digest(target)
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "migration operation is active",
                ):
                    apply_plan(
                        second_builder,
                        target,
                        second_plan,
                        run_gate=lambda _: None,
                    )
                self.assertEqual(before, _tree_digest(target))
            finally:
                os.write(release_write, b"1")
                _, child_status = os.waitpid(child, 0)
                os.close(ready_read)
                os.close(release_write)
            child_error = os.read(error_read, 4096).decode("utf-8")
            os.close(error_read)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(0, os.WEXITSTATUS(child_status), child_error)

    def test_recovery_acquires_directory_lock_after_process_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            (
                before,
                plan,
                _resolved_target,
                _journal_root,
                _journal,
                _action,
                _record_directory,
            ) = self._prepare_missing_agents_transaction(target)
            ready_read, ready_write = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(ready_read)

                def hard_stop_after_lock(
                    _target: Path,
                    _migration_id: str,
                ) -> str:
                    os.write(ready_write, b"1")
                    os._exit(23)

                with mock.patch.object(
                    applying,
                    "_recover_migration_locked",
                    side_effect=hard_stop_after_lock,
                ):
                    recover_migration(target, plan.migration_id)
                os._exit(24)

            os.close(ready_write)
            self.assertEqual(b"1", os.read(ready_read, 1))
            _, child_status = os.waitpid(child, 0)
            os.close(ready_read)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(23, os.WEXITSTATUS(child_status))

            self.assertEqual(
                "rolled-back",
                recover_migration(target, plan.migration_id),
            )
            self.assertEqual(before, _tree_digest(target))

    def test_apply_reports_every_outstanding_migration_deterministically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            recovery = target / ".vault-builder" / "recovery"
            recovery.mkdir(parents=True)
            (recovery / "bbbbbbbbbbbbbbbb").mkdir()
            (
                target
                / ".preparing-aaaaaaaaaaaaaaaa-keep-recovery-keep-state"
            ).mkdir()
            before = _tree_digest(target)

            with self.assertRaises(ValueError) as raised:
                apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            message = str(raised.exception)
            first = (
                f"{_PYTHON} tools/vault_migration/cli.py recover "
                f"--target {target.resolve()} --migration-id aaaaaaaaaaaaaaaa"
            )
            second = (
                f"{_PYTHON} tools/vault_migration/cli.py recover "
                f"--target {target.resolve()} --migration-id bbbbbbbbbbbbbbbb"
            )
            self.assertIn(first, message)
            self.assertIn(second, message)
            self.assertLess(message.index(first), message.index(second))
            self.assertEqual(before, _tree_digest(target))

    def test_stale_journal_cannot_roll_back_a_later_committed_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            stale_storage = root / "stale-journal"
            _copy_legacy_fixture(target)
            stale_plan = audit_vault(ROOT, target)
            resolved = applying._target_root(target)
            checked, actions = applying._verify_preconditions(
                ROOT,
                resolved,
                stale_plan,
            )
            stale_journal, _ = applying._prepare_journal(
                resolved,
                checked,
                actions,
            )
            stale_journal.rename(stale_storage)

            changed = target / "AGENTS.md"
            desired = load_catalog(
                ROOT / "tools" / "vault_migration" / "contracts" / "v2.json"
            ).managed_files["AGENTS.md"]
            changed.write_bytes((ROOT / "AGENTS.md").read_bytes())
            changed.chmod(int(desired.mode, 8))
            later_plan = audit_vault(ROOT, target)
            self.assertNotEqual(stale_plan.migration_id, later_plan.migration_id)
            later_manifest = apply_plan(
                ROOT,
                target,
                later_plan,
                run_gate=lambda _: None,
            )
            recovery = target / ".vault-builder" / "recovery"
            recovery.mkdir(exist_ok=True)
            stale_storage.rename(recovery / stale_plan.migration_id)
            before_recovery = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "manifest|stale|later"):
                recover_migration(target, stale_plan.migration_id)

            self.assertEqual(before_recovery, _tree_digest(target))
            self.assertEqual(later_manifest, load_manifest(target))

    def test_apply_rolls_back_on_either_gate_failure_and_restores_manifest(self):
        for fail_after_manifest in (False, True):
            with self.subTest(fail_after_manifest=fail_after_manifest), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)

                def fail_gate(root: Path) -> None:
                    if (load_manifest(root) is not None) == fail_after_manifest:
                        raise subprocess.CalledProcessError(1, ["python3", "tools/wiki/cli.py", "lint", "--report"])

                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

                self.assertEqual(before, _tree_digest(target))

    def test_mode_only_apply_repairs_and_failed_apply_restores_modes(self):
        cases = (
            ("AGENTS.md", 0o755, "0644"),
            (".github/workflows/release-gate.yml", 0o755, "0644"),
        )
        for relative, drift_mode, desired_mode in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                result = subprocess.run(
                    ["bash", "init-vault.sh", str(target)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    0,
                    result.returncode,
                    result.stdout + result.stderr,
                )
                managed = target / relative
                managed.chmod(drift_mode)
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)
                action = next(
                    action for action in plan.actions if action.path == relative
                )
                self.assertEqual("replace", action.action)
                self.assertEqual(action.current_sha256, action.desired_sha256)

                def fail_gate(_: Path) -> None:
                    raise subprocess.CalledProcessError(
                        1,
                        ["python3", "tools/wiki/cli.py", "lint", "--report"],
                    )

                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)
                self.assertEqual(before, _tree_digest(target))

                fresh = audit_vault(ROOT, target)
                apply_plan(ROOT, target, fresh, run_gate=lambda _: None)
                self.assertEqual(desired_mode, applying._observed_mode(managed))

    def test_apply_rejects_content_edit_between_verification_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            managed = target / "AGENTS.md"
            concurrent_contents = b"concurrent user edit\n"
            original_prepare = applying._prepare_journal

            def edit_then_prepare(
                resolved_target: Path,
                checked_plan: MigrationPlan,
                actions: tuple[MigrationAction, ...],
            ) -> tuple[Path, dict[str, object]]:
                managed.write_bytes(concurrent_contents)
                return original_prepare(
                    resolved_target,
                    checked_plan,
                    actions,
                )

            with mock.patch.object(
                applying,
                "_prepare_journal",
                side_effect=edit_then_prepare,
            ):
                with self.assertRaisesRegex(ValueError, "changed after audit"):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertEqual(concurrent_contents, managed.read_bytes())
            self.assertFalse(
                (target / ".vault-builder" / "recovery").exists()
            )

    def test_apply_rejects_mode_change_during_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            managed = target / "AGENTS.md"

            def change_mode(stage: str, relative: str) -> None:
                if stage == "after-open" and relative == "AGENTS.md":
                    managed.chmod(0o600)

            with mock.patch.object(
                applying,
                "_snapshot_step",
                side_effect=change_mode,
                create=True,
            ):
                with self.assertRaisesRegex(ValueError, "changed after audit"):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertEqual("0600", applying._observed_mode(managed))
            self.assertFalse(
                (target / ".vault-builder" / "recovery").exists()
            )

    def test_apply_revalidates_all_snapshots_before_first_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            managed = target / "AGENTS.md"
            concurrent_contents = b"edit after snapshot\n"
            original_prepare = applying._prepare_journal

            def edit_after_snapshot(
                resolved_target: Path,
                checked_plan: MigrationPlan,
                actions: tuple[MigrationAction, ...],
            ) -> tuple[Path, dict[str, object]]:
                prepared = original_prepare(
                    resolved_target,
                    checked_plan,
                    actions,
                )
                managed.write_bytes(concurrent_contents)
                return prepared

            with mock.patch.object(
                applying,
                "_prepare_journal",
                side_effect=edit_after_snapshot,
            ):
                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertEqual(concurrent_contents, managed.read_bytes())
            self.assertFalse(
                (target / ".vault-builder" / "recovery").exists()
            )

    def test_managed_publication_revalidates_destination_inside_publish_boundary(self):
        cases = (
            ("replace", "AGENTS.md"),
            ("create", ".agents/skills/wiki-ingest/SKILL.md"),
        )
        for action_name, relative in cases:
            with self.subTest(action=action_name), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                if action_name == "create":
                    shutil.rmtree(target / ".agents")
                plan = audit_vault(ROOT, target)
                action = next(item for item in plan.actions if item.path == relative)
                self.assertEqual(action_name, action.action)
                destination = (target / relative).resolve(strict=False)
                external_contents = f"external {action_name} writer\n".encode("utf-8")
                injected = False

                def publish_external_writer(stage: str, path: Path) -> None:
                    nonlocal injected
                    if (
                        not injected
                        and stage == "before-destination-validation"
                        and path == destination
                    ):
                        injected = True
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(external_contents)
                        destination.chmod(0o600)

                with mock.patch.object(
                    applying,
                    "_publication_step",
                    side_effect=publish_external_writer,
                    create=True,
                ):
                    with self.assertRaises(applying.MigrationApplyError):
                        apply_plan(
                            ROOT,
                            target,
                            plan,
                            run_gate=lambda _: None,
                        )

                self.assertTrue(injected)
                self.assertEqual(external_contents, destination.read_bytes())
                self.assertEqual("0600", applying._observed_mode(destination))
                self.assertTrue(
                    (
                        target
                        / ".vault-builder"
                        / "recovery"
                        / plan.migration_id
                        / "journal.json"
                    ).is_file()
                )

    def test_transactional_gate_failure_preserves_command_exit_and_rollback_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            def fail_gate(_: Path) -> None:
                raise subprocess.CalledProcessError(7, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with self.assertRaises(applying.MigrationApplyError) as raised:
                apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertTrue(failure.rollback_completed)
            self.assertEqual("python3 tools/wiki/cli.py lint --report", failure.failed_command)
            self.assertEqual(7, failure.exit_code)
            self.assertEqual(
                f"{_PYTHON} tools/vault_migration/cli.py audit --target {target.resolve()}",
                failure.audit_command,
            )
            self.assertIsNone(failure.recover_command)
            self.assertEqual(before, _tree_digest(target))

    def test_transactional_noncommand_failure_reports_completed_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            with mock.patch.object(applying, "_verify_candidate_files", side_effect=ValueError("validation exploded")):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            failure = raised.exception
            self.assertTrue(failure.rollback_completed)
            self.assertIsNone(failure.failed_command)
            self.assertIsNone(failure.exit_code)
            self.assertIn("validation exploded", str(failure))
            self.assertEqual(before, _tree_digest(target))

    def test_transactional_rollback_failure_preserves_original_failure_and_recover_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)

            def fail_gate(_: Path) -> None:
                raise subprocess.CalledProcessError(9, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with mock.patch.object(applying, "_rollback", side_effect=OSError("rollback exploded")):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertFalse(failure.rollback_completed)
            self.assertEqual("python3 tools/wiki/cli.py lint --report", failure.failed_command)
            self.assertEqual(9, failure.exit_code)
            self.assertEqual(
                f"{_PYTHON} tools/vault_migration/cli.py recover --target {target.resolve()} --migration-id {plan.migration_id}",
                failure.recover_command,
            )
            self.assertIsNone(failure.audit_command)
            self.assertTrue((target / ".vault-builder" / "recovery" / plan.migration_id).is_dir())

    def test_rollback_cleanup_fsync_failure_remains_recoverable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            journal_root = target.resolve() / ".vault-builder" / "recovery" / plan.migration_id
            original = journal_root / "originals" / "AGENTS.md"
            tombstone = applying._cleanup_tombstone(
                journal_root,
                recovery_existed=False,
                rolled_back=True,
                state_existed=False,
            )
            tombstone_original = tombstone / "originals" / "AGENTS.md"
            real_fsync = applying._fsync_directory
            failed = False
            gate_failed = False

            def fail_after_original_unlink(path: Path) -> None:
                nonlocal failed
                if (
                    gate_failed
                    and not failed
                    and (
                        (path == original.parent and not original.exists())
                        or (
                            path == tombstone_original.parent
                            and not tombstone_original.exists()
                        )
                    )
                ):
                    failed = True
                    raise OSError(errno.EIO, "injected rollback cleanup fsync")
                real_fsync(path)

            def fail_gate(_: Path) -> None:
                nonlocal gate_failed
                gate_failed = True
                raise subprocess.CalledProcessError(13, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with mock.patch.object(
                applying,
                "_fsync_directory",
                side_effect=fail_after_original_unlink,
            ):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertTrue(failed)
            self.assertTrue(failure.rollback_completed)
            self.assertTrue(failure.rollback_cleanup_pending, str(failure))
            self.assertIn(
                f"recover --target {target.resolve()} --migration-id {plan.migration_id}",
                failure.recover_command,
            )

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_rollback_ancestry_fsync_failure_keeps_terminal_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            journal_root = target.resolve() / ".vault-builder" / "recovery" / plan.migration_id
            tombstone = applying._cleanup_tombstone(
                journal_root,
                recovery_existed=False,
                rolled_back=True,
                state_existed=False,
            )
            state_directory = target.resolve() / ".vault-builder"
            real_fsync = applying._fsync_directory
            failed = False
            gate_failed = False

            def fail_removed_state_parent(path: Path) -> None:
                nonlocal failed
                if (
                    gate_failed
                    and path == target.resolve()
                    and not state_directory.exists()
                    and not failed
                ):
                    failed = True
                    raise OSError(errno.EIO, "injected rollback ancestry fsync")
                real_fsync(path)

            def fail_gate(_: Path) -> None:
                nonlocal gate_failed
                gate_failed = True
                raise subprocess.CalledProcessError(14, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with mock.patch.object(
                applying,
                "_fsync_directory",
                side_effect=fail_removed_state_parent,
            ):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertTrue(failed)
            self.assertTrue(failure.rollback_completed)
            self.assertTrue(failure.rollback_cleanup_pending)
            self.assertTrue(tombstone.is_dir())

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_precondition_failure_is_not_reported_as_a_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            (target / "tools" / "wiki" / "cli.py").write_text("changed after audit\n")

            with self.assertRaisesRegex(ValueError, "changed after audit") as raised:
                apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertNotIsInstance(raised.exception, getattr(applying, "MigrationApplyError", ()))
            self.assertFalse((target / ".vault-builder" / "recovery").exists())

    def test_journal_preparation_failure_reports_completed_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            with mock.patch.object(
                applying,
                "_journal_prepare_step",
                side_effect=OSError("prepared journal failure"),
                create=True,
            ):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            failure = raised.exception
            self.assertTrue(failure.rollback_completed)
            self.assertIn("prepared journal failure", str(failure))
            self.assertIn("audit --target", failure.audit_command)
            self.assertIsNone(failure.recover_command)
            self.assertEqual(before, _tree_digest(target))

    def test_journal_preparation_cleanup_failure_preserves_original_and_recover_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            with (
                mock.patch.object(
                    applying,
                    "_journal_prepare_step",
                    side_effect=OSError("prepared journal failure"),
                    create=True,
                ),
                mock.patch.object(
                    applying,
                    "_cleanup_preparing",
                    side_effect=OSError("journal cleanup failure"),
                ),
            ):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            failure = raised.exception
            self.assertFalse(failure.rollback_completed)
            self.assertIn("prepared journal failure", str(failure))
            self.assertIn(
                f"recover --target {target.resolve()} --migration-id {plan.migration_id}",
                failure.recover_command,
            )
            self.assertTrue((target / ".vault-builder" / "recovery" / plan.migration_id).is_dir())
            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_preparation_backup_hard_stop_has_durable_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            victim_bytes = (target / ".gitignore").read_bytes()

            child = os.fork()
            if child == 0:
                real_write_all = applying._write_all

                def hard_stop_first_backup(
                    descriptor: int,
                    contents: bytes,
                ) -> None:
                    if contents == victim_bytes:
                        os.write(descriptor, contents[: max(1, len(contents) // 2)])
                        os.fsync(descriptor)
                        os._exit(59)
                    real_write_all(descriptor, contents)

                with mock.patch.object(
                    applying,
                    "_write_all",
                    side_effect=hard_stop_first_backup,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(59, os.WEXITSTATUS(child_status))
            journal_root = (
                target / ".vault-builder" / "recovery" / plan.migration_id
            )
            journal = json.loads((journal_root / "journal.json").read_text())
            self.assertEqual("preparing", journal["state"])

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_preparation_bootstrap_hard_stop_has_durable_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                def hard_stop(stage: str) -> None:
                    if stage == "after-root":
                        os._exit(60)

                with mock.patch.object(
                    applying,
                    "_journal_bootstrap_step",
                    side_effect=hard_stop,
                    create=True,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(60, os.WEXITSTATUS(child_status))
            preparing_root = applying._preparing_journal_root(
                target.resolve(),
                plan.migration_id,
                recovery_existed=False,
                state_existed=False,
                create=False,
            )
            self.assertTrue(preparing_root.is_dir())
            self.assertFalse((preparing_root / "journal.json").exists())

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_partial_preparing_journal_has_durable_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                first_write = True

                def hard_stop_first_write(
                    descriptor: int,
                    contents: bytes,
                ) -> None:
                    nonlocal first_write
                    if first_write:
                        first_write = False
                        os.write(descriptor, contents[: max(1, len(contents) // 2)])
                        os.fsync(descriptor)
                        os._exit(61)
                    applying._write_all(descriptor, contents)

                with mock.patch.object(
                    applying,
                    "_write_all",
                    side_effect=hard_stop_first_write,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(61, os.WEXITSTATUS(child_status))
            preparing_root = applying._preparing_journal_root(
                target.resolve(),
                plan.migration_id,
                recovery_existed=False,
                state_existed=False,
                create=False,
            )
            self.assertTrue((preparing_root / ".journal.json.next").is_file())
            self.assertFalse((preparing_root / "journal.json").exists())

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_preparing_root_recovery_publication_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            preparing_root = applying._preparing_journal_root(
                target.resolve(),
                plan.migration_id,
                recovery_existed=False,
                state_existed=False,
                create=False,
            )
            journal_root = (
                target.resolve()
                / ".vault-builder"
                / "recovery"
                / plan.migration_id
            )

            child = os.fork()
            if child == 0:
                def stop_after_root(stage: str) -> None:
                    if stage == "after-root":
                        os._exit(62)

                with mock.patch.object(
                    applying,
                    "_journal_bootstrap_step",
                    side_effect=stop_after_root,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)
            _, child_status = os.waitpid(child, 0)
            self.assertEqual(62, os.WEXITSTATUS(child_status))

            child = os.fork()
            if child == 0:
                real_replace = applying.os.replace

                def stop_before_recovery_publication(
                    source: Path,
                    destination: Path,
                ) -> None:
                    if (
                        Path(source) == preparing_root
                        and Path(destination) == journal_root
                    ):
                        os._exit(63)
                    real_replace(source, destination)

                with mock.patch.object(
                    applying.os,
                    "replace",
                    side_effect=stop_before_recovery_publication,
                ):
                    recover_migration(target, plan.migration_id)
                os._exit(99)
            _, child_status = os.waitpid(child, 0)
            self.assertEqual(63, os.WEXITSTATUS(child_status))
            journal = json.loads((preparing_root / "journal.json").read_text())
            self.assertEqual("preparation-cleanup-pending", journal["state"])

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))

    def test_apply_rejects_changed_builder_bytes_before_journal_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            before = _tree_digest(target)
            original_hash = applying.sha256_file

            def changed_builder_hash(path: Path) -> str:
                if path == ROOT / "tools/wiki/cli.py":
                    return "0" * 64
                return original_hash(path)

            with mock.patch("tools.vault_migration.applying.sha256_file", side_effect=changed_builder_hash):
                with self.assertRaisesRegex(ValueError, "desired builder files"):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".vault-builder").exists())

    def test_apply_rolls_back_when_candidate_manifest_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            before = _tree_digest(target)

            with mock.patch(
                "tools.vault_migration.applying._publish_candidate_manifest",
                side_effect=ValueError("manifest write failed"),
            ):
                with self.assertRaisesRegex(applying.MigrationApplyError, "manifest write failed"):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))

    def test_apply_never_overwrites_manifest_newer_than_journal_precondition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            managed = target / "AGENTS.md"
            managed.chmod(0o600)
            plan = audit_vault(ROOT, target)
            current = load_manifest(target)
            assert current is not None
            later = VaultManifest(
                schema_version=current.schema_version,
                contract_version=current.contract_version,
                builder_revision=current.builder_revision,
                generated_at=current.generated_at,
                managed_files=current.managed_files,
                last_migration_id="f" * 16,
            )
            original_record = applying._record_manifest_publication_intent

            def publish_later_then_record(
                resolved_target: Path,
                journal_root: Path,
                journal: dict[str, object],
                candidate_sha256: str,
            ) -> None:
                write_manifest(resolved_target, later)
                original_record(
                    resolved_target,
                    journal_root,
                    journal,
                    candidate_sha256,
                )

            with mock.patch.object(
                applying,
                "_record_manifest_publication_intent",
                side_effect=publish_later_then_record,
            ):
                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertEqual(later, load_manifest(target))
            journal_root = (
                target
                / ".vault-builder"
                / "recovery"
                / plan.migration_id
            )
            self.assertTrue((journal_root / "journal.json").is_file())

    def test_publication_uses_stable_parent_descriptor_after_parent_symlink_swap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            journal = root / "journal"
            parent = root / "target" / "managed"
            outside = root / "outside"
            journal.mkdir()
            parent.mkdir(parents=True)
            outside.mkdir()
            destination = parent / "file.md"
            destination.write_bytes(b"prior\n")
            staged = journal / "candidate.md"
            candidate = b"candidate\n"
            applying._create_staged_file(
                journal,
                staged,
                candidate,
                "0644",
            )
            expected = applying._capture_destination_state(destination)
            moved_parent = root / "moved-managed"
            injected = False

            def swap_parent(stage: str, path: Path) -> None:
                nonlocal injected
                if stage == "after-destination-validation" and path == destination:
                    injected = True
                    parent.rename(moved_parent)
                    parent.symlink_to(outside, target_is_directory=True)

            with mock.patch.object(
                applying,
                "_publication_step",
                side_effect=swap_parent,
            ):
                applying._publish_staged_file(
                    staged,
                    destination,
                    hashlib.sha256(candidate).hexdigest(),
                    "0644",
                    expected,
                )

            self.assertTrue(injected)
            self.assertEqual(candidate, (moved_parent / "file.md").read_bytes())
            self.assertFalse((outside / "file.md").exists())

    def test_descriptor_open_does_not_follow_parent_swapped_during_walk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            journal = root / "journal"
            parent = root / "target" / "managed"
            outside = root / "outside"
            journal.mkdir()
            parent.mkdir(parents=True)
            outside.mkdir()
            destination = parent / "file.md"
            destination.write_bytes(b"prior\n")
            outside_destination = outside / destination.name
            outside_destination.write_bytes(b"prior\n")
            staged = journal / "candidate.md"
            candidate = b"candidate\n"
            applying._create_staged_file(
                journal,
                staged,
                candidate,
                "0644",
            )
            expected = applying._capture_destination_state(destination)
            moved_parent = root / "moved-managed"
            canonical_parent = parent.resolve()
            real_resolve = Path.resolve
            swapped = False

            def swap_once(path: Path) -> None:
                nonlocal swapped
                if not swapped and path in {parent, canonical_parent}:
                    swapped = True
                    parent.rename(moved_parent)
                    parent.symlink_to(outside, target_is_directory=True)

            def resolve_with_swap(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> Path:
                swap_once(path)
                return real_resolve(path, *args, **kwargs)

            def walk_with_swap(path: Path) -> None:
                swap_once(path)

            with (
                mock.patch.object(
                    Path,
                    "resolve",
                    autospec=True,
                    side_effect=resolve_with_swap,
                ),
                mock.patch.object(
                    applying,
                    "_directory_open_step",
                    side_effect=walk_with_swap,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "unsafe|unreadable"):
                    applying._publish_staged_file(
                        staged,
                        destination,
                        hashlib.sha256(candidate).hexdigest(),
                        "0644",
                        expected,
                    )

            self.assertTrue(swapped)
            self.assertEqual(b"prior\n", (moved_parent / destination.name).read_bytes())
            self.assertEqual(b"prior\n", outside_destination.read_bytes())

    def test_publication_preserves_external_writer_at_final_exchange_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            journal = root / "journal"
            parent = root / "target"
            journal.mkdir()
            parent.mkdir()
            destination = parent / "file.md"
            destination.write_bytes(b"prior\n")
            staged = journal / "candidate.md"
            candidate = b"candidate\n"
            external = b"external writer\n"
            applying._create_staged_file(
                journal,
                staged,
                candidate,
                "0644",
            )
            expected = applying._capture_destination_state(destination)
            injected = False

            def replace_after_validation(stage: str, path: Path) -> None:
                nonlocal injected
                if stage == "after-destination-validation" and path == destination:
                    injected = True
                    replacement = parent / "external.tmp"
                    replacement.write_bytes(external)
                    replacement.chmod(0o600)
                    os.replace(replacement, destination)

            with mock.patch.object(
                applying,
                "_publication_step",
                side_effect=replace_after_validation,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "changed at publication boundary",
                ):
                    applying._publish_staged_file(
                        staged,
                        destination,
                        hashlib.sha256(candidate).hexdigest(),
                        "0644",
                        expected,
                    )

            self.assertTrue(injected)
            self.assertEqual(external, destination.read_bytes())
            self.assertEqual("0600", applying._observed_mode(destination))
            self.assertEqual(candidate, staged.read_bytes())

    def test_candidate_manifest_preserves_final_boundary_external_writer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            (target / "AGENTS.md").chmod(0o600)
            plan = audit_vault(ROOT, target)
            current = load_manifest(target)
            assert current is not None
            later = VaultManifest(
                current.schema_version,
                current.contract_version,
                current.builder_revision,
                current.generated_at,
                current.managed_files,
                "c" * 16,
            )
            manifest_path = (
                target.resolve() / ".vault-builder" / "manifest.json"
            )
            injected = False

            def publish_external_manifest(stage: str, path: Path) -> None:
                nonlocal injected
                if (
                    not injected
                    and stage == "after-destination-validation"
                    and path == manifest_path
                ):
                    injected = True
                    write_manifest(target, later)

            with mock.patch.object(
                applying,
                "_publication_step",
                side_effect=publish_external_manifest,
            ):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertTrue(injected)
            self.assertFalse(raised.exception.rollback_completed)
            self.assertEqual(later, load_manifest(target))
            self.assertTrue(
                (
                    target
                    / ".vault-builder"
                    / "recovery"
                    / plan.migration_id
                    / "journal.json"
                ).is_file()
            )

    def test_rollback_preserves_final_boundary_external_writer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            destination = target.resolve() / "AGENTS.md"
            desired = (ROOT / "AGENTS.md").read_bytes()
            external = b"external rollback writer\n"
            injected = False

            def publish_external_managed(stage: str, path: Path) -> None:
                nonlocal injected
                if (
                    not injected
                    and stage == "after-destination-validation"
                    and path == destination
                    and destination.read_bytes() == desired
                ):
                    injected = True
                    replacement = destination.with_name(".external-agents")
                    replacement.write_bytes(external)
                    replacement.chmod(0o600)
                    os.replace(replacement, destination)

            def fail_gate(_: Path) -> None:
                raise ValueError("force rollback")

            with mock.patch.object(
                applying,
                "_publication_step",
                side_effect=publish_external_managed,
            ):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

            self.assertTrue(injected)
            self.assertFalse(raised.exception.rollback_completed)
            self.assertEqual(external, destination.read_bytes())
            self.assertEqual("0600", applying._observed_mode(destination))
            self.assertTrue(
                (
                    target
                    / ".vault-builder"
                    / "recovery"
                    / plan.migration_id
                    / "journal.json"
                ).is_file()
            )

    def test_recovery_preserves_managed_external_writer_captured_by_hard_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            destination = target.resolve() / "AGENTS.md"
            external = b"external managed writer\n"
            exit_code = 88

            child = os.fork()
            if child == 0:
                injected = False

                def stop_after_exchange(stage: str, path: Path) -> None:
                    nonlocal injected
                    if path != destination:
                        return
                    if stage == "after-destination-validation" and not injected:
                        replacement = destination.with_name(".external-managed")
                        replacement.write_bytes(external)
                        replacement.chmod(0o600)
                        os.replace(replacement, destination)
                        injected = True
                    elif stage == "after-exchange" and injected:
                        os._exit(exit_code)

                with mock.patch.object(
                    applying,
                    "_publication_step",
                    side_effect=stop_after_exchange,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(exit_code, os.WEXITSTATUS(status))

            with self.assertRaisesRegex(ValueError, "external|unknown"):
                recover_migration(target, plan.migration_id)
            self.assertEqual(external, destination.read_bytes())
            self.assertEqual("0600", applying._observed_mode(destination))

    def test_recovery_preserves_manifest_external_writer_captured_by_hard_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            (target / "AGENTS.md").chmod(0o600)
            plan = audit_vault(ROOT, target)
            current = load_manifest(target)
            assert current is not None
            later = VaultManifest(
                current.schema_version,
                current.contract_version,
                current.builder_revision,
                current.generated_at,
                current.managed_files,
                "e" * 16,
            )
            destination = (
                target.resolve() / ".vault-builder" / "manifest.json"
            )
            exit_code = 89

            child = os.fork()
            if child == 0:
                injected = False

                def stop_after_exchange(stage: str, path: Path) -> None:
                    nonlocal injected
                    if path != destination:
                        return
                    if stage == "after-destination-validation" and not injected:
                        write_manifest(target, later)
                        injected = True
                    elif stage == "after-exchange" and injected:
                        os._exit(exit_code)

                with mock.patch.object(
                    applying,
                    "_publication_step",
                    side_effect=stop_after_exchange,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(exit_code, os.WEXITSTATUS(status))

            with self.assertRaisesRegex(ValueError, "external|unknown"):
                recover_migration(target, plan.migration_id)
            self.assertEqual(later, load_manifest(target))

    def test_recovery_preserves_rollback_external_writer_captured_by_hard_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            destination = target.resolve() / "AGENTS.md"
            desired = (ROOT / "AGENTS.md").read_bytes()
            external = b"external rollback writer after crash\n"
            exit_code = 90

            child = os.fork()
            if child == 0:
                injected = False

                def stop_after_exchange(stage: str, path: Path) -> None:
                    nonlocal injected
                    if path != destination:
                        return
                    if (
                        stage == "after-destination-validation"
                        and not injected
                        and destination.read_bytes() == desired
                    ):
                        replacement = destination.with_name(".external-rollback")
                        replacement.write_bytes(external)
                        replacement.chmod(0o600)
                        os.replace(replacement, destination)
                        injected = True
                    elif stage == "after-exchange" and injected:
                        os._exit(exit_code)

                with mock.patch.object(
                    applying,
                    "_publication_step",
                    side_effect=stop_after_exchange,
                ):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: (_ for _ in ()).throw(
                            ValueError("force rollback")
                        ),
                    )
                os._exit(99)

            _, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(exit_code, os.WEXITSTATUS(status))

            with self.assertRaisesRegex(ValueError, "external|unknown"):
                recover_migration(target, plan.migration_id)
            self.assertEqual(external, destination.read_bytes())
            self.assertEqual("0600", applying._observed_mode(destination))

    def test_rollback_quarantines_external_replacements_of_created_files(self):
        cases = ("managed", "manifest")
        for boundary in cases:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                plan = audit_vault(ROOT, target)
                if boundary == "managed":
                    destination = (
                        target.resolve()
                        / "tools"
                        / "wiki"
                        / "lib"
                        / "authority.py"
                    )
                    gate = lambda _: (_ for _ in ()).throw(
                        ValueError("force managed rollback")
                    )
                else:
                    destination = (
                        target.resolve()
                        / ".vault-builder"
                        / "manifest.json"
                    )
                    calls = 0

                    def gate(_: Path) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise ValueError("force manifest rollback")

                external = f"external {boundary} delete writer\n".encode()
                real_unlink = Path.unlink
                injected = False

                def replace_before_delete(path: Path) -> None:
                    nonlocal injected
                    if path == destination and not injected:
                        replacement = destination.with_name(
                            f".external-{boundary}-delete"
                        )
                        replacement.write_bytes(external)
                        replacement.chmod(0o600)
                        os.replace(replacement, destination)
                        injected = True

                def unlink_with_replacement(
                    path: Path,
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    replace_before_delete(path)
                    real_unlink(path, *args, **kwargs)

                with (
                    mock.patch.object(
                        Path,
                        "unlink",
                        autospec=True,
                        side_effect=unlink_with_replacement,
                    ),
                    mock.patch.object(
                        applying,
                        "_deletion_step",
                        side_effect=replace_before_delete,
                        create=True,
                    ),
                ):
                    with self.assertRaises(applying.MigrationApplyError) as raised:
                        apply_plan(ROOT, target, plan, run_gate=gate)

                self.assertTrue(injected)
                self.assertFalse(raised.exception.rollback_completed)
                self.assertEqual(external, destination.read_bytes())
                self.assertEqual("0600", applying._observed_mode(destination))

    def test_recovery_restores_external_writer_from_interrupted_deletion_quarantine(self):
        cases = ("managed", "manifest")
        for index, boundary in enumerate(cases):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                plan = audit_vault(ROOT, target)
                if boundary == "managed":
                    destination = (
                        target.resolve()
                        / "tools"
                        / "wiki"
                        / "lib"
                        / "authority.py"
                    )
                    gate = lambda _: (_ for _ in ()).throw(
                        ValueError("force managed rollback")
                    )
                else:
                    destination = (
                        target.resolve()
                        / ".vault-builder"
                        / "manifest.json"
                    )
                    calls = 0

                    def gate(_: Path) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise ValueError("force manifest rollback")

                external = f"external interrupted {boundary} writer\n".encode()
                exit_code = 92 + index
                child = os.fork()
                if child == 0:
                    injected = False
                    real_move = applying._move_directory_entry_no_replace

                    def replace_before_delete(path: Path) -> None:
                        nonlocal injected
                        if path == destination and not injected:
                            replacement = destination.with_name(
                                f".external-interrupted-{boundary}"
                            )
                            replacement.write_bytes(external)
                            replacement.chmod(0o600)
                            os.replace(replacement, destination)
                            injected = True

                    def stop_after_quarantine(
                        source_parent: int,
                        source_name: str,
                        destination_parent: int,
                        destination_name: str,
                    ) -> None:
                        real_move(
                            source_parent,
                            source_name,
                            destination_parent,
                            destination_name,
                        )
                        if injected:
                            os._exit(exit_code)

                    with (
                        mock.patch.object(
                            applying,
                            "_deletion_step",
                            side_effect=replace_before_delete,
                        ),
                        mock.patch.object(
                            applying,
                            "_move_directory_entry_no_replace",
                            side_effect=stop_after_quarantine,
                        ),
                    ):
                        apply_plan(ROOT, target, plan, run_gate=gate)
                    os._exit(99)

                _, status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(exit_code, os.WEXITSTATUS(status))
                with self.assertRaisesRegex(ValueError, "external|unknown"):
                    recover_migration(target, plan.migration_id)
                self.assertEqual(external, destination.read_bytes())
                self.assertEqual("0600", applying._observed_mode(destination))

    def test_candidate_publication_rechecks_manifest_precondition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            (target / "AGENTS.md").chmod(0o600)
            plan = audit_vault(ROOT, target)
            current = load_manifest(target)
            assert current is not None
            later = VaultManifest(
                schema_version=current.schema_version,
                contract_version=current.contract_version,
                builder_revision=current.builder_revision,
                generated_at=current.generated_at,
                managed_files=current.managed_files,
                last_migration_id="e" * 16,
            )

            def publish_later_before_candidate(stage: str) -> None:
                if stage == "before-publication":
                    write_manifest(target, later)

            with mock.patch.object(
                applying,
                "_manifest_step",
                side_effect=publish_later_before_candidate,
            ):
                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertEqual(later, load_manifest(target))
            journal_root = (
                target
                / ".vault-builder"
                / "recovery"
                / plan.migration_id
            )
            self.assertTrue((journal_root / "journal.json").is_file())

    def test_candidate_publication_revalidates_manifest_inside_publish_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            (target / "AGENTS.md").chmod(0o600)
            plan = audit_vault(ROOT, target)
            current = load_manifest(target)
            assert current is not None
            later = VaultManifest(
                schema_version=current.schema_version,
                contract_version=current.contract_version,
                builder_revision=current.builder_revision,
                generated_at=current.generated_at,
                managed_files=current.managed_files,
                last_migration_id="d" * 16,
            )
            manifest_path = (
                target / ".vault-builder" / "manifest.json"
            ).resolve(strict=False)
            injected = False

            def publish_later_manifest(stage: str, path: Path) -> None:
                nonlocal injected
                if (
                    not injected
                    and stage == "before-destination-validation"
                    and path == manifest_path
                ):
                    injected = True
                    write_manifest(target, later)

            with mock.patch.object(
                applying,
                "_publication_step",
                side_effect=publish_later_manifest,
                create=True,
            ):
                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(
                        ROOT,
                        target,
                        plan,
                        run_gate=lambda _: None,
                    )

            self.assertTrue(injected)
            self.assertEqual(later, load_manifest(target))
            self.assertTrue(
                (
                    target
                    / ".vault-builder"
                    / "recovery"
                    / plan.migration_id
                    / "journal.json"
                ).is_file()
            )

    def test_candidate_manifest_write_ahead_crash_windows_recover_exactly(self):
        for stop_stage, exit_code in (("before-publication", 31), ("after-publication", 32)):
            with self.subTest(stop_stage=stop_stage), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)

                child = os.fork()
                if child == 0:
                    def hard_stop(stage: str) -> None:
                        if stage == stop_stage:
                            os._exit(exit_code)

                    with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop, create=True):
                        apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                    os._exit(99)

                _, child_status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(child_status))
                self.assertEqual(exit_code, os.WEXITSTATUS(child_status))
                journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
                self.assertTrue((journal_root / "journal.json").is_file())
                journal = json.loads((journal_root / "journal.json").read_text())
                manifest_intent = journal["manifest"]
                self.assertEqual("intent", manifest_intent["publication_state"])
                self.assertRegex(manifest_intent["candidate_sha256"], r"^[0-9a-f]{64}$")
                if stop_stage == "before-publication":
                    self.assertIsNone(load_manifest(target))
                else:
                    self.assertEqual(2, load_manifest(target).contract_version)
                    self.assertEqual(
                        manifest_intent["candidate_sha256"],
                        sha256_file(target / ".vault-builder" / "manifest.json"),
                    )

                recover_migration(target, plan.migration_id)

                self.assertEqual(before, _tree_digest(target))
                self.assertIsNone(load_manifest(target))

    def test_candidate_manifest_crash_recovery_restores_prior_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            prior = VaultManifest(
                schema_version=1,
                contract_version=1,
                builder_revision="prior",
                generated_at="2026-07-30T00:00:00Z",
                managed_files={},
            )
            write_manifest(target, prior)
            prior_bytes = (target / ".vault-builder" / "manifest.json").read_bytes()
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                def hard_stop(stage: str) -> None:
                    if stage == "after-publication":
                        os._exit(33)

                with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(33, os.WEXITSTATUS(child_status))
            self.assertNotEqual(prior_bytes, (target / ".vault-builder" / "manifest.json").read_bytes())

            recover_migration(target, plan.migration_id)

            self.assertEqual(before, _tree_digest(target))
            self.assertEqual(prior_bytes, (target / ".vault-builder" / "manifest.json").read_bytes())

    def test_prior_manifest_backup_write_ahead_crash_recovers_exactly(self):
        cases = (
            ("after-intent", 38, False, False),
            ("after-backup-stage", 56, True, False),
            ("before-backup-publication", 57, True, False),
            ("after-backup-publication", 58, False, True),
            ("after-original", 39, False, True),
        )
        for stop_stage, exit_code, staged_exists, original_exists in cases:
            with self.subTest(stop_stage=stop_stage), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                prior = VaultManifest(
                    schema_version=1,
                    contract_version=1,
                    builder_revision="prior",
                    generated_at="2026-07-30T00:00:00Z",
                    managed_files={},
                )
                write_manifest(target, prior)
                prior_bytes = (
                    target / ".vault-builder" / "manifest.json"
                ).read_bytes()
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)

                child = os.fork()
                if child == 0:
                    def hard_stop(stage: str) -> None:
                        if stage == stop_stage:
                            os._exit(exit_code)

                    with mock.patch.object(applying, "_manifest_backup_step", side_effect=hard_stop):
                        apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                    os._exit(99)

                _, child_status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(child_status))
                self.assertEqual(exit_code, os.WEXITSTATUS(child_status))
                journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
                journal = json.loads((journal_root / "journal.json").read_text())
                self.assertEqual("intent", journal["manifest"]["backup_state"])
                self.assertEqual(
                    "staged/prior-manifest.json",
                    journal["manifest"]["backup_staged_path"],
                )
                backup = journal_root / "originals" / ".vault-builder" / "manifest.json"
                staged = journal_root / "staged" / "prior-manifest.json"
                self.assertEqual(staged_exists, staged.is_file())
                self.assertEqual(original_exists, backup.is_file())
                if backup.is_file():
                    self.assertEqual(prior_bytes, backup.read_bytes())

                recover_migration(target, plan.migration_id)

                self.assertEqual(before, _tree_digest(target))
                self.assertEqual(prior, load_manifest(target))

    def test_prior_manifest_partial_backup_staging_recovers_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            prior = VaultManifest(
                schema_version=1,
                contract_version=1,
                builder_revision="prior",
                generated_at="2026-07-30T00:00:00Z",
                managed_files={},
            )
            write_manifest(target, prior)
            prior_bytes = (target / ".vault-builder" / "manifest.json").read_bytes()
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                real_write_all = applying._write_all

                def hard_stop_during_backup(descriptor: int, contents: bytes) -> None:
                    if contents == prior_bytes:
                        os.write(descriptor, contents[: max(1, len(contents) // 2)])
                        os.fsync(descriptor)
                        os._exit(55)
                    real_write_all(descriptor, contents)

                with mock.patch.object(
                    applying,
                    "_write_all",
                    side_effect=hard_stop_during_backup,
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(55, os.WEXITSTATUS(child_status))
            journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
            journal = json.loads((journal_root / "journal.json").read_text())
            self.assertEqual("intent", journal["manifest"]["backup_state"])
            self.assertEqual(prior, load_manifest(target))

            self.assertEqual("rolled-back", recover_migration(target, plan.migration_id))
            self.assertEqual(before, _tree_digest(target))
            self.assertEqual(prior, load_manifest(target))

    def test_candidate_manifest_recovery_refuses_unknown_current_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                def hard_stop(stage: str) -> None:
                    if stage == "after-publication":
                        os._exit(34)

                with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(34, os.WEXITSTATUS(child_status))
            manifest_path = target / ".vault-builder" / "manifest.json"
            manifest_path.write_text("unknown manifest bytes\n")
            before_recovery = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "unknown Vault builder manifest"):
                recover_migration(target, plan.migration_id)

            self.assertEqual(before_recovery, _tree_digest(target))
            self.assertTrue((target / ".vault-builder" / "recovery" / plan.migration_id).is_dir())

    def test_candidate_manifest_recovery_refuses_unknown_current_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                def hard_stop(stage: str) -> None:
                    if stage == "after-publication":
                        os._exit(54)

                with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(54, os.WEXITSTATUS(child_status))
            manifest_path = target / ".vault-builder" / "manifest.json"
            manifest_path.chmod(0o600)
            before_recovery = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "unknown Vault builder manifest"):
                recover_migration(target, plan.migration_id)

            self.assertEqual(before_recovery, _tree_digest(target))
            self.assertTrue((target / ".vault-builder" / "recovery" / plan.migration_id).is_dir())

    def test_recovery_preflights_all_managed_targets_before_restoring_any(self):
        for mutation in ("unknown-bytes", "unsafe-symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                plan = audit_vault(ROOT, target)

                child = os.fork()
                if child == 0:
                    def hard_stop(stage: str) -> None:
                        if stage == "after-publication":
                            os._exit(36)

                    with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop):
                        apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                    os._exit(99)

                _, child_status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(child_status))
                self.assertEqual(36, os.WEXITSTATUS(child_status))
                journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
                journal = json.loads((journal_root / "journal.json").read_text())
                victim = next(entry for entry in journal["entries"] if entry["existed"])
                victim_path = target / victim["path"]
                if mutation == "unknown-bytes":
                    victim_path.write_text("unknown concurrent bytes\n")
                else:
                    victim_path.unlink()
                    victim_path.symlink_to(target / "AGENTS.md")
                before_recovery = _tree_digest(target)

                with self.assertRaisesRegex(ValueError, "unknown|unsafe"):
                    recover_migration(target, plan.migration_id)

                self.assertEqual(before_recovery, _tree_digest(target))
                self.assertTrue(journal_root.is_dir())

    def test_automatic_rollback_preflights_all_originals_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            plan = audit_vault(ROOT, target)
            before_target: dict[str, tuple[str, str, str]] = {}
            before_journal_artifacts: dict[str, tuple[str, str, str]] = {}
            before_journal: dict[str, object] = {}

            def snapshot(path: Path) -> tuple[str, str, str]:
                state = path.lstat()
                mode = f"{state.st_mode & 0o7777:04o}"
                if path.is_symlink():
                    return ("symlink", mode, os.readlink(path))
                if path.is_file():
                    return ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
                return ("directory", mode, "")

            def fail_gate(_: Path) -> None:
                journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
                journal_path = journal_root / "journal.json"
                journal = json.loads(journal_path.read_text())
                victim = next(entry for entry in journal["entries"] if entry["existed"])
                original = journal_root / "originals" / victim["path"]
                original.write_text("corrupt later original\n")

                before_journal.update(json.loads(journal_path.read_text()))
                for path in sorted(target.rglob("*")):
                    if path == journal_root or journal_root in path.parents:
                        continue
                    before_target[path.relative_to(target).as_posix()] = snapshot(path)
                for path in sorted(journal_root.rglob("*")):
                    if path == journal_path:
                        continue
                    before_journal_artifacts[path.relative_to(journal_root).as_posix()] = snapshot(path)
                raise subprocess.CalledProcessError(12, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with self.assertRaises(applying.MigrationApplyError) as raised:
                apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertFalse(failure.rollback_completed)
            self.assertIn(
                f"recover --target {target.resolve()} --migration-id {plan.migration_id}",
                failure.recover_command,
            )
            journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
            journal_path = journal_root / "journal.json"
            after_journal = json.loads(journal_path.read_text())
            expected_journal = dict(before_journal)
            expected_journal["state"] = "rollback-failed"
            self.assertEqual(expected_journal, after_journal)
            self.assertEqual(
                before_target,
                {
                    path.relative_to(target).as_posix(): snapshot(path)
                    for path in sorted(target.rglob("*"))
                    if path != journal_root and journal_root not in path.parents
                },
            )
            self.assertEqual(
                before_journal_artifacts,
                {
                    path.relative_to(journal_root).as_posix(): snapshot(path)
                    for path in sorted(journal_root.rglob("*"))
                    if path != journal_path
                },
            )
            self.assertFalse((target / ".vault-builder" / "manifest.json").exists())

    def test_candidate_manifest_requires_prior_backup_before_recovery_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            prior = VaultManifest(
                schema_version=1,
                contract_version=1,
                builder_revision="prior",
                generated_at="2026-07-30T00:00:00Z",
                managed_files={},
            )
            write_manifest(target, prior)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                def hard_stop(stage: str) -> None:
                    if stage == "after-publication":
                        os._exit(40)

                with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(40, os.WEXITSTATUS(child_status))
            journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
            journal_path = journal_root / "journal.json"
            journal = json.loads(journal_path.read_text())
            journal["manifest"]["backup_state"] = "intent"
            journal_path.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n")
            (journal_root / "originals" / ".vault-builder" / "manifest.json").unlink()
            before_recovery = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "original is invalid"):
                recover_migration(target, plan.migration_id)

            self.assertEqual(before_recovery, _tree_digest(target))
            self.assertEqual(2, load_manifest(target).contract_version)
            self.assertTrue(journal_root.is_dir())

    def test_recovery_restores_manifest_before_managed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)

            child = os.fork()
            if child == 0:
                def hard_stop(stage: str) -> None:
                    if stage == "after-publication":
                        os._exit(37)

                with mock.patch.object(applying, "_manifest_step", side_effect=hard_stop):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                os._exit(99)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(37, os.WEXITSTATUS(child_status))
            self.assertEqual(2, load_manifest(target).contract_version)

            with mock.patch.object(applying, "_restore_entry", side_effect=OSError("stop during managed restore")):
                with self.assertRaisesRegex(OSError, "stop during managed restore"):
                    recover_migration(target, plan.migration_id)

            self.assertIsNone(load_manifest(target))
            self.assertTrue((target / ".vault-builder" / "recovery" / plan.migration_id).is_dir())

    def test_recover_restores_interrupted_journal_and_prior_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            relative = Path("tools/wiki/cli.py")
            original = (target / relative).read_bytes()
            migration_id = "0123456789abcdef"
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            original_path = journal_root / "originals" / relative
            original_path.parent.mkdir(parents=True, exist_ok=True)
            original_path.write_bytes(original)
            interrupted = b"interrupted replacement\n"
            (target / relative).write_bytes(interrupted)
            (journal_root / "journal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "migration_id": migration_id,
                        "target_contract_version": 1,
                        "state": "applying",
                        "entries": [{
                            "path": relative.as_posix(),
                            "existed": True,
                            "sha256": hashlib.sha256(original).hexdigest(),
                            "mode": "0644",
                            "applied_sha256": hashlib.sha256(interrupted).hexdigest(),
                            "applied_mode": "0644",
                        }],
                        "manifest": {
                            "existed": False,
                            "sha256": None,
                            "mode": None,
                            "backup_state": "not-required",
                            "backup_staged_path": None,
                            "candidate_sha256": "c" * 64,
                            "publication_state": "intent",
                        },
                        "directory_nonce": "b" * 64,
                        "directories": [],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )

            recover_migration(target, migration_id)

            self.assertEqual(original, (target / relative).read_bytes())
            self.assertFalse(journal_root.exists())
            with self.assertRaisesRegex(ValueError, "not found"):
                recover_migration(target, migration_id)

    def test_recover_prepared_journal_at_exact_path_removes_created_parent_chain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            relative = ".agents/skills/wiki-ingest/SKILL.md"
            desired = (ROOT / relative).read_bytes()
            migration_id = "0123456789abcdef"
            nonce = "b" * 64
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            target_path = target / relative
            target_path.parent.mkdir(parents=True)
            target_path.write_bytes(desired)
            (journal_root / "originals").mkdir(parents=True)
            marker_sources = journal_root / "directory-markers"
            marker_sources.mkdir()
            for directory in (target / ".agents", target / ".agents" / "skills", target_path.parent):
                relative_directory = directory.relative_to(target).as_posix()
                marker_source = marker_sources / f"{hashlib.sha256(relative_directory.encode()).hexdigest()}.json"
                marker_source.write_text(
                    json.dumps(
                        {"migration_id": migration_id, "nonce": nonce, "path": relative_directory, "schema_version": 1},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
                marker_source.chmod(0o644)
                os.link(marker_source, directory / ".vault-builder-migration.json")
            (journal_root / "journal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "migration_id": migration_id,
                        "target_contract_version": 1,
                        "state": "prepared",
                        "entries": [{
                            "path": relative,
                            "existed": False,
                            "sha256": None,
                            "mode": None,
                            "applied_sha256": hashlib.sha256(desired).hexdigest(),
                            "applied_mode": "0644",
                        }],
                        "manifest": None,
                        "directory_nonce": nonce,
                        "directories": [
                            {"existed": False, "path": ".agents", "state": "created"},
                            {"existed": False, "path": ".agents/skills", "state": "created"},
                            {"existed": False, "path": ".agents/skills/wiki-ingest", "state": "created"},
                        ],
                        "state_directory_existed": False,
                        "recovery_directory_existed": False,
                    }
                )
            )

            recover_migration(target, migration_id)

            self.assertFalse(target_path.exists())
            self.assertFalse((target / ".agents").exists())
            self.assertFalse(journal_root.exists())

    def test_recover_interrupted_transaction_uses_durable_directory_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            resolved_target = applying._target_root(target)
            checked, actions = applying._verify_preconditions(ROOT, resolved_target, plan)
            journal_root, journal = applying._prepare_journal(resolved_target, checked, actions)
            action = next(item for item in actions if item.path == ".agents/skills/wiki-ingest/SKILL.md")

            def record_directory(relative: str, lifecycle: str) -> None:
                records = journal["directories"]
                if lifecycle == "intent":
                    records.append({"existed": False, "path": relative, "state": "intent"})
                else:
                    next(item for item in records if item["path"] == relative)["state"] = "created"
                applying._write_journal_state(journal_root, journal, "applying")

            applying._ensure_managed_parent(
                resolved_target,
                resolved_target / action.path,
                action.path,
                migration_id=plan.migration_id,
                directory_nonce=journal["directory_nonce"],
                record_directory=record_directory,
            )
            self.assertTrue((target / ".agents" / ".vault-builder-migration.json").is_file())

            recover_migration(target, plan.migration_id)

            self.assertEqual(before, _tree_digest(target))

    def test_directory_write_ahead_crash_windows_recover_exactly(self):
        class StopAfterDirectoryStep(Exception):
            pass

        for stop_stage in ("before-intent", "after-intent", "after-mkdir", "after-marker"):
            with self.subTest(stop_stage=stop_stage), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                shutil.rmtree(target / ".agents")
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)
                resolved_target = applying._target_root(target)
                checked, actions = applying._verify_preconditions(ROOT, resolved_target, plan)
                journal_root, journal = applying._prepare_journal(resolved_target, checked, actions)
                action = next(item for item in actions if item.path == ".agents/skills/wiki-ingest/SKILL.md")

                def record_directory(relative: str, lifecycle: str) -> None:
                    records = journal["directories"]
                    if lifecycle == "intent":
                        records.append({"existed": False, "path": relative, "state": "intent"})
                        records.sort(key=lambda item: item["path"])
                    else:
                        next(item for item in records if item["path"] == relative)["state"] = "created"
                    applying._write_journal_state(journal_root, journal, "applying")

                def stop(stage: str, relative: str) -> None:
                    if stage == stop_stage and relative == ".agents":
                        raise StopAfterDirectoryStep()

                with mock.patch("tools.vault_migration.applying._directory_step", side_effect=stop):
                    with self.assertRaises(StopAfterDirectoryStep):
                        applying._ensure_managed_parent(
                            resolved_target,
                            resolved_target / action.path,
                            action.path,
                            migration_id=plan.migration_id,
                            directory_nonce=journal["directory_nonce"],
                            record_directory=record_directory,
                        )

                recover_migration(target, plan.migration_id)

                self.assertEqual(before, _tree_digest(target))
                self.assertFalse((target / ".agents" / ".vault-builder-migration.json").exists())

    def test_directory_rollback_hard_stops_resume_after_marker_and_rmdir(self):
        class StopDuringDirectoryRollback(Exception):
            pass

        for stop_stage in (
            "after-rollback-marker-removal",
            "after-rollback-directory-removal",
        ):
            with self.subTest(stop_stage=stop_stage), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                (
                    before,
                    plan,
                    resolved_target,
                    journal_root,
                    journal,
                    action,
                    record_directory,
                ) = self._prepare_missing_agents_transaction(target)
                applying._replace_from_builder(
                    ROOT,
                    resolved_target,
                    action,
                    migration_id=plan.migration_id,
                    directory_nonce=str(journal["directory_nonce"]),
                    record_directory=record_directory,
                )
                deepest = ".agents/skills/wiki-ingest"

                def stop(stage: str, relative: str) -> None:
                    if stage == stop_stage and relative == deepest:
                        raise StopDuringDirectoryRollback()

                with mock.patch.object(
                    applying,
                    "_directory_step",
                    side_effect=stop,
                ):
                    with self.assertRaises(StopDuringDirectoryRollback):
                        applying._rollback(
                            resolved_target,
                            journal_root,
                            journal,
                        )

                persisted = json.loads((journal_root / "journal.json").read_text())
                self.assertEqual("rolling-back", persisted["state"])
                self.assertEqual(
                    "rolled-back",
                    recover_migration(target, plan.migration_id),
                )
                self.assertEqual(before, _tree_digest(target))
                with self.assertRaisesRegex(ValueError, "not found"):
                    recover_migration(target, plan.migration_id)

    def test_directory_marker_partial_write_never_reaches_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            resolved_target = applying._target_root(target)
            checked, actions = applying._verify_preconditions(ROOT, resolved_target, plan)
            journal_root, journal = applying._prepare_journal(resolved_target, checked, actions)
            action = next(item for item in actions if item.path == ".agents/skills/wiki-ingest/SKILL.md")

            def record_directory(relative: str, lifecycle: str) -> None:
                records = journal["directories"]
                if lifecycle == "intent":
                    records.append({"existed": False, "path": relative, "state": "intent"})
                    records.sort(key=lambda item: item["path"])
                else:
                    next(item for item in records if item["path"] == relative)["state"] = "created"
                applying._write_journal_state(journal_root, journal, "applying")

            child = os.fork()
            if child == 0:
                def terminate_after_partial_write(descriptor: int, *_args: object, **_kwargs: object) -> None:
                    os.write(descriptor, b'{"partial"')
                    os.fsync(descriptor)
                    os._exit(23)

                with mock.patch.object(applying.os, "fdopen", side_effect=terminate_after_partial_write):
                    applying._ensure_managed_parent(
                        resolved_target,
                        resolved_target / action.path,
                        action.path,
                        migration_id=plan.migration_id,
                        directory_nonce=journal["directory_nonce"],
                        record_directory=record_directory,
                    )
                os._exit(24)

            _, child_status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(child_status))
            self.assertEqual(23, os.WEXITSTATUS(child_status))
            self.assertFalse((target / ".agents" / ".vault-builder-migration.json").exists())

            recover_migration(target, plan.migration_id)

            self.assertEqual(before, _tree_digest(target))

    def test_directory_marker_mode_and_recovery_ignore_restrictive_umask(self):
        class StopAfterMarkerPublication(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            resolved_target = applying._target_root(target)
            checked, actions = applying._verify_preconditions(ROOT, resolved_target, plan)
            journal_root, journal = applying._prepare_journal(resolved_target, checked, actions)
            action = next(item for item in actions if item.path == ".agents/skills/wiki-ingest/SKILL.md")

            def record_directory(relative: str, lifecycle: str) -> None:
                records = journal["directories"]
                if lifecycle == "intent":
                    records.append({"existed": False, "path": relative, "state": "intent"})
                    records.sort(key=lambda item: item["path"])
                else:
                    next(item for item in records if item["path"] == relative)["state"] = "created"
                applying._write_journal_state(journal_root, journal, "applying")

            def stop_after_marker(stage: str, relative: str) -> None:
                if stage == "after-marker" and relative == ".agents":
                    raise StopAfterMarkerPublication()

            previous_umask = os.umask(0o077)
            try:
                with mock.patch("tools.vault_migration.applying._directory_step", side_effect=stop_after_marker):
                    with self.assertRaises(StopAfterMarkerPublication):
                        applying._ensure_managed_parent(
                            resolved_target,
                            resolved_target / action.path,
                            action.path,
                            migration_id=plan.migration_id,
                            directory_nonce=journal["directory_nonce"],
                            record_directory=record_directory,
                        )

                marker = target / ".agents" / ".vault-builder-migration.json"
                self.assertEqual("0644", applying._observed_mode(marker))
                recover_migration(target, plan.migration_id)
            finally:
                os.umask(previous_umask)

            self.assertEqual(before, _tree_digest(target))

    def test_directory_marker_revalidates_staged_source_immediately_before_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            marker_name = f"{hashlib.sha256(b'.agents').hexdigest()}.json"
            staged_marker = target / ".vault-builder" / "recovery" / plan.migration_id / "directory-markers" / marker_name
            target_marker = target / ".agents" / ".vault-builder-migration.json"

            def corrupt_staged_marker(stage: str, relative: str) -> None:
                if stage == "after-mkdir" and relative == ".agents":
                    staged_marker.write_text("corrupt staged marker\n")

            with mock.patch("tools.vault_migration.applying._directory_step", side_effect=corrupt_staged_marker):
                with self.assertRaisesRegex(applying.MigrationApplyError, "marker is invalid"):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertFalse(target_marker.exists())
            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".vault-builder" / "recovery" / plan.migration_id).exists())

    def test_directory_marker_publish_preserves_hard_link_identity(self):
        class StopAfterMarkerPublication(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            before, plan, resolved_target, journal_root, journal, action, record_directory = (
                self._prepare_missing_agents_transaction(target)
            )

            def stop_after_marker(stage: str, relative: str) -> None:
                if stage == "after-marker" and relative == ".agents":
                    raise StopAfterMarkerPublication()

            with mock.patch("tools.vault_migration.applying._directory_step", side_effect=stop_after_marker):
                with self.assertRaises(StopAfterMarkerPublication):
                    applying._ensure_managed_parent(
                        resolved_target,
                        resolved_target / action.path,
                        action.path,
                        migration_id=plan.migration_id,
                        directory_nonce=journal["directory_nonce"],
                        record_directory=record_directory,
                    )

            marker_name = f"{hashlib.sha256(b'.agents').hexdigest()}.json"
            staged_marker = journal_root / "directory-markers" / marker_name
            target_marker = target / ".agents" / ".vault-builder-migration.json"
            staged_state = staged_marker.lstat()
            target_state = target_marker.lstat()
            self.assertEqual((staged_state.st_dev, staged_state.st_ino), (target_state.st_dev, target_state.st_ino))
            self.assertEqual("0644", applying._observed_mode(staged_marker))
            self.assertEqual(staged_marker.read_bytes(), target_marker.read_bytes())

            recover_migration(target, plan.migration_id)

            self.assertEqual(before, _tree_digest(target))

    def test_recovery_uses_hard_link_identity_after_linked_inode_corruption(self):
        class StopAfterMarkerCorruption(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            before, plan, resolved_target, _, journal, action, record_directory = self._prepare_missing_agents_transaction(target)
            target_marker = target / ".agents" / ".vault-builder-migration.json"

            def corrupt_linked_marker(stage: str, relative: str) -> None:
                if stage == "after-marker" and relative == ".agents":
                    target_marker.write_text("corrupt linked inode\n")
                    raise StopAfterMarkerCorruption()

            with mock.patch("tools.vault_migration.applying._directory_step", side_effect=corrupt_linked_marker):
                with self.assertRaises(StopAfterMarkerCorruption):
                    applying._ensure_managed_parent(
                        resolved_target,
                        resolved_target / action.path,
                        action.path,
                        migration_id=plan.migration_id,
                        directory_nonce=journal["directory_nonce"],
                        record_directory=record_directory,
                    )

            recover_migration(target, plan.migration_id)

            self.assertEqual(before, _tree_digest(target))

    def test_recovery_refuses_replaced_directory_marker_inode(self):
        class StopAfterMarkerPublication(Exception):
            pass

        for replaced_name in ("staged", "target"):
            with self.subTest(replaced_name=replaced_name), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _, plan, resolved_target, journal_root, journal, action, record_directory = (
                    self._prepare_missing_agents_transaction(target)
                )

                def stop_after_marker(stage: str, relative: str) -> None:
                    if stage == "after-marker" and relative == ".agents":
                        raise StopAfterMarkerPublication()

                with mock.patch("tools.vault_migration.applying._directory_step", side_effect=stop_after_marker):
                    with self.assertRaises(StopAfterMarkerPublication):
                        applying._ensure_managed_parent(
                            resolved_target,
                            resolved_target / action.path,
                            action.path,
                            migration_id=plan.migration_id,
                            directory_nonce=journal["directory_nonce"],
                            record_directory=record_directory,
                        )

                marker_name = f"{hashlib.sha256(b'.agents').hexdigest()}.json"
                staged_marker = journal_root / "directory-markers" / marker_name
                target_marker = target / ".agents" / ".vault-builder-migration.json"
                victim = staged_marker if replaced_name == "staged" else target_marker
                replacement = victim.with_name(f"{victim.name}.replacement")
                replacement.write_bytes(victim.read_bytes())
                replacement.chmod(0o644)
                os.replace(replacement, victim)
                before_recovery = _tree_digest(target)

                with self.assertRaisesRegex(ValueError, "identity"):
                    recover_migration(target, plan.migration_id)

                self.assertEqual(before_recovery, _tree_digest(target))
                self.assertTrue(journal_root.is_dir())

    def test_apply_with_restrictive_umask_cleans_up_directory_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            protected_before = _protected_hashes(target)
            plan = audit_vault(ROOT, target)

            previous_umask = os.umask(0o077)
            try:
                manifest = apply_plan(ROOT, target, plan, run_gate=lambda _: None)
            finally:
                os.umask(previous_umask)

            self.assertEqual(plan.migration_id, manifest.last_migration_id)
            self.assertEqual(protected_before, _protected_hashes(target))
            self.assertEqual([], list(target.rglob(".vault-builder-migration.json")))
            self.assertFalse((target / ".vault-builder" / "recovery" / plan.migration_id).exists())

    def test_directory_fsync_suppresses_only_explicit_unsupported_errors(self):
        descriptor = 123
        unsupported = {errno.EINVAL}
        if hasattr(errno, "ENOTSUP"):
            unsupported.add(errno.ENOTSUP)
        if hasattr(errno, "EOPNOTSUPP"):
            unsupported.add(errno.EOPNOTSUPP)

        for error_number in sorted(unsupported):
            with self.subTest(error_number=error_number), mock.patch.object(
                applying.os,
                "open",
                return_value=descriptor,
            ), mock.patch.object(
                applying.os,
                "fsync",
                side_effect=OSError(error_number, "unsupported"),
            ), mock.patch.object(applying.os, "close") as close:
                applying._fsync_directory(Path("/durability-test"))
                close.assert_called_once_with(descriptor)

        for error_number in (errno.EIO, errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EBADF):
            with self.subTest(error_number=error_number), mock.patch.object(
                applying.os,
                "open",
                return_value=descriptor,
            ), mock.patch.object(
                applying.os,
                "fsync",
                side_effect=OSError(error_number, "fatal"),
            ), mock.patch.object(applying.os, "close") as close:
                with self.assertRaises(OSError) as raised:
                    applying._fsync_directory(Path("/durability-test"))
                self.assertEqual(error_number, raised.exception.errno)
                close.assert_called_once_with(descriptor)

        with mock.patch.object(
            applying.os,
            "open",
            side_effect=OSError(errno.EACCES, "open denied"),
        ):
            with self.assertRaises(OSError) as raised:
                applying._fsync_directory(Path("/durability-test"))
            self.assertEqual(errno.EACCES, raised.exception.errno)

    def test_journal_ancestry_fsync_failure_aborts_without_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            real_fsync = applying._fsync_directory
            failed = False

            def fail_state_directory_creation(path: Path) -> None:
                nonlocal failed
                if path == target.resolve() and not failed:
                    failed = True
                    raise OSError(errno.EIO, "injected journal ancestry fsync")
                real_fsync(path)

            with mock.patch.object(applying, "_fsync_directory", side_effect=fail_state_directory_creation):
                with self.assertRaisesRegex(
                    applying.MigrationApplyError,
                    "journal ancestry fsync",
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(failed)
            self.assertEqual(before, _tree_digest(target))

    def test_journal_state_parent_fsync_failure_aborts_without_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            journal_root = target.resolve() / ".vault-builder" / "recovery" / plan.migration_id
            real_fsync = applying._fsync_directory
            failed = False

            def fail_first_journal_state(path: Path) -> None:
                nonlocal failed
                if path == journal_root and not failed:
                    failed = True
                    raise OSError(errno.EIO, "injected journal state parent fsync")
                real_fsync(path)

            with mock.patch.object(applying, "_fsync_directory", side_effect=fail_first_journal_state):
                with self.assertRaisesRegex(
                    applying.MigrationApplyError,
                    "journal state parent fsync",
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))

    def test_managed_replacement_parent_fsync_failure_rolls_back_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            victim = next(
                action for action in plan.actions
                if action.action == "replace" and action.path == "AGENTS.md"
            )
            victim_parent = (target.resolve() / victim.path).parent
            real_fsync = applying._fsync_directory
            failed = False

            def fail_managed_parent(path: Path) -> None:
                nonlocal failed
                if path == victim_parent and (target / victim.path).read_bytes() == (ROOT / victim.path).read_bytes() and not failed:
                    failed = True
                    raise OSError(errno.EIO, "injected managed replacement parent fsync")
                real_fsync(path)

            with mock.patch.object(applying, "_fsync_directory", side_effect=fail_managed_parent):
                with self.assertRaisesRegex(
                    applying.MigrationApplyError,
                    "managed replacement parent fsync",
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(failed)
            self.assertEqual(before, _tree_digest(target))

    def test_cross_directory_publication_fsyncs_destination_before_stage_removal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            journal = root / "journal"
            destination_parent = root / "target"
            journal.mkdir()
            destination_parent.mkdir()
            staged = journal / "staged.bin"
            destination = destination_parent / "managed.bin"
            contents = b"durable publication\n"
            applying._create_staged_file(journal, staged, contents, "0644")
            calls: list[Path] = []

            with mock.patch.object(
                applying,
                "_fsync_directory",
                side_effect=lambda path: calls.append(path),
            ):
                applying._publish_staged_file(
                    staged,
                    destination,
                    hashlib.sha256(contents).hexdigest(),
                    "0644",
                    applying._ABSENT_DESTINATION,
                )

            self.assertEqual(
                [destination_parent, journal],
                calls,
            )

    def test_candidate_manifest_parent_fsync_failure_rolls_back_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            manifest_parent = target.resolve() / ".vault-builder"
            manifest_path = manifest_parent / "manifest.json"
            real_fsync = applying._fsync_directory
            failed = False

            def fail_candidate_parent(path: Path) -> None:
                nonlocal failed
                if path == manifest_parent and manifest_path.is_file() and not failed:
                    failed = True
                    raise OSError(errno.EIO, "injected candidate manifest parent fsync")
                real_fsync(path)

            with mock.patch.object(applying, "_fsync_directory", side_effect=fail_candidate_parent):
                with self.assertRaisesRegex(
                    applying.MigrationApplyError,
                    "candidate manifest parent fsync",
                ):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(failed)
            self.assertEqual(before, _tree_digest(target))

    def test_managed_and_manifest_staging_hard_stops_recover_exactly(self):
        cases = (
            ("managed", 51),
            ("manifest", 52),
        )
        for boundary, exit_code in cases:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)
                managed_path = next(
                    action.path for action in plan.actions
                    if action.action == "replace"
                )

                child = os.fork()
                if child == 0:
                    def managed_stop(stage: str, relative: str) -> None:
                        if boundary == "managed" and stage == "after-stage" and relative == managed_path:
                            os._exit(exit_code)

                    def manifest_stop(stage: str) -> None:
                        if boundary == "manifest" and stage == "after-stage":
                            os._exit(exit_code)

                    with (
                        mock.patch.object(applying, "_managed_step", side_effect=managed_stop, create=True),
                        mock.patch.object(applying, "_manifest_step", side_effect=manifest_stop),
                    ):
                        apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                    os._exit(99)

                _, child_status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(child_status))
                self.assertEqual(exit_code, os.WEXITSTATUS(child_status))
                journal_root = target / ".vault-builder" / "recovery" / plan.migration_id
                self.assertTrue(journal_root.is_dir())
                self.assertTrue((journal_root / "staged").is_dir())

                recover_migration(target, plan.migration_id)

                self.assertEqual(before, _tree_digest(target))
                self.assertFalse(journal_root.exists())
                self.assertEqual([], list(target.rglob("*.tmp")))

    def test_cleanup_parent_fsync_failure_is_cleanup_pending_not_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            protected_before = _protected_hashes(target)
            plan = audit_vault(ROOT, target)
            recovery = target.resolve() / ".vault-builder" / "recovery"
            tombstone = applying._cleanup_tombstone(
                recovery / plan.migration_id,
                recovery_existed=False,
            )
            real_fsync = applying._fsync_directory
            failed = False

            def fail_cleanup_parent(path: Path) -> None:
                nonlocal failed
                if path == recovery and tombstone.exists() and not failed:
                    failed = True
                    raise OSError(errno.EIO, "injected cleanup parent fsync")
                real_fsync(path)

            with mock.patch.object(applying, "_fsync_directory", side_effect=fail_cleanup_parent):
                with self.assertRaises(applying.MigrationCleanupPendingError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(failed)
            self.assertIn("Migration committed, cleanup pending", str(raised.exception))
            self.assertIn(f"recover --target {target.resolve()}", str(raised.exception))
            self.assertEqual(protected_before, _protected_hashes(target))
            manifest = load_manifest(target)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(plan.migration_id, manifest.last_migration_id)

            self.assertEqual("cleanup-completed", recover_migration(target, plan.migration_id))
            self.assertEqual(manifest, load_manifest(target))
            self.assertEqual([], list(target.rglob(".vault-builder-migration.json")))
            self.assertFalse(recovery.exists())

    def test_cleanup_recovers_when_final_tombstone_fsync_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            recovery = target.resolve() / ".vault-builder" / "recovery"
            tombstone = applying._cleanup_tombstone(
                recovery / plan.migration_id,
                recovery_existed=False,
            )
            real_fsync = applying._fsync_directory
            failed = False

            def fail_empty_tombstone_fsync(path: Path) -> None:
                nonlocal failed
                if (
                    path == tombstone
                    and not (tombstone / "journal.json").exists()
                    and not failed
                ):
                    failed = True
                    raise OSError(errno.EIO, "injected empty tombstone fsync")
                real_fsync(path)

            with mock.patch.object(
                applying,
                "_fsync_directory",
                side_effect=fail_empty_tombstone_fsync,
            ):
                with self.assertRaises(applying.MigrationCleanupPendingError):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(failed)
            self.assertTrue(tombstone.is_dir())
            self.assertEqual([], list(tombstone.iterdir()))
            committed = load_manifest(target)

            self.assertEqual("cleanup-completed", recover_migration(target, plan.migration_id))
            self.assertEqual(committed, load_manifest(target))
            self.assertFalse(recovery.exists())

    def test_cleanup_transient_nonempty_error_keeps_journal_for_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            plan = audit_vault(ROOT, target)
            recovery = target.resolve() / ".vault-builder" / "recovery"
            tombstone = applying._cleanup_tombstone(
                recovery / plan.migration_id,
                recovery_existed=False,
            )
            real_rmdir = Path.rmdir
            failed = False

            def fail_first_empty_child(path: Path) -> None:
                nonlocal failed
                if (
                    tombstone in path.parents
                    and path != tombstone
                    and not any(path.iterdir())
                    and not failed
                ):
                    failed = True
                    raise OSError(errno.ENOTEMPTY, "injected transient nonempty error")
                real_rmdir(path)

            with mock.patch.object(
                Path,
                "rmdir",
                autospec=True,
                side_effect=fail_first_empty_child,
            ):
                with self.assertRaises(applying.MigrationCleanupPendingError):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(failed)
            self.assertTrue((tombstone / "journal.json").is_file())
            committed = load_manifest(target)

            self.assertEqual("cleanup-completed", recover_migration(target, plan.migration_id))
            self.assertEqual(committed, load_manifest(target))
            self.assertFalse(recovery.exists())

    def test_idempotent_cleanup_preserves_preexisting_recovery_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            recovery = target / ".vault-builder" / "recovery"
            recovery.mkdir(parents=True)
            plan = audit_vault(ROOT, target)

            committed = apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertTrue(recovery.is_dir())
            self.assertEqual([], list(recovery.iterdir()))
            self.assertEqual("cleanup-completed", recover_migration(target, plan.migration_id))
            self.assertEqual(committed, load_manifest(target))
            self.assertTrue(recovery.is_dir())

    def test_cleanup_pending_rejects_symlinked_created_directory_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            plan = audit_vault(ROOT, target)

            with mock.patch.object(
                applying,
                "_remove_created_markers",
                side_effect=OSError("injected marker cleanup failure"),
            ):
                with self.assertRaises(applying.MigrationCleanupPendingError):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            created = target / ".agents" / "skills" / "wiki-update"
            moved = target / ".agents" / "skills" / "wiki-update-moved"
            created.rename(moved)
            created.symlink_to(target / "docs", target_is_directory=True)
            before_recovery = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "marker|symlink|identity"):
                recover_migration(target, plan.migration_id)

            self.assertEqual(before_recovery, _tree_digest(target))
            self.assertTrue(
                (target / ".vault-builder" / "recovery" / plan.migration_id).is_dir()
            )

    def test_cleanup_hard_stops_resume_without_rollback(self):
        cases = (
            ("after-marker-removal", 61, True),
            ("after-journal-rename", 62, False),
            ("during-tombstone-delete", 63, False),
        )
        for stop_stage, exit_code, missing_agents in cases:
            with self.subTest(stop_stage=stop_stage), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                if missing_agents:
                    shutil.rmtree(target / ".agents")
                protected_before = _protected_hashes(target)
                plan = audit_vault(ROOT, target)

                child = os.fork()
                if child == 0:
                    def hard_stop(stage: str) -> None:
                        if stage == stop_stage:
                            os._exit(exit_code)

                    with mock.patch.object(applying, "_cleanup_step", side_effect=hard_stop, create=True):
                        apply_plan(ROOT, target, plan, run_gate=lambda _: None)
                    os._exit(99)

                _, child_status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(child_status))
                self.assertEqual(exit_code, os.WEXITSTATUS(child_status))
                committed = load_manifest(target)
                self.assertIsNotNone(committed)
                assert committed is not None
                self.assertEqual(plan.migration_id, committed.last_migration_id)
                self.assertEqual(protected_before, _protected_hashes(target))

                self.assertEqual("cleanup-completed", recover_migration(target, plan.migration_id))

                self.assertEqual(committed, load_manifest(target))
                self.assertEqual(protected_before, _protected_hashes(target))
                self.assertEqual([], list(target.rglob(".vault-builder-migration.json")))
                recovery = target / ".vault-builder" / "recovery"
                self.assertFalse(recovery.exists())
                self.assertEqual("cleanup-completed", recover_migration(target, plan.migration_id))

    def test_recovery_refuses_hostile_journals_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            migration_id = "0123456789abcdef"
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            journal_root.mkdir(parents=True)
            (journal_root / "journal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "migration_id": migration_id,
                        "target_contract_version": 1,
                        "state": "applying",
                        "entries": [{
                            "path": "../raw/sources/input.md",
                            "existed": True,
                            "sha256": "a" * 64,
                            "mode": "0644",
                        }],
                        "manifest": {
                            "existed": False,
                            "sha256": None,
                            "mode": None,
                            "backup_state": "not-required",
                            "backup_staged_path": None,
                            "candidate_sha256": "c" * 64,
                            "publication_state": "intent",
                        },
                        "directory_nonce": "b" * 64,
                        "directories": [],
                    }
                )
            )
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "safe relative"):
                recover_migration(target, migration_id)

            self.assertEqual(before, _tree_digest(target))

    def test_recovery_refuses_corrupt_original_and_complete_journal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            migration_id = "0123456789abcdef"
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            original = journal_root / "originals" / "tools" / "wiki" / "cli.py"
            original.parent.mkdir(parents=True)
            original.write_text("corrupt original\n")
            journal = {
                "schema_version": 1,
                "migration_id": migration_id,
                "target_contract_version": 1,
                "state": "applying",
                "entries": [{
                    "path": "tools/wiki/cli.py",
                    "existed": True,
                    "sha256": "a" * 64,
                    "mode": "0644",
                }],
                "manifest": {
                    "existed": False,
                    "sha256": None,
                    "mode": None,
                    "backup_state": "not-required",
                    "backup_staged_path": None,
                    "candidate_sha256": "c" * 64,
                    "publication_state": "intent",
                },
                "directory_nonce": "b" * 64,
                "directories": [],
            }
            (journal_root / "journal.json").write_text(json.dumps(journal))
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "original is invalid"):
                recover_migration(target, migration_id)
            self.assertEqual(before, _tree_digest(target))

            original.unlink()
            journal["entries"] = []
            journal["state"] = "complete"
            (journal_root / "journal.json").write_text(json.dumps(journal))
            with self.assertRaisesRegex(ValueError, "complete"):
                recover_migration(target, migration_id)

    def test_recovery_rejects_duplicate_json_keys_and_hostile_created_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            migration_id = "0123456789abcdef"
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            (journal_root / "originals").mkdir(parents=True)
            (journal_root / "journal.json").write_text(
                "{"
                '"schema_version":1,"migration_id":"0123456789abcdef",'
                '"target_contract_version":1,"state":"applying","state":"complete",'
                '"entries":[],"manifest":null,"directory_nonce":"'
                + "b" * 64
                + '","directories":[]}'
            )
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "invalid"):
                recover_migration(target, migration_id)
            self.assertEqual(before, _tree_digest(target))

            user_directory = target / "scratch" / "owned-empty"
            user_directory.mkdir()
            (journal_root / "journal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "migration_id": migration_id,
                        "target_contract_version": 1,
                        "state": "prepared",
                        "entries": [{
                            "path": ".agents/skills/wiki-ingest/SKILL.md",
                            "existed": False,
                            "sha256": None,
                            "mode": None,
                            "applied_sha256": "a" * 64,
                            "applied_mode": "0644",
                        }],
                        "manifest": None,
                        "directory_nonce": "b" * 64,
                        "directories": [{"existed": False, "path": "scratch/owned-empty", "state": "intent"}],
                    }
                )
            )
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "directory records"):
                recover_migration(target, migration_id)
            self.assertEqual(before, _tree_digest(target))
            self.assertTrue(user_directory.is_dir())

    def test_recovery_preserves_preexisting_directory_without_a_valid_marker(self):
        for marker_kind in ("missing", "symlink", "directory", "duplicate-json", "wrong-content", "wrong-nonce"):
            with self.subTest(marker_kind=marker_kind), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                directory = target / ".agents"
                shutil.rmtree(directory)
                directory.mkdir()
                migration_id = "0123456789abcdef"
                nonce = "b" * 64
                journal_root = target / ".vault-builder" / "recovery" / migration_id
                (journal_root / "originals").mkdir(parents=True)
                marker = directory / ".vault-builder-migration.json"
                if marker_kind == "symlink":
                    marker.symlink_to(target / "AGENTS.md")
                elif marker_kind == "directory":
                    marker.mkdir()
                elif marker_kind == "duplicate-json":
                    marker.write_text('{"migration_id":"x","migration_id":"y"}')
                elif marker_kind == "wrong-content":
                    marker.write_text(json.dumps({"migration_id": migration_id, "nonce": nonce, "path": "wrong", "schema_version": 1}))
                elif marker_kind == "wrong-nonce":
                    marker.write_text(json.dumps({"migration_id": migration_id, "nonce": "a" * 64, "path": ".agents", "schema_version": 1}))
                (journal_root / "journal.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "migration_id": migration_id,
                            "target_contract_version": 1,
                            "state": "prepared",
                            "entries": [{
                                "path": ".agents/skills/wiki-ingest/SKILL.md",
                                "existed": False,
                                "sha256": None,
                                "mode": None,
                                "applied_sha256": "a" * 64,
                                "applied_mode": "0644",
                            }],
                            "manifest": None,
                            "directory_nonce": nonce,
                            "directories": [{"existed": False, "path": ".agents", "state": "created"}],
                        }
                    )
                )
                before = _tree_digest(target)

                with self.assertRaisesRegex(ValueError, "marker"):
                    recover_migration(target, migration_id)

                self.assertEqual(before, _tree_digest(target))
                self.assertTrue(directory.is_dir())


class ReleaseGateValidationTest(unittest.TestCase):
    def _write_gate(self, target: Path, body: str) -> None:
        scripts = target / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "release_gate.sh").write_text("#!/usr/bin/env bash\n" + body)

    def test_release_gate_failure_is_captured_and_not_inherited(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            self._write_gate(
                target,
                'for i in $(seq 1 40); do echo "gate-line-$i"; done\n'
                'echo "gate-stderr-marker" >&2\n'
                "exit 3\n",
            )
            failures: list[subprocess.CalledProcessError] = []

            def run() -> None:
                try:
                    applying._run_release_gate(target)
                except subprocess.CalledProcessError as exc:
                    failures.append(exc)

            leaked = _capture_inherited_output(run)

            self.assertEqual(b"", leaked)
            self.assertEqual(1, len(failures))
            failure = failures[0]
            self.assertEqual(3, failure.returncode)
            self.assertEqual(["bash", "scripts/release_gate.sh"], failure.cmd)
            self.assertIn("gate-line-40", failure.output)
            self.assertIn("gate-stderr-marker", failure.output)

    def test_release_gate_success_is_quiet_and_uses_running_interpreter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            self._write_gate(target, 'echo "gate ok"\nprintf "%s" "$PYTHON" > python-used.txt\n')
            environment = {key: value for key, value in os.environ.items() if key != "PYTHON"}

            with mock.patch.dict(os.environ, environment, clear=True):
                leaked = _capture_inherited_output(lambda: applying._run_release_gate(target))

            self.assertEqual(b"", leaked)
            self.assertEqual(sys.executable, (target / "python-used.txt").read_text())

    def test_release_gate_failure_error_includes_output_tail(self):
        output = "".join(f"gate-line-{index}\n" for index in range(1, 41))
        error = subprocess.CalledProcessError(3, ["bash", "scripts/release_gate.sh"], output=output)

        failure = applying.MigrationApplyError(
            error,
            target=Path("/vault"),
            migration_id="a" * 16,
            rollback_completed=True,
        )

        lines = str(failure).splitlines()
        self.assertIn("Failed command: bash scripts/release_gate.sh", lines)
        self.assertIn("Exit code: 3", lines)
        self.assertIn("  gate-line-40", lines)
        self.assertIn("  gate-line-21", lines)
        self.assertNotIn("  gate-line-20", lines)
        self.assertIn(
            f"Run read-only audit again: {_PYTHON} tools/vault_migration/cli.py audit --target /vault",
            lines,
        )

    def test_release_gate_failure_without_output_has_no_output_section(self):
        error = subprocess.CalledProcessError(3, ["bash", "scripts/release_gate.sh"])

        failure = applying.MigrationApplyError(
            error,
            target=Path("/vault"),
            migration_id="a" * 16,
            rollback_completed=True,
        )

        self.assertNotIn("Output", str(failure))

    def test_apply_default_validation_runs_target_release_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            gate_error = subprocess.CalledProcessError(
                5,
                ["bash", "scripts/release_gate.sh"],
                output="[release-gate] gate exploded\n",
            )

            with mock.patch.object(applying, "_run_release_gate", side_effect=gate_error) as gate:
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan)

            gate.assert_called_once_with(target.resolve())
            self.assertTrue(raised.exception.rollback_completed)
            self.assertIn("[release-gate] gate exploded", str(raised.exception))
            self.assertEqual(before, _tree_digest(target))


class VaultMigrationCliTest(unittest.TestCase):
    def test_audit_stdout_is_deterministic_and_target_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan_path = root / "migration-plan.json"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)

            first = _run_cli("audit", "--target", str(target), "--plan-out", str(plan_path))
            second = _run_cli("audit", "--target", str(target))

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(before, _tree_digest(target))
            self.assertEqual(MigrationPlan.from_json(plan_path.read_text()).to_json(), plan_path.read_text())
            self.assertIn("Target writes: none", first.stdout)
            summary, _, _ = first.stdout.partition("\n\n{")
            self.assertNotIn("fingerprint", summary.lower())

    def test_audit_returns_two_and_writes_blocking_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan_path = root / "migration-plan.json"
            _copy_legacy_fixture(target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")

            result = _run_cli("audit", "--target", str(target), "--plan-out", str(plan_path))

            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("review-required: 1", result.stdout)
            self.assertTrue(plan_path.is_file())
            self.assertTrue(MigrationPlan.from_json(plan_path.read_text()).has_blocking_actions)
            self.assertFalse((target / ".vault-builder").exists())

    def test_audit_warns_once_about_uncommitted_changes_without_changing_plan_or_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)

            no_git = _run_cli("audit", "--target", str(target))
            _commit_all(target)
            clean = _run_cli("audit", "--target", str(target))
            (target / "uncommitted-note.md").write_text("draft\n")
            dirty = _run_cli("audit", "--target", str(target))

            self.assertEqual(0, no_git.returncode, no_git.stderr)
            self.assertEqual("", no_git.stderr)
            self.assertEqual(0, clean.returncode, clean.stderr)
            self.assertEqual("", clean.stderr)
            self.assertEqual(clean.returncode, dirty.returncode)
            self.assertEqual(clean.stdout, dirty.stdout)
            warnings = dirty.stderr.splitlines()
            self.assertEqual(1, len(warnings), dirty.stderr)
            self.assertIn("uncommitted", warnings[0])
            self.assertIn("before apply", warnings[0])

    def test_audit_uncommitted_warning_keeps_blocking_exit_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            _commit_all(target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")

            result = _run_cli("audit", "--target", str(target))

            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("review-required: 1", result.stdout)
            self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
            self.assertIn("before apply", result.stderr)

    def test_audit_report_written_by_audit_does_not_trigger_uncommitted_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            _commit_all(target)

            result = _run_cli(
                "audit",
                "--target",
                str(target),
                "--report",
                str(target / "scratch" / "reports" / "migration-audit.md"),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)

    def test_audit_report_does_not_invalidate_the_plan_emitted_by_that_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            report = target / "scratch" / "reports" / "migration-audit.md"
            _copy_legacy_fixture(target)

            reported = _run_cli(
                "audit",
                "--target",
                str(target),
                "--report",
                str(report),
            )
            checked = _run_cli("audit", "--target", str(target))

            self.assertEqual(0, reported.returncode, reported.stderr)
            self.assertEqual(0, checked.returncode, checked.stderr)
            reported_json = "{\n" + reported.stdout.partition("\n\n{\n")[2]
            checked_json = "{\n" + checked.stdout.partition("\n\n{\n")[2]
            self.assertEqual(
                MigrationPlan.from_json(reported_json),
                MigrationPlan.from_json(checked_json),
            )

    def test_audit_rejects_plan_and_report_pair_as_non_atomic_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan = root / "migration-plan.json"
            report = target / "scratch" / "reports" / "migration-audit.md"
            _copy_legacy_fixture(target)

            result = _run_cli(
                "audit",
                "--target",
                str(target),
                "--plan-out",
                str(plan),
                "--report",
                str(report),
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("one output artifact", result.stderr)
            self.assertFalse(plan.exists())
            self.assertFalse(report.exists())

    def test_single_plan_publication_hard_stop_leaves_final_absent_and_future_write_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "migration-plan.json"
            candidate = MigrationPlan(
                1,
                "fedcba9876543210",
                None,
                1,
                "candidate",
                "b" * 64,
                (),
                (),
            )

            child = os.fork()
            if child == 0:
                cli._write_atomic(
                    output.resolve(),
                    candidate.to_json(),
                    "Migration plan output",
                    before_replace=lambda: os._exit(87),
                )
                os._exit(99)
            _, status = os.waitpid(child, 0)

            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(87, os.WEXITSTATUS(status))
            self.assertFalse(output.exists())
            stale = list(root.glob(".migration-plan.json.*.tmp"))
            self.assertEqual(1, len(stale))
            cli._write_atomic(
                output.resolve(),
                candidate.to_json(),
                "Migration plan output",
            )
            self.assertEqual(candidate, cli._load_plan(str(output)))
            self.assertEqual(stale, list(root.glob(".migration-plan.json.*.tmp")))

    def test_single_output_hard_stop_after_link_leaves_complete_final(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "migration-plan.json"

            child = os.fork()
            if child == 0:
                with mock.patch.object(
                    cli,
                    "_fsync_parent",
                    side_effect=lambda _descriptor: os._exit(88),
                ):
                    cli._write_atomic(
                        output.resolve(),
                        "candidate\n",
                        "Migration plan output",
                    )
                os._exit(99)
            _, status = os.waitpid(child, 0)

            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(88, os.WEXITSTATUS(status))
            self.assertEqual("candidate\n", output.read_text())
            self.assertEqual(1, len(list(root.glob(".migration-plan.json.*.tmp"))))

    def test_single_output_publication_preserves_unowned_temp_shaped_sibling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "migration-plan.json"
            sibling = root / f".{output.name}.{'a' * 32}.tmp"
            sibling.write_text("external sibling\n")

            cli._write_atomic(
                output.resolve(),
                "candidate\n",
                "Migration plan output",
            )

            self.assertEqual("candidate\n", output.read_text())
            self.assertEqual("external sibling\n", sibling.read_text())

    def test_single_output_preserves_final_boundary_external_writer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "migration-plan.json"
            destination = output.resolve()
            external = b"external output writer\n"
            injected = False

            def publish_external(path: Path) -> None:
                nonlocal injected
                if path == destination and not injected:
                    replacement = output.with_name(".external-output")
                    replacement.write_bytes(external)
                    replacement.chmod(0o600)
                    os.replace(replacement, output)
                    injected = True

            with mock.patch.object(
                cli,
                "_output_publication_step",
                side_effect=publish_external,
                create=True,
            ):
                with self.assertRaisesRegex(ValueError, "publication boundary"):
                    cli._write_atomic(
                        destination,
                        "candidate\n",
                        "Migration plan output",
                    )

            self.assertTrue(injected)
            self.assertEqual(external, output.read_bytes())
            self.assertEqual("0600", f"{output.stat().st_mode & 0o7777:04o}")

    def test_single_output_refuses_to_overwrite_existing_leaf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "migration-plan.json"
            output.write_text("external output\n")
            output.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "already exists"):
                cli._write_atomic(
                    output.resolve(),
                    "candidate\n",
                    "Migration plan output",
                )

            self.assertEqual("external output\n", output.read_text())
            self.assertEqual("0600", f"{output.stat().st_mode & 0o7777:04o}")
            self.assertFalse(list(root.glob(".migration-plan.json.*.tmp")))

    def test_audit_refuses_to_overwrite_existing_plan_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            output = root / "migration-plan.json"
            _copy_legacy_fixture(target)
            output.write_text("external output\n")

            result = _run_cli(
                "audit",
                "--target",
                str(target),
                "--plan-out",
                str(output),
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("already exists", result.stderr)
            self.assertEqual("external output\n", output.read_text())

    def test_audit_validates_all_outputs_before_writing_either_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan_path = root / "migration-plan.json"
            _copy_legacy_fixture(target)

            result = _run_cli(
                "audit",
                "--target",
                str(target),
                "--plan-out",
                str(plan_path),
                "--report",
                str(root / "outside-report.md"),
            )

            self.assertEqual(1, result.returncode)
            self.assertFalse(plan_path.exists())

    def test_audit_prepares_report_before_writing_external_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan_path = root / "migration-plan.json"
            _copy_legacy_fixture(target)
            report_parent = target / "scratch" / "reports"
            original_mode = report_parent.stat().st_mode
            report_parent.chmod(0o500)
            try:
                result = _run_cli(
                    "audit",
                    "--target",
                    str(target),
                    "--plan-out",
                    str(plan_path),
                    "--report",
                    str(report_parent / "migration-audit.md"),
                )
            finally:
                report_parent.chmod(original_mode)

            self.assertEqual(1, result.returncode)
            self.assertFalse(plan_path.exists())

    def test_output_parent_locks_use_deterministic_device_inode_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parents = (root / "first", root / "second")
            for parent in parents:
                parent.mkdir()
            identities = {
                parent: (parent.stat().st_dev, parent.stat().st_ino)
                for parent in parents
            }
            paths = tuple(
                parent / "output.json"
                for parent in sorted(
                    parents,
                    key=identities.__getitem__,
                    reverse=True,
                )
            )
            lock_order: list[tuple[int, int]] = []

            def record_lock(descriptor: int, operation: int) -> None:
                if operation == cli.fcntl.LOCK_EX:
                    state = os.fstat(descriptor)
                    lock_order.append((state.st_dev, state.st_ino))

            created: list[cli._CreatedDirectory] = []
            with mock.patch.object(cli.fcntl, "flock", side_effect=record_lock):
                _output_parents, locked = cli._open_and_lock_output_parents(
                    tuple(
                        (path.resolve(), "value\n", "Audit output")
                        for path in paths
                    ),
                    created,
                )
                cli._release_output_parents(locked)

            self.assertEqual(sorted(identities.values()), lock_order)
            self.assertEqual([], created)

    def test_overlapping_single_output_publications_serialize_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "shared" / "migration-plan.json"
            output.parent.mkdir()

            def first_write(pause: Callable[[], None]) -> None:
                cli._write_atomic(
                    output.resolve(),
                    "first\n",
                    "Migration plan output",
                    before_replace=pause,
                )

            def second_write() -> None:
                cli._write_atomic(
                    output.resolve(),
                    "second\n",
                    "Migration plan output",
                )

            (
                second_finished_while_first_paused,
                first_status,
                second_status,
            ) = _run_concurrent_output_writes(
                first_write,
                second_write,
                timeout=1.0,
            )

            self.assertTrue(os.WIFEXITED(first_status))
            self.assertEqual(0, os.WEXITSTATUS(first_status))
            self.assertTrue(os.WIFEXITED(second_status))
            self.assertEqual(72, os.WEXITSTATUS(second_status))
            self.assertFalse(second_finished_while_first_paused)
            self.assertEqual("first\n", output.read_text())

    def test_disjoint_output_directories_can_publish_independently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first_output = root / "first" / "migration-plan.json"
            second_output = root / "second" / "migration-plan.json"
            first_output.parent.mkdir()
            second_output.parent.mkdir()

            def first_write(pause: Callable[[], None]) -> None:
                cli._write_atomic(
                    first_output.resolve(),
                    "first\n",
                    "Migration plan output",
                    before_replace=pause,
                )

            def second_write() -> None:
                cli._write_atomic(
                    second_output.resolve(),
                    "second\n",
                    "Migration plan output",
                )

            (
                second_finished_while_first_paused,
                first_status,
                second_status,
            ) = _run_concurrent_output_writes(
                first_write,
                second_write,
                timeout=3.0,
            )

            self.assertTrue(os.WIFEXITED(first_status))
            self.assertEqual(0, os.WEXITSTATUS(first_status))
            self.assertTrue(os.WIFEXITED(second_status))
            self.assertEqual(0, os.WEXITSTATUS(second_status))
            self.assertTrue(second_finished_while_first_paused)
            self.assertEqual("first\n", first_output.read_text())
            self.assertEqual("second\n", second_output.read_text())

    def test_audit_rejects_raw_traversal_for_report_and_plan_output_without_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            _copy_legacy_fixture(target)
            plans = root / "plans"
            plans.mkdir()
            plan_path = plans / "migration-plan.json"
            raw_report = target / "scratch" / "reports" / ".." / "reports" / "migration-audit.md"
            report_result = _run_cli("audit", "--target", str(target), "--report", str(raw_report))
            self.assertEqual(1, report_result.returncode)
            self.assertFalse((target / "scratch" / "reports" / "migration-audit.md").exists())

            raw_plan = plans / ".." / "plans" / "migration-plan.json"
            plan_result = _run_cli("audit", "--target", str(target), "--plan-out", str(raw_plan))
            self.assertEqual(1, plan_result.returncode)
            self.assertFalse(plan_path.exists())

    def test_audit_creates_safe_external_plan_parent_with_mode_0644_under_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan_path = root / "new" / "plans" / "migration-plan.json"
            _copy_legacy_fixture(target)
            previous_umask = os.umask(0o077)
            try:
                result = _run_cli("audit", "--target", str(target), "--plan-out", str(plan_path))
            finally:
                os.umask(previous_umask)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("0644", f"{plan_path.stat().st_mode & 0o7777:04o}")

    def test_audit_rejects_nonregular_report_leaf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            report = target / "scratch" / "reports" / "migration-audit.md"
            _copy_legacy_fixture(target)
            report.mkdir()

            result = _run_cli("audit", "--target", str(target), "--report", str(report))

            self.assertEqual(1, result.returncode)
            self.assertTrue(report.is_dir())

    def test_audit_report_is_an_explicit_safe_target_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            report = target / "scratch" / "reports" / "migration-audit.md"
            _copy_legacy_fixture(target)

            result = _run_cli("audit", "--target", str(target), "--report", str(report))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(report.is_file())
            self.assertIn("Target writes: scratch/reports/migration-audit.md", result.stdout)
            self.assertIn('"migration_id"', report.read_text())

    def test_audit_rejects_symlinked_report_parent_and_leaf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            _copy_legacy_fixture(target)
            outside = root / "outside"
            outside.mkdir()
            linked_parent = target / "scratch" / "reports" / "linked"
            linked_parent.symlink_to(outside, target_is_directory=True)

            parent_result = _run_cli("audit", "--target", str(target), "--report", str(linked_parent / "report.md"))
            self.assertEqual(1, parent_result.returncode)
            self.assertIn("symlink", parent_result.stderr)

            leaf = target / "scratch" / "reports" / "linked-report.md"
            leaf.symlink_to(outside / "report.md")
            leaf_result = _run_cli("audit", "--target", str(target), "--report", str(leaf))
            self.assertEqual(1, leaf_result.returncode)
            self.assertIn("symlink", leaf_result.stderr)

    def test_audit_rejects_symlinked_plan_output_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            outside = root / "outside"
            outside.mkdir()
            linked = root / "linked-output"
            linked.symlink_to(outside, target_is_directory=True)
            _copy_legacy_fixture(target)

            result = _run_cli("audit", "--target", str(target), "--plan-out", str(linked / "plan.json"))

            self.assertEqual(1, result.returncode)
            self.assertIn("symlink", result.stderr)
            self.assertFalse((outside / "plan.json").exists())

    def test_apply_requires_a_regular_nonsymlink_plan_before_target_access(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = _run_cli("apply", "--target", str(root / "missing"))
            self.assertEqual(1, missing.returncode)
            self.assertIn("--plan", missing.stderr)

            plan = root / "plan.json"
            plan.write_text("{}\n")
            linked = root / "linked-plan.json"
            linked.symlink_to(plan)
            result = _run_cli("apply", "--target", str(root / "missing"), "--plan", str(linked))
            self.assertEqual(1, result.returncode)
            self.assertRegex(result.stderr, "regular|symlink")

    def test_apply_rejects_duplicate_key_plan_before_target_access(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = root / "plan.json"
            plan.write_text(
                '{"schema_version":1,"schema_version":1,"migration_id":"0123456789abcdef",'
                '"current_contract_version":null,"target_contract_version":1,'
                '"builder_revision":"test","target_fingerprint":"' + "a" * 64 + '",'
                '"actions":[],"authority_issues":[]}\n'
            )

            result = _run_cli("apply", "--target", str(root / "missing"), "--plan", str(plan))

            self.assertEqual(1, result.returncode)
            self.assertIn("duplicate", result.stderr.lower())

    def test_apply_rejects_raw_plan_traversal_before_calling_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = MigrationPlan(
                schema_version=1,
                migration_id="0123456789abcdef",
                current_contract_version=None,
                target_contract_version=1,
                builder_revision="test",
                target_fingerprint="a" * 64,
                actions=(),
                authority_issues=(),
            )
            (root / "plans").mkdir()
            plan_path = root / "plan.json"
            plan_path.write_text(plan.to_json())
            raw_path = root / "plans" / ".." / "plan.json"

            with mock.patch.object(cli, "apply_plan") as apply:
                result = cli.main(["apply", "--target", str(root / "vault"), "--plan", str(raw_path)])

            self.assertEqual(1, result)
            apply.assert_not_called()

    def test_plan_reader_rejects_leaf_swap_to_symlink_from_the_open_descriptor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = root / "plan.json"
            plan.write_text(
                MigrationPlan(1, "0123456789abcdef", None, 1, "test", "a" * 64, (), ()).to_json()
            )
            outside = root / "outside.json"
            outside.write_text("{}\n")

            def swap_leaf() -> None:
                plan.unlink()
                plan.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "regular|symlink"):
                cli._load_plan(str(plan), before_leaf_open=swap_leaf)
            self.assertEqual("{}\n", outside.read_text())

    def test_atomic_output_keeps_parent_swap_from_redirecting_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = root / "output"
            parent.mkdir()
            destination = parent / "plan.json"
            moved_parent = root / "moved-output"
            outside = root / "outside"
            outside.mkdir()

            def swap_parent() -> None:
                parent.rename(moved_parent)
                parent.symlink_to(outside, target_is_directory=True)

            cli._write_atomic(destination.resolve(), "safe\n", "Migration plan output", before_replace=swap_parent)

            self.assertEqual("safe\n", (moved_parent / "plan.json").read_text())
            self.assertFalse((outside / "plan.json").exists())

    def test_apply_maps_service_and_gate_failures_to_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = MigrationPlan(
                schema_version=1,
                migration_id="0123456789abcdef",
                current_contract_version=None,
                target_contract_version=1,
                builder_revision="test",
                target_fingerprint="a" * 64,
                actions=(),
                authority_issues=(),
            )
            plan_path = root / "plan.json"
            plan_path.write_text(plan.to_json())
            for failure in (RuntimeError("service failed"), subprocess.CalledProcessError(7, ["gate"])):
                with self.subTest(failure=type(failure).__name__), mock.patch.object(cli, "apply_plan", side_effect=failure):
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        result = cli.main(["apply", "--target", str(root / "vault"), "--plan", str(plan_path)])
                    self.assertEqual(1, result)
                    self.assertIn("apply:", stderr.getvalue())

            manifest = VaultManifest(1, 1, "test", "2026-07-30T00:00:00Z", {})
            stdout = io.StringIO()
            with mock.patch.object(cli, "apply_plan", return_value=manifest), mock.patch("sys.stdout", stdout):
                result = cli.main(["apply", "--target", str(root / "vault"), "--plan", str(plan_path)])
            self.assertEqual(0, result)
            self.assertIn("Final contract: 1", stdout.getvalue())

    def test_apply_cli_reports_transaction_outcomes_without_guessing_preconditions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            plan = MigrationPlan(
                schema_version=1,
                migration_id="0123456789abcdef",
                current_contract_version=None,
                target_contract_version=1,
                builder_revision="test",
                target_fingerprint="a" * 64,
                actions=(),
                authority_issues=(),
            )
            plan_path = root / "plan.json"
            plan_path.write_text(plan.to_json())
            cases = (
                (
                    "gate",
                    applying.MigrationApplyError(
                        subprocess.CalledProcessError(7, ["python3", "tools/wiki/cli.py", "lint", "--report"]),
                        target=target,
                        migration_id=plan.migration_id,
                        rollback_completed=True,
                    ),
                    ("Failed command: python3 tools/wiki/cli.py lint --report", "Exit code: 7", "Automatic rollback: completed", "audit --target"),
                ),
                (
                    "non-command",
                    applying.MigrationApplyError(
                        ValueError("validation exploded"),
                        target=target,
                        migration_id=plan.migration_id,
                        rollback_completed=True,
                    ),
                    ("Failure: validation exploded", "Automatic rollback: completed", "audit --target"),
                ),
                (
                    "rollback-failed",
                    applying.MigrationApplyError(
                        subprocess.CalledProcessError(9, ["python3", "tools/wiki/cli.py", "lint", "--report"]),
                        target=target,
                        migration_id=plan.migration_id,
                        rollback_completed=False,
                        rollback_error=OSError("rollback exploded"),
                    ),
                    (
                        "Failed command: python3 tools/wiki/cli.py lint --report",
                        "Exit code: 9",
                        "Automatic rollback: failed",
                        f"recover --target {target} --migration-id {plan.migration_id}",
                    ),
                ),
                (
                    "cleanup-pending",
                    applying.MigrationCleanupPendingError(
                        OSError("cleanup exploded"),
                        target=target,
                        migration_id=plan.migration_id,
                    ),
                    (
                        "Migration committed, cleanup pending",
                        "Cleanup failure: cleanup exploded",
                        f"recover --target {target} --migration-id {plan.migration_id}",
                    ),
                ),
                (
                    "precondition",
                    ValueError("Migration target changed after audit; run audit again."),
                    ("Migration target changed after audit",),
                ),
            )
            for name, failure, expected in cases:
                with self.subTest(name=name), mock.patch.object(cli, "apply_plan", side_effect=failure):
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        result = cli.main(["apply", "--target", str(target), "--plan", str(plan_path)])
                    rendered = stderr.getvalue()
                    self.assertEqual(1, result)
                    for token in expected:
                        self.assertIn(token, rendered)
                    if name == "precondition":
                        self.assertNotIn("Automatic rollback", rendered)
                        self.assertNotIn("recover --target", rendered)

    def test_recover_cli_distinguishes_cleanup_only_from_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            target.mkdir()
            manifest = VaultManifest(
                1,
                1,
                "test",
                "2026-07-30T00:00:00Z",
                {},
                "0123456789abcdef",
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    cli,
                    "recover_migration",
                    return_value="cleanup-completed",
                ),
                mock.patch.object(cli, "load_manifest", return_value=manifest),
                mock.patch("sys.stdout", stdout),
            ):
                result = cli.main(
                    [
                        "recover",
                        "--target",
                        str(target),
                        "--migration-id",
                        "0123456789abcdef",
                    ]
                )

            self.assertEqual(0, result)
            self.assertIn("Migration committed; cleanup completed.", stdout.getvalue())
            self.assertNotIn("rollback completed", stdout.getvalue().lower())

    def test_recover_prints_actionable_failure_for_missing_journal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)

            result = _run_cli(
                "recover",
                "--target",
                str(target),
                "--migration-id",
                "0123456789abcdef",
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("Recovery journal not found", result.stderr)


class VaultMigrationAcceptanceTest(unittest.TestCase):
    def test_fresh_vault_has_v2_manifest_and_passes_its_release_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"

            _run(["bash", "init-vault.sh", str(target)], ROOT)

            manifest = load_manifest(target)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(2, manifest.contract_version)
            self.assertIsNone(manifest.last_migration_id)
            self.assertFalse((target / "builder_tests").exists())
            self.assertFalse((target / "tools" / "vault_migration").exists())
            self.assertFalse((target / "init-vault.sh").exists())
            self.assertFalse((target / "scripts" / "bootstrap.sh").exists())
            _run(["bash", "scripts/release_gate.sh"], target)

    def test_legacy_vault_migrates_without_changing_user_role_bytes_or_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = _initialize_legacy_vault(Path(tmpdir))
            expected_user_modes = _add_user_role_content(target)
            build_navigation_maps(target)
            self.assertEqual([], build_navigation_maps(target, check=True).changed_paths)
            protected_before = _protected_hashes(target)

            plan = audit_vault(ROOT, target)

            self.assertIsNone(plan.current_contract_version)
            self.assertFalse(plan.has_blocking_actions)
            self.assertIn("replace", {action.action for action in plan.actions})
            self.assertIn("create", {action.action for action in plan.actions})

            manifest = apply_plan(ROOT, target, plan)

            protected_after = _protected_hashes(target)
            self.assertEqual(protected_before, protected_after)
            for relative, expected_mode in expected_user_modes.items():
                self.assertIn(relative, protected_after)
                self.assertEqual(expected_mode, protected_after[relative][1], relative)
            self.assertEqual(2, manifest.contract_version)
            self.assertEqual(plan.migration_id, manifest.last_migration_id)
            self.assertEqual(manifest, load_manifest(target))
            self.assertFalse((target / ".vault-builder" / "recovery").exists())
            self.assertEqual([], list(target.rglob(".vault-builder-migration.json")))
            _run([sys.executable, "tools/wiki/cli.py", "lint", "--report"], target)

    def test_legacy_source_manifest_violation_blocks_without_changing_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = _initialize_legacy_vault(Path(tmpdir))
            source = target / "wiki" / "sources" / "legacy.md"
            source.write_text(
                "---\n"
                "title: Legacy Source\n"
                "type: source\n"
                "status: active\n"
                "created: 2026-07-30\n"
                "updated: 2026-07-30\n"
                "owner: human\n"
                "summary: Legacy source page with copied semantics.\n"
                "source_count: 1\n"
                "tags: []\n"
                "related: []\n"
                "confidence: medium\n"
                "quality:\n"
                "  provenance: claim\n"
                "  links: unchecked\n"
                "  contradictions: none\n"
                "  review_required: false\n"
                "canonical_source: raw/sources/legacy.md\n"
                f"raw_sha256: {'a' * 64}\n"
                "source_kind: markdown\n"
                "---\n\n"
                "# Legacy Source\n\n"
                "## Key Takeaways\n\n"
                "Copied semantic explanation.\n"
            )
            before = _tree_digest(target)

            plan = audit_vault(ROOT, target)

            actions = {action.path: action for action in plan.actions}
            issues = {(str(issue["code"]), str(issue["path"])) for issue in plan.authority_issues}
            self.assertTrue(plan.has_blocking_actions)
            self.assertEqual("review-required", actions["wiki/sources/legacy.md"].action)
            self.assertIn(("source-manifest-semantic-body", "wiki/sources/legacy.md"), issues)
            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".vault-builder").exists())

    def test_actual_legacy_gate_failure_restores_the_complete_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = _initialize_legacy_vault(Path(tmpdir))
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            def fail_gate(_: Path) -> None:
                raise subprocess.CalledProcessError(1, ["python3", "tools/wiki/cli.py", "lint", "--report"])

            with self.assertRaises(applying.MigrationApplyError):
                apply_plan(ROOT, target, plan, run_gate=fail_gate)

            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".vault-builder").exists())


_CLAUDE_FILES = (
    "CLAUDE.md",
    ".claude/skills/wiki-ingest/SKILL.md",
    ".claude/skills/wiki-update/SKILL.md",
    ".claude/skills/wiki-query/SKILL.md",
    ".claude/skills/memory-cleanup/SKILL.md",
)
_POINTER_SKILLS = ("wiki-ingest", "wiki-update", "wiki-query", "memory-cleanup")


def _skill_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise AssertionError(f"Missing frontmatter: {path}")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


class FreshVaultClaudeIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.target = Path(cls._tmpdir.name) / "vault"
        # Pass a bare interpreter name found only through PATH, so .mcp.json must record an absolute path.
        bin_dir = Path(cls._tmpdir.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "vault-builder-test-python").symlink_to(sys.executable)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHON": "vault-builder-test-python",
        }
        result = subprocess.run(
            ["bash", "init-vault.sh", str(cls.target)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            cls._tmpdir.cleanup()
            raise AssertionError(result.stdout + result.stderr)
        manifest = load_manifest(cls.target)
        assert manifest is not None
        cls.manifest = manifest

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_fresh_vault_release_gate_is_executable_and_in_manifest(self):
        gate = self.target / "scripts" / "release_gate.sh"
        self.assertTrue(gate.is_file())
        self.assertFalse(gate.is_symlink())
        self.assertEqual(0o755, gate.stat().st_mode & 0o7777)
        self.assertIn("scripts/release_gate.sh", self.manifest.managed_files)
        self.assertEqual("0755", self.manifest.managed_files["scripts/release_gate.sh"].mode)

    def test_fresh_vault_claude_md_imports_agents_without_copying_it(self):
        claude = (self.target / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", claude.splitlines())
        agents = (self.target / "AGENTS.md").read_text(encoding="utf-8")
        headings = [line for line in agents.splitlines() if line.startswith("## ")]
        self.assertTrue(headings)
        for heading in headings:
            self.assertNotIn(heading, claude.splitlines())

    def test_fresh_vault_claude_files_are_regular_and_recorded_in_manifest(self):
        for relative in _CLAUDE_FILES:
            with self.subTest(path=relative):
                path = self.target / relative
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                self.assertIn(relative, self.manifest.managed_files)
                self.assertEqual(sha256_file(path), self.manifest.managed_files[relative].sha256)

    def test_claude_pointer_skills_match_agents_skill_frontmatter(self):
        for root in (ROOT, self.target):
            for name in _POINTER_SKILLS:
                with self.subTest(root=str(root), skill=name):
                    pointer = root / ".claude" / "skills" / name / "SKILL.md"
                    original_relative = f".agents/skills/{name}/SKILL.md"
                    original = root / original_relative
                    self.assertTrue(pointer.is_file())
                    self.assertFalse(pointer.is_symlink())
                    pointer_fields = _skill_frontmatter(pointer)
                    original_fields = _skill_frontmatter(original)
                    self.assertEqual(original_fields["name"], pointer_fields["name"])
                    self.assertEqual(original_fields["description"], pointer_fields["description"])
                    self.assertIn(original_relative, pointer.read_text(encoding="utf-8"))

    def test_fresh_vault_mcp_json_uses_chosen_python_and_absolute_server_path(self):
        config_path = self.target / ".mcp.json"
        self.assertTrue(config_path.is_file())
        config = json.loads(config_path.read_text(encoding="utf-8"))
        server = config["mcpServers"]["llm-wiki"]
        command = Path(server["command"])
        self.assertTrue(command.is_absolute(), command)
        self.assertTrue(command.is_file(), command)
        self.assertTrue(os.access(command, os.X_OK), command)
        script_args = [arg for arg in server["args"] if arg.endswith("tools/wiki/mcp_server.py")]
        self.assertEqual(1, len(script_args), server["args"])
        script = Path(script_args[0])
        self.assertTrue(script.is_absolute(), script)
        self.assertTrue(script.is_file(), script)
        self.assertEqual((self.target / "tools" / "wiki" / "mcp_server.py").resolve(), script.resolve())
        version = subprocess.run(
            [server["command"], "-c", "import sys; print(sys.version_info >= (3, 10))"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("True", version.stdout.strip())

    def test_fresh_vault_mcp_json_is_not_managed(self):
        self.assertNotIn(".mcp.json", self.manifest.managed_files)
        self.assertNotIn(".mcp.json", MANAGED_PATHS)

    def test_legacy_manifest_without_claude_files_loads_and_audit_plans_create(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            shutil.copytree(self.target, target, symlinks=True)
            legacy_files = {
                relative: managed
                for relative, managed in self.manifest.managed_files.items()
                if relative not in _CLAUDE_FILES
            }
            self.assertIn("scripts/release_gate.sh", legacy_files)
            write_manifest(
                target,
                VaultManifest(
                    self.manifest.schema_version,
                    self.manifest.contract_version,
                    "legacy-revision",
                    self.manifest.generated_at,
                    legacy_files,
                ),
            )
            for relative in _CLAUDE_FILES:
                (target / relative).unlink()
            shutil.rmtree(target / ".claude")

            loaded = load_manifest(target)

            assert loaded is not None
            for relative in _CLAUDE_FILES:
                self.assertNotIn(relative, loaded.managed_files)
            plan = audit_vault(ROOT, target)
            actions = {action.path: action for action in plan.actions}
            for relative in _CLAUDE_FILES:
                with self.subTest(path=relative):
                    self.assertEqual("create", actions[relative].action)
            self.assertEqual("unchanged", actions["scripts/release_gate.sh"].action)

    def test_vault_without_release_gate_audits_and_plans_create(self):
        # Vaults built between e8f998b and this change have a valid v1 manifest but no gate file.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            shutil.copytree(self.target, target, symlinks=True)
            gateless_files = {
                relative: managed
                for relative, managed in self.manifest.managed_files.items()
                if relative != "scripts/release_gate.sh"
            }
            write_manifest(
                target,
                VaultManifest(
                    self.manifest.schema_version,
                    self.manifest.contract_version,
                    "gateless-revision",
                    self.manifest.generated_at,
                    gateless_files,
                ),
            )
            (target / "scripts" / "release_gate.sh").unlink()

            plan = audit_vault(ROOT, target)

            actions = {action.path: action for action in plan.actions}
            self.assertEqual("create", actions["scripts/release_gate.sh"].action)


PROJECT_MEMORY_ASSET = ROOT / "tools" / "vault_migration" / "assets" / "project-memory"
RECALL_ASSET = PROJECT_MEMORY_ASSET / "scripts" / "recall.sh"
PROJECT_MEMORY_SKILL_ASSET = PROJECT_MEMORY_ASSET / "SKILL.md"
RECALL_PYTHON_PLACEHOLDER = "@VAULT_BUILDER_PYTHON@"
RECALL_SECTIONS = ("open", "decisions", "corrections")


def _materialize_recall_script(destination: Path) -> Path:
    """Fill the interpreter placeholder the way install does and make the script executable."""
    text = RECALL_ASSET.read_text(encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text.replace(RECALL_PYTHON_PLACEHOLDER, shlex.quote(sys.executable)), encoding="utf-8")
    destination.chmod(0o755)
    return destination


def _recall_namespace() -> dict[str, object]:
    """Load the Python embedded in recall.sh without running its main function."""
    text = RECALL_ASSET.read_text(encoding="utf-8")
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    end = text.index("\nPY\n", start)
    namespace: dict[str, object] = {"__name__": "recall_embedded"}
    exec(compile(text[start:end], str(RECALL_ASSET), "exec"), namespace)
    return namespace


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
        env={"HOME": str(cwd), "PATH": os.environ.get("PATH", "")},
    )


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("sample\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _write_summary_page(vault: Path, project_id: str, sections: list[list[str]]) -> Path:
    page = vault / "wiki" / "maps" / "projects" / f"{project_id}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f'title: "Project Summary: {project_id}"\n'
        "type: map\n"
        "generated_by: tools/wiki/cli.py maps build\n"
        f"project: {project_id}\n"
        "---\n"
    )
    page.write_text(frontmatter + "\n" + _render_project_summary(project_id, sections), encoding="utf-8")
    return page


def _small_sections() -> list[list[str]]:
    return [
        ["- [[inbox]] — Fix login timeout (deferred)"],
        ["- [[decisions/use-sqlite]] — Use SQLite for local state (wiki/decisions/use-sqlite.md)"],
        ["- [[rules/no-force-push]] — Never force push shared branches (wiki/rules/no-force-push.md)"],
    ]


def _large_korean_sections(label: str) -> list[list[str]]:
    def lines(kind: str) -> list[str]:
        return [
            f"- [[{kind}/{label}-{index:03d}]] — {label} 저장소의 배포 순서와 데이터 보존 기간을 정한 한국어 결정 요약 {index}번 "
            f"(wiki/{kind}/{label}-{index:03d}.md)"
            for index in range(60)
        ]

    return [
        [f"- [[inbox]] — {label} 남은 일 {index}번: 배포 스크립트 점검 (deferred)" for index in range(60)],
        lines("decisions"),
        lines("rules"),
    ]


class ProjectMemoryRecallTest(unittest.TestCase):
    """recall.sh runs with a temporary HOME and without CLAUDE_CONFIG_DIR (A-26)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name).resolve()
        self.home = self.tmp / "home"
        self.config = self.home / ".claude"
        self.script = _materialize_recall_script(
            self.config / "skills" / "project-memory" / "scripts" / "recall.sh"
        )
        self.repo = _init_git_repo(self.tmp / "work" / "sample-app")
        self.vault_a = self.tmp / "vaults" / "personal"
        self.page_a = _write_summary_page(self.vault_a, "sample-app", _small_sections())
        self._write_vault_map(
            {
                "version": 1,
                "vaults": {"personal": {"path": str(self.vault_a), "role": "personal"}},
                "projects": {"sample-app": {"root": str(self.repo), "vaults": ["personal"]}},
            }
        )

    def _write_vault_map(self, data: object, *, config: Path | None = None) -> Path:
        path = (config or self.config) / "vault-builder" / "vault-map.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
        return path

    def _recall(self, cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            **(env or {}),
        }
        # The environment is built from scratch, so CLAUDE_CONFIG_DIR is unset unless a test passes it.
        return subprocess.run(
            [str(self.script), *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )

    def _assert_one_reason_line(self, result: subprocess.CompletedProcess[str], *needles: str) -> str:
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines), result.stdout)
        for needle in needles:
            self.assertIn(needle, lines[0])
        return lines[0]

    def test_recall_prints_only_the_requested_section(self):
        for index, section in enumerate(RECALL_SECTIONS):
            with self.subTest(section=section):
                result = self._recall(self.repo, section)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stderr)
                headings = [line for line in result.stdout.splitlines() if line.startswith("## ")]
                self.assertEqual([PROJECT_SECTION_HEADINGS[index]], headings)
                self.assertIn(_small_sections()[index][0], result.stdout)
                header = result.stdout.splitlines()[0]
                self.assertIn("personal", header)
                self.assertIn(str(self.page_a), header)

        result = self._recall(self.repo)
        self.assertEqual(0, result.returncode, result.stderr)
        headings = [line for line in result.stdout.splitlines() if line.startswith("## ")]
        self.assertEqual(list(PROJECT_SECTION_HEADINGS), headings)
        self.assertNotIn("## Purpose", result.stdout)
        self.assertNotIn("generated_by", result.stdout)
        for lines in _small_sections():
            self.assertIn(lines[0], result.stdout)

    def test_recall_from_git_worktree_matches_main_worktree(self):
        worktree = self.tmp / "work" / "feature-checkout"
        _git(self.repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
        nested = worktree / "nested"
        nested.mkdir()
        for arguments in ((), *((section,) for section in RECALL_SECTIONS)):
            with self.subTest(arguments=arguments):
                main_result = self._recall(self.repo, *arguments)
                for cwd in (worktree, nested):
                    worktree_result = self._recall(cwd, *arguments)
                    self.assertEqual(0, worktree_result.returncode, worktree_result.stderr)
                    self.assertEqual(main_result.stdout, worktree_result.stdout)
                    self.assertIn("## ", worktree_result.stdout)

    def test_recall_outside_git_prints_one_reason_line(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self._assert_one_reason_line(self._recall(outside), "git")
        self._assert_one_reason_line(self._recall(outside, "open"), "git")

    def test_recall_unlinked_repository_prints_reason_and_link_lines(self):
        other = _init_git_repo(self.tmp / "work" / "other-app")
        result = self._recall(other)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(2, len(lines), result.stdout)
        self.assertIn("other-app", lines[0])
        self.assertTrue(lines[1].startswith("link: "), lines[1])

    def test_recall_missing_or_damaged_link_config_prints_one_reason_line(self):
        vault_map = self.config / "vault-builder" / "vault-map.json"
        damaged = {
            "invalid-json": "{not json",
            "not-an-object": "[]",
            "projects-not-object": json.dumps({"vaults": {}, "projects": []}),
            "project-entry-not-object": json.dumps({"vaults": {}, "projects": {"sample-app": "personal"}}),
            "unknown-vault": json.dumps(
                {"vaults": {}, "projects": {"sample-app": {"root": str(self.repo), "vaults": ["ghost"]}}}
            ),
            "vault-without-path": json.dumps(
                {"vaults": {"personal": {"role": "personal"}}, "projects": {"sample-app": {"root": "x", "vaults": ["personal"]}}}
            ),
        }
        for name, content in damaged.items():
            with self.subTest(case=name):
                vault_map.write_text(content, encoding="utf-8")
                self._assert_one_reason_line(self._recall(self.repo), "vault-map.json")
        vault_map.unlink()
        self._assert_one_reason_line(self._recall(self.repo), "vault-map.json")

    def test_recall_rejects_unknown_arguments_with_exit_2(self):
        for arguments in (("recent",), ("Open",), ("open", "decisions"), ("--help",)):
            with self.subTest(arguments=arguments):
                result = self._recall(self.repo, *arguments)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
                self.assertIn("usage: recall.sh [open|decisions|corrections]", result.stderr)

    def test_recall_skips_missing_vaults_pages_and_sections(self):
        missing_vault = self.tmp / "vaults" / "gone"
        no_page_vault = self.tmp / "vaults" / "empty"
        no_page_vault.mkdir(parents=True)
        self._write_vault_map(
            {
                "version": 1,
                "vaults": {
                    "gone": {"path": str(missing_vault), "role": "team"},
                    "empty": {"path": str(no_page_vault), "role": "team"},
                    "personal": {"path": str(self.vault_a), "role": "personal"},
                },
                "projects": {"sample-app": {"root": str(self.repo), "vaults": ["gone", "empty", "personal"]}},
            }
        )
        result = self._recall(self.repo, "open")
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("gone", lines[0])
        self.assertIn("empty", lines[1])
        self.assertIn(_small_sections()[0][0], result.stdout)

        self._write_vault_map(
            {
                "version": 1,
                "vaults": {"gone": {"path": str(missing_vault), "role": "team"}},
                "projects": {"sample-app": {"root": str(self.repo), "vaults": ["gone"]}},
            }
        )
        self._assert_one_reason_line(self._recall(self.repo), "gone")

        self._write_vault_map(
            {
                "version": 1,
                "vaults": {"personal": {"path": str(self.vault_a), "role": "personal"}},
                "projects": {"sample-app": {"root": str(self.repo), "vaults": ["personal"]}},
            }
        )
        self.page_a.write_text(
            self.page_a.read_text(encoding="utf-8").replace("## Corrections and Rules", "## Other"),
            encoding="utf-8",
        )
        result = self._recall(self.repo, "corrections")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("## Corrections and Rules", result.stdout)
        self.assertNotIn("## Open Work", result.stdout)
        self.assertNotIn("no-force-push", result.stdout)

    def test_recall_caps_two_vault_output_with_one_truncation_line(self):
        vault_b = self.tmp / "vaults" / "team"
        _write_summary_page(self.vault_a, "sample-app", _large_korean_sections("개인"))
        _write_summary_page(vault_b, "sample-app", _large_korean_sections("팀"))
        self._write_vault_map(
            {
                "version": 1,
                "vaults": {
                    "personal": {"path": str(self.vault_a), "role": "personal"},
                    "team": {"path": str(vault_b), "role": "team"},
                },
                "projects": {"sample-app": {"root": str(self.repo), "vaults": ["personal", "team"]}},
            }
        )
        cases = [((section,), PROJECT_SECTION_MAX_TOKENS) for section in RECALL_SECTIONS]
        cases.append(((), PROJECT_MAP_MAX_TOKENS))
        for arguments, limit in cases:
            with self.subTest(arguments=arguments):
                result = self._recall(self.repo, *arguments)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertLessEqual(estimate_tokens(result.stdout), limit)
                lines = result.stdout.splitlines()
                self.assertEqual(1, sum("truncated" in line for line in lines), result.stdout)
                self.assertTrue(lines[-1].startswith("project-memory: output truncated"), lines[-1])
                self.assertEqual(2, sum(line.startswith("# vault ") for line in lines))
                self.assertIn("개인", result.stdout)
                self.assertIn("팀", result.stdout)
                expected = [PROJECT_SECTION_HEADINGS[RECALL_SECTIONS.index(arguments[0])]] if arguments else list(
                    PROJECT_SECTION_HEADINGS
                )
                headings = [line for line in lines if line.startswith("## ")]
                self.assertEqual(expected * 2, headings)
                self.assertEqual(result.stdout, self._recall(self.repo, *arguments).stdout)

    def test_recall_small_output_has_no_truncation_line(self):
        result = self._recall(self.repo)
        self.assertNotIn("truncated", result.stdout)

    def test_recall_reads_link_config_from_claude_config_dir(self):
        custom = self.tmp / "custom-config"
        (self.config / "vault-builder" / "vault-map.json").unlink()
        self._write_vault_map(
            {
                "version": 1,
                "vaults": {"personal": {"path": str(self.vault_a), "role": "personal"}},
                "projects": {"sample-app": {"root": str(self.repo), "vaults": ["personal"]}},
            },
            config=custom,
        )
        result = self._recall(self.repo, "open", env={"CLAUDE_CONFIG_DIR": str(custom)})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("## Open Work", result.stdout)

    def test_recall_estimate_tokens_matches_maps(self):
        recall_estimate = _recall_namespace()["estimate_tokens"]
        samples = {
            "korean": "프로젝트 요약은 필요한 섹션만 불러온다. 한글 자모 ㄱㅏ, 옛한글 각, 확장 ꥠ ힰ.",
            "english": "Load only the requested section of the project summary.\n- [[decisions/x]] — y (wiki/x.md)",
            "mixed": "- [[decisions/결정-001]] — SQLite를 local state 저장소로 쓴다 (wiki/decisions/결정-001.md)\n",
        }
        for name, sample in samples.items():
            with self.subTest(sample=name):
                self.assertEqual(estimate_tokens(sample), recall_estimate(sample))
        for sample in ("", "a", "가", "ab가", "\n\n\n\n\n"):
            self.assertEqual(estimate_tokens(sample), recall_estimate(sample))

    def test_recall_constants_match_maps(self):
        namespace = _recall_namespace()
        self.assertEqual(PROJECT_SECTION_MAX_TOKENS, namespace["SECTION_MAX_TOKENS"])
        self.assertEqual(PROJECT_MAP_MAX_TOKENS, namespace["ALL_MAX_TOKENS"])
        self.assertEqual(
            dict(zip(RECALL_SECTIONS, PROJECT_SECTION_HEADINGS)),
            namespace["SECTION_HEADINGS"],
        )

    def test_recall_script_has_one_interpreter_placeholder_and_valid_syntax(self):
        text = RECALL_ASSET.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(RECALL_PYTHON_PLACEHOLDER))
        self.assertNotIn(RECALL_PYTHON_PLACEHOLDER, self.script.read_text(encoding="utf-8"))
        subprocess.run(["bash", "-n", str(RECALL_ASSET)], check=True)
        self.assertTrue(os.access(self.script, os.X_OK))

    def test_project_memory_skill_frontmatter_and_body(self):
        text = PROJECT_MEMORY_SKILL_ASSET.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter_end = text.index("\n---\n", 4)
        frontmatter = text[4:frontmatter_end]
        body = text[frontmatter_end + 5 :]
        self.assertLessEqual(len(frontmatter), 1536)
        fields = dict(line.split(": ", 1) for line in frontmatter.splitlines())
        self.assertEqual(["name", "description", "allowed-tools"], list(fields))
        self.assertEqual("project-memory", fields["name"])
        description = fields["description"]
        for word in ("spec", "설계", "계획", "지난 결정", "남은 일"):
            self.assertIn(word, description)
        self.assertLessEqual(estimate_tokens(description), 60)
        self.assertEqual("Bash(${CLAUDE_SKILL_DIR}/scripts/recall.sh *)", fields["allowed-tools"])
        self.assertEqual(1, frontmatter.count("allowed-tools"))
        self.assertIn("${CLAUDE_SKILL_DIR}/scripts/recall.sh", body)

    def test_project_memory_skill_assets_have_no_home_path_and_stay_out_of_catalog(self):
        for path in sorted(PROJECT_MEMORY_ASSET.rglob("*")):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("/Users/", text, path)
                self.assertNotIn("/home/", text, path)
                self.assertNotIn("~/", text, path)
        catalog_paths = json.loads((ROOT / "tools" / "vault_migration" / "contracts" / "v2.json").read_text(encoding="utf-8"))
        self.assertNotIn("assets/", json.dumps(catalog_paths))
        self.assertFalse([path for path in MANAGED_PATHS if path.startswith("tools/vault_migration/")])


class RecallProjectLookupTests(unittest.TestCase):
    """recall.sh finds the project by root, origin remote, then folder name (spec 4.3, 4.4, A-11)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name).resolve()
        self.home = self.tmp / "home"
        self.config = self.home / ".claude"
        self.script = _materialize_recall_script(
            self.config / "skills" / "project-memory" / "scripts" / "recall.sh"
        )
        self.vault = self.tmp / "vaults" / "personal"
        self.team = self.tmp / "vaults" / "team"
        _write_summary_page(self.vault, "sample-app", _small_sections())
        self.vaults = {
            "personal": {"path": str(self.vault), "role": "personal"},
            "team": {"path": str(self.team), "role": "team"},
        }

    def _write_map(self, projects: dict[str, object], *, version: object = 2) -> None:
        path = self.config / "vault-builder" / "vault-map.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, object] = {"vaults": self.vaults, "projects": projects}
        if version is not None:
            data["version"] = version
        path.write_text(json.dumps(data), encoding="utf-8")

    def _repo(self, name: str, origin: str | None = None) -> Path:
        repo = _init_git_repo(self.tmp / "work" / name)
        if origin is not None:
            _git(repo, "remote", "add", "origin", origin)
        return repo

    def _recall(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env={"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "GIT_CEILING_DIRECTORIES": str(self.tmp)},
            timeout=30,
        )

    def _relink_lines(self, stdout: str) -> list[str]:
        return [line for line in stdout.splitlines() if "link" in line and "--repo" in line]

    def test_unlinked_repository_prints_reason_and_link_line(self):
        repo = self._repo("other-app")
        self._write_map({"sample-app": {"root": str(self.tmp / "work" / "sample-app"), "vaults": ["personal"]}})
        result = self._recall(repo, "open")
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(2, len(lines), result.stdout)
        self.assertTrue(lines[0].startswith("project-memory: "), lines[0])
        self.assertIn(str(repo), lines[0])
        self.assertEqual(
            f"link: run tools/vault_migration/cli.py link <vault> --repo {repo} "
            "from the vault-builder checkout (vaults: personal, team)",
            lines[1],
        )

    def test_root_stage_finds_renamed_folder_without_relink_line(self):
        repo = self._repo("renamed-folder")
        self._write_map({"sample-app": {"root": str(repo), "vaults": ["personal"]}})
        result = self._recall(repo, "open")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("## Open Work", result.stdout)
        self.assertIn("sample-app.md", result.stdout.splitlines()[0])
        self.assertEqual([], self._relink_lines(result.stdout))

    def test_moved_repository_found_by_remote_prints_section_and_one_relink_line(self):
        cases = {
            "scp": ("git@GitHub.com:acme/sample-app.git", "https://github.com/acme/sample-app"),
            "https": ("https://user@github.com:443/acme/sample-app.git/", "ssh://git@github.com/acme/sample-app"),
        }
        for name, (origin, recorded) in cases.items():
            with self.subTest(form=name):
                repo = self._repo(f"moved-{name}", origin)
                old_root = self.tmp / "old" / "sample-app"
                self._write_map({"sample-app": {"root": str(old_root), "remote": recorded, "vaults": ["personal"]}})
                result = self._recall(repo, "open")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("## Open Work", result.stdout)
                self.assertIn(_small_sections()[0][0], result.stdout)
                relink = self._relink_lines(result.stdout)
                self.assertEqual(1, len(relink), result.stdout)
                self.assertIn(str(old_root), relink[0])
                self.assertIn(f"--repo {repo}", relink[0])

    def test_name_stage_without_origin_finds_project(self):
        repo = self._repo("sample-app")
        self._write_map({"sample-app": {"root": str(self.tmp / "elsewhere" / "sample-app"), "vaults": ["personal"]}}, version=1)
        result = self._recall(repo, "decisions")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("## Current Decisions", result.stdout)
        self.assertEqual(1, len(self._relink_lines(result.stdout)), result.stdout)

    def test_name_stage_skips_entry_with_different_remote(self):
        repo = self._repo("sample-app", "https://github.com/acme/sample-app.git")
        self._write_map(
            {
                "sample-app": {
                    "root": str(self.tmp / "elsewhere" / "sample-app"),
                    "remote": "git@github.com:other/sample-app.git",
                    "vaults": ["personal"],
                }
            }
        )
        lines = self._recall(repo).stdout.splitlines()
        self.assertEqual(2, len(lines), lines)
        self.assertTrue(lines[1].startswith("link: "), lines[1])

    def test_two_projects_with_same_remote_stop_with_ids(self):
        repo = self._repo("moved", "git@github.com:acme/sample-app.git")
        remote = "https://github.com/acme/sample-app"
        self._write_map(
            {
                "alpha": {"root": str(self.tmp / "a"), "remote": remote, "vaults": ["personal"]},
                "beta": {"root": str(self.tmp / "b"), "remote": remote, "vaults": ["personal"]},
            }
        )
        result = self._recall(repo)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines), result.stdout)
        self.assertIn("alpha", lines[0])
        self.assertIn("beta", lines[0])

    def test_unsupported_vault_map_version_prints_one_reason_line(self):
        repo = self._repo("sample-app")
        for version in (3, "2", True, None):
            with self.subTest(version=version):
                self._write_map({"sample-app": {"root": str(repo), "vaults": ["personal"]}}, version=version)
                lines = self._recall(repo).stdout.splitlines()
                self.assertEqual(1, len(lines), lines)
                self.assertIn("version", lines[0])
                self.assertIn("damaged", lines[0])

    def test_relative_local_remote_of_entry_is_read_against_its_stored_root(self):
        old_root = self.tmp / "old" / "sample-app"
        repo = self._repo("moved", str(self.tmp / "old" / "upstream.git"))
        self._write_map({"sample-app": {"root": str(old_root), "remote": "../upstream.git", "vaults": ["personal"]}})
        result = self._recall(repo, "open")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("## Open Work", result.stdout)
        relink = self._relink_lines(result.stdout)
        self.assertEqual(1, len(relink), result.stdout)
        self.assertIn("Run link again", relink[0])

    def test_normalize_remote_forms(self):
        normalize = _recall_namespace()["normalize_remote"]
        base = self.tmp / "work" / "repo"
        expected = "github.com/acme/sample-app"
        for url in (
            "git@github.com:acme/sample-app.git",
            " https://github.com/acme/sample-app/ ",
            "ssh://git@GitHub.com:22/acme/sample-app.git",
            "https://user@github.com/acme/sample-app.git/",
        ):
            with self.subTest(url=url):
                self.assertEqual(expected, normalize(url, base))
        self.assertEqual(expected, normalize("GitHub.com:acme/sample-app.git", base))
        self.assertEqual(str(self.tmp / "remotes" / "x.git"), normalize(f"file://{self.tmp}/remotes/x.git", base))
        self.assertEqual(str(self.tmp / "remotes" / "x.git"), normalize(f"{self.tmp}/remotes/x.git/", base))
        self.assertEqual(str(self.tmp / "work" / "up.git"), normalize("../up.git", base))

    def test_skill_tells_agent_to_pass_link_line_to_user(self):
        text = PROJECT_MEMORY_SKILL_ASSET.read_text(encoding="utf-8")
        reading = text[text.index("Reading the output:") :]
        items = [line for line in reading.splitlines() if line.startswith("- ")]
        self.assertTrue(any("`link:`" in item and "unchanged" in item for item in items), items)


def _tree_snapshot(*roots: Path) -> dict[str, tuple[str, int]]:
    """Return every entry beneath the roots with its kind or content hash and mode."""
    snapshot: dict[str, tuple[str, int]] = {}
    for root in roots:
        if not root.exists() and not root.is_symlink():
            continue
        for path in sorted([root, *root.rglob("*")]):
            state = path.lstat()
            if path.is_symlink():
                kind = "link:" + os.readlink(path)
            elif path.is_dir():
                kind = "dir"
            else:
                kind = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[str(path)] = (kind, state.st_mode & 0o7777)
    return snapshot


class _UserHomeTestCase(unittest.TestCase):
    """Runs with a temporary HOME and without CLAUDE_CONFIG_DIR unless a test sets it (A-26)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config = self.home / ".claude"
        self.work = self.tmp / "work"
        self.work.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def _env(self, **extra: str) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
        environment["HOME"] = str(self.home)
        environment.update(extra)
        return environment

    def _install(self, *arguments: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "tools/vault_migration/cli.py", "install", *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=self._env(**extra_env),
            timeout=300,
        )

    def _write_claude_json(self, value: object, path: Path | None = None) -> Path:
        target = path or self.home / ".claude.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value, encoding="utf-8")
        return target

    def _user_snapshot(self) -> dict[str, tuple[str, int]]:
        return _tree_snapshot(self.home)

    def _assert_installed(self, target: Path, name: str, config: Path, claude_json: Path) -> dict[str, object]:
        self.assertTrue((target / "tools" / "wiki" / "cli.py").is_file())
        self.assertTrue((target / "wiki" / "index.md").is_file())
        skill = config / "skills" / "project-memory" / "SKILL.md"
        recall = config / "skills" / "project-memory" / "scripts" / "recall.sh"
        vault_map_path = config / "vault-builder" / "vault-map.json"
        self.assertIn("name: project-memory", skill.read_text(encoding="utf-8").splitlines())
        self.assertEqual(PROJECT_MEMORY_SKILL_ASSET.read_bytes(), skill.read_bytes())
        self.assertTrue(os.access(recall, os.X_OK))
        recall_text = recall.read_text(encoding="utf-8")
        self.assertNotIn(RECALL_PYTHON_PLACEHOLDER, recall_text)
        self.assertIn(f"PYTHON={shlex.quote(sys.executable)}\n", recall_text)
        vault_map = json.loads(vault_map_path.read_text(encoding="utf-8"))
        self.assertEqual(1, vault_map["version"])
        self.assertEqual({"path": str(target), "role": "personal"}, vault_map["vaults"][name])
        self.assertIsInstance(vault_map["projects"], dict)
        claude = json.loads(claude_json.read_text(encoding="utf-8"))
        entry = claude["mcpServers"][f"llm-wiki-{name}"]
        self.assertEqual(
            {"command": sys.executable, "args": [str(target / "tools" / "wiki" / "mcp_server.py")]},
            entry,
        )
        mcp_json = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(entry, mcp_json["mcpServers"]["llm-wiki"])
        manifest = json.loads((target / ".vault-builder" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual(2, manifest["contract_version"])
        self.assertIn("tools/wiki/cli.py", manifest["managed_files"])
        records = {record["location"]: record for record in manifest["external_records"]}

        def relative(path: Path) -> str:
            return path.relative_to(self.home).as_posix()

        self.assertEqual(
            {
                relative(skill): {"kind": "file", "location": relative(skill), "sha256": sha256_file(skill), "shared": True},
                relative(recall): {"kind": "file", "location": relative(recall), "sha256": sha256_file(recall), "shared": True},
                relative(vault_map_path): {"kind": "config", "location": relative(vault_map_path), "shared": True},
                relative(claude_json): {
                    "kind": "json-entry",
                    "key_path": f"mcpServers.llm-wiki-{name}",
                    "location": relative(claude_json),
                    "sha256": normalized_json_sha256(entry),
                    "shared": False,
                },
            },
            records,
        )
        self.assertFalse((config / "settings.json").exists())
        return claude


class InstallTest(_UserHomeTestCase):
    """install runs with a temporary HOME and without CLAUDE_CONFIG_DIR unless a test sets it (A-26)."""

    def test_install_new_vault_creates_vault_user_files_and_v2_manifest(self):
        claude_json = self._write_claude_json(
            {
                "numStartups": 7,
                "projects": {"/somewhere": {"allowedTools": []}},
                "mcpServers": {"other": {"command": "node", "args": ["server.js"]}},
            }
        )
        before = json.loads(claude_json.read_text(encoding="utf-8"))
        target = self.work / "vault-a"

        result = self._install(str(target), "--name", "a")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        claude = self._assert_installed(target, "a", self.config, claude_json)
        self.assertEqual({key: value for key, value in before.items() if key != "mcpServers"},
                         {key: value for key, value in claude.items() if key != "mcpServers"})
        self.assertEqual(before["mcpServers"]["other"], claude["mcpServers"]["other"])
        self.assertEqual(["other", "llm-wiki-a"], list(claude["mcpServers"]))
        self.assertEqual([], [path.name for path in self.work.iterdir() if path.name != "vault-a"])
        self.assertEqual([], sorted(str(path) for path in self.home.rglob("*vault-builder-backup*")))
        next_lines = result.stdout.split("Next:\n", 1)[1].splitlines()
        commands = [line.strip() for line in next_lines if "tools/wiki/cli.py" in line]
        self.assertTrue(commands, result.stdout)
        for command in commands:
            self.assertTrue(command.startswith(shlex.quote(sys.executable) + " "), command)
        # The installed vault is self-consistent: lint and the recall script both run.
        lint = subprocess.run(
            [sys.executable, "tools/wiki/cli.py", "lint"], cwd=target, text=True, capture_output=True, check=False
        )
        self.assertEqual(0, lint.returncode, lint.stdout + lint.stderr)
        recall = subprocess.run(
            [str(self.config / "skills" / "project-memory" / "scripts" / "recall.sh")],
            cwd=self.tmp,
            text=True,
            capture_output=True,
            check=False,
            env={**self._env(), "GIT_CEILING_DIRECTORIES": str(self.tmp)},
        )
        self.assertEqual(0, recall.returncode, recall.stdout + recall.stderr)

    def test_install_creates_claude_json_when_absent_and_new_vault_git_status_omits_mcp_json(self):
        target = self.work / "notes"

        result = self._install(str(target), "--name", "a")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(target, "a", self.config, self.home / ".claude.json")
        self.assertIn(".mcp.json", (target / ".gitignore").read_text(encoding="utf-8").splitlines())
        _git(target, "init", "-q")
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=target,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertNotIn(".mcp.json", status)
        self.assertNotIn("__pycache__", status)
        self.assertIn("tools/wiki/cli.py", status)

    def test_install_second_vault_shares_user_files_and_adds_its_own_entry(self):
        first = self.work / "a"
        second = self.work / "b"
        self.assertEqual(0, self._install(str(first)).returncode)
        shared = _tree_snapshot(self.config / "skills")

        result = self._install(str(second))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(shared, _tree_snapshot(self.config / "skills"))
        vault_map = json.loads((self.config / "vault-builder" / "vault-map.json").read_text(encoding="utf-8"))
        self.assertEqual(["a", "b"], list(vault_map["vaults"]))
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(["llm-wiki-a", "llm-wiki-b"], list(claude["mcpServers"]))
        self._assert_installed(second, "b", self.config, self.home / ".claude.json")

    def test_install_uses_claude_config_dir_when_set(self):
        config = self.home / "custom-config"
        target = self.work / "vault-a"

        result = self._install(str(target), "--name", "a", CLAUDE_CONFIG_DIR=str(config))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(target, "a", config, config / ".claude.json")
        self.assertFalse(self.config.exists())
        self.assertFalse((self.home / ".claude.json").exists())

    def test_install_rejects_claude_config_dir_outside_home(self):
        outside = self.tmp / "outside-config"
        target = self.work / "vault-a"
        before = _tree_snapshot(self.tmp)

        result = self._install(str(target), "--name", "a", CLAUDE_CONFIG_DIR=str(outside))

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR", result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_install_default_name_is_target_basename_or_requires_name(self):
        target = self.work / "my-notes"
        self.assertEqual(0, self._install(str(target)).returncode)
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertIn("llm-wiki-my-notes", claude["mcpServers"])

        odd = self.work / "my notes"
        before = self._user_snapshot()
        result = self._install(str(odd))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("--name", result.stderr)
        self.assertFalse(odd.exists())
        self.assertEqual(before, self._user_snapshot())

        result = self._install(str(odd), "--name", "bad name")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertFalse(odd.exists())

    def test_install_rejects_invalid_claude_json_without_writing(self):
        self._write_claude_json("{not json")
        target = self.work / "vault-a"
        before = self._user_snapshot()

        result = self._install(str(target), "--name", "a")

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(".claude.json", result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.work.iterdir()))
        self.assertEqual(before, self._user_snapshot())

    def test_install_rejects_name_used_by_another_vault(self):
        other = self.work / "other"
        self.assertEqual(0, self._install(str(other), "--name", "a").returncode)
        target = self.work / "vault-a"
        before = self._user_snapshot()

        result = self._install(str(target), "--name", "a")

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("--name", result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(before, self._user_snapshot())

    def test_install_replaces_entry_whose_vault_no_longer_exists(self):
        gone = self.work / "gone"
        self.assertEqual(0, self._install(str(gone), "--name", "a").returncode)
        shutil.rmtree(gone)
        target = self.work / "vault-a"

        result = self._install(str(target), "--name", "a")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(target, "a", self.config, self.home / ".claude.json")

    def test_install_rejects_non_empty_target_and_accepts_empty_directory(self):
        busy = self.work / "busy"
        busy.mkdir()
        (busy / "notes.txt").write_text("keep\n", encoding="utf-8")
        before = _tree_snapshot(self.tmp)
        result = self._install(str(busy), "--name", "busy")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

        empty = self.work / "empty"
        empty.mkdir()
        result = self._install(str(empty), "--name", "empty")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(empty, "empty", self.config, self.home / ".claude.json")
        self.assertEqual(0o755, empty.stat().st_mode & 0o777)

    def test_install_keeps_unknown_user_skill_and_lists_it(self):
        skill = self.config / "skills" / "project-memory" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("my own skill\n", encoding="utf-8")
        target = self.work / "vault-a"

        result = self._install(str(target), "--name", "a")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("my own skill\n", skill.read_text(encoding="utf-8"))
        self.assertIn(str(skill), result.stdout)
        manifest = json.loads((target / ".vault-builder" / "manifest.json").read_text(encoding="utf-8"))
        locations = [record["location"] for record in manifest["external_records"]]
        self.assertNotIn(".claude/skills/project-memory/SKILL.md", locations)
        self.assertIn(".claude/skills/project-memory/scripts/recall.sh", locations)

    def test_install_replaces_recall_script_built_for_another_interpreter(self):
        recall = self.config / "skills" / "project-memory" / "scripts" / "recall.sh"
        recall.parent.mkdir(parents=True)
        recall.write_text(
            RECALL_ASSET.read_text(encoding="utf-8").replace(RECALL_PYTHON_PLACEHOLDER, "/old/python3.10"),
            encoding="utf-8",
        )
        target = self.work / "vault-a"

        result = self._install(str(target), "--name", "a")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(target, "a", self.config, self.home / ".claude.json")

    def test_install_dry_run_writes_nothing(self):
        self._write_claude_json({"mcpServers": {}})
        target = self.work / "vault-a"
        before = _tree_snapshot(self.tmp)

        result = self._install(str(target), "--name", "a", "--dry-run")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertIn("llm-wiki-a", result.stdout)
        self.assertEqual(before, _tree_snapshot(self.tmp))

        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        (self.config / "skills" / "project-memory" / "SKILL.md").unlink()
        before = _tree_snapshot(self.tmp)
        result = self._install(str(target), "--name", "a", "--dry-run")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_install_existing_vault_reinstalls_user_files_only(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        shutil.rmtree(self.config)
        (self.home / ".claude.json").unlink()
        (target / ".mcp.json").unlink()
        vault_files = {
            path: digest
            for path, digest in _tree_snapshot(target).items()
            if not path.endswith((".mcp.json", "manifest.json"))
        }

        result = self._install(str(target), "--name", "a")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(target, "a", self.config, self.home / ".claude.json")
        self.assertEqual(
            vault_files,
            {
                path: digest
                for path, digest in _tree_snapshot(target).items()
                if not path.endswith((".mcp.json", "manifest.json"))
            },
        )

    def test_install_existing_vault_refuses_when_audit_needs_changes(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        shutil.rmtree(self.config)
        (self.home / ".claude.json").unlink()
        (target / "docs" / "usage.md").unlink()
        before = _tree_snapshot(self.tmp)

        result = self._install(str(target), "--name", "a")

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("audit", result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_install_existing_vault_refuses_another_name(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        before = _tree_snapshot(self.tmp)

        result = self._install(str(target), "--name", "b")

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("mcpServers.llm-wiki-a", result.stderr)
        self.assertIn("uninstall", result.stderr)
        self.assertIn("--name a", result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_install_existing_vault_refuses_another_config_folder(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        before = _tree_snapshot(self.tmp)

        result = self._install(str(target), "--name", "a", CLAUDE_CONFIG_DIR=str(self.home / "custom-config"))

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR", result.stderr)
        self.assertIn("uninstall", result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_install_assembly_failure_creates_no_target(self):
        claude_json = self._write_claude_json({"mcpServers": {}})
        target = self.work / "vault-a"
        before = _tree_snapshot(self.tmp)

        with mock.patch.object(installing, "_self_check", side_effect=installing.InstallError("lint failed")):
            with self.assertRaisesRegex(installing.InstallError, "lint failed"):
                installing.install(target, name="a", out=io.StringIO())

        self.assertFalse(target.exists())
        self.assertEqual(before, _tree_snapshot(self.tmp))
        self.assertTrue(claude_json.exists())

    def test_install_failure_outside_vault_restores_user_files_and_removes_target(self):
        for failing in ("SKILL.md", "recall.sh", "vault-map.json", ".claude.json", "manifest.json"):
            with self.subTest(failing=failing):
                # Existing shared files of another vault, so restore must bring back prior bytes.
                home = self.tmp / f"home-{failing}"
                home.mkdir()
                os.environ["HOME"] = str(home)
                self.home = home
                self.config = home / ".claude"
                self.assertEqual(0, installing.install(self.work / f"first-{failing}", name="first", out=io.StringIO()))
                # Every outside file now needs a write: create the skill, replace recall.sh, update both JSON files.
                (self.config / "skills" / "project-memory" / "SKILL.md").unlink()
                (self.config / "skills" / "project-memory" / "scripts" / "recall.sh").write_text(
                    RECALL_ASSET.read_text(encoding="utf-8").replace(RECALL_PYTHON_PLACEHOLDER, "/old/python3.11"),
                    encoding="utf-8",
                )
                target = self.work / f"vault-{failing}"
                before = _tree_snapshot(home)

                def fail(path: Path, failing: str = failing) -> None:
                    if path.name == failing:
                        raise OSError(f"injected failure: {path.name}")

                with mock.patch.object(installing, "_before_write", side_effect=fail):
                    with self.assertRaisesRegex(installing.InstallError, "injected failure"):
                        installing.install(target, name="a", out=io.StringIO())

                self.assertFalse(target.exists())
                self.assertEqual(before, _tree_snapshot(home))
                self.assertEqual([], [path.name for path in self.work.iterdir() if f"vault-{failing}" in path.name])

    def test_install_python_check_rejects_old_versions(self):
        with self.assertRaisesRegex(installing.InstallError, "3.10"):
            installing.require_python((3, 9, 18))
        installing.require_python((3, 10, 0))

    def test_init_vault_builds_install_structure_and_prints_chosen_python(self):
        target = self.work / "init-vault"
        result = subprocess.run(
            ["bash", "init-vault.sh", str(target)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=self._env(PYTHON=sys.executable),
            timeout=300,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self._assert_installed(target, "init-vault", self.config, self.home / ".claude.json")
        next_lines = [line.strip() for line in result.stdout.split("Next:\n", 1)[1].splitlines()]
        commands = [line for line in next_lines if "tools/wiki/cli.py" in line]
        self.assertEqual(2, len(commands), result.stdout)
        for command in commands:
            self.assertFalse(command.startswith("python3 "), command)
            self.assertTrue(command.startswith(shlex.quote(sys.executable) + " "), command)

    def test_init_vault_with_old_python_only_writes_nothing(self):
        stub_bin = self.tmp / "bin"
        stub_bin.mkdir()
        for command in ("dirname", "uname"):
            resolved = shutil.which(command)
            assert resolved is not None, command
            (stub_bin / command).symlink_to(resolved)
        stub_python = stub_bin / "python3"
        stub_python.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "Python 3.9.18"; exit 0; fi\n'
            "cat >/dev/null\n"
            "exit 1\n",
            encoding="utf-8",
        )
        stub_python.chmod(0o755)
        target = self.work / "vault-a"
        before = _tree_snapshot(self.home, self.work)

        result = subprocess.run(
            [shutil.which("bash") or "bash", str(ROOT / "init-vault.sh"), str(target)],
            cwd=self.tmp,
            text=True,
            capture_output=True,
            check=False,
            env={"HOME": str(self.home), "PATH": str(stub_bin)},
        )

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("Python 3.10 or newer", result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(before, _tree_snapshot(self.home, self.work))


class UninstallDoctorTest(_UserHomeTestCase):
    """uninstall and doctor run with a temporary HOME and without CLAUDE_CONFIG_DIR (A-15, A-16, A-17, A-26)."""

    def _cli(self, command: str, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "tools/vault_migration/cli.py", command, *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env or self._env(),
            timeout=300,
        )

    @property
    def skill(self) -> Path:
        return self.config / "skills" / "project-memory" / "SKILL.md"

    @property
    def recall(self) -> Path:
        return self.config / "skills" / "project-memory" / "scripts" / "recall.sh"

    @property
    def vault_map(self) -> Path:
        return self.config / "vault-builder" / "vault-map.json"

    def _claude(self) -> dict[str, object]:
        return json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))

    def _knowledge_snapshot(self, vault: Path) -> dict[str, tuple[str, int]]:
        return _tree_snapshot(*(vault / name for name in ("raw", "wiki", "scratch", ".git")))

    def test_uninstall_dry_run_writes_nothing(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        before = _tree_snapshot(self.tmp)

        result = self._cli("uninstall", str(target), "--dry-run")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertIn("mcpServers.llm-wiki-a", result.stdout)
        self.assertIn("Nothing was written.", result.stdout)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_uninstall_vault_a_then_b_removes_only_owned_files(self):
        claude_json = self._write_claude_json(
            {
                "numStartups": 3,
                "projects": {"/somewhere": {"allowedTools": []}},
                "mcpServers": {"other": {"command": "node", "args": ["server.js"]}},
            }
        )
        first = self.work / "vault-a"
        second = self.work / "vault-b"
        self.assertEqual(0, self._install(str(first), "--name", "a").returncode)
        self.assertEqual(0, self._install(str(second), "--name", "b").returncode)
        # A later install from another interpreter rewrites the shared recall.sh, so neither
        # vault's recorded hash matches; it is still the builder asset form.
        self.recall.write_text(
            RECALL_ASSET.read_text(encoding="utf-8").replace(RECALL_PYTHON_PLACEHOLDER, "/other/python3.11"),
            encoding="utf-8",
        )
        vault_map = json.loads(self.vault_map.read_text(encoding="utf-8"))
        vault_map["projects"] = {
            "app": {"root": str(self.work / "app"), "vaults": ["a", "b"]},
            "lib": {"root": str(self.work / "lib"), "vaults": ["a"]},
        }
        self.vault_map.write_text(json.dumps(vault_map, indent=2), encoding="utf-8")
        # Knowledge, an unmanaged file, a user-modified managed file, and git history in vault a.
        (first / "raw" / "sources" / "note.md").write_text("Raw note.\n", encoding="utf-8")
        (first / "scratch" / "reports" / "report.md").write_text("Report.\n", encoding="utf-8")
        (first / "tools" / "project_signal_builder.py").write_text("print('mine')\n", encoding="utf-8")
        with (first / "docs" / "usage.md").open("a", encoding="utf-8") as handle:
            handle.write("\nMy local note.\n")
        _git(first, "init", "-q")
        _git(first, "add", "-A")
        _git(first, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "seed")
        manifest = json.loads((first / ".vault-builder" / "manifest.json").read_text(encoding="utf-8"))
        managed = sorted(manifest["managed_files"])
        knowledge = self._knowledge_snapshot(first)
        claude_before = self._claude()
        shared_before = _tree_snapshot(self.config / "skills")

        result = self._cli("uninstall", str(first))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("docs/usage.md", result.stdout)
        remaining = sorted(path for path in managed if (first / path).exists())
        self.assertEqual([".gitignore", "docs/usage.md"], remaining)
        self.assertFalse((first / ".mcp.json").exists())
        self.assertFalse((first / ".vault-builder").exists())
        self.assertTrue((first / "tools" / "project_signal_builder.py").is_file())
        self.assertEqual(knowledge, self._knowledge_snapshot(first))
        self.assertEqual(shared_before, _tree_snapshot(self.config / "skills"))
        vault_map = json.loads(self.vault_map.read_text(encoding="utf-8"))
        self.assertEqual(["b"], list(vault_map["vaults"]))
        self.assertEqual(["b"], vault_map["projects"]["app"]["vaults"])
        self.assertEqual([], vault_map["projects"]["lib"]["vaults"])
        claude = self._claude()
        self.assertEqual(["other", "llm-wiki-b"], list(claude["mcpServers"]))
        expected = dict(claude_before)
        expected["mcpServers"] = {
            key: value for key, value in claude_before["mcpServers"].items() if key != "llm-wiki-a"
        }
        self.assertEqual(expected, claude)
        self.assertTrue((second / ".vault-builder" / "manifest.json").is_file())

        result = self._cli("uninstall", str(second))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.config / "skills" / "project-memory").exists())
        self.assertFalse((self.config / "vault-builder").exists())
        claude = self._claude()
        expected["mcpServers"] = {"other": {"command": "node", "args": ["server.js"]}}
        self.assertEqual(expected, claude)
        self.assertEqual(json.loads(claude_json.read_text(encoding="utf-8")), claude)
        self.assertTrue((second / "wiki" / "index.md").is_file())
        self.assertFalse((second / "tools" / "wiki" / "cli.py").exists())
        self.assertFalse((self.config / "settings.json").exists())
        self.assertEqual([], sorted(str(path) for path in self.home.rglob("*vault-builder-backup*")))

    def test_uninstall_last_vault_keeps_changed_entry_and_unknown_recall(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        claude = self._claude()
        claude["mcpServers"]["llm-wiki-a"]["env"] = {"DEBUG": "1"}
        self._write_claude_json(claude)
        with self.recall.open("a", encoding="utf-8") as handle:
            handle.write("# my change\n")

        result = self._cli("uninstall", str(target))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("llm-wiki-a", self._claude()["mcpServers"])
        self.assertIn("mcpServers.llm-wiki-a", result.stdout)
        self.assertTrue(self.recall.is_file())
        self.assertIn(str(self.recall), result.stdout)
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.vault_map.exists())
        self.assertFalse((self.config / "vault-builder").exists())

    def test_uninstall_v1_manifest_processes_vault_only(self):
        target = _initialize_v1_vault(self.work)
        claude_json = self._write_claude_json({"mcpServers": {"llm-wiki-vault": {"command": "x", "args": []}}})
        before_home = _tree_snapshot(self.home)

        result = self._cli("uninstall", str(target))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("schema 1", result.stdout)
        self.assertEqual(before_home, _tree_snapshot(self.home))
        self.assertTrue(claude_json.is_file())
        self.assertFalse((target / ".vault-builder").exists())
        self.assertFalse((target / "tools" / "wiki" / "cli.py").exists())
        self.assertTrue((target / ".gitignore").is_file())
        self.assertTrue((target / "wiki" / "index.md").is_file())

    def test_uninstall_without_manifest_fails_and_writes_nothing(self):
        target = self.work / "plain"
        (target / "wiki").mkdir(parents=True)
        (target / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
        before = _tree_snapshot(self.tmp)

        result = self._cli("uninstall", str(target))

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("manifest", result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_uninstall_rejects_invalid_claude_json_and_writes_nothing(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        self._write_claude_json("{broken")
        before = _tree_snapshot(self.tmp)

        result = self._cli("uninstall", str(target))

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(".claude.json", result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

    def test_uninstall_and_doctor_refuse_unexpected_outside_records(self):
        settings = self.config / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"theme": "dark"}\n', encoding="utf-8")
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        manifest_path = target / ".vault-builder" / "manifest.json"
        saved = manifest_path.read_bytes()

        def edit(kind: str, field: str, value: str) -> None:
            manifest = json.loads(saved)
            record = next(record for record in manifest["external_records"] if record["kind"] == kind)
            record[field] = value
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        cases = {
            "config location": (lambda: edit("config", "location", ".claude/settings.json"), ".claude/settings.json"),
            "file location": (lambda: edit("file", "location", ".claude/settings.json"), ".claude/settings.json"),
            "json-entry location": (lambda: edit("json-entry", "location", ".claude/settings.json"), ".claude/settings.json"),
            "json-entry key": (lambda: edit("json-entry", "key_path", "mcpServers.other"), "mcpServers.other"),
        }
        for label, (damage, expected) in cases.items():
            with self.subTest(case=label):
                damage()
                try:
                    before = _tree_snapshot(self.tmp)
                    for arguments in (("--dry-run",), ()):
                        result = self._cli("uninstall", str(target), *arguments)
                        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                        self.assertIn(expected, result.stderr)
                        self.assertEqual(before, _tree_snapshot(self.tmp))
                    result = self._cli("doctor", str(target))
                    self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                    problem_lines = [line for line in result.stdout.splitlines() if line.startswith("Problem:")]
                    self.assertTrue(any(expected in line for line in problem_lines), result.stdout)
                    self.assertEqual('{"theme": "dark"}\n', settings.read_text(encoding="utf-8"))
                finally:
                    manifest_path.write_bytes(saved)

        vault_map_saved = self.vault_map.read_bytes()
        for label, value in {
            "no version": {"vaults": {}},
            "vaults not an object": {"version": 1, "vaults": []},
            "no vaults": {"version": 1, "theme": "dark"},
        }.items():
            with self.subTest(vault_map=label):
                self.vault_map.write_text(json.dumps(value), encoding="utf-8")
                try:
                    before = _tree_snapshot(self.tmp)
                    for arguments in (("--dry-run",), ()):
                        result = self._cli("uninstall", str(target), *arguments)
                        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                        self.assertIn("vault-map.json", result.stderr)
                        self.assertEqual(before, _tree_snapshot(self.tmp))
                finally:
                    self.vault_map.write_bytes(vault_map_saved)
        self.assertEqual(0, self._cli("doctor", str(target)).returncode)

    def test_uninstall_removes_every_mcp_entry_record(self):
        # Manifests written before install refused a second name hold two json-entry records.
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        claude = self._claude()
        entry = claude["mcpServers"]["llm-wiki-a"]
        claude["mcpServers"]["llm-wiki-b"] = dict(entry)
        claude["mcpServers"]["other"] = {"command": "node", "args": ["server.js"]}
        self._write_claude_json(claude)
        vault_map = json.loads(self.vault_map.read_text(encoding="utf-8"))
        vault_map["vaults"]["b"] = {"path": str(target), "role": "personal"}
        self.vault_map.write_text(json.dumps(vault_map, indent=2), encoding="utf-8")
        manifest_path = target / ".vault-builder" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        first = next(record for record in manifest["external_records"] if record["kind"] == "json-entry")
        manifest["external_records"].append({**first, "key_path": "mcpServers.llm-wiki-b"})
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        dry_run = self._cli("uninstall", str(target), "--dry-run")
        self.assertEqual(0, dry_run.returncode, dry_run.stdout + dry_run.stderr)
        self.assertIn("mcpServers.llm-wiki-a", dry_run.stdout)
        self.assertIn("mcpServers.llm-wiki-b", dry_run.stdout)

        result = self._cli("uninstall", str(target))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["other"], list(self._claude()["mcpServers"]))
        self.assertFalse(self.vault_map.exists())
        self.assertFalse((self.config / "skills" / "project-memory").exists())

    def test_doctor_healthy_vault_exits_zero_and_each_problem_exits_one(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        before = _tree_snapshot(self.tmp)

        result = self._cli("doctor", str(target))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, _tree_snapshot(self.tmp))

        claude_json = self.home / ".claude.json"
        problems = {
            "skill": (self.skill, lambda: self.skill.unlink(), "SKILL.md"),
            "recall": (self.recall, lambda: self.recall.unlink(), "recall.sh"),
            "vault map": (self.vault_map, lambda: self.vault_map.write_text("{broken", encoding="utf-8"), "vault-map.json"),
            "mcp entry": (
                claude_json,
                lambda: self._write_claude_json({"mcpServers": {}}),
                "llm-wiki-a",
            ),
            "modified managed file": (
                target / "AGENTS.md",
                lambda: (target / "AGENTS.md").write_text("changed\n", encoding="utf-8"),
                "AGENTS.md",
            ),
        }
        for label, (path, damage, expected) in problems.items():
            with self.subTest(problem=label):
                saved = path.read_bytes()
                mode = path.stat().st_mode & 0o7777
                damage()
                try:
                    result = self._cli("doctor", str(target))
                    self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                    problem_lines = [line for line in result.stdout.splitlines() if line.startswith("Problem:")]
                    self.assertTrue(any(expected in line for line in problem_lines), result.stdout)
                finally:
                    path.write_bytes(saved)
                    path.chmod(mode)
        self.assertEqual(0, self._cli("doctor", str(target)).returncode)

    def test_doctor_regenerates_missing_or_stale_mcp_json(self):
        target = self.work / "vault-a"
        self.assertEqual(0, self._install(str(target), "--name", "a").returncode)
        mcp_json = target / ".mcp.json"
        expected = {"command": sys.executable, "args": [str(target / "tools" / "wiki" / "mcp_server.py")]}
        old_python = self.tmp / "old-python"
        old_python.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -c "import sys; sys.exit(1)"\n',
            encoding="utf-8",
        )
        old_python.chmod(0o755)
        cases = {
            "missing": lambda: mcp_json.unlink(),
            "other vault": lambda: mcp_json.write_text(
                json.dumps({"mcpServers": {"llm-wiki": {"command": sys.executable, "args": ["/elsewhere/tools/wiki/mcp_server.py"]}}}),
                encoding="utf-8",
            ),
            "old python": lambda: mcp_json.write_text(
                json.dumps({"mcpServers": {"llm-wiki": {"command": str(old_python), "args": expected["args"]}}}),
                encoding="utf-8",
            ),
        }
        for label, damage in cases.items():
            with self.subTest(case=label):
                damage()
                result = self._cli("doctor", str(target))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("Regenerated", result.stdout)
                self.assertEqual(expected, json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]["llm-wiki"])

    def test_doctor_checks_claude_mcp_list_when_claude_config_dir_is_set(self):
        config = self.home / "custom-config"
        target = self.work / "vault-a"
        self.assertEqual(
            0, self._install(str(target), "--name", "a", CLAUDE_CONFIG_DIR=str(config)).returncode
        )
        stub_bin = self.tmp / "bin"
        stub_bin.mkdir()
        claude = stub_bin / "claude"
        listing = self.tmp / "listing.txt"
        claude.write_text('#!/bin/sh\ncat "$CLAUDE_STUB_LISTING"\n', encoding="utf-8")
        claude.chmod(0o755)
        environment = self._env(
            CLAUDE_CONFIG_DIR=str(config),
            PATH=f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            CLAUDE_STUB_LISTING=str(listing),
        )

        listing.write_text("llm-wiki-a: python mcp_server.py - Connected\n", encoding="utf-8")
        result = self._cli("doctor", str(target), env=environment)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

        listing.write_text("other: node server.js - Connected\n", encoding="utf-8")
        result = self._cli("doctor", str(target), env=environment)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("claude mcp list", result.stdout)


class LinkingTests(unittest.TestCase):
    """link, unlink, and list edit the link config only (spec 4.4, A-12)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.vault_map = self.home / ".claude" / "vault-builder" / "vault-map.json"
        self.vault_map.parent.mkdir(parents=True)
        self.vault_map.write_text(
            json.dumps(
                {
                    "version": 1,
                    "vaults": {
                        "a": {"path": str(self.tmp / "vault-a"), "role": "personal"},
                        "b": {"path": str(self.tmp / "missing-b"), "role": "team"},
                    },
                    "projects": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.tmp / "vault-a").mkdir()
        self.mcp_config = self.home / ".claude.json"
        self.mcp_config.write_text('{"mcpServers": {"other": {"command": "x"}}}\n', encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment.pop("CLAUDE_CONFIG_DIR", None)
        return environment

    def _cli(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "vault_migration" / "cli.py"), *arguments],
            cwd=cwd or ROOT,
            env=self._env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def _repo(self, name: str, origin: str | None = None) -> Path:
        repo = self.tmp / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=self._env())
        if origin is not None:
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True, env=self._env())
        return repo

    def _map(self) -> dict[str, object]:
        return json.loads(self.vault_map.read_text(encoding="utf-8"))

    def _digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_link_creates_version_2_project_and_repeat_keeps_bytes(self):
        repo = self._repo("r", "git@github.com:me/r.git")
        mcp_before = self._digest(self.mcp_config)
        result = self._cli("link", "a", "--repo", str(repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        data = self._map()
        self.assertEqual(2, data["version"])
        self.assertEqual(
            {"root": str(repo), "vaults": ["a"], "remote": "git@github.com:me/r.git"}, data["projects"]["r"]
        )
        before = self.vault_map.read_bytes()
        again = self._cli("link", "a", "--repo", str(repo))
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())
        self.assertEqual(mcp_before, self._digest(self.mcp_config))

    def test_link_uses_cwd_and_adds_team_vault(self):
        repo = self._repo("r")
        self.assertEqual(0, self._cli("link", "a", cwd=repo).returncode)
        result = self._cli("link", "b", cwd=repo)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({"root": str(repo), "vaults": ["a", "b"]}, self._map()["projects"]["r"])

    def test_link_after_move_updates_root_of_same_id(self):
        repo = self._repo("r", "https://github.com/me/r.git")
        self.assertEqual(0, self._cli("link", "a", "--repo", str(repo)).returncode)
        moved = self.tmp / "moved" / "renamed"
        moved.parent.mkdir()
        repo.rename(moved)
        result = self._cli("link", "a", "--repo", str(moved))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        projects = self._map()["projects"]
        self.assertEqual(["r"], list(projects))
        self.assertEqual(str(moved), projects["r"]["root"])
        self.assertEqual(["a"], projects["r"]["vaults"])

    def test_link_refuses_same_name_other_repo_until_project_given(self):
        first = self._repo("one/r", "git@github.com:me/r.git")
        self.assertEqual(0, self._cli("link", "a", "--repo", str(first)).returncode)
        other = self._repo("two/r", "git@github.com:someone/r.git")
        before = self.vault_map.read_bytes()
        refused = self._cli("link", "a", "--repo", str(other))
        self.assertEqual(1, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("--project", refused.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())
        created = self._cli("link", "a", "--repo", str(other), "--project", "r2")
        self.assertEqual(0, created.returncode, created.stdout + created.stderr)
        projects = self._map()["projects"]
        self.assertEqual(str(other), projects["r2"]["root"])
        self.assertEqual(str(first), projects["r"]["root"])

    def test_link_refuses_project_that_differs_from_found_id(self):
        repo = self._repo("r")
        self.assertEqual(0, self._cli("link", "a", "--repo", str(repo)).returncode)
        before = self.vault_map.read_bytes()
        result = self._cli("link", "b", "--repo", str(repo), "--project", "other")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("r", result.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())

    def test_link_refuses_unknown_vault_and_folder_outside_git(self):
        repo = self._repo("r")
        before = self.vault_map.read_bytes()
        unknown = self._cli("link", "zzz", "--repo", str(repo))
        self.assertEqual(1, unknown.returncode)
        self.assertIn("a, b", unknown.stderr)
        plain = self.tmp / "plain"
        plain.mkdir()
        outside = self._cli("link", "a", "--repo", str(plain))
        self.assertEqual(1, outside.returncode)
        self.assertIn("git", outside.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())

    def test_unlink_last_vault_removes_project_and_missing_link_exits_0(self):
        repo = self._repo("r", "git@github.com:me/r.git")
        self.assertEqual(0, self._cli("link", "a", "--repo", str(repo)).returncode)
        self.assertEqual(0, self._cli("link", "b", "--repo", str(repo)).returncode)
        mcp_before = self._digest(self.mcp_config)
        first = self._cli("unlink", "b", "--repo", str(repo))
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        self.assertEqual(["a"], self._map()["projects"]["r"]["vaults"])
        last = self._cli("unlink", "a", "--repo", str(repo))
        self.assertEqual(0, last.returncode, last.stdout + last.stderr)
        data = self._map()
        self.assertEqual(2, data["version"])
        self.assertEqual({}, data["projects"])
        before = self.vault_map.read_bytes()
        again = self._cli("unlink", "a", "--repo", str(repo))
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertEqual(1, len(again.stdout.strip().splitlines()))
        self.assertEqual(before, self.vault_map.read_bytes())
        self.assertEqual(mcp_before, self._digest(self.mcp_config))

    def test_unlink_vault_not_in_project_exits_0_without_writing(self):
        repo = self._repo("r")
        self.assertEqual(0, self._cli("link", "a", "--repo", str(repo)).returncode)
        before = self.vault_map.read_bytes()
        result = self._cli("unlink", "b", "--repo", str(repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())

    def test_list_and_dry_run_write_nothing(self):
        repo = self._repo("r", "git@github.com:me/r.git")
        map_before = self._digest(self.vault_map)
        mcp_before = self._digest(self.mcp_config)
        dry_link = self._cli("link", "a", "--repo", str(repo), "--dry-run")
        self.assertEqual(0, dry_link.returncode, dry_link.stdout + dry_link.stderr)
        self.assertEqual(map_before, self._digest(self.vault_map))
        listed = self._cli("list", "--repo", str(repo))
        self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
        self.assertIn("not linked", listed.stdout)
        self.assertEqual(map_before, self._digest(self.vault_map))

        self.assertEqual(0, self._cli("link", "a", "--repo", str(repo)).returncode)
        map_before = self._digest(self.vault_map)
        dry_unlink = self._cli("unlink", "a", "--repo", str(repo), "--dry-run")
        self.assertEqual(0, dry_unlink.returncode, dry_unlink.stdout + dry_unlink.stderr)
        self.assertEqual(map_before, self._digest(self.vault_map))
        listed = self._cli("list", "--repo", str(repo))
        self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
        self.assertEqual(map_before, self._digest(self.vault_map))
        self.assertEqual(mcp_before, self._digest(self.mcp_config))
        output = listed.stdout
        self.assertIn("a", output)
        self.assertIn("personal", output)
        self.assertIn(str(self.tmp / "missing-b"), output)
        self.assertIn("missing", output)
        self.assertIn("git@github.com:me/r.git", output)
        self.assertIn("linked to project r by root", output)

    def test_list_reports_stage_and_relink_note_after_move(self):
        repo = self._repo("r", "git@github.com:me/r.git")
        self.assertEqual(0, self._cli("link", "a", "--repo", str(repo)).returncode)
        moved = self.tmp / "elsewhere"
        repo.rename(moved)
        listed = self._cli("list", "--repo", str(moved))
        self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
        self.assertIn("linked to project r by remote", listed.stdout)
        self.assertIn("Run link again", listed.stdout)

    def test_all_commands_exit_1_without_link_config(self):
        self.vault_map.unlink()
        repo = self._repo("r")
        for arguments in (("link", "a"), ("unlink", "a"), ("list",)):
            with self.subTest(command=arguments[0]):
                result = self._cli(*arguments, "--repo", str(repo))
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("Run install first", result.stderr)
                self.assertFalse(self.vault_map.exists())
