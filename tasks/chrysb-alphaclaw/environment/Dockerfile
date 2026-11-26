# Public Node base, the same digest the verifier image uses. The repository is
# checked out from the bundled baseline during this build, so delivery does not
# depend on a prebuilt local image.
FROM node@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203
WORKDIR /workspace/repo

ARG NPM_REGISTRY=https://registry.npmjs.org
ARG CLAUDE_CODE_VERSION=2.1.295
ARG CODEX_VERSION=0.154.0
ARG TARGETARCH
ARG NODE22_VERSION=22.22.0
ARG NODE22_X64_SHA256=9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37
ARG NODE22_ARM64_SHA256=1bf1eb9ee63ffc4e5d324c0b9b62cf4a289f44332dfef9607cea1a0d9596ba6f

# Claude Code and Codex CLI need a Node >=22 toolchain. Keep it off-PATH so the
# candidate keeps using the image's Node runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git procps ripgrep xz-utils libstdc++6 python3 \
    && rm -rf /var/lib/apt/lists/*

COPY base-repo.bundle /tmp/base-repo.bundle
RUN git init /workspace/repo \
    && git -C /workspace/repo fetch /tmp/base-repo.bundle HEAD \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    # The bundle preserves the source tree but has a synthetic root commit.
    # Restore the real, instruction-pinned Git object before exposing the
    # workspace. Never substitute a different tree for the bundled baseline.
    && git -C /workspace/repo fetch --depth=1 https://github.com/chrysb/alphaclaw.git dfe3b2f09b0200a21481e51e9a9b240376d09061 \
    && test "$(git -C /workspace/repo rev-parse HEAD^{tree})" = "$(git -C /workspace/repo rev-parse dfe3b2f09b0200a21481e51e9a9b240376d09061^{tree})" \
    && git -C /workspace/repo checkout --detach dfe3b2f09b0200a21481e51e9a9b240376d09061 \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo config user.email grader@example.invalid \
    && git -C /workspace/repo config user.name "Migration Grader" \
    && git config --global --add safe.directory /workspace/repo \
    && git config --system --add safe.directory /workspace/repo \
    && rm -f /tmp/base-repo.bundle \
    && cd /workspace/repo && npm ci --no-audit --no-fund --cache /opt/offline-deps/npm \
    && useradd --create-home --shell /bin/bash agent \
    && chown -R agent:agent /workspace/repo

RUN case "${TARGETARCH:-amd64}" in \
        amd64) node22_arch=x64; node22_sha="${NODE22_X64_SHA256}" ;; \
        arm64) node22_arch=arm64; node22_sha="${NODE22_ARM64_SHA256}" ;; \
        *)     node22_arch=x64; node22_sha="${NODE22_X64_SHA256}" ;; \
    esac && \
    curl -fsSL "https://nodejs.org/dist/v${NODE22_VERSION}/node-v${NODE22_VERSION}-linux-${node22_arch}.tar.xz" -o /tmp/node22.tar.xz && \
    printf '%s  %s\n' "$node22_sha" /tmp/node22.tar.xz | sha256sum -c - && \
    mkdir -p /opt/node22 && \
    tar -xJf /tmp/node22.tar.xz -C /opt/node22 --strip-components=1 && \
    test "$(/opt/node22/bin/node --version)" = "v${NODE22_VERSION}" && \
    rm -f /tmp/node22.tar.xz

COPY install_claude_code.sh /tmp/install_claude_code.sh
COPY install_codex.sh /tmp/install_codex.sh
COPY mini-swe-agent-requirements.txt /tmp/mini-swe-agent-requirements.txt

# mini-swe-agent: pinned Python environment. Harbor supplies the standard
# agent prompt and completion protocol at runtime.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
    && HOME=/home/agent uv python install 3.12 \
    && uv venv /opt/mini-swe-agent --python 3.12 \
    && uv pip install --python /opt/mini-swe-agent/bin/python --require-hashes --only-binary=:all: -r /tmp/mini-swe-agent-requirements.txt \
    && ln -sf /opt/mini-swe-agent/bin/mini-swe-agent /usr/local/bin/mini-swe-agent \
    && mini-swe-agent --help >/dev/null \
    && chown -R agent:agent /home/agent

# Claude Code and Codex: global CLIs. Codex is wrapped with Node 22 because its
# npm entrypoint is a Node module; Claude Code installs a native executable.
RUN NPM_CONFIG_OFFLINE=false NPM_CONFIG_CACHE=/opt/npm-cache \
        NPM_REGISTRY="${NPM_REGISTRY}" CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION}" SKIP_APT_INSTALL=1 \
        bash /tmp/install_claude_code.sh \
    && NPM_CONFIG_OFFLINE=false NPM_CONFIG_CACHE=/opt/npm-cache \
        NPM_REGISTRY="${NPM_REGISTRY}" CODEX_VERSION="${CODEX_VERSION}" SKIP_APT_INSTALL=1 \
        bash /tmp/install_codex.sh \
    && rm -f /tmp/install_claude_code.sh /tmp/install_codex.sh \
        /tmp/mini-swe-agent-requirements.txt \
    && claude --version \
    && codex --version

# Offline dependency prewarm (contract-declared target deps; build-time network).
RUN set -eux; mkdir -p /opt/offline-deps
RUN cd /opt/offline-deps && npm install --no-save --no-audit --no-fund --cache /opt/offline-deps/npm hono@4.13.7 @hono/node-server@2.1.1
RUN node --version || echo 'offline verify skipped: npm' >&2
RUN npm --version || echo 'offline verify skipped: npm' >&2
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'
ENV LITELLM_LOCAL_MODEL_COST_MAP='True'
ENV MSWEA_MODEL_NAME='openai/gemini-3.8-flash'
ENV MSWEA_COST_TRACKING='ignore_errors'
ENV MSWEA_GLOBAL_COST_LIMIT='0'
