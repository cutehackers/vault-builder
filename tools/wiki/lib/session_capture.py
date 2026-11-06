"""Capture Claude Code session turns into a project vault's raw/sessions/ (spec 4.2-4.4).

The transcript format is not a public contract. The rules below follow transcripts written by
Claude Code 2.1.309: one JSON object per line, turn candidates have "type" "user" or "assistant",
and subagent conversations live in "<session_id>/subagents/*.jsonl" next to the main transcript.

The capture writes one new file under the target vault's raw/sessions/ and nothing else. It never
runs git commands that change state and never writes the vault manifest.

`sessions capture --normalized` reads a vault-graph normalized session file (vg-session-jsonl/1)
instead of a transcript and writes the part in vault-session/v2 (vault-graph request S10).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO
from urllib.parse import quote

from .frontmatter import _parse_frontmatter, render_page
from .memory_scan import MemoryScanError, ProjectStop, find_project, load_vault_map, vault_map_path
from .session_archive import (
    SESSION_AGENTS,
    SESSION_ARCHIVE_DIR,
    SESSION_ID_PATTERN,
    SESSION_SCHEMA,
    SESSION_SCHEMA_V2,
    check_session_file,
    parse_session_path,
    session_body_sha256,
    split_session_text,
)

TURN_ROLES = ("user", "assistant")
SUBAGENT_DIR = "subagents"

_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_UNKNOWN_AGENT = "unknown"


@dataclass(frozen=True)
class Turn:
    uuid: str
    at: datetime
    role: str
    agent_id: str | None
    text: str
    tools: tuple[str, ...]

    @property
    def at_text(self) -> str:
        return format_turn_at(self.at)

    @property
    def is_subagent(self) -> bool:
        return self.agent_id is not None


@dataclass(frozen=True)
class PartCursor:
    last_turn_uuid: str
    last_turn_at: str
    part: int = 0
    last_exchange_id: str | None = None  # Only vault-session/v2 parts have it.


def format_turn_at(value: datetime) -> str:
    """UTC time with milliseconds, the precision Claude Code writes."""
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_turn_at(value: str) -> datetime:
    """Parse an ISO 8601 UTC time such as 2026-10-30T01:00:00Z or 2026-10-30T01:00:00.123Z."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Not a timestamp: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_turns(transcript_path: Path, session_id: str, *, warnings: list[str] | None = None) -> list[Turn]:
    """Read the main transcript and its subagent transcripts, filter them, and sort by time then file order.

    Raises OSError when the main transcript cannot be read. Lines that are not JSON are skipped,
    and one message per file with skipped lines is appended to ``warnings``.
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"Session id has characters outside {SESSION_ID_PATTERN}: {session_id!r}")
    transcript_path = Path(transcript_path)
    sources = [(transcript_path, None)]
    subagent_dir = transcript_path.parent / session_id / SUBAGENT_DIR
    if subagent_dir.is_dir():
        for path in sorted(subagent_dir.glob("*.jsonl")):
            if path.is_file():
                sources.append((path, _agent_id_from_file(path)))

    seen: set[str] = set()
    ordered: list[tuple[datetime, int, Turn]] = []
    position = 0
    for path, file_agent_id in sources:
        for entry in _read_entries(path, warnings):
            turn = _entry_turn(entry, file_agent_id)
            if turn is None or turn.uuid in seen:
                continue
            seen.add(turn.uuid)
            ordered.append((turn.at, position, turn))
            position += 1
    ordered.sort(key=lambda item: (item[0], item[1]))
    return [turn for _, _, turn in ordered]


def new_turns(turns: list[Turn], cursor: PartCursor | None, *, warnings: list[str] | None = None) -> list[Turn]:
    """Return the turns after the previous part's last turn.

    If the cursor uuid is in ``turns``, every turn after it. Otherwise every turn later than
    ``last_turn_at``, with a message appended to ``warnings``. Raises ValueError when
    ``last_turn_at`` is needed and cannot be parsed.
    """
    if cursor is None:
        return list(turns)
    for index, turn in enumerate(turns):
        if turn.uuid == cursor.last_turn_uuid:
            return list(turns[index + 1 :])
    last_at = parse_turn_at(cursor.last_turn_at)
    if warnings is not None:
        warnings.append(
            f"Previous last_turn_uuid {cursor.last_turn_uuid} is not in the transcript; "
            f"keeping turns later than {cursor.last_turn_at}."
        )
    return [turn for turn in turns if turn.at > last_at]


def _agent_id_from_file(path: Path) -> str:
    stem = path.stem
    if stem.startswith("agent-"):
        stem = stem[len("agent-") :]
    return _clean_agent_id(stem)


def _clean_agent_id(value: Any) -> str:
    if isinstance(value, str) and _AGENT_ID_RE.fullmatch(value):
        return value
    return _UNKNOWN_AGENT


def _read_entries(path: Path, warnings: list[str] | None) -> list[dict[str, Any]]:
    text = path.read_bytes().decode("utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(entry, dict):
            entries.append(entry)
        else:
            skipped += 1
    if skipped and warnings is not None:
        warnings.append(f"Skipped {skipped} line(s) that are not JSON objects in {path.name}.")
    return entries


def _entry_turn(entry: dict[str, Any], file_agent_id: str | None) -> Turn | None:
    role = entry.get("type")
    if role not in TURN_ROLES:
        return None
    # isMeta user entries are text the system inserted (skill bodies, command caveats), not the person.
    # Slash command wrapper entries (<command-name>...) do not carry isMeta and stay.
    if role == "user" and entry.get("isMeta") is True:
        return None
    uuid = entry.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    try:
        at = parse_turn_at(entry.get("timestamp"))
    except (TypeError, ValueError):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None

    texts, tools = _content_parts(message.get("content"))
    text = _clean_text("\n\n".join(texts))
    if not text and not tools:
        return None

    agent_id: str | None = None
    if file_agent_id is not None:
        agent_id = _clean_agent_id(entry.get("agentId")) if entry.get("agentId") else file_agent_id
    elif entry.get("isSidechain") is True:
        agent_id = _clean_agent_id(entry.get("agentId"))
    return Turn(uuid=uuid, at=at, role=role, agent_id=agent_id, text=text, tools=tuple(tools))


def _content_parts(content: Any) -> tuple[list[str], list[str]]:
    """Keep text and tool names. Drop thinking, tool input, tool results, images and unknown blocks."""
    if isinstance(content, str):
        return [content], []
    texts: list[str] = []
    tools: list[str] = []
    if not isinstance(content, list):
        return texts, tools
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "tool_use" and isinstance(block.get("name"), str) and block["name"].strip():
            tools.append(block["name"].strip())
    return texts, tools


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


# Secret patterns (spec 4.3). Order matters: earlier patterns replace whole values so later
# patterns do not count the same secret twice. No value pattern matches an existing placeholder.
_PLACEHOLDER = "[REDACTED:"
_NOT_PLACEHOLDER = r"(?!\[REDACTED:)"

_PEM_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_URL_PASSWORD_RE = re.compile(
    r"(?P<pre>\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)" + _NOT_PLACEHOLDER + r"(?P<value>[^\s/@]+)(?=@)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9_-])authorization[\"']?\s*:\s*[\"']?)" + _NOT_PLACEHOLDER + r"(?P<value>[^\"'\s](?:[^\"'\r\n]*[^\"'\s])?)",
    re.IGNORECASE,
)
# A bearer token must contain a digit so prose such as "Bearer authentication" stays.
_BEARER_RE = re.compile(
    r"(?P<pre>\bBearer\s+)(?P<value>(?=[A-Za-z0-9\-._~+/]*[0-9])[A-Za-z0-9\-._~+/]{8,}=*)"
)
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai-key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}(?![0-9A-Za-z])")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")),
)
_SECRET_KEYS = ("password", "passwd", "secret", "token", "apikey", "accesskey", "privatekey", "clientsecret")


def _key_pattern(name: str) -> str:
    # Case and "-"/"_" do not matter, so api_key, API-KEY and apikey all match.
    return "[-_]?".join(re.escape(char) for char in name)


# The key may carry a prefix such as GITHUB_ or db- (GITHUB_TOKEN, db-password).
_KEY_VALUE_RE = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9])[\"']?(?:[A-Za-z0-9]+[-_])*(?:"
    + "|".join(_key_pattern(name) for name in _SECRET_KEYS)
    + r")[\"']?\s*[:=](?!=)\s*)"
    + _NOT_PLACEHOLDER
    + r"(?P<value>\"" + _NOT_PLACEHOLDER + r"(?:[^\"\\\n]|\\.)+\"|'" + _NOT_PLACEHOLDER + r"[^'\n]+'|[^\s\"',;(){}\[\]<>]+)",
    re.IGNORECASE,
)


def redact(text: str) -> tuple[str, int]:
    """Replace known secret shapes with [REDACTED:<kind>] and return the text and the number replaced."""
    count = 0

    def whole(kind: str) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return f"{_PLACEHOLDER}{kind}]"

        return replace

    def value_only(kind: str) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            value = match.group("value")
            quote = value[0] if value[:1] in ("'", '"') and len(value) >= 2 and value[-1] == value[0] else ""
            count += 1
            return f"{match.group('pre')}{quote}{_PLACEHOLDER}{kind}]{quote}"

        return replace

    text = _PEM_RE.sub(whole("private-key"), text)
    text = _URL_PASSWORD_RE.sub(value_only("url-password"), text)
    text = _AUTHORIZATION_RE.sub(value_only("authorization"), text)
    text = _BEARER_RE.sub(value_only("bearer"), text)
    for kind, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(whole(kind), text)
    text = _KEY_VALUE_RE.sub(value_only("secret"), text)
    return text, count


# Capture command (spec 4.2, 4.4 and section 6)

CAPTURE_AGENT = "claude-code"
TRIGGERS = ("PreCompact", "SessionEnd", "manual")
PROJECT_ROLE = "project"
MIN_CONTRACT_VERSION = 3
MANIFEST_RELATIVE = Path(".vault-builder") / "manifest.json"
LINK_HELP = "docs/usage.md section 17.7 (project vault 연결하기)"
_WRITE_ATTEMPTS = 2


class CaptureStop(Exception):
    """The capture writes nothing. manual_exit is the exit code for --transcript input; hook input exits 0."""

    def __init__(self, message: str, manual_exit: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.manual_exit = manual_exit


@dataclass(frozen=True)
class CaptureRequest:
    transcript_path: Path
    session_id: str
    cwd: Path
    trigger: str
    vault: Path | None
    from_hook: bool


@dataclass(frozen=True)
class TargetVault:
    name: str
    path: Path
    project: str


@dataclass(frozen=True)
class CaptureSkip:
    reason: str
    manual_exit: int


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    project: str
    cwd: str
    part: int
    collected_at: str
    trigger: str
    agent: str = CAPTURE_AGENT


@dataclass(frozen=True)
class CaptureResult:
    status: str  # "written", "no-new-turns" or "skipped"
    message: str
    manual_exit: int = 0
    vault: str | None = None
    path: str | None = None
    turn_count: int = 0
    redactions: int = 0
    warnings: tuple[str, ...] = field(default=())
    extra: tuple[tuple[str, Any], ...] = field(default=())  # --normalized adds schema, part, exchange_count.

    def exit_code(self, from_hook: bool) -> int:
        return 0 if from_hook else self.manual_exit

    def as_json(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "message": self.message,
            "vault": self.vault,
            "path": self.path,
            "turn_count": self.turn_count,
            "redactions": self.redactions,
            "warnings": list(self.warnings),
        }
        data.update(self.extra)
        return data


def read_capture_request(values: Mapping[str, Any], stdin: TextIO | None) -> CaptureRequest:
    """Build the request from --transcript options (manual form) or one hook JSON object on stdin (hook form).

    Raises CaptureStop when the input cannot name a transcript, a session id or a trigger.
    """
    transcript = values.get("transcript")
    if transcript:
        from_hook = False
        transcript_path = Path(transcript).expanduser()
        session_id = values.get("session_id") or transcript_path.stem
        cwd_value = values.get("cwd")
        trigger = values.get("trigger") or "manual"
    else:
        from_hook = True
        hook = _read_hook_json(stdin)
        transcript_value = hook.get("transcript_path")
        if not isinstance(transcript_value, str) or not transcript_value.strip():
            raise CaptureStop("Hook JSON has no transcript_path; nothing written.")
        transcript_path = Path(transcript_value).expanduser()
        session_id = _text(hook.get("session_id")) or values.get("session_id") or transcript_path.stem
        cwd_value = _text(hook.get("cwd")) or values.get("cwd")
        trigger = values.get("trigger") or hook.get("hook_event_name")

    if trigger not in TRIGGERS:
        raise CaptureStop(f"Trigger {trigger!r} is not one of {', '.join(TRIGGERS)}; nothing written.")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise CaptureStop(f"Session id {session_id!r} has characters outside {SESSION_ID_PATTERN}; nothing written.")
    if cwd_value:
        cwd = Path(cwd_value).expanduser()
    else:
        cwd = _transcript_cwd(transcript_path) or Path.cwd()
    vault = values.get("vault")
    return CaptureRequest(
        transcript_path=transcript_path,
        session_id=session_id,
        cwd=cwd,
        trigger=trigger,
        vault=Path(vault).expanduser() if vault else None,
        from_hook=from_hook,
    )


def select_target_vault(request: CaptureRequest, env: Mapping[str, str] | None = None) -> TargetVault | CaptureSkip:
    """Pick the one role project vault linked to the cwd project (spec 4.4). Never personal or team."""
    map_path = vault_map_path(env)
    if not map_path.is_file():
        return CaptureSkip(f"No vault map at {display_home_path(map_path, env)}; nothing written.", 1)
    try:
        vaults, projects = load_vault_map(map_path)
    except MemoryScanError as exc:
        return CaptureSkip(f"{exc} Nothing written.", 1)

    found = find_project(request.cwd, {"projects": projects})
    if isinstance(found, ProjectStop):
        return CaptureSkip(f"{found.reason} Nothing written.", 1)
    project_id = found.project_id
    candidates = [vaults[name] for name in projects[project_id]["vaults"] if vaults[name].role == PROJECT_ROLE]

    if request.vault is not None:
        wanted = _resolve(request.vault)
        chosen = [item for item in candidates if _resolve(item.path) == wanted]
        if not chosen:
            return CaptureSkip(
                f"--vault {display_home_path(request.vault, env)} is not a role project vault of project "
                f"{project_id}; nothing written.",
                1,
            )
        candidates = chosen[:1]
    elif not candidates:
        return CaptureSkip(
            f"Project {project_id} has no role project vault, so the session was not saved. Create a project vault "
            f"and link it to this project with link (see {LINK_HELP}).",
            0,
        )
    elif len(candidates) > 1:
        names = ", ".join(item.name for item in candidates)
        return CaptureSkip(
            f"Project {project_id} has {len(candidates)} role project vaults ({names}); pass --vault to choose one. "
            "Nothing written.",
            0,
        )

    target = candidates[0]
    contract_problem = _contract_problem(target.path)
    if contract_problem is not None:
        return CaptureSkip(
            f"Vault {target.name} {contract_problem}; run the contract {MIN_CONTRACT_VERSION} migration "
            "(audit, then apply) for that vault first. Nothing written.",
            0,
        )
    return TargetVault(name=target.name, path=target.path, project=project_id)


def find_previous_part(
    vault_root: Path, agent: str, session_id: str, *, warnings: list[str] | None = None
) -> PartCursor | None:
    """Read the cursor from the highest part file of the session. No separate state file exists.

    Raises CaptureStop when that file's last_turn_uuid or last_turn_at cannot be read.
    """
    directory = Path(vault_root) / SESSION_ARCHIVE_DIR / agent
    if not directory.is_dir():
        return None
    parts: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if path.name.startswith(".") or not path.is_file():
            continue
        parsed = parse_session_path(f"{SESSION_ARCHIVE_DIR}/{agent}/{path.name}")
        if parsed is not None and parsed.session_id == session_id:
            parts.append((parsed.part, path))
    if not parts:
        return None
    part = max(number for number, _ in parts)
    paths = sorted(path for number, path in parts if number == part)
    path = paths[-1]
    relative = f"{SESSION_ARCHIVE_DIR}/{agent}/{path.name}"
    if len(paths) > 1 and warnings is not None:
        warnings.append(f"Part {part} of session {session_id} exists {len(paths)} times; reading {relative}.")

    check = check_session_file(Path(vault_root), path)
    if check.hash_mismatch and warnings is not None:
        warnings.append(
            f"Previous part {relative} body does not match its body_sha256; using its cursor and leaving the file as is."
        )
    frontmatter = check.frontmatter or {}
    last_uuid = frontmatter.get("last_turn_uuid")
    last_at = frontmatter.get("last_turn_at")
    try:
        if not isinstance(last_uuid, str) or not last_uuid or not isinstance(last_at, str):
            raise ValueError
        parse_turn_at(last_at)
    except ValueError:
        raise CaptureStop(
            f"Cannot read last_turn_uuid and last_turn_at from {relative}; nothing written to avoid repeating turns."
        ) from None
    last_exchange = frontmatter.get("last_exchange_id")
    return PartCursor(
        last_turn_uuid=last_uuid,
        last_turn_at=last_at,
        part=part,
        last_exchange_id=last_exchange if isinstance(last_exchange, str) and last_exchange else None,
    )


def highest_part(vault_root: Path, agent: str, session_id: str) -> int:
    """The highest part number of the session in raw/sessions/<agent>/, v1 and v2 together. 0 when none."""
    directory = Path(vault_root) / SESSION_ARCHIVE_DIR / agent
    if not directory.is_dir():
        return 0
    numbers = [
        parsed.part
        for path in directory.iterdir()
        if not path.name.startswith(".") and path.is_file()
        for parsed in [parse_session_path(f"{SESSION_ARCHIVE_DIR}/{agent}/{path.name}")]
        if parsed is not None and parsed.session_id == session_id
    ]
    return max(numbers, default=0)


def render_session_file(meta: SessionMeta, turns: list[Turn]) -> str:
    """Render the session file text: frontmatter in SESSION_FRONTMATTER_FIELDS order, then redacted turns."""
    if not turns:
        raise ValueError("A session file needs at least one turn.")
    blocks: list[str] = []
    redactions = 0
    for turn in turns:
        role = f"subagent {turn.agent_id} {turn.role}" if turn.is_subagent else turn.role
        pieces = [f"## {turn.at_text} {role}"]
        if turn.text:
            text, count = redact(turn.text)
            redactions += count
            pieces.append(_clean_text(text))
        if turn.tools:
            pieces.append("Tools: " + ", ".join(turn.tools))
        blocks.append("\n\n".join(pieces))
    body = "\n\n".join(blocks) + "\n"

    frontmatter: dict[str, Any] = {
        "schema": SESSION_SCHEMA,
        "agent": meta.agent,
        "session_id": meta.session_id,
        "project": meta.project,
        "cwd": meta.cwd,
        "part": meta.part,
        "first_turn_uuid": turns[0].uuid,
        "first_turn_at": turns[0].at_text,
        "last_turn_uuid": turns[-1].uuid,
        "last_turn_at": turns[-1].at_text,
        "turn_count": len(turns),
        "collected_at": meta.collected_at,
        "trigger": meta.trigger,
        "redactions": redactions,
        "body_sha256": "",
    }
    return _session_text(frontmatter, body)


def _session_text(frontmatter: dict[str, Any], body: str) -> str:
    """Render frontmatter and body, filling body_sha256 last."""
    for key, value in frontmatter.items():
        if isinstance(value, str) and ("\n" in value or "\r" in value):
            raise ValueError(f"Frontmatter field {key} cannot hold a line break.")
    _, rendered_body = split_session_text(render_page(frontmatter, body))
    frontmatter["body_sha256"] = session_body_sha256(rendered_body)
    text = render_page(frontmatter, body)
    if split_session_text(text)[1] != rendered_body:
        raise ValueError("Rendered session body changed after adding body_sha256.")
    return text


def write_new_file(path: Path, text: str) -> bool:
    """Write text to a dot temp file in the same folder, then hard link it to path.

    Returns False and leaves path untouched when path already exists. The temp file is always removed.
    Other OSErrors (permission, disk full, no hard link support) propagate.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, _default_file_mode())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def capture_session(
    request: CaptureRequest, env: Mapping[str, str] | None = None, now: datetime | None = None
) -> CaptureResult:
    """Run spec 4.2 steps 2-8 for one request. Writes at most one new file under raw/sessions/."""
    warnings: list[str] = []

    def skipped(message: str, manual_exit: int, vault: str | None = None) -> CaptureResult:
        return CaptureResult("skipped", message, manual_exit, vault=vault, warnings=tuple(warnings))

    selected = select_target_vault(request, env)
    if isinstance(selected, CaptureSkip):
        return skipped(selected.reason, selected.manual_exit)

    try:
        turns = read_turns(request.transcript_path, request.session_id, warnings=warnings)
    except OSError as exc:
        transcript = display_home_path(request.transcript_path, env)
        return skipped(f"Cannot read transcript {transcript}: {exc.strerror or exc}; nothing written.", 1)

    collected_at = (now or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cwd_text = display_home_path(request.cwd, env)
    archive = selected.path / SESSION_ARCHIVE_DIR / CAPTURE_AGENT
    part = 1
    for _ in range(_WRITE_ATTEMPTS):
        try:
            cursor = find_previous_part(selected.path, CAPTURE_AGENT, request.session_id, warnings=warnings)
            fresh = new_turns(turns, cursor, warnings=warnings)
        except CaptureStop as stop:
            return skipped(stop.message, stop.manual_exit, selected.name)
        if not fresh:
            return CaptureResult("no-new-turns", "no new turns", 0, vault=selected.name, warnings=tuple(warnings))

        part = cursor.part + 1 if cursor is not None else 1
        meta = SessionMeta(
            session_id=request.session_id,
            project=selected.project,
            cwd=cwd_text,
            part=part,
            collected_at=collected_at,
            trigger=request.trigger,
        )
        try:
            text = render_session_file(meta, fresh)
        except ValueError as exc:
            return skipped(f"{exc} Nothing written.", 1, selected.name)
        relative = (
            f"{SESSION_ARCHIVE_DIR}/{CAPTURE_AGENT}/{fresh[0].at.date().isoformat()}-{request.session_id}-part{part}.md"
        )
        if _part_taken(archive, request.session_id, part):
            continue
        try:
            written = write_new_file(selected.path / relative, text)
        except OSError as exc:
            return skipped(f"Cannot write {relative} in vault {selected.name}: {exc}; nothing written.", 1, selected.name)
        if not written:
            continue
        raw_frontmatter, _ = split_session_text(text)
        redactions = _parse_frontmatter(raw_frontmatter or "")[0].get("redactions", 0)
        return CaptureResult(
            "written",
            f"Wrote {relative} in vault {selected.name} ({len(fresh)} turns, {redactions} redactions).",
            0,
            vault=selected.name,
            path=relative,
            turn_count=len(fresh),
            redactions=redactions if isinstance(redactions, int) else 0,
            warnings=tuple(warnings),
        )
    return skipped(
        f"Part {part} of session {request.session_id} was created by another capture again after one retry; "
        "nothing written.",
        1,
        selected.name,
    )


def run_capture_command(
    values: Mapping[str, Any],
    stdin: TextIO | None,
    stdout: TextIO,
    stderr: TextIO,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """CLI body of `sessions capture`. Hook input (no --transcript) always returns 0 (spec decision M).

    With --normalized, hook JSON on stdin is optional; when present the run counts as hook input.
    """
    normalized = values.get("normalized")
    hook: dict[str, Any] | None = None
    if normalized:
        try:
            hook = _read_optional_hook_json(stdin)
        except CaptureStop as stop:
            hook = None
            result = CaptureResult("skipped", stop.message, stop.manual_exit)
            return _print_result(result, values, stdout, stderr, from_hook=True)
    from_hook = hook is not None if normalized else not values.get("transcript")
    try:
        if normalized:
            result = capture_normalized(values, hook, env, now)
        else:
            request = read_capture_request(values, stdin)
            result = capture_session(request, env, now)
    except CaptureStop as stop:
        result = CaptureResult("skipped", stop.message, stop.manual_exit)
    except Exception as exc:  # A hook must never block session end or compaction.
        if not from_hook:
            raise
        result = CaptureResult("skipped", f"sessions capture failed: {type(exc).__name__}: {exc}", 1)
    return _print_result(result, values, stdout, stderr, from_hook=from_hook)


def _print_result(
    result: CaptureResult, values: Mapping[str, Any], stdout: TextIO, stderr: TextIO, *, from_hook: bool
) -> int:
    for warning in result.warnings:
        print(warning, file=stderr)
    if values.get("json"):
        print(json.dumps(result.as_json(), ensure_ascii=False), file=stdout)
    if result.status == "skipped":
        print(result.message, file=stderr)
    elif not values.get("json"):
        print(result.message, file=stdout)
    return result.exit_code(from_hook)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Normalized session JSONL (vg-session-jsonl/1, vault-graph spec 4.2.2) and the vault-session/v2 body
# (vault-graph spec 4.2.5). The same input and the same RENDERER always give the same body bytes.

SOURCE_SCHEMA = "vg-session-jsonl/1"
RENDERER = "vg-session-md/2.0"
TITLE_CHARS = 60
CONTINUED_TITLE = "(이전 파트에서 이어짐)"
FAILED_MARK = "→ 실패"
TOOL_ONLY = "tool_only"
HUMAN = "human"

_HEADING_LINE_RE = re.compile(r"^( {0,3})(#)")
_META_LINE_RE = re.compile(r"^(\s*)<!--(\s*vg:)")
_SPACE_RE = re.compile(r"\s+")


class NormalizedInputError(ValueError):
    """The normalized file cannot be used. Nothing is written."""


@dataclass(frozen=True)
class NormTurn:
    turn_id: str
    exchange_id: str
    role: str
    turn_kind: str
    at: datetime
    text: str
    actions: tuple[dict[str, Any], ...]


@dataclass
class NormExchange:
    exchange_id: str
    seq: int
    agent_scope: str
    subagent_type: str | None
    started_at: datetime | None
    ended_at: datetime | None
    closed: bool
    tools: tuple[tuple[str, int], ...]
    files: tuple[str, ...]
    errors: int
    outcome: str
    turns: list[NormTurn] = field(default_factory=list)

    @property
    def is_subagent(self) -> bool:
        return self.agent_scope != "main"

    @property
    def agent_id(self) -> str:
        return self.agent_scope.split(":", 1)[1] if ":" in self.agent_scope else self.agent_scope


@dataclass(frozen=True)
class NormSession:
    session_id: str
    agent: str
    project: str | None
    cwd: str | None
    exchanges: tuple[NormExchange, ...]

    def turns(self) -> list[NormTurn]:
        return [turn for exchange in self.exchanges for turn in exchange.turns]


@dataclass(frozen=True)
class PartExchange:
    """One exchange as written in a part: the turns after the cursor, and whether earlier turns were left out."""

    exchange: NormExchange
    turns: tuple[NormTurn, ...]
    continued: bool


def read_normalized(
    path: Path, parse_at: Callable[[str], datetime], *, warnings: list[str] | None = None
) -> NormSession:
    """Read one normalized session file. Raises NormalizedInputError on any line that breaks the format."""
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NormalizedInputError(f"Normalized file is not UTF-8: {exc}") from None
    session: dict[str, Any] | None = None
    exchanges: list[NormExchange] = []
    skipped: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NormalizedInputError(f"Line {number} is not JSON ({exc.msg}).") from None
        if not isinstance(data, dict):
            raise NormalizedInputError(f"Line {number} is not a JSON object.")
        if data.get("schema") != SOURCE_SCHEMA:
            raise NormalizedInputError(f"Line {number} schema is {data.get('schema')!r}, not {SOURCE_SCHEMA}.")
        kind = data.get("kind")
        if session is None:
            if kind != "session":
                raise NormalizedInputError(f"Line {number}: the first line must be kind session, not {kind!r}.")
            session = data
            continue
        if kind == "session":
            raise NormalizedInputError(f"Line {number}: a second session line.")
        if kind == "exchange":
            exchanges.append(_exchange(data, number, parse_at))
        elif kind == "turn":
            if not exchanges:
                raise NormalizedInputError(f"Line {number}: a turn line before any exchange line.")
            turn = _turn(data, number, parse_at)
            if turn.exchange_id != exchanges[-1].exchange_id:
                raise NormalizedInputError(
                    f"Line {number}: turn {turn.turn_id} belongs to exchange {turn.exchange_id}, "
                    f"not to the exchange above it ({exchanges[-1].exchange_id})."
                )
            exchanges[-1].turns.append(turn)
        else:
            skipped[str(kind)] = skipped.get(str(kind), 0) + 1
    if session is None:
        raise NormalizedInputError("Normalized file is empty.")
    if skipped and warnings is not None:
        kinds = ", ".join(f"{name} {count}" for name, count in sorted(skipped.items()))
        warnings.append(f"Skipped normalized lines of unknown kind: {kinds}.")
    return NormSession(
        session_id=_required_text(session, "session_id", 1),
        agent=_required_text(session, "agent", 1),
        project=_optional_text(session.get("project")),
        cwd=_optional_text(session.get("cwd")),
        exchanges=tuple(exchanges),
    )


def _exchange(data: dict[str, Any], number: int, parse_at: Callable[[str], datetime]) -> NormExchange:
    closed = data.get("closed")
    if not isinstance(closed, bool):
        raise NormalizedInputError(f"Line {number}: exchange closed must be true or false.")
    tools_value = data.get("tools") or {}
    if not isinstance(tools_value, dict):
        raise NormalizedInputError(f"Line {number}: exchange tools must be an object.")
    tools = tuple(
        (str(name), count if isinstance(count, int) and not isinstance(count, bool) else 0)
        for name, count in sorted(tools_value.items())
    )
    files_value = data.get("files_touched") or []
    if not isinstance(files_value, list):
        raise NormalizedInputError(f"Line {number}: exchange files_touched must be a list.")
    errors = data.get("errors")
    return NormExchange(
        exchange_id=_required_text(data, "exchange_id", number),
        seq=data.get("seq") if isinstance(data.get("seq"), int) else 0,
        agent_scope=_required_text(data, "agent_scope", number),
        subagent_type=_optional_text(data.get("subagent_type")),
        started_at=_optional_at(data.get("started_at"), parse_at),
        ended_at=_optional_at(data.get("ended_at"), parse_at),
        closed=closed,
        tools=tools,
        files=tuple(str(item) for item in files_value if isinstance(item, str) and item),
        errors=errors if isinstance(errors, int) and not isinstance(errors, bool) else 0,
        outcome=_optional_text(data.get("outcome")) or "open",
    )


def _turn(data: dict[str, Any], number: int, parse_at: Callable[[str], datetime]) -> NormTurn:
    at_value = _required_text(data, "at", number)
    try:
        at = parse_at(at_value)
    except ValueError:
        raise NormalizedInputError(f"Line {number}: turn at {at_value!r} is not a time.") from None
    actions = data.get("actions") or []
    if not isinstance(actions, list):
        raise NormalizedInputError(f"Line {number}: turn actions must be a list.")
    text = data.get("text")
    return NormTurn(
        turn_id=_required_text(data, "turn_id", number),
        exchange_id=_required_text(data, "exchange_id", number),
        role=_required_text(data, "role", number),
        turn_kind=_required_text(data, "turn_kind", number),
        at=at,
        text=text if isinstance(text, str) else "",
        actions=tuple(item for item in actions if isinstance(item, dict)),
    )


def _required_text(data: dict[str, Any], key: str, number: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise NormalizedInputError(f"Line {number}: {data.get('kind')} field {key} must be a one-line string.")
    return value


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_at(value: Any, parse_at: Callable[[str], datetime]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_at(value)
    except ValueError:
        return None


# Cursor (spec 4.2.12)


def select_part_exchanges(
    session: NormSession,
    *,
    last_exchange_id: str | None,
    last_turn_uuid: str | None,
    last_turn_at: datetime | None,
    warnings: list[str] | None = None,
) -> list[PartExchange]:
    """Pick the exchanges for the next part.

    Start after the cursor: after the exchange last_exchange_id when it is in the input, else after
    the turn last_turn_uuid, else after the time last_turn_at (with a warning). No cursor means the
    whole input. Exchanges are taken in file order and the part stops before the first exchange that
    is not closed, so a later capture never skips an exchange that closes late.
    """
    flat = [(index, turn) for index, exchange in enumerate(session.exchanges) for turn in exchange.turns]
    keep: Callable[[int, int, NormTurn], bool]
    exchange_ids = [exchange.exchange_id for exchange in session.exchanges]
    if last_exchange_id is None and last_turn_uuid is None:
        keep = lambda exchange_index, position, turn: True  # noqa: E731
    elif last_exchange_id is not None and last_exchange_id in exchange_ids:
        after = exchange_ids.index(last_exchange_id)
        keep = lambda exchange_index, position, turn: exchange_index > after  # noqa: E731
    elif last_turn_uuid is not None and any(turn.turn_id == last_turn_uuid for _, turn in flat):
        cut = next(position for position, (_, turn) in enumerate(flat) if turn.turn_id == last_turn_uuid)
        keep = lambda exchange_index, position, turn: position > cut  # noqa: E731
    else:
        if last_turn_at is None:
            raise NormalizedInputError("The previous part has no usable cursor.")
        if warnings is not None:
            warnings.append(
                f"Previous last_turn_uuid {last_turn_uuid} is not in the normalized file; keeping turns later than "
                f"{last_turn_at.isoformat()}."
            )
        keep = lambda exchange_index, position, turn: turn.at > last_turn_at  # noqa: E731

    chosen: list[PartExchange] = []
    position = 0
    for index, exchange in enumerate(session.exchanges):
        written = []
        for turn in exchange.turns:
            if keep(index, position, turn):
                written.append(turn)
            position += 1
        if not written:
            continue
        if not exchange.closed:
            break
        chosen.append(PartExchange(exchange, tuple(written), continued=len(written) < len(exchange.turns)))
    return chosen


# Rendering (spec 4.2.5)


def render_v2_body(
    exchanges: Iterable[PartExchange],
    format_at: Callable[[datetime], str],
    redact: Callable[[str], tuple[str, int]],
    clean: Callable[[str], str],
) -> tuple[str, int]:
    """Render the part body and return it with the number of secrets replaced.

    Main exchanges come first in file order, then subagent exchanges at the end of the file.
    """
    items = list(exchanges)
    ordered = [item for item in items if not item.exchange.is_subagent] + [
        item for item in items if item.exchange.is_subagent
    ]
    redactions = 0

    def scrub(value: str) -> str:
        nonlocal redactions
        text, count = redact(value)
        redactions += count
        return text

    blocks = [_render_exchange(item, format_at, scrub, clean) for item in ordered]
    return "\n\n".join(blocks) + "\n", redactions


def _render_exchange(
    item: PartExchange,
    format_at: Callable[[datetime], str],
    scrub: Callable[[str], str],
    clean: Callable[[str], str],
) -> str:
    exchange = item.exchange
    turns = item.turns
    texts = {turn.turn_id: clean(scrub(turn.text)) if turn.text else "" for turn in turns}
    started = turns[0].at if item.continued else (exchange.started_at or turns[0].at)
    ended = exchange.ended_at or turns[-1].at
    short_id = exchange.exchange_id[:8]
    if exchange.is_subagent:
        heading = (
            f"## Subagent {_one_line(exchange.agent_id)} · {_one_line(exchange.subagent_type or 'unknown')} · "
            f"{format_at(started)} · {short_id}"
        )
    else:
        if item.continued:
            title = CONTINUED_TITLE
        else:
            human = next((turn for turn in turns if turn.turn_kind == HUMAN), None)
            title = _title(texts[human.turn_id]) if human is not None else ""
            title = title or "(no human text)"
        heading = f"## {title} {format_at(started)} · {short_id}"

    meta = [
        f"id={_value(exchange.exchange_id)}",
        f"scope={_value(exchange.agent_scope)}",
        f"started={format_at(started)}",
        f"ended={format_at(ended)}",
        "tools=" + ",".join(f"{_value(name, ':,')}:{count}" for name, count in exchange.tools),
        f"errors={exchange.errors}",
        f"outcome={_value(exchange.outcome)}",
        "files=" + ",".join(_value(scrub(name), ",") for name in exchange.files),
    ]
    if item.continued:
        meta.append("continued=1")
    lines = [heading, "<!-- vg:exchange " + " ".join(meta) + " -->"]

    blocks: list[list[str]] = []  # [heading, text, action lines...]
    for turn in turns:
        actions = [_action_line(action, scrub) for action in turn.actions]
        text = texts[turn.turn_id]
        if turn.turn_kind == TOOL_ONLY and not text and blocks:
            blocks[-1].extend(actions)
            continue
        role = "user" if turn.turn_kind == HUMAN else (
            "assistant" if turn.turn_kind in ("assistant_text", TOOL_ONLY) else _one_line(turn.turn_kind)
        )
        block = [f"### {role} · {format_at(turn.at)} · {turn.turn_id[:8]}", _escape(text) if text else ""]
        block.extend(actions)
        blocks.append(block)

    rendered = []
    for block in blocks:
        parts = [block[0]]
        if block[1]:
            parts.append(block[1])
        if len(block) > 2:
            parts.append("\n".join(block[2:]))
        rendered.append("\n\n".join(parts))
    return "\n".join(lines) + ("\n\n" + "\n\n".join(rendered) if rendered else "")


def _action_line(action: dict[str, Any], scrub: Callable[[str], str]) -> str:
    tool = _one_line(str(action.get("tool") or "unknown")) or "unknown"
    line = f"- {tool}"
    target = action.get("target")
    if isinstance(target, str) and target.strip():
        line += " " + _code(_one_line(scrub(target)))
    if action.get("ok") is False:
        head = action.get("error_head")
        line += f" {FAILED_MARK}"
        if isinstance(head, str) and head.strip():
            line += ": " + _one_line(scrub(head))
    return line


def _title(text: str) -> str:
    collapsed = _one_line(text)
    return collapsed[:TITLE_CHARS].rstrip()


def _one_line(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


def _code(value: str) -> str:
    if "`" in value:
        return f"`` {value} ``"
    return f"`{value}`"


def _escape(text: str) -> str:
    """Keep turn text from looking like an exchange heading, turn heading or meta line."""
    lines = []
    for line in text.split("\n"):
        line = _HEADING_LINE_RE.sub(r"\1\\\2", line, count=1)
        line = _META_LINE_RE.sub(r"\1&lt;!--\2", line, count=1)
        lines.append(line)
    return "\n".join(lines)


def _value(value: str, extra_unsafe: str = "") -> str:
    """Percent-encode one meta line value so spaces, commas and '-->' cannot break the line."""
    safe = "".join(char for char in "/._-~:+@" if char not in extra_unsafe)
    return quote(value, safe=safe)


# Normalized capture (vault-graph request S10, spec 4.2.5 and 4.2.12)


def capture_normalized(
    values: Mapping[str, Any],
    hook: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> CaptureResult:
    """Render the closed exchanges after the cursor of one normalized session file as a new v2 part.

    With rerender, ignore the cursor and write every closed exchange as a new part after the highest
    part. Existing files are never changed.
    """
    warnings: list[str] = []
    rerender = bool(values.get("rerender"))

    def skipped(message: str, manual_exit: int, vault: str | None = None) -> CaptureResult:
        return CaptureResult("skipped", message, manual_exit, vault=vault, warnings=tuple(warnings))

    source = Path(str(values["normalized"])).expanduser()
    trigger = values.get("trigger") or (hook or {}).get("hook_event_name") or "manual"
    if trigger not in TRIGGERS:
        return skipped(f"Trigger {trigger!r} is not one of {', '.join(TRIGGERS)}; nothing written.", 1)
    try:
        session = read_normalized(source, parse_turn_at, warnings=warnings)
    except OSError as exc:
        return skipped(
            f"Cannot read normalized file {display_home_path(source, env)}: {exc.strerror or exc}; nothing written.", 1
        )
    except NormalizedInputError as exc:
        return skipped(f"Normalized file {display_home_path(source, env)}: {exc} Nothing written.", 1)
    if session.agent not in SESSION_AGENTS:
        return skipped(f"Agent {session.agent!r} is not one of {', '.join(SESSION_AGENTS)}; nothing written.", 1)
    if not _SESSION_ID_RE.fullmatch(session.session_id):
        return skipped(
            f"Session id {session.session_id!r} has characters outside {SESSION_ID_PATTERN}; nothing written.", 1
        )

    cwd_value = values.get("cwd") or _text((hook or {}).get("cwd")) or session.cwd
    cwd = _expand_home(cwd_value, env) if cwd_value else Path.cwd()
    vault = values.get("vault")
    request = CaptureRequest(
        transcript_path=source,
        session_id=session.session_id,
        cwd=cwd,
        trigger=trigger,
        vault=Path(vault).expanduser() if vault else None,
        from_hook=hook is not None,
    )
    selected = select_target_vault(request, env)
    if isinstance(selected, CaptureSkip):
        return skipped(selected.reason, selected.manual_exit)

    collected_at = (now or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    archive = selected.path / SESSION_ARCHIVE_DIR / session.agent
    part = 1
    for _ in range(_WRITE_ATTEMPTS):
        try:
            if rerender:
                part = highest_part(selected.path, session.agent, session.session_id) + 1
                chosen = select_part_exchanges(
                    session, last_exchange_id=None, last_turn_uuid=None, last_turn_at=None, warnings=warnings
                )
            else:
                cursor = find_previous_part(selected.path, session.agent, session.session_id, warnings=warnings)
                part = cursor.part + 1 if cursor is not None else 1
                chosen = select_part_exchanges(
                    session,
                    last_exchange_id=cursor.last_exchange_id if cursor else None,
                    last_turn_uuid=cursor.last_turn_uuid if cursor else None,
                    last_turn_at=parse_turn_at(cursor.last_turn_at) if cursor else None,
                    warnings=warnings,
                )
        except CaptureStop as stop:
            return skipped(stop.message, stop.manual_exit, selected.name)
        except (NormalizedInputError, ValueError) as exc:
            return skipped(f"{exc} Nothing written.", 1, selected.name)
        if not chosen:
            return CaptureResult("no-new-turns", "no new turns", 0, vault=selected.name, warnings=tuple(warnings))

        first_turn = chosen[0].turns[0]
        last_turn = chosen[-1].turns[-1]
        turn_count = sum(len(item.turns) for item in chosen)
        body, redactions = render_v2_body(chosen, format_turn_at, redact, _clean_text)
        frontmatter: dict[str, Any] = {
            "schema": SESSION_SCHEMA_V2,
            "renderer": RENDERER,
            "source_schema": SOURCE_SCHEMA,
            "agent": session.agent,
            "session_id": session.session_id,
            "project": selected.project,
            "cwd": display_home_path(cwd, env),
            "part": part,
            "first_exchange_id": chosen[0].exchange.exchange_id,
            "last_exchange_id": chosen[-1].exchange.exchange_id,
            "first_turn_uuid": first_turn.turn_id,
            "first_turn_at": format_turn_at(first_turn.at),
            "last_turn_uuid": last_turn.turn_id,
            "last_turn_at": format_turn_at(last_turn.at),
            "exchange_count": len(chosen),
            "turn_count": turn_count,
            "collected_at": collected_at,
            "trigger": trigger,
            "redactions": redactions,
            "body_sha256": "",
        }
        try:
            text = _session_text(frontmatter, body)
        except ValueError as exc:
            return skipped(f"{exc} Nothing written.", 1, selected.name)
        relative = (
            f"{SESSION_ARCHIVE_DIR}/{session.agent}/{first_turn.at.date().isoformat()}-{session.session_id}-part{part}.md"
        )
        if _part_taken(archive, session.session_id, part, session.agent):
            continue
        try:
            written = write_new_file(selected.path / relative, text)
        except OSError as exc:
            return skipped(f"Cannot write {relative} in vault {selected.name}: {exc}; nothing written.", 1, selected.name)
        if not written:
            continue
        return CaptureResult(
            "written",
            f"Wrote {relative} in vault {selected.name} ({len(chosen)} exchanges, {turn_count} turns, "
            f"{redactions} redactions).",
            0,
            vault=selected.name,
            path=relative,
            turn_count=turn_count,
            redactions=redactions,
            warnings=tuple(warnings),
            extra=(("schema", SESSION_SCHEMA_V2), ("part", part), ("exchange_count", len(chosen))),
        )
    return skipped(
        f"Part {part} of session {session.session_id} was created by another capture again after one retry; "
        "nothing written.",
        1,
        selected.name,
    )


def _read_optional_hook_json(stdin: TextIO | None) -> dict[str, Any] | None:
    """Hook JSON on stdin, or None when stdin is a terminal or empty."""
    if stdin is None or (hasattr(stdin, "isatty") and stdin.isatty()):
        return None
    text = stdin.read()
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaptureStop(f"stdin is not JSON ({exc.msg}); nothing written.") from None
    if not isinstance(data, dict):
        raise CaptureStop("Hook JSON must be an object; nothing written.")
    return data


def _expand_home(value: str, env: Mapping[str, str] | None) -> Path:
    """Expand a leading $HOME or ~ (normalized files may record the home folder as $HOME)."""
    environ = os.environ if env is None else env
    home = environ.get("HOME") or str(Path.home())
    if value == "$HOME" or value.startswith("$HOME/"):
        value = home + value[len("$HOME") :]
    return Path(value).expanduser()


def display_home_path(path: Path, env: Mapping[str, str] | None = None) -> str:
    """Write a path under the user's home as $HOME/... so session files do not record the home path."""
    environ = os.environ if env is None else env
    home = environ.get("HOME") or str(Path.home())
    homes = {home.rstrip("/"), str(_resolve(Path(home))).rstrip("/")}
    for candidate in (str(Path(path).absolute()), str(_resolve(Path(path)))):
        for prefix in homes:
            if not prefix:
                continue
            if candidate == prefix:
                return "$HOME"
            if candidate.startswith(prefix + "/"):
                return "$HOME/" + candidate[len(prefix) + 1 :]
    return str(path)


def _read_hook_json(stdin: TextIO | None) -> dict[str, Any]:
    if stdin is None or (hasattr(stdin, "isatty") and stdin.isatty()):
        raise CaptureStop("No hook JSON on stdin and no --transcript; nothing written.")
    text = stdin.read()
    if not text.strip():
        raise CaptureStop("stdin is empty; pass hook JSON on stdin or use --transcript. Nothing written.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaptureStop(f"stdin is not JSON ({exc.msg}); nothing written.") from None
    if not isinstance(data, dict):
        raise CaptureStop("Hook JSON must be an object; nothing written.")
    return data


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _transcript_cwd(path: Path) -> Path | None:
    """The cwd of the first transcript entry that has one."""
    try:
        with Path(path).open("rb") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and _text(entry.get("cwd")):
                    return Path(entry["cwd"]).expanduser()
    except OSError as exc:
        raise CaptureStop(f"Cannot read transcript {path.name}: {exc.strerror or exc}; nothing written.") from None
    return None


def _contract_problem(vault_root: Path) -> str | None:
    """Read contract_version straight from the manifest JSON (approved finding 3), without manifest validation."""
    manifest = Path(vault_root) / MANIFEST_RELATIVE
    if not manifest.is_file():
        return f"has no {MANIFEST_RELATIVE.as_posix()}"
    try:
        data = json.loads(manifest.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return f"has an unreadable {MANIFEST_RELATIVE.as_posix()}"
    version = data.get("contract_version") if isinstance(data, dict) else None
    if type(version) is not int:
        return "manifest has no integer contract_version"
    if version < MIN_CONTRACT_VERSION:
        return f"manifest is contract {version}"
    return None


def _part_taken(archive: Path, session_id: str, part: int, agent: str = CAPTURE_AGENT) -> bool:
    """True when any file already holds this part number, whatever date its name carries."""
    if not archive.is_dir():
        return False
    for path in archive.iterdir():
        if path.name.startswith("."):
            continue
        parsed = parse_session_path(f"{SESSION_ARCHIVE_DIR}/{agent}/{path.name}")
        if parsed is not None and parsed.session_id == session_id and parsed.part == part:
            return True
    return False


def _default_file_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _resolve(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(path).expanduser().absolute()
