"""Immutable, validated JSON contracts for Vault migration."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIGRATION_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MODES = frozenset({"0644", "0755"})
_POLICIES = frozenset({"replace-if-unmodified"})
_ACTIONS = frozenset(
    {
        "unchanged",
        "create",
        "replace",
        "preserve",
        "review-required",
        "rebuild-suggested",
        "invalid",
    }
)

MigrationActionName = Literal[
    "unchanged",
    "create",
    "replace",
    "preserve",
    "review-required",
    "rebuild-suggested",
    "invalid",
]


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _require_exact_keys(value: object, keys: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    actual = set(value)
    unknown = actual - keys
    missing = keys - actual
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {', '.join(sorted(unknown))}.")
    if missing:
        raise ValueError(f"{name} is missing keys: {', '.join(sorted(missing))}.")
    return value


def _require_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must not be negative.")
    return value


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _validate_path(value: object, name: str = "path") -> str:
    path = _require_string(value, name)
    if "\\" in path:
        raise ValueError(f"{name} must use a safe relative POSIX path.")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"{name} must be a safe relative path.")
    return path


def _validate_sha256(value: object, name: str) -> str:
    digest = _require_string(value, name)
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"{name} must be a 64-character lowercase hexadecimal SHA-256.")
    return digest


def _validate_optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _validate_sha256(value, name)


def _validate_mode(value: object, name: str) -> str:
    mode = _require_string(value, name)
    if mode not in _MODES:
        raise ValueError(f"{name} must be one of {sorted(_MODES)}.")
    return mode


def _validate_optional_mode(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _validate_mode(value, name)


@dataclass(frozen=True)
class ManagedFile:
    path: str
    sha256: str
    mode: str
    policy: str = "replace-if-unmodified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_path(self.path))
        object.__setattr__(self, "sha256", _validate_sha256(self.sha256, "sha256"))
        object.__setattr__(self, "mode", _validate_mode(self.mode, "mode"))
        if self.policy not in _POLICIES:
            raise ValueError(f"policy must be one of {sorted(_POLICIES)}.")

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "path": self.path,
            "policy": self.policy,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ManagedFile":
        data = _require_exact_keys(value, frozenset({"path", "sha256", "mode", "policy"}), "managed file")
        return cls(
            path=_validate_path(data["path"]),
            sha256=_validate_sha256(data["sha256"], "sha256"),
            mode=_validate_mode(data["mode"], "mode"),
            policy=_require_string(data["policy"], "policy"),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "ManagedFile":
        return cls.from_dict(_parse_json(value, "managed file"))


def _validate_managed_files(value: object, name: str) -> Mapping[str, ManagedFile]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    managed: dict[str, ManagedFile] = {}
    for path, item in value.items():
        relative = _validate_path(path, f"{name} path")
        file = item if isinstance(item, ManagedFile) else ManagedFile.from_dict(item)
        if file.path != relative:
            raise ValueError(f"{name} key must match managed file path: {relative}.")
        managed[relative] = file
    return MappingProxyType(dict(sorted(managed.items())))


@dataclass(frozen=True)
class ContractCatalog:
    schema_version: int
    contract_version: int
    builder_revision: str
    managed_files: Mapping[str, ManagedFile]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_int(self.schema_version, "schema_version"))
        object.__setattr__(self, "contract_version", _require_int(self.contract_version, "contract_version"))
        object.__setattr__(self, "builder_revision", _require_string(self.builder_revision, "builder_revision"))
        object.__setattr__(self, "managed_files", _validate_managed_files(dict(self.managed_files), "managed_files"))

    def to_dict(self) -> dict[str, object]:
        return {
            "builder_revision": self.builder_revision,
            "contract_version": self.contract_version,
            "managed_files": {path: item.to_dict() for path, item in self.managed_files.items()},
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ContractCatalog":
        data = _require_exact_keys(
            value,
            frozenset({"schema_version", "contract_version", "builder_revision", "managed_files"}),
            "catalog",
        )
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version"),
            contract_version=_require_int(data["contract_version"], "contract_version"),
            builder_revision=_require_string(data["builder_revision"], "builder_revision"),
            managed_files=_validate_managed_files(data["managed_files"], "managed_files"),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "ContractCatalog":
        return cls.from_dict(_parse_json(value, "catalog"))


@dataclass(frozen=True)
class VaultManifest:
    schema_version: int
    contract_version: int
    builder_revision: str
    generated_at: str
    managed_files: Mapping[str, ManagedFile]
    last_migration_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_int(self.schema_version, "schema_version"))
        object.__setattr__(self, "contract_version", _require_int(self.contract_version, "contract_version"))
        object.__setattr__(self, "builder_revision", _require_string(self.builder_revision, "builder_revision"))
        object.__setattr__(self, "generated_at", _require_string(self.generated_at, "generated_at"))
        object.__setattr__(self, "managed_files", _validate_managed_files(dict(self.managed_files), "managed_files"))
        if self.last_migration_id is not None and not _MIGRATION_ID_RE.fullmatch(self.last_migration_id):
            raise ValueError("last_migration_id must be 16 lowercase hexadecimal characters.")

    def to_dict(self) -> dict[str, object]:
        return {
            "builder_revision": self.builder_revision,
            "contract_version": self.contract_version,
            "generated_at": self.generated_at,
            "last_migration_id": self.last_migration_id,
            "managed_files": {path: item.to_dict() for path, item in self.managed_files.items()},
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "VaultManifest":
        data = _require_exact_keys(
            value,
            frozenset(
                {"schema_version", "contract_version", "builder_revision", "generated_at", "managed_files", "last_migration_id"}
            ),
            "manifest",
        )
        migration_id = data["last_migration_id"]
        if migration_id is not None:
            migration_id = _require_string(migration_id, "last_migration_id")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version"),
            contract_version=_require_int(data["contract_version"], "contract_version"),
            builder_revision=_require_string(data["builder_revision"], "builder_revision"),
            generated_at=_require_string(data["generated_at"], "generated_at"),
            managed_files=_validate_managed_files(data["managed_files"], "managed_files"),
            last_migration_id=migration_id,
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "VaultManifest":
        return cls.from_dict(_parse_json(value, "manifest"))


@dataclass(frozen=True)
class MigrationAction:
    path: str
    action: MigrationActionName
    current_sha256: str | None
    desired_sha256: str | None
    current_mode: str | None
    desired_mode: str | None
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_path(self.path))
        if self.action not in _ACTIONS:
            raise ValueError(f"action must be one of {sorted(_ACTIONS)}.")
        object.__setattr__(self, "current_sha256", _validate_optional_sha256(self.current_sha256, "current_sha256"))
        object.__setattr__(self, "desired_sha256", _validate_optional_sha256(self.desired_sha256, "desired_sha256"))
        object.__setattr__(self, "current_mode", _validate_optional_mode(self.current_mode, "current_mode"))
        object.__setattr__(self, "desired_mode", _validate_optional_mode(self.desired_mode, "desired_mode"))
        object.__setattr__(self, "reason", _require_string(self.reason, "reason"))

    def to_dict(self) -> dict[str, str | None]:
        return {
            "action": self.action,
            "current_mode": self.current_mode,
            "current_sha256": self.current_sha256,
            "desired_mode": self.desired_mode,
            "desired_sha256": self.desired_sha256,
            "path": self.path,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> "MigrationAction":
        data = _require_exact_keys(
            value,
            frozenset(
                {"path", "action", "current_sha256", "desired_sha256", "current_mode", "desired_mode", "reason"}
            ),
            "migration action",
        )
        action = _require_string(data["action"], "action")
        return cls(
            path=_validate_path(data["path"]),
            action=action,  # type: ignore[arg-type]
            current_sha256=_validate_optional_sha256(data["current_sha256"], "current_sha256"),
            desired_sha256=_validate_optional_sha256(data["desired_sha256"], "desired_sha256"),
            current_mode=_validate_optional_mode(data["current_mode"], "current_mode"),
            desired_mode=_validate_optional_mode(data["desired_mode"], "desired_mode"),
            reason=_require_string(data["reason"], "reason"),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "MigrationAction":
        return cls.from_dict(_parse_json(value, "migration action"))


def _validate_actions(value: object) -> tuple[MigrationAction, ...]:
    if not isinstance(value, list):
        raise ValueError("actions must be a list.")
    return tuple(MigrationAction.from_dict(item) for item in value)


def _validate_authority_issues(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("authority_issues must be a list of objects.")
    return tuple(dict(item) for item in value)


@dataclass(frozen=True)
class MigrationPlan:
    schema_version: int
    migration_id: str
    current_contract_version: int | None
    target_contract_version: int
    builder_revision: str
    target_fingerprint: str
    actions: tuple[MigrationAction, ...]
    authority_issues: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_int(self.schema_version, "schema_version"))
        if not _MIGRATION_ID_RE.fullmatch(self.migration_id):
            raise ValueError("migration_id must be 16 lowercase hexadecimal characters.")
        if self.current_contract_version is not None:
            object.__setattr__(
                self,
                "current_contract_version",
                _require_int(self.current_contract_version, "current_contract_version"),
            )
        object.__setattr__(self, "target_contract_version", _require_int(self.target_contract_version, "target_contract_version"))
        object.__setattr__(self, "builder_revision", _require_string(self.builder_revision, "builder_revision"))
        object.__setattr__(self, "target_fingerprint", _validate_sha256(self.target_fingerprint, "target_fingerprint"))
        object.__setattr__(self, "actions", tuple(self.actions))
        if any(not isinstance(action, MigrationAction) for action in self.actions):
            raise ValueError("actions must contain migration actions.")
        object.__setattr__(self, "authority_issues", _validate_authority_issues(list(self.authority_issues)))

    @property
    def has_blocking_actions(self) -> bool:
        return any(action.action in {"review-required", "invalid"} for action in self.actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "authority_issues": [dict(issue) for issue in self.authority_issues],
            "builder_revision": self.builder_revision,
            "current_contract_version": self.current_contract_version,
            "migration_id": self.migration_id,
            "schema_version": self.schema_version,
            "target_contract_version": self.target_contract_version,
            "target_fingerprint": self.target_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> "MigrationPlan":
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema_version",
                    "migration_id",
                    "current_contract_version",
                    "target_contract_version",
                    "builder_revision",
                    "target_fingerprint",
                    "actions",
                    "authority_issues",
                }
            ),
            "migration plan",
        )
        current_version = data["current_contract_version"]
        if current_version is not None:
            current_version = _require_int(current_version, "current_contract_version")
        return cls(
            schema_version=_require_int(data["schema_version"], "schema_version"),
            migration_id=_require_string(data["migration_id"], "migration_id"),
            current_contract_version=current_version,
            target_contract_version=_require_int(data["target_contract_version"], "target_contract_version"),
            builder_revision=_require_string(data["builder_revision"], "builder_revision"),
            target_fingerprint=_validate_sha256(data["target_fingerprint"], "target_fingerprint"),
            actions=_validate_actions(data["actions"]),
            authority_issues=_validate_authority_issues(data["authority_issues"]),
        )

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "MigrationPlan":
        return cls.from_dict(_parse_json(value, "migration plan"))


def _parse_json(value: str, name: str) -> object:
    if not isinstance(value, str):
        raise ValueError(f"{name} JSON must be a string.")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} JSON is invalid.") from exc
