FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134

USER root

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG UV_VERSION=0.7.13
ARG MINI_SWE_AGENT_VERSION=2.4.6
ARG MINI_SWE_AGENT_LOCK_SHA256=caefbe2dd5db9c798476bbe932b0cb1145a7789d66053d5a3648126d3d0ff307
ARG UV_LOCK_SHA256=c58e4738e279a3eeb9e67b1b04f4dc7f1119b54f17872992e7d984aec6dc238d
ARG NODE14_DIST_URL=https://nodejs.org/dist/v14.18.0/node-v14.18.0-linux-x64.tar.gz
ARG NODE14_DIST_SHA256=f411b8aee36d6dc6a5435906f42bd4ea59d6f678894cf562beaf115b58a318ee
ARG TARGETARCH
ARG NODE22_VERSION=22.22.0
ARG NODE22_X64_SHA256=9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37
ARG NODE22_ARM64_SHA256=1bf1eb9ee63ffc4e5d324c0b9b62cf4a289f44332dfef9607cea1a0d9596ba6f
ARG CODEX_VERSION=0.154.0
ARG CLAUDE_CODE_VERSION=2.1.295

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
    ALL_PROXY="" all_proxy="" NO_PROXY="*" no_proxy="*" \
    PIP_INDEX_URL="${PIP_INDEX_URL}" \
    UV_DEFAULT_INDEX="${PIP_INDEX_URL}" \
    UV_TOOL_BIN_DIR="/opt/harbor/bootstrap-bin" \
    LITELLM_LOCAL_MODEL_COST_MAP="True" \
    MSWEA_CONFIG_DIR="/opt/mini-swe-agent" \
    MSWEA_MINI_CONFIG_PATH="/opt/mini-swe-agent/mini.yaml" \
    MSWEA_MODEL_NAME="openai/gemini-3.8-flash" \
    MSWEA_COST_TRACKING="ignore_errors" \
    MSWEA_GLOBAL_COST_LIMIT="0" \
    NPM_CONFIG_CACHE="/opt/npm-cache" \
    NPM_CONFIG_REGISTRY="${NPM_REGISTRY}" \
    NPM_CONFIG_AUDIT="false" \
    NPM_CONFIG_FUND="false"

WORKDIR /workspace
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git nodejs npm procps ripgrep xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Same pinned candidate runtime as the evaluator image (see tests/Dockerfile).
# npm 9 is required because the bundled npm 6 cannot read lockfileVersion 2/3.
RUN curl -fsSL "${NODE14_DIST_URL}" -o /tmp/node14.tar.gz && \
    printf '%s  %s\n' "${NODE14_DIST_SHA256}" /tmp/node14.tar.gz | sha256sum -c - && \
    mkdir -p /opt/node14 && \
    tar -xzf /tmp/node14.tar.gz -C /opt/node14 --strip-components=1 && \
    test "$(/opt/node14/bin/node --version)" = "v14.18.0" && \
    rm -f /tmp/node14.tar.gz && \
    npm install -g npm@9.2.0 --prefix /opt/node14 && \
    test "$(/opt/node14/bin/npm --version)" = "9.2.0"

# Codex CLI runs against a pinned Node 22 toolchain, installed off-PATH so
# candidate code keeps running on the Node 14.18 runtime above.
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


RUN rm -rf /workspace/repo
COPY base-repo.tar.gz /tmp/base-repo.tar.gz
RUN printf '%s  %s\n' \
        '96c7fc220214dbec9dd781bbb5863560053c4f5028da81cd848662eb480fdee0' /tmp/base-repo.tar.gz | sha256sum -c - && \
    mkdir -p /workspace/repo && \
    tar -xzf /tmp/base-repo.tar.gz -C /workspace/repo && \
    rm -f /tmp/base-repo.tar.gz
COPY collect_patch.py /opt/harbor/collect_patch.py
COPY install_codex.sh /tmp/install_codex.sh
COPY install_claude_code.sh /tmp/install_claude_code.sh
COPY mini_swe_agent_config.yaml /tmp/mini_swe_agent_config.yaml
COPY mini_swe_agent_launcher.py /opt/harbor/mini_swe_agent_launcher.py
COPY prewarm_agent_npm.sh /tmp/prewarm_agent_npm.sh
COPY install_mini_swe_agent.sh /tmp/install_mini_swe_agent.sh
COPY mini-swe-agent-requirements.txt /tmp/mini-swe-agent-requirements.txt
COPY uv-requirements.txt /tmp/uv-requirements.txt
COPY target-package.json /tmp/target-package.json
COPY target-package-lock.json /tmp/target-package-lock.json

RUN find /workspace/repo -type f \( -name "*.js" -o -name "*.json" -o -name "*.md" -o -name "*.html" -o -name "*.css" -o -name "*.svg" -o -name "*.yml" -o -name "*.yaml" -o -name ".gitignore" -o -name "LICENSE" \) \
    -exec sed -i 's/\r$//' {} +

RUN git -C /workspace/repo init -q && \
    git -C /workspace/repo config user.name evaluator && \
    git -C /workspace/repo config user.email evaluator@harbor.local && \
    git -C /workspace/repo config core.autocrlf false && \
    chmod 755 /workspace/repo/bin/index.js && \
    git -C /workspace/repo add -A && \
    GIT_AUTHOR_DATE=2000-01-01T00:00:00Z GIT_COMMITTER_DATE=2000-01-01T00:00:00Z \
        git -C /workspace/repo commit -qm "baseline: easy-sharing Express 4" && \
    git -C /workspace/repo rev-parse HEAD > /workspace/repo/.base_commit && \
    git -C /workspace/repo rev-parse HEAD^{tree} > /workspace/repo/.base_tree_hash && \
    printf '%s\n' 'e103cc3124227f42ea1e31f138560cbe7c031904' > /workspace/repo/.source_base_commit && \
    printf '%s\n' '.base_commit' '.base_tree_hash' '.source_base_commit' '.git-fsck-unreachable' '.git-object-inventory' '.git-reflog' >> /workspace/repo/.git/info/exclude && \
    git -C /workspace/repo fsck --full --unreachable > /workspace/repo/.git-fsck-unreachable && \
    git -C /workspace/repo cat-file --batch-all-objects --batch-check > /workspace/repo/.git-object-inventory && \
    git -C /workspace/repo reflog --all > /workspace/repo/.git-reflog

# Preload both sides of the migration into one writable npm cache. The agent
# starts with the Express dependency tree installed; after applying its
# migration, npm can install the Fastify tree entirely offline.
RUN mkdir -p /opt/npm-cache && \
    npm config set cache /opt/npm-cache && \
    cd /workspace/repo && \
    npm ci --ignore-scripts --no-audit --no-fund --registry="${NPM_REGISTRY}" && \
    rm -rf node_modules && \
    npm ci --offline --ignore-scripts --no-audit --no-fund && \
    node -e "for (const name of ['express','express-basic-auth','express-fileupload','archiver','serve-handler']) require.resolve(name)" && \
    rm -rf /tmp/target-prewarm && cp -a /workspace/repo /tmp/target-prewarm && \
    rm -rf /tmp/target-prewarm/node_modules /tmp/target-prewarm/package.json /tmp/target-prewarm/package-lock.json && \
    cp /tmp/target-package.json /tmp/target-prewarm/package.json && \
    cp /tmp/target-package-lock.json /tmp/target-prewarm/package-lock.json && \
    cd /tmp/target-prewarm && \
    npm ci --ignore-scripts --no-audit --no-fund --registry="${NPM_REGISTRY}" && \
    rm -rf node_modules && \
    npm ci --offline --ignore-scripts --no-audit --no-fund && \
    node -e "for (const name of ['fastify','@fastify/multipart','archiver','serve-handler']) require.resolve(name)" && \
    rm -rf /tmp/target-prewarm && \
    chmod -R a+rwX /opt/npm-cache

RUN bash /tmp/prewarm_agent_npm.sh online
RUN --network=none bash /tmp/prewarm_agent_npm.sh offline && \
    rm -f /tmp/prewarm_agent_npm.sh && \
    chmod -R a+rwX /opt/npm-cache

RUN printf '%s  %s\n' "${UV_LOCK_SHA256}" /tmp/uv-requirements.txt | sha256sum -c - && \
    pip3 install --no-cache-dir --break-system-packages --require-hashes --only-binary=:all: \
        --index-url "${PIP_INDEX_URL}" -r /tmp/uv-requirements.txt && \
    printf '%s  %s\n' "${MINI_SWE_AGENT_LOCK_SHA256}" /tmp/mini-swe-agent-requirements.txt | sha256sum -c - && \
    uv venv /opt/mini-swe-agent --python python3 && \
    uv pip install --python /opt/mini-swe-agent/bin/python --require-hashes --only-binary=:all: \
        -r /tmp/mini-swe-agent-requirements.txt && \
    printf '%s\n' '#!/bin/sh' \
        'exec /opt/mini-swe-agent/bin/python -S /opt/harbor/mini_swe_agent_launcher.py "$@"' \
        > /usr/local/bin/mini-swe-agent && \
    chmod 0755 /usr/local/bin/mini-swe-agent && \
    mini-swe-agent --help >/dev/null && \
    mkdir -p "${MSWEA_CONFIG_DIR}" && \
    cp /tmp/mini_swe_agent_config.yaml "${MSWEA_CONFIG_DIR}/mini.yaml" && \
    rm -f /tmp/mini_swe_agent_config.yaml /tmp/install_mini_swe_agent.sh \
        /tmp/mini-swe-agent-requirements.txt /tmp/uv-requirements.txt \
        /tmp/target-package.json /tmp/target-package-lock.json

RUN SKIP_APT_INSTALL=1 NPM_REGISTRY="${NPM_REGISTRY}" CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION}" \
        bash /tmp/install_claude_code.sh && \
    SKIP_APT_INSTALL=1 NPM_REGISTRY="${NPM_REGISTRY}" CODEX_VERSION="${CODEX_VERSION}" \
        bash /tmp/install_codex.sh && \
    rm -f /tmp/install_claude_code.sh /tmp/install_codex.sh && \
    claude --version && \
    codex --version

# Make dependency resolution fail closed at agent runtime. All task Node
# packages used before and after migration were verified in the image above.
ENV NPM_CONFIG_OFFLINE="true"

# Agent commands run on the same pinned runtime the evaluator uses to execute
# candidate code; the distro Node above is build tooling only.
ENV PATH="/opt/node14/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RUN node --version | grep -qx "v14.18.0" && npm --version | grep -qx "9.2.0"

WORKDIR /workspace/repo
