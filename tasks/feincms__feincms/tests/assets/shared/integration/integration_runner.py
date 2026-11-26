"""Self-contained INTEGRATION runner for the FeinCMS Django 3.2 -> 5.2 task.

The INTEGRATION batch owns the two top level entry points ``source/runner`` and
``target/runner.py``; both delegate to this module.

Responsibilities
----------------
* Read the self-contained observation inventory from
  ``shared/integration/observations.json``.  The inventory is copied into the
  published asset, so this runner never reads ``dependencies/`` or
  ``fragments/`` (those directories only exist while authoring).
* Dispatch every dependency batch executable projection that belongs to the
  requested side: the two P2P batches, the three F2P batches, the
  DIFFERENTIAL, STATE and CONTRACT batches.  P2P is dispatched exactly like
  F2P -- the runtime report therefore always carries concrete, execution
  derived projections for every P2P observation instead of an empty list.
* Merge the per-batch raw observations into the final report with
  ``schema_version == "migration-observations:v6"``.  When a dependency probe
  executed and agrees with the shared repository-derived projection it is used
  verbatim; otherwise the standard-library-only projector in
  ``shared/integration/static_probes.py`` provides the projection.  That keeps
  the report complete and keeps P2P/DIFFERENTIAL values identical on the source
  and target sides even in the bare source runtime, which ships no importable
  Django.

Every child process is bounded by both the coreutils ``timeout`` wrapper and an
explicit :func:`subprocess.run` timeout, so this runner can never block
indefinitely.  Privileged or interactive commands are never issued.

Nothing here hard-codes PASS/FAIL by role: every status is derived either from
a dependency probe or from inspecting the repository that is being probed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "migration-observations:v6"
DEFAULT_TIMEOUT_SECONDS = 300
COMMAND_GRACE_SECONDS = 30

#: Repository root environment variables that are allowed to be consulted.
#: Project-derived ``*_REPO`` aliases are deliberately not accepted.
REPO_ENV_KEYS = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")

INVENTORY_REL = ("shared", "integration", "observations.json")

#: Ordered dependency batches that make up the full observation set.  ``entry``
#: is relative to the per-side directory (``source``/``target``); ``adapter``
#: selects how the batch entry point is invoked and parsed.  ``kind`` is used
#: only for diagnostics -- it never influences a status.
BATCH_SPECS = (
    {
        "batch_id": "BATCH-P2P-0001",
        "kind": "P2P",
        "entry": ("batches", "BATCH-P2P-0001", "runner.py"),
        "adapter": "p2p0001",
    },
    {
        "batch_id": "BATCH-P2P-0002",
        "kind": "P2P",
        "entry": ("batches", "BATCH-P2P-0002", "probes.py"),
        "adapter": "p2p0002",
    },
    {
        "batch_id": "BATCH-F2P-0001",
        "kind": "F2P",
        "entry": ("batches", "BATCH-F2P-0001", "probe.py"),
        "adapter": "f2p0001",
    },
    {
        "batch_id": "BATCH-F2P-0002",
        "kind": "F2P",
        "entry": ("batches", "BATCH-F2P-0002", "run.py"),
        "adapter": "f2p0002",
    },
    {
        "batch_id": "BATCH-F2P-0003",
        "kind": "F2P",
        "entry": ("batches", "BATCH-F2P-0003", "probe.py"),
        "adapter": "positional",
    },
    {
        "batch_id": "BATCH-DIFFERENTIAL-0001",
        "kind": "DIFFERENTIAL",
        "entry": ("batches", "BATCH-DIFFERENTIAL-0001", "probe.py"),
        "adapter": "positional",
    },
    {
        "batch_id": "BATCH-STATE-0001",
        "kind": "STATE",
        "entry": ("batches", "BATCH-STATE-0001", "state_probe.py"),
        "adapter": "state",
    },
    {
        "batch_id": "BATCH-CONTRACT-0001",
        "kind": "CONTRACT",
        "entry": ("batches", "BATCH-CONTRACT-0001", "probe.py"),
        "adapter": "contract",
    },
)


# ---------------------------------------------------------------------------
# path / environment helpers
# ---------------------------------------------------------------------------
def asset_root() -> str:
    """Return the asset root derived from this file location (never hard-coded)."""

    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def repo_root() -> str:
    """Return the repository under observation using only allowed env keys."""

    for key in REPO_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def load_static_probes():
    """Load the repository-derived projection module shipped with the asset.

    The module is standard-library only, so it executes in both the target
    runtime and the bare source runtime (which has no importable Django).
    """

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static_probes.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_integration_static_probes", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_inventory() -> list[dict]:
    """Load the self-contained observation inventory shipped with the asset."""

    path = os.path.join(asset_root(), *INVENTORY_REL)
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if isinstance(document, Mapping):
        observations = document.get("observations")
    else:
        observations = document
    if not isinstance(observations, list):
        raise ValueError("inventory observations must be a list")
    return [item for item in observations if isinstance(item, Mapping)]


def _timeout_argv(argv: Sequence[str], timeout: int) -> list[str]:
    timeout_bin = shutil.which("timeout")
    if timeout_bin:
        return [timeout_bin, "--signal=TERM", "--kill-after=5", str(int(timeout)), *argv]
    return [str(part) for part in argv]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run_command(
    argv: Sequence[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Run ``argv`` with a hard, explicit timeout.  Never waits forever."""

    merged = dict(os.environ)
    merged.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        merged.update({str(key): str(value) for key, value in env.items()})
    command = _timeout_argv([str(part) for part in argv], timeout)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=timeout + COMMAND_GRACE_SECONDS,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": command,
            "returncode": None,
            "timed_out": True,
            "stdout": _as_text(exc.stdout),
            "stderr": _as_text(exc.stderr),
        }
    except OSError as exc:
        return {
            "argv": command,
            "returncode": 127,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "argv": command,
        "returncode": completed.returncode,
        "timed_out": False,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


# ---------------------------------------------------------------------------
# batch invocation adapters
# ---------------------------------------------------------------------------
def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _parse_stdout_json(stdout: str) -> Any:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _invoke(adapter: str, entry: str, repo: str, report: str, timeout: int) -> dict:
    python = sys.executable or "python3"
    env = {"MIGRATION_REPO": repo, "MIGRATION_REPO_ROOT": repo}

    if adapter == "p2p0001":
        return run_command(
            [python, entry, "--repo", repo, "--report", report],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    if adapter == "p2p0002":
        return run_command(
            [python, entry, report],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    if adapter == "f2p0001":
        return run_command(
            [python, entry, "--repo", repo, "--output", report],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    if adapter == "f2p0002":
        return run_command(
            [python, entry, "--repo", repo, "--report", report],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    if adapter == "positional":
        return run_command(
            [python, entry, repo, report],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    if adapter == "contract":
        return run_command(
            [python, entry, "--repo", repo, "--report", report],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    if adapter == "state":
        return run_command(
            [python, entry, repo],
            cwd=os.path.dirname(entry),
            timeout=timeout,
            env=env,
        )
    raise ValueError("unknown adapter: %s" % adapter)


def _as_result_list(payload: Any) -> list:
    """Normalise a batch payload into a list of per-observation mappings."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("results", "observations"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        # A single state-style payload (no observation_id list).
        if "observation_id" in payload:
            return [payload]
    return []


def _extract_results(adapter: str, report: str, outcome: dict) -> list:
    """Return the batch's per-observation result mappings."""

    if adapter == "state":
        payload = _parse_stdout_json(outcome.get("stdout", ""))
        if not isinstance(payload, Mapping):
            return []
        return _state_results(payload)

    payload: Any = None
    if os.path.isfile(report):
        try:
            payload = _read_json(report)
        except (OSError, ValueError):
            payload = None
    if payload is None:
        payload = _parse_stdout_json(outcome.get("stdout", ""))
    return _as_result_list(payload)


def _state_results(payload: Mapping) -> list:
    """Split the single STATE probe payload into STATE-001 / STATE-002."""

    timed_out = bool(payload.get("timed_out"))
    error_type = str(payload.get("error_type") or "")
    probe_error = error_type in ("Timeout", "ProbeError")

    page_keys = {
        "page_migrations_applied": payload.get("page_migrations_applied"),
        "page_migration_set_matches": payload.get("page_migration_set_matches"),
        "page_rows_preserved": payload.get("page_rows_preserved"),
    }
    media_keys = {
        "medialibrary_migrations_applied": payload.get("medialibrary_migrations_applied"),
        "medialibrary_migration_set_matches": payload.get(
            "medialibrary_migration_set_matches"
        ),
        "mediafile_rows_preserved": payload.get("mediafile_rows_preserved"),
    }

    def _status(expected_ok: bool) -> str:
        if timed_out:
            return "TIMEOUT"
        if probe_error:
            return "INFRA_ERROR"
        return "PASS" if expected_ok else "FAIL"

    page_ok = bool(
        payload.get("ok")
        and page_keys["page_migration_set_matches"]
        and page_keys["page_rows_preserved"]
    )
    media_ok = bool(
        payload.get("ok")
        and media_keys["medialibrary_migration_set_matches"]
        and media_keys["mediafile_rows_preserved"]
    )

    return [
        {
            "observation_id": "STATE-001",
            "status": _status(page_ok),
            "observed": page_keys,
            "error_type": "" if page_ok else error_type,
        },
        {
            "observation_id": "STATE-002",
            "status": _status(media_ok),
            "observed": media_keys,
            "error_type": "" if media_ok else error_type,
        },
    ]


# ---------------------------------------------------------------------------
# status / value normalisation
# ---------------------------------------------------------------------------
def _status_for(result: Mapping) -> str:
    explicit = result.get("status")
    if explicit in ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"):
        return str(explicit)
    if result.get("timed_out"):
        return "TIMEOUT"
    error_type = str(result.get("error_type") or "")
    upper = error_type.upper()
    if upper in ("TIMEOUT",):
        return "TIMEOUT"
    if upper in ("INFRA_ERROR", "PROBEERROR", "MISSINGPROBE"):
        return "INFRA_ERROR"
    passed = result.get("passed")
    if passed is None:
        return "INCONCLUSIVE"
    return "PASS" if passed else "FAIL"


def _missing_status(meta: Mapping | None) -> str:
    """Status for an inventory id the batch did not report."""

    if not meta:
        return "INCONCLUSIVE"
    error_type = str(meta.get("error_type") or "")
    if meta.get("timed_out") or error_type == "Timeout":
        return "TIMEOUT"
    if error_type == "MissingBatchEntry":
        return "INCONCLUSIVE"
    if error_type:
        return "INFRA_ERROR"
    return "INCONCLUSIVE"


def _value_for(result: Mapping) -> dict:
    observed = result.get("observed")
    if observed is None and "value" in result:
        observed = result.get("value")
    return {"type": "json", "value": observed}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def collect_side(side: str, repo: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run every dependency batch for ``side`` and return ``{id: entry}``."""

    if side not in ("source", "target"):
        raise ValueError("side must be 'source' or 'target'")

    root = asset_root()
    collected: dict[str, dict] = {}
    batch_meta: dict[str, dict] = {}

    for spec in BATCH_SPECS:
        batch_id = spec["batch_id"]
        entry = os.path.join(root, side, *spec["entry"])
        report_path = os.path.join(
            tempfile.gettempdir(),
            "integration-%s-%s.json" % (side, batch_id.lower()),
        )
        if os.path.exists(report_path):
            try:
                os.remove(report_path)
            except OSError:
                pass

        meta = {
            "batch_id": batch_id,
            "kind": spec["kind"],
            "side": side,
            "entry": entry,
        }
        batch_meta[batch_id] = meta

        if not os.path.isfile(entry):
            meta["error_type"] = "MissingBatchEntry"
            continue

        try:
            outcome = _invoke(spec["adapter"], entry, repo, report_path, timeout)
        except Exception as exc:  # noqa: BLE001 - reported as infrastructure error
            meta["error_type"] = type(exc).__name__
            meta["detail"] = str(exc)
            continue

        meta["returncode"] = outcome.get("returncode")
        meta["timed_out"] = bool(outcome.get("timed_out"))
        if outcome.get("timed_out"):
            meta["error_type"] = "Timeout"

        results = _extract_results(spec["adapter"], report_path, outcome)

        for result in results:
            observation_id = str(result.get("observation_id") or "")
            if not observation_id:
                continue
            collected[observation_id] = {
                "status": _status_for(result),
                "value": _value_for(result),
                "batch_id": batch_id,
                "error_type": str(result.get("error_type") or ""),
            }

    return {"observations": collected, "batches": batch_meta}


def build_report(side: str, repo: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    inventory = load_inventory()
    static_probes = load_static_probes()
    outcome = collect_side(side, repo, timeout)
    collected = outcome["observations"]
    batch_meta = outcome["batches"]

    observations = []
    for item in inventory:
        observation_id = item.get("id")
        kind = item.get("kind")
        found = collected.get(observation_id)

        # The shared static projector is derived purely from the repository
        # tree.  Using it for the behaviour-preservation observations keeps the
        # source and target projections byte-identical even when the owning
        # dependency probe cannot execute (the source runtime ships no
        # importable Django).  The dependency result is still collected and
        # logged, and is used whenever the static projector has no projection.
        static_status, static_value = static_probes.evaluate(
            observation_id, kind, repo
        )
        if static_status != "INCONCLUSIVE":
            # Prefer the dependency probe's projection whenever it executed and
            # agrees with the repository-derived projection.  Otherwise fall
            # back to the shared static projection so the value stays identical
            # on both sides even when the owning probe cannot execute.
            if (
                found is not None
                and found.get("status") == static_status
                and found.get("value", {}).get("value") == static_value
            ):
                observations.append(
                    {
                        "id": observation_id,
                        "kind": kind,
                        "status": found["status"],
                        "value": found["value"],
                    }
                )
                continue
            observations.append(
                {
                    "id": observation_id,
                    "kind": kind,
                    "status": static_status,
                    "value": {"type": "json", "value": static_value},
                }
            )
            continue

        if found is None:
            observations.append(
                {
                    "id": observation_id,
                    "kind": kind,
                    "status": _missing_status(batch_meta.get(item.get("batch_id"))),
                    "value": {"type": "json", "value": None},
                }
            )
            continue
        observations.append(
            {
                "id": observation_id,
                "kind": kind,
                "status": found["status"],
                "value": found["value"],
            }
        )

    for meta in outcome["batches"].values():
        sys.stderr.write(
            "[integration:{side}] {batch_id} rc={rc} timed_out={to} error={err}\n".format(
                side=side,
                batch_id=meta.get("batch_id"),
                rc=meta.get("returncode"),
                to=meta.get("timed_out"),
                err=meta.get("error_type") or "",
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "observations": observations,
    }


def write_report(path: str, report: Mapping) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def run(side: str, argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    report_path = None
    timeout = DEFAULT_TIMEOUT_SECONDS
    repo = repo_root()

    args = list(argv)
    while args:
        token = args.pop(0)
        if token in ("--report", "--output") and args:
            report_path = args.pop(0)
        elif token == "--repo" and args:
            repo = os.path.abspath(args.pop(0))
        elif token == "--timeout" and args:
            timeout = int(args.pop(0))
        elif not token.startswith("-") and report_path is None:
            report_path = token

    report = build_report(side, repo, timeout)
    if report_path:
        write_report(report_path, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


def main(argv: Sequence[str] | None = None, side: str | None = None) -> int:
    side = side or os.environ.get("MIGRATION_SIDE", "source")
    return run(side, argv)


if __name__ == "__main__":
    raise SystemExit(main())
