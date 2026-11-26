import os
import shutil

from contextlib import contextmanager
from pathlib import Path

import pytest

from shiv.bootstrap.environment import Environment


@pytest.fixture
def zip_location():
    return Path(__file__).absolute().parent / "test.zip"


@pytest.fixture
def package_location(tmp_path):
    # pip builds write build/ and egg-info beside setup.py. A per-test copy
    # avoids races with other installers or cleanup in concurrent test runs.
    package = tmp_path / "package"
    shutil.copytree(
        Path(__file__).absolute().parent / "package", package,
        ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"),
    )
    return package


@pytest.fixture
def sp():
    return [Path(__file__).absolute().parent / "sp" / "site-packages"]


@pytest.fixture
def env():
    return Environment(
        built_at=str("2019-01-01 12:12:12"),
        build_id=str("test_id"),
        entry_point="test_entry_point",
        script="test_console_script",
        compile_pyc=False,
        extend_pythonpath=False,
        shiv_version="0.0.1",
    )


@pytest.fixture
def env_var():

    @contextmanager
    def _env_var(key, value):
        os.environ[key] = value
        yield
        del os.environ[key]

    return _env_var
