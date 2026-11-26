# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:release-action-base-image-20261126@sha256:35fa3ba4d05e9526ab49259e915f341ccf3809985d53fdeec91f7909c95cbf4a
WORKDIR /workspace/repo

# Migration eval harness: avoid uv's GitHub-only Python download, which can
# fail before any agent starts. Keep the agent interpreter separate from the
# project's default Python, and make Python 3.12 discoverable during setup.
COPY install_agent_python.sh /tmp/install_agent_python.sh
RUN bash /tmp/install_agent_python.sh && rm -f /tmp/install_agent_python.sh

# Preinstall mini-swe-agent for the agent user's restricted runtime network.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent UV_PYTHON_DOWNLOADS=never uv tool install --python "$(command -v python3.12)" mini-swe-agent --with litellm --with orjson --with fastapi --with mcp \
 && chown -R agent:agent /home/agent

# Migration eval harness: reuse the installed Python when Harbor explicitly
# requests it again, and keep the mcp extra when it reinstalls mini-swe-agent.
RUN set -eux; \
    if ! command -v uv >/dev/null 2>&1; then \
      curl -LsSf https://astral.sh/uv/install.sh | sh; \
      install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; \
    fi; \
    if [ ! -x /usr/local/bin/uv.real ]; then \
      uv_path="$(command -v uv)"; \
      if [ "$uv_path" = "/usr/local/bin/uv" ] && grep -q 'Migration eval harness minimal uv wrapper' "$uv_path"; then \
        echo 'uv wrapper exists without uv.real' >&2; exit 1; \
      fi; \
      install -m 0755 "$uv_path" /usr/local/bin/uv.real; \
    fi; \
    printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      '# Migration eval harness minimal uv wrapper' \
      real_uv=/usr/local/bin/uv.real \
      'if [ "$#" -eq 3 ] && [ "$1" = python ] && [ "$2" = install ] && [ "$3" = 3.12 ]; then' \
      '    if command -v python3.12 >/dev/null 2>&1; then' \
      '        exec python3.12 -c "import bz2, ctypes, lzma, sqlite3, ssl, sys, uuid; assert sys.version_info[:2] == (3, 12)"' \
      '    fi' \
      fi \
      saw_tool=0 \
      saw_install=0 \
      saw_agent=0 \
      saw_mcp=0 \
      previous= \
      'for argument in "$@"; do' \
      '    case "$argument" in' \
      '        tool) saw_tool=1 ;;' \
      '        install) if [ "$saw_tool" -eq 1 ]; then saw_install=1; fi ;;' \
      '        mini-swe-agent|mini-swe-agent==*|mini-swe-agent@*) saw_agent=1 ;;' \
      '        --with=mcp) saw_mcp=1 ;;' \
      '        mcp) if [ "$previous" = "--with" ]; then saw_mcp=1; fi ;;' \
      '    esac' \
      '    previous=$argument' \
      done \
      'if [ "$saw_tool" -eq 1 ] && [ "$saw_install" -eq 1 ] && [ "$saw_agent" -eq 1 ]; then' \
      '    export UV_PYTHON_DOWNLOADS=never' \
      '    if [ "$saw_mcp" -eq 0 ]; then exec "$real_uv" "$@" --with mcp; fi' \
      fi \
      'exec "$real_uv" "$@"' \
      > /usr/local/bin/uv; \
    chmod 0755 /usr/local/bin/uv

# Migration eval harness: install the Go target toolchain (1.23.0) for the agent
# user, plus a warmed module cache so candidates can build/test the migrated code
# offline (the agent's runtime network is restricted to the model host).
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) goarch=amd64 ;; \
      arm64) goarch=arm64 ;; \
      *) echo "unsupported arch $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://go.dev/dl/go1.23.0.linux-${goarch}.tar.gz" -o /tmp/go.tgz; \
    tar -C /usr/local -xzf /tmp/go.tgz; \
    rm -f /tmp/go.tgz; \
    mkdir -p /opt/gopath/pkg/mod /tmp/gomod-warm; \
    printf '%s\n' \
      'module warm' \
      '' \
      'go 1.23.0' \
      '' \
      'require github.com/bmatcuk/doublestar/v4 v4.10.4' \
      > /tmp/gomod-warm/go.mod; \
    printf '%s\n' 'package main' '' 'func main() {}' > /tmp/gomod-warm/main.go; \
    cd /tmp/gomod-warm; \
    GOMODCACHE=/opt/gopath/pkg/mod GOFLAGS= /usr/local/go/bin/go mod download github.com/bmatcuk/doublestar/v4; \
    rm -rf /tmp/gomod-warm; \
    chmod -R a+rwX /opt/gopath
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOTOOLCHAIN=local \
    GOPATH=/opt/gopath \
    GOMODCACHE=/opt/gopath/pkg/mod \
    GOPROXY=off \
    GOFLAGS=-mod=mod

# Preinstall Codex and Claude Code using marwes-combine's pinned Node 22 setup.
# Keep the project runtime and candidate-only offline settings intact.
USER root
COPY install_agents.sh /tmp/install_agents.sh
RUN candidate_node_before="$(node --version 2>/dev/null || true)" \
 && bash /tmp/install_agents.sh \
 && test "$(node --version 2>/dev/null || true)" = "$candidate_node_before" \
 && rm -f /tmp/install_agents.sh

