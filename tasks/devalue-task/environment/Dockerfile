# syntax=docker/dockerfile:1.7

FROM node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6

ARG REPO_URL="https://github.com/sveltejs/devalue"
ARG BASE_COMMIT="3ba57a6a8e60b171f624b6343be704b23a34af6e"
ARG PNPM_VERSION="10.33.4"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        patch \
        python3 \
        python3-pip \
        ripgrep \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# Harbor can only reach the model gateway at runtime. Install uv and the
# agent dependencies during image construction so bootstrap does not need
# astral.sh or PyPI later.
RUN python3 -m pip install --no-cache-dir --break-system-packages uv

RUN npm install --global "pnpm@${PNPM_VERSION}" \
    && test "$(pnpm --version)" = "${PNPM_VERSION}"

# devalue 4.3.3 pins "packageManager": "pnpm@8.5.1" in package.json. pnpm 10
# otherwise downloads that exact version on every invocation, which the
# network-isolated scored run cannot do. Keep the image's pinned pnpm instead.
ENV npm_config_manage_package_manager_versions=false

RUN git init /workspace/repo \
    && git -C /workspace/repo remote add origin "${REPO_URL}" \
    && for attempt in 1 2 3 4 5; do \
         git -C /workspace/repo -c http.version=HTTP/1.1 \
           -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=120 \
           fetch --depth=1 origin "${BASE_COMMIT}" && break; \
         test "${attempt}" -lt 5 || exit 1; \
         sleep 5; \
       done \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && test "$(git -C /workspace/repo rev-parse HEAD)" = "${BASE_COMMIT}" \
    && git -C /workspace/repo remote remove origin \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now

WORKDIR /workspace/repo

# Public migration contract: the exact target devDependencies and frozen
# lockfile. The agent may read these files and copy them into the workspace;
# they are not part of the repository checkout.
COPY target /opt/migration/target
RUN chmod -R a+rX /opt/migration/target

# Frozen baseline (devalue 4.3.3, uvu) dependencies and prebuild. 4.3.3 ships
# a lockfileVersion 6.0 lockfile, which pnpm 10 rejects as a breaking change,
# so the baseline layers run the pnpm 8 release that wrote it.  Everything at
# run time (and every migration layer below) uses the pinned pnpm 10.33.4.
ARG BASELINE_PNPM_VERSION="8.5.1"
RUN npx --yes "pnpm@${BASELINE_PNPM_VERSION}" install --frozen-lockfile \
    && npx --yes "pnpm@${BASELINE_PNPM_VERSION}" build

# The baseline lockfile is not resolvable with the target toolchain; the agent
# starts from the preinstalled dependency set and migrates the manifest and
# lockfile before running an offline install.

# devalue 6.0.0 migration toolchain (TypeScript 6 / Vitest 5 / Vite 8 /
# dts-buddy / prettier / @js-temporal/polyfill) plus the legacy uvu runner.
# Installed from a standalone manifest so the pristine repo tree stays clean
# (the final `git status --porcelain` check must pass), then staged as the
# dependency set materialized into the agent workspace at runtime.
RUN mkdir -p /opt/migration/dependencies/build \
    && cat > /opt/migration/dependencies/build/package.json <<'JSON'
{
  "name": "devalue-migration-dependencies",
  "version": "6.0.0",
  "private": true,
  "type": "module",
  "packageManager": "pnpm@10.33.4",
  "engines": {
    "node": ">=22.17"
  },
  "devDependencies": {
    "@changesets/cli": "^2.29.6",
    "@js-temporal/polyfill": "^0.5.1",
    "@types/node": "^24.12.0",
    "dts-buddy": "^0.8.3",
    "prettier": "^3.2.4",
    "publint": "^0.3.12",
    "typescript": "^6.0.3",
    "uvu": "^0.5.6",
    "vite": "8.3.0",
    "vitest": "5.0.1"
  }
}
JSON

# Pre-populate the shared store with the exact target graph *before* the
# mixed legacy/target install below.  The target lockfile pins older versions
# than a fresh caret resolution can select, so a store filled only by the
# mixed install would not satisfy the evaluator's frozen install.
RUN mkdir -p /opt/migration/target-fetch \
    && cp /opt/migration/target/package.json \
          /opt/migration/target/pnpm-lock.yaml \
          /opt/migration/target-fetch/ \
    && cd /opt/migration/target-fetch \
    && pnpm fetch --frozen-lockfile --store-dir /opt/migration/pnpm-store \
    && rm -rf /opt/migration/target-fetch

RUN cd /opt/migration/dependencies/build \
    && pnpm install --no-frozen-lockfile --store-dir /opt/migration/pnpm-store \
    && mv node_modules /opt/migration/dependencies/node_modules \
    && rm -rf /opt/migration/dependencies/build

# Make the in-image checkout immediately usable with the full dependency set.
RUN rm -rf /workspace/repo/node_modules \
    && cp -a /opt/migration/dependencies/node_modules /workspace/repo/node_modules

RUN mkdir -p /opt/migration \
    && cat > /opt/migration/runtime.json <<'EOF'
{
  "schema_version": "migration-environment-runtime:v3",
  "executor": {"id": "command", "version": "1"},
  "technology": {
    "language": {"name": "JavaScript", "version": "ECMAScript modules"},
    "framework": {"name": "vitest", "version": "5.0.1"},
    "toolchain": {"name": "pnpm", "version": "10.33.4"}
  },
  "workdir": "/workspace/repo",
  "preflight_commands": [
    ["node", "--version"],
    ["pnpm", "--version"]
  ],
  "build_commands": [
    ["pnpm", "--offline", "build"]
  ],
  "native_test_commands": [
    ["pnpm", "--offline", "test"]
  ],
  "harness_command": ["node", "{assets}", "--report", "{report}"],
  "workspace_materializations": [
    {"source": "/opt/migration/dependencies/node_modules", "target": "node_modules", "mode": "copy"}
  ],
  "environment": ["CI=true"],
  "timeout_seconds": 1800
}
EOF

ENV ENVPIPE_BASE_COMMIT="${BASE_COMMIT}" \
    ENVPIPE_REPO_PATH="/workspace/repo" \
    SWEBENCH_REPO_PATH="/workspace/repo"

RUN test "$(git rev-parse HEAD)" = "${BASE_COMMIT}" \
    && test -z "$(git status --porcelain)"

RUN usermod -l agent -d /home/agent -m node \
    && groupmod -n agent node \
    && chown -R agent:agent /workspace /opt/migration \
    && printf 'store-dir=/opt/migration/pnpm-store\n' > /home/agent/.npmrc \
    && chown agent:agent /home/agent/.npmrc

# Install the complete agent toolchain while the image is being built. The
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
# package from its tool-call path without depending on it.
RUN runuser -u agent -- env HOME=/home/agent bash -lc '\
        set -eux; \
        export PATH="$HOME/.local/bin:$PATH"; \
        printf "litellm==1.102.4\n" > "$HOME/.uv-constraints.txt"; \
        export UV_CONSTRAINT="$HOME/.uv-constraints.txt"; \
        uv python install 3.12; \
        uv tool install --python 3.12 mini-swe-agent \
            --with litellm --with orjson --with fastapi; \
        mini-swe-agent --help >/dev/null'

# Expose the agent toolchain on the default PATH so the bootstrap probe
# (`command -v uv`) succeeds before it would attempt a network install.
RUN ln -sf /home/agent/.local/bin/mini-swe-agent /usr/local/bin/mini-swe-agent \
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

# Preinstall Codex and Claude Code using marwes-combine's pinned Node 22 setup.
# Keep the project runtime and candidate-only offline settings intact.
USER root
COPY install_agents.sh /tmp/install_agents.sh
RUN candidate_node_before="$(node --version 2>/dev/null || true)" \
 && bash /tmp/install_agents.sh \
 && test "$(node --version 2>/dev/null || true)" = "$candidate_node_before" \
 && rm -f /tmp/install_agents.sh

USER agent

ENV PATH="/home/agent/.local/bin:${PATH}"

CMD ["/bin/bash"]
