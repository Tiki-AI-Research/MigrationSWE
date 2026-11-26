# Reuse the verifier's pinned Go toolchain; agent runtime egress is restricted.
FROM golang@sha256:3d699e4d15d0f8f13c9195c0632a16702b8cbdece2955af1c23b37ae5d55a253 AS migrationgotoolchain

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:ramosbugs-oauth2-rs-base-image-20261126@sha256:390239b24bf3bd9f18f7a9b3e29790cf24ba591a0a8fa3ffb029e4955d07b6ce
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

# Migration difficulty reduction: ship a compiling Go export-surface skeleton on
# top of the unchanged base commit. The files are intentionally left UNTRACKED
# (no commit), so the baseline commit/history are untouched and `git add -A` in
# the collect hook still picks them up into model.patch exactly as the model
# leaves them. The agent starts from the public API surface and fills in the
# TODO stubs.
COPY skeleton/ /workspace/repo/
RUN chown -R agent:agent /workspace/repo/go.mod /workspace/repo/oauth2

# Preinstall Codex and Claude Code using marwes-combine's pinned Node 22 setup.
# Keep the project runtime and candidate-only offline settings intact.
USER root
COPY install_agents.sh /tmp/install_agents.sh
RUN candidate_node_before="$(node --version 2>/dev/null || true)" \
 && bash /tmp/install_agents.sh \
 && test "$(node --version 2>/dev/null || true)" = "$candidate_node_before" \
 && rm -f /tmp/install_agents.sh

# The Rust source image has no Go compiler. Install it at build time so agents
# can build, format, and test the Go migration without downloading a toolchain.
COPY --from=migrationgotoolchain /usr/local/go/ /usr/local/go/
ENV GOTOOLCHAIN=local
ENV PATH="/usr/local/go/bin:${PATH}"
# Login shells can reset PATH; keep Go available through /usr/local/bin too.
RUN ln -sf /usr/local/go/bin/go /usr/local/bin/go \
 && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
 && go version \
 && runuser -u agent -- sh -c 'command -v go && command -v gofmt && go version && test "$(go env GOTOOLCHAIN)" = local'

