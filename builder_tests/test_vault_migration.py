from __future__ import annotations

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


ROOT = Path(__file__).resolve().parents[1]


def _copy_legacy_fixture(target: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "47cbf83"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        if sys.version_info >= (3, 12):
            contents.extractall(target, filter="data")
        else:
            contents.extractall(target)
    (target / "raw" / "sources" / "input.md").write_text("Legacy input.\n")
    (target / "wiki" / "concepts" / "topic.md").write_text(
        "---\ntitle: Topic\ntype: concept\nstatus: active\ncreated: 2026-07-30\n"
        "updated: 2026-07-30\nowner: human\nsummary: Topic knowledge.\nsource_count: 0\n"
        "tags: []\nrelated: []\nconfidence: high\nquality:\n  provenance: none\n"
        "  links: valid\n  contradictions: none\n  review_required: false\n---\n\n# Topic\n\nOwned knowledge.\n"
    )
    (target / "scratch" / "reports" / "audit.md").write_text("Prior audit evidence.\n")


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

                if fail_gate:
                    with self.assertRaises(subprocess.CalledProcessError):
                        apply_plan(ROOT, target, plan, run_gate=gate)
                    self.assertEqual(before, _tree_digest(target))
                    self.assertFalse((target / ".agents").exists())
                else:
                    manifest = apply_plan(ROOT, target, plan, run_gate=gate)
                    self.assertTrue((target / ".agents" / "skills" / "wiki-ingest" / "SKILL.md").is_file())
                    self.assertEqual(manifest.last_migration_id, load_manifest(target).last_migration_id)

    def test_apply_rolls_back_parents_when_create_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(target)
            shutil.rmtree(target / ".agents")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)
            original_write = applying._atomic_write

            def fail_created_file(destination: Path, contents: bytes, mode: str) -> None:
                if destination.as_posix().endswith("/.agents/skills/wiki-ingest/SKILL.md"):
                    raise OSError("injected create failure")
                original_write(destination, contents, mode)

            with mock.patch("tools.vault_migration.applying._atomic_write", side_effect=fail_created_file):
                with self.assertRaisesRegex(OSError, "injected create failure"):
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

                with self.assertRaises(subprocess.CalledProcessError):
                    apply_plan(ROOT, target, plan, run_gate=fail_gate)

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

            with mock.patch("tools.vault_migration.applying.write_manifest", side_effect=ValueError("manifest write failed")):
                with self.assertRaisesRegex(ValueError, "manifest write failed"):
                    apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))

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
            (target / relative).write_text("interrupted replacement\n")
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
                        }],
                        "manifest": {"existed": False, "sha256": None, "mode": None},
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
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            target_path = target / relative
            target_path.parent.mkdir(parents=True)
            target_path.write_bytes(desired)
            (journal_root / "originals").mkdir(parents=True)
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
                        }],
                        "manifest": None,
                        "created_directories": [
                            ".agents",
                            ".agents/skills",
                            ".agents/skills/wiki-ingest",
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
                        "manifest": {"existed": False, "sha256": None, "mode": None},
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
                "manifest": {"existed": False, "sha256": None, "mode": None},
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
                '"entries":[],"manifest":null}'
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
                        }],
                        "manifest": None,
                        "created_directories": ["scratch/owned-empty"],
                    }
                )
            )
            before = _tree_digest(target)

            with self.assertRaisesRegex(ValueError, "created directories"):
                recover_migration(target, migration_id)
            self.assertEqual(before, _tree_digest(target))
            self.assertTrue(user_directory.is_dir())
