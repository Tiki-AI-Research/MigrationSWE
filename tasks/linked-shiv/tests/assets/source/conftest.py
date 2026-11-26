"""Source test-tree runner fixtures (click 6.7)."""

import pytest

from source.runner_registry import invoke


@pytest.fixture
def runner():
    """Invoke the ``shiv`` console command against the source tree."""

    return lambda args, env=None: invoke("shiv", args, env=env)


@pytest.fixture
def info_runner():
    """Invoke the ``shiv-info`` console command against the source tree."""

    return lambda args: invoke("shiv-info", args)
