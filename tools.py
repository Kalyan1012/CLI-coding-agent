"""Tools exposed to the LLM, plus workspace snapshot/restore helpers.

All file access is sandboxed to ./python_project (next to this file).
All line numbers are 1-based.
"""

from __future__ import annotations

import re
from pathlib import Path

from google.genai import types

WORKSPACE = Path(__file__).resolve().parent / "python_project"

CONTEXT_LINES = 3
MAX_FILE_BYTES = 1_000_000
MAX_SEARCH_MATCHES = 200

# Every successful replace_line is recorded here; the agent reads and clears it per attempt.
EDIT_LOG: list[dict] = []


class ToolError(Exception):
    """Raised for bad tool input. The message is sent back to the model."""


# --------------------------------------------------------------------------- helpers


def _workspace() -> Path:
    return WORKSPACE.resolve()


def _rel(path: Path) -> str:
    return path.relative_to(_workspace()).as_posix()


def _resolve(file_path: str) -> Path:
    """Map a model-supplied path to a real file inside the workspace."""
    if not isinstance(file_path, str) or not file_path.strip():
        raise ToolError("file_path must be a non-empty string.")
    workspace = _workspace()
    p = Path(file_path.strip().replace("\\", "/"))
    if p.is_absolute():
        target = p.resolve()
    else:
        parts = [part for part in p.parts if part != "."]
        if parts and parts[0] == workspace.name:  # accept "python_project/main.py"
            parts = parts[1:]
        target = workspace.joinpath(*parts).resolve()
    if workspace not in target.parents:
        raise ToolError(f"Path '{file_path}' is outside the project workspace.")
    if not target.is_file():
        raise ToolError(f"File not found: '{file_path}'. Use search_files to list valid paths.")
    return target


def _load(path: Path) -> tuple[list[str], bool]:
    """Return (lines, had_trailing_newline). Lines keep any trailing '\\r' so writes are lossless."""
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"File is too large to process ({len(data)} bytes).")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("File is not UTF-8 text.") from None
    if text == "":
        return [], False
    trailing = text.endswith("\n")
    lines = text.split("\n")
    if trailing:
        lines.pop()
    return lines, trailing


def _display(line: str) -> str:
    return line[:-1] if line.endswith("\r") else line


# --------------------------------------------------------------------------- the four tools


def search_files(query: str) -> list[str]:
    """Return workspace-relative paths whose name or contents match any keyword in `query`."""
    keywords = [k.lower() for k in str(query).split() if k]
    matches: list[str] = []
    for path in sorted(_workspace().rglob("*")):
        if not path.is_file():
            continue
        rel = _rel(path)
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        if not keywords or any(k in rel.lower() for k in keywords):
            matches.append(rel)
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
        if any(k in content for k in keywords):
            matches.append(rel)
    return matches


def search_in_file(file_path: str, keyword: str) -> list[dict]:
    """Case-insensitive search. Each hit has its line number, the line, and +/-3 lines of context."""
    path = _resolve(file_path)
    needle = str(keyword).lower()
    if not needle:
        raise ToolError("keyword must not be empty.")
    lines, _ = _load(path)
    results: list[dict] = []
    for number, line in enumerate(lines, start=1):
        if needle not in line.lower():
            continue
        start = max(1, number - CONTEXT_LINES)
        end = min(len(lines), number + CONTEXT_LINES)
        context = "\n".join(f"{n}: {_display(lines[n - 1])}" for n in range(start, end + 1))
        results.append({"line_number": number, "line": _display(line), "context": context})
        if len(results) >= MAX_SEARCH_MATCHES:
            break
    return results


def read_file(file_path: str) -> str:
    """Return the whole file with 1-based line numbers prefixed ('1: ...')."""
    path = _resolve(file_path)
    lines, _ = _load(path)
    return "\n".join(f"{n}: {_display(line)}" for n, line in enumerate(lines, start=1))


def replace_line(file_path: str, line_number: int, new_line: str) -> str:
    """Replace exactly one line. Original indentation is kept unless new_line starts with its own."""
    path = _resolve(file_path)
    try:
        if isinstance(line_number, float) and not line_number.is_integer():
            raise ValueError
        number = int(line_number)
    except (TypeError, ValueError):
        raise ToolError("line_number must be an integer.") from None

    new_line = str(new_line)
    if "\n" in new_line or "\r" in new_line:
        raise ToolError("new_line must be a single line (no newline characters).")

    lines, trailing = _load(path)
    if not 1 <= number <= len(lines):
        raise ToolError(f"line_number {number} is out of range (file has {len(lines)} lines).")

    original = lines[number - 1]
    carriage_return = "\r" if original.endswith("\r") else ""
    before = _display(original)
    indent = before[: len(before) - len(before.lstrip(" \t"))]

    if new_line.strip() == "":
        after = ""
    elif new_line[0] in (" ", "\t"):  # caller supplied explicit indentation
        after = new_line
    else:
        after = indent + new_line

    lines[number - 1] = after + carriage_return
    path.write_bytes(("\n".join(lines) + ("\n" if trailing else "")).encode("utf-8"))

    rel = _rel(path)
    EDIT_LOG.append({"file": rel, "line_number": number, "before": before, "after": after})
    print(f"    Before ({rel}:{number}): {before}")
    print(f"    After  ({rel}:{number}): {after}")
    return f"Replaced line {number} of {rel}.\nBefore: {before}\nAfter: {after}"


# --------------------------------------------------------------------------- snapshot / rollback


def snapshot_workspace() -> dict[str, bytes]:
    """Capture every file under the workspace (exact bytes) in memory."""
    workspace = _workspace()
    return {
        p.relative_to(workspace).as_posix(): p.read_bytes()
        for p in sorted(workspace.rglob("*"))
        if p.is_file()
    }


def restore_workspace(snapshot: dict[str, bytes]) -> None:
    """Make the workspace byte-for-byte identical to `snapshot`."""
    workspace = _workspace()
    for p in list(workspace.rglob("*")):
        if p.is_file() and p.relative_to(workspace).as_posix() not in snapshot:
            p.unlink()
    for rel, data in snapshot.items():
        p = workspace / rel
        if not p.is_file() or p.read_bytes() != data:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)


# --------------------------------------------------------------------------- Gemini declarations

_S = types.Schema
_T = types.Type

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_files",
        description=(
            "Recursively search the project workspace. Returns paths of files whose name or "
            "contents contain ANY whitespace-separated keyword in the query (case-insensitive). "
            "Use short, specific keywords such as a function name."
        ),
        parameters=_S(
            type=_T.OBJECT,
            properties={"query": _S(type=_T.STRING, description="Keywords to look for, e.g. 'factorial'.")},
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_in_file",
        description=(
            "Case-insensitive search inside one file. Returns each matching line with its exact "
            "1-based line number plus 3 lines of context before and after."
        ),
        parameters=_S(
            type=_T.OBJECT,
            properties={
                "file_path": _S(type=_T.STRING, description="Path from search_files, e.g. 'main.py'."),
                "keyword": _S(type=_T.STRING, description="Text to find within the file."),
            },
            required=["file_path", "keyword"],
        ),
    ),
    types.FunctionDeclaration(
        name="read_file",
        description="Return the full contents of a file with 1-based line numbers prefixed on every line.",
        parameters=_S(
            type=_T.OBJECT,
            properties={"file_path": _S(type=_T.STRING, description="Path from search_files, e.g. 'main.py'.")},
            required=["file_path"],
        ),
    ),
    types.FunctionDeclaration(
        name="replace_line",
        description=(
            "Replace ONLY the single line at line_number (1-based) with new_line. The original "
            "line's indentation is preserved automatically, so pass new_line without leading "
            "whitespace. Never adds or removes lines."
        ),
        parameters=_S(
            type=_T.OBJECT,
            properties={
                "file_path": _S(type=_T.STRING, description="Path of the file to edit."),
                "line_number": _S(type=_T.INTEGER, description="1-based number of the line to replace."),
                "new_line": _S(type=_T.STRING, description="The new content for that single line."),
            },
            required=["file_path", "line_number", "new_line"],
        ),
    ),
]

TOOL_FUNCTIONS = {
    "search_files": search_files,
    "search_in_file": search_in_file,
    "read_file": read_file,
    "replace_line": replace_line,
}
