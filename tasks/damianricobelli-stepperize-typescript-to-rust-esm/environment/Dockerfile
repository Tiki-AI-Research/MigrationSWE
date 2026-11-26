FROM rust@sha256:0ff31c9ffa641a62e48d543fb00b4960955ea375f40776f40f585b89e654cc5e AS rust-toolchain
RUN rustup target add --toolchain 1.85.0 wasm32-unknown-unknown

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:damianricobelli-stepperize-typescript-to-rust-esm-base-image-20261126@sha256:359ef23e2dbecf0b3191773319e084ffbb178b66e026141ba153b817ca0c2bab
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
RUN cd /opt/offline-deps && (npm install --no-save --no-audit --no-fund --cache /opt/offline-deps/npm Rust@1.85.0) || echo 'offline prewarm skipped: npm' >&2
RUN node --version || echo 'offline verify skipped: npm' >&2
RUN npm --version || echo 'offline verify skipped: npm' >&2
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'

# Migration eval harness: provision the declared Node package manager for the agent user.
ENV COREPACK_HOME=/opt/offline-deps/corepack
RUN set -eux; mkdir -p /opt/offline-deps/corepack; COREPACK_HOME=/opt/offline-deps/corepack COREPACK_ENABLE_NETWORK=1 COREPACK_DEFAULT_TO_LATEST=0 corepack install --global pnpm@10.28.0; corepack enable --install-directory /usr/local/bin pnpm; chmod -R a+rX /opt/offline-deps/corepack; test -r /opt/offline-deps/corepack
ENV COREPACK_ENABLE_NETWORK=0
ENV COREPACK_DEFAULT_TO_LATEST=0

# The base image installed node_modules with root's default pnpm store. Agent
# commands must use the same, writable store recorded in .modules.yaml, or an
# offline `pnpm add` fails with ERR_PNPM_UNEXPECTED_STORE before resolving deps.
ENV npm_config_store_dir=/opt/offline-deps/pnpm-store
RUN set -eux; \
    mkdir -p "$npm_config_store_dir"; \
    if [ -d /root/.local/share/pnpm/store ]; then \
      cp -a /root/.local/share/pnpm/store/. "$npm_config_store_dir/"; \
    fi; \
    CI=true NPM_CONFIG_OFFLINE=false pnpm install --force --frozen-lockfile; \
    chown -R agent:agent "$npm_config_store_dir" /workspace/repo; \
    runuser -u agent -- env HOME=/home/agent CI=true NPM_CONFIG_OFFLINE=true \
      pnpm install --offline --frozen-lockfile --ignore-scripts

# The install lifecycle regenerates absolute paths in this tracked docs file.
# Present the supplied baseline to the agent without that infrastructure delta.
RUN git restore --source=f1b5bfafa5d585de47f6c70eeeb355b76b20ff24 --worktree -- apps/docs/.source/dynamic.ts


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# --- migration target toolchain (Rust 1.85.0) ---
# instruction Validation builds/tests the migrated package and the verifier
# compiles the same crate with this exact fixed release, so the agent image must
# ship it too; the agent user drops privileges, so keep the toolchain caches in
# the agent-writable home and pin offline behaviour (run-time egress is the
# model gateway only). The declared target crate dependencies are prewarmed into
# the agent Cargo registry because that fetch needs build-time network.
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/home/agent/.cargo \
    CARGO_TARGET_DIR=/home/agent/target \
    CARGO_NET_OFFLINE=true \
    PATH=/usr/local/cargo/bin:$PATH
RUN set -eux; \
    mkdir -p /home/agent/.cargo /home/agent/target /tmp/napi-prewarm/src; \
    printf '%s\n' \
      '[package]' \
      'name = "napi-prewarm"' \
      'version = "0.0.0"' \
      'edition = "2021"' \
      '[lib]' \
      'crate-type = ["cdylib"]' \
      '[dependencies]' \
      'napi = { version = "=2.16.17", default-features = false, features = ["napi8", "async", "tokio_rt"] }' \
      'napi-derive = "=2.16.13"' \
      '[build-dependencies]' \
      'napi-build = "=2.1.6"' \
      > /tmp/napi-prewarm/Cargo.toml; \
    printf '%s\n' 'pub fn ok() -> u32 { 0 }' > /tmp/napi-prewarm/src/lib.rs; \
    (cd /tmp/napi-prewarm && CARGO_NET_OFFLINE=false cargo fetch) || echo 'offline prewarm skipped: napi' >&2; \
    rm -rf /tmp/napi-prewarm; \
    chown -R agent:agent /home/agent/.cargo /home/agent/target; \
    smoke="$(mktemp -d)"; \
    printf '%s\n' '[package]' 'name = "toolchain-smoke"' 'version = "0.0.0"' 'edition = "2021"' > "$smoke/Cargo.toml"; \
    mkdir -p "$smoke/src"; printf '%s\n' 'pub fn ok() -> u32 { 42 }' > "$smoke/src/lib.rs"; \
    chown -R agent:agent "$smoke"; \
    runuser -u agent -- env HOME=/home/agent PATH="$PATH" CARGO_HOME=/home/agent/.cargo RUSTUP_HOME=/usr/local/rustup CARGO_NET_OFFLINE=true sh -c "cd '$smoke' && rustc --version && cargo --version && cargo build --offline"; \
    rm -rf "$smoke"
