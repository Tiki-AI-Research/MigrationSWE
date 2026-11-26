"""Runner registry for the target (click 8.1.7) test tree.

The registry mirrors ``source.runner_registry`` so the integration tests can
assert that the public runner surface is preserved across the migration.
Importing this module must never fail at collection time on a tree where
``shiv`` is not installed; callers are expected to skip when the import raises.
"""

from click.testing import CliRunner

from shiv.cli import main as shiv_main
from shiv.info import main as shiv_info_main


RUNNERS = {
    "shiv": shiv_main,
    "shiv-info": shiv_info_main,
}


def invoke(name, args, env=None):
    """Invoke a registered shiv command and return the click result."""

    return CliRunner().invoke(RUNNERS[name], list(args), env=env)
