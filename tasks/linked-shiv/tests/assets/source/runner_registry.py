"""Runner registry for the source (click 6.7) test tree.

The registry exposes the public ``shiv`` console commands as callables so the
integration tests can compare the source and target test trees without
importing click internals directly.  Importing this module must never fail at
collection time on a tree where ``shiv`` is not installed; callers are expected
to skip when the import raises.
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
