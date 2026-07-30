from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

from tools.vault_migration import cli
from tools.vault_migration import applying
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
from tools.wiki.lib.maps import build_navigation_maps


ROOT = Path(__file__).resolve().parents[1]


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
            self.assertEqual(1, manifest.contract_version)
            self.assertEqual("test-revision", manifest.builder_revision)
            self.assertNotIn("wiki/overview.md", manifest.managed_files)
            self.assertNotIn("tools/vault_migration/cli.py", manifest.managed_files)
            for relative, managed in manifest.managed_files.items():
                self.assertEqual(managed.sha256, sha256_file(target / relative), relative)

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
        managed = catalog.managed_files["scripts/release_gate.sh"]
        contents = subprocess.run(
            ["git", "show", "47cbf83:scripts/release_gate.sh"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout

        self.assertEqual(hashlib.sha256(contents).hexdigest(), managed.sha256)
        self.assertEqual("0755", managed.mode)

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

    def test_v1_catalog_matches_current_managed_files(self):
        catalog = load_catalog(ROOT / "tools" / "vault_migration" / "contracts" / "v1.json")

        self.assertEqual(1, catalog.contract_version)
        self.assertEqual(set(MANAGED_PATHS), set(catalog.managed_files))
        for relative, managed in catalog.managed_files.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertFalse(path.is_symlink(), relative)
            self.assertEqual(managed.sha256, sha256_file(path), relative)

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
            self.assertEqual("preserve", actions["scratch/reports/audit.md"].action)
            self.assertEqual("rebuild-suggested", actions["wiki/maps/topic-map.md"].action)
            self.assertEqual("preserve", actions["init-vault.sh"].action)
            self.assertEqual("preserve", actions["scripts/bootstrap.sh"].action)

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

    def test_audit_blocks_unknown_manifest_contract_without_trusting_its_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            managed_path = target / "tools" / "wiki" / "cli.py"
            managed_path.write_text("unknown contract generated variant\n")
            manifest_path = target / ".vault-builder" / "manifest.json"
            manifest_path.parent.mkdir()
            manifest_path.write_text(
                VaultManifest(
                    1,
                    2,
                    "unknown-contract",
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
            self.assertEqual(2, plan.current_contract_version)
            self.assertTrue(plan.has_blocking_actions)
            self.assertEqual("review-required", actions["tools/wiki/cli.py"].action)
            self.assertEqual("invalid", actions[".vault-builder/manifest.json"].action)
            self.assertIn("unsupported-vault-builder-manifest-contract", {issue["code"] for issue in plan.authority_issues})

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

            self.assertEqual(1, manifest.contract_version)
            self.assertEqual(plan.migration_id, manifest.last_migration_id)
            persisted = load_manifest(target)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(plan.migration_id, persisted.last_migration_id)
            self.assertEqual([None, 1], gate_versions)
            self.assertEqual(protected_before, _protected_hashes(target))
            self.assertFalse((target / ".vault-builder" / "recovery" / plan.migration_id).exists())

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
                        raise subprocess.CalledProcessError(1, ["scripts/release_gate.sh"])
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

    def test_apply_rolls_back_on_either_gate_failure_and_restores_manifest(self):
        for fail_after_manifest in (False, True):
            with self.subTest(fail_after_manifest=fail_after_manifest), tempfile.TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "vault"
                _copy_legacy_fixture(target)
                before = _tree_digest(target)
                plan = audit_vault(ROOT, target)

                def fail_gate(root: Path) -> None:
                    if (load_manifest(root) is not None) == fail_after_manifest:
                        raise subprocess.CalledProcessError(1, ["scripts/release_gate.sh"])

                with self.assertRaises(applying.MigrationApplyError):
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

                self.assertEqual(before, _tree_digest(target))

    def test_transactional_gate_failure_preserves_command_exit_and_rollback_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            def fail_gate(_: Path) -> None:
                raise subprocess.CalledProcessError(7, ["bash", "scripts/release_gate.sh"])

            with self.assertRaises(applying.MigrationApplyError) as raised:
                apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertTrue(failure.rollback_completed)
            self.assertEqual("bash scripts/release_gate.sh", failure.failed_command)
            self.assertEqual(7, failure.exit_code)
            self.assertEqual(
                f"python3 tools/vault_migration/cli.py audit --target {target.resolve()}",
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
                raise subprocess.CalledProcessError(9, ["bash", "scripts/release_gate.sh"])

            with mock.patch.object(applying, "_rollback", side_effect=OSError("rollback exploded")):
                with self.assertRaises(applying.MigrationApplyError) as raised:
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

            failure = raised.exception
            self.assertFalse(failure.rollback_completed)
            self.assertEqual("bash scripts/release_gate.sh", failure.failed_command)
            self.assertEqual(9, failure.exit_code)
            self.assertEqual(
                f"python3 tools/vault_migration/cli.py recover --target {target.resolve()} --migration-id {plan.migration_id}",
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
                raise subprocess.CalledProcessError(13, ["bash", "scripts/release_gate.sh"])

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
                raise subprocess.CalledProcessError(14, ["bash", "scripts/release_gate.sh"])

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
                    self.assertEqual(1, load_manifest(target).contract_version)
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
                raise subprocess.CalledProcessError(12, ["scripts/release_gate.sh"])

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
            self.assertEqual(1, load_manifest(target).contract_version)
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
            self.assertEqual(1, load_manifest(target).contract_version)

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

    def test_output_preparation_cleans_missing_parent_and_temps_when_later_output_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nested_report = root / "report" / "nested" / "audit.md"
            blocked_parent = root / "blocked"
            blocked_parent.write_text("not a directory\n")
            blocked_plan = blocked_parent / "migration-plan.json"

            with self.assertRaisesRegex(ValueError, "unsafe parent"):
                cli._write_outputs(
                    ((nested_report.resolve(), "report\n", "Audit report"), (blocked_plan.resolve(), "plan\n", "Migration plan output"))
                )

            self.assertFalse((root / "report").exists())
            self.assertFalse(list(root.rglob(".*.tmp")))

    def test_output_transaction_restores_existing_outputs_after_second_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = root / "migration-plan.json"
            report = root / "migration-audit.md"
            plan.write_text("old plan\n")
            report.write_text("old report\n")
            plan.chmod(0o600)
            report.chmod(0o640)

            def fail_second_replace(index: int) -> None:
                if index == 1:
                    raise OSError("injected second replace failure")

            with self.assertRaisesRegex(ValueError, "Could not write"):
                cli._write_outputs(
                    ((plan.resolve(), "new plan\n", "Migration plan output"), (report.resolve(), "new report\n", "Audit report")),
                    before_replace=fail_second_replace,
                )

            self.assertEqual("old plan\n", plan.read_text())
            self.assertEqual("old report\n", report.read_text())
            self.assertEqual("0600", f"{plan.stat().st_mode & 0o7777:04o}")
            self.assertEqual("0640", f"{report.stat().st_mode & 0o7777:04o}")
            self.assertFalse(list(root.glob(".*.tmp")))

    def test_output_transaction_restores_replaced_outputs_when_either_commit_parent_fsync_fails(self):
        for failing_call in (1, 2):
            with self.subTest(failing_call=failing_call), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                plan = root / "migration-plan.json"
                report = root / "migration-audit.md"
                plan.write_text("old plan\n")
                report.write_text("old report\n")
                plan.chmod(0o600)
                report.chmod(0o640)
                real_fsync = cli._fsync_parent
                calls = 0

                def fail_selected_parent_fsync(descriptor: int) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failing_call:
                        raise OSError(f"injected parent fsync {failing_call}")
                    real_fsync(descriptor)

                with mock.patch.object(cli, "_fsync_parent", side_effect=fail_selected_parent_fsync):
                    with self.assertRaisesRegex(ValueError, "Could not write"):
                        cli._write_outputs(
                            ((plan.resolve(), "new plan\n", "Migration plan output"), (report.resolve(), "new report\n", "Audit report"))
                        )

                self.assertEqual("old plan\n", plan.read_text())
                self.assertEqual("old report\n", report.read_text())
                self.assertEqual("0600", f"{plan.stat().st_mode & 0o7777:04o}")
                self.assertEqual("0640", f"{report.stat().st_mode & 0o7777:04o}")
                self.assertFalse(list(root.glob(".*.tmp")))

    def test_restore_temp_is_cleaned_when_restore_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = root / "migration-plan.json"
            report = root / "migration-audit.md"
            plan.write_text("old plan\n")
            report.write_text("old report\n")
            real_write = cli._write_temporary
            calls = 0

            def fail_restore_write(descriptor: int, contents: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("injected restore write failure")
                real_write(descriptor, contents, mode)

            with mock.patch.object(cli, "_write_temporary", side_effect=fail_restore_write):
                with self.assertRaisesRegex(ValueError, "commit failed.*rollback failed"):
                    cli._write_outputs(
                        ((plan.resolve(), "new plan\n", "Migration plan output"), (report.resolve(), "new report\n", "Audit report")),
                        before_replace=lambda index: (_ for _ in ()).throw(OSError("injected second replace")) if index == 1 else None,
                    )

            self.assertEqual("new plan\n", plan.read_text())
            self.assertEqual("old report\n", report.read_text())
            self.assertFalse(list(root.glob(".*.tmp")))

    def test_restore_parent_fsync_failure_reports_commit_and_rollback_failures_without_temp_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plan = root / "migration-plan.json"
            report = root / "migration-audit.md"
            plan.write_text("old plan\n")
            report.write_text("old report\n")
            real_fsync = cli._fsync_parent
            calls = 0

            def fail_restore_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected restore parent fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(cli, "_fsync_parent", side_effect=fail_restore_fsync):
                with self.assertRaisesRegex(ValueError, "injected second replace.*rollback failed"):
                    cli._write_outputs(
                        ((plan.resolve(), "new plan\n", "Migration plan output"), (report.resolve(), "new report\n", "Audit report")),
                        before_replace=lambda index: (_ for _ in ()).throw(OSError("injected second replace")) if index == 1 else None,
                    )

            self.assertEqual("old plan\n", plan.read_text())
            self.assertEqual("old report\n", report.read_text())
            self.assertFalse(list(root.glob(".*.tmp")))

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
                        subprocess.CalledProcessError(7, ["bash", "scripts/release_gate.sh"]),
                        target=target,
                        migration_id=plan.migration_id,
                        rollback_completed=True,
                    ),
                    ("Failed command: bash scripts/release_gate.sh", "Exit code: 7", "Automatic rollback: completed", "audit --target"),
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
                        subprocess.CalledProcessError(9, ["bash", "scripts/release_gate.sh"]),
                        target=target,
                        migration_id=plan.migration_id,
                        rollback_completed=False,
                        rollback_error=OSError("rollback exploded"),
                    ),
                    (
                        "Failed command: bash scripts/release_gate.sh",
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
    def test_fresh_vault_has_v1_manifest_and_passes_its_release_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"

            _run(["bash", "init-vault.sh", str(target)], ROOT)

            manifest = load_manifest(target)
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(1, manifest.contract_version)
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
            self.assertEqual(1, manifest.contract_version)
            self.assertEqual(plan.migration_id, manifest.last_migration_id)
            self.assertEqual(manifest, load_manifest(target))
            self.assertFalse((target / ".vault-builder" / "recovery").exists())
            self.assertEqual([], list(target.rglob(".vault-builder-migration.json")))
            _run(["bash", "scripts/release_gate.sh"], target)

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
                raise subprocess.CalledProcessError(1, ["scripts/release_gate.sh"])

            with self.assertRaises(applying.MigrationApplyError):
                apply_plan(ROOT, target, plan, run_gate=fail_gate)

            self.assertEqual(before, _tree_digest(target))
            self.assertFalse((target / ".vault-builder").exists())
