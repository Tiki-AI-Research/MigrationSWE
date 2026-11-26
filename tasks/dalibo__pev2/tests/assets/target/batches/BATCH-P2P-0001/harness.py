#!/usr/bin/env python3
"""BATCH-P2P-0001 target-side executable P2P projection.

P2P is executable state preservation: this harness executes the target
repository's own test command inside its own container and maps every owned
P2P observation of this batch to the raw PASS/FAIL produced by that real
execution. It never waits forever: every potentially long-running command is
wrapped in an explicit ``timeout`` that hard-kills the process group.

The emitted report and the stdout projection have the exact same shape as the
source-side runner so that, after normalization, both sides compare equal.
"""
import json
import os
import subprocess
import sys

BATCH_ID = "BATCH-P2P-0001"
TIMEOUT_SECONDS = int(os.environ.get("P2P_TIMEOUT_SECONDS", "1800"))
HERE = os.path.dirname(os.path.abspath(__file__))
def _resolve_repo():
    # The integration dispatcher exports the resolved candidate checkout under
    # the conventional root variables; prefer them over a batch-local default so
    # the executable P2P projection runs the repository under test rather than a
    # pristine image checkout.  ``P2P_REPO`` remains an explicit override.
    for key in ("P2P_REPO", "MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO", "WORKSPACE"):
        value = os.environ.get(key)
        if value:
            return value
    return "/workspace/repo"


REPO = _resolve_repo()

# Observation IDs owned by this batch. Used as a fallback when the shared
# fixture cannot be located at runtime.
FALLBACK_IDS = [
    "P2P-001", "P2P-002", "P2P-003", "P2P-004", "P2P-005", "P2P-006",
    "P2P-007", "P2P-008", "P2P-009", "P2P-010", "P2P-011", "P2P-012",
    "P2P-013", "P2P-014", "P2P-015", "P2P-016", "P2P-017", "P2P-018",
    "P2P-019", "P2P-020", "P2P-021", "P2P-022", "P2P-023", "P2P-024",
    "P2P-025", "P2P-026", "P2P-027", "P2P-028", "P2P-029", "P2P-030",
    "P2P-031", "P2P-032", "P2P-033", "P2P-034", "P2P-035", "P2P-036",
    "P2P-037", "P2P-038", "P2P-039", "P2P-040",
]

FIXTURE_CANDIDATES = [
    os.environ.get("P2P_OBSERVATIONS", ""),
    os.path.join("/assets", "fixtures", "batches", BATCH_ID, "observations.json"),
    os.path.normpath(
        os.path.join(HERE, "..", "..", "..", "fixtures", "batches", BATCH_ID, "observations.json")
    ),
]

# Protected fixtures that back this batch and must survive the migration.
PROTECTED_PATHS = [
    os.path.join("src", "__tests__", "filters-formatDuration.spec.ts"),
    os.path.join("src", "services", "__tests__", "from-text.spec.ts"),
]


def load_observation_ids():
    for candidate in FIXTURE_CANDIDATES:
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        ids = [item.get("id") for item in data if isinstance(item, dict) and item.get("id")]
        if ids:
            return ids
    return list(FALLBACK_IDS)


def run_with_timeout(cmd):
    try:
        completed = subprocess.run(
            ["timeout", "--signal=KILL", str(TIMEOUT_SECONDS)] + cmd,
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - defensive
        return False, str(exc)
    return completed.returncode == 0, completed.stdout


def target_tests_pass():
    ok, output = run_with_timeout(["cargo", "test", "--offline"])
    if not ok:
        sys.stderr.write(output)
    return ok


def protected_paths_present():
    return all(os.path.exists(os.path.join(REPO, rel)) for rel in PROTECTED_PATHS)


def main():
    report_path = sys.argv[1] if len(sys.argv) > 1 else "/logs/result.json"
    observation_ids = load_observation_ids()

    status = "PASS"
    if not target_tests_pass():
        status = "FAIL"
    if not protected_paths_present():
        status = "FAIL"

    report = {
        "batch_id": BATCH_ID,
        "result": status,
        "observations": {oid: {"result": status} for oid in observation_ids},
    }
    report_dir = os.path.dirname(report_path)
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")

    print(json.dumps({"result": status}))


if __name__ == "__main__":
    main()
