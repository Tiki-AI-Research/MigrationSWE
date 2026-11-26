# Build-time only: fetch the crate registry cache for the Rust target.
#
# The migration target is Rust 1.83.0 and the verifier resolves dependencies
# fully offline against a prefetched CARGO_HOME.  The agent must be able to
# build and test the migrated crate under exactly the same toolchain and the
# same registry, otherwise a candidate cannot produce a Cargo.lock that
# resolves within the evaluation registry (see instruction.md).  We reuse the
# verifier's rust image digest so the toolchain is identical, and prefetch the
# locked dependency set here (build-time network is public) so the agent run
# stays fully offline.  This stage is discarded; only the toolchain and the
# registry cache reach the agent image, never the prefetch manifests.
FROM rust@sha256:a45bf1f5d9af0a23b26703b3500d70af1abff7f984a7abef5a104b42c02a292b AS rust-toolchain
ENV CARGO_HOME=/opt/prefetch-cargo
COPY prefetch /prefetch
RUN cd /prefetch \
    && CARGO_NET_OFFLINE=false cargo fetch --locked \
    && rm -rf /prefetch \
    && chmod -R a+rX /opt/prefetch-cargo

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:dalibo__pev2-base-image-20261126@sha256:cd1422680589ab6effa0645d4a79d719a011876de80e03d69c3f4f60d30e757f
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

# Rust 1.83.0 target toolchain, identical to the verifier image, plus the
# offline crate registry so the agent can `cargo build --offline` /
# `cargo test --offline` and commit a Cargo.lock that resolves entirely within
# the evaluation registry.  CARGO_HOME is redirected to the agent home so the
# unprivileged agent can write cargo's lock files; the toolchain itself stays
# read-only and world-readable.
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /opt/prefetch-cargo /home/agent/.cargo
RUN chmod -R a+rX /usr/local/rustup /usr/local/cargo \
    && chown -R agent:agent /home/agent/.cargo
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/home/agent/.cargo \
    CARGO_NET_OFFLINE=true \
    PATH=/usr/local/cargo/bin:$PATH


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh
