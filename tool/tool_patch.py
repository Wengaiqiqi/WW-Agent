"""
V4A patch tool — batch multi-file edits via a unified-diff-like format.

Format (same as hermes-agent / codex / cline):

    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     context line
    -removed line
    +added line
    *** Add File: path/to/new.py
    +line 1
    +line 2
    *** Delete File: path/to/old.py
    *** Move File: old/path.py -> new/path.py
    *** End Patch

Compared to ``hermes-agent/tools/patch_parser.py`` this port:
- uses *exact* string matching for hunks (no fuzzy matcher dependency);
- drops the lint and PatchResult abstractions in favor of a plain dict result;
- uses ``tool_file_ops.resolve_workspace_path`` for workspace-boundary safety.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from tool.tool_file_ops import resolve_workspace_path


class OperationType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    prefix: str  # " ", "-", or "+"
    content: str


@dataclass
class Hunk:
    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    operation: OperationType
    file_path: str
    new_path: Optional[str] = None  # MOVE only
    hunks: List[Hunk] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def parse_v4a_patch(patch_content: str) -> Tuple[List[PatchOperation], Optional[str]]:
    """Parse V4A patch text. Returns ``(operations, error_or_None)``."""
    lines = patch_content.split("\n")

    start_idx = -1
    end_idx = len(lines)
    for i, line in enumerate(lines):
        if "*** Begin Patch" in line or "***Begin Patch" in line:
            start_idx = i
        elif "*** End Patch" in line or "***End Patch" in line:
            end_idx = i
            break

    operations: List[PatchOperation] = []
    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None

    def flush_current() -> None:
        nonlocal current_op, current_hunk
        if current_op is None:
            return
        if current_hunk and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)
        current_op = None
        current_hunk = None

    i = start_idx + 1
    while i < end_idx:
        line = lines[i]
        i += 1

        update_match = re.match(r"\*\*\*\s*Update\s+File:\s*(.+)", line)
        add_match = re.match(r"\*\*\*\s*Add\s+File:\s*(.+)", line)
        delete_match = re.match(r"\*\*\*\s*Delete\s+File:\s*(.+)", line)
        move_match = re.match(r"\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)", line)

        if update_match:
            flush_current()
            current_op = PatchOperation(OperationType.UPDATE, update_match.group(1).strip())
            continue
        if add_match:
            flush_current()
            current_op = PatchOperation(OperationType.ADD, add_match.group(1).strip())
            current_hunk = Hunk()
            continue
        if delete_match:
            flush_current()
            operations.append(PatchOperation(OperationType.DELETE, delete_match.group(1).strip()))
            continue
        if move_match:
            flush_current()
            operations.append(PatchOperation(
                OperationType.MOVE,
                move_match.group(1).strip(),
                new_path=move_match.group(2).strip(),
            ))
            continue
        if line.startswith("@@"):
            if current_op is not None:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                hint_match = re.match(r"@@\s*(.+?)\s*@@", line)
                hint = hint_match.group(1).strip() if hint_match else None
                current_hunk = Hunk(context_hint=hint)
            continue

        if current_op is None or not line:
            continue

        if current_hunk is None:
            current_hunk = Hunk()

        if line.startswith("+"):
            current_hunk.lines.append(HunkLine("+", line[1:]))
        elif line.startswith("-"):
            current_hunk.lines.append(HunkLine("-", line[1:]))
        elif line.startswith(" "):
            current_hunk.lines.append(HunkLine(" ", line[1:]))
        elif line.startswith("\\"):
            # "\ No newline at end of file" marker — skip.
            pass
        else:
            # Treat unprefixed lines as context.
            current_hunk.lines.append(HunkLine(" ", line))

    flush_current()

    errors: List[str] = []
    for op in operations:
        if not op.file_path:
            errors.append("Operation with empty file path")
        if op.operation == OperationType.UPDATE and not op.hunks:
            errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == OperationType.MOVE and not op.new_path:
            errors.append(f"MOVE {op.file_path!r}: missing destination path (expected 'src -> dst')")
    if errors:
        return [], "Parse error: " + "; ".join(errors)
    return operations, None


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def _read_text(path_str: str) -> Tuple[str, Optional[str]]:
    try:
        resolved = resolve_workspace_path(path_str)
    except FileNotFoundError as exc:
        return "", str(exc)
    except PermissionError as exc:
        return "", str(exc)
    try:
        return resolved.read_text(encoding="utf-8"), None
    except OSError as exc:
        return "", f"read failed: {exc}"


def _validate_operations(operations: List[PatchOperation]) -> List[str]:
    """Phase-1 validation. Simulates UPDATE/DELETE/MOVE without writing."""
    errors: List[str] = []
    for op in operations:
        if op.operation == OperationType.ADD:
            try:
                resolve_workspace_path(op.file_path, allow_missing=True)
            except PermissionError as exc:
                errors.append(f"{op.file_path}: {exc}")
            continue

        if op.operation == OperationType.DELETE:
            content, err = _read_text(op.file_path)
            if err:
                errors.append(f"{op.file_path}: file not found for deletion ({err})")
            continue

        if op.operation == OperationType.MOVE:
            src, err = _read_text(op.file_path)
            if err:
                errors.append(f"{op.file_path}: source not found for move ({err})")
                continue
            try:
                dst_resolved = resolve_workspace_path(op.new_path or "", allow_missing=True)
                if dst_resolved.exists():
                    errors.append(f"{op.new_path}: destination already exists — move would overwrite")
            except PermissionError as exc:
                errors.append(f"{op.new_path}: {exc}")
            continue

        # UPDATE
        current, err = _read_text(op.file_path)
        if err:
            errors.append(f"{op.file_path}: {err}")
            continue
        simulated = current
        for hunk_idx, hunk in enumerate(op.hunks):
            new, count, err = _apply_hunk(simulated, hunk)
            if err:
                label = f"hunk #{hunk_idx + 1}"
                if hunk.context_hint:
                    label += f" '{hunk.context_hint}'"
                errors.append(f"{op.file_path}: {label} — {err}")
                break
            simulated = new
    return errors


def _apply_hunk(content: str, hunk: Hunk) -> Tuple[str, int, Optional[str]]:
    """Apply a single hunk. Returns ``(new_content, count, error_or_None)``."""
    search_lines = [l.content for l in hunk.lines if l.prefix in {" ", "-"}]
    replace_lines = [l.content for l in hunk.lines if l.prefix in {" ", "+"}]
    insert_text = "\n".join(replace_lines)
    search_pattern = "\n".join(search_lines)

    if not search_lines:
        # Addition-only hunk.
        if hunk.context_hint:
            occurrences = content.count(hunk.context_hint)
            if occurrences == 0:
                return content, 0, f"context hint not found: {hunk.context_hint!r}"
            if occurrences > 1:
                return content, 0, (
                    f"context hint '{hunk.context_hint}' is ambiguous ({occurrences} occurrences)"
                )
            hint_pos = content.find(hunk.context_hint)
            eol = content.find("\n", hint_pos)
            if eol == -1:
                return content + "\n" + insert_text, 1, None
            return content[: eol + 1] + insert_text + "\n" + content[eol + 1 :], 1, None
        return content.rstrip("\n") + "\n" + insert_text + "\n", 1, None

    occurrences = content.count(search_pattern)
    if occurrences == 0:
        return content, 0, "search block not found in file"
    if occurrences > 1:
        return content, 0, (
            f"search block matches {occurrences} locations; tighten context or add @@ hint"
        )
    return content.replace(search_pattern, insert_text, 1), 1, None


def _diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def apply_v4a_patch(patch_content: str) -> dict:
    """Parse and apply a V4A patch. Returns a structured result dict."""
    operations, parse_err = parse_v4a_patch(patch_content)
    if parse_err:
        return {"success": False, "error": parse_err}
    if not operations:
        return {"success": False, "error": "Empty patch: no operations found"}

    validation_errors = _validate_operations(operations)
    if validation_errors:
        return {
            "success": False,
            "error": "Patch validation failed (no files were modified)",
            "details": validation_errors,
        }

    files_modified: List[str] = []
    files_created: List[str] = []
    files_deleted: List[str] = []
    moves: List[str] = []
    diffs: List[str] = []
    apply_errors: List[str] = []

    for op in operations:
        try:
            if op.operation == OperationType.ADD:
                content_lines = [
                    line.content
                    for hunk in op.hunks
                    for line in hunk.lines
                    if line.prefix == "+"
                ]
                body = "\n".join(content_lines)
                target = resolve_workspace_path(op.file_path, allow_missing=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
                files_created.append(op.file_path)
                diffs.append(_diff("", body, op.file_path))

            elif op.operation == OperationType.DELETE:
                target = resolve_workspace_path(op.file_path)
                old = target.read_text(encoding="utf-8")
                target.unlink()
                files_deleted.append(op.file_path)
                diffs.append(_diff(old, "", op.file_path))

            elif op.operation == OperationType.MOVE:
                src = resolve_workspace_path(op.file_path)
                dst = resolve_workspace_path(op.new_path or "", allow_missing=True)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moves.append(f"{op.file_path} -> {op.new_path}")
                diffs.append(f"# Moved: {op.file_path} -> {op.new_path}\n")

            elif op.operation == OperationType.UPDATE:
                target = resolve_workspace_path(op.file_path)
                old = target.read_text(encoding="utf-8")
                new = old
                for hunk in op.hunks:
                    candidate, count, err = _apply_hunk(new, hunk)
                    if err:
                        raise RuntimeError(err)
                    new = candidate
                target.write_text(new, encoding="utf-8")
                files_modified.append(op.file_path)
                diffs.append(_diff(old, new, op.file_path))

        except Exception as exc:
            apply_errors.append(f"{op.file_path}: {exc}")

    result = {
        "success": not apply_errors,
        "filesModified": files_modified,
        "filesCreated": files_created,
        "filesDeleted": files_deleted,
        "filesMoved": moves,
        "diff": "\n".join(d for d in diffs if d),
    }
    if apply_errors:
        result["error"] = "Apply phase failed (state may be inconsistent — run `git diff` to assess)"
        result["details"] = apply_errors
    return result


def apply_patch_tool(patch: str) -> str:
    """JSON-string entry point used by the LangChain tool wrapper."""
    return json.dumps(apply_v4a_patch(patch), ensure_ascii=False, indent=2)
