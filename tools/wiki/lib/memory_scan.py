"""Read-only scan of Claude Code L1 memory notes for the memory-cleanup skill.

The scan reads the vault map at <config dir>/vault-builder/vault-map.json, finds the
projects linked to this vault, lists each project's L1 memory notes, and reports
whether each note's sha256 already appears as `raw_sha256` on a source page in any
vault linked to that project. It never writes a file.

`find_project` repeats the project lookup rule of the router and the recall script
(root, then origin remote, then folder name). This file is installed inside vaults
and cannot import either of them, so the rule lives here on its own.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from tools.wiki.lib.frontmatter import parse_page

VAULT_MAP_RELATIVE = Path("vault-builder") / "vault-map.json"
INDEX_NOTE = "MEMORY.md"
VAULT_MAP_VERSIONS = (1, 2)


class MemoryScanError(ValueError):
    """A one-line reason the scan cannot run."""


@dataclass(frozen=True)
class LinkedVault:
    name: str
    path: Path
    role: str


@dataclass(frozen=True)
class ProjectMatch:
    """A project found for a repository: its id, the lookup step, its link config root,
    and a line asking to run link again when that root differs from the current root."""

    project_id: str
    step: str
    root: str
    note: str | None = None


@dataclass(frozen=True)
class ProjectStop:
    """No single project matches: a one-line reason and the matching ids (empty for none)."""

    reason: str
    ids: tuple[str, ...] = ()


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    home = environ.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def vault_map_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / VAULT_MAP_RELATIVE


def memory_folder_key(project_root: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", project_root)


def raw_import_name(project_id: str, note_name: str, sha256: str) -> str:
    return f"{project_id}--{Path(note_name).stem}--{sha256[:8]}.md"


def main_worktree_root(start: Path) -> Path | None:
    """Return the main worktree root of the repository holding start, or None outside git."""

    def git(*arguments: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(start), "rev-parse", *arguments],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        output = result.stdout.strip()
        return output if result.returncode == 0 and output else None

    common_dir = git("--path-format=absolute", "--git-common-dir")
    if common_dir is None:
        return None
    if Path(common_dir).name == ".git":
        return _resolve(Path(common_dir).parent)
    top = git("--show-toplevel")
    return _resolve(Path(top)) if top else None


def origin_url(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


STAGE_ROOT = "root"
STAGE_REMOTE = "remote"
STAGE_NAME = "name"


def normalize_remote(url: str, base: Path) -> str:
    """Normalize a remote URL so the same repository compares equal.

    Host forms (scheme URLs and scp `[user@]host:path`) become host/path with a
    lowercase host and no trailing `/` or `.git`. Local paths and file:// URLs
    become absolute path strings. A relative local path is read against base.
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


def find_project(start: Path, vault_map: Mapping[str, Any]) -> ProjectMatch | ProjectStop:
    """Find the linked project of the repository holding start (spec section 4.3).

    Stages run in order: root, origin remote, folder name. The first stage with
    one match wins. A stage with two or more matches stops with the ids.
    """
    root = main_worktree_root(Path(start))
    if root is None:
        return ProjectStop(f"{start} is not inside a git repository.")
    projects = vault_map.get("projects") or {}

    def config_root(entry: Mapping[str, Any]) -> Path:
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
    return ProjectStop(f"Repository {root} is not linked to a project.")


def _decide(ids: list[str], stage: str, root: Path, projects: Mapping[str, Any]) -> ProjectMatch | ProjectStop:
    if len(ids) > 1:
        return ProjectStop(f"Several projects match repository {root} by {stage}: {', '.join(ids)}.", tuple(ids))
    project_id = ids[0]
    stored = projects[project_id]["root"]
    note = None
    if stage != STAGE_ROOT and _resolve(Path(stored)) != root:
        note = (
            f"Link config root for project {project_id} is {stored}, but this repository is at {root}. "
            "Run link again to update it."
        )
    return ProjectMatch(project_id, stage, stored, note)


def scan_memory(
    vault_root: Path,
    *,
    project: str | None = None,
    repo: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    map_path = vault_map_path(env)
    if not map_path.is_file():
        raise MemoryScanError(
            f"No vault map at {map_path}. Add this vault and its projects there first (see docs/usage.md)."
        )
    vaults, projects = _load_vault_map(map_path)

    this_vault = _resolve(vault_root)
    vault_name = next((name for name, entry in vaults.items() if _resolve(entry.path) == this_vault), None)
    if vault_name is None:
        raise MemoryScanError(f"This vault ({this_vault}) is not listed under vaults in {map_path}.")

    linked = {
        project_id: entry
        for project_id, entry in projects.items()
        if vault_name in entry["vaults"]
    }
    repo_report: dict[str, Any] | None = None
    if repo is not None:
        found = find_project(repo, {"projects": projects})
        if isinstance(found, ProjectStop):
            raise MemoryScanError(f"{found.reason} (vault map {map_path})")
        repo_report = {
            "root": str(main_worktree_root(repo)),
            "project": found.project_id,
            "step": found.step,
            "linked_root": found.root,
            "hint": found.note,
        }
        project = found.project_id
    if project is not None:
        if project not in linked:
            raise MemoryScanError(f"Project {project} is not linked to vault {vault_name} in {map_path}.")
        linked = {project: linked[project]}

    source_hashes: dict[str, dict[str, list[str]]] = {}
    memory_base = config_dir(env) / "projects"
    report_projects = []
    for project_id in sorted(linked):
        entry = linked[project_id]
        project_vaults = [vaults[name] for name in entry["vaults"]]
        for linked_vault in project_vaults:
            if linked_vault.name not in source_hashes:
                source_hashes[linked_vault.name] = _source_hashes(linked_vault.path)
        memory_dir = memory_base / memory_folder_key(entry["root"]) / "memory"
        notes = []
        for note_path in _note_paths(memory_dir):
            digest = hashlib.sha256(note_path.read_bytes()).hexdigest()
            sources = [
                {"vault": linked_vault.name, "page": page}
                for linked_vault in project_vaults
                for page in source_hashes[linked_vault.name].get(digest, [])
            ]
            notes.append(
                {
                    "name": note_path.name,
                    "path": str(note_path),
                    "sha256": digest,
                    "type": _note_type(note_path),
                    "raw_name": raw_import_name(project_id, note_path.name, digest),
                    "imported": bool(sources),
                    "sources": sources,
                }
            )
        report_projects.append(
            {
                "id": project_id,
                "root": entry["root"],
                "memory_dir": str(memory_dir),
                "memory_dir_exists": memory_dir.is_dir(),
                "vaults": [
                    {"name": item.name, "path": str(item.path), "role": item.role}
                    for item in project_vaults
                ],
                "notes": notes,
            }
        )

    report: dict[str, Any] = {
        "vault_map": str(map_path),
        "vault": str(this_vault),
        "vault_name": vault_name,
        "projects": report_projects,
    }
    if repo_report is not None:
        report["repo"] = repo_report
    return report


def render_memory_scan(scan: dict[str, Any]) -> str:
    lines = [f"Vault: {scan['vault_name']} ({scan['vault']})"]
    repo = scan.get("repo")
    if repo is not None:
        lines.append(f"Repo {repo['root']} matches project {repo['project']} by {repo['step']}.")
        if repo["hint"]:
            lines.append(repo["hint"])
    if not scan["projects"]:
        lines.append("No projects are linked to this vault.")
    for project in scan["projects"]:
        roles = ", ".join(f"{item['name']}={item['role']}" for item in project["vaults"])
        lines.append(f"Project {project['id']} ({project['root']}) vaults: {roles}")
        if not project["memory_dir_exists"]:
            lines.append(f"  memory folder not found: {project['memory_dir']}")
            continue
        lines.append(f"  memory folder: {project['memory_dir']}")
        if not project["notes"]:
            lines.append("  (no notes)")
        for note in project["notes"]:
            state = "imported" if note["imported"] else "new"
            line = f"  {state}  {note['name']}  type={note['type'] or '-'}  raw={note['raw_name']}"
            if note["sources"]:
                line += "  sources=" + ", ".join(f"{item['vault']}:{item['page']}" for item in note["sources"])
            lines.append(line)
    return "\n".join(lines)


def _load_vault_map(map_path: Path) -> tuple[dict[str, LinkedVault], dict[str, dict[str, Any]]]:
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryScanError(f"Cannot read vault map {map_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MemoryScanError(f"Vault map {map_path} must be a JSON object.")
    version = data.get("version")
    if type(version) is not int or version not in VAULT_MAP_VERSIONS:
        raise MemoryScanError(f"Vault map {map_path} has unsupported version {version!r}; expected 1 or 2.")
    raw_vaults = data.get("vaults")
    raw_projects = data.get("projects", {})
    if not isinstance(raw_vaults, dict) or not isinstance(raw_projects, dict):
        raise MemoryScanError(f"Vault map {map_path} needs objects for vaults and projects.")

    vaults: dict[str, LinkedVault] = {}
    for name, entry in raw_vaults.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("role"), str):
            raise MemoryScanError(f"Vault map {map_path}: vault {name} needs string path and role.")
        vaults[name] = LinkedVault(name=name, path=Path(entry["path"]).expanduser(), role=entry["role"])

    projects: dict[str, dict[str, Any]] = {}
    for project_id, entry in raw_projects.items():
        names = entry.get("vaults") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("root"), str)
            or not isinstance(names, list)
            or not all(isinstance(name, str) for name in names)
        ):
            raise MemoryScanError(f"Vault map {map_path}: project {project_id} needs a string root and a vaults list.")
        remote = entry.get("remote")
        if remote is not None and not isinstance(remote, str):
            raise MemoryScanError(f"Vault map {map_path}: project {project_id} remote must be a string.")
        unknown = [name for name in names if name not in vaults]
        if unknown:
            raise MemoryScanError(
                f"Vault map {map_path}: project {project_id} links unknown vaults: {', '.join(unknown)}."
            )
        projects[project_id] = {"root": entry["root"], "vaults": list(names)}
        if remote is not None:
            projects[project_id]["remote"] = remote
    return vaults, projects


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path.expanduser().absolute()


def _note_paths(memory_dir: Path) -> list[Path]:
    if not memory_dir.is_dir():
        return []
    return sorted(
        path for path in memory_dir.glob("*.md") if path.is_file() and path.name != INDEX_NOTE
    )


def _note_type(note_path: Path) -> str | None:
    try:
        frontmatter = parse_page(note_path).frontmatter
    except (OSError, UnicodeDecodeError):
        return None
    if not frontmatter:
        return None
    value = frontmatter.get("type")
    if not isinstance(value, str) or not value:
        metadata = frontmatter.get("metadata")
        value = metadata.get("type") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value else None


def _source_hashes(vault_root: Path) -> dict[str, list[str]]:
    hashes: dict[str, list[str]] = {}
    sources = vault_root / "wiki" / "sources"
    if not sources.is_dir():
        return hashes
    for page_path in sorted(sources.glob("*.md")):
        try:
            frontmatter = parse_page(page_path).frontmatter
        except (OSError, UnicodeDecodeError):
            continue
        value = frontmatter.get("raw_sha256") if frontmatter else None
        if isinstance(value, str) and value:
            hashes.setdefault(value, []).append(page_path.relative_to(vault_root).as_posix())
    return hashes
