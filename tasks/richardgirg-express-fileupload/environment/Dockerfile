# syntax=docker/dockerfile:1.7
# Generated environment definition
# Generator: dockerfile-generator.v1
# Candidate: npm-test-suite

FROM --platform=linux/amd64 docker.io/blacktt94/harbor-bundle:codex-0.153.4@sha256:20cd21055a368cc29af8a214e335e7df939611eda135a54c3e7a40197246841d
ARG REPO_URL="https://github.com/richardgirges/express-fileupload"
ARG BASE_COMMIT="20269154bb9aab9672e7ff7ca500349e5d23fb5c"
ARG BASE_REPO_SHA256="2e9d6fa1ff2020cfa01d19999e5241be7bf7936d6c2ede2af2c1e983030e1f26"
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG CLAUDE_CODE_VERSION=2.1.295

LABEL org.opencontainers.image.source="$REPO_URL" \
      org.opencontainers.image.revision="$BASE_COMMIT" \
      io.envpipe.image.generator="dockerfile-generator.v1" \
      org.opencontainers.image.title="Repository runtime environment" \
      io.envpipe.project.runtime.family="node" \
      io.envpipe.project.runtime.version="18.20.8" \
      io.envpipe.project.runtime.provisioner="mise" \
      io.envpipe.harbor-bundle.digest="sha256:20cd21055a368cc29af8a214e335e7df939611eda135a54c3e7a40197246841d"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       git \
       ripgrep \
    && rm -rf /var/lib/apt/lists/*

ARG ENVPIPE_RUNTIME_SPEC=node@18.20.8

RUN runuser -u agent -- env HOME=/home/agent mise install --yes "$ENVPIPE_RUNTIME_SPEC" \
    && runtime_dir="$(runuser -u agent -- env HOME=/home/agent mise where "$ENVPIPE_RUNTIME_SPEC")" \
    && test -d "$runtime_dir" \
    && ln -s "$runtime_dir" /opt/envpipe/runtime \
    && basename "$runtime_dir" > /opt/envpipe/runtime-install-version \
    && chown root:root /opt/envpipe/runtime-install-version

ENV PATH="/opt/envpipe/runtime/bin:${PATH}" \
    ENVPIPE_RUNTIME_FAMILY=node \
    ENVPIPE_RUNTIME_VERSION="18.20.8"

RUN node --version 2>&1 \
    | grep -F 18.20.8

COPY base-repo.tar.gz /tmp/base-repo.tar.gz
RUN printf '%s  %s\n' "$BASE_REPO_SHA256" /tmp/base-repo.tar.gz | sha256sum -c - \
    && mkdir -p /workspace/repo \
    && tar -xzf /tmp/base-repo.tar.gz -C /workspace/repo \
    && rm -f /tmp/base-repo.tar.gz \
    && test "$(git -C /workspace/repo rev-parse HEAD)" = "$BASE_COMMIT" \
    && test -z "$(git -C /workspace/repo status --porcelain)" \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo config user.name "Harbor Agent" \
    && git -C /workspace/repo config user.email "harbor-agent@localhost"
# The archive includes the exact shallow Git commit and its worktree. Keep its
# object database and reflog intact for the hard gate's provenance checks.

# dependency installation
RUN cd /workspace/repo && npm ci --no-audit --no-fund

RUN mkdir -p /opt/koa-test-deps \
    && npm install --prefix /opt/koa-test-deps \
       --registry="${NPM_REGISTRY}" \
       --no-save --no-package-lock --ignore-scripts \
       --no-audit --no-fund \
       koa@2.16.4

ENV NODE_PATH="/opt/koa-test-deps/node_modules"

RUN cd /workspace/repo \
    && runuser -u agent -- node -e "const assert = require('assert'); \
       assert.strictEqual(require('koa/package.json').version, '2.16.4'); \
       assert.strictEqual(typeof require('koa'), 'function'); \
       console.log('Koa environment ready');"

ENV \
    ENVPIPE_BASE_COMMIT="$BASE_COMMIT" \
    ENVPIPE_REPO_PATH="/workspace/repo" \
    SWEBENCH_REPO_PATH="/workspace/repo"

WORKDIR /workspace/repo

RUN cd /workspace/repo \
    && test "$(git rev-parse HEAD)" = "$BASE_COMMIT" \
    && test -z "$(git status --porcelain)"

RUN chown -R agent:agent /workspace/repo \
    && git config --system --add safe.directory /workspace/repo

# Codex is already on PATH from the harbor bundle. Claude Code is installed
# against that bundle's Node 22, and mini-swe-agent is the bundle venv linked
# onto PATH. Neither install changes the candidate Node 18 runtime above.
COPY install_claude_code.sh install_codex.sh install_mini_swe_agent.sh /tmp/
COPY mini_swe_agent_config.yaml /tmp/mini_swe_agent_config.yaml
# Harbor installs a separate mini-swe-agent tool at runtime with uv. Keep that
# installation on the compatible Responses client instead of resolving the
# latest LiteLLM, which can reject gateway aliases before making a request.
COPY mini_swe_agent_constraints.txt /opt/mini-swe-agent/constraints.txt

ENV NPM_CONFIG_REGISTRY="${NPM_REGISTRY}" \
    MSWEA_CONFIG_DIR="/opt/mini-swe-agent" \
    MSWEA_MINI_CONFIG_PATH="/opt/mini-swe-agent/mini.yaml" \
    UV_CONSTRAINT="/opt/mini-swe-agent/constraints.txt" \
    LITELLM_LOCAL_MODEL_COST_MAP="True"

RUN SKIP_APT_INSTALL=1 NPM_REGISTRY="${NPM_REGISTRY}" CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION}" \
        bash /tmp/install_claude_code.sh \
    && SKIP_APT_INSTALL=1 \
        bash /tmp/install_codex.sh \
    && MSWEA_CONFIG_SRC=/tmp/mini_swe_agent_config.yaml \
        bash /tmp/install_mini_swe_agent.sh \
    && rm -f /tmp/install_claude_code.sh /tmp/install_codex.sh /tmp/install_mini_swe_agent.sh \
        /tmp/mini_swe_agent_config.yaml \
    && claude --version \
    && codex --version \
    && mini-swe-agent --help >/dev/null \
    && node --version | grep -F 18.20.8

CMD ["/bin/bash"]
