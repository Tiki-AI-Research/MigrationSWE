"""Execute the registered migration tests for source-side calibration."""

import importlib.util
import sys
from pathlib import Path

TARGET_RUNNER = Path(__file__).resolve().parents[1] / "target" / "runner.py"


def load_runner():
    """Load the shared runner as a module so the role is a real argument.

    The previous wrapper rewrote the shared source text, which changed only the
    report's ``role`` string and left the runtime digest pointing at the target
    environment.  Passing ``role`` explicitly keeps both halves of the identity
    together.
    """
    spec = importlib.util.spec_from_file_location("migration_runner", TARGET_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: runner.py <report-path>")
    raise SystemExit(load_runner().main(role="source"))
