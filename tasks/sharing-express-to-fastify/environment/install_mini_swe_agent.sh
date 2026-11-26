#!/bin/bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
MSWEA_VERSION="${MSWEA_VERSION:-1.16.0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
INSTALL_ROOT="${MSWEA_CONFIG_DIR:-${MINI_SWE_AGENT_HOME:-/opt/mini-swe-agent}}"
HOME="${HOME:-/root}"
export HOME

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Python executable not found: $PYTHON_BIN" >&2
    exit 1
}

"$PYTHON_BIN" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --index-url "$PIP_INDEX_URL" \
    "mini-swe-agent==${MSWEA_VERSION}"

MINI_BIN="$(command -v mini 2>/dev/null || true)"
if [ -z "$MINI_BIN" ]; then
    for candidate in "$HOME/.local/bin/mini" "/usr/local/bin/mini"; do
        if [ -x "$candidate" ]; then MINI_BIN="$candidate"; break; fi
    done
fi
[ -n "$MINI_BIN" ] || { echo "mini executable not found" >&2; exit 1; }
[ "$MINI_BIN" = /usr/local/bin/mini ] || ln -sf "$MINI_BIN" /usr/local/bin/mini

MINI_EXTRA_BIN="$(command -v mini-extra 2>/dev/null || true)"
if [ -n "$MINI_EXTRA_BIN" ] && [ "$MINI_EXTRA_BIN" != /usr/local/bin/mini-extra ]; then
    ln -sf "$MINI_EXTRA_BIN" /usr/local/bin/mini-extra
fi

"$PYTHON_BIN" -c "import minisweagent"
mini-swe-agent --help >/dev/null

MSWEA_CONFIG_DIR="${MSWEA_CONFIG_DIR:-/opt/mini-swe-agent}"
if [ -f "${MSWEA_CONFIG_SRC:-/tmp/mini_swe_agent_config.yaml}" ]; then
    mkdir -p "$MSWEA_CONFIG_DIR"
    cp "${MSWEA_CONFIG_SRC:-/tmp/mini_swe_agent_config.yaml}" "$MSWEA_CONFIG_DIR/mini.yaml"
fi

MINI_VERSION="$($PYTHON_BIN -c 'import importlib.metadata; print(importlib.metadata.version("mini-swe-agent"))')"
printf 'mini-swe-agent %s installed\n' "$MINI_VERSION"
