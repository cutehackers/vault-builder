#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.wiki.cli import main as cli_main


TOOLS = [
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


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps(_error(None, -32700, f"Parse error: {exc}")), flush=True)
            continue
        if not isinstance(request, dict):
            print(json.dumps(_error(None, -32600, "Invalid Request: request must be a JSON object.")), flush=True)
            continue
        response = _handle_request(request)
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


def _handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    try:
        if method == "initialize":
            return _result(request_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "llm-wiki", "version": "1.0.0"}})
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params") or {}
            return _result(request_id, _call_tool(str(params.get("name")), params.get("arguments") or {}))
        return _error(request_id, -32601, f"Unknown method: {method}")
    except Exception as exc:
        return _error(request_id, -32000, str(exc))


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    dispatch: dict[str, Callable[[dict[str, Any]], list[str]]] = {
        "wiki_validate_page": lambda args: ["validate-page", _required(args, "path")],
        "wiki_lint": _lint_args,
        "wiki_health": lambda args: ["health"],
        "wiki_metrics": _metrics_args,
        "wiki_hash_source": lambda args: ["hash-source", _required(args, "path")],
        "wiki_ingest_source": _ingest_source_args,
        "wiki_source_registry": _source_registry_args,
        "wiki_bulk_ingest": _bulk_ingest_args,
        "wiki_apply_draft": _apply_draft_args,
        "wiki_publish_draft": _publish_draft_args,
        "wiki_publish_batch": _publish_batch_args,
        "wiki_review_create": _review_create_args,
        "wiki_review_list": _review_list_args,
        "wiki_review_resolve": _review_resolve_args,
        "wiki_merge_scan": _merge_scan_args,
        "wiki_maps_build": _maps_build_args,
        "wiki_workflow_ingest": _workflow_ingest_args,
        "wiki_workflow_update_preflight": _workflow_update_preflight_args,
        "wiki_workflow_update_publish": _workflow_update_publish_args,
        "wiki_workflow_query_prepare": _workflow_query_prepare_args,
        "wiki_workflow_query_capture": _workflow_query_capture_args,
        "wiki_workflow_query_publish": _workflow_query_publish_args,
    }
    if name not in dispatch:
        raise ValueError(f"Unknown tool: {name}")
    exit_code, output = _run_cli(dispatch[name](arguments))
    return {
        "content": [{"type": "text", "text": output.strip() or f"exit_code={exit_code}"}],
        "isError": exit_code != 0,
    }


def _lint_args(args: dict[str, Any]) -> list[str]:
    command = ["lint"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _metrics_args(args: dict[str, Any]) -> list[str]:
    command = ["metrics"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    if args.get("check"):
        command.append("--check")
    if args.get("policy"):
        command.extend(["--policy", str(args["policy"])])
    return command


def _ingest_source_args(args: dict[str, Any]) -> list[str]:
    command = ["ingest-source", _required(args, "path")]
    if args.get("title"):
        command.extend(["--title", str(args["title"])])
    if args.get("summary"):
        command.extend(["--summary", str(args["summary"])])
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _source_registry_args(args: dict[str, Any]) -> list[str]:
    command = ["source-registry"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _bulk_ingest_args(args: dict[str, Any]) -> list[str]:
    command = ["bulk-ingest"]
    paths = args.get("paths") or []
    if not isinstance(paths, list):
        raise ValueError("paths must be a list when provided.")
    command.extend(str(path) for path in paths)
    if args.get("all_raw"):
        command.append("--all-raw")
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _apply_draft_args(args: dict[str, Any]) -> list[str]:
    command = ["apply-draft", _required(args, "path"), "--dry-run"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _publish_draft_args(args: dict[str, Any]) -> list[str]:
    command = ["publish-draft", _required(args, "path")]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _publish_batch_args(args: dict[str, Any]) -> list[str]:
    command = ["publish-batch", _required(args, "path")]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    if args.get("dry_run"):
        command.append("--dry-run")
    return command


def _review_create_args(args: dict[str, Any]) -> list[str]:
    command = ["review", "create", "--type", _required(args, "type"), "--summary", _required(args, "summary")]
    if args.get("context"):
        command.extend(["--context", str(args["context"])])
    for related in _optional_string_list(args, "related"):
        command.extend(["--related", related])
    for evidence in _optional_string_list(args, "evidence"):
        command.extend(["--evidence", evidence])
    return command


def _review_list_args(args: dict[str, Any]) -> list[str]:
    command = ["review", "list"]
    if args.get("status"):
        command.extend(["--status", str(args["status"])])
    return command


def _review_resolve_args(args: dict[str, Any]) -> list[str]:
    return [
        "review",
        "resolve",
        _required(args, "path"),
        "--status",
        _required(args, "status"),
        "--resolution",
        _required(args, "resolution"),
    ]


def _merge_scan_args(args: dict[str, Any]) -> list[str]:
    command = ["merge", "scan"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    if args.get("create_review"):
        command.append("--create-review")
    return command


def _maps_build_args(args: dict[str, Any]) -> list[str]:
    command = ["maps", "build"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    if args.get("dry_run"):
        command.append("--dry-run")
    if args.get("check"):
        command.append("--check")
    return command


def _workflow_ingest_args(args: dict[str, Any]) -> list[str]:
    command = ["workflow", "ingest", "--source", _required(args, "source")]
    if args.get("title"):
        command.extend(["--title", str(args["title"])])
    if args.get("summary"):
        command.extend(["--summary", str(args["summary"])])
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _workflow_update_preflight_args(args: dict[str, Any]) -> list[str]:
    command = ["workflow", "update", "--target", _required(args, "target"), "--preflight"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _workflow_update_publish_args(args: dict[str, Any]) -> list[str]:
    command = [
        "workflow",
        "update",
        "--target",
        _required(args, "target"),
        "--draft",
        _required(args, "draft"),
        "--publish",
    ]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _workflow_query_prepare_args(args: dict[str, Any]) -> list[str]:
    command = ["workflow", "query", "--question", _required(args, "question"), "--prepare-report"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _workflow_query_capture_args(args: dict[str, Any]) -> list[str]:
    command = [
        "workflow",
        "query",
        "--question",
        _required(args, "question"),
        "--mode",
        _required(args, "mode"),
    ]
    if args.get("answer_summary"):
        command.extend(["--answer-summary", str(args["answer_summary"])])
    for page in _optional_string_list(args, "consulted_pages"):
        command.extend(["--consulted-page", page])
    for source in _optional_string_list(args, "consulted_sources"):
        command.extend(["--consulted-source", source])
    if args.get("confidence"):
        command.extend(["--confidence", str(args["confidence"])])
    for contradiction in _optional_string_list(args, "contradictions"):
        command.extend(["--contradiction", contradiction])
    if args.get("capture_recommendation"):
        command.extend(["--capture-recommendation", str(args["capture_recommendation"])])
    if args.get("capture_target"):
        command.extend(["--capture-target", str(args["capture_target"])])
    if args.get("capture_title"):
        command.extend(["--capture-title", str(args["capture_title"])])
    if args.get("draft"):
        command.extend(["--draft", str(args["draft"])])
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _workflow_query_publish_args(args: dict[str, Any]) -> list[str]:
    command = ["workflow", "query", "--draft", _required(args, "draft"), "--publish"]
    if args.get("report"):
        command.extend(["--report", str(args["report"])])
    return command


def _required(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not value:
        raise ValueError(f"Missing required argument: {name}")
    return str(value)


def _optional_string_list(args: dict[str, Any], name: str) -> list[str]:
    values = args.get(name) or []
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list when provided.")
    return [str(value) for value in values]


def _run_cli(argv: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            exit_code = cli_main(argv)
        except SystemExit as exc:
            exit_code = _system_exit_code(exc)
    return exit_code, stdout.getvalue() + stderr.getvalue()


def _system_exit_code(exc: SystemExit) -> int:
    if isinstance(exc.code, int):
        return exc.code
    if exc.code is None:
        return 0
    return 1


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    raise SystemExit(main())
