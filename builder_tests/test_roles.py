"""The known role table in roles.py (spec 4.6, decisions 7 and 8, A-12, A-13).

The list test runs with a temporary HOME, so the real link config is never read.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.vault_migration import linking, roles


class RoleTableTests(unittest.TestCase):
    def test_first_entries_are_personal_team_and_project(self) -> None:
        self.assertEqual(("personal", "team", "project"), roles.role_names())

    def test_every_entry_has_name_description_and_per_repo(self) -> None:
        for role in roles.ROLES:
            self.assertIsInstance(role.name, str)
            self.assertTrue(role.name)
            self.assertIsInstance(role.description, str)
            self.assertTrue(role.description)
            self.assertNotIn("\n", role.description)
            self.assertIsInstance(role.per_repo, bool)

    def test_only_project_is_per_repo(self) -> None:
        self.assertEqual(["project"], [role.name for role in roles.ROLES if role.per_repo])

    def test_known_role_returns_entry_or_none(self) -> None:
        project = roles.known_role("project")
        assert project is not None
        self.assertTrue(project.per_repo)
        self.assertIsNone(roles.known_role("client"))

    def test_is_shared_counts_unknown_roles_as_shared(self) -> None:
        self.assertTrue(roles.is_shared("personal"))
        self.assertTrue(roles.is_shared("team"))
        self.assertFalse(roles.is_shared("project"))
        self.assertTrue(roles.is_shared("client"))

    def test_project_role_points_at_table_entry(self) -> None:
        self.assertEqual(roles.PROJECT_ROLE, linking._PROJECT_ROLE)
        entry = roles.known_role(linking._PROJECT_ROLE)
        assert entry is not None
        self.assertTrue(entry.per_repo)

    def test_vault_side_session_capture_does_not_import_roles(self) -> None:
        source = Path(__file__).resolve().parents[1] / "tools" / "wiki" / "lib" / "session_capture.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("vault_migration", text)
        self.assertIn(f'PROJECT_ROLE = "{roles.PROJECT_ROLE}"', text)


class ListOutputTests(unittest.TestCase):
    def test_list_shows_role_description_and_marks_unknown_role_shared(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw).resolve()
            home = tmp / "home"
            vault_map = home / ".claude" / "vault-builder" / "vault-map.json"
            vault_map.parent.mkdir(parents=True)
            data = {
                "version": 2,
                "vaults": {
                    "p": {"path": str(tmp / "p"), "role": "project"},
                    "c": {"path": str(tmp / "c"), "role": "client"},
                },
                "projects": {},
            }
            vault_map.write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = linking.list_links(repo=str(tmp), environ={"HOME": str(home)})
        self.assertEqual(0, code)
        lines = out.getvalue().splitlines()
        project_line = next(line for line in lines if line.startswith("  p "))
        client_line = next(line for line in lines if line.startswith("  c "))
        project = roles.known_role("project")
        assert project is not None
        self.assertIn(project.description, project_line)
        self.assertIn("per repository", project_line)
        self.assertIn("not a known role", client_line)
        self.assertIn("shared", client_line)


if __name__ == "__main__":
    unittest.main()
