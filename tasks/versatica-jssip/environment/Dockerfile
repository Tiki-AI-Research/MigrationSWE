# syntax=docker/dockerfile:1.7
# Generated environment definition
# Generator: dockerfile-generator.v1
# Candidate: jssip-ci-node20-linux

FROM --platform=linux/amd64 docker.io/blacktt94/harbor-bundle:codex-0.153.4@sha256:20cd21055a368cc29af8a214e335e7df939611eda135a54c3e7a40197246841d

ARG REPO_URL="https://github.com/versatica/JsSIP"
ARG BASE_COMMIT="7a963d16ae5c17b430b57b307d3f3ba74c0cbb1f"

LABEL org.opencontainers.image.source="$REPO_URL" \
      org.opencontainers.image.revision="$BASE_COMMIT" \
      io.envpipe.image.generator="dockerfile-generator.v1" \
      org.opencontainers.image.title="Repository runtime environment" \
      io.envpipe.project.runtime.family="node" \
      io.envpipe.project.runtime.version="20.20.2" \
      io.envpipe.project.runtime.provisioner="mise" \
      io.envpipe.harbor-bundle.digest="sha256:20cd21055a368cc29af8a214e335e7df939611eda135a54c3e7a40197246841d"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       git \
    && rm -rf /var/lib/apt/lists/*

ARG ENVPIPE_RUNTIME_SPEC=node@20.20.2

RUN runuser -u agent -- env HOME=/home/agent mise install --yes "$ENVPIPE_RUNTIME_SPEC" \
    && runtime_dir="$(runuser -u agent -- env HOME=/home/agent mise where "$ENVPIPE_RUNTIME_SPEC")" \
    && test -d "$runtime_dir" \
    && ln -s "$runtime_dir" /opt/envpipe/runtime \
    && basename "$runtime_dir" > /opt/envpipe/runtime-install-version \
    && chown root:root /opt/envpipe/runtime-install-version

ENV PATH="/opt/envpipe/runtime/bin:${PATH}" \
    ENVPIPE_RUNTIME_FAMILY=node \
    ENVPIPE_RUNTIME_VERSION="20.20.2"

RUN node --version 2>&1 \
    | grep -F 20.20.2

RUN git init /workspace/repo \
    && git -C /workspace/repo remote add origin "$REPO_URL" \
    && git -C /workspace/repo fetch --depth=1 origin "$BASE_COMMIT" \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && test "$(git -C /workspace/repo rev-parse HEAD)" = "$BASE_COMMIT" \
    && git -C /workspace/repo remote remove origin \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now

# dependency installation
RUN cd /workspace/repo && npm ci --cache=/home/agent/.npm

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

# Install the migration target generator at image build time. The container is
# network restricted at run time, so the Peggy CLI must already be present in
# the repository dependency tree for `npm run grammar` to work offline.
RUN cd /workspace/repo \
    && npm install --cache=/home/agent/.npm --no-save --no-package-lock --ignore-scripts peggy@4.2.0 \
    && test -x /workspace/repo/node_modules/.bin/peggy \
    && find /workspace/repo/node_modules -type d -name pegjs -prune -exec rm -rf {} + \
    && rm -f /workspace/repo/node_modules/.bin/pegjs \
    && ! node -e "require.resolve('pegjs')"

# Runtime npm commands run as agent with no registry access. Keep both the
# original dependency tarballs and Peggy's registry metadata in that user's
# cache, and make packages added by the root preinstall writable as well.
# Prove a normal offline dependency/lockfile update works on disposable copies;
# the task repository's original manifest and lockfile must stay unchanged.
RUN chown -R agent:agent /home/agent/.npm /workspace/repo/node_modules \
    && npm_probe_dir="$(mktemp -d /tmp/peggy-offline.XXXXXX)" \
    && cp /workspace/repo/package.json /workspace/repo/package-lock.json "$npm_probe_dir/" \
    && node -e 'const fs = require("fs"); const file = process.argv[1]; const pkg = JSON.parse(fs.readFileSync(file, "utf8")); delete pkg.devDependencies.pegjs; fs.writeFileSync(file, JSON.stringify(pkg));' "$npm_probe_dir/package.json" \
    && chown -R agent:agent "$npm_probe_dir" \
    && runuser -u agent -- bash -c 'cd "$1" && npm install --offline --package-lock-only --ignore-scripts --no-audit --no-fund --save-dev --save-exact peggy@4.2.0' _ "$npm_probe_dir" \
    && rm -rf "$npm_probe_dir"

# Preinstall the complete agent toolchain while the image is being built. The
# agent container is allowlisted to the model endpoint at runtime and must not
# attempt to reach PyPI, GitHub, or the uv installer after startup.
#
# IMPORTANT: the install spec below MUST match Harbor's runtime bootstrap
# exactly -- `uv tool install --python 3.12 mini-swe-agent --with litellm
# --with orjson --with fastapi` -- so that the runtime install is a no-op.
# Adding version pins or extra `--with` entries here changes the recorded
# requirements, which makes `uv tool install` re-resolve at runtime, reach
# PyPI, and fail in the offline sandbox.
#
# LiteLLM is pinned with UV_CONSTRAINT instead of a version in `--with` so the
# recorded requirement stays identical to Harbor's while the resolver still
# uses the known-good release. LiteLLM 1.104.0+ has a circular import when the
# local cost map is used, and 1.106.x additionally imports the optional `mcp`
# package from its tool-call path without depending on it, which makes every
# agent request fail with `ModuleNotFoundError: No module named 'mcp'`.
RUN runuser -u agent -- env HOME=/home/agent bash -lc '\
        set -eux; \
        export PATH="$HOME/.local/bin:$PATH"; \
        printf "litellm==1.102.4\n" > "$HOME/.uv-constraints.txt"; \
        export UV_CONSTRAINT="$HOME/.uv-constraints.txt"; \
        curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh; \
        uv python install 3.12; \
        uv tool install --python 3.12 mini-swe-agent \
            --with litellm --with orjson --with fastapi; \
        mini-swe-agent --help >/dev/null'

# Expose the agent toolchain on the default PATH so the bootstrap probe
# (`command -v uv`) succeeds before it would attempt a network install.
RUN ln -sf /home/agent/.local/bin/uv /usr/local/bin/uv \
    && ln -sf /home/agent/.local/bin/uvx /usr/local/bin/uvx \
    && ln -sf /home/agent/.local/bin/mini-swe-agent /usr/local/bin/mini-swe-agent \
    && command -v uv \
    && command -v mini-swe-agent \
    && runuser -u agent -- env HOME=/home/agent PATH=/home/agent/.local/bin:$PATH \
       mini-swe-agent --help >/dev/null

# Do not let package managers or LiteLLM's optional remote cost-map refresh
# create runtime egress outside Harbor's AI-provider allowlist.
ENV UV_OFFLINE=1 \
    UV_CONSTRAINT=/home/agent/.uv-constraints.txt \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_OFFLINE=true \
    LITELLM_LOCAL_MODEL_COST_MAP=true

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

CMD ["/bin/bash"]
