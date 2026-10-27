"""Shared Vault manifest schema validation for schema versions 1 and 2."""
from __future__ import annotations

import hashlib
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
        ".claude/skills/wiki-ingest/SKILL.md",
        ".claude/skills/wiki-query/SKILL.md",
        ".claude/skills/wiki-update/SKILL.md",
        ".github/workflows/release-gate.yml",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "README-kr.md",
        "README.md",
        "docs/LLM-WIKI.md",
        "docs/USER_PROMPTS.md",
        "docs/VAULT_PROMPTS.md",
        "docs/agent/DRAFTS.md",
        "docs/agent/OPERATING-SCHEMA.md",
        "docs/usage.md",
        "scripts/release_gate.sh",
        "tests/__init__.py",
        "tests/test_wiki_quality_baseline.py",
        "tests/test_wiki_tools.py",
        "tools/wiki/cli.py",
        "tools/wiki/lib/.gitkeep",
        "tools/wiki/lib/authority.py",
        "tools/wiki/lib/draft.py",
        "tools/wiki/lib/draft_patch.py",
        "tools/wiki/lib/frontmatter.py",
        "tools/wiki/lib/hash_source.py",
        "tools/wiki/lib/inbox.py",
        "tools/wiki/lib/ingest.py",
        "tools/wiki/lib/links.py",
        "tools/wiki/lib/lint.py",
        "tools/wiki/lib/maps.py",
        "tools/wiki/lib/merge.py",
        "tools/wiki/lib/metrics.py",
        "tools/wiki/lib/paths.py",
        "tools/wiki/lib/review.py",
        "tools/wiki/lib/schema.py",
        "tools/wiki/lib/search.py",
        "tools/wiki/lib/source_registry.py",
        "tools/wiki/lib/vault_manifest.py",
        "tools/wiki/lib/wiki_update.py",
        "tools/wiki/lib/workflow.py",
        "tools/wiki/mcp_server.py",
        "tools/wiki/metrics-policy.json",
        "tools/wiki/templates/draft-batch-upsert-pages.json",
        "tools/wiki/templates/draft-patch-page.json",
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

# Written manifests always use the current schema; older schemas stay readable.
MANIFEST_SCHEMA_VERSION = 2
MANIFEST_CONTRACT_VERSION = 2
# Each readable schema version has exactly one contract version.
_SUPPORTED_VERSIONS = {1: 1, 2: 2}
EXTERNAL_RECORDS_KEY = "external_records"
# Records of files and entries created outside the Vault, located relative to $HOME.
EXTERNAL_RECORD_KEYS = {
    "file": frozenset({"kind", "location", "sha256", "shared"}),
    "config": frozenset({"kind", "location", "shared"}),
    "json-entry": frozenset({"kind", "location", "key_path", "sha256", "shared"}),
}
_JSON_ENTRY_KEY_PATH_RE = re.compile(r"^mcpServers\.([A-Za-z0-9][A-Za-z0-9_.-]*)$")


def normalized_json_sha256(value: object) -> str:
    """Return the SHA-256 of a JSON value serialized in canonical form."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_manifest_json(contents: str) -> dict[str, object]:
    """Parse JSON without accepting duplicate keys, then validate all fields."""
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
    return validate_manifest(value)


def validate_v1_manifest(value: object) -> dict[str, object]:
    """Validate a manifest and require schema version 1."""
    manifest = validate_manifest(value)
    if manifest["schema_version"] != 1:
        raise ValueError("Vault builder manifest supported schema version is 1.")
    return manifest


def validate_manifest(value: object) -> dict[str, object]:
    """Return a normalized schema-1 or schema-2 manifest after complete validation.

    Schema 2 output always contains ``external_records``; schema 1 output never does.
    """
    if not isinstance(value, dict):
        raise ValueError("Vault builder manifest must be a JSON object.")
    actual = set(value)
    schema_version = (
        _schema_version(value["schema_version"]) if "schema_version" in value else 0
    )
    required = set(_REQUIRED_MANIFEST_KEYS)
    if schema_version >= 2:
        required.add(EXTERNAL_RECORDS_KEY)
    allowed = required | _OPTIONAL_MANIFEST_KEYS
    unknown = actual - allowed
    missing = required - actual
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

    contract_version = _contract_version(value["contract_version"], schema_version)
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
    normalized: dict[str, object] = {
        "builder_revision": builder_revision,
        "contract_version": contract_version,
        "generated_at": generated_at,
        "last_migration_id": last_migration_id,
        "managed_files": managed_files,
        "schema_version": schema_version,
    }
    if schema_version >= 2:
        normalized[EXTERNAL_RECORDS_KEY] = validate_external_records(
            value[EXTERNAL_RECORDS_KEY]
        )
    return normalized


def _schema_version(value: object) -> int:
    if type(value) is not int or value not in _SUPPORTED_VERSIONS:
        supported = ", ".join(str(version) for version in sorted(_SUPPORTED_VERSIONS))
        raise ValueError(
            f"Vault builder manifest supported schema versions are {supported}."
        )
    return value


def _contract_version(value: object, schema_version: int) -> int:
    expected = _SUPPORTED_VERSIONS[schema_version]
    if type(value) is not int or value != expected:
        raise ValueError(
            f"Vault builder manifest supported contract version is {expected} "
            f"for schema version {schema_version}."
        )
    return value


def validate_external_records(value: object) -> list[dict[str, object]]:
    """Return sorted, normalized records of files and entries outside the Vault."""
    if not isinstance(value, list):
        raise ValueError(
            "Vault builder manifest external_records must be a list."
        )
    records: list[dict[str, object]] = []
    whole_file_locations: set[str] = set()
    entry_locations: set[str] = set()
    entries: set[tuple[str, str]] = set()
    for item in value:
        record = _external_record(item)
        location = str(record["location"])
        if record["kind"] == "json-entry":
            identity = (location, str(record["key_path"]))
            if identity in entries or location in whole_file_locations:
                raise ValueError(
                    "Vault builder manifest external record is duplicated: "
                    f"{location} {record['key_path']}."
                )
            entries.add(identity)
            entry_locations.add(location)
        else:
            if location in whole_file_locations or location in entry_locations:
                raise ValueError(
                    "Vault builder manifest external record is duplicated: "
                    f"{location}."
                )
            whole_file_locations.add(location)
        records.append(record)
    return sorted(
        records,
        key=lambda record: (str(record["location"]), str(record.get("key_path", ""))),
    )


def _external_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(
            "Vault builder manifest external record must be an object."
        )
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in EXTERNAL_RECORD_KEYS:
        raise ValueError(
            "Vault builder manifest external record kind must be one of "
            f"{', '.join(sorted(EXTERNAL_RECORD_KEYS))}."
        )
    expected = EXTERNAL_RECORD_KEYS[kind]
    actual = set(value)
    if actual != expected:
        unknown = actual - expected
        missing = expected - actual
        if unknown:
            raise ValueError(
                f"Vault builder manifest {kind} external record contains "
                f"unknown fields: {', '.join(sorted(unknown))}."
            )
        raise ValueError(
            f"Vault builder manifest {kind} external record is missing "
            f"fields: {', '.join(sorted(missing))}."
        )
    location = _safe_path(value["location"], "external record location")
    if location.split("/", 1)[0].startswith("~"):
        raise ValueError(
            "Vault builder manifest external record location must be "
            "relative to $HOME without a ~ prefix."
        )
    shared = value["shared"]
    if type(shared) is not bool:
        raise ValueError(
            "Vault builder manifest external record shared must be a boolean."
        )
    record: dict[str, object] = {
        "kind": kind,
        "location": location,
        "shared": shared,
    }
    if "sha256" in expected:
        digest = value["sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(
                f"Vault builder manifest external record SHA-256 is invalid: {location}."
            )
        record["sha256"] = digest
    if kind == "json-entry":
        key_path = value["key_path"]
        if not isinstance(key_path, str) or not _JSON_ENTRY_KEY_PATH_RE.fullmatch(key_path):
            raise ValueError(
                "Vault builder manifest json-entry key_path must be "
                "mcpServers.<name>."
            )
        if shared:
            raise ValueError(
                "Vault builder manifest json-entry external record must not be shared."
            )
        record["key_path"] = key_path
    return record


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
    """Return whether a safe path is an exact catalog member."""
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
