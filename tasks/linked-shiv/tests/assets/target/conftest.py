"""Target test-tree runner fixtures (click 8.1.7)."""

import pytest

from target.runner_registry import invoke


@pytest.fixture
def runner():
    """Invoke the ``shiv`` console command against the target tree."""

    return lambda args, env=None: invoke("shiv", args, env=env)


@pytest.fixture
def info_runner():
    """Invoke the ``shiv-info`` console command against the target tree."""

    return lambda args: invoke("shiv-info", args)
