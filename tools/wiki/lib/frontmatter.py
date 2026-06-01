from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Page:
    path: Path
    frontmatter: dict[str, Any] | None
    body: str
    parse_errors: tuple[str, ...] = ()


def parse_page(path: Path | str) -> Page:
    page_path = Path(path)
    text = page_path.read_text()
    if not text.startswith("---\n"):
        return Page(path=page_path, frontmatter=None, body=text)

    end = text.find("\n---\n", 4)
    if end == -1:
        return Page(
            path=page_path,
            frontmatter=None,
            body=text,
            parse_errors=("frontmatter closing marker not found",),
        )

    raw_frontmatter = text[4:end]
    body = text[end + len("\n---\n") :]
    frontmatter, errors = _parse_frontmatter(raw_frontmatter)
    return Page(path=page_path, frontmatter=frontmatter, body=body, parse_errors=tuple(errors))


def render_page(frontmatter: dict[str, Any], body: str) -> str:
    content = ["---"]
    content.extend(_render_mapping(frontmatter, 0))
    content.append("---")
    content.append("")
    content.append(body.rstrip())
    content.append("")
    return "\n".join(content)


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], list[str]]:
    root: dict[str, Any] = {}
    errors: list[str] = []
    lines = raw.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(" "):
            index += 1
            continue

        split = _split_key_value(line)
        if split is None:
            errors.append(f"line {index + 1}: expected key: value")
            index += 1
            continue
        key, value = split
        if value == "":
            values, index, block_errors = _parse_block_value(lines, index + 1)
            errors.extend(block_errors)
            root[key] = values
            continue

        root[key] = _parse_scalar(value)
        index += 1

    return root, errors


def _parse_block_value(lines: list[str], start: int) -> tuple[Any, int, list[str]]:
    mapping: dict[str, Any] = {}
    sequence: list[Any] = []
    errors: list[str] = []
    saw_mapping = False
    saw_sequence = False
    index = start

    while index < len(lines):
        line = lines[index]
        if not line.startswith("  "):
            break
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("- "):
            saw_sequence = True
            sequence.append(_parse_scalar(stripped[2:].strip()))
        elif ":" in stripped:
            saw_mapping = True
            split = _split_key_value(stripped)
            if split is None:
                errors.append(f"line {index + 1}: expected key: value")
                index += 1
                continue
            key, value = split
            mapping[key] = _parse_scalar(value)
        else:
            errors.append(f"line {index + 1}: expected list item or key: value")
        index += 1

    if saw_mapping and not saw_sequence:
        return mapping, index, errors
    if saw_sequence and not saw_mapping:
        return sequence, index, errors
    return {}, index, errors


def _split_key_value(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value.startswith(("\"", "'"))
    ):
        return value[1:-1]
    return value


def _render_mapping(mapping: dict[str, Any], indent: int) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in mapping.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.extend(_render_mapping(value, indent + 2))
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}{key}: []")
            else:
                lines.append(f"{prefix}{key}:")
                for item in value:
                    lines.append(f"{prefix}  - {_render_scalar(item)}")
        else:
            lines.append(f"{prefix}{key}: {_render_scalar(value)}")
    return lines


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return '""'
    text = str(value)
    if text == "":
        return '""'
    if _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _needs_quotes(text: str) -> bool:
    special = [":", "#", "[", "]", "{", "}", ",", "&", "*", "!", "|", ">", "%", "@"]
    return (
        text != text.strip()
        or any(char in text for char in special)
        or text.lower() in {"true", "false", "null", "none"}
        or text[:1].isdigit()
    )
