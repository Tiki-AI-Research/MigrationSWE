# Multi-stage build: reuse the exact Go toolchain the verifier grades with.
# tests/Dockerfile pins this same golang digest, so agent and verifier compile
# the Go 1.23.0 target with an identical release.
FROM golang@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS gotoolchain

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:stremio-stremio-core-base-image-20261126@sha256:22c46286b17a09efe15fa9f687418eaea0f7a857f5607f73b61f4dab9318d493
WORKDIR /workspace/repo

# Migration eval toolchain: the non-root `agent` user must be able to build and
# self-test the Go 1.23.0 target without egress. Copy /usr/local/go from the
# pinned verifier image, expose it on PATH, and pin offline behaviour so the
# stdlib-only target never needs a module proxy.
COPY --from=gotoolchain /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOTOOLCHAIN=local \
    GOPROXY=off \
    GOCACHE=/home/agent/.cache/go-build \
    GOMODCACHE=/home/agent/go/pkg/mod

# Keep the Go caches writable by the non-root agent and fail the build early if
# the offline toolchain cannot compile a stdlib-only program as the agent user.
RUN set -eux; \
    mkdir -p /home/agent/.cache/go-build /home/agent/go/pkg/mod; \
    chown -R agent:agent /home/agent/.cache/go-build /home/agent/go; \
    smoke="$(mktemp -d)"; \
    printf 'module goenvsmoke\n\ngo 1.23.0\n' > "$smoke/go.mod"; \
    printf 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("go-ok") }\n' > "$smoke/main.go"; \
    chown -R agent:agent "$smoke"; \
    runuser -u agent -- env HOME=/home/agent PATH="$PATH" GOCACHE="$GOCACHE" GOMODCACHE="$GOMODCACHE" GOTOOLCHAIN=local GOPROXY=off sh -c "cd '$smoke' && go version && go build ./..."; \
    rm -rf "$smoke"

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent uv python install 3.12 \
 && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi --with mcp \
 && chown -R agent:agent /home/agent

# Migration eval harness: keep the mcp extra when Harbor reinstalls mini-swe-agent.
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
      'if [ "$saw_tool" -eq 1 ] && [ "$saw_install" -eq 1 ] && [ "$saw_agent" -eq 1 ] && [ "$saw_mcp" -eq 0 ]; then' \
      '    exec "$real_uv" "$@" --with mcp' \
      fi \
      'exec "$real_uv" "$@"' \
      > /usr/local/bin/uv; \
    chmod 0755 /usr/local/bin/uv

# Offline dependency prewarm (contract-declared target deps; build-time network).
RUN set -eux; mkdir -p /opt/offline-deps
RUN (cargo fetch) || echo 'offline prewarm skipped: cargo' >&2
RUN cargo --version || echo 'offline verify skipped: cargo' >&2
ENV CARGO_NET_OFFLINE='true'


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh
