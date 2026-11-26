"""Pytest configuration for the BATCH-F2P-0001 fixtures.

The probes are executed through the timeout guarded harness entry points rather
than through pytest, but this file keeps a deterministic Django bootstrap for
any supplemental unit-level checks.
"""

import os
import sys

import django

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))


def pytest_configure(config):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    if FIXTURES_DIR not in sys.path:
        sys.path.insert(0, FIXTURES_DIR)
    django.setup()
