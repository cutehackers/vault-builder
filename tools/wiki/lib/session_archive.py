from __future__ import annotations

import hashlib
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import _parse_frontmatter
from .schema import Issue

SESSION_ARCHIVE_DIR = "raw/sessions"
SESSION_SCHEMA = "vault-session/v1"
SESSION_AGENTS = ("claude-code",)
SESSION_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]*"
SESSION_PATH_RE = re.compile(
    r"^raw/sessions/(?P<agent>" + "|".join(re.escape(agent) for agent in SESSION_AGENTS) + r")/"
    r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<session_id>" + SESSION_ID_PATTERN + r")"
    r"-part(?P<part>[1-9][0-9]*)\.md$"
)
COLLECTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SESSION_FRONTMATTER_FIELDS = (
    "schema",
    "agent",
    "session_id",
    "project",
    "cwd",
    "part",
    "first_turn_uuid",
    "first_turn_at",
    "last_turn_uuid",
    "last_turn_at",
    "turn_count",
    "collected_at",
    "trigger",
    "redactions",
    "body_sha256",
)

INVALID_PATH = "session-file-invalid-path"
INVALID_FRONTMATTER = "session-file-invalid-frontmatter"
HASH_MISMATCH = "session-file-hash-mismatch"
PART_DUPLICATE = "session-part-duplicate"

_FRONTMATTER_CLOSE = "\n---\n"


@dataclass(frozen=True)
class SessionPath:
    agent: str
    date: str
    session_id: str
    part: int


@dataclass(frozen=True)
class SessionFileCheck:
    path: Path
    relative: str
    session_path: SessionPath | None
    frontmatter: dict[str, Any] | None
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def hash_mismatch(self) -> bool:
        return any(issue.code == HASH_MISMATCH for issue in self.issues)


@dataclass(frozen=True)
class SessionArchiveStatus:
    file_count: int
    last_collected_at: str | None
    failure_count: int
    hash_mismatch_count: int


def session_archive_root(root: Path) -> Path:
    return root / SESSION_ARCHIVE_DIR


def list_session_files(root: Path) -> list[Path]:
    archive = session_archive_root(root)
    if not archive.is_dir():
        return []
    return sorted(
        path
        for path in archive.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(archive).parts)
    )


def parse_session_path(relative: str) -> SessionPath | None:
    match = SESSION_PATH_RE.match(relative)
    if match is None:
        return None
    try:
        date.fromisoformat(match.group("date"))
    except ValueError:
        return None
    return SessionPath(
        agent=match.group("agent"),
        date=match.group("date"),
        session_id=match.group("session_id"),
        part=int(match.group("part")),
    )


def read_session_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def split_session_text(text: str) -> tuple[str | None, str]:
    """Split like parse_page: frontmatter between the opening marker and the first closing marker."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find(_FRONTMATTER_CLOSE, 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + len(_FRONTMATTER_CLOSE) :]


def session_body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def check_session_file(root: Path, path: Path) -> SessionFileCheck:
    relative = _relative(root, path)
    session_path = parse_session_path(relative)
    if session_path is None:
        issue = _issue(INVALID_PATH, path, f"Session archive file does not match {SESSION_ARCHIVE_DIR}/<agent>/<YYYY-MM-DD>-<session_id>-part<N>.md.")
        return SessionFileCheck(path, relative, None, None, (issue,))

    try:
        text = read_session_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        issue = _issue(INVALID_FRONTMATTER, path, f"Session file cannot be read as UTF-8: {exc}")
        return SessionFileCheck(path, relative, session_path, None, (issue,))

    raw_frontmatter, body = split_session_text(text)
    if raw_frontmatter is None:
        issue = _issue(INVALID_FRONTMATTER, path, "Session file is missing frontmatter.")
        return SessionFileCheck(path, relative, session_path, None, (issue,))

    frontmatter, parse_errors = _parse_frontmatter(raw_frontmatter)
    issues = [_issue(INVALID_FRONTMATTER, path, f"Frontmatter parse error: {error}") for error in parse_errors]
    missing = [field for field in SESSION_FRONTMATTER_FIELDS if field not in frontmatter]
    if missing:
        issues.append(_issue(INVALID_FRONTMATTER, path, f"Missing session fields: {', '.join(missing)}."))
    if "schema" in frontmatter and frontmatter["schema"] != SESSION_SCHEMA:
        issues.append(_issue(INVALID_FRONTMATTER, path, f"Unknown session schema: {frontmatter['schema']!r}."))
    if "session_id" in frontmatter and str(frontmatter["session_id"]) != session_path.session_id:
        issues.append(
            _issue(
                INVALID_FRONTMATTER,
                path,
                f"Frontmatter session_id {frontmatter['session_id']!r} differs from path session id {session_path.session_id!r}.",
            )
        )
    if "part" in frontmatter and frontmatter["part"] != session_path.part:
        issues.append(
            _issue(
                INVALID_FRONTMATTER,
                path,
                f"Frontmatter part {frontmatter['part']!r} differs from path part {session_path.part}.",
            )
        )

    expected_hash = frontmatter.get("body_sha256")
    if isinstance(expected_hash, str) and expected_hash:
        actual_hash = session_body_sha256(body)
        if actual_hash != expected_hash:
            issues.append(
                _issue(HASH_MISMATCH, path, f"Body SHA-256 {actual_hash} differs from body_sha256 {expected_hash}.")
            )
    elif "body_sha256" in frontmatter:
        issues.append(_issue(INVALID_FRONTMATTER, path, "body_sha256 is empty or not a string."))

    return SessionFileCheck(path, relative, session_path, frontmatter, tuple(issues))


def check_session_archive(root: Path) -> list[SessionFileCheck]:
    checks = [check_session_file(root, path) for path in list_session_files(root)]
    groups: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    for index, check in enumerate(checks):
        if check.session_path is not None:
            key = (check.session_path.agent, check.session_path.session_id, check.session_path.part)
            groups[key].append(index)

    for (agent, session_id, part), indexes in groups.items():
        if len(indexes) < 2:
            continue
        relatives = [checks[index].relative for index in indexes]
        for index in indexes:
            check = checks[index]
            others = ", ".join(relative for relative in relatives if relative != check.relative)
            issue = _issue(
                PART_DUPLICATE,
                check.path,
                f"Session {agent}/{session_id} part {part} also exists in: {others}.",
            )
            checks[index] = SessionFileCheck(
                check.path, check.relative, check.session_path, check.frontmatter, check.issues + (issue,)
            )
    return checks


def validate_session_archive(root: Path) -> list[Issue]:
    return [issue for check in check_session_archive(root) for issue in check.issues]


def session_archive_status(root: Path) -> SessionArchiveStatus:
    checks = check_session_archive(root)
    collected = [
        value
        for check in checks
        if check.frontmatter is not None
        for value in [check.frontmatter.get("collected_at")]
        if isinstance(value, str) and COLLECTED_AT_RE.match(value)
    ]
    return SessionArchiveStatus(
        file_count=len(checks),
        last_collected_at=max(collected) if collected else None,
        failure_count=sum(1 for check in checks if not check.ok),
        hash_mismatch_count=sum(1 for check in checks if check.hash_mismatch),
    )


@dataclass(frozen=True)
class SessionGitProblem:
    relative: str
    reason: str  # "missing" or "changed"


@dataclass(frozen=True)
class SessionGitCheck:
    checked_count: int
    problems: tuple[SessionGitProblem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def is_git_work_tree(root: Path) -> bool:
    """True when root is the top folder of a Git work tree, as release_gate.sh requires."""
    try:
        top = _run_git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.CalledProcessError):
        return False
    return Path(top.decode("utf-8").strip()).resolve() == root.resolve()


def count_uncommitted_session_files(root: Path) -> int | None:
    """Lines of `git status --porcelain --untracked-files=all -- raw/sessions`. None outside Git."""
    if not is_git_work_tree(root):
        return None
    try:
        output = _run_git(root, "status", "--porcelain", "--untracked-files=all", "--", SESSION_ARCHIVE_DIR)
    except (OSError, subprocess.CalledProcessError):
        return None
    return sum(1 for line in output.decode("utf-8", "replace").splitlines() if line.strip())


def first_added_session_blobs(root: Path) -> dict[str, str]:
    """Map each path ever added under raw/sessions/ in history reachable from HEAD to its first added blob id."""
    try:
        _run_git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    except subprocess.CalledProcessError:
        return {}  # No commits yet.
    output = _run_git(
        root,
        "log",
        "--reverse",
        "--diff-filter=A",
        "--no-renames",
        "--no-abbrev",
        "--raw",
        "-z",
        "--format=",
        "HEAD",
        "--",
        SESSION_ARCHIVE_DIR,
    )
    blobs: dict[str, str] = {}
    fields = output.split(b"\0")
    index = 0
    while index < len(fields):
        header = fields[index].strip(b"\n")
        if not header.startswith(b":"):
            index += 1
            continue
        # ":<old mode> <new mode> <old blob> <new blob> <status>" then the path in the next field.
        parts = header[1:].split()
        relative = fields[index + 1].decode("utf-8") if index + 1 < len(fields) else ""
        index += 2
        if len(parts) < 5 or not relative:
            continue
        blobs.setdefault(relative, parts[3].decode("ascii"))
    return blobs


def check_session_git_archive(root: Path) -> SessionGitCheck | None:
    """Compare work-tree bytes with the first committed blob of every session path. None outside Git."""
    if not is_git_work_tree(root):
        return None
    blobs = first_added_session_blobs(root)
    problems = []
    for relative in sorted(blobs):
        path = root / relative
        if not path.is_file():
            problems.append(SessionGitProblem(relative, "missing"))
        elif _git_blob_id(path.read_bytes(), len(blobs[relative])) != blobs[relative]:
            problems.append(SessionGitProblem(relative, "changed"))
    return SessionGitCheck(checked_count=len(blobs), problems=tuple(problems))


def _git_blob_id(data: bytes, id_length: int) -> str:
    algorithm = hashlib.sha256 if id_length == 64 else hashlib.sha1
    return algorithm(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _run_git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _issue(code: str, path: Path, message: str) -> Issue:
    return Issue(severity="error", code=code, path=str(path), message=message)
