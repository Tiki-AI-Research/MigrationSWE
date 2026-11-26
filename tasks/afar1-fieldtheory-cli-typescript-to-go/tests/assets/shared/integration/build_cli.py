"""Build the candidate's real Go entry point without prescribing its layout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def packages(text):
    decoder = json.JSONDecoder()
    while text.strip():
        text = text.lstrip()
        row, end = decoder.raw_decode(text)
        yield row
        text = text[end:]


def main():
    root, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    listing = subprocess.run([os.environ.get('FT_GO_BIN') or 'go', 'list', '-json', './...'],
                             cwd=root, text=True, capture_output=True, timeout=120)
    if listing.returncode:
        sys.stderr.write(listing.stderr)
        return listing.returncode
    mains = [row for row in packages(listing.stdout) if row.get('Name') == 'main']
    priority = {'cmd/ft': 0, '.': 1, 'cmd/fieldtheory': 2}
    mains.sort(key=lambda row: (priority.get(os.path.relpath(row['Dir'], root), 3), row['ImportPath']))
    if not mains:
        sys.stderr.write('No Go main package found for ft/fieldtheory\n')
        return 1
    # An existing conventional entry point must build; never hide its failure
    # by falling through to an unrelated executable.
    selected = mains[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run([os.environ.get('FT_GO_BIN') or 'go', 'build', '-o', str(output),
                           selected['ImportPath']], cwd=root, timeout=300).returncode


if __name__ == '__main__':
    raise SystemExit(main())
