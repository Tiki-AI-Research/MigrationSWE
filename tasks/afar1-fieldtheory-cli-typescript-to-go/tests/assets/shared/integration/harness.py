#!/usr/bin/env python3
"""INTEGRATION harness for the TypeScript -> Go migration test assets.

This is the executable projection registry for the INTEGRATION batch. It
dispatches the *executable projection* of every dependency batch for one side
(``source`` or ``target``), collects the per-observation result that the batch
probe actually observed, and emits the normalized runtime report described by
``migration-observations:v6``.

Design rules enforced here:

* Every potentially long-running command is executed under an explicit
  ``timeout`` (the coreutils ``timeout`` binary is always the argv prefix, and a
  subprocess-level timeout is kept as a hard backstop).  The harness can never
  wait indefinitely.
* Nothing is read from ``dependencies/``, ``fragments/`` or any authoring-only
  path.  The only inventory consumed at runtime is the self-contained
  ``shared/integration/observations.json``.
* The source and target repositories are resolved independently (never collapsed
  onto one another) from the documented variables ``MIGRATION_REPO_ROOT``,
  ``MIGRATION_REPO``, ``REPO_ROOT`` or ``REPO`` and are then projected onto
  *every* alias the dependency probes read (``REPO_ROOT``/
  ``REPO_ROOT``, ``FT_SOURCE``/``FT_TARGET``, the side-local ``REPO_ROOT``
  and ``REPO_ROOT``, and ``MIGRATION_REPO_ROOT``/``MIGRATION_REPO``/``REPO``) so
  each batch's executable projection runs against the repository for its own
  side rather than a stale default.  When no override is present the read-only
  authoring references ``/input/source`` and ``/input/target`` are used if they
  exist, otherwise ``/workspace/repo``.
* Results are the raw observation of the side under test.  A probe that cannot
  be executed yields ``INCONCLUSIVE``; a probe that is still running when its
  budget expires yields ``TIMEOUT``.  Expectations are never copied in as
  results.
* For P2P and DIFFERENTIAL the value is the stable projection produced by the
  batch probe (``{"result": ...}``); both sides must normalize equal.
"""

import json
import os
import re
import subprocess
import sys
import time

SCHEMA_VERSION = "migration-observations:v6"

VALID_STATUS = frozenset(
    ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT")
)

# Wall-clock budget (seconds) for each batch projection.  These are backstops:
# the batch probes apply their own per-command timeouts long before these fire.
BUDGET = {
    "P2P-0001": 420,
    "P2P-0002": 420,
    "DIFFERENTIAL": 420,
    "CONTRACT": 420,
    "STATE": 420,
    "F2P-0001": 420,
    # Includes the real 120-second default engine timeout observation.
    "F2P-0002": 600,
    "F2P-0002-ONE": 300,
    # Separate adaptive module probes include the 120-second engine default.
    "F2P-0003": 600,
}

# Hard ceiling for the whole run; remaining batches are skipped once exceeded.
# Kept below the entry-point outer ``timeout`` (default 2000s) so the harness
# always gets a chance to write a complete report instead of being killed.
TOTAL_BUDGET = int(os.environ.get("FT_INTEGRATION_TOTAL_BUDGET", "1500"))

# The documented repository-root variables, in priority order.  Project-name
# derived ``*_REPO`` aliases are intentionally *not* consulted.
REPO_ENV_VARS = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")


def asset_root():
    """Asset root derived from this file's location: <root>/shared/integration."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


ASSETS = asset_root()

# Signals that a tree is the TypeScript source / the Go target respectively.
# ``package.json`` alone is deliberately excluded because the migrated Go tree
# still preserves the public TypeScript test wiring.
_SOURCE_MARKERS = (os.path.join("src", "cli.ts"), os.path.join("bin", "ft.mjs"))
_TARGET_MARKERS = (os.path.join("cmd", "ft", "main.go"), "go.mod")


def _has_marker(root, markers):
    return any(os.path.exists(os.path.join(root, m)) for m in markers)


def _repo_override():
    for var in REPO_ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val
    return None


def resolve_repo(side):
    """Resolve the repository a given side's probes must run against.

    Priority: an explicit documented override, then the read-only authoring
    reference mount, then the runtime working tree.  A candidate is preferred
    when it carries the markers of the requested side; otherwise the first
    existing candidate is used so the probes can still report a precise,
    non-fatal observation.
    """
    override = _repo_override()
    reference = "/input/source" if side == "source" else "/input/target"
    markers = _SOURCE_MARKERS if side == "source" else _TARGET_MARKERS

    candidates = []
    if override:
        candidates.append(override)
    candidates.append(reference)
    candidates.append("/workspace/repo")

    for cand in candidates:
        if cand and os.path.isdir(cand) and _has_marker(cand, markers):
            return cand
    for cand in candidates:
        if cand and os.path.isdir(cand):
            return cand
    return override or "/workspace/repo"


# id -> (status, value)
RESULTS = {}
DIAGNOSTICS = {}
OBSERVATION_OWNERS = {}
CURRENT_BATCH = None
# Per-side repository mapping, populated by ``main``.  Keeping these distinct is
# what stops a source-side probe from resolving onto the Go tree (and vice
# versa).
SOURCE_ROOT = "/input/source"
TARGET_ROOT = "/input/target"
ENV = dict(os.environ)
WORK = ""


def _timeout_bin():
    for cand in ("/usr/bin/timeout", "/bin/timeout"):
        if os.path.exists(cand):
            return cand
    return "timeout"


TIMEOUT_BIN = _timeout_bin()


def build_env(side):
    """Project the resolved repositories onto every alias the probes read."""
    env = dict(os.environ)
    source_repo = SOURCE_ROOT
    target_repo = TARGET_ROOT
    side_repo = source_repo if side == "source" else target_repo

    env["REPO_ROOT"] = source_repo
    env["REPO_ROOT"] = target_repo
    env["FT_SOURCE"] = source_repo
    env["FT_TARGET"] = target_repo
    env["REPO_ROOT"] = side_repo
    env["REPO_ROOT"] = side_repo
    env["FT_SOURCE_REPO"] = source_repo
    env["FT_TARGET_REPO"] = target_repo
    env["FT_REPO"] = side_repo
    for var in REPO_ENV_VARS:
        env[var] = side_repo
    env["FT_SIDE"] = side

    env["FT_ASSET_ROOT"] = ASSETS
    env["FT_ASSETS_ROOT"] = ASSETS
    env.setdefault("FT_HARNESS_WORKDIR", WORK)

    env.setdefault("GOTOOLCHAIN", "local")
    env.setdefault("GOFLAGS", "-mod=mod")
    if not env.get("GOMODCACHE"):
        local = os.path.join(side_repo, ".gomodcache")
        if os.path.isdir(local):
            env["GOMODCACHE"] = local
        elif os.path.isdir(os.path.join(ASSETS, ".gomodcache")):
            env["GOMODCACHE"] = os.path.join(ASSETS, ".gomodcache")
        elif os.path.isdir("/opt/migration/dependencies/gomodcache"):
            env["GOMODCACHE"] = "/opt/migration/dependencies/gomodcache"
    return env


def record(oid, status, value=None):
    if not oid:
        return
    if CURRENT_BATCH is not None and OBSERVATION_OWNERS.get(oid) != CURRENT_BATCH:
        return
    status = str(status if status is not None else "").strip().upper()
    if status not in VALID_STATUS:
        status = "INCONCLUSIVE"
    # The batch that owns an observation is the only writer for it.
    if oid not in RESULTS:
        RESULTS[oid] = [status, value]


def run(cmd, timeout, cwd=None):
    """Run ``cmd`` under an explicit coreutils ``timeout`` and return (rc, out, err)."""
    argv = [TIMEOUT_BIN, "-k", "5", str(int(timeout))] + [str(c) for c in cmd]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=ENV,
            timeout=timeout + 15,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def batch_dir(side, batch_id):
    return os.path.join(ASSETS, side, "batches", batch_id)


def probe(side, batch_id, name):
    return os.path.join(batch_dir(side, batch_id), name)


def _read_if_nonempty(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        return data if data.strip() else None
    except OSError:
        return None


def _extract_json_object(text):
    """Best-effort extraction of a JSON object embedded in probe output."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def _fallback_status(rc):
    if rc == 0:
        return "PASS"
    if rc in (124, 137):
        return "TIMEOUT"
    return "FAIL"


# --------------------------------------------------------------------------- #
# Batch projections
# --------------------------------------------------------------------------- #
def collect_p2p_0001(side):
    path = probe(side, "BATCH-P2P-0001", "probe.sh")
    if not os.path.isfile(path):
        return
    report = os.path.join(WORK, "p2p-0001-%s.json" % side)
    rc, out, _err = run(["sh", path, report], BUDGET["P2P-0001"])
    if _err:
        DIAGNOSTICS['P2P-0001'] = {'text': _err[:1024 * 1024], 'truncated': len(_err) > 1024 * 1024}
        sys.stderr.write('P2P-0001 diagnostics:\n' + _err[-16000:] + '\n')
    text = _read_if_nonempty(report) or out
    doc = _extract_json_object(text)
    found = set()
    def receipt(oid, value):
        if oid not in P2P_0001_IDS:
            return
        status = str(value).upper()
        if status not in VALID_STATUS:
            status = 'INCONCLUSIVE'
        record(oid, status, {'result': status})
        found.add(oid)
    if isinstance(doc, dict):
        observations = doc.get('observations')
        for oid, val in (observations.items() if isinstance(observations, dict) else []):
            result = val.get("result") if isinstance(val, dict) else val
            receipt(oid, result)
    # Malformed text is not a per-case execution receipt. In particular, exit
    # zero cannot supply passing results for a missing or partial report.
    status = 'TIMEOUT' if rc in (124, 137) else 'INFRA_ERROR' if rc in (126, 127) else 'INCONCLUSIVE'
    for oid in P2P_0001_IDS:
        if oid not in found:
            record(oid, status, {'result': status})


def _record_p2p_0002(oid, value, found):
    """Record one normalized P2P-0002 projection; return the updated flag."""
    if oid and isinstance(value, dict) and "result" in value:
        status = str(value.get("result")).upper()
        if status not in VALID_STATUS:
            status = "INCONCLUSIVE"
        record(oid, status, {"result": status})
        found.add(oid)
    return found


def collect_p2p_0002(side):
    path = probe(side, "BATCH-P2P-0002", "probe.sh")
    if not os.path.isfile(path):
        return
    # The runner consumes the ``{report}`` document written by
    # ``p2p_write_report``, which is a single ``.json`` object whose
    # ``observations`` array carries ``observation_id``/``value`` records.  A
    # batch probe may instead stream line-delimited projections to stdout
    # (``p2p_emit_jsonl``), so both shapes are accepted and neither may be
    # silently collapsed into a single exit-code status.
    report = os.path.join(WORK, "p2p-0002-%s.json" % side)
    rc, out, _err = run(["sh", path, report], BUDGET["P2P-0002"])
    if _err:
        DIAGNOSTICS['P2P-0002'] = {'text': _err[:1024 * 1024], 'truncated': len(_err) > 1024 * 1024}
        sys.stderr.write('P2P-0002 diagnostics:\n' + _err[-16000:] + '\n')
    text = _read_if_nonempty(report) or out
    found = set()
    doc = _extract_json_object(text)
    if isinstance(doc, dict):
        rows = doc.get("observations")
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    found = _record_p2p_0002(
                        item.get("observation_id"), item.get("value"), found
                    )
    if not found:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            found = _record_p2p_0002(
                obj.get("observation_id"), obj.get("value"), found
            )
    # A successful batch exit is not evidence that a missing observation ran.
    status = "TIMEOUT" if rc in (124, 137) else "INFRA_ERROR" if rc in (126, 127) else "INCONCLUSIVE"
    for oid in P2P_0002_IDS:
        if oid not in found:
            record(oid, status, {"result": status})


def collect_differential(side):
    name = "target-probe.sh" if side == "target" else "source-probe.sh"
    path = probe(side, "BATCH-DIFFERENTIAL-0001", name)
    if not os.path.isfile(path):
        return
    rc, out, _err = run(["sh", path], BUDGET["DIFFERENTIAL"])
    if _err:
        DIAGNOSTICS['DIFFERENTIAL'] = {'text': _err[:1024 * 1024], 'truncated': len(_err) > 1024 * 1024}
        sys.stderr.write("DIFFERENTIAL diagnostics:\n" + _err[-16000:] + "\n")
    text = out
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r"^(DIFF-\d+)\tresult=(PASS|FAIL|INCONCLUSIVE|INFRA_ERROR|TIMEOUT)$", line)
        if match:
            status = match.group(2)
            record(match.group(1), status, {"result": status})
            seen.add(match.group(1))
            continue
        match = re.match(r"^RESULT\t(DIFF-\d+)\t(PASS|FAIL|INCONCLUSIVE|INFRA_ERROR|TIMEOUT)\b", line)
        if match:
            status = match.group(2)
            record(match.group(1), status, {"result": status})
            seen.add(match.group(1))
    # A successful batch process is not a receipt for any omitted observation.
    # Build/test runner failures also cannot prove all CLI behaviors failed.
    status = "TIMEOUT" if rc in (124, 137) else "INFRA_ERROR" if rc in (126, 127) else "INCONCLUSIVE"
    for oid in DIFF_IDS:
        if oid not in seen:
            record(oid, status, {"result": status})


def collect_contract(side):
    path = probe(side, "BATCH-CONTRACT-0001", "contract-probes.sh")
    if not os.path.isfile(path):
        return
    _rc, out, _err = run(["sh", path], BUDGET["CONTRACT"])
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        oid = obj.get("observation_id")
        if not oid:
            continue
        record(oid, obj.get("status"), obj.get("value", ""))


def collect_state(side):
    path = probe(side, "BATCH-STATE-0001", "run.sh")
    if not os.path.isfile(path):
        return
    _rc, out, _err = run(["sh", path], BUDGET["STATE"])
    if _err:
        sys.stderr.write("STATE diagnostics:\n" + _err[-16000:] + "\n")
    for match in re.finditer(
        r"^\s*(STATE-\d+)\s+(pass|fail)\s*$", out, re.M | re.I
    ):
        status = match.group(2).upper()
        record(match.group(1), status, status)


def collect_f2p_0001(side):
    name = "run-target-probes.sh" if side == "target" else "run-source-probes.sh"
    path = probe(side, "BATCH-F2P-0001", name)
    if not os.path.isfile(path):
        return
    _rc, out, _err = run(["sh", path], BUDGET["F2P-0001"])
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        oid = obj.get("observation_id")
        relation = str(obj.get("relation", "")).lower()
        status = {"pass": "PASS", "fail": "FAIL"}.get(relation, "INCONCLUSIVE")
        record(oid, status, status)


def collect_f2p_0002(side):
    directory = batch_dir(side, "BATCH-F2P-0002")
    if not os.path.isdir(directory):
        return
    deadline = time.time() + BUDGET["F2P-0002"]
    for name in sorted(os.listdir(directory)):
        if not re.match(r"F2P-\d+\.sh$", name):
            continue
        if time.time() > deadline:
            break
        path = os.path.join(directory, name)
        _rc, out, _err = run(["sh", path], BUDGET["F2P-0002-ONE"])
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            oid = obj.get("observation_id")
            if oid:
                status = obj.get("status")
                diagnostic = obj.get('diagnostic_log')
                if isinstance(diagnostic, dict) and isinstance(diagnostic.get('text'), str):
                    text = diagnostic['text']
                    DIAGNOSTICS[oid] = {'text': text[:1024 * 1024],
                                        'truncated': bool(diagnostic.get('truncated')) or len(text) > 1024 * 1024}
                if status != "PASS":
                    detail = str(obj.get("evidence", ""))
                    sys.stderr.write("%s diagnostics: %s\n" % (oid, detail[:4000]))
                record(oid, status, status)
        if _err:
            sys.stderr.write("%s stderr: %s\n" % (name, _err[-4000:]))


def collect_f2p_0003(side):
    name = "run-target-probes.sh" if side == "target" else "run-source-probes.sh"
    path = probe(side, "BATCH-F2P-0003", name)
    if not os.path.isfile(path):
        return
    _rc, out, _err = run(["sh", path], BUDGET["F2P-0003"])
    if _err:
        DIAGNOSTICS['F2P-0003'] = {'text': _err[:1024 * 1024], 'truncated': len(_err) > 1024 * 1024}
        sys.stderr.write('F2P-0003 diagnostics:\n' + _err[-16000:] + '\n')
    for match in re.finditer(r"^OBS\s+(F2P-\d+)\s+(\S+)\s*$", out, re.M):
        status = match.group(2).upper()
        record(match.group(1), status, status)


DISPATCH = {
    "BATCH-P2P-0001": collect_p2p_0001,
    "BATCH-P2P-0002": collect_p2p_0002,
    "BATCH-DIFFERENTIAL-0001": collect_differential,
    "BATCH-CONTRACT-0001": collect_contract,
    "BATCH-STATE-0001": collect_state,
    "BATCH-F2P-0001": collect_f2p_0001,
    "BATCH-F2P-0002": collect_f2p_0002,
    "BATCH-F2P-0003": collect_f2p_0003,
}

DIFF_IDS = []
P2P_0001_IDS = []
P2P_0002_IDS = []


def load_inventory():
    path = os.path.join(ASSETS, "shared", "integration", "observations.json")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("observations", [])


def write_report(path, inventory):
    observations = []
    for item in inventory:
        oid = item["id"]
        kind = item["kind"]
        if oid in RESULTS:
            status, value = RESULTS[oid]
        else:
            status, value = "INCONCLUSIVE", None
        if value is None:
            value = {"result": status}
        observations.append(
            {
                "id": oid,
                "kind": kind,
                "status": status,
                "value": {"type": "json", "value": value},
            }
        )
    # Harbor copies this report into verifier/result.json before disposing of
    # the candidate scratch tree. Keep logs outside scored observation values.
    document = {"schema_version": SCHEMA_VERSION, "observations": observations,
                "diagnostics": DIAGNOSTICS}
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv):
    global WORK, ENV, DIFF_IDS, P2P_0001_IDS, P2P_0002_IDS
    global SOURCE_ROOT, TARGET_ROOT
    global CURRENT_BATCH, OBSERVATION_OWNERS
    if len(argv) < 3:
        sys.stderr.write("usage: harness.py <source|target> <report.json>\n")
        return 2
    side = argv[1]
    report = argv[2]
    if side not in ("source", "target"):
        sys.stderr.write("side must be 'source' or 'target'\n")
        return 2

    SOURCE_ROOT = resolve_repo("source")
    TARGET_ROOT = resolve_repo("target")

    WORK = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "ft-integration-%d" % os.getpid()
    )
    os.makedirs(WORK, exist_ok=True)
    ENV = build_env(side)

    inventory = load_inventory()
    OBSERVATION_OWNERS = {item['id']: item['batch_id'] for item in inventory}
    DIFF_IDS = [o["id"] for o in inventory if o["kind"] == "DIFFERENTIAL"]
    P2P_0001_IDS = [
        o["id"] for o in inventory if o.get("batch_id") == "BATCH-P2P-0001"
    ]
    P2P_0002_IDS = [
        o["id"] for o in inventory if o.get("batch_id") == "BATCH-P2P-0002"
    ]

    started = time.time()
    seen_batches = []
    for item in inventory:
        b = item["batch_id"]
        if b not in seen_batches:
            seen_batches.append(b)

    for b in seen_batches:
        collector = DISPATCH.get(b)
        if collector is None:
            continue
        if time.time() - started > TOTAL_BUDGET:
            sys.stderr.write("integration total budget exceeded; skipping %s\n" % b)
            break
        try:
            CURRENT_BATCH = b
            collector(side)
        except Exception as exc:  # never abort the report because of one batch
            sys.stderr.write("collector %s failed: %r\n" % (b, exc))
        finally:
            CURRENT_BATCH = None

    write_report(report, inventory)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
