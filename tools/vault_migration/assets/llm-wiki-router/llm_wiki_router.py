#!/usr/bin/env python3
"""User-scope MCP router for llm-wiki vaults.

The router keeps no state between calls. Every call reads the link config
again, finds the session project, and runs the target vault's own CLI.

This file also owns the project lookup rule (root, origin remote, folder
name). The builder's link, unlink, and list commands import this file so the
rule lives in one place.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

VAULT_MAP_VERSIONS = (1, 2)
STAGE_ROOT = "root"
STAGE_REMOTE = "remote"
STAGE_NAME = "name"


class VaultMapError(Exception):
    """The link config is missing or damaged."""


class ProjectMatch(NamedTuple):
    """The project found for a repository.

    root is the root stored in the link config. note is a line asking the
    user to run link again when that root differs from the current root.
    """

    project_id: str
    stage: str
    root: str
    note: str | None


class Stop(NamedTuple):
    """No single project matches. ids lists the matches when there are several."""

    reason: str
    ids: tuple[str, ...]


def read_vault_map(path: Path) -> dict:
    """Read and check the link config. Versions 1 and 2 are accepted."""
    if not path.is_file():
        raise VaultMapError(f"No link config at {path}. Run install first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise VaultMapError(f"Link config {path} is damaged: {exc}".replace("\n", " ")) from exc
    problem = _vault_map_problem(data)
    if problem:
        raise VaultMapError(f"Link config {path} is damaged: {problem}")
    return data


def _vault_map_problem(data: object) -> str | None:
    if not isinstance(data, dict):
        return "it must be a JSON object."
    version = data.get("version")
    if type(version) is not int or version not in VAULT_MAP_VERSIONS:
        return f"version must be one of {', '.join(str(item) for item in VAULT_MAP_VERSIONS)}."
    vaults = data.get("vaults")
    projects = data.get("projects")
    if not isinstance(vaults, dict) or not isinstance(projects, dict):
        return "vaults and projects must be objects."
    for name, entry in vaults.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("role"), str):
            return f"vault {name} needs string path and role."
    for project_id, entry in projects.items():
        names = entry.get("vaults") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("root"), str)
            or not isinstance(names, list)
            or not all(isinstance(name, str) for name in names)
        ):
            return f"project {project_id} needs a string root and a vaults list."
        if "remote" in entry and not isinstance(entry["remote"], str):
            return f"project {project_id} remote must be a string."
    return None


def _git(folder: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return None
    return output


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path.expanduser().absolute()


def main_worktree_root(start: Path) -> Path | None:
    """Return the main worktree root the same way recall does, or None outside git."""
    common_dir = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common_dir is None:
        return None
    common = Path(common_dir)
    if common.name == ".git":
        return _resolve(common.parent)
    top = _git(start, "rev-parse", "--show-toplevel")
    return _resolve(Path(top)) if top else None


def origin_url(root: Path) -> str | None:
    """Return the origin URL as written in the git config, or None."""
    return _git(root, "config", "--get", "remote.origin.url")


def normalize_remote(url: str, base: Path) -> str:
    """Normalize a remote URL so the same repository compares equal.

    Host forms become host/path with a lowercase host. Local paths and
    file:// URLs become absolute path strings. A relative local path is read
    against base.
    """
    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme.lower() == "file":
            return str(_resolve(Path(unquote(parts.path))))
        return _host_path(parts.hostname or "", parts.path)
    colon = text.find(":")
    if colon > 0 and "/" not in text[:colon]:
        host = text[:colon].rpartition("@")[2]
        return _host_path(host, text[colon + 1 :])
    local = Path(text).expanduser()
    if not local.is_absolute():
        local = base / local
    return str(_resolve(local))


def _host_path(host: str, path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")].rstrip("/")
    return f"{host.lower()}/{path}"


def find_project(start: Path, vault_map: dict) -> ProjectMatch | Stop:
    """Find the link config project for the repository that contains start.

    Stages run in order: root, origin remote, folder name. The first stage
    with one match wins. A stage with two or more matches stops with the ids.
    """
    root = main_worktree_root(Path(start))
    if root is None:
        return Stop(f"{start} is not inside a git repository.", ())
    projects = vault_map.get("projects") or {}

    def config_root(entry: dict) -> Path:
        return _resolve(Path(entry["root"]))

    by_root = sorted(project_id for project_id, entry in projects.items() if config_root(entry) == root)
    if by_root:
        return _decide(by_root, STAGE_ROOT, root, projects)

    origin = origin_url(root)
    if origin is not None:
        wanted = normalize_remote(origin, root)
        by_remote = sorted(
            project_id
            for project_id, entry in projects.items()
            if isinstance(entry.get("remote"), str) and normalize_remote(entry["remote"], config_root(entry)) == wanted
        )
        if by_remote:
            return _decide(by_remote, STAGE_REMOTE, root, projects)

    by_name = sorted(
        project_id
        for project_id, entry in projects.items()
        if project_id == root.name and not (origin is not None and isinstance(entry.get("remote"), str))
    )
    if by_name:
        return _decide(by_name, STAGE_NAME, root, projects)
    return Stop(f"Repository {root} is not linked to a project.", ())


def _decide(ids: list[str], stage: str, root: Path, projects: dict) -> ProjectMatch | Stop:
    if len(ids) > 1:
        return Stop(f"Several projects match repository {root} by {stage}: {', '.join(ids)}.", tuple(ids))
    project_id = ids[0]
    stored = projects[project_id]["root"]
    note = None
    if stage != STAGE_ROOT and _resolve(Path(stored)) != root:
        note = (
            f"Link config root for project {project_id} is {stored}, but this repository is at {root}. "
            "Run link again to update it."
        )
    return ProjectMatch(project_id, stage, stored, note)
