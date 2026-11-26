#!/bin/bash
set -euo pipefail

# The harbor-bundle base image already installs Codex and wraps it at
# /usr/local/bin/codex. Reinstalling would replace that pin, so this script only
# verifies the CLI. Harbor supplies model configuration at runtime.
command -v codex >/dev/null 2>&1 || { echo "codex executable not found" >&2; exit 1; }

codex --version
