"""Shared version 1 Vault manifest schema validation."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIGRATION_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
_MANAGED_MODES = frozenset({"0644", "0755"})
_MANAGED_POLICIES = frozenset({"replace-if-unmodified"})
MANAGED_EXACT_PATH_SCOPE = frozenset(
    {
        ".agents/skills/wiki-ingest/SKILL.md",
        ".agents/skills/wiki-query/SKILL.md",
        ".agents/skills/wiki-update/SKILL.md",
        ".github/workflows/release-gate.yml",
        ".gitignore",
        "AGENTS.md",
        "README-kr.md",
        "README.md",
        "docs/LLM-WIKI.md",
        "docs/USER_PROMPTS.md",
        "docs/VAULT_PROMPTS.md",
        "docs/agent/DRAFTS.md",
        "docs/agent/OPERATING-SCHEMA.md",
        "docs/usage.md",
        "tests/__init__.py",
        "tests/test_wiki_quality_baseline.py",
        "tests/test_wiki_tools.py",
        "tools/wiki/cli.py",
        "tools/wiki/lib/.gitkeep",
        "tools/wiki/lib/authority.py",
        "tools/wiki/lib/draft.py",
        "tools/wiki/lib/frontmatter.py",
        "tools/wiki/lib/hash_source.py",
        "tools/wiki/lib/ingest.py",
        "tools/wiki/lib/links.py",
        "tools/wiki/lib/lint.py",
        "tools/wiki/lib/maps.py",
        "tools/wiki/lib/merge.py",
        "tools/wiki/lib/metrics.py",
        "tools/wiki/lib/paths.py",
        "tools/wiki/lib/review.py",
        "tools/wiki/lib/schema.py",
        "tools/wiki/lib/source_registry.py",
        "tools/wiki/lib/vault_manifest.py",
        "tools/wiki/lib/wiki_update.py",
        "tools/wiki/lib/workflow.py",
        "tools/wiki/mcp_server.py",
        "tools/wiki/metrics-policy.json",
        "tools/wiki/templates/draft-batch-upsert-pages.json",
        "tools/wiki/templates/draft-upsert-page.json",
        "tools/wiki/templates/frontmatter.yaml",
    }
)
MANAGED_TREE_ROOT_SCOPE: tuple[str, ...] = ()
_REQUIRED_MANIFEST_KEYS = frozenset(
    {
        "builder_revision",
        "contract_version",
        "generated_at",
        "managed_files",
        "schema_version",
    }
)
_OPTIONAL_MANIFEST_KEYS = frozenset({"last_migration_id"})
_MANAGED_FILE_KEYS = frozenset({"mode", "path", "policy", "sha256"})


def parse_v1_manifest_json(contents: str) -> dict[str, object]:
    """Parse JSON without accepting duplicate keys, then validate all v1 fields."""
    if not isinstance(contents, str):
        raise ValueError("Vault builder manifest JSON must be text.")
    try:
        value = json.loads(
            contents,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Vault builder manifest JSON is invalid.") from exc
    return validate_v1_manifest(value)


def validate_v1_manifest(value: object) -> dict[str, object]:
    """Return a normalized manifest after complete contract-v1 validation."""
    if not isinstance(value, dict):
        raise ValueError("Vault builder manifest must be a JSON object.")
    actual = set(value)
    allowed = _REQUIRED_MANIFEST_KEYS | _OPTIONAL_MANIFEST_KEYS
    unknown = actual - allowed
    missing = _REQUIRED_MANIFEST_KEYS - actual
    if unknown:
        raise ValueError(
            "Vault builder manifest contains unknown fields: "
            f"{', '.join(sorted(unknown))}."
        )
    if missing:
        raise ValueError(
            "Vault builder manifest is missing fields: "
            f"{', '.join(sorted(missing))}."
        )

    schema_version = _exact_version(value["schema_version"], "schema_version")
    contract_version = _exact_version(
        value["contract_version"],
        "contract_version",
    )
    builder_revision = value["builder_revision"]
    if (
        not isinstance(builder_revision, str)
        or not builder_revision
        or not builder_revision.strip()
    ):
        raise ValueError(
            "Vault builder manifest builder_revision must be a non-empty string."
        )
    generated_at = _utc_timestamp(value["generated_at"])
    managed_files = _managed_files(value["managed_files"])
    last_migration_id = value.get("last_migration_id")
    if last_migration_id is not None and (
        not isinstance(last_migration_id, str)
        or not _MIGRATION_ID_RE.fullmatch(last_migration_id)
    ):
        raise ValueError(
            "Vault builder manifest last_migration_id must be "
            "16 lowercase hexadecimal characters."
        )
    return {
        "builder_revision": builder_revision,
        "contract_version": contract_version,
        "generated_at": generated_at,
        "last_migration_id": last_migration_id,
        "managed_files": managed_files,
        "schema_version": schema_version,
    }


def _exact_version(value: object, name: str) -> int:
    if type(value) is not int or value != 1:
        label = "schema" if name == "schema_version" else "contract"
        raise ValueError(f"Vault builder manifest supported {label} version is 1.")
    return value


def _utc_timestamp(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _UTC_TIMESTAMP_RE.fullmatch(value)
    ):
        raise ValueError(
            "Vault builder manifest generated_at must use "
            "UTC format YYYY-MM-DDTHH:MM:SSZ."
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(
            "Vault builder manifest generated_at must be a valid UTC timestamp."
        ) from exc
    return value


def _managed_files(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError(
            "Vault builder manifest managed_files must be an object."
        )
    managed: dict[str, dict[str, str]] = {}
    for raw_path, raw_entry in value.items():
        path = _safe_path(raw_path, "managed_files path")
        if not managed_path_is_in_contract_scope(path):
            raise ValueError(
                "Vault builder manifest managed file is outside the "
                f"contract-managed catalog scope: {path}."
            )
        if not isinstance(raw_entry, dict):
            raise ValueError(
                f"Vault builder manifest managed file must be an object: {path}."
            )
        actual = set(raw_entry)
        if actual != _MANAGED_FILE_KEYS:
            unknown = actual - _MANAGED_FILE_KEYS
            missing = _MANAGED_FILE_KEYS - actual
            if unknown:
                raise ValueError(
                    "Vault builder manifest managed file contains unknown "
                    f"fields for {path}: {', '.join(sorted(unknown))}."
                )
            raise ValueError(
                "Vault builder manifest managed file is missing fields for "
                f"{path}: {', '.join(sorted(missing))}."
            )
        entry_path = _safe_path(raw_entry["path"], f"{path} path")
        if entry_path != path:
            raise ValueError(
                "Vault builder manifest managed_files key must match entry "
                f"path: {path}."
            )
        digest = raw_entry["sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(
                f"Vault builder manifest SHA-256 is invalid: {path}."
            )
        mode = raw_entry["mode"]
        if not isinstance(mode, str) or mode not in _MANAGED_MODES:
            raise ValueError(
                f"Vault builder manifest mode is invalid: {path}."
            )
        policy = raw_entry["policy"]
        if (
            not isinstance(policy, str)
            or policy not in _MANAGED_POLICIES
        ):
            raise ValueError(
                f"Vault builder manifest policy is invalid: {path}."
            )
        managed[path] = {
            "mode": mode,
            "path": entry_path,
            "policy": policy,
            "sha256": digest,
        }
    return dict(sorted(managed.items()))


def managed_path_is_in_contract_scope(path: str) -> bool:
    """Return whether a safe path is an exact version-1 catalog member."""
    return path in MANAGED_EXACT_PATH_SCOPE or path.startswith(
        MANAGED_TREE_ROOT_SCOPE
    )


def _safe_path(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(
            f"Vault builder manifest {name} must be a safe relative POSIX path."
        )
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        raise ValueError(
            f"Vault builder manifest {name} must be a safe relative POSIX path."
        )
    return value


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(
                f"Vault builder manifest contains duplicate field: {key}."
            )
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(
        f"Vault builder manifest contains invalid numeric constant: {value}."
    )
