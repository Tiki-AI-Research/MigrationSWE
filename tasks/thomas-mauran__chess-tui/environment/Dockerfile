# Use the verifier's pinned Go toolchain and prewarm target dependencies while
# build-time networking is available. Agent execution remains offline.
FROM golang@sha256:81dc45d05a7444ead8c92a389621fafabc8e40f8fd1a19d7e5df14e61e98bc1a AS migrationgo
ENV GOTOOLCHAIN=local
ENV GOMODCACHE=/opt/migration/dependencies/gomodcache
WORKDIR /tmp/go-dependencies
RUN set -eu; \
    test "$(go env GOVERSION)" = go1.25.0; \
    go mod init migration-offline-dependencies; \
    go mod edit -go=1.25.0 \
      -require=github.com/BurntSushi/toml@v1.6.0 \
      -require=github.com/notnil/chess@v1.10.0; \
    go mod download all; \
    go mod download github.com/notnil/chess@v1.9.0 github.com/notnil/chess@v1.3.0 \
      github.com/BurntSushi/toml@v1.4.0 \
      github.com/pelletier/go-toml/v2@v2.2.3 github.com/pelletier/go-toml/v2@v2.2.4; \
    chmod -R a+rX /opt/migration/dependencies

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:thomas-mauran__chess-tui-base-image-20261126@sha256:1f34e075b8c8b56fa81f28e57ed5533e717026a0e16c8a9c05375aef615d9e48
WORKDIR /workspace/repo

COPY --from=migrationgo /usr/local/go /usr/local/go
COPY --from=migrationgo --chown=agent:agent /opt/migration/dependencies/gomodcache /opt/migration/dependencies/gomodcache
ENV PATH="/usr/local/go/bin:${PATH}"
ENV GOTOOLCHAIN=local
ENV GOMODCACHE=/opt/migration/dependencies/gomodcache
ENV GOPROXY=off
ENV GOSUMDB=off
ENV GOFLAGS=""
ENV CGO_ENABLED=0
# Login shells may reset PATH; keep both commands on their standard search path.
RUN ln -sf /usr/local/go/bin/go /usr/local/bin/go \
    && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
    && test "$(go env GOVERSION)" = go1.25.0

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
