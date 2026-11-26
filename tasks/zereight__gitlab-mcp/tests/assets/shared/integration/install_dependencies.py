"""Install the candidate's declared Python runtime dependencies from image wheels.

The task fixes Python and the MCP wire contract, not a Python SDK version.
Never replace a candidate's declared SDK with the reference SDK.  Missing
cached distributions are evaluator infrastructure errors, not model failures.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tomllib


def requirements(server: Path) -> list[str]:
    project = server / 'pyproject.toml'
    if project.is_file():
        data = tomllib.loads(project.read_text(encoding='utf-8'))
        dependencies = data.get('project', {}).get('dependencies')
        if dependencies is not None:
            if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
                raise ValueError('project.dependencies must be an array of requirement strings')
            return dependencies
    declared = server / 'requirements.txt'
    if declared.is_file():
        return ['-r', str(declared)]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--wheelhouse', required=True)
    args = parser.parse_args()
    server = Path(args.repo) / 'mcp-server'
    dependencies = requirements(server)
    if not dependencies:
        return 0
    completed = subprocess.run([
        sys.executable, '-m', 'pip', 'install', '--no-index', '--only-binary=:all:',
        '--find-links', args.wheelhouse, *dependencies,
    ], cwd=server, check=False)
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
