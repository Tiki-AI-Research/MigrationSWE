"""Validate unified diffs against real inputs, without invoking a patch tool.

Only presentation is flexible: timestamps, explicit single-line counts, hunk
headings and context sizes. Paths, line numbers, counts, original bytes and
resulting bytes are checked exactly, including missing final newlines. Shared
by P2P, F2P and the differential corpus so these paths cannot disagree.
"""

from __future__ import annotations

import difflib
import re


class DiffError(ValueError):
    pass


_HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: [^\n]*)?\n")
_NO_NEWLINE = b"\\ No newline at end of file\n"


def _bytes(value):
    return value.encode("utf-8") if isinstance(value, str) else value


def _lines(data):
    # Ruby source can contain CR, VT or other bytes: only LF delimits a line.
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _require(condition, reason):
    if not condition:
        raise DiffError(reason)


def validate_diff(output, changes):
    """Require one exact transformation per (header path, before, after) tuple.

    No filesystem reads or writes depend on candidate-supplied diff paths.
    Hunk offsets/fuzz are never tolerated. ``changes`` comes from the probe's
    known inputs, not from a candidate's formatting result.
    """
    lines = _lines(_bytes(output))
    index = 0
    pending = list(changes)
    while pending:
        _require(index < len(lines) and lines[index].startswith(b"--- "), "missing file header")
        header_path = lines[index][4:-1].split(b"\t", 1)[0]
        selected = next((i for i, change in enumerate(pending) if _bytes(change[0]) == header_path), None)
        _require(selected is not None, "wrong or duplicate diff path")
        path, before, after = pending.pop(selected)
        path, before, after = _bytes(path), _bytes(before), _bytes(after)
        _require(before != after, "a diff was requested for unchanged input")
        for prefix in (b"--- ", b"+++ "):
            _require(index < len(lines), "missing file header")
            line = lines[index]
            _require(line.startswith(prefix) and line.endswith(b"\n"), "invalid file header")
            _require(line[len(prefix):-1].split(b"\t", 1)[0] == path, "wrong diff path")
            index += 1
        source, result, cursor, hunks = _lines(before), [], 0, 0
        while index < len(lines) and lines[index].startswith(b"@@ "):
            match = _HUNK.fullmatch(lines[index])
            _require(match is not None, "invalid hunk header")
            try:
                old_start, old_count, new_start, new_count = (
                    int(match[1]), int(match[2]) if match[2] is not None else 1,
                    int(match[3]), int(match[4]) if match[4] is not None else 1,
                )
            except ValueError as exc:
                raise DiffError("invalid hunk number") from exc
            _require(old_count == 0 or old_start > 0, "invalid old hunk start")
            _require(new_count == 0 or new_start > 0, "invalid new hunk start")
            _require(old_count + new_count > 0, "empty hunk")
            old_index = old_start - 1 if old_count else old_start
            new_index = new_start - 1 if new_count else new_start
            _require(cursor <= old_index <= len(source), "old hunk range is outside input or overlaps")
            result.extend(source[cursor:old_index])
            cursor = old_index
            _require(new_index == len(result), "new hunk range disagrees with output position")
            index += 1
            old_seen = new_seen = 0
            while old_seen < old_count or new_seen < new_count:
                _require(index < len(lines), "truncated hunk")
                line = lines[index]
                _require(line[:1] in (b" ", b"-", b"+") and line.endswith(b"\n"), "invalid hunk line")
                prefix, content = line[:1], line[1:]
                index += 1
                if index < len(lines) and lines[index] == _NO_NEWLINE:
                    content = content[:-1]
                    index += 1
                if prefix != b"+":
                    _require(old_seen < old_count and cursor < len(source), "old hunk count exceeds input")
                    _require(source[cursor] == content, "context or deletion does not match input bytes")
                    cursor += 1
                    old_seen += 1
                if prefix != b"-":
                    _require(new_seen < new_count, "new hunk count exceeded")
                    result.append(content)
                    new_seen += 1
            hunks += 1
        _require(hunks > 0, "file diff contains no hunks")
        result.extend(source[cursor:])
        # Comparing lines also rejects a no-newline marker in the middle of
        # the new file, even if joining the malformed lines yields its bytes.
        _require(result == _lines(after), "diff does not produce required output bytes")
    _require(index == len(lines), "extra output or unexpected file diff")


def diff_matches(output, changes):
    try:
        validate_diff(output, changes)
    except DiffError:
        return False
    return True


def canonical_diff(output, changes):
    """Canonicalize only a validated transformation for differential checks.

    Invalid diffs stay visibly invalid, even when their text resembles a
    canonical diff. Existing source expectations remain byte-identical.
    """
    data = _bytes(output)
    changes = list(changes)
    try:
        validate_diff(data, changes)
    except DiffError:
        canonical = b"INVALID UNIFIED DIFF\n" + data
    else:
        chunks = []
        for path, before, after in changes:
            path = _bytes(path)
            for line in difflib.diff_bytes(difflib.unified_diff, _lines(_bytes(before)),
                                          _lines(_bytes(after)), fromfile=path, tofile=path):
                chunks.append(line if line.endswith(b"\n") else line + b"\n" + _NO_NEWLINE)
        canonical = b"".join(chunks)
    return canonical.decode("utf-8") if isinstance(output, str) else canonical
