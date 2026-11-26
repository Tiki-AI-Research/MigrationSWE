#!/bin/bash
set -euo pipefail

HOME="${HOME:-/root}"
export HOME

# The harbor-bundle image already has mini-swe-agent 2.4.2 inside
# /opt/harbor-agent, but that venv is not on the candidate PATH. Link the
# existing console script instead of installing another copy.
MINI_BIN="${MINI_SWE_AGENT_BIN:-/opt/harbor-agent/bin/mini-swe-agent}"
[ -x "$MINI_BIN" ] || { echo "mini-swe-agent executable not found at ${MINI_BIN}" >&2; exit 1; }
[ "$MINI_BIN" = /usr/local/bin/mini-swe-agent ] || ln -sf "$MINI_BIN" /usr/local/bin/mini-swe-agent

for extra in mini mini-extra; do
    extra_bin="/opt/harbor-agent/bin/${extra}"
    if [ -x "$extra_bin" ] && [ "$extra_bin" != "/usr/local/bin/${extra}" ]; then
        ln -sf "$extra_bin" "/usr/local/bin/${extra}"
    fi
done

MSWEA_CONFIG_DIR="${MSWEA_CONFIG_DIR:-/opt/mini-swe-agent}"
if [ -f "${MSWEA_CONFIG_SRC:-/tmp/mini_swe_agent_config.yaml}" ]; then
    mkdir -p "$MSWEA_CONFIG_DIR"
    cp "${MSWEA_CONFIG_SRC:-/tmp/mini_swe_agent_config.yaml}" "$MSWEA_CONFIG_DIR/mini.yaml"
fi

mini-swe-agent --help >/dev/null
"$MINI_BIN" --help >/dev/null
printf 'mini-swe-agent linked from %s\n' "$MINI_BIN"
