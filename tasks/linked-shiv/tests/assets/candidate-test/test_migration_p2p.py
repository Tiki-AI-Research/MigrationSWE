"""Tests for Click migration public contract and behavior.

Node ids are registered in ``case/tests/config.json``.  The set is deliberately
not contiguous: retired nodes keep their numbers unused, and new coverage uses
the next free numbers, so an id never silently changes meaning.
"""

import contextlib
import errno
import hashlib
import hmac
import io
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import warnings
import zipfile

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Tuple
from unittest import mock

import click
import pytest

from migration_support import isolated_python_env, normalize_info_output, normalize_process_output, run_isolated
from click.testing import CliRunner
from zipapp import ZipAppError

from shiv.bootstrap import (
    cache_path,
    ensure_no_modify,
    extend_python_path,
    extract_site_packages,
    get_first_sitedir_index,
)
from shiv.bootstrap.environment import Environment
from shiv.bootstrap.filelock import FileLock
from shiv.builder import BINPRM_BUF_SIZE, create_archive, write_file_prefix
from shiv.cli import console_script_exists, main as shiv_main
from shiv.constants import (
    BINPRM_ERROR,
    NO_OUTFILE,
    NO_PIP_ARGS_OR_SITE_PACKAGES,
    PIP_INSTALL_ERROR,
)
from shiv.info import main as info_main

UGOX = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def assert_source_merge(path, sources):
    """Check retained source members as an ordered subsequence ending in the last source.

    Compare bytes exactly and POSIX modes where supported, including metadata
    files and non-conflicting paths. Identical contributions are matched in
    order, rather than assigning every equal value to its first contributor.
    """
    expected = {}
    for source in sources:
        for member in sorted(source.rglob("*")):
            if member.is_file():
                name = "site-packages/" + member.relative_to(source).as_posix()
                mode = stat.S_IMODE(member.stat().st_mode) if os.name == "posix" else None
                expected.setdefault(name, []).append((member.read_bytes(), mode))
    with zipfile.ZipFile(str(path)) as archive:
        observed = {}
        for info in archive.infolist():
            assert "\\" not in info.filename, "ZIP entries must use the '/' separator"
            mode = stat.S_IMODE(info.external_attr >> 16) if os.name == "posix" else None
            observed.setdefault(info.filename, []).append((archive.read(info), mode))
        for name, declared in expected.items():
            retained = observed.get(name, [])
            assert 1 <= len(retained) <= len(declared), (
                f"{name}: expected 1..{len(declared)} contributing members, got {len(retained)}"
            )
            assert retained[-1] == declared[-1], (
                f"{name}: the last source must supply effective bytes and permissions"
            )
            remaining = iter(declared[:-1])
            for contribution in retained[:-1]:
                assert any(item == contribution for item in remaining), (
                    f"{name}: retained members must follow declared source order without repeated contributors"
                )
            assert archive.read(name) == declared[-1][0], f"{name}: wrong effective bytes"
        for name, retained in observed.items():
            if name not in expected:
                assert len(retained) == 1, f"an unshared archive path must appear exactly once: {name}"
    return list(observed)


@contextlib.contextmanager
def observe_file_locks(root):
    """Observe successful public FileLock use and native POSIX lock acquisitions.

    A residual path is not identified as a lock by its filename. Recording the
    public class also covers imported aliases; native locks cover builders that
    use flock/lockf directly instead of the public class.
    """
    observed = {"paths": set()}
    requested = {}
    original_init, original_acquire, original_enter = FileLock.__init__, FileLock.acquire, FileLock.__enter__
    signature = inspect.signature(original_init)
    path_parameter = next(name for name in signature.parameters if name != "self")

    def init(lock, *args, **kwargs):
        bound = signature.bind(lock, *args, **kwargs)
        requested[id(lock)] = Path(os.path.abspath(bound.arguments[path_parameter]))
        original_init(lock, *args, **kwargs)

    def record(lock, original, *args, **kwargs):
        result = original(lock, *args, **kwargs)
        if result is not False and id(lock) in requested:
            observed["paths"].add(requested[id(lock)])
        return result

    def acquire(lock, *args, **kwargs):
        return record(lock, original_acquire, *args, **kwargs)

    def enter(lock, *args, **kwargs):
        return record(lock, original_enter, *args, **kwargs)

    def native_wrapper(original, lock_operations):
        def locked(fd, operation, *args, **kwargs):
            result = original(fd, operation, *args, **kwargs)
            if operation & lock_operations:
                info = os.fstat(fd if isinstance(fd, int) else fd.fileno())
                # Resolve identities while the descriptor is live. Saving only
                # an inode would misclassify an unrelated file if a removed
                # temporary lock's inode were later reused.
                for path in root.rglob("*"):
                    try:
                        candidate = path.stat()
                    except FileNotFoundError:
                        continue
                    if (candidate.st_dev, candidate.st_ino) == (info.st_dev, info.st_ino):
                        observed["paths"].add(Path(os.path.abspath(path)))
            return result
        return locked

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(FileLock, "__init__", init))
        stack.enter_context(mock.patch.object(FileLock, "acquire", acquire))
        stack.enter_context(mock.patch.object(FileLock, "__enter__", enter))
        if os.name == "posix":
            import fcntl
            for name in ("flock", "lockf"):
                original = getattr(fcntl, name)
                replacement = native_wrapper(original, fcntl.LOCK_EX | fcntl.LOCK_SH)
                # Previously imported function aliases in production modules
                # must be observed just like calls through the fcntl module.
                for module_name, module in tuple(sys.modules.items()):
                    if module is not None and (module_name == "shiv" or module_name.startswith("shiv.")):
                        for attribute, value in tuple(vars(module).items()):
                            if value is original:
                                stack.enter_context(mock.patch.object(module, attribute, replacement))
                stack.enter_context(mock.patch.object(
                    fcntl, name, replacement
                ))
        yield observed


def assert_no_unusable_residue(parent, results, locks):
    """Allow published results and actual, released lock paths; reject all other residue."""
    expected = {Path(os.path.abspath(path)) for path in results}
    residue = []
    # Snapshot before probing: acquiring a lock must not classify its own newly
    # created path as evidence that a previously unknown file was a lock.
    known_paths = set(locks["paths"])
    for path in parent.iterdir():
        if Path(os.path.abspath(path)) in expected:
            continue
        if Path(os.path.abspath(path)) not in known_paths:
            residue.append(path.name)
            continue
        if os.name == "posix" and path.is_file():
            # Probe in another process before opening through FileLock here:
            # lockf locks are process-scoped, and closing any same-process fd
            # can silently release them. flock and lockf are separate domains.
            program = (
                "import fcntl, sys\n"
                "with open(sys.argv[1], 'r+b') as stream:\n"
                "    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "    fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            )
            probe = run_isolated([sys.executable, "-I", "-S", "-c", program, str(path)],
                                 capture_output=True, text=True, timeout=5)
            assert probe.returncode == 0, f"lock must be released after operation: {path}: {probe.stderr}"
        with FileLock(str(path), timeout=0.1, poll_interval=0.005):
            pass
    assert residue == [], f"operation left unusable temporary or backup state: {sorted(residue)}"


@pytest.fixture
def observed_lock_paths(tmp_path):
    with observe_file_locks(tmp_path) as observed:
        yield observed


@contextlib.contextmanager
def archive_write_limit():
    """Cause a real output write failure without depending on a packaging helper."""
    import resource
    import signal

    previous_limits = resource.getrlimit(resource.RLIMIT_FSIZE)
    previous_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (128, previous_limits[1]))
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, previous_limits)
        signal.signal(signal.SIGXFSZ, previous_handler)


@contextlib.contextmanager
def pip_child_status(code):
    """Use a real child with the requested status, retaining the complete Popen protocol."""
    if code < 0:
        assert os.name == "posix", "negative child statuses require POSIX signals"
        program = (
            "import os, resource, signal\n"
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
            f"number = {-code}\n"
            "if number not in (signal.SIGKILL, signal.SIGSTOP):\n"
            "    signal.signal(number, signal.SIG_DFL)\n"
            "os.kill(os.getpid(), number)\n"
        )
    else:
        program = f"import sys; sys.exit({code})"
    original_init = subprocess.Popen.__init__
    signature = inspect.signature(original_init)
    children = []

    def start(child, *args, **kwargs):
        # Change only the command used for fault injection. The returned object
        # is a real Popen, so run/check_call, iteration, communicate, wait, poll,
        # text/binary streams, context management and cleanup retain their APIs.
        bound = signature.bind(child, *args, **kwargs)
        bound.arguments["args"] = [sys.executable, "-c", program]
        if "executable" in bound.arguments:
            bound.arguments["executable"] = sys.executable
        if "shell" in bound.arguments:
            bound.arguments["shell"] = False
        original_init(*bound.args, **bound.kwargs)
        children.append(child)

    try:
        # Patch construction on the real class so previously imported Popen
        # aliases and subprocess.run/check_call all use the same real child.
        with mock.patch.object(subprocess.Popen, "__init__", start):
            yield children
    finally:
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            finally:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
    assert children, "the install probe did not execute a dependency child"
    assert all(child.returncode == code for child in children), "fault injection did not produce the requested status"


@pytest.fixture
def tmp_path(tmp_path_factory):
    # The original runtime lacks long-path support on Windows. Short fixture
    # names prevent MAX_PATH failures from masquerading as Click regressions.
    return tmp_path_factory.mktemp("migration")


@pytest.fixture
def hello_sp(tmp_path):
    """Portable site-packages fixture containing hello package for reliable cross-platform testing."""
    sp = tmp_path / "sp_hello_portable"
    pkg = sp / "hello"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("def main():\n    print('hello world')\n")
    return sp


@pytest.fixture
def hello_wheel(tmp_path):
    """Portable pre-built wheel fixture for fast, deterministic pip tests across platforms."""
    whl = tmp_path / "hello-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(str(whl), "w") as zf:
        zf.writestr("hello/__init__.py", "def main():\n    print('hello world')\n")
        zf.writestr("hello-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: hello\nVersion: 1.0.0\n\n")
        zf.writestr("hello-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        zf.writestr("hello-1.0.0.dist-info/entry_points.txt", "[console_scripts]\nhello = hello:main\n")
        zf.writestr("hello-1.0.0.dist-info/RECORD", "")
    return whl


@pytest.fixture
def local_runner():
    """Returns a click CLI runner for shiv."""
    def invoke(args, env=None):
        full_args = list(args)
        if "-p" not in full_args and "--python" not in full_args:
            full_args.extend(["-p", f"{sys.executable}"])
        return CliRunner().invoke(shiv_main, full_args, env=env)
    return invoke


@pytest.fixture(params=["source", "target"])
def runtime_python(request, baseline_envs):
    return getattr(baseline_envs, request.param + "_python")


def python_cli_runner(python):
    def invoke(args, env=None):
        full_args = list(args)
        if "-p" not in full_args and "--python" not in full_args:
            full_args.extend(["-p", str(python)])
        result = run_isolated([str(python), "-m", "shiv"] + full_args,
                              capture_output=True, text=True, env=env)
        return SimpleNamespace(exit_code=result.returncode, output=result.stdout + result.stderr)
    return invoke


@pytest.fixture
def runner(baseline_envs):
    """Target CLI builds must use the target dependency environment."""
    return python_cli_runner(baseline_envs.target_python)


@pytest.fixture
def isolated_runner(runtime_python):
    """Build each side's archive with its own implementation and Click version."""
    return python_cli_runner(runtime_python)


@pytest.fixture
def info_runner():
    """Returns a click CLI runner for shiv-info."""
    return lambda args: CliRunner().invoke(info_main, list(args))


# ---------------------------------------------------------------------
# Shared contract helpers
# ---------------------------------------------------------------------

HELP_ALIASES = ("-h", "--help", "--halp")

# Explicit representations this case understands for the two pieces of derived
# archive state.  The tables are maintained here, in one place, instead of being
# discovered by scanning arbitrary strings.
COMPRESSION_NAMES = {
    "deflated": True,
    "deflate": True,
    "stored": False,
    "store": False,
}
INTERPRETER_FIELDS = ("interpreter", "shebang")

# The runner sets this to a file path before invoking pytest.  A test that
# cannot establish the property it exists to check appends a record there, so
# the harness can report the observation as INCONCLUSIVE instead of PASS.
UNVERIFIED_COVERAGE_ENV = "SHIV_UNVERIFIED_COVERAGE_PATH"

_FIELD_LINE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*:[ \t]?(.*)$")
_MISSING = object()
_NOT_PARSED = object()


def record_unverified_coverage(observation_id, reason):
    """Persist a coverage gap so the harness reports an unfinished evaluation.

    A run that could not establish the property it exists to check must not be
    reported as a passing observation.  This writes a record the runner turns
    into an INCONCLUSIVE observation, which the hardgate treats as "evaluation
    incomplete" rather than as a candidate failure.  Outside the harness (no
    record file configured) the same information is emitted as a warning so a
    local run stays readable.
    """
    destination = os.environ.get(UNVERIFIED_COVERAGE_ENV)
    if not destination:
        warnings.warn(f"{observation_id}: {reason}", RuntimeWarning, stacklevel=2)
        return
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"id": observation_id, "reason": reason}, ensure_ascii=False) + "\n"
        )


def command_help_option(cmd):
    """Return the one real Click option that owns the three help aliases.

    Click 8.1.7 offers two legitimate ways to declare that option: the
    automatic help option driven by ``context_settings["help_option_names"]``,
    and an explicit ``click.help_option("-h", "--help", "--halp")`` combined
    with ``@click.command(add_help_option=False)``.  Only the first one is
    returned by ``Command.get_help_option()``, so the same question is answered
    from the command's actual parameter list, which contains both.
    """
    ctx = click.Context(cmd, **cmd.context_settings)
    owners = []
    for param in cmd.get_params(ctx):
        if not isinstance(param, click.Option):
            continue
        names = set(param.opts) | set(param.secondary_opts)
        if names & set(HELP_ALIASES):
            owners.append(param)
    assert len(owners) == 1, (
        "the three help aliases must belong to exactly one Click option; "
        f"found {[(option.name, option.opts, option.secondary_opts) for option in owners]}"
    )
    option = owners[0]
    names = list(option.opts) + list(option.secondary_opts)
    assert set(names) == set(HELP_ALIASES), (
        f"-h, --help and --halp must be aliases of one help option: {names}"
    )
    assert len(names) == len(set(names)), f"the help option must not repeat an alias: {names}"
    return option


def normalize_interpreter(value):
    """Return the interpreter a representation names, or a reason it cannot.

    Two spellings are defined here because both state the same fact and both can
    be checked against the archive: ``interpreter`` holding the program path and
    ``shebang`` holding the archive's whole ``#!`` line.  Nothing else is
    searched for something that might denote an interpreter.
    """
    if not isinstance(value, str):
        return None, f"must be a string, got {value!r}"
    text = value.strip()
    if text.startswith("#!"):
        text = text[2:].strip()
    if not text:
        return None, "is empty"
    return text, None


def reported_interpreter(metadata):
    """Return the interpreter a metadata document reports, or why it does not.

    Accepts the canonical ``interpreter`` field and the equivalent ``shebang``
    line.  Conflicting representations, an unusable value, or no representation
    at all are failures; the caller compares the result with the archive.
    """
    seen = []
    problems = []
    for key in INTERPRETER_FIELDS:
        if key not in metadata:
            continue
        value, reason = normalize_interpreter(metadata[key])
        if reason:
            problems.append(f"{key}: {reason}")
        else:
            seen.append((key, value))
    if problems:
        return None, "; ".join(problems)
    if not seen:
        return None, "no interpreter reported"
    if len({value for _, value in seen}) > 1:
        return None, f"conflicting interpreter representations: {seen}"
    return seen[0][1], None


def reported_compression(metadata):
    """Return the compression state a metadata document reports, or a reason.

    Two explicit representations are accepted: a boolean ``compressed`` flag and
    a ``compression`` method name.  A document may use either.  When both are
    present they must agree, and a name outside :data:`COMPRESSION_NAMES` cannot
    be interpreted and therefore fails.  No other field is scanned for a value
    that happens to look like a compression state.
    """
    pairs = []
    if "compressed" in metadata:
        value = metadata["compressed"]
        if not isinstance(value, bool):
            return None, f"compressed={value!r} is not a boolean"
        pairs.append(("compressed", value))
    if "compression" in metadata:
        value = metadata["compression"]
        if not isinstance(value, str) or value.strip().lower() not in COMPRESSION_NAMES:
            return None, f"compression={value!r} is not a recognized compression name"
        pairs.append(("compression", COMPRESSION_NAMES[value.strip().lower()]))
    return combine_compression(pairs)


def combine_compression(pairs):
    """Collapse reported compression fields into one state or a conflict reason."""
    if not pairs:
        return None, "no compression state reported"
    if len({state for _, state in pairs}) > 1:
        return None, f"conflicting compression fields: {pairs}"
    return pairs[0][1], None


def _collect_structured_value(first, lines, next_index):
    """Collect a JSON structure that may start inline or on the next line.

    The end of the value is decided by JSON's own decoder, so braces or brackets
    inside strings and escapes cannot truncate or over-extend it.  A scalar
    (a path, an interpreter, a timestamp) never enters this path.
    """
    decoder = json.JSONDecoder()
    pieces = [first]
    index = next_index
    while True:
        candidate = "\n".join(pieces).strip()
        if candidate:
            try:
                _, end = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                end = None
            if end is not None and end == len(candidate):
                return candidate, index
        if index >= len(lines):
            return candidate, index
        pieces.append(lines[index])
        index += 1


def parse_info_text(output):
    """Return the ordered ``(label, raw value)`` occurrences of a text report.

    A label starts a field only at the beginning of a line, optionally indented,
    so uniformly indented output parses the same way.  Its value is either the
    remainder of that line (scalars, including values containing punctuation) or
    a JSON structure that continues on following lines.  Repeated labels are all
    returned: the caller decides whether their values agree, instead of the
    parser silently keeping the first one.
    """
    occurrences = []
    lines = output.splitlines()
    index = 0
    while index < len(lines):
        match = _FIELD_LINE.match(lines[index])
        if not match:
            index += 1
            continue
        key, rest = match.group(1), match.group(2)
        if rest.lstrip()[:1] in ("{", "["):
            raw, index = _collect_structured_value(rest, lines, index + 1)
        elif rest.strip() == "":
            lookahead = lines[index + 1].lstrip() if index + 1 < len(lines) else ""
            if lookahead[:1] in ("{", "["):
                raw, index = _collect_structured_value("", lines, index + 1)
            else:
                raw, index = "", index + 1
        else:
            raw, index = rest, index + 1
        occurrences.append((key, raw))
    return occurrences


def parse_info_scalar(raw, expected):
    """Decode one rendered text value into the shape ``expected`` describes.

    Whitespace is irrelevant, booleans accept their common spellings, absent
    values accept the Python and JSON spellings, and mappings/sequences are
    parsed as JSON.  Anything that cannot be decoded unambiguously returns
    ``_NOT_PARSED`` so the caller reports a mismatch instead of guessing.
    """
    text = raw.strip()
    if isinstance(expected, (dict, list)):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return _NOT_PARSED
    if isinstance(expected, bool):
        lowered = text.lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
        return _NOT_PARSED
    if expected is None:
        if text in {"None", "null", ""}:
            return None
        return _NOT_PARSED
    if isinstance(expected, int):
        try:
            return int(text, 10)
        except ValueError:
            return _NOT_PARSED
    if isinstance(expected, float):
        try:
            return float(text)
        except ValueError:
            return _NOT_PARSED
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _canonical(value):
    """A hashable form of a decoded value, for duplicate comparison."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _decoded_occurrences(occurrences, key, expected):
    """Decode every occurrence of ``key``; report undecodable or conflicting ones.

    Repeated labels are accepted only when they normalize to the same value.
    Both "correct then wrong" and "wrong then correct" therefore fail, and the
    parser never decides the question by keeping the first or the last line.
    """
    raws = [raw for label, raw in occurrences if label == key]
    if not raws:
        return [], []
    decoded = []
    problems = []
    for raw in raws:
        value = parse_info_scalar(raw, expected)
        if value is _NOT_PARSED:
            problems.append(f"{key}: cannot decode {raw!r} as {expected!r}")
        else:
            decoded.append(value)
    if len({_canonical(value) for value in decoded}) > 1:
        problems.append(f"{key}: conflicting values for a repeated field: {decoded}")
    return decoded, problems


def archive_state(path):
    """Read one complete archive version: recorded metadata plus derived facts."""
    source = Path(path)
    with open(source, "rb") as handle:
        shebang = handle.readline().rstrip(b"\r\n")
    with zipfile.ZipFile(str(source)) as archive:
        recorded = json.loads(archive.read("environment.json"))
        compress_types = sorted({info.compress_type for info in archive.infolist()})
    return {
        "recorded": recorded,
        "interpreter": (
            shebang[2:].decode(sys.getfilesystemencoding()) if shebang.startswith(b"#!") else ""
        ),
        "compress_types": compress_types,
        "compressed": compress_types == [zipfile.ZIP_DEFLATED],
    }


def json_equal(left, right):
    """Compare two decoded JSON values without Python's cross-type equality.

    ``False == 0`` and ``True == 1`` in Python, so a report that turns a
    recorded ``false`` into ``0`` would otherwise look identical to the archive.
    Every JSON type is therefore compared separately: booleans, integers,
    floats, strings, null, arrays (element by element) and objects (key by key).
    A value that is not a JSON scalar/array/object falls back to exact type and
    equality, which is the strictest reading available.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return type(left) is type(right) and left == right
    if isinstance(left, str) or isinstance(right, str):
        return type(left) is type(right) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            json_equal(left[key], right[key]) for key in left
        )
    return type(left) is type(right) and left == right


def state_mismatches(metadata, state, *, require_derived=False):
    """Explain why ``metadata`` is not exactly one complete archive version.

    A report matches a version only when every recorded field carries that
    version's value.  Mixing values from two versions therefore matches neither
    version.

    The derived state (interpreter, compression) is checked whenever the report
    expresses it, and in every case it must name the same version as the
    recorded fields: that is what makes a report that mixes two opens
    detectable.  ``require_derived`` additionally demands that the report
    express it at all.  The frozen source baseline does not, so P2P nodes keep
    the default and only require truthfulness; a covered node that can assume
    the representation passes ``require_derived=True``.
    """
    problems = []
    missing = sorted(set(state["recorded"]) - set(metadata))
    if missing:
        problems.append(f"missing recorded fields: {missing}")
    wrong = {
        key: (metadata[key], value)
        for key, value in state["recorded"].items()
        if key in metadata and not json_equal(metadata[key], value)
    }
    if wrong:
        problems.append(f"recorded values do not match: {wrong}")

    if require_derived or any(key in metadata for key in INTERPRETER_FIELDS):
        interpreter, reason = reported_interpreter(metadata)
        if reason:
            problems.append(reason)
        elif interpreter != state["interpreter"]:
            problems.append(f"interpreter {interpreter!r} != {state['interpreter']!r}")

    if require_derived or any(key in metadata for key in ("compressed", "compression")):
        compressed, reason = reported_compression(metadata)
        if reason:
            problems.append(reason)
        elif compressed != state["compressed"]:
            problems.append(
                f"compression {compressed!r} != {state['compressed']!r} "
                f"(member types {state['compress_types']})"
            )
    return problems


def text_state_mismatches(occurrences, state, *, require_derived=False):
    """Explain why a rendered text report is not one complete archive version.

    ``occurrences`` is the ordered ``(label, raw value)`` list a text view
    produced, so each value is compared with the label it was printed under
    rather than with the whole output.  Values are decoded according to the type
    the archive records, which normalizes whitespace, boolean spellings and
    multi-line structures; repeated labels must agree with each other.

    As with :func:`state_mismatches`, derived state is checked whenever it is
    printed and must always name the same version as the recorded fields;
    ``require_derived`` additionally demands that it be printed at all.
    """
    problems = []
    for key, expected in state["recorded"].items():
        decoded, issues = _decoded_occurrences(occurrences, key, expected)
        problems.extend(issues)
        if not decoded:
            if not any(label == key for label, _ in occurrences):
                problems.append(f"missing recorded field: {key}")
            continue
        for value in decoded:
            if not json_equal(value, expected):
                problems.append(f"{key}: {value!r} != recorded {expected!r}")

    # Interpreter: canonical field and shebang line are equivalent spellings of
    # the same fact; both must name the archive's own interpreter.
    if require_derived or any(label in INTERPRETER_FIELDS for label, _ in occurrences):
        interpreter_forms = {}
        for key in INTERPRETER_FIELDS:
            decoded, issues = _decoded_occurrences(occurrences, key, "")
            problems.extend(issues)
            for value in decoded:
                normalized, reason = normalize_interpreter(value)
                if reason:
                    problems.append(f"{key}: {reason}")
                else:
                    interpreter_forms.setdefault(normalized, []).append(key)
        if not interpreter_forms:
            problems.append("no interpreter reported")
        elif len(interpreter_forms) > 1:
            problems.append(
                f"conflicting interpreter representations: {sorted(interpreter_forms)}"
            )
        else:
            reported = next(iter(interpreter_forms))
            if reported != state["interpreter"]:
                problems.append(f"interpreter: {reported!r} != {state['interpreter']!r}")

    # Compression: boolean flag and method name are equivalent spellings.
    if require_derived or any(label in ("compressed", "compression") for label, _ in occurrences):
        compression_pairs = []
        decoded, issues = _decoded_occurrences(occurrences, "compressed", True)
        problems.extend(issues)
        compression_pairs.extend(("compressed", value) for value in decoded)
        decoded, issues = _decoded_occurrences(occurrences, "compression", "")
        problems.extend(issues)
        for value in decoded:
            name = value.strip().lower() if isinstance(value, str) else None
            if name not in COMPRESSION_NAMES:
                problems.append(f"compression: {value!r} is not a recognized compression name")
            else:
                compression_pairs.append(("compression", COMPRESSION_NAMES[name]))
        compressed, reason = combine_compression(compression_pairs)
        if reason:
            problems.append(reason)
        elif compressed != state["compressed"]:
            problems.append(f"compression: {compressed!r} != {state['compressed']!r}")
    return problems


def assert_info_rejection(python, target, mode, fragment, label):
    """One bad input must fail with a diagnostic for its cause, in the documented channels.

    The contract requires status 1, no success-shaped output, and a diagnostic
    that names the underlying cause.  It does not prescribe the Python exception
    class or its formatting, so implementations may translate the cause into a
    CLI error as long as the observable rejection remains the same.
    """
    proc = run_isolated(
        [str(python), "-c", "from shiv.info import main; main()", *mode, str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.returncode == 1, (
        f"{label}: the documented invalid-input status is 1, got {proc.returncode}"
    )
    assert proc.stdout.strip() == "", (
        f"{label}: no success output may be produced, got {proc.stdout!r}"
    )
    assert "pyz file:" not in proc.stdout, f"{label}: the success header must not appear"
    assert proc.stderr.strip() != "", f"{label}: the diagnostic must be written to stderr"
    assert fragment in proc.stderr, (
        f"{label}: the diagnostic must name the underlying cause ({fragment!r})"
    )
    try:
        parsed = json.loads(proc.stdout)
    except ValueError:
        parsed = None
    assert parsed is None, (
        f"{label}: no success-shaped JSON may be produced, got {parsed!r}"
    )


# =====================================================================
# CLI Group
# =====================================================================
# =====================================================================
# CLI Group
# =====================================================================

def test_p2p_001_shiv_cli_option_surface(baseline_envs):
    """P2P-001: every public option lives on the real Click command, and the three help aliases share one help option."""
    param_opts = set()
    for param in shiv_main.params:
        param_opts.update(param.opts)
        param_opts.update(param.secondary_opts)

    expected_options = {
        "-e", "--entry-point",
        "-c", "--console-script",
        "-o", "--output-file",
        "-p", "--python",
        "--site-packages",
        "--build-id",
        "--compressed", "--uncompressed",
        "--compile-pyc",
        "-E", "--extend-pythonpath",
        "--reproducible",
        "--no-modify",
        "--preamble",
        "--root",
        "--version",
    }
    for opt in expected_options:
        assert opt in param_opts, f"Expected option {opt} in shiv CLI"

    assert isinstance(shiv_main, click.Command)

    # All three aliases must live on the single real help option rather than
    # being recognised as strings inside the command body.  The rendering order
    # is asserted by P2P-010; here the set of aliases is what matters.  Both the
    # automatic help option and an explicit `click.help_option(...)` declaration
    # place a real option in the command's parameter list, so the surface is
    # read from there instead of from `get_help_option()` alone.
    command_help_option(shiv_main)

    # A real entry point confirms the installed command renders every alias.
    help_result = CliRunner().invoke(shiv_main, ["--help"])
    assert help_result.exit_code == 0
    alias_lines = [line for line in help_result.output.splitlines() if "-h" in line and "--halp" in line]
    assert alias_lines and "--help" in alias_lines[0]

    p_tgt = run_isolated(
        [str(baseline_envs.target_python), "-m", "shiv", "--help"],
        capture_output=True,
        text=True,
    )
    assert p_tgt.returncode == 0, p_tgt.stderr
    assert any("-h" in line and "--help" in line and "--halp" in line for line in p_tgt.stdout.splitlines())
def test_p2p_002_shiv_info_cli_surface(baseline_envs):
    """P2P-002: shiv-info keeps its real Click surface: the positional PYZ argument, -j/--json, and one help option."""
    param_opts = set()
    for param in info_main.params:
        param_opts.update(param.opts)
        param_opts.update(param.secondary_opts)
    assert "-j" in param_opts
    assert "--json" in param_opts

    arguments = [param for param in info_main.params if isinstance(param, click.Argument)]
    assert [argument.name for argument in arguments] == ["pyz"]

    assert isinstance(info_main, click.Command)

    command_help_option(info_main)

    help_result = CliRunner().invoke(info_main, ["--help"])
    assert help_result.exit_code == 0
    alias_lines = [line for line in help_result.output.splitlines() if "-h" in line and "--halp" in line]
    assert alias_lines and "--help" in alias_lines[0]

    p_tgt = run_isolated(
        [str(baseline_envs.target_python), "-c", "from shiv.info import main; main()", "--help"],
        capture_output=True,
        text=True,
    )
    assert p_tgt.returncode == 0, p_tgt.stderr
    assert "-j, --json" in p_tgt.stdout
    assert "PYZ" in p_tgt.stdout
@pytest.mark.parametrize(
    "cmd_type,args,expected_exit,expected_text",
    [
        ("shiv", [], 1, NO_PIP_ARGS_OR_SITE_PACKAGES.strip()),
        ("shiv", ["-e", "hello:main"], 1, NO_PIP_ARGS_OR_SITE_PACKAGES.strip()),
        ("shiv", ["-e", "hello:main", "some_pkg"], 1, NO_OUTFILE.strip()),
        ("shiv", ["-o", "out.pyz", "-t", "foo"], 1, "disallowed pip argument! '-t'"),
        ("shiv", ["-o", "out.pyz", "--target", "foo"], 1, "disallowed pip argument! '--target'"),
        ("shiv", ["-o", "out.pyz", "--editable", "foo"], 1, "disallowed pip argument! '--editable'"),
        ("shiv", ["-o", "out.pyz", "-d", "foo"], 1, "disallowed pip argument! '-d'"),
        ("shiv", ["-o", "out.pyz", "--download", "foo"], 1, "disallowed pip argument! '--download'"),
        ("shiv", ["-o", "out.pyz", "--download=foo"], 1, "disallowed pip argument! '--download=foo'"),
        ("shiv", ["-o", "out.pyz", "--user"], 1, "disallowed pip argument! '--user'"),
        ("shiv", ["-o", "out.pyz", "--prefix", "foo"], 1, "disallowed pip argument! '--prefix'"),
        ("shiv", ["-o", "out.pyz", "--site-packages", ".", "-p", "/" + "a" * 130 + "/python"], 1, BINPRM_ERROR.strip()),
        ("shiv_pip_fail", ["-o", "out.pyz", "nonexistent-pkg-404"], 1, "Pip install failed!"),
        ("shiv_info", [], 2, "Missing argument"),
        ("wrong_path_fresh", ["--site-packages"], 1, "--site-packages"),
        ("wrong_path_existing", ["--site-packages"], 1, "--site-packages"),
        ("wrong_path_fresh", ["--preamble"], 1, "--preamble"),
        ("wrong_path_existing", ["--preamble"], 1, "--preamble"),
    ],
)
def test_f2p_009_error_exits(tmp_path, cmd_type, args, expected_exit, expected_text, baseline_envs):
    """F2P-009: target preserves the required CLI error contract where the source baseline diverges."""
    resolved_args = [str(tmp_path / "out.pyz") if a == "out.pyz" else a for a in args]
    if cmd_type.startswith("wrong_path_"):
        source = tmp_path / "valid source"
        source.mkdir()
        (source / "app.py").write_text("def main():\n    print('PATH_OK')\n")
        output = tmp_path / "path-error.pyz"
        before = None
        if cmd_type == "wrong_path_existing":
            build = CliRunner().invoke(shiv_main, ["--site-packages", str(source), "-o", str(output)])
            assert build.exit_code == 0, build.output
            before = output.read_bytes()
        option = args[0]
        wrong = source / "app.py" if option == "--site-packages" else source
        result = run_isolated(
            [str(baseline_envs.target_python), "-m", "shiv", "-o", str(output),
             "--site-packages", str(source), option, str(wrong)],
            capture_output=True, text=True,
        )
        diagnostic = result.stdout + result.stderr
        assert result.returncode == 1, diagnostic
        assert option in diagnostic and str(wrong) in diagnostic, diagnostic
        assert output.read_bytes() == before if before is not None else not output.exists()
    elif cmd_type == "shiv":
        # 1. CliRunner path
        result = CliRunner().invoke(shiv_main, resolved_args)
        assert result.exit_code == expected_exit, f"Output: {result.output}"
        assert expected_text in result.output

        # 2. Run the same command through both isolated interpreters.
        for py in (baseline_envs.source_python, baseline_envs.target_python):
            proc = run_isolated(
                [str(py), "-m", "shiv"] + resolved_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.returncode == expected_exit
            if py == baseline_envs.source_python and any("=" in arg for arg in args):
                assert PIP_INSTALL_ERROR.strip() in proc.stderr
                assert expected_text not in proc.stdout + proc.stderr
            else:
                assert expected_text in proc.stdout + proc.stderr

    elif cmd_type == "shiv_pip_fail":
        output_file = tmp_path / "failed.pyz"
        with mock.patch("shiv.pip.install", side_effect=SystemExit(PIP_INSTALL_ERROR)):
            result = CliRunner().invoke(shiv_main, ["-o", str(output_file), "nonexistent-pkg-404"])
            assert result.exit_code == expected_exit
            assert expected_text in result.output

    elif cmd_type == "shiv_info":
        res = CliRunner().invoke(info_main, [])
        assert res.exit_code == expected_exit
        assert expected_text in res.output

        for py in (baseline_envs.source_python, baseline_envs.target_python):
            proc = run_isolated(
                [str(py), "-c", "from shiv.info import main; main()"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.returncode == expected_exit
            assert expected_text in proc.stderr


def test_p2p_006_unprocessed_pip_tokens_forwarding(tmp_path, hello_wheel, isolated_runner, runtime_python):
    """P2P-006: Unrecognized pip arguments are forwarded unchanged, with real wheel build and pip failure checks.

    The signalled-child status rule is a target requirement (the pre-migration
    implementation compares with ``> 0``) and lives in F2P-010.
    """
    output_file = tmp_path / "out.pyz"
    tokens = ["--no-cache-dir", "--find-links", "https://wheels.example.com", "my-package==2.0.0"]
    with mock.patch("shiv.pip.install") as mock_install:
        result = CliRunner().invoke(shiv_main, ["-o", str(output_file), "-p", sys.executable] + tokens)
        assert result.exit_code == 0
        called_args = mock_install.call_args[0][0]
        assert called_args[0] == "--target"
        assert called_args[2:] == tokens

    # Real wheel build using find-links without network
    pyz_real = tmp_path / "real_pip.pyz"
    res_real = isolated_runner(
        ["-o", str(pyz_real), "-p", str(runtime_python), "--no-index", "--find-links", str(hello_wheel.parent), "hello", "-e", "hello:main"]
    )
    assert res_real.exit_code == 0, res_real.output
    assert pyz_real.exists()
    p_real = run_isolated([str(runtime_python), str(pyz_real)], capture_output=True, text=True)
    assert p_real.returncode == 0
    assert "hello world" in p_real.stdout

    # Pip failure stops build and does not create pyz
    pyz_fail = tmp_path / "fail_pip.pyz"
    res_fail = isolated_runner(
        ["-o", str(pyz_fail), "-p", str(runtime_python), "--no-index", "nonexistent_package_404", "-e", "hello:main"]
    )
    assert res_fail.exit_code == 1
    assert "Pip install failed!" in res_fail.output
    assert not pyz_fail.exists()


def test_p2p_007_public_cli_contract_summary(baseline_envs, tmp_path, hello_sp):
    """P2P-007: Small cross-process smoke of the public CLI contract; detailed coverage lives in the dedicated nodes.

    Coverage ownership: this node only proves that the public entry points are
    real Click commands which run cross-process: ``--version`` succeeds, a missing
    input exits 1, and ``shiv-info`` renders a text view and a parseable JSON view
    for a real archive.  The complete metadata field set and the per-field
    text/JSON agreement belong to P2P-042/P2P-043/P2P-050; the option surface belongs to
    P2P-001/P2P-002; help short-circuiting belongs to P2P-010.

    The command object's internal ``name`` is deliberately not asserted.  The
    public contract is the installed entry point and its options, not the
    attribute the command happens to carry; an implementation naming its commands
    ``shiv`` and ``shiv-info`` renders a more accurate usage line and must not be
    rejected here.
    """
    assert isinstance(shiv_main, click.Command)
    assert isinstance(info_main, click.Command)

    for python in (baseline_envs.source_python, baseline_envs.target_python):
        version = run_isolated([str(python), "-m", "shiv", "--version"], capture_output=True, text=True)
        assert version.returncode == 0
        assert version.stdout.startswith("shiv, version")

        missing_input = run_isolated([str(python), "-m", "shiv"], capture_output=True, text=True)
        assert missing_input.returncode == 1
        assert "You must supply PIP ARGS or --site-packages!" in (missing_input.stderr + missing_input.stdout)

    pyz = tmp_path / "info_summary.pyz"
    create_archive([hello_sp], pyz, sys.executable, "hello:main", Environment("2026-09-30 00:00:00", "1.1.0"))

    info_text = run_isolated(
        [str(baseline_envs.target_python), "-c", "from shiv.info import main; main()", str(pyz)],
        capture_output=True,
        text=True,
    )
    assert info_text.returncode == 0, info_text.stderr
    assert f"pyz file: {str(pyz)}" in info_text.stdout

    info_json = run_isolated(
        [str(baseline_envs.target_python), "-c", "from shiv.info import main; main()", "--json", str(pyz)],
        capture_output=True,
        text=True,
    )
    assert info_json.returncode == 0, info_json.stderr
    data = json.loads(info_json.stdout)
    assert isinstance(data, dict)
    assert data, "the JSON view must describe the archive, not an empty document"
def test_p2p_008_pip_forwarding_and_disallowed_boundaries(tmp_path):
    """P2P-008: Pip forwarding with space and equal signs, and rejection boundary for disallowed options."""
    output_file = tmp_path / "out.pyz"
    tokens = ["--index-url=https://pypi.org/simple", "--extra-index-url", "https://custom.repo", "pkg name==1.0"]
    with mock.patch("shiv.pip.install") as mock_install:
        result = CliRunner().invoke(shiv_main, ["-o", str(output_file), "-p", sys.executable] + tokens)
        assert result.exit_code == 0
        called_args = mock_install.call_args[0][0]
        assert tokens[0] in called_args
        assert tokens[1] in called_args
        assert tokens[2] in called_args
        assert tokens[3] in called_args

    for disallowed in ("-t", "--target", "--editable", "-d", "--download", "--user", "--prefix"):
        res = CliRunner().invoke(shiv_main, ["-o", str(output_file), disallowed])
        assert res.exit_code == 1
        assert "disallowed pip argument!" in res.output


@pytest.mark.parametrize("cmd,help_flag", [
    (shiv_main, "-h"), (shiv_main, "--help"), (shiv_main, "--halp"),
    (info_main, "-h"), (info_main, "--help"), (info_main, "--halp"),
])
def test_p2p_010_help_entries_and_output(cmd, help_flag, baseline_envs):
    """P2P-010: -h, --help and --halp are one real eager help option that returns 0, renders a usage line, and short-circuits.

    The three spellings must be aliases of a single real help option rather than
    strings recognised inside the command body, and a help request must be
    answered before any build work happens.  The rendered order of the aliases
    and the program name shown in the usage line are Click's own presentation
    choices: the alias check therefore requires one rendered line carrying all
    three spellings and compares the option object's alias *set*, while the usage
    line is validated through the real published entry point with only its
    option/argument shape pinned.
    """
    res = CliRunner().invoke(cmd, [help_flag])
    assert res.exit_code == 0
    assert res.exception is None

    # All three spellings of the help option must appear on one rendered line.
    alias_lines = [
        line for line in res.output.splitlines()
        if "-h" in line and "--help" in line and "--halp" in line
    ]
    assert alias_lines, (
        f"the rendered help must present -h, --help and --halp as one option: {res.output.splitlines()[:6]}"
    )

    # A representative rendered help must expose every public option, so an
    # implementation cannot hide or omit an option from the documented surface
    # while still passing the option-object checks of P2P-001/P2P-002.  Only the
    # option spellings are compared, not their rendering order or the metavar
    # text Click derives from the parameter type.
    if cmd is shiv_main and help_flag == "--help":
        option_tokens = set()
        for line in res.output.splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            head = stripped.split("  ", 1)[0]
            for token in head.replace(",", " ").split():
                option_tokens.add(token)
        for alias in (
            "--version",
            "-e", "--entry-point",
            "-c", "--console-script",
            "-o", "--output-file",
            "-p", "--python",
            "--site-packages",
            "--build-id",
            "--compressed", "--uncompressed",
            "--compile-pyc",
            "-E", "--extend-pythonpath",
            "--reproducible",
            "--no-modify",
            "--preamble",
            "--root",
            "-h", "--help", "--halp",
        ):
            assert alias in option_tokens, (
                f"the rendered shiv help must document {alias}; documented: {sorted(option_tokens)}"
            )
    if cmd is info_main and help_flag == "--help":
        option_lines = [line.strip() for line in res.output.splitlines()]
        for alias in ("-j", "--json", "-h", "--help", "--halp"):
            assert any(alias in line for line in option_lines), (
                f"the rendered shiv-info help must document {alias}"
            )
        assert any("PYZ" in line for line in option_lines), "the rendered shiv-info help must document PYZ"

    # The three spellings must be aliases of one option object, and that option
    # must be eager: it has to run before the command body regardless of the
    # other arguments supplied.  `command_help_option` reads the command's
    # actual parameters, so both an automatic help option and an explicit
    # `click.help_option(...)` declaration are recognised.
    help_option = command_help_option(cmd)
    assert help_option.is_eager is True, "the help option must be eager so it short-circuits the command body"

    # No other option may claim one of the aliases: three separate options that
    # each print help would not be "the same help option".  Click builds a fresh
    # help option object per `get_params` call, so the help parameter is
    # identified by name rather than by identity.
    ctx = click.Context(cmd, **cmd.context_settings)
    for param in cmd.get_params(ctx):
        if not isinstance(param, click.Option) or param.name == help_option.name:
            continue
        overlap = (set(param.opts) | set(param.secondary_opts)) & set(HELP_ALIASES)
        assert not overlap, f"only the help option may own {sorted(overlap)}; {param.name} also declares them"

    # A help request with extra arguments must be answered by the eager help
    # option before any build work happens.
    with mock.patch("shiv.pip.install") as mock_install, mock.patch("shiv.builder.create_archive") as mock_archive:
        res_extra = CliRunner().invoke(cmd, [help_flag, "some_extra_arg"])
        assert res_extra.exit_code == 0
        assert res_extra.exception is None
        mock_install.assert_not_called()
        mock_archive.assert_not_called()

    invalid = "missing-unrelated-path-for-help"
    unrelated = (["--site-packages", invalid, "--preamble", invalid, "-o"]
                 if cmd is shiv_main else ["--unknown", "missing.pyz", "extra"])
    for arguments in ([help_flag, *unrelated], [*unrelated, help_flag]):
        with mock.patch("shiv.pip.install") as install, mock.patch("shiv.builder.create_archive") as build:
            immediate = CliRunner().invoke(cmd, arguments)
        assert immediate.exit_code == 0, immediate.output
        assert immediate.output.startswith("Usage:"), immediate.output
        install.assert_not_called()
        build.assert_not_called()

    # One real entry point per command: the usage line must describe the same
    # option and argument shape, and help must not create an output file.
    if cmd is shiv_main:
        pyz_sub = Path(tempfile.gettempdir()) / f"help_short_{os.getpid()}_{help_flag.strip('-')}.pyz"
        if pyz_sub.exists():
            pyz_sub.unlink()
        p_sub = run_isolated(
            [str(baseline_envs.target_python), "-m", "shiv", help_flag, "-o", str(pyz_sub), "nonexistent_pkg_short"],
            capture_output=True,
            text=True,
        )
        assert p_sub.returncode == 0
        assert not pyz_sub.exists()
        usage = [line for line in p_sub.stdout.splitlines() if line.startswith("Usage:")]
        assert usage, f"the real entry point must render a usage line: {p_sub.stdout[:400]}"
        assert re.match(r"^Usage: .+ \[OPTIONS\] \[PIP_ARGS\]\.\.\.$", usage[0]), usage[0]
    else:
        p_sub = run_isolated(
            [str(baseline_envs.target_python), "-c", "from shiv.info import main; main()", help_flag, "some_extra_arg"],
            capture_output=True,
            text=True,
        )
        assert p_sub.returncode == 0
        usage = [line for line in p_sub.stdout.splitlines() if line.startswith("Usage:")]
        assert usage, f"the real entry point must render a usage line: {p_sub.stdout[:400]}"
        assert re.match(r"^Usage: .+ \[OPTIONS\] PYZ$", usage[0]), usage[0]
def test_p2p_011_version(baseline_envs):
    """P2P-011: shiv --version returns 0 and does not trigger build or pip install."""
    with mock.patch("shiv.pip.install") as mock_install, mock.patch("shiv.builder.create_archive") as mock_archive:
        res = CliRunner().invoke(shiv_main, ["--version"])
        assert res.exit_code == 0
        assert res.output.startswith("shiv, version")
        mock_install.assert_not_called()
        mock_archive.assert_not_called()

    # Subprocess execution: python -m shiv --version
    for py in (baseline_envs.source_python, baseline_envs.target_python):
        proc_module = run_isolated(
            [str(py), "-m", "shiv", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc_module.returncode == 0
        assert proc_module.stdout.startswith("shiv, version")

    # Verify short-circuit without building target file
    pyz_ver_sub = Path(tempfile.gettempdir()) / f"version_short_{os.getpid()}.pyz"
    if pyz_ver_sub.exists():
        pyz_ver_sub.unlink()
    for py in (baseline_envs.source_python, baseline_envs.target_python):
        p_ver_sub = run_isolated([str(py), "-m", "shiv", "--version", "-o", str(pyz_ver_sub), "nonexistent_pkg_short"], capture_output=True, text=True)
        assert p_ver_sub.returncode == 0
        assert not pyz_ver_sub.exists()


    for arguments in (["--version", "-o"], ["--site-packages", "missing-version-path", "-o", "--version"]):
        with mock.patch("shiv.pip.install") as install, mock.patch("shiv.builder.create_archive") as build:
            immediate = CliRunner().invoke(shiv_main, arguments)
        assert immediate.exit_code == 0, immediate.output
        assert immediate.output.startswith("shiv, version "), immediate.output
        install.assert_not_called()
        build.assert_not_called()


def test_f2p_008_entry_point_precedence_over_console_script(tmp_path, runner, baseline_envs):
    """F2P-008: target --entry-point precedence fixes the source console-script precedence defect."""
    sp_dir = tmp_path / "site-packages"
    sp_dir.mkdir()
    mod_file = sp_dir / "two_callables.py"
    mod_file.write_text(
        "def from_ep():\n"
        "    print('CALL_FROM_ENTRY_POINT')\n"
        "def from_cs():\n"
        "    print('CALL_FROM_CONSOLE_SCRIPT')\n"
    )
    dist_info = sp_dir / "my_pkg-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "entry_points.txt").write_text("[console_scripts]\ncs_cmd = two_callables:from_cs\n")

    output_pyz = tmp_path / "precedence.pyz"
    res = runner([
        "-o", str(output_pyz),
        "--site-packages", str(sp_dir),
        "-e", "two_callables:from_ep",
        "-c", "cs_cmd",
    ])
    assert res.exit_code == 0, res.output
    assert output_pyz.exists()

    source_pyz = tmp_path / "precedence_source.pyz"
    source_build = run_isolated(
        [str(baseline_envs.source_python), "-m", "shiv", "-p", str(baseline_envs.source_python), "-o", str(source_pyz),
         "--site-packages", str(sp_dir), "-e", "two_callables:from_ep", "-c", "cs_cmd"],
        capture_output=True, text=True,
    )
    assert source_build.returncode == 0, source_build.stderr
    target_proc = run_isolated([str(baseline_envs.target_python), str(output_pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    source_proc = run_isolated([str(baseline_envs.source_python), str(source_pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # The source baseline incorrectly prioritizes the console script; target
    # behavior is the required post-migration result.
    assert source_proc.returncode == 1
    assert "cs_cmd" in source_proc.stderr and "FileNotFoundError" in source_proc.stderr
    assert target_proc.returncode == 0
    assert target_proc.stdout == "CALL_FROM_ENTRY_POINT\n"
    assert target_proc.stderr == ""


def test_p2p_013_entry_points_parsing(tmp_path, runner, baseline_envs):
    """P2P-013: entry_points.txt lookup keeps source order, unique and missing names, and both build orders.

    Both declared orders are compared at helper level and then built and run
    end to end, so a change that silently reorders the lookup is caught by the
    archive's actual startup, not only by the probe.
    """
    sp1 = tmp_path / "sp1"
    sp1.mkdir()
    (sp1 / "ep_mod.py").write_text("def run1():\n    print('EP_RUN_1')\n")
    dist1 = sp1 / "pkg1-0.1.dist-info"
    dist1.mkdir()
    (dist1 / "entry_points.txt").write_text("[console_scripts]\nshared = ep_mod:run1\nonly1 = ep_mod:run1\n")

    sp2 = tmp_path / "sp2"
    sp2.mkdir()
    (sp2 / "ep_mod2.py").write_text("def run2():\n    print('EP_RUN_2')\n")
    dist2 = sp2 / "pkg2-0.1.dist-info"
    dist2.mkdir()
    (dist2 / "entry_points.txt").write_text("[console_scripts]\nshared = ep_mod2:run2\nonly2 = ep_mod2:run2\n")

    # Source-order lookup must agree between the source and target implementations.
    probe_script = f"""
from pathlib import Path
from shiv.cli import find_entry_point
sp1 = Path(r'{sp1.resolve()}')
sp2 = Path(r'{sp2.resolve()}')
print('1:', find_entry_point([sp1, sp2], 'shared'))
print('2:', find_entry_point([sp2, sp1], 'shared'))
print('3:', find_entry_point([sp1, sp2], 'only1'))
print('4:', find_entry_point([sp1, sp2], 'only2'))
"""
    p_src = run_isolated([str(baseline_envs.source_python), "-c", probe_script], capture_output=True, text=True)
    p_tgt = run_isolated([str(baseline_envs.target_python), "-c", probe_script], capture_output=True, text=True)
    assert p_src.returncode == p_tgt.returncode == 0
    assert p_tgt.stdout == p_src.stdout

    # End-to-end in both declared orders: the order handed to the build decides
    # which implementation provides the effective console script.  Each order
    # uses its own archive so one build cannot mask the other.  Both
    # entry_points.txt files declare ``shared``, so the later declared source is
    # effective for that name.
    expected_marker = {
        (sp1, sp2): "EP_RUN_2",
        (sp2, sp1): "EP_RUN_1",
    }
    for index, order in enumerate(((sp1, sp2), (sp2, sp1))):
        pyz = tmp_path / f"ep_order_{index}.pyz"
        source_pyz = tmp_path / f"ep_order_{index}_source.pyz"
        args = ["--site-packages", str(order[0]), "--site-packages", str(order[1]), "-c", "shared"]
        res = runner(["-o", str(pyz), *args])
        assert res.exit_code == 0, res.output
        source_build = run_isolated(
            [str(baseline_envs.source_python), "-m", "shiv",
             "-p", str(baseline_envs.source_python), "-o", str(source_pyz), *args],
            capture_output=True, text=True,
        )
        assert source_build.returncode == 0, source_build.stderr

        proc = run_isolated(
            [str(baseline_envs.target_python), str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        source_proc = run_isolated(
            [str(baseline_envs.source_python), str(source_pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert (proc.returncode, proc.stdout, proc.stderr) == (
            source_proc.returncode, source_proc.stdout, source_proc.stderr
        ), f"order {index}: source and target must select the same script"
        marker = expected_marker[order]
        assert marker in proc.stdout, (
            f"order {index}: the later declared source must provide the script; "
            f"expected {marker}, got {proc.stdout!r}"
        )

    def error_type(stderr):
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        return lines[-1].split(":", 1)[0] if lines else ""

    # Missing [console_scripts] section: both implementations reject the lookup the same way.
    sp_nosect = tmp_path / "sp_nosect"
    sp_nosect.mkdir()
    dist_nosect = sp_nosect / "nosect-1.0.dist-info"
    dist_nosect.mkdir()
    (dist_nosect / "entry_points.txt").write_text("[gui_scripts]\napp = foo:bar\n")
    no_section_probe = f"from pathlib import Path; from shiv.cli import find_entry_point; find_entry_point([Path(r'{sp_nosect.resolve()}')], 'app')"
    p_src_nosect = run_isolated([str(baseline_envs.source_python), "-c", no_section_probe], capture_output=True, text=True)
    p_tgt_nosect = run_isolated([str(baseline_envs.target_python), "-c", no_section_probe], capture_output=True, text=True)
    assert p_src_nosect.returncode != 0 and p_tgt_nosect.returncode != 0
    assert error_type(p_tgt_nosect.stderr) == error_type(p_src_nosect.stderr)

    # Missing console-script name: a clear CLI failure naming the requested entry point.
    missing_probe = f"from pathlib import Path; from shiv.cli import find_entry_point; find_entry_point([Path(r'{sp1.resolve()}')], 'nonexistent_cmd')"
    p_src_missing = run_isolated([str(baseline_envs.source_python), "-c", missing_probe], capture_output=True, text=True)
    p_tgt_missing = run_isolated([str(baseline_envs.target_python), "-c", missing_probe], capture_output=True, text=True)
    assert p_src_missing.returncode != 0 and p_tgt_missing.returncode != 0
    assert error_type(p_tgt_missing.stderr) == error_type(p_src_missing.stderr)

    res_missing = runner(["-o", str(tmp_path / "missing.pyz"), "--site-packages", str(sp1), "-c", "nonexistent_cmd"])
    assert res_missing.exit_code == 1
    assert "No entry point 'nonexistent_cmd' found" in res_missing.output
def test_f2p_010_signalled_pip_install_fails(tmp_path, hello_wheel):
    """F2P-010: a pip child killed by a signal is a failed install.

    ``Popen.wait()`` reports a signalled child as ``-signal``.  The pre-migration
    implementation compares with ``> 0``, so a terminated install looks like a
    success and an archive can be published from an incomplete site-packages.
    The target treats any non-zero status as a failure.
    """
    import signal

    from shiv import pip as pip_module

    def run_with_status(code):
        @click.command()
        def install_command():
            pip_module.install(["--target", str(tmp_path / "sp_status"), "anything"])
        with pip_child_status(code):
            return CliRunner().invoke(install_command, [])

    statuses = [1]
    if os.name == "posix":
        signals = [signal.SIGTERM, signal.SIGINT]
        signals.extend(getattr(signal, name) for name in ("SIGKILL", "SIGHUP", "SIGQUIT")
                       if hasattr(signal, name))
        statuses.extend(-number for number in dict.fromkeys(int(value) for value in signals))
    else:
        record_unverified_coverage("F2P-010", "Negative subprocess signal statuses require a POSIX host.")
    for status in statuses:
        result = run_with_status(status)
        assert result.exit_code == 1, f"pip status {status} must exit 1: {result.output}"
        assert "Pip install failed!" in result.output, result.output
    success = run_with_status(0)
    assert success.exit_code == 0, f"a zero exit status must not fail the install: {success.output}"

    # The same through the public command: exit 1, the existing message, and no
    # archive published over the previous one.
    output_file = tmp_path / "killed.pyz"
    failure_status = -int(signal.SIGTERM) if os.name == "posix" else 1
    with pip_child_status(failure_status):
        result = CliRunner().invoke(
            shiv_main,
            ["-o", str(output_file), "-p", sys.executable, "--no-index",
             "--find-links", str(hello_wheel.parent), "hello", "-e", "hello:main"],
        )
    assert result.exit_code == 1, f"a killed install must exit 1: {result.output}"
    assert "Pip install failed!" in result.output
    assert not output_file.exists(), "a failed install must not publish an archive"

    existing = tmp_path / "killed_existing.pyz"
    build = CliRunner().invoke(
        shiv_main,
        ["-o", str(existing), "-p", sys.executable, "--no-index",
         "--find-links", str(hello_wheel.parent), "hello", "-e", "hello:main"],
    )
    assert build.exit_code == 0, build.output
    before = existing.read_bytes()
    with pip_child_status(failure_status):
        result = CliRunner().invoke(
            shiv_main,
            ["-o", str(existing), "-p", sys.executable, "--no-index",
             "--find-links", str(hello_wheel.parent), "hello", "-e", "hello:main"],
        )
    assert result.exit_code == 1
    assert existing.read_bytes() == before, "a failed install must not replace the previous archive"


def test_f2p_011_failed_replacement_preserves_archive(tmp_path, observed_lock_paths):
    """F2P-011: archive and extraction publication are all-or-nothing.

    The instruction requires archive replacement to be all-or-nothing: a failed
    replacement must leave the complete previous result unchanged, a failure on a
    fresh target must leave nothing that could be mistaken for a result, and two
    builds of one output path must not interleave their writes.  The
    pre-migration builder writes through the target before it is complete, so a
    mid-write failure damages the existing archive and two concurrent builds
    leave a corrupt one.  The target writes privately and publishes with a single
    atomic replace, so the surviving archive is always one complete version.

    Extraction has the same failure guarantee.  A corrupt ``site-packages``
    member makes extraction genuinely fail after valid content was encountered;
    it must not publish a partial cache or destroy an existing valid one.  A
    later valid extraction must still work without manual cleanup.  An empty
    source directory is valid input, not evidence of an extraction failure.
    """
    sp = tmp_path / "sp_atomic"
    sp.mkdir()
    (sp / "mod_atomic.py").write_text("def run():\n    print('ATOMIC_OK')\n")

    pyz = tmp_path / "atomic.pyz"
    build = CliRunner().invoke(
        shiv_main,
        ["-o", str(pyz), "--site-packages", str(sp), "-e", "mod_atomic:run", "-p", str(sys.executable)],
    )
    assert build.exit_code == 0, build.output
    original = pyz.read_bytes()

    env_test = Environment("2026-09-30 00:00:00", "1.1.0")
    if os.name == "posix":
        with archive_write_limit():
            with pytest.raises(OSError) as error:
                create_archive([sp], pyz, sys.executable, "mod_atomic:run", env_test)
        assert error.value.errno == errno.EFBIG, "the fixture must fail on a real limited write"
    else:
        record_unverified_coverage("F2P-011", "A deterministic real write failure requires POSIX RLIMIT_FSIZE.")

    assert pyz.exists(), "the previous archive must still exist"
    assert pyz.read_bytes() == original, (
        "a failed replacement must leave the previous archive byte-identical"
    )
    assert_no_unusable_residue(pyz.parent, [sp, pyz], observed_lock_paths)

    # The over-long shebang is rejected after the target has been opened for
    # writing, so it must still leave the previous archive intact.
    with pytest.raises(SystemExit):
        create_archive([sp], pyz, "/" + "a" * BINPRM_BUF_SIZE, "mod_atomic:run", env_test)
    assert pyz.read_bytes() == original, (
        "a rejection after the target was opened must not damage the previous archive"
    )
    assert_no_unusable_residue(pyz.parent, [sp, pyz], observed_lock_paths)

    # A failure on a fresh target must not leave an archive behind.
    fresh = tmp_path / "fresh_atomic.pyz"
    if os.name == "posix":
        with archive_write_limit():
            with pytest.raises(OSError) as error:
                create_archive([sp], fresh, sys.executable, "mod_atomic:run", env_test)
        assert error.value.errno == errno.EFBIG
    assert not fresh.exists(), "a failed first build must not leave an archive behind"
    assert_no_unusable_residue(pyz.parent, [sp, pyz], observed_lock_paths)

    # Two builds of one output path must not interleave their writes.  A
    # successful FileLock probe on its own does not show that; only the
    # artifact two concurrent builds leave behind does.
    sp_a = tmp_path / "sp_race_a"
    sp_a.mkdir()
    (sp_a / "mod_race.py").write_text("def run():\n    print('VERSION_1_OK')\n")
    sp_b = tmp_path / "sp_race_b"
    sp_b.mkdir()
    (sp_b / "mod_race.py").write_text("def run():\n    print('VERSION_2_OK')\n")
    build_dir = tmp_path / "race"
    build_dir.mkdir()
    build_pyz = build_dir / "shared_output.pyz"

    errors = []

    def competing_build(source_dir):
        try:
            environment = Environment(
                "2026-09-30 00:00:00", "1.1.0", entry_point="mod_race:run"
            )
            create_archive(
                [source_dir], build_pyz, sys.executable, "_bootstrap:bootstrap", environment
            )
        except Exception as exc:  # pragma: no cover - only reachable on a real race failure
            errors.append(exc)

    threads = [
        threading.Thread(target=competing_build, args=(sp_a,)),
        threading.Thread(target=competing_build, args=(sp_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120.0)
        assert not thread.is_alive(), "a competing build did not finish"

    assert errors == [], f"competing builds must both complete: {errors}"
    assert build_pyz.exists(), "the racing builds must leave the output archive behind"
    with zipfile.ZipFile(str(build_pyz)) as archive:
        names = [info.filename for info in archive.infolist()]
        assert archive.testzip() is None, "the final archive must be readable end to end"
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert duplicates == [], f"central directory must not repeat a member: {duplicates}"
        for required in ("__main__.py", "environment.json", "_bootstrap/__init__.py"):
            assert required in names, f"the final archive is missing {required}"
    execution = run_isolated(
        [sys.executable, str(build_pyz)], capture_output=True, text=True,
        env={**os.environ, "SHIV_ROOT": str(tmp_path / "race-cache")}, timeout=15,
    )
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert execution.stdout.strip() in ("VERSION_1_OK", "VERSION_2_OK"), execution.stdout
    # Identify locks from their successful use, independently of their names.
    assert_no_unusable_residue(build_dir, [build_pyz], observed_lock_paths)

    # A declared source may be empty.  It is not an invalid build merely
    # because the resulting archive has no site-packages file members.
    empty_sp = tmp_path / "empty-sp"
    empty_sp.mkdir()
    empty_pyz = tmp_path / "empty-sp.pyz"
    with mock.patch("shiv.pip.install") as mock_install:
        empty_build = CliRunner().invoke(
            shiv_main,
            ["-o", str(empty_pyz), "--site-packages", str(empty_sp), "-p", str(sys.executable)],
        )
    mock_install.assert_not_called()
    assert empty_build.exit_code == 0, empty_build.output
    with zipfile.ZipFile(str(empty_pyz)) as archive:
        assert archive.testzip() is None
        assert "__main__.py" in archive.namelist()
        assert "environment.json" in archive.namelist()

    # Corrupt the payload (not the central directory) of the second, stored
    # site-packages member.  A reader can process the first member, but reading
    # the second must fail its CRC check.  Unlike a missing member, this is an
    # actual extraction error under the instruction's all-or-nothing rule.
    incomplete_zip = tmp_path / "corrupt-extraction.zip"
    broken_name = "site-packages/broken.py"
    with zipfile.ZipFile(str(incomplete_zip), "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("site-packages/first.py", "VALUE = 'FIRST'\n")
        archive.writestr(broken_name, "VALUE = 'BROKEN'\n")
    with zipfile.ZipFile(str(incomplete_zip)) as archive:
        header = archive.getinfo(broken_name).header_offset
    damaged = bytearray(incomplete_zip.read_bytes())
    name_size = int.from_bytes(damaged[header + 26:header + 28], "little")
    extra_size = int.from_bytes(damaged[header + 28:header + 30], "little")
    payload_offset = header + 30 + name_size + extra_size
    damaged[payload_offset] ^= 1
    incomplete_zip.write_bytes(damaged)
    with zipfile.ZipFile(str(incomplete_zip)) as archive:
        assert archive.testzip() == broken_name, "the fixture must fail while reading package data"

    extraction_parent = tmp_path / "extraction-recovery"
    extraction_parent.mkdir()
    extraction_results = set()
    extraction_target = extraction_parent / "result"
    with zipfile.ZipFile(str(incomplete_zip)) as archive:
        with pytest.raises(Exception):
            extract_site_packages(archive, extraction_target, compile_pyc=False, force=False)

    assert not extraction_target.exists(), (
        "a failed first extraction must not publish a partial target"
    )
    assert_no_unusable_residue(extraction_parent, extraction_results, observed_lock_paths)

    # The failure must not poison a later invocation with usable content.
    complete_zip = tmp_path / "complete-extraction.zip"
    with zipfile.ZipFile(str(complete_zip), "w") as archive:
        archive.writestr("site-packages/recovered.py", "VALUE = 'RECOVERED'\n")
    with zipfile.ZipFile(str(complete_zip)) as archive:
        extract_site_packages(archive, extraction_target, compile_pyc=False, force=False)
    extraction_results.add(extraction_target)
    assert (
        extraction_target / "site-packages" / "recovered.py"
    ).read_text() == "VALUE = 'RECOVERED'\n"

    # The same real read failure during a forced replacement must leave the
    # complete prior extraction available, not expose first.py from the failed
    # replacement.  A later forced extraction can still publish new content.
    with zipfile.ZipFile(str(incomplete_zip)) as archive:
        with pytest.raises(Exception):
            extract_site_packages(archive, extraction_target, compile_pyc=False, force=True)
    assert (
        extraction_target / "site-packages" / "recovered.py"
    ).read_text() == "VALUE = 'RECOVERED'\n"
    assert not (extraction_target / "site-packages" / "first.py").exists()
    assert_no_unusable_residue(extraction_parent, extraction_results, observed_lock_paths)
    with zipfile.ZipFile(str(complete_zip)) as archive:
        extract_site_packages(archive, extraction_target, compile_pyc=False, force=True)
    assert (
        extraction_target / "site-packages" / "recovered.py"
    ).read_text() == "VALUE = 'RECOVERED'\n"


    # A compiler returning False is a real extraction error.  Neither a fresh
    # extraction nor a forced replacement may publish its partially compiled tree.
    syntax_zip = tmp_path / "syntax-error.zip"
    with zipfile.ZipFile(syntax_zip, "w") as archive:
        archive.writestr("site-packages/bad.py", "def broken syntax!\n")
    for existing in (False, True):
        target = extraction_parent / ("compile-existing" if existing else "compile-fresh")
        if existing:
            with zipfile.ZipFile(complete_zip) as archive:
                extract_site_packages(archive, target)
            extraction_results.add(target)
        previous = ({path.relative_to(target).as_posix(): path.read_bytes()
                     for path in target.rglob("*") if path.is_file()} if existing else None)
        with zipfile.ZipFile(syntax_zip) as archive:
            with pytest.raises(RuntimeError):
                extract_site_packages(archive, target, compile_pyc=True, force=existing)
        if existing:
            assert {path.relative_to(target).as_posix(): path.read_bytes()
                    for path in target.rglob("*") if path.is_file()} == previous
        else:
            assert not target.exists()
        assert_no_unusable_residue(extraction_parent, extraction_results, observed_lock_paths)

    # Interrupt a real replacement immediately after the previous result moves
    # aside. The next call must restore that exact tree, including local changes,
    # before deciding whether an existing cache can be reused.
    recovery_target = extraction_parent / "interrupted"
    with zipfile.ZipFile(complete_zip) as archive:
        extract_site_packages(archive, recovery_target)
    extraction_results.add(recovery_target)
    (recovery_target / "site-packages/recovered.py").write_text("VALUE = 'PRIOR_LOCAL_BYTES'\n")
    previous = {path.relative_to(recovery_target).as_posix(): path.read_bytes()
                for path in recovery_target.rglob("*") if path.is_file()}
    program = r"""
import os, sys, zipfile
from pathlib import Path
from shiv.bootstrap import extract_site_packages
target = Path(sys.argv[2])
def interrupt_move(original):
    def move(source, destination, *args, **kwargs):
        displacing = Path(source).absolute() == target.absolute()
        result = original(source, destination, *args, **kwargs)
        if displacing:
            os._exit(86)
        return result
    return move
os.rename = interrupt_move(os.rename)
os.replace = interrupt_move(os.replace)
with zipfile.ZipFile(sys.argv[1]) as archive:
    extract_site_packages(archive, target, force=True)
"""
    interrupted = run_isolated(
        [sys.executable, "-c", program, str(complete_zip), str(recovery_target)],
        capture_output=True, text=True, timeout=15,
    )
    assert interrupted.returncode in (0, 86), interrupted.stdout + interrupted.stderr
    if interrupted.returncode == 86:
        # A complete backup and an incomplete replacement can coexist after an
        # interruption. Directory existence alone does not make the latter valid.
        (recovery_target / "site-packages").mkdir(parents=True, exist_ok=True)
        (recovery_target / "site-packages/partial.py").write_text("PARTIAL = True\n")
        with zipfile.ZipFile(incomplete_zip) as archive:
            extract_site_packages(archive, recovery_target, force=False)
        assert {path.relative_to(recovery_target).as_posix(): path.read_bytes()
                for path in recovery_target.rglob("*") if path.is_file()} == previous
    else:
        # Implementations with a single atomic exchange need no displacement
        # gap. They may finish normally, but must leave the complete new result.
        assert (recovery_target / "site-packages/recovered.py").read_text() == "VALUE = 'RECOVERED'\n"
    assert_no_unusable_residue(extraction_parent, extraction_results, observed_lock_paths)


def test_f2p_012_filelock_bounded_acquisition(tmp_path):
    """F2P-012: bounded lock acquisition and recovery after timeout.

    The instruction requires a finite timeout and polling interval, a timed-out
    acquisition that raises ``TimeoutError`` naming the lock path, and no effect
    on a later acquisition.  The pre-migration ``FileLock`` accepts neither
    parameter, so it cannot provide a bounded wait.

    Locking matters because two builds of one output path must not interleave.
    The artifact that proves it belongs to F2P-011, which owns archive
    publication; this node owns the lock object itself.
    """
    lock_file = tmp_path / "bounded.lock"
    holder = FileLock(str(lock_file))
    waiter = FileLock(str(lock_file), timeout=0.05, poll_interval=0.005)

    with holder:
        assert holder.is_locked
        started = time.monotonic()
        with pytest.raises(TimeoutError) as error:
            waiter.acquire()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"the bounded acquisition must return promptly, took {elapsed:.3f}s"
        assert str(lock_file) in str(error.value), (
            f"the timeout error must identify the lock path: {error.value}"
        )
        assert not waiter.is_locked, "a timed-out acquisition must not hold the lock"

    # The timeout must not poison a later acquisition.
    with waiter:
        assert waiter.is_locked
    assert not waiter.is_locked


def test_f2p_013_info_reports_derived_state(tmp_path, baseline_envs):
    """F2P-013: both real shiv-info views express the archive's interpreter and compression.

    The instruction requires the text and JSON views to describe the same archive
    state including the interpreter and the compression state.  The pre-migration
    implementation reports neither, so this is a target requirement rather than a
    preserved behaviour, and it is checked on real CLI output instead of on a
    synthetic dictionary.

    Both defined representations are accepted (a canonical interpreter field or an
    accurate shebang line; a boolean ``compressed`` flag or an accurate
    ``compression`` name).  Omitting the facts in either view, expressing only one
    of them, or naming a different interpreter/compression fails.
    """
    sp = tmp_path / "sp_derived"
    (sp / "hello").mkdir(parents=True)
    (sp / "hello" / "__init__.py").write_text("def main():\n    print('hello world')\n")
    build_env = {**os.environ, "SOURCE_DATE_EPOCH": "1700000000"}
    python = baseline_envs.target_python

    for flag, expected_compressed in (("--compressed", True), ("--uncompressed", False)):
        pyz = tmp_path / f"derived_{flag.strip('-')}.pyz"
        build = run_isolated(
            [str(python), "-m", "shiv", "-o", str(pyz), "-p", str(python),
             "--site-packages", str(sp), "-e", "hello:main", flag,
             "--reproducible", "--no-modify"],
            capture_output=True, text=True, env=build_env,
        )
        assert build.returncode == 0, f"{flag} build: {build.stderr[-800:]}"

        state = archive_state(pyz)
        assert state["compressed"] is expected_compressed, (
            f"{flag}: the archive's own compression must follow the flag: {state['compress_types']}"
        )

        json_proc = run_isolated(
            [str(python), "-c", "from shiv.info import main; main()", "--json", str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert json_proc.returncode == 0, f"{flag} json: {json_proc.stderr[-800:]}"
        data = json.loads(json_proc.stdout)

        interpreter, reason = reported_interpreter(data)
        assert reason is None, f"{flag}: the JSON view must report the interpreter: {reason}"
        assert interpreter == state["interpreter"], (
            f"{flag}: the JSON interpreter must be the archive shebang: "
            f"{interpreter!r} != {state['interpreter']!r}"
        )
        compressed, reason = reported_compression(data)
        assert reason is None, f"{flag}: the JSON view must report compression: {reason}"
        assert compressed is state["compressed"], (
            f"{flag}: the JSON compression must match the members: {compressed!r}"
        )

        text_proc = run_isolated(
            [str(python), "-c", "from shiv.info import main; main()", str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert text_proc.returncode == 0, f"{flag} text: {text_proc.stderr[-800:]}"
        occurrences = parse_info_text(text_proc.stdout)
        assert text_state_mismatches(occurrences, state, require_derived=True) == [], (
            f"{flag}: the text view must express interpreter and compression: "
            f"{text_state_mismatches(occurrences, state, require_derived=True)}"
        )

        # Dropping either fact is a failure, so the check cannot be satisfied by
        # a report that merely omits the fields.
        without_interpreter = {
            key: value for key, value in data.items() if key not in INTERPRETER_FIELDS
        }
        assert state_mismatches(without_interpreter, state, require_derived=True), (
            f"{flag}: a JSON view without the interpreter must fail"
        )
        without_compression = {
            key: value for key, value in data.items() if key not in ("compressed", "compression")
        }
        assert state_mismatches(without_compression, state, require_derived=True), (
            f"{flag}: a JSON view without the compression state must fail"
        )
        assert state_mismatches(
            {**data, "interpreter": state["interpreter"] + "-other"}, state, require_derived=True
        ), f"{flag}: a wrong interpreter must fail"
        wrong_compression = dict(data)
        if "compressed" in wrong_compression:
            wrong_compression["compressed"] = not state["compressed"]
        else:
            wrong_compression["compression"] = "stored" if state["compressed"] else "deflated"
        assert state_mismatches(wrong_compression, state, require_derived=True), (
            f"{flag}: a wrong compression state must fail"
        )

        # The same omissions in the text view.
        text_without_interpreter = [
            (label, raw) for label, raw in occurrences if label not in INTERPRETER_FIELDS
        ]
        assert text_state_mismatches(text_without_interpreter, state, require_derived=True), (
            f"{flag}: a text view without the interpreter must fail"
        )
        text_without_compression = [
            (label, raw) for label, raw in occurrences
            if label not in ("compressed", "compression")
        ]
        assert text_state_mismatches(text_without_compression, state, require_derived=True), (
            f"{flag}: a text view without the compression state must fail"
        )


    # Reading metadata and the shebang from separate opens can mix two complete
    # archives when a concurrent build replaces the path. Simulate that rename
    # at a deterministic point and require a report for one complete version.
    if os.name == "posix":
        for as_json in (False, True):
            output = tmp_path / ("snapshot-json.pyz" if as_json else "snapshot-text.pyz")
            replacement = tmp_path / "snapshot-replacement.pyz"
            create_archive([], output, "/old/python", "_bootstrap:bootstrap",
                           Environment("2020-01-01 00:00:00", "test", build_id="OLD_SNAPSHOT"), compressed=False)
            create_archive([], replacement, "/new/python", "_bootstrap:bootstrap",
                           Environment("2021-01-01 00:00:00", "test", build_id="NEW_SNAPSHOT"), compressed=True)
            allowed = [archive_state(output), archive_state(replacement)]
            real_zipfile = zipfile.ZipFile

            class ReplacingZipFile(real_zipfile):
                def __enter__(self):
                    archive = super().__enter__()
                    if replacement.exists():
                        os.replace(replacement, output)
                    return archive

            with mock.patch("shiv.info.zipfile.ZipFile", ReplacingZipFile):
                response = CliRunner().invoke(info_main, (["--json"] if as_json else []) + [str(output)])
            assert response.exit_code == 0, response.output
            if as_json:
                document = json.loads(response.output)
                matches = [not state_mismatches(document, state, require_derived=True) for state in allowed]
            else:
                occurrences = parse_info_text(response.output)
                matches = [not text_state_mismatches(occurrences, state, require_derived=True) for state in allowed]
            assert any(matches), "shiv-info mixed metadata from different archive versions: " + response.output


def test_f2p_014_launcher_returns_script_status(tmp_path, baseline_envs):
    """F2P-014: a successful console-script launcher run returns the script's own status.

    The pre-migration bootstrap hands the ``runpy`` result to ``sys.exit()``, so a
    successful launcher ends status 1 and prints the returned globals on stderr.
    The instruction requires the archive to return the launcher's status while
    keeping its output channels intact, so this is a target requirement.

    The success path must return 0 without the wrapper artifact; a failing
    launcher must still return 33 with its diagnostic on stderr.
    """
    if os.name == "nt":
        record_unverified_coverage(
            "F2P-014",
            "POSIX launcher execution cannot be exercised on this platform.",
        )
        return

    sp = tmp_path / "sp_launcher_status"
    (sp / "bin").mkdir(parents=True)
    script = sp / "bin" / "demo"
    script.write_text(
        "#!/usr/bin/env python\n"
        "import sys\n"
        "if '--fail' in sys.argv:\n"
        "    sys.stderr.write('UNIX_SCRIPT_FAILED\\n')\n"
        "    sys.exit(33)\n"
        "print('BIN_DEMO_OK')\n"
        "print('FORWARDED_ARGS:' + ','.join(sys.argv[1:]))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    python = baseline_envs.target_python
    pyz = tmp_path / "launcher_status.pyz"
    build = run_isolated(
        [str(python), "-m", "shiv", "-o", str(pyz),
         "--site-packages", str(sp), "-c", "demo"],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr[-800:]

    normal = run_isolated(
        [str(python), str(pyz), "arg1", "--flag=val"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert normal.returncode == 0, (
        f"a successful launcher must return its own status 0, got {normal.returncode}: "
        f"{normal.stderr[-400:]!r}"
    )
    assert "BIN_DEMO_OK" in normal.stdout
    assert "FORWARDED_ARGS:arg1,--flag=val" in normal.stdout
    assert "'__name__': '__main__'" not in normal.stderr, (
        f"a successful launcher must not expose the wrapper's returned globals: {normal.stderr[-400:]!r}"
    )
    assert normal.stderr.strip() == "", (
        f"a successful launcher must not write to stderr: {normal.stderr[-400:]!r}"
    )

    failing = run_isolated(
        [str(python), str(pyz), "--fail"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert failing.returncode == 33, (
        f"a failing launcher must return 33, got {failing.returncode}"
    )
    assert "UNIX_SCRIPT_FAILED" in failing.stderr
    assert failing.stdout.strip() == ""

def test_f2p_007_target_rejects_equals_disallowed_options(tmp_path, baseline_envs):
    """F2P-007: target rejects equals-form disallowed pip options."""
    for option in ("--target=foo", "--editable=foo", "--prefix=foo"):
        output = tmp_path / (option[2:].replace("=", "_") + ".pyz")
        source = run_isolated([str(baseline_envs.source_python), "-m", "shiv", "-o", str(output), option], capture_output=True, text=True)
        target = run_isolated([str(baseline_envs.target_python), "-m", "shiv", "-o", str(output), option], capture_output=True, text=True)
        assert target.returncode == 1
        assert "disallowed pip argument" in target.stdout + target.stderr
        assert source.returncode == 0 or "Pip install failed" in source.stdout + source.stderr


def test_p2p_045_posix_launcher_contract(tmp_path, baseline_envs):
    """P2P-045: a bin launcher archive forwards arguments, streams, and its exit status.

    Coverage ownership: POSIX launcher execution.  The archive is built from a
    package whose only entry is an executable ``bin/demo`` script; no metadata
    declares the name.  Both sides build and run their own archive, so the node
    shows agreement instead of one interpreter's behaviour.  Windows uses
    generated ``.exe`` shims and is not covered here; on such a platform the gap
    is recorded instead of being reported as a pass.

    The archive must return the launcher's own status; that requirement is the
    target gate F2P-014 because the pre-migration bootstrap returns the runpy
    result instead.  This node keeps the parts that are preserved behaviour:
    arguments, output channels, and the failing launcher's status and stream.
    """
    if os.name == "nt":
        record_unverified_coverage(
            "P2P-045",
            "POSIX launcher execution cannot be exercised on this platform; "
            "Windows launchers need their own environment and evidence.",
        )
        return

    sp = tmp_path / "sp_launcher"
    bin_dir = sp / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "demo"
    script.write_text(
        "#!/usr/bin/env python\n"
        "import sys\n"
        "if '--fail' in sys.argv:\n"
        "    sys.stderr.write('UNIX_SCRIPT_FAILED\\n')\n"
        "    sys.exit(33)\n"
        "print('BIN_DEMO_OK')\n"
        "print('FORWARDED_ARGS:' + ','.join(sys.argv[1:]))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        pyz = tmp_path / f"launcher_{side}.pyz"
        build = run_isolated(
            [str(python), "-m", "shiv", "-o", str(pyz),
             "--site-packages", str(sp), "-c", "demo"],
            capture_output=True, text=True,
        )
        assert build.returncode == 0, f"{side} build: {build.stderr[-800:]}"

        normal = run_isolated(
            [str(python), str(pyz), "arg1", "--flag=val"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert "BIN_DEMO_OK" in normal.stdout, (
            f"{side}: the launcher must run and write to stdout: {normal.stdout!r}"
        )
        assert "FORWARDED_ARGS:arg1,--flag=val" in normal.stdout, (
            f"{side}: launcher arguments must keep their order and spelling: {normal.stdout!r}"
        )
        assert "UNIX_SCRIPT_FAILED" not in normal.stdout + normal.stderr
        # The launcher ran and forwarded its arguments on both sides.  Its exact
        # success status is *not* asserted here: the pre-migration bootstrap hands
        # the runpy result to sys.exit(), so a successful script ends 1 and prints
        # the returned globals.  F2P-014 is the gate that requires the archive to
        # return the launcher's own status; a 1 is only tolerated here when that
        # documented artifact is actually present, so an ordinary failure cannot
        # pass silently.
        assert normal.returncode in (0, 1), (
            f"{side}: a successful launcher run must not report an unrelated status, "
            f"got {normal.returncode}"
        )
        if normal.returncode == 1:
            assert "'__name__': '__main__'" in normal.stderr, (
                f"{side}: status 1 is only tolerated as the documented wrapper artifact; "
                f"a bare failure status is not acceptable: {normal.stderr[-400:]!r}"
            )

        failing = run_isolated(
            [str(python), str(pyz), "--fail"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert failing.returncode == 33, (
            f"{side}: the launcher's status must reach the caller unchanged, "
            f"got {failing.returncode}"
        )
        assert "UNIX_SCRIPT_FAILED" in failing.stderr, (
            f"{side}: the launcher's diagnostic must stay on stderr: {failing.stderr!r}"
        )
        assert failing.stdout.strip() == "", (
            f"{side}: a failing launcher must not write its diagnostic to stdout: {failing.stdout!r}"
        )


def test_p2p_046_missing_console_script(tmp_path, baseline_envs):
    """P2P-046: an undeclared console script fails with status 1 and leaves no archive."""
    sp = tmp_path / "sp_named"
    bin_dir = sp / "bin"
    bin_dir.mkdir(parents=True)
    other = bin_dir / "present_tool"
    other.write_text("#!/usr/bin/env python\nprint('PRESENT_TOOL_RAN')\n")
    other.chmod(other.stat().st_mode | stat.S_IEXEC)
    dist = sp / "named-1.0.dist-info"
    dist.mkdir()
    (dist / "entry_points.txt").write_text("[console_scripts]\npresent_tool = present_tool:run\n")

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        output = tmp_path / f"missing_name_{side}.pyz"
        assert not output.exists()
        proc = run_isolated(
            [str(python), "-m", "shiv", "-o", str(output),
             "--site-packages", str(sp), "-c", "nonexistent_cmd"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc.returncode == 1, (
            f"{side}: a missing console script is status 1, got {proc.returncode}"
        )
        assert "No entry point 'nonexistent_cmd' found" in proc.stderr, (
            f"{side}: the diagnostic must name the requested script: {proc.stderr!r}"
        )
        assert proc.stdout.strip() == "", f"{side}: no success output may be produced: {proc.stdout!r}"
        assert not output.exists(), f"{side}: a failed build must not leave an archive behind"
        assert "PRESENT_TOOL_RAN" not in proc.stdout + proc.stderr


def test_p2p_051_metadata_precedes_launcher(tmp_path, baseline_envs):
    """P2P-051: a metadata entry point wins over a same-named executable launcher."""
    sp = tmp_path / "sp_priority"
    bin_dir = sp / "bin"
    bin_dir.mkdir(parents=True)
    (sp / "ep_mod.py").write_text("def main():\n    print('METADATA_PRIORITY_OK')\n")
    launcher = bin_dir / "prio"
    launcher.write_text("#!/usr/bin/env python\nprint('BIN_PRIO_OK')\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    dist = sp / "priority-1.0.dist-info"
    dist.mkdir()
    (dist / "entry_points.txt").write_text("[console_scripts]\nprio = ep_mod:main\n")

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        pyz = tmp_path / f"priority_{side}.pyz"
        build = run_isolated(
            [str(python), "-m", "shiv", "-o", str(pyz),
             "--site-packages", str(sp), "-c", "prio"],
            capture_output=True, text=True,
        )
        assert build.returncode == 0, f"{side} build: {build.stderr[-800:]}"
        run = run_isolated(
            [str(python), str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert run.returncode == 0, f"{side} run: {run.stderr[-800:]}"
        assert "METADATA_PRIORITY_OK" in run.stdout, (
            f"{side}: the declared metadata entry point must be used: {run.stdout!r}"
        )
        assert "BIN_PRIO_OK" not in run.stdout, (
            f"{side}: the same-named launcher must not be selected instead: {run.stdout!r}"
        )


def test_p2p_052_console_script_name_boundaries(tmp_path, baseline_envs):
    """P2P-052: path-like and unsupported-platform script names are rejected, never redirected."""
    sp = tmp_path / "sp_names"
    bin_dir = sp / "bin"
    bin_dir.mkdir(parents=True)
    real = bin_dir / "real_tool"
    real.write_text("#!/usr/bin/env python\nprint('REAL_TOOL_RAN')\n")
    real.chmod(real.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "demo_only.exe").write_text("#!/bin/sh\necho DEMO_ONLY_RAN\n")

    names = ["../real_tool", "bin/real_tool"]
    if os.name != "nt":
        # A .exe shim is not a runnable launcher here, so the name must be
        # rejected rather than redirected to it.
        names.append("demo_only")

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        for name in names:
            output = tmp_path / f"reject_{side}_{name.replace('/', '_').replace('.', '_')}.pyz"
            assert not output.exists()
            proc = run_isolated(
                [str(python), "-m", "shiv", "-o", str(output),
                 "--site-packages", str(sp), "-c", name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert proc.returncode == 1, (
                f"{side} {name!r}: an unusable script name is status 1, got {proc.returncode}"
            )
            assert name in proc.stderr, (
                f"{side} {name!r}: the diagnostic must name the requested value: {proc.stderr!r}"
            )
            assert not output.exists(), (
                f"{side} {name!r}: a rejected name must not produce an archive"
            )
            assert "REAL_TOOL_RAN" not in proc.stdout + proc.stderr
            assert "DEMO_ONLY_RAN" not in proc.stdout + proc.stderr

@dataclass
class BaselineEnvs:
    source_python: Path
    target_python: Path
    source_repo: Path
    target_repo: Path
    manifest: dict


_CACHED_BASELINE_ENVS: Optional[BaselineEnvs] = None


def get_baseline_envs() -> BaselineEnvs:
    """Construct or reuse isolated source (Click 6.7) and target (Click 8.1.7) virtualenvs.

    Returns source Python, target Python, source repo, target repo, and verification manifest.
    """
    global _CACHED_BASELINE_ENVS
    if _CACHED_BASELINE_ENVS is not None:
        if _CACHED_BASELINE_ENVS.source_python.exists() and _CACHED_BASELINE_ENVS.target_python.exists():
            return _CACHED_BASELINE_ENVS

    repo_root = Path(__file__).resolve().parents[1]
    base_commit = "ff542cbe75ea832df3a989d07c7fdf5214f727fa"
    p_check = run_isolated(["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=str(repo_root), capture_output=True)
    if p_check.returncode != 0:
        raise RuntimeError(
            f"Base commit {base_commit} cannot be resolved in git repository {repo_root}. "
            "Cannot construct source baseline without the required base commit."
        )

    base_tmp = Path(tempfile.gettempdir()) / f"shiv_p2p_baseline_{base_commit[:8]}"
    base_tmp.mkdir(parents=True, exist_ok=True)

    def _make_or_get_venv(name: str, click_version: str, install_repo: Path) -> Tuple[Path, dict]:
        v_dir = base_tmp / name
        py = v_dir / ("Scripts" if sys.platform == "win32" else "bin") / ("python.exe" if sys.platform == "win32" else "python")
        manifest_file = v_dir / "manifest.json"
        if py.exists() and manifest_file.exists():
            try:
                m = json.loads(manifest_file.read_text(encoding="utf-8"))
                if (
                    m.get("base_commit") == base_commit
                    and m.get("click_version") == click_version
                    and m.get("install_repo") == str(install_repo.resolve())
                    and m.get("sys_executable") == str(Path(sys.executable).resolve())
                ):
                    check_p = run_isolated([str(py), "-c", "import click, shiv, json; print(json.dumps([click.__version__, click.__file__, shiv.__file__]))"], capture_output=True, text=True)
                    if check_p.returncode == 0:
                        version, click_file, shiv_file = json.loads(check_p.stdout)
                        if (version == click_version
                                and v_dir.resolve() in Path(click_file).resolve().parents
                                and Path(shiv_file).resolve() == (install_repo / "src/shiv/__init__.py").resolve()):
                            m["shiv_file"] = shiv_file
                            m["click_file"] = click_file
                            return py, m
            except Exception:
                pass

        shutil.rmtree(str(v_dir), ignore_errors=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(v_dir)], env=isolated_python_env())
        subprocess.check_call([str(py), "-m", "pip", "install", f"click=={click_version}", "setuptools"], env=isolated_python_env())
        subprocess.check_call([str(py), "-m", "pip", "install", "--no-deps", "-e", str(install_repo)], env=isolated_python_env())
        chk = run_isolated([str(py), "-c", "import click, shiv, json; print(json.dumps([click.__version__, click.__file__, shiv.__file__]))"], capture_output=True, text=True, check=True)
        version, click_file, shiv_file = json.loads(chk.stdout)
        assert version == click_version
        assert v_dir.resolve() in Path(click_file).resolve().parents
        assert Path(shiv_file).resolve() == (install_repo / "src/shiv/__init__.py").resolve()
        manifest_data = {
            "base_commit": base_commit,
            "click_version": click_version,
            "install_repo": str(install_repo.resolve()),
            "sys_executable": str(Path(sys.executable).resolve()),
            "shiv_file": shiv_file,
            "click_file": click_file,
        }
        manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        return py, manifest_data

    # 1. Source repo checkout from base commit
    src_repo = base_tmp / "src_repo"
    if not (src_repo / "setup.cfg").exists():
        src_repo.mkdir(parents=True, exist_ok=True)
        archive_tar = base_tmp / "base.tar"
        with open(archive_tar, "wb") as f:
            subprocess.check_call(["git", "archive", base_commit], stdout=f, cwd=str(repo_root))
        with tarfile.open(archive_tar) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=str(src_repo), filter="data")
            else:
                tar.extractall(path=str(src_repo))

    src_cfg_text = (src_repo / "setup.cfg").read_text(encoding="utf-8")
    assert "click>=6.7,!=7.0" in src_cfg_text, "Source baseline setup.cfg must declare click>=6.7,!=7.0"

    src_py, src_m = _make_or_get_venv("v_src", "6.7", src_repo)
    tgt_py, tgt_m = _make_or_get_venv("v_tgt", "8.1.7", repo_root)

    _CACHED_BASELINE_ENVS = BaselineEnvs(
        source_python=src_py,
        target_python=tgt_py,
        source_repo=src_repo,
        target_repo=repo_root,
        manifest={"source": src_m, "target": tgt_m},
    )
    return _CACHED_BASELINE_ENVS


@pytest.fixture(scope="module")
def baseline_envs() -> BaselineEnvs:
    return get_baseline_envs()


@pytest.mark.parametrize("scenario", [
    "explicit_override", "source_order", "three_source_order", "single_source",
    "pip_mixed",
])
def test_p2p_015_multiple_site_packages_merge(tmp_path, runner, hello_wheel, scenario):
    """P2P-015: source order determines effective bytes and permissions.

    Retained copies may be any ordered contributor subsequence ending in the
    final contributor. Non-conflicting files and package metadata must survive,
    and neither extra copies nor stale effective permissions are permitted.
    The three-source case also exercises equal contributors, keeping the five
    collected cases aligned with the frozen source observation.
    """
    sources = [tmp_path / ("sp_" + key) for key in ("a", "b", "c")]
    for source, key, mode in zip(sources, ("A", "B", "C"), (0o640, 0o740, 0o750)):
        source.mkdir()
        (source / ("unique_" + key.lower() + ".py")).write_text("ID = %r\n" % (key * 3))
        (source / "common.py").write_text("TAG = 'from_%s'\n" % key)
        metadata = source / "demo-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nX-Source: %s\n" % key
        )
        (metadata / "entry_points.txt").write_text("[console_scripts]\ndemo = from_%s:main\n" % key)
        if os.name == "posix":
            for member in source.rglob("*"):
                if member.is_file():
                    member.chmod(mode)

    if scenario == "pip_mixed":
        explicit = tmp_path / "sp_explicit"
        explicit.mkdir()
        (explicit / "explicit_only.py").write_text("EXPLICIT = 1\n")
        if os.name == "posix":
            (explicit / "explicit_only.py").chmod(0o750)
        pyz = tmp_path / "merged_pip.pyz"
        result = runner(["-o", str(pyz), "--site-packages", str(explicit), str(hello_wheel)])
        assert result.exit_code == 0, result.output
        names = assert_source_merge(pyz, [explicit])
        with zipfile.ZipFile(pyz) as archive:
            assert archive.read("site-packages/hello/__init__.py") == b"def main():\n    print('hello world')\n"
        assert any(name.startswith("site-packages/hello-1.0.0.dist-info/") for name in names), names
    else:
        order = {
            "explicit_override": (0, 1), "source_order": (1, 0),
            "three_source_order": (0, 1, 2), "single_source": (0,),
            "identical_contributors": (0, 1, 2),
        }[scenario]
        selected = [sources[index] for index in order]
        variants = [scenario]
        if scenario == "three_source_order":
            variants.append("identical_contributors")
        for variant in variants:
            if variant == "identical_contributors":
                # Equal contributions must be matched by their possible positions,
                # not always attributed to the first equal source.
                (sources[1] / "common.py").write_bytes((sources[0] / "common.py").read_bytes())
                if os.name == "posix":
                    (sources[1] / "common.py").chmod(stat.S_IMODE((sources[0] / "common.py").stat().st_mode))
            pyz = tmp_path / (variant + ".pyz")
            arguments = ["-o", str(pyz)]
            for source in selected:
                arguments.extend(["--site-packages", str(source)])
            result = runner(arguments)
            assert result.exit_code == 0, result.output
            names = assert_source_merge(pyz, selected)
            assert "_bootstrap/__init__.py" in names and "environment.json" in names
    assert "_bootstrap/__init__.py" in names and "environment.json" in names


@pytest.mark.parametrize("scenario", ["with_site_packages", "with_wheel", "with_pythonpath"])
def test_p2p_016_no_entry_point_bootstrap_stdin(tmp_path, isolated_runner, hello_sp, hello_wheel, scenario, runtime_python):
    """P2P-016: When no entry point is provided, pyz drops into bootstrap and accepts code from stdin."""
    pyz = tmp_path / f"no_ep_{scenario}.pyz"
    if scenario == "with_wheel":
        res = isolated_runner(["-o", str(pyz), str(hello_wheel)])
    else:
        res = isolated_runner(["-o", str(pyz), "--site-packages", str(hello_sp)])
    assert res.exit_code == 0, res.output

    env = os.environ.copy()
    if scenario == "with_pythonpath":
        env["PYTHONPATH"] = str(tmp_path / "extra_dir")

    proc = run_isolated(
        [str(runtime_python), str(pyz)],
        input="import hello; print('HELLO_STDIN_OK')\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "HELLO_STDIN_OK" in proc.stdout


@pytest.mark.parametrize("compile_pyc", [True, False])
def test_p2p_017_compile_pyc(tmp_path, isolated_runner, compile_pyc, runtime_python):
    """P2P-017: --compile-pyc verifies pyc compilation cache in runtime directory."""
    sp = tmp_path / f"sp_compile_{compile_pyc}"
    sp.mkdir()
    nested_pkg = sp / "nested_pkg"
    nested_pkg.mkdir()
    (nested_pkg / "__init__.py").write_text("MSG = 'nested_ok'\n")
    (nested_pkg / "worker.py").write_text("import nested_pkg\ndef run():\n    print(nested_pkg.MSG)\n")

    pyz = tmp_path / f"pyc_{compile_pyc}.pyz"
    args = ["-o", str(pyz), "--site-packages", str(sp), "-e", "nested_pkg.worker:run"]
    if compile_pyc:
        args.append("--compile-pyc")
    res = isolated_runner(args)
    assert res.exit_code == 0, res.output

    # Check directly via extract_site_packages before running to verify compileall behavior without execution
    with zipfile.ZipFile(str(pyz)) as zf:
        ext_target = tmp_path / f"ext_direct_{compile_pyc}"
        extract_site_packages(zf, ext_target, compile_pyc=compile_pyc)
        ext_pycs = list(ext_target.rglob("*.pyc"))
        if compile_pyc:
            assert len(ext_pycs) > 0, "extract_site_packages must pre-compile pyc when compile_pyc=True"
        else:
            assert len(ext_pycs) == 0, "extract_site_packages must not compile pyc when compile_pyc=False"

    shiv_root = tmp_path / f"r_{int(compile_pyc)}"
    env = {
        **os.environ,
        "SHIV_ROOT": str(shiv_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "nested_ok" in proc.stdout

    # Check extracted directory for pyc files
    pyc_files = list(shiv_root.rglob("*.pyc"))
    if compile_pyc:
        assert len(pyc_files) > 0, "Expected compiled pyc files in cache directory"
    else:
        assert len(pyc_files) == 0, "Expected no compiled pyc files when --compile-pyc is disabled"


@pytest.mark.parametrize("compressed_flag", ["--compressed", "--uncompressed"])
def test_p2p_018_compressed_uncompressed(tmp_path, isolated_runner, hello_sp, compressed_flag, runtime_python):
    """P2P-018: --compressed and --uncompressed select the archive's compression and nothing else.

    Coverage ownership: this node owns the *compression* state of the published
    archive (``instruction.md`` 38/39).  It reads the compression type of every
    member of the archive that was actually built, so an implementation that
    accepts the flag but ignores it cannot pass.  P2P-050 owns the environment
    metadata document, and P2P-032 owns the shebang interpreter.
    """
    pyz = tmp_path / f"archive_{compressed_flag.strip('-')}.pyz"
    res = isolated_runner(["-o", str(pyz), "-e", "hello:main", "--site-packages", str(hello_sp), compressed_flag])
    assert res.exit_code == 0, res.output
    with zipfile.ZipFile(str(pyz)) as archive:
        assert archive.testzip() is None
        infos = archive.infolist()
        assert infos, "the archive must contain members"
        expected = zipfile.ZIP_DEFLATED if compressed_flag == "--compressed" else zipfile.ZIP_STORED
        observed = sorted({info.compress_type for info in infos})
        assert observed == [expected], (
            f"{compressed_flag} must select compression type {expected} for every member, got {observed}"
        )
        # The flag must affect compression only: the payload and the metadata
        # document must still be present and readable.
        assert "__main__.py" in [info.filename for info in infos]
        assert "environment.json" in [info.filename for info in infos]
        import json as _json

        recorded = _json.loads(archive.read("environment.json"))
        assert recorded["entry_point"] == "hello:main"

    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert proc.returncode == 0
    assert "hello world" in proc.stdout
@pytest.mark.parametrize("flag", ["--extend-pythonpath", "-E", "none"])
def test_p2p_019_extend_pythonpath(tmp_path, isolated_runner, flag, runtime_python):
    """P2P-019: --extend-pythonpath and -E expose extraction directory to PYTHONPATH, preserving existing path order."""
    sp = tmp_path / "sp_env"
    sp.mkdir()
    (sp / "env_check.py").write_text(
        "import os, subprocess, sys\n"
        "def main():\n"
        "    pp = os.environ.get('PYTHONPATH', '')\n"
        "    print('PP:' + pp)\n"
    )
    pyz = tmp_path / f"pp_{flag}.pyz"
    args = ["-o", str(pyz), "--site-packages", str(sp), "-e", "env_check:main"]
    if flag != "none":
        args.append(flag)
    res = isolated_runner(args)
    assert res.exit_code == 0, res.output

    initial_pp = f"existing_p1{os.pathsep}existing_p2"
    env = {**os.environ, "PYTHONPATH": initial_pp}
    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True, env=env)
    assert proc.returncode == 0
    output_line = [line for line in proc.stdout.splitlines() if line.startswith("PP:")][0][3:]
    parts = output_line.split(os.pathsep)

    if flag != "none":
        # Existing paths must remain first in order
        assert parts[0] == "existing_p1"
        assert parts[1] == "existing_p2"
        # The site-packages path should be appended
        assert any("site-packages" in p for p in parts[2:])
    else:
        # In disabled mode, new site-packages path is not added to PYTHONPATH
        assert output_line == initial_pp


def test_p2p_020_build_id(tmp_path, isolated_runner, info_runner, hello_sp, runtime_python):
    """P2P-020: Custom build-id is preserved in environment metadata, and default build-id is stable across output paths."""
    custom_id = "test-custom-build-id-12345"
    pyz = tmp_path / "custom_id.pyz"
    res = isolated_runner(["-o", str(pyz), "-e", "hello:main", "--site-packages", str(hello_sp), "--build-id", custom_id])
    assert res.exit_code == 0, res.output

    # 1. Custom build-id in shiv-info and pyz executes successfully
    info_res = info_runner([str(pyz), "--json"])
    assert info_res.exit_code == 0
    data = json.loads(info_res.output)
    assert data["build_id"] == custom_id

    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert proc.returncode == 0
    assert "hello world" in proc.stdout

    # 2. Default build-id is deterministic and independent of output file path
    sp = tmp_path / "sp_bid"
    sp.mkdir()
    (sp / "mod_b.py").write_text("def run(): pass\n")

    pyz_p1 = tmp_path / "bid1.pyz"
    pyz_p2 = tmp_path / "bid2.pyz"
    r1 = isolated_runner(["-o", str(pyz_p1), "--site-packages", str(sp), "-e", "mod_b:run"])
    r2 = isolated_runner(["-o", str(pyz_p2), "--site-packages", str(sp), "-e", "mod_b:run"])
    assert r1.exit_code == 0 and r2.exit_code == 0

    d1 = json.loads(info_runner([str(pyz_p1), "--json"]).output)
    d2 = json.loads(info_runner([str(pyz_p2), "--json"]).output)
    assert len(d1["build_id"]) == 64
    assert d1["build_id"] == d2["build_id"]

    # 3. Content modification changes default build-id
    sp_mod = tmp_path / "sp_bid_mod"
    sp_mod.mkdir()
    (sp_mod / "mod_b.py").write_text("def run(): print('DIFFERENT')\n")
    pyz_mod = tmp_path / "bid_mod.pyz"
    r_mod = isolated_runner(["-o", str(pyz_mod), "--site-packages", str(sp_mod), "-e", "mod_b:run"])
    assert r_mod.exit_code == 0
    d_mod = json.loads(info_runner([str(pyz_mod), "--json"]).output)
    assert d_mod["build_id"] != d1["build_id"], "build_id must change when content changes"

    # 4. Renaming a file changes default build-id (path.relative_to is hashed)
    sp_rename = tmp_path / "sp_bid_rename"
    sp_rename.mkdir()
    (sp_rename / "mod_renamed.py").write_text("def run(): pass\n")
    pyz_rename = tmp_path / "bid_rename.pyz"
    r_rename = isolated_runner(["-o", str(pyz_rename), "--site-packages", str(sp_rename), "-e", "mod_renamed:run"])
    assert r_rename.exit_code == 0
    d_rename = json.loads(info_runner([str(pyz_rename), "--json"]).output)
    assert d_rename["build_id"] != d1["build_id"], "build_id must change when file is renamed"

    # 5. Adding a file changes default build-id
    sp_add = tmp_path / "sp_bid_add"
    sp_add.mkdir()
    (sp_add / "mod_b.py").write_text("def run(): pass\n")
    (sp_add / "extra.py").write_text("EXTRA = 1\n")
    pyz_add = tmp_path / "bid_add.pyz"
    r_add = isolated_runner(["-o", str(pyz_add), "--site-packages", str(sp_add), "-e", "mod_b:run"])
    assert r_add.exit_code == 0
    d_add = json.loads(info_runner([str(pyz_add), "--json"]).output)
    assert d_add["build_id"] != d1["build_id"], "build_id must change when file is added"


@pytest.mark.parametrize("param", ["--site-packages", "--preamble"])
def test_p2p_025_click_path_parameter_validation(tmp_path, param, hello_sp):
    """P2P-025: Nonexistent paths for --site-packages and --preamble exit with status 2, and existing paths pass."""
    # 1. Nonexistent path
    nonexistent = "nonexistent_dir_or_file_404"
    res = CliRunner().invoke(shiv_main, ["-o", str(tmp_path / "err.pyz"), param, nonexistent])
    assert res.exit_code == 2
    assert "does not exist" in res.output
    assert param in res.output
    assert nonexistent in res.output

    # 2. Existing path validation passes CLI path check (including paths with spaces)
    existing = tmp_path / ("existing sp path" if param == "--site-packages" else "existing preamble path.py")
    if param == "--site-packages":
        existing.mkdir()
        (existing / "mod.py").write_text("def run(): pass\n")
        res_ok = CliRunner().invoke(shiv_main, ["-o", str(tmp_path / "ok.pyz"), param, str(existing), "-e", "mod:run"])
    else:
        existing.write_text("#!/usr/bin/env python\n")
        res_ok = CliRunner().invoke(
            shiv_main,
            ["-o", str(tmp_path / "ok.pyz"), "--site-packages", str(hello_sp), param, str(existing), "-e", "hello:main"]
        )
    assert res_ok.exit_code == 0, res_ok.output


def test_p2p_026_unprocessed_tokens_ordering(tmp_path, hello_wheel, isolated_runner, runtime_python):
    """P2P-026: Tokens with spaces, equal signs, and flags are passed to pip unchanged, with real wheel build checks."""
    pyz = tmp_path / "pip_tokens.pyz"
    tokens = ["--timeout", "60", "--retries=5", "package with spaces==1.0"]
    with mock.patch("shiv.pip.install") as mock_install:
        res = CliRunner().invoke(shiv_main, ["-o", str(pyz), "-p", sys.executable] + tokens)
        assert res.exit_code == 0
        called = mock_install.call_args[0][0]
        assert called[0] == "--target"
        assert called[2:] == tokens

    # Real wheel build with pip tokens
    pyz_real = tmp_path / "tokens_real.pyz"
    res_real = isolated_runner(
        ["-o", str(pyz_real), "-p", str(runtime_python), "--timeout", "30", "--retries=2", "--no-index", "--find-links", str(hello_wheel.parent), "hello", "-e", "hello:main"]
    )
    assert res_real.exit_code == 0, res_real.output
    assert pyz_real.exists()
    p_real = run_isolated([str(runtime_python), str(pyz_real)], capture_output=True, text=True)
    assert p_real.returncode == 0
    assert "hello world" in p_real.stdout


# =====================================================================
# Builder / Archive Group
# =====================================================================

def test_p2p_004_build_hello_zipapp_and_execute(tmp_path, isolated_runner, hello_sp, runtime_python):
    """P2P-004: Building hello package and running outputs 'hello world'."""
    pyz = tmp_path / "hello.pyz"
    res = isolated_runner(["-o", str(pyz), "-e", "hello:main", "--site-packages", str(hello_sp)])
    assert res.exit_code == 0, res.output

    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hello world"


def test_p2p_042_info_json_basic(tmp_path, baseline_envs):
    """P2P-042: ``shiv-info --json`` reports the archive's recorded metadata truthfully."""
    sp = tmp_path / "sp_json"
    (sp / "hello").mkdir(parents=True)
    (sp / "hello" / "__init__.py").write_text("def main():\n    print('hello world')\n")
    build_env = {**os.environ, "SOURCE_DATE_EPOCH": "1700000000"}

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        pyz = tmp_path / f"json_{side}.pyz"
        build = run_isolated(
            [str(python), "-m", "shiv", "-o", str(pyz), "-p", str(python),
             "--site-packages", str(sp), "-e", "hello:main",
             "--build-id", "JSON-BASIC", "--reproducible", "--no-modify"],
            capture_output=True, text=True, env=build_env,
        )
        assert build.returncode == 0, f"{side} build: {build.stderr[-800:]}"

        proc = run_isolated(
            [str(python), "-c", "from shiv.info import main; main()", "--json", str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc.returncode == 0, f"{side} info --json: {proc.stderr[-800:]}"
        assert "Traceback" not in proc.stderr, (
            f"{side}: a successful inspection must not raise: {proc.stderr[-800:]}"
        )

        data = json.loads(proc.stdout)
        assert isinstance(data, dict), f"{side}: the JSON view must be one object"

        # The expectation comes from the archive, not from the report under test.
        recorded = archive_state(pyz)["recorded"]
        missing = sorted(set(recorded) - set(data))
        assert missing == [], f"{side}: the JSON view must report every recorded field: {missing}"
        forged = {
            key: (data[key], value)
            for key, value in recorded.items()
            if not json_equal(data[key], value)
        }
        assert forged == {}, f"{side}: the JSON view must report the recorded values: {forged}"
        assert data["build_id"] == "JSON-BASIC", f"{side}: the requested build id must be reported"
        assert data["entry_point"] == "hello:main", f"{side}: the entry-point mode must be reported"
        assert data["built_at"] == "2023-11-14 22:13:20", (
            f"{side}: SOURCE_DATE_EPOCH must drive the recorded timestamp: {data['built_at']!r}"
        )


def test_p2p_043_info_text_basic(tmp_path, baseline_envs):
    """P2P-043: the text view identifies the archive and binds each recorded field to its label."""
    sp = tmp_path / "sp_text"
    (sp / "hello").mkdir(parents=True)
    (sp / "hello" / "__init__.py").write_text("def main():\n    print('hello world')\n")
    build_env = {**os.environ, "SOURCE_DATE_EPOCH": "1700000000"}

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        pyz = tmp_path / f"text_{side}.pyz"
        build = run_isolated(
            [str(python), "-m", "shiv", "-o", str(pyz), "-p", str(python),
             "--site-packages", str(sp), "-e", "hello:main",
             "--build-id", "TEXT-BASIC", "--reproducible", "--no-modify"],
            capture_output=True, text=True, env=build_env,
        )
        assert build.returncode == 0, f"{side} build: {build.stderr[-800:]}"

        text = run_isolated(
            [str(python), "-c", "from shiv.info import main; main()", str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert text.returncode == 0, f"{side} text: {text.stderr[-800:]}"
        assert str(pyz) in text.stdout, f"{side}: the text view must identify the archive path"

        state = archive_state(pyz)
        occurrences = parse_info_text(text.stdout)
        assert text_state_mismatches(occurrences, state) == [], (
            f"{side}: each recorded value must be bound to its own label: "
            f"{text_state_mismatches(occurrences, state)}"
        )

        # ...and the binding must reject a value that only appears under another
        # label, which is what a whole-output substring search would accept.
        assert text_state_mismatches([("build_id", "WRONG-ID")] + occurrences, state), (
            f"{side}: a wrong value under the field's own label must fail"
        )
        decoded, issues = _decoded_occurrences(occurrences, "build_id", "TEXT-BASIC")
        assert issues == [], f"{side}: the build id must decode cleanly: {issues}"
        assert [value for value in decoded if json_equal(value, "TEXT-BASIC")], (
            f"{side}: the text view must report the recorded build id: {decoded}"
        )

        # Both views must describe the same archive.
        js = run_isolated(
            [str(python), "-c", "from shiv.info import main; main()", "--json", str(pyz)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert js.returncode == 0, f"{side} json: {js.stderr[-800:]}"
        assert json.loads(js.stdout)["build_id"] == "TEXT-BASIC", (
            f"{side}: both views must report the same build id"
        )


def test_p2p_047_info_missing_path(tmp_path, baseline_envs):
    """P2P-047: a missing archive fails with a diagnostic in both output modes."""
    missing = tmp_path / "nonexistent.pyz"
    assert not missing.exists()
    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        for mode in ([], ["--json"]):
            label = f"{side} missing {'json' if mode else 'text'}"
            assert_info_rejection(
                python, missing, mode, "No such file", label
            )


def test_p2p_048_info_plain_zip(tmp_path, baseline_envs):
    """P2P-048: a zip without archive metadata fails with a diagnostic naming the missing metadata."""
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(str(plain), "w") as archive:
        archive.writestr("test.txt", "hello")
    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        for mode in ([], ["--json"]):
            label = f"{side} plain {'json' if mode else 'text'}"
            assert_info_rejection(
                python, plain, mode, "environment.json", label
            )


def test_p2p_049_info_corrupt_zip(tmp_path, baseline_envs):
    """P2P-049: a corrupt archive fails with its own diagnostic, not a false report."""
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a valid zip file header")
    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        for mode in ([], ["--json"]):
            label = f"{side} corrupt {'json' if mode else 'text'}"
            assert_info_rejection(
                python, corrupt, mode, "not a zip file", label
            )


def test_p2p_050_info_complete_state(tmp_path, baseline_envs):
    """P2P-050: one report describes the archive's complete recorded state, type-exactly.

    Coverage ownership: the recorded metadata document and its rendering.  Every
    field the archive's own ``environment.json`` carries must be reported with
    the recorded value and JSON type by both views; the recorded source hashes
    must be the digests of the bytes that were packaged; and a derived state the
    report does express must name the same archive.

    Requiring the derived interpreter/compression fields on *every* report is
    not a preserved source behaviour (the pre-migration implementation omits
    both), so it is not asserted here as if it were a P2P property; the exact
    requirement and its scope are recorded in the case's coverage reasons.
    """
    sp = tmp_path / "sp_state"
    (sp / "hello").mkdir(parents=True)
    (sp / "hello" / "__init__.py").write_text("def main():\n    print('hello world')\n")
    (sp / "hello" / "extra.py").write_text("VALUE = 1\n")
    build_env = {**os.environ, "SOURCE_DATE_EPOCH": "1700000000"}
    root = tmp_path / "state_root"
    scenarios = [("--compressed", True), ("--uncompressed", False)]

    for side in ("source", "target"):
        python = getattr(baseline_envs, f"{side}_python")
        for flag, expected_compressed in scenarios:
            pyz = tmp_path / f"state_{side}_{flag.strip('-')}.pyz"
            build = run_isolated(
                [str(python), "-m", "shiv", "-o", str(pyz), "-p", str(python),
                 "--site-packages", str(sp), "-e", "hello:main", flag,
                 "--build-id", "STATE-ID", "--reproducible", "--no-modify",
                 "--compile-pyc", "-E", "--root", str(root)],
                capture_output=True, text=True, env=build_env,
            )
            assert build.returncode == 0, f"{side} {flag} build: {build.stderr[-800:]}"

            state = archive_state(pyz)
            assert state["compressed"] is expected_compressed, (
                f"{side} {flag}: the archive's own compression state must follow the flag: "
                f"{state['compress_types']}"
            )

            js = run_isolated(
                [str(python), "-c", "from shiv.info import main; main()", "--json", str(pyz)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert js.returncode == 0, f"{side} {flag} json: {js.stderr[-800:]}"
            data = json.loads(js.stdout)
            assert state_mismatches(data, state) == [], (
                f"{side} {flag}: the JSON view must describe the archive exactly: "
                f"{state_mismatches(data, state)}"
            )
            assert data["compile_pyc"] is True, f"{side} {flag}: --compile-pyc must be recorded"
            assert data["extend_pythonpath"] is True, f"{side} {flag}: -E must be recorded"
            assert data["no_modify"] is True, f"{side} {flag}: --no-modify must be recorded"
            assert data["reproducible"] is True, f"{side} {flag}: --reproducible must be recorded"
            assert os.path.normcase(os.path.normpath(str(data["root"]))) == os.path.normcase(
                os.path.normpath(str(root))
            ), f"{side} {flag}: the requested root must be recorded"

            # The recorded hashes are the digests of the packaged bytes, item by
            # item; an empty map, a missing entry, or a fabricated digest fails.
            actual = {
                path.relative_to(sp).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sp.rglob("*.py")
            }
            recorded = {str(key).replace("\\", "/"): value for key, value in data["hashes"].items()}
            assert set(recorded) == set(actual), (
                f"{side} {flag}: recorded hashes must cover exactly the built sources: "
                f"missing {sorted(set(actual) - set(recorded))}, "
                f"unexpected {sorted(set(recorded) - set(actual))}"
            )
            for relative, digest in actual.items():
                assert recorded[relative] == digest, (
                    f"{side} {flag}: the recorded hash for {relative} must be the built digest"
                )
            assert state_mismatches({**data, "hashes": {}}, state), (
                f"{side} {flag}: an empty hash map must be rejected"
            )
            partial = dict(data["hashes"])
            partial.pop(sorted(partial)[0])
            assert state_mismatches({**data, "hashes": partial}, state), (
                f"{side} {flag}: a partial hash map must be rejected"
            )

            text = run_isolated(
                [str(python), "-c", "from shiv.info import main; main()", str(pyz)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert text.returncode == 0, f"{side} {flag} text: {text.stderr[-800:]}"
            occurrences = parse_info_text(text.stdout)
            assert text_state_mismatches(occurrences, state) == [], (
                f"{side} {flag}: the text view must bind every recorded field: "
                f"{text_state_mismatches(occurrences, state)}"
            )

            # A recorded boolean rendered as a number is a different value.
            assert state_mismatches({**data, "compile_pyc": 1}, state), (
                f"{side} {flag}: a recorded boolean reported as 1 must fail"
            )
            assert state_mismatches({**data, "reproducible": 0}, state), (
                f"{side} {flag}: a recorded boolean reported as 0 must fail"
            )

    # The comparator must separate a report carrying another archive's derived
    # state from the archive that was actually built, and must require both
    # derived facts when the contract asks for them.
    compressed_state = archive_state(tmp_path / "state_source_compressed.pyz")
    uncompressed_state = archive_state(tmp_path / "state_source_uncompressed.pyz")
    assert compressed_state["compressed"] is not uncompressed_state["compressed"]
    borrowed = {
        **compressed_state["recorded"],
        "interpreter": uncompressed_state["interpreter"],
        "compressed": uncompressed_state["compressed"],
    }
    assert state_mismatches(borrowed, compressed_state), (
        "a report borrowing the other archive's derived state must fail"
    )
    assert state_mismatches(compressed_state["recorded"], compressed_state, require_derived=True), (
        "when the derived state is required, omitting it must fail"
    )
    assert state_mismatches(
        {**compressed_state["recorded"],
         "interpreter": compressed_state["interpreter"],
         "compressed": compressed_state["compressed"]},
        compressed_state,
        require_derived=True,
    ) == [], "a truthful report must satisfy the required derived state"


def test_p2p_009_packaging_and_execution_pipeline(tmp_path, isolated_runner, info_runner, hello_sp, runtime_python):
    """P2P-009: Full lifecycle of archive creation, inspection, and execution."""
    pyz = tmp_path / "pipeline.pyz"
    res = isolated_runner(["-o", str(pyz), "-e", "hello:main", "--site-packages", str(hello_sp), "--compile-pyc"])
    assert res.exit_code == 0
    assert zipfile.is_zipfile(str(pyz))

    with zipfile.ZipFile(str(pyz)) as z:
        assert "__main__.py" in z.namelist()
        assert "environment.json" in z.namelist()

    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert proc.returncode == 0
    assert "hello world" in proc.stdout


def test_p2p_027_reproducible_build(tmp_path, local_runner):
    """P2P-027: Reproducible builds with fixed SOURCE_DATE_EPOCH produce identical byte hashes and entry metadata."""
    sp = tmp_path / "sp_reprod"
    sp.mkdir()
    (sp / "rep_mod.py").write_text("def run():\n    print('REPRODUCIBLE_OK')\n")

    pyz1 = tmp_path / "reprod1.pyz"
    pyz2 = tmp_path / "reprod2.pyz"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1600000000"}

    res1 = local_runner(["-o", str(pyz1), "--site-packages", str(sp), "-e", "rep_mod:run", "--reproducible", "--no-modify"], env=env)
    res2 = local_runner(["-o", str(pyz2), "--site-packages", str(sp), "-e", "rep_mod:run", "--reproducible", "--no-modify"], env=env)
    assert res1.exit_code == 0
    assert res2.exit_code == 0

    # 1. Compare full byte hash
    assert hashlib.sha256(pyz1.read_bytes()).hexdigest() == hashlib.sha256(pyz2.read_bytes()).hexdigest()

    # 2. Inspect zipinfo and metadata
    with zipfile.ZipFile(str(pyz1)) as z1, zipfile.ZipFile(str(pyz2)) as z2:
        assert z1.namelist() == z2.namelist()
        for info1, info2 in zip(z1.infolist(), z2.infolist()):
            assert info1.filename == info2.filename
            assert info1.date_time == info2.date_time
            assert info1.compress_type == info2.compress_type
        # Verify environment.json has fixed reproducible fields
        env1 = json.loads(z1.read("environment.json"))
        env2 = json.loads(z2.read("environment.json"))
        assert env1["reproducible"] is True
        assert env1["built_at"] == env2["built_at"]
        assert env1["build_id"] == env2["build_id"]

    # 3. Non-reproducible mode with distinct timestamps produces different hashes
    pyz_nr1 = tmp_path / "nr1.pyz"
    pyz_nr2 = tmp_path / "nr2.pyz"
    clean_env = {k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"}
    with mock.patch("time.time", return_value=1600000000.0):
        r_nr1 = local_runner(["-o", str(pyz_nr1), "--site-packages", str(sp), "-e", "rep_mod:run"], env=clean_env)
    with mock.patch("time.time", return_value=1700000000.0):
        r_nr2 = local_runner(["-o", str(pyz_nr2), "--site-packages", str(sp), "-e", "rep_mod:run"], env=clean_env)
    assert r_nr1.exit_code == 0 and r_nr2.exit_code == 0
    assert hashlib.sha256(pyz_nr1.read_bytes()).hexdigest() != hashlib.sha256(pyz_nr2.read_bytes()).hexdigest()


def test_p2p_029_repeat_target_overwrite(tmp_path, runner, baseline_envs):
    """P2P-029: repeated writes replace the target; a pre-packaging failure leaves it untouched.

    The node owns repeated overwrite for one output path: a first build, an
    overwrite, a rejection raised before packaging starts, and a stale temporary
    file from an earlier attempt.

    Two related behaviours belong to the migration target rather than to
    preserved behaviour, because the pre-migration builder does not provide
    them: a failure *after* packaging starts must leave the previous archive
    byte-identical, and two competing builds must leave one complete archive
    (both in F2P-011, which owns archive publication).  Asserting them here
    would make this node fail on the unpatched source.
    """
    sp1 = tmp_path / "sp_rep1"
    sp1.mkdir()
    (sp1 / "mod_rep.py").write_text("def run():\n    print('VERSION_1_OK')\n")

    sp2 = tmp_path / "sp_rep2"
    sp2.mkdir()
    (sp2 / "mod_rep.py").write_text("def run():\n    print('VERSION_2_OK')\n")

    pyz = tmp_path / "repeat_overwrite.pyz"

    def temporary_state(directory, name):
        return sorted(
            path.name for path in Path(directory).iterdir()
            if (".tmp" in path.name or ".bak" in path.name) and name in path.name
        )

    # 1. First build on a nonexistent target.
    res1 = runner(["-o", str(pyz), "--site-packages", str(sp1), "-e", "mod_rep:run"])
    assert res1.exit_code == 0, res1.output
    p1 = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert p1.returncode == 0
    assert "VERSION_1_OK" in p1.stdout

    # 2. Overwrite with a second version.
    res2 = runner(["-o", str(pyz), "--site-packages", str(sp2), "-e", "mod_rep:run"])
    assert res2.exit_code == 0, res2.output
    p2 = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert p2.returncode == 0
    assert "VERSION_2_OK" in p2.stdout
    orig_bytes = pyz.read_bytes()

    # 3. A rejection raised while validating the request happens before any byte
    # reaches the target, so the previous archive is untouched.  This is
    # preserved behaviour; anything that fails after packaging starts is F2P-011.
    env_test = Environment("2026-09-30 00:00:00", "1.1.0")
    with pytest.raises(ZipAppError):
        create_archive([sp2], pyz, sys.executable, "invalid:entry:extra", env_test)
    assert pyz.read_bytes() == orig_bytes
    assert temporary_state(pyz.parent, pyz.name) == []

    # 4. Leftover temporary state from an earlier attempt does not block a later successful build.
    stale_tmp = pyz.parent / f".{pyz.name}.tmp"
    stale_tmp.write_text("STALE_TEMP_CONTENT")
    res_stale = runner(["-o", str(pyz), "--site-packages", str(sp1), "-e", "mod_rep:run"])
    assert res_stale.exit_code == 0, res_stale.output
    p_stale = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert p_stale.returncode == 0
    assert "VERSION_1_OK" in p_stale.stdout
    stale_tmp.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "scenario,invalid_ep",
    [
        ("missing_colon", "missing_colon"),
        ("extra_colon", "mod:fn:extra"),
        ("empty_mod", ":fn"),
        ("empty_fn", "mod:"),
        ("bad_mod_ident", "123mod:fn"),
        ("bad_fn_ident", "mod:123fn"),
        ("invalid_char", "mod:bad-ident"),
        ("invalid_syntax", "invalid identifier"),
        ("unimportable_module", "nonexistent_module_404:run"),
    ],
)
def test_p2p_030_invalid_entry_point(tmp_path, runner, hello_sp, scenario, invalid_ep, baseline_envs):
    """P2P-030: Invalid archive entry points raise ZipAppError, leave target uncorrupted, and unimportable modules fail at runtime."""
    target = tmp_path / f"inv_{scenario}.pyz"
    env = Environment("2026-09-30 00:00:00", "1.1.0")

    if scenario != "unimportable_module":
        # 1. create_archive raises ZipAppError
        with pytest.raises(ZipAppError) as excinfo:
            create_archive([hello_sp], target, sys.executable, invalid_ep, env)
        assert "Invalid entry point" in str(excinfo.value)
        assert not target.exists()

        # 2. Existing file is untouched when create_archive fails with invalid entry point
        target.write_bytes(b"EXISTING_SENTINEL_DATA")
        with pytest.raises(ZipAppError):
            create_archive([hello_sp], target, sys.executable, invalid_ep, env)
        assert target.read_bytes() == b"EXISTING_SENTINEL_DATA"
        target.unlink()
    else:
        # Syntactically valid but nonexistent module: build succeeds, runtime fails
        res = runner(["-o", str(target), "--site-packages", str(hello_sp), "-e", invalid_ep])
        assert res.exit_code == 0
        proc = run_isolated([str(baseline_envs.target_python), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.returncode != 0
        assert "ModuleNotFoundError" in proc.stderr or "No module named" in proc.stderr


@pytest.mark.parametrize(
    "creation_umask,existing_mode",
    [(0o022, None), (0o027, None), (0o077, None), (0o077, 0o644), (0o077, 0o640), (0o022, 0o600), (0o077, 0o664)],
)
def test_p2p_031_executable_permissions(tmp_path, hello_sp, runtime_python, creation_umask, existing_mode):
    """P2P-031: New archives honor umask, overwritten archives retain read/write permissions, and both run."""
    pyz = tmp_path / "exec_perm.pyz"
    if existing_mode is not None:
        pyz.write_bytes(b"PREVIOUS_ARCHIVE")
        if os.name != "nt":
            pyz.chmod(existing_mode)

    # Change umask only inside the build subprocess, never in the pytest host.
    build_script = (
        "import os, sys\n"
        "mask = int(sys.argv.pop(1))\n"
        "if os.name != 'nt': os.umask(mask)\n"
        "from shiv.cli import main\n"
        "main()\n"
    )
    res = run_isolated(
        [str(runtime_python), "-c", build_script, str(creation_umask), "-o", str(pyz), "-e", "hello:main",
         "--site-packages", str(hello_sp), "-p", str(runtime_python)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    if os.name != "nt":
        expected_mode = (existing_mode if existing_mode is not None else 0o666 & ~creation_umask) | UGOX
        actual_mode = stat.S_IMODE(pyz.stat().st_mode)
        assert actual_mode == expected_mode, (oct(actual_mode), oct(expected_mode))
        assert actual_mode & 0o444 == expected_mode & 0o444
        direct = run_isolated([str(pyz)], capture_output=True, text=True)
        assert direct.returncode == 0, direct.stderr
        assert direct.stdout.strip() == "hello world"

    assert zipfile.is_zipfile(pyz)
    assert not list(tmp_path.glob(".exec_perm.pyz.*.tmp"))

    proc = run_isolated([str(runtime_python), str(pyz)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "hello world"


def test_p2p_032_shebang_length(tmp_path, runner, baseline_envs):
    """P2P-032: Python interpreter exceeding BINPRM_BUF_SIZE exits with BINPRM_ERROR, exact boundary passes."""
    sp = tmp_path / "sp_shebang"
    sp.mkdir()
    (sp / "shebang_mod.py").write_text("def run():\n    print('SHEBANG_OK')\n")

    # 1. Exact boundary: length 128 characters succeeds
    shebang_128 = "/" + "a" * (BINPRM_BUF_SIZE - 1)
    assert len(shebang_128) == BINPRM_BUF_SIZE

    with tempfile.TemporaryFile() as fd:
        write_file_prefix(fd, shebang_128)

    pyz_128 = tmp_path / "shebang_128.pyz"
    proc_128 = run_isolated(
        [str(baseline_envs.target_python), "-m", "shiv", "-o", str(pyz_128), "--site-packages", str(sp), "-e", "shebang_mod:run", "-p", shebang_128],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc_128.returncode == 0
    assert pyz_128.exists()

    # 2. Exceeding boundary: length 129 characters exits with BINPRM_ERROR
    shebang_129 = "/" + "a" * BINPRM_BUF_SIZE
    assert len(shebang_129) == BINPRM_BUF_SIZE + 1

    with tempfile.TemporaryFile() as fd:
        with pytest.raises(SystemExit) as excinfo:
            write_file_prefix(fd, shebang_129)
        assert str(excinfo.value.code).strip() == BINPRM_ERROR.strip()

    pyz_129 = tmp_path / "shebang_129.pyz"
    proc_129 = run_isolated(
        [str(baseline_envs.target_python), "-m", "shiv", "-o", str(pyz_129), "--site-packages", str(sp), "-e", "shebang_mod:run", "-p", shebang_129],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc_129.returncode == 1
    assert BINPRM_ERROR.strip() in (proc_129.stdout + proc_129.stderr)


# =====================================================================
# Bootstrap / Runtime Group
# =====================================================================

@pytest.mark.parametrize("use_pip", [False, True])
def test_p2p_021_preamble_python(tmp_path, isolated_runner, hello_wheel, use_pip, runtime_python):
    """P2P-021: Python preamble runs before entry point, inherits environment, and stops execution on failure."""
    # 1. Normal sequential execution and environment inheritance
    preamble = tmp_path / f"preamble_{use_pip}.py"
    preamble.write_text(
        "import os\n"
        "print('PY_PREAMBLE_RUNNING')\n"
        "print('INHERITED_VAR:' + os.environ.get('PREAMBLE_TEST_VAR', 'NONE'))\n"
    )
    preamble.chmod(preamble.stat().st_mode | stat.S_IEXEC)

    pyz = tmp_path / f"preamble_py_{use_pip}.pyz"
    if use_pip:
        args = ["-o", str(pyz), "-e", "hello:main", "--preamble", str(preamble), str(hello_wheel)]
    else:
        sp = tmp_path / f"sp_py_{use_pip}"
        sp.mkdir()
        (sp / "entry_mod.py").write_text("def run():\n    print('ENTRY_RUN_OK')\n")
        args = ["-o", str(pyz), "-e", "entry_mod:run", "--preamble", str(preamble), "--site-packages", str(sp)]

    res = isolated_runner(args)
    assert res.exit_code == 0, res.output

    test_env = {**os.environ, "PREAMBLE_TEST_VAR": "INHERITED_VALUE_OK"}
    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True, env=test_env)
    assert proc.returncode == 0
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines[0] == "PY_PREAMBLE_RUNNING"
    assert lines[1] == "INHERITED_VAR:INHERITED_VALUE_OK"
    assert lines[2] in ("hello world", "ENTRY_RUN_OK")

    # 2. Error handling: non-zero exit in python preamble halts before entry point
    err_preamble = tmp_path / f"preamble_err_{use_pip}.py"
    err_preamble.write_text("import sys\nprint('PY_PREAMBLE_FAILING')\nsys.exit(37)\n")
    pyz_err = tmp_path / f"preamble_err_{use_pip}.pyz"
    if use_pip:
        err_args = ["-o", str(pyz_err), "-e", "hello:main", "--preamble", str(err_preamble), str(hello_wheel)]
    else:
        err_args = ["-o", str(pyz_err), "-e", "entry_mod:run", "--preamble", str(err_preamble), "--site-packages", str(sp)]

    res_err = isolated_runner(err_args)
    assert res_err.exit_code == 0
    proc_err = run_isolated([str(runtime_python), str(pyz_err)], stdout=subprocess.PIPE, text=True)
    assert proc_err.returncode == 37
    assert "PY_PREAMBLE_FAILING" in proc_err.stdout
    assert "ENTRY_RUN_OK" not in proc_err.stdout
    assert "hello world" not in proc_err.stdout


@pytest.mark.parametrize("use_pip", [False, True])
def test_p2p_022_preamble_shell(tmp_path, isolated_runner, hello_wheel, use_pip, runtime_python):
    """P2P-022: Shell preamble runs before entry point, inherits environment, and non-zero status does not stop entry point."""
    # 1. Normal execution with path containing spaces and environment inheritance
    is_win = os.name == "nt"
    preamble_dir = tmp_path / f"dir with spaces {use_pip}"
    preamble_dir.mkdir()
    preamble_name = f"preamble_{use_pip}.bat" if is_win else f"preamble_{use_pip}.sh"
    preamble = preamble_dir / preamble_name

    if is_win:
        preamble.write_text("@echo SH_PREAMBLE_RUNNING\n@echo INHERITED:%PREAMBLE_SHELL_VAR%\n@echo SH_STDERR_OK >&2\n")
    else:
        preamble.write_text("#!/bin/sh\necho 'SH_PREAMBLE_RUNNING'\necho \"INHERITED:$PREAMBLE_SHELL_VAR\"\necho 'SH_STDERR_OK' >&2\n")
        preamble.chmod(preamble.stat().st_mode | stat.S_IEXEC)

    pyz = tmp_path / f"preamble_sh_{use_pip}.pyz"
    if use_pip:
        args = ["-o", str(pyz), "-e", "hello:main", "--preamble", str(preamble), str(hello_wheel)]
    else:
        sp = tmp_path / f"sp_sh_{use_pip}"
        sp.mkdir()
        (sp / "entry_sh.py").write_text("def run():\n    print('ENTRY_SH_OK')\n")
        args = ["-o", str(pyz), "-e", "entry_sh:run", "--preamble", str(preamble), "--site-packages", str(sp)]

    res = isolated_runner(args)
    assert res.exit_code == 0, res.output

    # Verify preamble is in the archive metadata and bin dir
    with zipfile.ZipFile(str(pyz)) as zf:
        env_data = json.loads(zf.read("environment.json"))
        assert env_data["preamble"] == preamble.name
        assert any("bin/" + preamble.name in name for name in zf.namelist())

    # Execute pyz with environment variable and verify sequential order, stderr, and inheritance
    test_env = {**os.environ, "PREAMBLE_SHELL_VAR": "SHELL_INHERIT_OK"}
    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=test_env)
    assert proc.returncode == 0
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines[0] == "SH_PREAMBLE_RUNNING"
    assert lines[1] == "INHERITED:SHELL_INHERIT_OK"
    assert lines[2] in ("hello world", "ENTRY_SH_OK")
    assert "SH_STDERR_OK" in proc.stderr

    # 2. Legacy behavior: a non-zero shell status still runs the entry point.
    err_preamble_name = f"pre_err_{use_pip}.bat" if is_win else f"pre_err_{use_pip}.sh"
    err_preamble = tmp_path / err_preamble_name
    if is_win:
        err_preamble.write_text("@echo SH_ERR_PREAMBLE\n@echo ERR_STDERR >&2\n@exit /b 27\n")
    else:
        err_preamble.write_text("#!/bin/sh\necho 'SH_ERR_PREAMBLE'\necho 'ERR_STDERR' >&2\nexit 27\n")
        err_preamble.chmod(err_preamble.stat().st_mode | stat.S_IEXEC)

    pyz_err = tmp_path / f"preamble_sh_err_{use_pip}.pyz"
    if use_pip:
        err_args = ["-o", str(pyz_err), "-e", "hello:main", "--preamble", str(err_preamble), str(hello_wheel)]
    else:
        err_args = ["-o", str(pyz_err), "-e", "entry_sh:run", "--preamble", str(err_preamble), "--site-packages", str(sp)]

    res_err = isolated_runner(err_args)
    assert res_err.exit_code == 0
    proc_err = run_isolated([str(runtime_python), str(pyz_err)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert "SH_ERR_PREAMBLE" in proc_err.stdout
    assert "ERR_STDERR" in proc_err.stderr
    assert proc_err.returncode == 0
    assert proc_err.stdout.splitlines() == ["SH_ERR_PREAMBLE", "hello world" if use_pip else "ENTRY_SH_OK"]


def test_p2p_023_root_explicit_path(tmp_path, isolated_runner, hello_sp, runtime_python):
    """P2P-023: --root directs archive extraction to explicit directory."""
    extract_root = tmp_path / "r"
    pyz = tmp_path / "rt.pyz"
    res = isolated_runner(["-o", str(pyz), "-e", "hello:main", "--root", str(extract_root), "--site-packages", str(hello_sp)])
    assert res.exit_code == 0, res.output

    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert proc.returncode == 0
    assert "hello world" in proc.stdout
    assert extract_root.exists()


def test_p2p_024_root_env_var(tmp_path, isolated_runner, hello_sp, runtime_python):
    """P2P-024: --root expands environment variables."""
    extract_root = tmp_path / "ev"
    pyz = tmp_path / "ev.pyz"
    res = isolated_runner(["-o", str(pyz), "-e", "hello:main", "--root", "$MY_TEST_SHIV_ROOT", "--site-packages", str(hello_sp)])
    assert res.exit_code == 0, res.output

    env = {**os.environ, "MY_TEST_SHIV_ROOT": str(extract_root)}
    proc = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True, env=env)
    assert proc.returncode == 0
    assert "hello world" in proc.stdout
    assert extract_root.exists()


def test_p2p_028_no_modify(tmp_path, runner, baseline_envs):
    """P2P-028: --no-modify triggers RuntimeError on modified/added files, handles deletion per source baseline, and recovers with SHIV_FORCE_EXTRACT."""
    sp = tmp_path / "sp_nm"
    sp.mkdir()
    (sp / "helper.py").write_text("MSG = 'HELPER_OK'\n")
    (sp / "unused.py").write_text("DATA = 'UNUSED'\n")
    (sp / "check_mod.py").write_text(
        "import helper\n"
        "def run():\n"
        "    print('NO_MODIFY_CHECK_OK:' + helper.MSG)\n"
    )

    pyz = tmp_path / "nm.pyz"
    extract_root = tmp_path / "shiv_root_nm"
    res = runner(["-o", str(pyz), "--site-packages", str(sp), "-e", "check_mod:run", "--no-modify", "--root", str(extract_root)])
    assert res.exit_code == 0, res.output

    # 1. First run: extracts and runs successfully
    proc1 = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc1.returncode == 0
    assert "NO_MODIFY_CHECK_OK:HELPER_OK" in proc1.stdout

    # Locate extracted files in cache
    extracted_check = list(extract_root.rglob("check_mod.py"))[0]
    extracted_helper = list(extract_root.rglob("helper.py"))[0]
    extracted_unused = list(extract_root.rglob("unused.py"))[0]
    orig_check_bytes = extracted_check.read_bytes()

    # 2. Modify an existing file on disk -> triggers RuntimeError before entry point
    extracted_check.write_bytes(orig_check_bytes + b"# tampered\n")
    proc_mod = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc_mod.returncode != 0
    assert "RuntimeError" in proc_mod.stderr
    assert "modified" in proc_mod.stderr.lower()

    # 3. Force extraction with SHIV_FORCE_EXTRACT=1 overwrites modified source file and succeeds
    proc_force = run_isolated(
        [str(baseline_envs.target_python), str(pyz)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "SHIV_FORCE_EXTRACT": "1"},
    )
    assert proc_force.returncode == 0
    assert "NO_MODIFY_CHECK_OK:HELPER_OK" in proc_force.stdout
    # Verify file was restored
    assert extracted_check.read_bytes() == orig_check_bytes

    # 4. Add an untracked .py file in cache -> triggers RuntimeError
    extra_py = extracted_check.parent / "extra_untracked.py"
    extra_py.write_text("print('ADDED')\n")
    proc_add = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc_add.returncode != 0
    assert "RuntimeError" in proc_add.stderr
    assert "modified" in proc_add.stderr.lower()
    extra_py.unlink()

    # 5. Delete an unimported file in cache:
    # In Click 6.7 source baseline, ensure_no_modify only iterates on-disk files via rglob("**/*.py"),
    # so deleting an unimported file is permitted by ensure_no_modify and does not halt execution.
    extracted_unused.unlink()
    proc_del_unimported = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc_del_unimported.returncode == 0
    assert "NO_MODIFY_CHECK_OK:HELPER_OK" in proc_del_unimported.stdout

    # 6. Delete an imported file in cache:
    # ensure_no_modify allows it, but runtime import subsequently fails with non-zero exit code.
    extracted_helper.unlink()
    proc_del_imported = run_isolated([str(baseline_envs.target_python), str(pyz)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc_del_imported.returncode != 0
    assert "ModuleNotFoundError" in proc_del_imported.stderr or "ImportError" in proc_del_imported.stderr

    # 7. Compare deletion semantics directly with source baseline execution
    pyz_base = tmp_path / "nm_base.pyz"
    extract_base = tmp_path / "shiv_root_nm_base"
    cmd_base = [str(baseline_envs.source_python), "-m", "shiv", "-p", str(baseline_envs.source_python), "-o", str(pyz_base), "--site-packages", str(sp), "-e", "check_mod:run", "--no-modify", "--root", str(extract_base)]
    p_b_build = run_isolated(cmd_base, capture_output=True, text=True)
    assert p_b_build.returncode == 0, p_b_build.stderr

    p_b1 = run_isolated([str(baseline_envs.source_python), str(pyz_base)], capture_output=True, text=True)
    assert p_b1.returncode == 0
    assert "NO_MODIFY_CHECK_OK:HELPER_OK" in p_b1.stdout

    list(extract_base.rglob("unused.py"))[0].unlink()
    p_b_del_unimported = run_isolated([str(baseline_envs.source_python), str(pyz_base)], capture_output=True, text=True)
    assert p_b_del_unimported.returncode == 0
    assert proc_del_unimported.returncode == p_b_del_unimported.returncode

    list(extract_base.rglob("helper.py"))[0].unlink()
    p_b_del_imported = run_isolated([str(baseline_envs.source_python), str(pyz_base)], capture_output=True, text=True)
    assert p_b_del_imported.returncode != 0
    assert (proc_del_imported.returncode != 0) == (p_b_del_imported.returncode != 0)


def test_p2p_034_cache_path():
    """P2P-034: cache_path correctly computes paths for regular, $ENV_VAR, ~, empty, spaces, and Windows drive roots."""
    mock_zip = mock.MagicMock(spec=zipfile.ZipFile)
    mock_zip.filename = "my_archive.pyz"
    uid = "abc1234"
    expected_name = f"my_archive.pyz_{uid}"

    # 1. Regular absolute path
    p1 = cache_path(mock_zip, "/var/cache", uid)
    assert p1 == Path("/var/cache", expected_name)

    # 2. $ENV_VAR path
    with mock.patch.dict(os.environ, {"CACHE_ROOT": "/env/cache"}):
        p2 = cache_path(mock_zip, "$CACHE_ROOT", uid)
        assert p2 == Path("/env/cache", expected_name)

    # 3. ~ home directory expansion
    p3 = cache_path(mock_zip, "~", uid)
    assert p3 == Path.home() / expected_name

    p3_sub = cache_path(mock_zip, "~/my_cache", uid)
    assert p3_sub == Path.home() / "my_cache" / expected_name

    # 4. Empty and None root fall back to ~/.shiv
    default_root = Path("~/.shiv").expanduser()
    p4_none = cache_path(mock_zip, None, uid)
    assert p4_none == default_root / expected_name

    p4_empty = cache_path(mock_zip, "", uid)
    assert p4_empty == default_root / expected_name

    # 5. Path with spaces
    p5 = cache_path(mock_zip, "/path with spaces/cache", uid)
    assert p5 == Path("/path with spaces/cache", expected_name)

    # 6. Windows drive path
    if os.name == "nt":
        p6 = cache_path(mock_zip, "C:\\shiv_cache", uid)
        assert p6 == Path("C:/shiv_cache", expected_name)


def test_p2p_035_first_sitedir_index():
    """P2P-035: get_first_sitedir_index accurately finds site-packages and dist-packages positions."""
    # 1. Empty sys.path
    with mock.patch.object(sys, "path", []):
        assert get_first_sitedir_index() is None

    # 2. Path not containing site-packages or dist-packages
    with mock.patch.object(sys, "path", ["/opt/foo", "/bar", "/usr/bin"]):
        assert get_first_sitedir_index() is None

    # 3. Only dist-packages
    with mock.patch.object(sys, "path", ["/usr/local/lib/python3.10/dist-packages", "/bar"]):
        assert get_first_sitedir_index() == 0

    # 4. site-packages in middle
    with mock.patch.object(sys, "path", ["/opt/foo", "/usr/lib/python3.10/site-packages", "/bar"]):
        assert get_first_sitedir_index() == 1

    # 5. Multiple site-packages: returns first one
    with mock.patch.object(sys, "path", ["/a", "/b/site-packages", "/c", "/d/site-packages"]):
        assert get_first_sitedir_index() == 1

    # 6. dist-packages before site-packages
    with mock.patch.object(sys, "path", ["/a", "/b/dist-packages", "/c/site-packages"]):
        assert get_first_sitedir_index() == 1


def test_p2p_037_extend_python_path(baseline_envs):
    """P2P-037: extend_python_path keeps existing entries first, appends without duplicates, and never sorts or folds case."""
    env = {"PYTHONPATH": f"a{os.pathsep}b"}
    extend_python_path(env, ["c", "b", "d"])
    assert env["PYTHONPATH"].split(os.pathsep) == ["a", "b", "c", "d"]

    # A small table is enough: existing order is retained, appended duplicates are
    # dropped once, empty entries survive, and comparisons stay case-sensitive.
    cases = [
        ({"PYTHONPATH": f"a{os.pathsep}b"}, ["c", "b", "d"]),
        ({}, ["foo", "bar", "foo", "baz"]),
        ({"PYTHONPATH": ""}, ["x", "", "y", ""]),
        ({"PYTHONPATH": "/base/dir"}, ["./rel/dir", "/base/dir", "/other/dir"]),
        ({"PYTHONPATH": "CaseDir"}, ["casedir", "CaseDir"]),
    ]

    for env_init, add_paths in cases:
        probe_code = f"""
from shiv.bootstrap import extend_python_path
env = {repr(env_init)}
extend_python_path(env, {repr(add_paths)})
print(env.get("PYTHONPATH", ""))
"""
        p_src = run_isolated([str(baseline_envs.source_python), "-c", probe_code], capture_output=True, text=True)
        p_tgt = run_isolated([str(baseline_envs.target_python), "-c", probe_code], capture_output=True, text=True)
        assert p_src.returncode == p_tgt.returncode == 0
        assert p_tgt.stdout == p_src.stdout, f"Mismatch for init={env_init}, add={add_paths}"
# =====================================================================
# Environment / Locking Group
# =====================================================================

def test_p2p_038_environment_env_var_overrides(tmp_path, isolated_runner, runtime_python):
    """P2P-038: Environment properties properly reflect all 9 environment variable overrides, defaults, and invalid values."""
    env_clean = {k: v for k, v in os.environ.items() if not k.startswith("SHIV_")}

    # 1. Unset environment defaults
    with mock.patch.dict(os.environ, env_clean, clear=True):
        env = Environment(
            built_at="2026-09-30 00:00:00",
            shiv_version="1.1.0",
            root="/default/root",
            entry_point="default:ep",
            script="default_script",
            compile_pyc=True,
            extend_pythonpath=False,
            prepend_pythonpath="/default/prep",
            always_write_cache=False,
        )
        assert env.root == "/default/root"
        assert env.entry_point == "default:ep"
        assert env.script == "default_script"
        assert env.interpreter is None
        assert env.force_extract is False
        assert env.compile_pyc is True
        assert env.compile_workers == 0
        assert env.extend_pythonpath is False
        assert env.prepend_pythonpath == "/default/prep"

    # 2. Test overrides for each of the 9 variables
    with mock.patch.dict(os.environ, {
        "SHIV_ROOT": "/env/root",
        "SHIV_ENTRY_POINT": "override:ep",
        "SHIV_CONSOLE_SCRIPT": "override_script",
        "SHIV_INTERPRETER": "1",
        "SHIV_FORCE_EXTRACT": "true",
        "SHIV_COMPILE_PYC": "0",
        "SHIV_COMPILE_WORKERS": "8",
        "SHIV_EXTEND_PYTHONPATH": "yes",
        "SHIV_PREPEND_PYTHONPATH": "/env/prep",
    }):
        assert env.root == "/env/root"
        assert env.entry_point == "override:ep"
        assert env.script == "override_script"
        assert env.interpreter == "1"
        assert env.force_extract is True
        assert env.compile_pyc is False
        assert env.compile_workers == 8
        assert env.extend_pythonpath is True
        assert env.prepend_pythonpath == "/env/prep"

    # 3. Test boolean variations and invalid values
    with mock.patch.dict(os.environ, {
        "SHIV_FORCE_EXTRACT": "False",
        "SHIV_COMPILE_PYC": "yes",
        "SHIV_COMPILE_WORKERS": "invalid_number_str",
    }):
        assert env.force_extract is False
        assert env.compile_pyc is True
        assert env.compile_workers == 0  # Invalid non-digit falls back to 0

    # 4. Real pyz execution with SHIV_ENTRY_POINT runtime override
    sp = tmp_path / "sp_env_override"
    sp.mkdir()
    (sp / "multi_ep.py").write_text(
        "def orig():\n    print('ORIGINAL_EP_CALLED')\n"
        "def overridden():\n    print('OVERRIDDEN_EP_CALLED')\n"
    )
    pyz = tmp_path / "override_test.pyz"
    res = isolated_runner(["-o", str(pyz), "--site-packages", str(sp), "-e", "multi_ep:orig"])
    assert res.exit_code == 0

    # Normal execution calls original
    p_orig = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True)
    assert p_orig.returncode == 0
    assert "ORIGINAL_EP_CALLED" in p_orig.stdout

    # Override execution calls overridden
    env_override = {**os.environ, "SHIV_ENTRY_POINT": "multi_ep:overridden"}
    p_over = run_isolated([str(runtime_python), str(pyz)], stdout=subprocess.PIPE, text=True, env=env_override)
    assert p_over.returncode == 0
    assert "OVERRIDDEN_EP_CALLED" in p_over.stdout


def test_p2p_039_environment_json_roundtrip():
    """P2P-039: Environment JSON serialization and deserialization roundtrip for all fields."""
    env1 = Environment(
        built_at="2026-09-30 00:00:00",
        shiv_version="1.1.0",
        build_id="build123",
        entry_point="foo:bar",
        hashes={"file.py": "hash123"},
        no_modify=True,
        reproducible=True,
        preamble="preamble.py",
        root="/custom/root",
        script="custom_script",
        compile_pyc=False,
        extend_pythonpath=True,
        prepend_pythonpath="/prep",
        always_write_cache=True,
    )
    json_str = env1.to_json()
    env2 = Environment.from_json(json_str)

    data1 = json.loads(env1.to_json())
    data2 = json.loads(env2.to_json())
    assert data1 == data2
    assert env1.__dict__ == env2.__dict__

    # Also test minimal environment with defaults
    env_min = Environment(built_at="2026-09-30 00:00:00", shiv_version="1.1.0")
    env_min_rt = Environment.from_json(env_min.to_json())
    assert json.loads(env_min.to_json()) == json.loads(env_min_rt.to_json())


@pytest.mark.parametrize("child_environment", ["source", "target"])
def test_p2p_040_filelock(tmp_path, baseline_envs, child_environment):
    """P2P-040: FileLock mutual exclusion across instances, release on exit, thread/process isolation, and exception recovery.

    Bounded acquisition (``timeout``/``poll_interval``/``TimeoutError``) is a
    target requirement and lives in F2P-012: the pre-migration lock accepts
    neither parameter.
    """
    child_python = getattr(baseline_envs, child_environment + "_python")
    lock_file = str(tmp_path / "mutex.lock")
    lock1 = FileLock(lock_file)
    lock2 = FileLock(lock_file)

    worker_trying = threading.Event()
    worker_acquired = threading.Event()
    worker_release = threading.Event()

    def worker():
        worker_trying.set()
        with lock2:
            worker_acquired.set()
            worker_release.wait(timeout=3.0)

    # 1. Mutual exclusion between threads
    with lock1:
        assert lock1.is_locked
        t1 = threading.Thread(target=worker)
        t1.start()
        assert worker_trying.wait(timeout=2.0), "Worker thread failed to start"
        # While lock1 is held, worker cannot acquire lock2
        assert not worker_acquired.wait(timeout=0.2), "lock2 acquired while lock1 was held"

    # lock1 released; worker thread should now acquire lock2 promptly
    assert worker_acquired.wait(timeout=3.0), "lock2 failed to acquire after lock1 released"
    worker_release.set()
    t1.join(timeout=3.0)
    assert not t1.is_alive()
    assert not lock2.is_locked

    # 2. Context exception properly releases lock
    try:
        with lock1:
            assert lock1.is_locked
            raise RuntimeError("forced lock test error")
    except RuntimeError:
        pass

    assert not lock1.is_locked
    with lock2:
        assert lock2.is_locked
    assert not lock2.is_locked

    # 3. Acquiring when lock file already exists on disk
    assert os.path.exists(lock_file)
    with lock1:
        assert lock1.is_locked

    # 4. Multi-process mutual exclusion with observable two-way IPC
    proc_script = (
        "import sys; from shiv.bootstrap.filelock import FileLock;\n"
        "with FileLock(sys.argv[1]):\n"
        "    sys.stdout.write('LOCKED\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stdin.readline()\n"
    )
    proc = subprocess.Popen(
        [str(child_python), "-c", proc_script, lock_file],
        env=isolated_python_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        first_line = proc.stdout.readline()
        assert first_line.strip() == "LOCKED"

        # While child process holds lock, second thread tries to acquire
        p_trying = threading.Event()
        p_acquired = threading.Event()

        def try_proc_lock():
            p_trying.set()
            with FileLock(lock_file):
                p_acquired.set()

        t2 = threading.Thread(target=try_proc_lock)
        t2.start()

        assert p_trying.wait(timeout=2.0), "Thread failed to attempt lock acquisition"
        # Confirm thread cannot acquire while child process holds it
        assert not p_acquired.wait(timeout=0.2), "Acquired lock while child process held it"

        # Signal child process to release lock
        proc.stdin.write("RELEASE\n")
        proc.stdin.flush()
        proc.wait(timeout=3.0)
        assert proc.returncode == 0

        # Now thread 2 acquires lock promptly
        assert p_acquired.wait(timeout=3.0), "Failed to acquire lock after child process exited"
        t2.join(timeout=3.0)
        assert not t2.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_p2p_041_ensure_no_modify(tmp_path, baseline_envs):
    """P2P-041: ensure_no_modify validates on disk Python files against hashes, detecting modifications and additions per source baseline."""
    sp = tmp_path / "sp_modify_test"
    sp.mkdir()
    pkg_dir = sp / "testpkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("INIT = 1\n")
    (pkg_dir / "worker.py").write_text("def run(): pass\n")

    # Compute genuine hashes (including both native and posix keys)
    genuine_hashes = {}
    for p in sp.rglob("**/*.py"):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        genuine_hashes[str(p.relative_to(sp))] = h
        genuine_hashes[p.relative_to(sp).as_posix()] = h

    def run_sub(py_bin, sp_path, h_dict):
        code = f"""
from pathlib import Path
from shiv.bootstrap import ensure_no_modify
ensure_no_modify(Path(r'{sp_path}'), {repr(h_dict)})
"""
        return run_isolated([str(py_bin), "-c", code], capture_output=True, text=True)

    # 1. Unmodified files pass in local and both venvs
    ensure_no_modify(sp, genuine_hashes)
    p_src1 = run_sub(baseline_envs.source_python, sp, genuine_hashes)
    p_tgt1 = run_sub(baseline_envs.target_python, sp, genuine_hashes)
    assert p_src1.returncode == p_tgt1.returncode == 0

    # 2. Modify an existing file on disk: triggers RuntimeError in local and both venvs
    target_py = next(sp.rglob("**/*.py"))
    orig_bytes = target_py.read_bytes()
    target_py.write_bytes(orig_bytes + b"# tampered\n")

    with pytest.raises(RuntimeError) as excinfo:
        ensure_no_modify(sp, genuine_hashes)
    assert "modified" in str(excinfo.value).lower()
    p_src2 = run_sub(baseline_envs.source_python, sp, genuine_hashes)
    p_tgt2 = run_sub(baseline_envs.target_python, sp, genuine_hashes)
    assert p_src2.returncode != 0 and p_tgt2.returncode != 0
    assert "RuntimeError" in p_src2.stderr and "RuntimeError" in p_tgt2.stderr

    # Restore original content
    target_py.write_bytes(orig_bytes)
    ensure_no_modify(sp, genuine_hashes)

    # 3. Add a new .py file (not in hashes) -> triggers RuntimeError in both
    added_py = sp / "extra_untracked.py"
    added_py.write_text("print('ADDED')\n")
    with pytest.raises(RuntimeError) as excinfo:
        ensure_no_modify(sp, genuine_hashes)
    assert "modified" in str(excinfo.value).lower()
    p_src3 = run_sub(baseline_envs.source_python, sp, genuine_hashes)
    p_tgt3 = run_sub(baseline_envs.target_python, sp, genuine_hashes)
    assert p_src3.returncode != 0 and p_tgt3.returncode != 0
    added_py.unlink()

    # 4. Missing hash entry for an existing file -> triggers RuntimeError in both
    target_rel = str(target_py.relative_to(sp))
    target_rel_posix = target_py.relative_to(sp).as_posix()
    incomplete_hashes = {k: v for k, v in genuine_hashes.items() if k not in (target_rel, target_rel_posix)}
    with pytest.raises(RuntimeError) as excinfo:
        ensure_no_modify(sp, incomplete_hashes)
    assert "modified" in str(excinfo.value).lower()
    p_src4 = run_sub(baseline_envs.source_python, sp, incomplete_hashes)
    p_tgt4 = run_sub(baseline_envs.target_python, sp, incomplete_hashes)
    assert p_src4.returncode != 0 and p_tgt4.returncode != 0

    # 5. Delete an existing .py file -> empirically verified by source baseline:
    # ensure_no_modify only iterates on-disk files, so deletion does not fail ensure_no_modify
    target_py.unlink()
    ensure_no_modify(sp, genuine_hashes)
    p_src5 = run_sub(baseline_envs.source_python, sp, genuine_hashes)
    p_tgt5 = run_sub(baseline_envs.target_python, sp, genuine_hashes)
    assert p_src5.returncode == p_tgt5.returncode == 0

    # 6. POSIX keys work in the target. Backslash keys are native on Windows;
    # on POSIX both implementations reject them, even for unchanged files.
    posix_hashes = {Path(k).as_posix(): v for k, v in genuine_hashes.items() if (sp / k).exists()}
    ensure_no_modify(sp, posix_hashes)
    p_tgt6 = run_sub(baseline_envs.target_python, sp, posix_hashes)
    assert p_tgt6.returncode == 0

    win_hashes = {Path(k).as_posix().replace("/", "\\"): v for k, v in genuine_hashes.items() if (sp / k).exists()}
    if os.name == "nt":
        ensure_no_modify(sp, win_hashes)
    else:
        with pytest.raises(RuntimeError, match="A Python source file has been modified"):
            ensure_no_modify(sp, win_hashes)
    p_tgt7 = run_sub(baseline_envs.target_python, sp, win_hashes)
    p_src7 = run_sub(baseline_envs.source_python, sp, win_hashes)
    if os.name == "nt":
        assert p_src7.returncode == p_tgt7.returncode == 0
    else:
        assert p_src7.returncode != 0 and p_tgt7.returncode != 0
        assert "A Python source file has been modified" in p_src7.stderr
        assert "A Python source file has been modified" in p_tgt7.stderr
