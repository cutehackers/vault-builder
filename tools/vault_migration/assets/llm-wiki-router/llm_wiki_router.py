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
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

VAULT_MAP_VERSIONS = (1, 2)
STAGE_ROOT = "root"
STAGE_REMOTE = "remote"
STAGE_NAME = "name"

SERVER_NAME = "llm-wiki"
SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2024-11-05"
CLI_TIMEOUT_SECONDS = 120

ALWAYS_READ_TOOLS = frozenset(
    {"wiki_search", "wiki_read_page", "wiki_validate_page", "wiki_hash_source", "wiki_review_list"}
)
ALWAYS_WRITE_TOOLS = frozenset(
    {
        "wiki_health",
        "wiki_metrics",
        "wiki_ingest_source",
        "wiki_bulk_ingest",
        "wiki_publish_draft",
        "wiki_publish_batch",
        "wiki_review_create",
        "wiki_review_resolve",
        "wiki_workflow_ingest",
        "wiki_workflow_update_preflight",
        "wiki_workflow_update_publish",
        "wiki_workflow_query_prepare",
        "wiki_workflow_query_capture",
        "wiki_workflow_query_publish",
    }
)
PATH_ARGUMENTS = ("path", "paths", "source", "target", "draft", "report")
LINK_HINT = (
    "To reach another vault from this repository, run tools/vault_migration/cli.py link <vault> "
    "--repo <repository> from the vault-builder checkout."
)
STALE_TOOLS_HINT = (
    "The tools in this vault may be out of date. Run audit and apply from the vault-builder checkout."
)
VAULT_DESCRIPTION = (
    "Vault name from the link config. Required for writes, which may target any vault. "
    "Reads accept only vaults this session can reach and default to them."
)
CWD_DESCRIPTION = "Absolute path of the session folder. Used only to find the session repository."


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


def _server_tools() -> list[dict[str, Any]]:
    """The tool definitions of tools/wiki/mcp_server.py, copied unchanged. Built fresh on every call."""
    return [
        {
            "name": "wiki_search",
            "description": (
                "Search durable wiki pages by keywords in title, summary, tags and body. "
                "Returns paths, summaries and a matching line. Deprecated pages and superseded claims are left out "
                "unless include_deprecated is true."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": {"type": "string", "description": "Only pages of this frontmatter type, such as decision."},
                    "limit": {"type": "integer"},
                    "include_deprecated": {"type": "boolean"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "wiki_read_page",
            "description": "Read one durable wiki page, or one section of it, by its path under wiki/.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Page path such as wiki/decisions/example.md."},
                    "section": {"type": "string", "description": "A heading line such as '## Decision'."},
                },
                "required": ["path"],
            },
        },
        {
            "name": "wiki_validate_page",
            "description": "Validate one wiki markdown page.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "wiki_lint",
            "description": "Lint the whole wiki and optionally write a report.",
            "inputSchema": {
                "type": "object",
                "properties": {"report": {"type": "string"}},
            },
        },
        {
            "name": "wiki_health",
            "description": "Run wiki health check and write the default lint report.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "wiki_metrics",
            "description": "Write sustainable maintenance metrics for reviews, sources, provenance, lifecycle, and lint health.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "report": {"type": "string"},
                    "check": {"type": "boolean"},
                    "policy": {"type": "string"},
                },
            },
        },
        {
            "name": "wiki_hash_source",
            "description": "Compute SHA-256 for a raw source file.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "wiki_ingest_source",
            "description": "Register a raw source as a source page with hash, index, log, and report.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "wiki_source_registry",
            "description": "Build a source registry inventory across raw files and source pages.",
            "inputSchema": {
                "type": "object",
                "properties": {"report": {"type": "string"}},
            },
        },
        {
            "name": "wiki_bulk_ingest",
            "description": "Register multiple raw sources and write a bulk ingest report.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "all_raw": {"type": "boolean"},
                    "report": {"type": "string"},
                },
            },
        },
        {
            "name": "wiki_apply_draft",
            "description": "Validate a JSON draft without durable writes. Use wiki_publish_draft to publish.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "wiki_publish_draft",
            "description": "Validate, apply, and lint a JSON draft through the publish-draft facade.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "wiki_publish_batch",
            "description": "Transactionally validate, apply, lint, and report a multi-page batch draft.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "report": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "wiki_review_create",
            "description": "Create a pending human-review queue item under scratch/review.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "summary": {"type": "string"},
                    "context": {"type": "string"},
                    "related": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "summary"],
            },
        },
        {
            "name": "wiki_review_list",
            "description": "List human-review queue items under scratch/review.",
            "inputSchema": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
        },
        {
            "name": "wiki_review_resolve",
            "description": "Resolve a human-review queue item and append the resolution to wiki/log.md.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "status": {"type": "string"},
                    "resolution": {"type": "string"},
                },
                "required": ["path", "status", "resolution"],
            },
        },
        {
            "name": "wiki_merge_scan",
            "description": "Scan for duplicate page candidates and optionally create a merge review item.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "report": {"type": "string"},
                    "create_review": {"type": "boolean"},
                },
            },
        },
        {
            "name": "wiki_maps_build",
            "description": "Build or dry-run generated navigation maps for topics, sources, decisions, reviews, and lifecycle signals.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "report": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "check": {"type": "boolean"},
                },
            },
        },
        {
            "name": "wiki_workflow_ingest",
            "description": "Run deterministic workflow ingest checkpoint for a raw source. Semantic extraction remains agent work.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["source"],
            },
        },
        {
            "name": "wiki_workflow_update_preflight",
            "description": "Prepare deterministic update preflight report for a target wiki page.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["target"],
            },
        },
        {
            "name": "wiki_workflow_update_publish",
            "description": "Publish an update draft through workflow target validation and publish-draft.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "draft": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["target", "draft"],
            },
        },
        {
            "name": "wiki_workflow_query_prepare",
            "description": "Prepare deterministic query report scaffold. Semantic answer synthesis remains agent work.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["question"],
            },
        },
        {
            "name": "wiki_workflow_query_capture",
            "description": "Create deterministic query report and optional reusable answer capture draft scaffold.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "mode": {"type": "string"},
                    "answer_summary": {"type": "string"},
                    "consulted_pages": {"type": "array", "items": {"type": "string"}},
                    "consulted_sources": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string"},
                    "contradictions": {"type": "array", "items": {"type": "string"}},
                    "capture_recommendation": {"type": "string"},
                    "capture_target": {"type": "string"},
                    "capture_title": {"type": "string"},
                    "draft": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["question", "mode"],
            },
        },
        {
            "name": "wiki_workflow_query_publish",
            "description": "Publish a query capture draft through publish-draft.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft": {"type": "string"},
                    "report": {"type": "string"},
                },
                "required": ["draft"],
            },
        },
    ]


def tool_list() -> list[dict[str, Any]]:
    """Return the fixed tool list. Every tool takes vault and cwd. Always-write tools require vault."""
    tools = _server_tools()
    for tool in tools:
        schema = tool["inputSchema"]
        schema["properties"]["vault"] = {"type": "string", "description": VAULT_DESCRIPTION}
        schema["properties"]["cwd"] = {"type": "string", "description": CWD_DESCRIPTION}
        if tool["name"] in ALWAYS_WRITE_TOOLS:
            schema["required"] = [*schema.get("required", []), "vault"]
    return tools


def is_write_call(name: str, arguments: dict[str, Any]) -> bool:
    """Classify one call. A combination the read table does not list counts as a write."""
    if name in ALWAYS_READ_TOOLS:
        return False
    if name in ("wiki_lint", "wiki_source_registry", "wiki_apply_draft"):
        return bool(arguments.get("report"))
    if name == "wiki_merge_scan":
        return bool(arguments.get("report") or arguments.get("create_review"))
    if name == "wiki_maps_build":
        # maps build --dry-run --report still writes the report file.
        return not (arguments.get("dry_run") or arguments.get("check")) or bool(arguments.get("report"))
    return True


def cli_arguments(name: str, args: dict[str, Any]) -> list[str]:
    """Turn a tool call into vault CLI arguments, the same way tools/wiki/mcp_server.py does."""
    if name == "wiki_search":
        command = ["search", _required(args, "query")]
        if args.get("type"):
            command.extend(["--type", str(args["type"])])
        if args.get("limit") is not None:
            command.extend(["--limit", str(int(args["limit"]))])
        if args.get("include_deprecated"):
            command.append("--include-deprecated")
        return command
    if name == "wiki_read_page":
        return ["read", _required(args, "path"), *_option(args, "section", "--section")]
    if name == "wiki_validate_page":
        return ["validate-page", _required(args, "path")]
    if name == "wiki_lint":
        return ["lint", *_option(args, "report", "--report")]
    if name == "wiki_health":
        return ["health"]
    if name == "wiki_metrics":
        return [
            "metrics",
            *_option(args, "report", "--report"),
            *_flag(args, "check", "--check"),
            *_option(args, "policy", "--policy"),
        ]
    if name == "wiki_hash_source":
        return ["hash-source", _required(args, "path")]
    if name == "wiki_ingest_source":
        return [
            "ingest-source",
            _required(args, "path"),
            *_option(args, "title", "--title"),
            *_option(args, "summary", "--summary"),
            *_option(args, "report", "--report"),
        ]
    if name == "wiki_source_registry":
        return ["source-registry", *_option(args, "report", "--report")]
    if name == "wiki_bulk_ingest":
        paths = args.get("paths") or []
        if not isinstance(paths, list):
            raise ValueError("paths must be a list when provided.")
        return [
            "bulk-ingest",
            *(str(path) for path in paths),
            *_flag(args, "all_raw", "--all-raw"),
            *_option(args, "report", "--report"),
        ]
    if name == "wiki_apply_draft":
        return ["apply-draft", _required(args, "path"), "--dry-run", *_option(args, "report", "--report")]
    if name == "wiki_publish_draft":
        return ["publish-draft", _required(args, "path"), *_option(args, "report", "--report")]
    if name == "wiki_publish_batch":
        return [
            "publish-batch",
            _required(args, "path"),
            *_option(args, "report", "--report"),
            *_flag(args, "dry_run", "--dry-run"),
        ]
    if name == "wiki_review_create":
        command = ["review", "create", "--type", _required(args, "type"), "--summary", _required(args, "summary")]
        command.extend(_option(args, "context", "--context"))
        for related in _string_list(args, "related"):
            command.extend(["--related", related])
        for evidence in _string_list(args, "evidence"):
            command.extend(["--evidence", evidence])
        return command
    if name == "wiki_review_list":
        return ["review", "list", *_option(args, "status", "--status")]
    if name == "wiki_review_resolve":
        return [
            "review",
            "resolve",
            _required(args, "path"),
            "--status",
            _required(args, "status"),
            "--resolution",
            _required(args, "resolution"),
        ]
    if name == "wiki_merge_scan":
        return ["merge", "scan", *_option(args, "report", "--report"), *_flag(args, "create_review", "--create-review")]
    if name == "wiki_maps_build":
        return [
            "maps",
            "build",
            *_option(args, "report", "--report"),
            *_flag(args, "dry_run", "--dry-run"),
            *_flag(args, "check", "--check"),
        ]
    if name == "wiki_workflow_ingest":
        return [
            "workflow",
            "ingest",
            "--source",
            _required(args, "source"),
            *_option(args, "title", "--title"),
            *_option(args, "summary", "--summary"),
            *_option(args, "report", "--report"),
        ]
    if name == "wiki_workflow_update_preflight":
        return [
            "workflow",
            "update",
            "--target",
            _required(args, "target"),
            "--preflight",
            *_option(args, "report", "--report"),
        ]
    if name == "wiki_workflow_update_publish":
        return [
            "workflow",
            "update",
            "--target",
            _required(args, "target"),
            "--draft",
            _required(args, "draft"),
            "--publish",
            *_option(args, "report", "--report"),
        ]
    if name == "wiki_workflow_query_prepare":
        return [
            "workflow",
            "query",
            "--question",
            _required(args, "question"),
            "--prepare-report",
            *_option(args, "report", "--report"),
        ]
    if name == "wiki_workflow_query_capture":
        command = ["workflow", "query", "--question", _required(args, "question"), "--mode", _required(args, "mode")]
        command.extend(_option(args, "answer_summary", "--answer-summary"))
        for page in _string_list(args, "consulted_pages"):
            command.extend(["--consulted-page", page])
        for source in _string_list(args, "consulted_sources"):
            command.extend(["--consulted-source", source])
        command.extend(_option(args, "confidence", "--confidence"))
        for contradiction in _string_list(args, "contradictions"):
            command.extend(["--contradiction", contradiction])
        command.extend(_option(args, "capture_recommendation", "--capture-recommendation"))
        command.extend(_option(args, "capture_target", "--capture-target"))
        command.extend(_option(args, "capture_title", "--capture-title"))
        command.extend(_option(args, "draft", "--draft"))
        command.extend(_option(args, "report", "--report"))
        return command
    if name == "wiki_workflow_query_publish":
        return ["workflow", "query", "--draft", _required(args, "draft"), "--publish", *_option(args, "report", "--report")]
    raise ValueError(f"Unknown tool: {name}")


def _required(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not value:
        raise ValueError(f"Missing required argument: {name}")
    return str(value)


def _option(args: dict[str, Any], name: str, flag: str) -> list[str]:
    return [flag, str(args[name])] if args.get(name) else []


def _flag(args: dict[str, Any], name: str, flag: str) -> list[str]:
    return [flag] if args.get(name) else []


def _string_list(args: dict[str, Any], name: str) -> list[str]:
    values = args.get(name) or []
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list when provided.")
    return [str(value) for value in values]


class Refusal(Exception):
    """The call is refused before any vault CLI starts."""

    def __init__(self, message: str, vault: str = "-") -> None:
        super().__init__(message)
        self.vault = vault


class Reach(NamedTuple):
    """The vaults a session can read. note asks to run link again; reason says why nothing is reachable."""

    names: tuple[str, ...]
    note: str | None
    reason: str | None


class Outcome(NamedTuple):
    """One finished tool call: the MCP result and the vault and exit labels for the log line."""

    result: dict[str, Any]
    vault: str
    exit: str


def vault_map_path() -> Path:
    """Return <config folder>/vault-builder/vault-map.json, read from the environment on every call."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    config = Path(configured) if configured else Path.home() / ".claude"
    return config / "vault-builder" / "vault-map.json"


def reachable_vaults(start: Path, vault_map: dict) -> Reach:
    """The session vault when start is in one, otherwise the vaults linked to the session repository."""
    vaults = vault_map["vaults"]
    root = main_worktree_root(start)
    if root is not None:
        for name in sorted(vaults):
            if _resolve(Path(vaults[name]["path"])) == root:
                return Reach((name,), None, None)
    found = find_project(start, vault_map)
    if isinstance(found, Stop):
        return Reach((), None, found.reason)
    names = tuple(dict.fromkeys(name for name in vault_map["projects"][found.project_id]["vaults"] if name in vaults))
    reason = None if names else f"Project {found.project_id} has no vault from the link config."
    return Reach(names, found.note, reason)


def check_path_arguments(arguments: dict[str, Any], root: Path, vault: str) -> None:
    """Refuse path arguments that are absolute, keep .. after normalizing, or leave the vault through a symlink."""
    for key in PATH_ARGUMENTS:
        value = arguments.get(key)
        if not value:
            continue
        for item in value if isinstance(value, list) else [value]:
            text = str(item)
            parts = Path(os.path.normpath(text)).parts
            if os.path.isabs(text):
                problem = "is an absolute path"
            elif parts and parts[0] == "..":
                problem = "leaves the vault through .."
            elif not _inside(_resolve(root / text), root):
                problem = "leaves the vault through a symlink"
            else:
                continue
            raise Refusal(f"Argument {key} {problem}. Paths must be relative to vault {vault}.", vault)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def start_vault_cli(command: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    """Start one vault CLI process. Tests count calls to this function."""
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_in_vault(name: str, entry: dict, argv: list[str]) -> tuple[str, str]:
    """Run the vault's own CLI. Returns the exit label and the output text."""
    root = _resolve(Path(entry["path"]))
    cli = root / "tools" / "wiki" / "cli.py"
    if not root.is_dir():
        raise Refusal(f"Vault {name} folder {entry['path']} does not exist.", name)
    if not cli.is_file():
        raise Refusal(f"Vault {name} has no tools/wiki/cli.py. {STALE_TOOLS_HINT}", name)
    try:
        completed = start_vault_cli([sys.executable, "-I", "-B", str(cli), *argv], root, CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "timeout", f"Vault {name} CLI timed out after {CLI_TIMEOUT_SECONDS} seconds and was stopped."
    output = (completed.stdout + completed.stderr).strip() or f"exit_code={completed.returncode}"
    if completed.returncode != 0 and "invalid choice" in output:
        output += "\n" + STALE_TOOLS_HINT
    return str(completed.returncode), output


def _known_tool(name: str) -> bool:
    return any(tool["name"] == name for tool in _server_tools())


def _names(names: Any) -> str:
    return ", ".join(sorted(names)) or "none"


def _text_result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _session_start(arguments: dict[str, Any]) -> Path:
    cwd = arguments.get("cwd")
    if cwd is None:
        return Path.cwd()
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise Refusal("Argument cwd must be an absolute path.")
    return Path(cwd)


def _route(name: str, arguments: Any) -> Outcome:
    """Handle one tools/call from scratch: read the link config, pick vaults, check the call, run the CLI."""
    if not isinstance(arguments, dict):
        raise Refusal("Tool arguments must be a JSON object.")
    if not _known_tool(name):
        raise Refusal("Unknown tool.")
    try:
        argv = cli_arguments(name, arguments)
    except (TypeError, ValueError) as exc:
        raise Refusal(str(exc)) from exc
    vault_arg = arguments.get("vault")
    if vault_arg is not None and not isinstance(vault_arg, str):
        raise Refusal("Argument vault must be a string.")
    try:
        vault_map = read_vault_map(vault_map_path())
    except VaultMapError as exc:
        raise Refusal(f"{exc}\nRun vault-builder install to create the link config.") from exc
    vaults = vault_map["vaults"]
    if vault_arg and vault_arg not in vaults:
        raise Refusal(f"Vault {vault_arg} is not in the link config. Vaults: {_names(vaults)}.")
    note = None
    if is_write_call(name, arguments):
        _session_start(arguments)
        if not vault_arg:
            raise Refusal(f"Writes need the vault argument. Vaults: {_names(vaults)}.")
        targets = [vault_arg]
    else:
        reach = reachable_vaults(_session_start(arguments), vault_map)
        note = reach.note
        reachable = f"Reachable vaults: {_names(reach.names)}."
        why = f"{reach.reason}\n" if reach.reason else ""
        if vault_arg:
            if vault_arg not in reach.names:
                raise Refusal(f"Vault {vault_arg} is not reachable from this session.\n{why}{reachable}\n{LINK_HINT}", vault_arg)
            targets = [vault_arg]
        elif name == "wiki_search":
            if not reach.names:
                raise Refusal(f"{why}No vault is reachable from this session. Vaults: {_names(vaults)}.\n{LINK_HINT}")
            targets = sorted(reach.names)
        elif len(reach.names) != 1:
            raise Refusal(
                f"{why}Pass the vault argument. Choose one of: {_names(reach.names if reach.names else vaults)}.\n"
                f"{reachable}\n{LINK_HINT}"
            )
        else:
            targets = list(reach.names)
    if name == "wiki_search":
        return _search(targets, vaults, argv, note)
    target = targets[0]
    check_path_arguments(arguments, _resolve(Path(vaults[target]["path"])), target)
    exit_label, output = _run_in_vault(target, vaults[target], argv)
    if note:
        output += "\n" + note
    return Outcome(_text_result(output, exit_label != "0"), target, exit_label)


def _search(targets: list[str], vaults: dict, argv: list[str], note: str | None) -> Outcome:
    sections = []
    labels = []
    for name in targets:
        header = f"# vault {name} ({vaults[name]['role']})"
        try:
            label, output = _run_in_vault(name, vaults[name], argv)
        except Refusal as exc:
            label, output = "refused", str(exc)
        if label != "0":
            first = next((line for line in output.splitlines() if line.strip()), output)
            output = f"Error: {first.strip()}"
        labels.append(label)
        sections.append(f"{header}\n{output}")
    text = "\n\n".join(sections)
    if note:
        text += "\n\n" + note
    all_failed = all(label != "0" for label in labels)
    return Outcome(_text_result(text, all_failed), ",".join(targets), ",".join(labels))


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    """Run one tool call and write one log line to stderr. Argument values never reach the log."""
    started = time.monotonic()
    known = _known_tool(name)
    try:
        outcome = _route(name, arguments)
    except Refusal as exc:
        vault = exc.vault
        outcome = Outcome(_text_result(str(exc), True), vault, "refused")
    milliseconds = int((time.monotonic() - started) * 1000)
    print(
        f"llm-wiki tool={name if known else 'unknown'} vault={outcome.vault} exit={outcome.exit} ms={milliseconds}",
        file=sys.stderr,
        flush=True,
    )
    return outcome.result


def _server_info() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC request. initialize, tools/list, and server/discover are fixed answers."""
    method = request.get("method")
    request_id = request.get("id")
    try:
        if method == "initialize":
            return _result(request_id, _server_info())
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _result(request_id, {"tools": tool_list()})
        if method == "server/discover":
            return _result(request_id, {**_server_info(), "tools": tool_list()})
        if method == "tools/call":
            params = request.get("params") or {}
            return _result(request_id, call_tool(str(params.get("name")), params.get("arguments") or {}))
        return _error(request_id, -32601, f"Unknown method: {method}")
    except Exception as exc:  # noqa: BLE001 - one bad request must not stop the server.
        return _error(request_id, -32000, str(exc))


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"Parse error: {exc}")
        else:
            if isinstance(request, dict):
                response = handle_request(request)
            else:
                response = _error(None, -32600, "Invalid Request: request must be a JSON object.")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
