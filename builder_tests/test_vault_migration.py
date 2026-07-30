from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.vault_migration.catalogs import load_catalog, sha256_file
from tools.vault_migration.managed_paths import MANAGED_PATHS
from tools.vault_migration.models import ContractCatalog, ManagedFile, MigrationAction, MigrationPlan


ROOT = Path(__file__).resolve().parents[1]


class ContractCatalogTest(unittest.TestCase):
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
