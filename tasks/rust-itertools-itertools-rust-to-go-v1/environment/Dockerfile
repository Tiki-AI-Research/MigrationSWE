# Target toolchain stage: the Go compiler pinned to the exact digest the verifier
# image uses (tests/Dockerfile + tests/assets/images.manifest.json), so the
# candidate and the evaluator compile with an identical toolchain.
FROM golang@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS gotoolchain

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:rust-itertools-itertools-rust-to-go-v1-base-image-20261126@sha256:a02ba4ba0007f38e6e1561fe04172e0e488cb5c31884921b364e9612989d17e7
WORKDIR /workspace/repo

# The task requires a commit; provide a noninteractive identity for the agent.
RUN git config --system user.name "Harbor Agent" \
 && git config --system user.email "agent@harbor.local"

# Go 1.23.12 (the migration target toolchain) must be available offline: the
# agent runtime has no package-manager egress and the declared target is Go
# 1.23.0. The source digest is the one the verifier pins, so both sides compile
# with an identical toolchain. The build cache lives outside the repository so
# it can never leak into the collected `model.patch`.
COPY --from=gotoolchain /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOTOOLCHAIN=local \
    CGO_ENABLED=0 \
    GOPROXY=off \
    GOSUMDB=off \
    GOTELEMETRY=off \
    GOCACHE="/home/agent/.cache/go-build"
RUN mkdir -p /home/agent/.cache/go-build

# Expose the Go toolchain to login shells too (mirrors the base image's
# /etc/profile.d/envpipe-runtime.sh, which exposes the Rust toolchain).
RUN printf '%s\n' 'case ":${PATH}:" in *:/usr/local/go/bin:*) ;; *) PATH="/usr/local/go/bin:${PATH}" ;; esac' \
      > /etc/profile.d/go-toolchain.sh \
    && chmod 0644 /etc/profile.d/go-toolchain.sh

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

# Build-time verification (fail-fast): the Go toolchain must run offline and
# compile a minimal program using a throwaway cache (keeps the agent cache clean).
RUN set -eux; \
    go version; \
    tmp="$(mktemp -d)"; \
    cd "$tmp"; \
    go mod init verify >/dev/null; \
    printf '%s\n' 'package main' 'func main() { println("ok") }' > main.go; \
    GOCACHE="$tmp/cache" go build ./...; \
    cd /; rm -rf "$tmp"

# Preinstall Codex and Claude Code using the other tasks' pinned Node 22 setup.
# Keep the project's existing Node runtime and offline settings intact.
USER root
COPY install_agents.sh /tmp/install_agents.sh
RUN candidate_node_before="$(node --version 2>/dev/null || true)" \
 && bash /tmp/install_agents.sh \
 && test "$(node --version 2>/dev/null || true)" = "$candidate_node_before" \
 && rm -f /tmp/install_agents.sh
