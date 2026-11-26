"""Regression guard: the public-contract probe must pass a declared ``runtime.Env``.

The r8 harbor report ``QGLmm8J`` failed the verifier-owned contract probe with a
nil-pointer panic at ``analytics.go:90`` (``env.Now()``).  The probe's
``TestAnalyticsQueueSurface`` built the queue with ``analytics.New(nil, ...)``,
so a candidate that consumes its ``env`` argument like the real engine (the
runtime always injects a live ``Env``) crashed inside the verifier itself.
That is a hidden nil-env requirement: the instruction declares a valid
environment at ``instruction.md`` lines 163-165 --

    func New() *Env; type Env struct {...}; Env implements runtime.Env

so the probe MUST construct the analytics surface with ``testenv.New()``
(the declared ``internal/testenv`` constructor), never a ``nil`` env.

This self-check parses ``tests/verifier/harbor_entry.py`` independently of the
verifier runtime and pins:

* the embedded ``_CONTRACT_PROBE_GO`` imports the declared
  ``internal/testenv`` package and constructs ``analytics.New(testenv.New(), ...)``
  with the same ``sendFn`` closure the test relied on;
* ``nil`` is never handed to ``analytics.New``;
* the probe still asserts the empty-authKey no-queue behavior, the queue length
  after a real emit, and the ``Flush`` batch delivery -- checked from the parsed
  call arguments and comparisons, not from one frozen literal line;
* the Go probe source stays structurally balanced (no truncation), and the
  harness still writes it as ``probe_test.go``.

No ``_CONTRACT_PROBE_GO`` text is executed and no Go toolchain is required; if a
``go``/``gofmt`` binary is unavailable the check degrades to the structural
balance assertion rather than silently passing.

Run it from anywhere::

    PYTHONDONTWRITEBYTECODE=1 python3 -B environment/verifier_contract_env_selfcheck.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_TASK_ROOT = Path(__file__).resolve().parents[1]
_ENTRY = _TASK_ROOT / "tests" / "verifier" / "harbor_entry.py"
_ANALYTICS_TEST = "TestAnalyticsQueueSurface"
_DECLARED_ENV = re.compile(r"testenv\.New\(\)")

_GO_STRING = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`', re.DOTALL)


def _check(name: str, condition: object, detail: object = "") -> None:
    if not condition:
        raise RuntimeError(f"{name}: {detail}")


def _extract_probe(entry_source: str) -> str:
    tree = ast.parse(entry_source, filename=str(_ENTRY))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_CONTRACT_PROBE_GO"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            _check("probe.is-string", isinstance(value, str), type(value))
            return value
    raise RuntimeError("_CONTRACT_PROBE_GO: assignment not found")


def _function_body(source: str, name: str) -> str:
    marker = f"\nfunc {name}("
    start = source.find(marker)
    _check(f"go.function.{name}.present", start != -1, f"{name} not found in probe")
    start += 1  # drop the leading newline
    following = source.find("\nfunc ", start + 1)
    return source[start:] if following == -1 else source[start:following]


def _strip_go_strings(text: str) -> str:
    return _GO_STRING.sub('""', text)


def _split_top_level_args(args_text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    in_string: str | None = None
    index = 0
    while index < len(args_text):
        char = args_text[index]
        if in_string is not None:
            current.append(char)
            if char == "\\" and index + 1 < len(args_text):
                current.append(args_text[index + 1])
                index += 2
                continue
            if char == in_string:
                in_string = None
            index += 1
            continue
        if char in ('"', "`"):
            in_string = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    trailing = "".join(current).strip()
    if trailing:
        args.append(trailing)
    return args


def _find_calls(text: str, callee: str) -> list[tuple[list[str], int]]:
    """Return ``(top_level_args, match_start)`` for every ``callee(`` call."""

    calls: list[tuple[list[str], int]] = []
    for match in re.finditer(re.escape(callee) + r"\(", text):
        cursor = match.end()  # just past the opening paren
        depth = 1
        in_string: str | None = None
        while cursor < len(text) and depth > 0:
            char = text[cursor]
            if in_string is not None:
                if char == "\\":
                    cursor += 2
                    continue
                if char == in_string:
                    in_string = None
            elif char in ('"', "`"):
                in_string = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        calls.append((_split_top_level_args(text[match.end():cursor - 1]), match.start()))
    return calls


def _check_wiring(entry_source: str) -> None:
    _check(
        "wiring.writes-probe",
        re.search(r"write_text\(\s*_CONTRACT_PROBE_GO", entry_source) is not None
        and "probe_test.go" in entry_source,
        "harness no longer writes _CONTRACT_PROBE_GO as probe_test.go",
    )
    _check(
        "wiring.runner-present",
        "def _run_contract_probe(" in entry_source,
        "contract probe runner missing",
    )


def _check_construction(body: str, probe: str) -> None:
    constructors = _find_calls(body, "analytics.New")
    _check("probe.analytics.new.count", len(constructors) == 1, constructors)
    args, _ = constructors[0]
    _check("probe.analytics.new.arity", len(args) >= 2, args)
    env_arg = args[0]
    _check("probe.analytics.env.not-nil", env_arg != "nil", env_arg)
    _check(
        "probe.analytics.env.declared",
        _DECLARED_ENV.fullmatch(env_arg) is not None,
        env_arg,
    )
    _check(
        "probe.imports-declared-testenv",
        re.search(r'"[^"]*internal/testenv"', probe) is not None,
        "internal/testenv import missing from probe",
    )
    _check(
        "probe.imports-runtime",
        '"github.com/stremio/stremio-core/runtime"' in probe,
        "runtime import missing from probe",
    )
    send_fn = args[1]
    _check(
        "probe.analytics.sendfn.signature",
        send_fn.startswith("func(b analytics.Batch) error"),
        send_fn,
    )
    _check(
        "probe.analytics.sendfn.captures-sent",
        "sent = append(sent, b)" in send_fn,
        send_fn,
    )
    _check(
        "probe.analytics.no-nil-anywhere",
        re.search(r"analytics\.New\(\s*nil\b", probe) is None,
        "nil env passed to analytics.New",
    )


def _check_queue_semantics(body: str) -> None:
    emits = _find_calls(body, "a.Emit")
    _check("probe.emit.count", len(emits) == 2, emits)
    empty = [(args, pos) for args, pos in emits if len(args) >= 3 and args[2] == '""']
    nonempty = [(args, pos) for args, pos in emits if len(args) >= 3 and args[2] not in ('""', "")]
    _check("probe.emit.empty-authkey", len(empty) == 1, emits)
    _check("probe.emit.nonempty-authkey", len(nonempty) == 1, emits)

    comparisons = list(re.finditer(r"got := a\.QueueLen\(\);\s*got\s*!=\s*(\d+)", body))
    _check("probe.queue.comparisons", len(comparisons) == 2, comparisons)
    _check("probe.queue.zero-when-empty", comparisons[0].group(1) == "0", comparisons[0].group(0))
    _check("probe.queue.one-after-emit", comparisons[1].group(1) == "1", comparisons[1].group(0))
    _check(
        "probe.queue.order",
        empty[0][1] < comparisons[0].start() < nonempty[0][1] < comparisons[1].start(),
        "empty/no-queue check must precede the real emit/queue check",
    )
    _check("probe.queue.fatalf", body.count("t.Fatalf") >= 2, body.count("t.Fatalf"))


def _check_flush_semantics(body: str) -> None:
    flushes = _find_calls(body, "a.Flush")
    _check("probe.flush.present", len(flushes) == 1, flushes)
    _check(
        "probe.flush.asserts-batch-count",
        re.search(r"len\(sent\)\s*!=\s*1", body) is not None,
        "Flush must assert one delivered batch",
    )
    _check(
        "probe.flush.asserts-batch-events",
        re.search(r"sent\[0\]\.Events", body) is not None,
        "Flush must inspect the delivered batch events",
    )
    _check(
        "probe.flush.asserts-event-name",
        re.search(r'\.Name\s*!=\s*"evt"', body) is not None,
        "Flush must keep the event-name assertion",
    )


def _check_balanced(name: str, text: str) -> None:
    stripped = _strip_go_strings(text)
    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        opened = stripped.count(opener)
        closed = stripped.count(closer)
        _check(
            f"go.balance.{name}.{opener}",
            opened == closed,
            f"{opener}={opened} {closer}={closed}",
        )


def _check_source(probe: str) -> None:
    _check(
        "probe.test.present",
        f"func {_ANALYTICS_TEST}(t *testing.T)" in probe,
        f"{_ANALYTICS_TEST} missing",
    )
    body = _function_body(probe, _ANALYTICS_TEST)
    _check_balanced(f"function.{_ANALYTICS_TEST}", body)
    _check_construction(body, probe)
    _check_queue_semantics(body)
    _check_flush_semantics(body)


def main() -> int:
    _check("entry.exists", _ENTRY.is_file(), _ENTRY)
    entry_source = _ENTRY.read_text(encoding="utf-8")
    _check("entry.nonempty", bool(entry_source.strip()), "harbor_entry.py is empty")
    probe = _extract_probe(entry_source)
    _check("probe.nonempty", bool(probe.strip()), "embedded Go probe is empty")
    _check_wiring(entry_source)
    _check_balanced("probe", probe)
    _check_source(probe)
    print("verifier_contract_env_selfcheck: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
