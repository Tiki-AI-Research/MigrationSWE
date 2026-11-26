"""Shared probe helpers for BATCH-F2P-0003.

The batch owns the fail-to-pass (F2P) observations listed in the authoring
input.  Every observation is a *target-only* postcondition: the same semantic
predicate is evaluated on the source repository (Rust) and on the reference
target (Go).  On the source side the predicate must fail because the Go
capability does not exist; on the target side it must pass.

This module holds the observation table plus the predicate helpers that both
runners import, so the source and target probes cannot drift apart.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

BATCH_ID = "BATCH-F2P-0003"
RESULT_SCHEMA_VERSION = "migration-result:v1"

# observation id -> (requirement ids, go test function or None, extra checks)
OBSERVATIONS = [
    ("F2P-050", ["R107"], None, ["build_tags"]),
    ("F2P-053", ["R104"], "TestF2P053EitherOrBoth", []),
    ("F2P-054", ["R113"], "TestF2P054Positions", []),
    ("F2P-072", ["R025"], None, ["go_mod", "cgo_build"]),
    ("F2P-070", ["R024"], None, ["dep_scan"]),
    ("F2P-049", ["R108"], "TestF2P049Interleave", []),
    ("F2P-051", ["R024"], "TestF2P051Exports", ["dep_scan"]),
    ("F2P-052", ["R108"], "TestF2P052Interleave", []),
    ("F2P-055", ["R108"], "TestF2P055Laziness", []),
    ("F2P-056", ["R105"], "TestF2P056MergeAndPanics", []),
    ("F2P-057", ["R108"], "TestF2P057Dedup", []),
    ("F2P-058", ["R108"], "TestF2P058Dedup", []),
    ("F2P-059", ["R108"], "TestF2P059Protocol", []),
    ("F2P-060", ["R103"], "TestF2P060AllEqualValue", []),
    ("F2P-061", ["R104"], "TestF2P061EitherOrBoth", []),
    ("F2P-062", ["R113"], "TestF2P062Positions", []),
    ("F2P-063", ["R115"], "TestF2P063SizeHintArith", []),
    ("F2P-064", ["R108"], "TestF2P064Protocol", []),
    ("F2P-065", ["R102"], "TestF2P065SizeHints", []),
    ("F2P-066", ["R112"], "TestF2P066ProcessResults", []),
    ("F2P-067", ["R108"], "TestF2P067Laziness", []),
    ("F2P-068", ["R108"], "TestF2P068Interleave", []),
    ("F2P-069", ["R105"], "TestF2P069Panics", []),
    ("F2P-071", ["R114"], "TestF2P071ExactlyOne", []),
]

GO_TEST_FILE = "f2p_observation_test.go"

# Markers that would indicate a Go pseudo-migration delegating to Rust.
FORBIDDEN_GO_MARKERS = [
    'import "C"',
    "#cgo",
    "//go:cgo",
    'exec.Command("cargo"',
    'exec.Command("rustc"',
    'exec.Command("cargo test"',
    '"cargo test"',
    'plugin.Open',
    "dlopen",
    "libitertools",
]


def observation_ids():
    return [row[0] for row in OBSERVATIONS]


def go_env():
    env = dict(os.environ)
    env.setdefault("GOFLAGS", "")
    env["GOPROXY"] = "off"
    env["GOSUMDB"] = "off"
    env["GOTOOLCHAIN"] = "local"
    env["GOCACHE"] = env.get("F2P_GOCACHE", "/tmp/gocache-f2p")
    env.setdefault("CGO_ENABLED", "0")
    return env


def run(cmd, cwd, timeout=300):
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=go_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, out.strip()
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout running {cmd!r}"


def has_go_module(repo):
    return os.path.isfile(os.path.join(repo, "go.mod"))


def _iter_go_files(repo):
    for root, _dirs, files in os.walk(repo):
        if ".git" in root.split(os.sep):
            continue
        for name in files:
            if name.endswith(".go"):
                yield os.path.join(root, name)


def scan_forbidden_go(repo):
    """Return a list of (path, line, marker) for Rust-delegation markers."""
    hits = []
    for path in _iter_go_files(repo):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, 1):
                    for marker in FORBIDDEN_GO_MARKERS:
                        if marker in line:
                            hits.append((path, lineno, marker))
        except OSError:
            continue
    return hits


def _scratch_copy(repo, base):
    dest = tempfile.mkdtemp(prefix=base + "-")
    shutil.copytree(repo, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    return dest


def _probe_module(target_copy, body):
    """Create an isolated probe module that imports the local ``itertools``."""
    probe = tempfile.mkdtemp(prefix="f2p-probe-")
    with open(os.path.join(probe, "go.mod"), "w", encoding="utf-8") as handle:
        handle.write(
            "module f2pprobe\n\ngo 1.23.0\n\n"
            "require itertools v0.0.0\n\n"
            "replace itertools => %s\n" % target_copy
        )
    with open(os.path.join(probe, "main.go"), "w", encoding="utf-8") as handle:
        handle.write(body)
    return probe


def _go_code(text):
    """Mask comments and literals while retaining offsets in trusted probe text."""
    pattern = r'//[^\n]*|/\*[\s\S]*?\*/|`[^`]*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda match: re.sub(r'[^\n]', ' ', match[0]), text)


def observation_source(source, testname):
    """Keep the selected test and the transitive closure of its declarations.

    The probe asset uses column-zero, individually named func/type declarations.
    Only this evaluator-owned source is sliced; candidate code is never changed.
    Missing APIs in another observation must not prevent this one from compiling.
    Receiver methods are owned by their local receiver type.
    """
    code = _go_code(source)
    starts = list(re.finditer(r'^(func|type)\s+', code, re.M))
    declarations = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(source)
        text = source[start.start():end]
        masked = code[start.start():end]
        if start[1] == 'type':
            name = re.match(r'type\s+(\w+)', masked)[1]
        elif masked.startswith('func ('):
            name = re.match(r'func\s+\(\s*(?:\w+\s+)?\*?(\w+)', masked)[1]
        else:
            name = re.match(r'func\s+(\w+)', masked)[1]
        declarations.append((name, text, set(re.findall(r'\b\w+\b', masked))))
    names = {name for name, _, _ in declarations}
    if testname not in names:
        raise ValueError('missing observation test: ' + testname)
    needed = {testname}
    while True:
        expanded = needed | set().union(*(refs & names for name, _, refs in declarations if name in needed))
        if expanded == needed:
            break
        needed = expanded
    body = '\n'.join(text for name, text, _ in declarations if name in needed)
    imports = []
    block = re.search(r'import\s*\(([\s\S]*?)\)', source)
    if block is None:
        raise ValueError('observation asset has no import block')
    for alias, path in re.findall(r'(\w+)?\s*"([^"\n]+)"', block[1]):
        alias = alias or path.rsplit('/', 1)[-1]
        if re.search(r'\b' + re.escape(alias) + r'\s*\.', _go_code(body)):
            imports.append('%s "%s"' % (alias, path))
    return 'package itertools_test\n\nimport (\n' + '\n'.join(imports) + '\n)\n\n' + body


def _operation_arity(repo, name):
    """Inspect a public declaration as data; never execute candidate code."""
    if repo is None:
        return None
    arities = set()
    for filename in os.listdir(repo):
        if not filename.endswith('.go') or filename.endswith('_test.go'):
            continue
        path = os.path.join(repo, filename)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        with open(path, encoding='utf-8', errors='replace') as handle:
            code = _go_code(handle.read())
        for match in re.finditer(r'^func\s+' + re.escape(name) + r'\b', code, re.M):
            pos = match.end()
            while pos < len(code) and code[pos].isspace(): pos += 1
            if pos < len(code) and code[pos] == '[':
                depth = 1
                pos += 1
                while pos < len(code) and depth:
                    depth += (code[pos] == '[') - (code[pos] == ']')
                    pos += 1
            while pos < len(code) and code[pos].isspace(): pos += 1
            if pos == len(code) or code[pos] != '(': continue
            pos += 1
            start = pos
            depth = 0
            commas = 0
            while pos < len(code):
                char = code[pos]
                if char == ')' and depth == 0: break
                if char in '([{': depth += 1
                elif char in ')]}': depth -= 1
                elif char == ',' and depth == 0: commas += 1
                pos += 1
            if pos < len(code):
                arities.add(commas + 1 if code[start:pos].strip() else 0)
    return next(iter(arities)) if len(arities) == 1 else None


def observation_sources(source, testname, repo=None):
    """Accept legal constructor locations and both FlattenOK input projections.

    The instruction names core.RcIter without fixing its constructor signature.
    All variants keep the same sharing, borrow, mixed-direction, error, order
    and size-hint assertions. Only evaluator-owned source is adapted, and the
    caller retries solely after compilation fails, never after an assertion.
    """
    selected = observation_source(source, testname)
    if repo is not None and 'it.FlattenOK(' in selected:
        uses_result = False
        result_arity = 1
        for path in _iter_go_files(repo):
            if path.endswith('_test.go'):
                continue
            with open(path, encoding='utf-8', errors='replace') as handle:
                code = _go_code(handle.read())
            uses_result |= bool(re.search(r'^func\s+FlattenOK[^\n]*core\.Result\[', code, re.M))
            declaration = re.search(r'^type\s+Result\[([^\]]+)\]', code, re.M)
            if declaration:
                result_arity = declaration[1].count(',') + 1
        if uses_result and result_arity in {1, 2}:
            params = 'core.Iter[int]' + (', error' if result_arity == 2 else '')
            selected = selected.replace('core.Pair[core.Iter[int], error]', 'core.Result[' + params + ']')
            selected = re.sub(r'\{First: (core\.FromSlice\(\[\]int\{[^}]*\}\))\}',
                              lambda match: 'core.Ok[' + params + '](' + match[1] + ')', selected)
            constructor = 'core.Err' if result_arity == 2 else 'core.ErrResult'
            selected = selected.replace('{Second: boom}', constructor + '[' + params + '](boom)')
    # These signatures are not fixed by instruction.md. Adapt only the
    # evaluator-owned invocation; every behavior assertion remains intact.
    adaptations = {
        'Tee': (2, 'it.Tee(core.FromSlice([]int{1, 2, 3}))',
                'it.Tee(core.FromSlice([]int{1, 2, 3}), func(v int) int { return v })'),
        'ChunkBy': (3, 'it.ChunkBy(s,func(v int) int { return v })',
                    'it.ChunkBy(s,func(v int) int { return v },func(a,b int) bool { return a == b })'),
        'TupleCombinations': (3, 'it.TupleCombinations(s,2)',
                              'it.TupleCombinations(s,2,f2pCloneIntIterator)'),
    }
    for name, (arity, before, after) in adaptations.items():
        if _operation_arity(repo, name) == arity:
            selected = selected.replace(before, after)
    if _operation_arity(repo, 'TupleCombinations') == 1:
        selected = selected.replace('it.TupleCombinations(s,2)', 'it.TupleCombinations(s)')
    if _operation_arity(repo, 'CartesianProduct') == 3:
        for before in ('it.CartesianProduct(left, core.FromSlice([]int{10, 20}))',
                       'it.CartesianProduct(untouched, core.FromSlice([]int{}))',
                       'it.CartesianProduct(core.FromSlice([]int{10, 20}), &f2pProductRightSource{})'):
            selected = selected.replace(before, before[:-1] + ', f2pCloneIntIterator)')
    if 'f2pCloneIntIterator)' in selected and 'func f2pCloneIntIterator' not in selected:
        helper = observation_source(source, 'f2pCloneIntIterator').split(')\n\n', 1)[1]
        selected += '\n' + helper
    constructors = ['core.NewRcIter']
    if 'func f2pNewRcIter' in selected:
        constructors += ['it.Rciter', 'it.RcIter']
        if repo is not None:
            for directory, qualifier, result in ((os.path.join(repo, 'core'), 'core.', 'RcIter'),
                                                   (repo, 'it.', 'core.RcIter')):
                if not os.path.isdir(directory): continue
                for filename in sorted(os.listdir(directory)):
                    if not filename.endswith('.go') or filename.endswith('_test.go'): continue
                    path = os.path.join(directory, filename)
                    if not os.path.isfile(path) or os.path.islink(path): continue
                    with open(path, encoding='utf-8', errors='replace') as handle:
                        code = _go_code(handle.read())
                    pattern = r'^func\s+([A-Z]\w*)\[[^\n]+?\]\([^\n]+\)\s+\*?' + re.escape(result) + r'\['
                    for match in re.finditer(pattern, code, re.M):
                        factory = qualifier + match[1]
                        if _operation_arity(directory, match[1]) == 1 and factory not in constructors:
                            constructors.append(factory)
    for constructor in constructors:
        variant = selected.replace('core.NewRcIter(source)', constructor + '(source)')
        # A constructor variant can introduce a previously unused root import.
        # The protocol observations already import it for the adaptor checks.
        yield variant
        if 'func f2pAuditCapabilityBehavior' in selected:
            alternate = variant.replace('core.Pair[core.Iter[int], error]', 'core.Pair[[]int, error]')
            alternate = re.sub(r'First: core\.FromSlice\((\[\]int\{[^}]*\})\)', r'First: \1', alternate)
            yield alternate


def check_go_tests(repo, test_file, obs_to_test):
    """Run the per-observation Go tests. Returns obs -> (passed, detail)."""
    results = {obs: (False, "not evaluated") for obs in obs_to_test}
    if test_file is None or not os.path.isfile(test_file):
        for obs in obs_to_test:
            results[obs] = (False, "Go observation test file missing")
        return results, "go test file missing", 2
    if not has_go_module(repo):
        for obs in obs_to_test:
            results[obs] = (False, "no Go module (go.mod) present in repository")
        return results, "source repository has no Go module", None
    scratch = _scratch_copy(repo, "f2p-gotest")
    transcript = []
    failed = False
    try:
        with open(test_file, encoding='utf-8') as handle:
            source = handle.read()
        fd, probe = tempfile.mkstemp(prefix='harbor_observation_', suffix='_test.go', dir=scratch)
        os.close(fd)
        for obs, testname in obs_to_test.items():
            state = None
            for variant in observation_sources(source, testname, scratch):
                with open(probe, 'w', encoding='utf-8') as handle:
                    handle.write(variant)
                # Explicit files compile just this observation; the candidate's
                # own tests have already been run by the verifier native phase.
                rc, out = run(['go', 'test', '-run', '^' + testname + '$', '-v', probe], scratch, timeout=600)
                transcript.append('%s rc=%d\n%s' % (testname, rc, out))
                for line in out.splitlines():
                    if line.startswith('--- PASS: ' + testname + ' '):
                        state = rc == 0
                    elif line.startswith('--- FAIL: ' + testname + ' '):
                        state = False
                # Retry another legal signature only after a compile failure;
                # never use a variant to override a behavioral assertion failure.
                compile_failed = rc != 0 and any(
                    marker in out for marker in ('[build failed]', '[setup failed]')
                )
                if state is not None or not compile_failed:
                    break
            detail = 'go test %s %s (rc=%d)' % (testname, 'passed' if state else 'failed', rc)
            if not state:
                detail += ': ' + out[-3000:]
                failed = True
            results[obs] = (state is True, detail)
    finally:
        shutil.rmtree(scratch)
    return results, '\n'.join(transcript), 1 if failed else 0


def check_build_tags(repo):
    if not has_go_module(repo):
        return False, "no Go module (go.mod) present in repository"
    scratch = _scratch_copy(repo, "f2p-tags")
    details = []
    for tag in ("nostd", "noalloc"):
        rc, out = run(["go", "build", "-tags", tag, "./..."], scratch)
        if rc != 0:
            return False, "go build -tags %s ./... failed: %s" % (tag, out.splitlines()[-1] if out else "")
        details.append("-tags %s ok" % tag)

    excluded = itertools_ident_exclusion(scratch, "nostd", "Unique", "any")
    if excluded is True:
        details.append("nostd hides Unique")
    else:
        return False, "Unique not excluded under -tags nostd: %s" % excluded

    excluded = itertools_ident_exclusion(scratch, "noalloc", "Iproduct", None)
    if excluded is True:
        details.append("noalloc hides Iproduct")
    else:
        return False, "Iproduct not excluded under -tags noalloc: %s" % excluded
    return True, "; ".join(details)


def ident_reference_candidates(ident, arg):
    """Candidate Go call forms that reference ``ident`` for build-tag probing.

    The public contract fixes *what* must disappear under a build tag, not the Go
    signature of the operation.  ``Unique`` is the projection of Rust's
    ``unique()`` and a candidate may legally implement either the keyed form
    (``Unique(it, key)``) or a keyless one (``Unique(it)``).  The probe used to
    hard-code the keyed form, so a keyless migration could never build the
    untagged reference probe and was reported as ``nostd did not hide Unique``
    even though ``-tags nostd`` correctly excluded it.

    Every candidate form is returned; the caller builds them in order and checks
    the exclusion semantics on the first one the candidate actually supports.
    """
    plain = "itertools.%s[int](core.FromSlice([]int{1}))" % ident
    if arg == "any":
        keyed = (
            "itertools.%s[int](core.FromSlice([]int{1}), func(v int) any { return v })" % ident
        )
        return [keyed, plain]
    return [plain]


def itertools_ident_exclusion(repo, tag, ident, arg):
    """Return True when ident is excluded under tag but present by default.

    Exclusion is decided solely by the build-tag semantics: the identifier must
    be referenceable in the default build, and the tagged build must reject the
    reference with ``undefined``/``build constraints``.  The identifier's exact
    signature is not part of the contract, so every legal call form is tried.
    """
    imports = 'import (\n\t"itertools"\n\t"itertools/core"\n)'
    for ref in ident_reference_candidates(ident, arg):
        body = "package main\n\n%s\n\nfunc main() { _ = %s }\n" % (imports, ref)
        probe = _probe_module(repo, body)
        rc_default, _ = run(["go", "build", "-o", os.devnull, "./..."], probe)
        if rc_default != 0:
            continue
        rc_tag, out_tag = run(["go", "build", "-tags", tag, "-o", os.devnull, "./..."], probe)
        if rc_tag == 0:
            return "%s still present under -tags %s" % (ident, tag)
        if "undefined" not in out_tag and "build constraints" not in out_tag:
            return "unexpected failure under -tags %s: %s" % (tag, out_tag.splitlines()[-1] if out_tag else "")
        return True
    return "reference build without tags failed"


def check_go_mod(repo):
    if not has_go_module(repo):
        return False, "no Go module (go.mod) present in repository"
    with open(os.path.join(repo, "go.mod"), "r", encoding="utf-8") as handle:
        header = handle.read()
    if "module itertools" not in header:
        return False, "go.mod does not declare module itertools"
    if "go 1.23" not in header:
        return False, "go.mod does not declare go 1.23"
    return True, "self-contained module itertools at go 1.23"


def check_cgo_build(repo):
    if not has_go_module(repo):
        return False, "no Go module (go.mod) present in repository"
    scratch = _scratch_copy(repo, "f2p-cgo")
    env = go_env()
    env["CGO_ENABLED"] = "0"
    try:
        completed = subprocess.run(
            ["go", "build", "./..."],
            cwd=scratch,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        return False, "go toolchain not available"
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip().splitlines()
        return False, "CGO_ENABLED=0 go build ./... failed: %s" % (detail[-1] if detail else "")
    hits = scan_forbidden_go(scratch)
    if hits:
        path, lineno, marker = hits[0]
        return False, "Rust-delegation marker %r at %s:%d" % (marker, path, lineno)
    return True, "CGO_ENABLED=0 go build ./... succeeded with no Rust linkage markers"


def check_dep_scan(repo):
    if not has_go_module(repo):
        return False, "no Go module (go.mod) present in repository"
    hits = scan_forbidden_go(repo)
    if hits:
        path, lineno, marker = hits[0]
        return False, "Rust-delegation marker %r at %s:%d" % (marker, path, lineno)
    return True, "Go module present and no Rust import/link/call/subprocess markers"


def evaluate(repo, test_file):
    """Evaluate every F2P observation against repo.

    Returns (results, transcript) where results maps observation id to
    (passed: bool, detail: str).
    """
    results = {}
    transcript = []

    obs_to_test = {
        obs: test for (obs, _reqs, test, _extra) in OBSERVATIONS if test is not None
    }
    test_results, tail, rc = check_go_tests(repo, test_file, obs_to_test)
    transcript.append("go test: %s" % tail)

    for obs, reqs, test, extra in OBSERVATIONS:
        if test is not None:
            results[obs] = test_results[obs]
        else:
            results[obs] = (False, "no direct probe")

    for obs, reqs, test, extra in OBSERVATIONS:
        for check in extra:
            if check == "build_tags":
                results[obs] = check_build_tags(repo)
            elif check == "go_mod":
                results[obs] = check_go_mod(repo)
            elif check == "cgo_build":
                passed, detail = check_cgo_build(repo)
                prev = results.get(obs, (True, ""))[0]
                previous_detail = results.get(obs, (True, ""))[1]
                results[obs] = (prev and passed, "; ".join(filter(None, [previous_detail, detail])))
            elif check == "dep_scan":
                passed, detail = check_dep_scan(repo)
                if test is not None:
                    prev = results.get(obs, (True, ""))[0]
                    previous_detail = results.get(obs, (True, ""))[1]
                    results[obs] = (prev and passed, "; ".join(filter(None, [previous_detail, detail])))
                else:
                    results[obs] = (passed, detail)

    transcript.append(
        "observations: " + ", ".join(
            "%s=%s" % (obs, "PASS" if results[obs][0] else "FAIL")
            for obs in observation_ids()
        )
    )
    return results, transcript


def make_report(results):
    entries = []
    for obs in observation_ids():
        passed, detail = results[obs]
        entries.append(
            {
                "observation_id": obs,
                "status": "PASS" if passed else "FAIL",
                "passed": bool(passed),
                "detail": str(detail),
            }
        )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "results": entries,
    }


def write_report(path, report):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
