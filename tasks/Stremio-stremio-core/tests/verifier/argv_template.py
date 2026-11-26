"""Runtime argv placeholders shared by authoring, replay and the shipped verifier."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


_ALLOWED = frozenset({"repo", "assets", "report"})
_PLACEHOLDER = re.compile(
    r"^(?P<prefix>--[A-Za-z][A-Za-z0-9_-]*=)?"
    r"\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"(?P<suffix>/[^{}\s]*)?$"
)
_PATH_START = re.compile(r"^(?:--[A-Za-z][A-Za-z0-9_-]*=)?\{[A-Za-z_]")
_ANGLE_PLACEHOLDER = re.compile(r"<[A-Za-z_][A-Za-z0-9_.-]*>")


def runtime_arg_placeholder(item: str, field: str) -> str | None:
    if _ANGLE_PLACEHOLDER.search(item):
        raise ValueError(f"runtime contract {field} contains an unresolved angle placeholder")
    match = _PLACEHOLDER.fullmatch(item)
    if match is None:
        if _PATH_START.match(item):
            raise ValueError(f"runtime contract {field} contains an invalid placeholder")
        return None
    name = match.group("name")
    if name not in _ALLOWED:
        raise ValueError(f"runtime contract {field} contains unsupported placeholder: {name}")
    suffix = match.group("suffix")
    if suffix and any(segment in {".", ".."} for segment in suffix.split("/")):
        raise ValueError(f"runtime contract {field} contains an unsafe placeholder path")
    return name


def render_runtime_argv(command: Sequence[str], values: Mapping[str, str]) -> tuple[str, ...]:
    rendered: list[str] = []
    for item in command:
        name = runtime_arg_placeholder(item, "argv")
        if name is None:
            rendered.append(item)
            continue
        match = _PLACEHOLDER.fullmatch(item)
        assert match is not None
        rendered.append(
            (match.group("prefix") or "") + values[name] + (match.group("suffix") or "")
        )
    return tuple(rendered)
