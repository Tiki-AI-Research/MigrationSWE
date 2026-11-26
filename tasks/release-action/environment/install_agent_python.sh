#!/usr/bin/env bash
# Install the agent's Python without depending on GitHub release downloads.
set -euo pipefail

agent_python_check='import bz2, ctypes, lzma, sqlite3, ssl, sys, uuid; assert sys.version_info[:2] == (3, 12)'
if command -v python3.12 >/dev/null 2>&1 && python3.12 -c "$agent_python_check"; then
    exit 0
fi

# The pinned candidate image is Debian/Ubuntu based. These libraries keep all
# modules needed by uv's tools available in the source-built interpreter.
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    build-essential ca-certificates curl libbz2-dev libffi-dev liblzma-dev \
    libreadline-dev libsqlite3-dev libssl-dev uuid-dev xz-utils zlib1g-dev
rm -rf /var/lib/apt/lists/*

agent_python_version=3.12.12
agent_python_build=$(mktemp -d /tmp/harbor-agent-python.XXXXXX)
trap 'rm -rf "$agent_python_build"' EXIT
curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 30 --max-time 300 \
    "https://www.python.org/ftp/python/${agent_python_version}/Python-${agent_python_version}.tar.xz" \
    -o "$agent_python_build/Python.tar.xz"
tar -xJf "$agent_python_build/Python.tar.xz" -C "$agent_python_build" --strip-components=1
cd "$agent_python_build"
./configure --prefix=/opt/harbor-python --with-ensurepip=install
# Avoid exhausting the task's two-CPU, 4 GiB build allocation.
make -j2
make altinstall

# Expose only the versioned command; leave python/python3 and project tools alone.
ln -sf /opt/harbor-python/bin/python3.12 /usr/local/bin/python3.12
python3.12 -c "$agent_python_check"
