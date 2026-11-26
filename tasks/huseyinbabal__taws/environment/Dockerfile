# Migration eval harness: the *target* toolchain (Go 1.23.0) must be present in the
# agent implementation environment, pinned to the exact digest the verifier grades
# with (see tests/Dockerfile).  The Task-Pool source image only ships the Rust
# toolchain, so without this the candidate cannot build or self-test the migrated Go
# implementation (network is allowlisted; go.dev is unreachable).
FROM golang@sha256:32096e84705b30bb39cc9c65ef2896efacc4268203b7876049847763cefc934d AS go-toolchain

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:huseyinbabal__taws-base-image-20261126@sha256:1633d5ce19c27536684b2d427d43d38f3b0835ef12d9d555aa77d3f7a4063056
WORKDIR /workspace/repo

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

# Target toolchain: Go 1.23.0, copied from the verifier's pinned golang image so both
# sides share one toolchain version.  GOTOOLCHAIN=local pins the toolchain (no download
# attempt); GOPROXY=off mirrors the verifier runtime, so the migrated implementation
# must build from the standard library (offline), exactly as the verifier grades it.
COPY --from=go-toolchain /usr/local/go /usr/local/go
ENV GOROOT=/usr/local/go
ENV GOTOOLCHAIN=local
ENV GOPROXY=off
ENV PATH=/usr/local/go/bin:$PATH
RUN chmod -R a+rX /usr/local/go && go version


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh
