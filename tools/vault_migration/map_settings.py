"""set-default-write and set-role: edit project and vault settings in the link config (contract 5).

The owner uses these commands instead of editing vault-map.json by hand. Each command reads the link config
with the router's format check, changes one field, keeps every other field, and writes the file through a
temporary sibling and a rename. The field rules are in docs/contracts/vault-map-v2.md.

This module does not import hooks.py, and hooks.py does not import this module (spec 4.4).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from tools.vault_migration.linking import _load, _Refused, _write, default_write_from_project_role

DEFAULT_WRITE_FIELD = "default_write_vault"


def set_default_write(
    project: str,
    *,
    vault: str | None = None,
    clear: bool = False,
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Set or clear projects.<project>.default_write_vault. The vault must be in that project's vaults list."""
    command = "set-default-write"
    try:
        if (vault is None) == (not clear):
            raise _Refused("Give exactly one of --vault and --clear.")
        path, data = _load(environ)
        projects = data["projects"]
        if project not in projects:
            names = ", ".join(sorted(projects)) or "none"
            raise _Refused(f"Project {project} is not in the link config. Projects: {names}.")
        entry = projects[project]
        if clear:
            if DEFAULT_WRITE_FIELD in entry:
                del entry[DEFAULT_WRITE_FIELD]
            print(f"{command}: project {project} has no default write vault.")
        else:
            if vault not in entry["vaults"]:
                linked = ", ".join(entry["vaults"]) or "none"
                raise _Refused(
                    f"Vault {vault} is not linked to project {project}. Linked vaults: {linked}. "
                    f"Run link {vault} in that repository first."
                )
            entry[DEFAULT_WRITE_FIELD] = vault
            print(f"{command}: project {project} default write vault: {vault}")
        _write(path, data, False, command)
        return 0
    except _Refused as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 1


def set_role(vault: str, role: str, *, environ: Mapping[str, str] = os.environ) -> int:
    """Set vaults.<vault>.role. Any non-empty string is accepted. personal, project, and team are documented."""
    command = "set-role"
    try:
        value = role.strip()
        if not value:
            raise _Refused("The role must be a non-empty string, for example personal, project, or team.")
        path, data = _load(environ)
        vaults = data["vaults"]
        if vault not in vaults:
            names = ", ".join(sorted(vaults)) or "none"
            raise _Refused(f"Vault {vault} is not in the link config. Vaults: {names}.")
        vaults[vault]["role"] = value
        print(f"{command}: vault {vault} role: {value}")
        for project_id, entry in data["projects"].items():
            if vault in entry["vaults"]:
                default_write_from_project_role(data, project_id, command)
        _write(path, data, False, command)
        return 0
    except _Refused as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return 1
