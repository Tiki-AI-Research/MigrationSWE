"""Subprocess isolation shared by migration tests."""

import os
import re
import subprocess


def isolated_python_env(env=None):
    """Remove host import overrides, retaining intentional test PYTHONPATH values.

    Tests often pass a copy of os.environ with SHIV_* overrides. An unchanged
    PYTHONPATH in such a copy is still a host override, not a test fixture.
    """
    result = dict(os.environ if env is None else env)
    result.pop("PYTHONHOME", None)
    if env is None or result.get("PYTHONPATH") == os.environ.get("PYTHONPATH"):
        result.pop("PYTHONPATH", None)
    result["PYTHONNOUSERSITE"] = "1"
    return result


def run_isolated(args, **kwargs):
    kwargs["env"] = isolated_python_env(kwargs.get("env"))
    return subprocess.run(args, **kwargs)


def normalize_info_output(text, input_path, repositories):
    """Keep all output except dependency traceback frames and location details."""
    text = text.replace("\\", "/")
    text = text.replace(str(input_path).replace("\\", "/"), "<INPUT>")
    for repo in repositories:
        text = text.replace(str(repo).replace("\\", "/"), "<SHIV>")
    lines = text.splitlines(keepends=True)
    output = []
    skip_source = False
    for line in lines:
        if skip_source:
            skip_source = False
            continue
        if re.match(r'\s*File ".*/click/[^\"]+", line \d+, in ', line):
            skip_source = True
            continue
        if re.fullmatch(r"\s*[~^]+\s*", line):
            continue  # Python's traceback location indicator
        if re.match(r'\s*File "<SHIV>/', line):
            line = re.sub(r", line \d+, in ", ", line <LINE>, in ", line)
        output.append(line)
    return "".join(output)


def normalize_process_output(text, roots=()):
    """Normalize platform-specific paths while retaining complete output."""
    normalized = text.replace("\\", "/")
    for root in roots:
        normalized = normalized.replace(str(root).replace("\\", "/"), "<ROOT>")
    return re.sub(r'File "[^"]+", line \d+', 'File "<PATH>", line <LINE>', normalized)
