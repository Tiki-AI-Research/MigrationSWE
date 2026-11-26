"""Execution driver for the BATCH-F2P-0002 F2P probes.

Runs every probe declared in probe_observations.PROBES against the helper
modules of a single repository root (the source mount or the target mount) and
serialises the raw per-observation results.  It never assumes a side: the same
semantic predicate decides PASS/FAIL/INCONCLUSIVE for both roots.
"""
import json
import os
import sys
import traceback


def _setup_probe_environment():
    os.environ.setdefault("FLOWPILOT_CUSTOM_IMAP_HOST", "127.0.0.1")
    os.environ.setdefault("FLOWPILOT_CUSTOM_IMAP_PORT", "1")
    os.environ.setdefault("FLOWPILOT_CUSTOM_IMAP_TIMEOUT", "2")
    os.environ.setdefault("FLOWPILOT_CUSTOM_IMAP_USER", "probe@example.com")
    os.environ.setdefault("FLOWPILOT_CUSTOM_IMAP_PASS", "probe")
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


def run(root, output):
    _setup_probe_environment()
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import probe_observations

    results = {}
    for observation_id in probe_observations.OBSERVATION_ORDER:
        probe = probe_observations.PROBES[observation_id]
        try:
            ok, detail = probe(root)
            results[observation_id] = {"status": "PASS" if ok else "FAIL", "detail": detail}
        except Exception as exc:  # infrastructure failure, not a semantic verdict
            results[observation_id] = {
                "status": "INCONCLUSIVE",
                "detail": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-1200:],
                },
            }
    probe_observations.shutdown_all()

    payload = {"schema_version": "migration-asset-fragment:v2", "root": root, "results": results}
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return results
