from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.vault_migration import cli
from tools.vault_migration.catalogs import build_catalog, load_catalog, sha256_file, write_catalog
from tools.vault_migration.manifests import create_manifest, load_manifest, write_manifest
from tools.vault_migration.managed_paths import MANAGED_PATHS
from tools.vault_migration.models import ContractCatalog, ManagedFile, MigrationAction, MigrationPlan, VaultManifest


ROOT = Path(__file__).resolve().parents[1]


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
