"""Read-only scan of Claude Code L1 memory notes for the memory-cleanup skill.

The scan reads the vault map at <config dir>/vault-builder/vault-map.json, finds the
projects linked to this vault, lists each project's L1 memory notes, and reports
whether each note's sha256 already appears as `raw_sha256` on a source page in any
vault linked to that project. It never writes a file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.wiki.lib.frontmatter import parse_page

VAULT_MAP_RELATIVE = Path("vault-builder") / "vault-map.json"
INDEX_NOTE = "MEMORY.md"


class MemoryScanError(ValueError):
    """A one-line reason the scan cannot run."""


@dataclass(frozen=True)
class LinkedVault:
    name: str
    path: Path
    role: str


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


def scan_memory(
    vault_root: Path,
    *,
    project: str | None = None,
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

    return {
        "vault_map": str(map_path),
        "vault": str(this_vault),
        "vault_name": vault_name,
        "projects": report_projects,
    }


def render_memory_scan(scan: dict[str, Any]) -> str:
    lines = [f"Vault: {scan['vault_name']} ({scan['vault']})"]
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
        unknown = [name for name in names if name not in vaults]
        if unknown:
            raise MemoryScanError(
                f"Vault map {map_path}: project {project_id} links unknown vaults: {', '.join(unknown)}."
            )
        projects[project_id] = {"root": entry["root"], "vaults": list(names)}
    return vaults, projects


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


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
