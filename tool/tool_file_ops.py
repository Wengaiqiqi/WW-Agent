from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import asdict, dataclass
from glob import glob
from pathlib import Path
from time import perf_counter


def json_result(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def workspace_root() -> Path:
    return Path(os.getenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", os.getcwd())).resolve()


def resolve_workspace_path(path: str, *, allow_missing: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace_root() / candidate
    # Use strict=False first to resolve the path even if it doesn't exist,
    # then check workspace boundaries before verifying existence.
    candidate = candidate.resolve(strict=False)
    root = workspace_root()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path is outside workspace: {candidate}") from exc
    if not allow_missing and not candidate.exists():
        raise FileNotFoundError(f"Path does not exist: {candidate}")
    return candidate


@dataclass
class TextFilePayload:
    filePath: str
    content: str
    numLines: int
    startLine: int
    totalLines: int


@dataclass
class ReadFileOutput:
    type: str
    file: TextFilePayload


@dataclass
class WriteFileOutput:
    type: str
    filePath: str
    content: str
    structuredPatch: list[str]
    originalFile: str | None


@dataclass
class EditFileOutput:
    filePath: str
    oldString: str
    newString: str
    originalFile: str
    structuredPatch: list[str]
    replaceAll: bool


def make_patch(original: str, updated: str, fromfile: str = "before", tofile: str = "after") -> list[str]:
    return list(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )


def read_text_file(path: str, offset: int = 0, limit: int | None = None) -> str:
    absolute = resolve_workspace_path(path)
    content = absolute.read_text(encoding="utf-8")
    lines = content.splitlines()
    start = max(0, min(offset, len(lines)))
    end = len(lines) if limit is None else min(len(lines), start + max(0, limit))
    selected = "\n".join(lines[start:end])
    output = ReadFileOutput(
        type="text",
        file=TextFilePayload(
            filePath=str(absolute),
            content=selected,
            numLines=end - start,
            startLine=start + 1,
            totalLines=len(lines),
        ),
    )
    return json_result(asdict(output))


def write_text_file(path: str, content: str) -> str:
    absolute = resolve_workspace_path(path, allow_missing=True)
    original = absolute.read_text(encoding="utf-8") if absolute.exists() else None
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_text(content, encoding="utf-8")
    output = WriteFileOutput(
        type="update" if original is not None else "create",
        filePath=str(absolute),
        content=content,
        structuredPatch=make_patch(original or "", content),
        originalFile=original,
    )
    return json_result(asdict(output))


def edit_text_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    absolute = resolve_workspace_path(path)
    original = absolute.read_text(encoding="utf-8")
    if old_string == new_string:
        raise ValueError("old_string and new_string must differ")
    if old_string not in original:
        raise ValueError("old_string not found in file")
    updated = original.replace(old_string, new_string) if replace_all else original.replace(old_string, new_string, 1)
    absolute.write_text(updated, encoding="utf-8")
    output = EditFileOutput(
        filePath=str(absolute),
        oldString=old_string,
        newString=new_string,
        originalFile=original,
        structuredPatch=make_patch(original, updated),
        replaceAll=replace_all,
    )
    return json_result(asdict(output))


def list_directory_structured(path: str = ".") -> str:
    absolute = resolve_workspace_path(path)
    dirs: list[dict[str, object]] = []
    files: list[dict[str, object]] = []
    for entry in sorted(absolute.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if entry.is_dir():
            dirs.append({"name": entry.name, "path": str(entry), "type": "directory"})
        else:
            files.append({"name": entry.name, "path": str(entry), "type": "file", "size": entry.stat().st_size})
    return json_result({"directory": str(absolute), "count": len(dirs) + len(files), "directories": dirs, "files": files})


def glob_search_files(pattern: str, path: str = ".") -> str:
    started = perf_counter()
    base = resolve_workspace_path(path)
    search_pattern = pattern if Path(pattern).is_absolute() else str(base / pattern)
    filenames = []
    for item in glob(search_pattern, recursive=True):
        resolved = resolve_workspace_path(item)
        if resolved.is_file():
            filenames.append(resolved)
    filenames.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    truncated = len(filenames) > 100
    payload = {
        "durationMs": int((perf_counter() - started) * 1000),
        "numFiles": min(len(filenames), 100),
        "filenames": [str(item) for item in filenames[:100]],
        "truncated": truncated,
    }
    return json_result(payload)


def grep_search_files(
    pattern: str,
    path: str = ".",
    glob_pattern: str | None = None,
    output_mode: str = "files_with_matches",
    context: int = 0,
    line_numbers: bool = True,
    case_insensitive: bool = False,
    head_limit: int = 250,
    offset: int = 0,
    multiline: bool = False,
) -> str:
    base = resolve_workspace_path(path)
    flags = re.IGNORECASE if case_insensitive else 0
    if multiline:
        flags |= re.DOTALL
    regex = re.compile(pattern, flags)

    # ``rglob`` transparently follows symlinks / Windows junctions, so a link
    # inside the workspace pointing outside it would otherwise let grep read
    # (and disclose) files beyond the sandbox. Resolve every candidate and keep
    # only those still under the workspace root — same boundary the read/write
    # wrappers enforce via ``resolve_workspace_path``.
    root = workspace_root()

    def _within_root(item: Path) -> bool:
        try:
            item.resolve(strict=False).relative_to(root)
            return True
        except (ValueError, OSError):
            return False

    if base.is_file():
        files = [base]
    else:
        files = [item for item in base.rglob("*") if item.is_file() and _within_root(item)]
    if glob_pattern:
        files = [item for item in files if item.match(glob_pattern)]

    filenames: list[str] = []
    content_lines: list[str] = []
    total_matches = 0
    skipped_binary = 0

    for file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            skipped_binary += 1
            continue
        matches = list(regex.finditer(text))
        if not matches:
            continue
        filenames.append(str(file_path))
        total_matches += len(matches)
        if output_mode == "content":
            lines = text.splitlines()
            matched_line_indexes = [idx for idx, line in enumerate(lines) if regex.search(line)]
            for idx in matched_line_indexes:
                start = max(0, idx - max(0, context))
                end = min(len(lines), idx + max(0, context) + 1)
                for current in range(start, end):
                    prefix = f"{file_path}:{current + 1}:" if line_numbers else f"{file_path}:"
                    content_lines.append(prefix + lines[current])

    def slice_items(items: list[str]) -> tuple[list[str], int | None, int | None]:
        sliced = items[max(0, offset) :]
        applied_limit = head_limit if len(sliced) > head_limit else None
        return sliced[:head_limit], applied_limit, offset if offset else None

    if output_mode == "content":
        lines, applied_limit, applied_offset = slice_items(content_lines)
        return json_result(
            {
                "mode": output_mode,
                "numFiles": len(filenames),
                "filenames": filenames,
                "content": "\n".join(lines),
                "numLines": len(lines),
                "appliedLimit": applied_limit,
                "appliedOffset": applied_offset,
                "skippedBinaryFiles": skipped_binary,
            }
        )
    if output_mode == "count":
        return json_result({
            "mode": output_mode,
            "numFiles": len(filenames),
            "filenames": filenames,
            "numMatches": total_matches,
            "skippedBinaryFiles": skipped_binary,
        })
    files_out, applied_limit, applied_offset = slice_items(filenames)
    return json_result(
        {
            "mode": output_mode,
            "numFiles": len(files_out),
            "filenames": files_out,
            "appliedLimit": applied_limit,
            "appliedOffset": applied_offset,
            "skippedBinaryFiles": skipped_binary,
        }
    )
