"""The known vault roles (spec 4.6).

A role stays a free, non-empty string (decision 8). This table is used only for
prompts, --role help, picking shared vaults for link without a name, and the
list and doctor output. A role not in the table is a shared role (decision 7).

To add a role, add one line to ROLES and one line to the table in usage.md 17.2.
The vault-side copy tools/wiki/lib/session_capture.py keeps its own value and
does not import this file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    per_repo: bool  # True: the vault belongs to one repository and is linked only by name


PROJECT_ROLE = "project"

ROLES: tuple[Role, ...] = (
    Role("personal", "one person's notes, shared by every repository", per_repo=False),
    Role("team", "a team's shared knowledge, shared by every repository", per_repo=False),
    Role(PROJECT_ROLE, "one repository's history and session transcripts", per_repo=True),
)

_BY_NAME = {role.name: role for role in ROLES}


def role_names() -> tuple[str, ...]:
    return tuple(role.name for role in ROLES)


def known_role(name: str) -> Role | None:
    return _BY_NAME.get(name)


def is_shared(name: str) -> bool:
    """A vault is shared unless its role is in the table with per_repo. Unknown roles are shared."""
    role = known_role(name)
    return role is None or not role.per_repo
