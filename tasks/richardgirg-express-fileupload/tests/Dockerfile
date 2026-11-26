FROM --platform=linux/amd64 docker.io/blacktt94/harbor-bundle:codex-0.153.4@sha256:20cd21055a368cc29af8a214e335e7df939611eda135a54c3e7a40197246841d

ARG BASE_COMMIT="20269154bb9aab9672e7ff7ca500349e5d23fb5c"
ARG BASE_REPO_SHA256="2e9d6fa1ff2020cfa01d19999e5241be7bf7936d6c2ede2af2c1e983030e1f26"
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG ENVPIPE_RUNTIME_SPEC=node@18.20.8

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN runuser -u agent -- env HOME=/home/agent mise install --yes "$ENVPIPE_RUNTIME_SPEC" \
    && runtime_dir="$(runuser -u agent -- env HOME=/home/agent mise where "$ENVPIPE_RUNTIME_SPEC")" \
    && test -d "$runtime_dir" \
    && ln -s "$runtime_dir" /opt/envpipe/runtime

ENV PATH="/opt/envpipe/runtime/bin:${PATH}" \
    ENVPIPE_RUNTIME_FAMILY="node" \
    ENVPIPE_RUNTIME_VERSION="18.20.8" \
    ENVPIPE_BASE_COMMIT="$BASE_COMMIT" \
    ENVPIPE_REPO_PATH="/workspace/repo" \
    SWEBENCH_REPO_PATH="/workspace/repo" \
    NPM_CONFIG_REGISTRY="$NPM_REGISTRY"

RUN node --version | grep -F 18.20.8

COPY assets/base-repo.tar.gz /tmp/base-repo.tar.gz
RUN printf '%s  %s\n' "$BASE_REPO_SHA256" /tmp/base-repo.tar.gz | sha256sum -c - \
    && mkdir -p /workspace/repo \
    && tar -xzf /tmp/base-repo.tar.gz -C /workspace/repo \
    && rm -f /tmp/base-repo.tar.gz \
    && test "$(git -C /workspace/repo rev-parse HEAD)" = "$BASE_COMMIT" \
    && test -z "$(git -C /workspace/repo status --porcelain)" \
    && git -C /workspace/repo config core.hooksPath /dev/null

RUN cd /workspace/repo && npm ci --no-audit --no-fund --registry="$NPM_REGISTRY"

WORKDIR /workspace/repo
COPY . /tests

# The verifier runs offline (network_mode = no-network), so the extra Koa /
# supertest dependencies are vendored next to their lockfile. Fail the build
# early if the vendored tree is incomplete instead of trying to reach the
# network at evaluation time.
RUN test -d /tests/deps/offline_deps \
    && cp -a /tests/deps/offline_deps /tests/deps/node_modules

RUN test -f /tests/deps/package-lock.json \
    && test -d /tests/deps/node_modules/koa \
    && test -d /tests/deps/node_modules/supertest \
    && test -f /tests/deps/node_modules/koa/package.json

# The verifier runs as root (task.toml [verifier].user = "root") and wraps every
# subprocess that executes candidate code in a private network/PID/mount
# namespace before dropping it to the unprivileged identity below:
#
#   tests/verifier/sandbox.py:
#     unshare --net --pid --fork --mount-proc --kill-child
#     setpriv --reuid --regid --clear-groups --inh-caps=-all --ambient-caps=-all
#            --bounding-set=-all --no-new-privs
#
# The namespace flags need CAP_SYS_ADMIN + CAP_NET_ADMIN and an unconfined
# AppArmor profile, which tests/docker-compose.yaml grants. The privilege drop
# only protects anything if the evaluation assets are *not* readable by that
# identity, so the hidden F2P payload, the node registry and the frozen
# manifests are locked to root while the vendored runtime dependencies the
# candidate legitimately needs stay world-readable.
RUN id -u agent >/dev/null 2>&1 \
    || useradd --system --create-home --shell /usr/sbin/nologin agent

# Dedicated, candidate-owned home/tmp/cache tree. The root is root-owned and
# 0711 (traversable, not listable); the leaves belong to the candidate so node,
# npm and tempfile writes succeed without exposing the evaluator's own home.
RUN mkdir -p /workspace/candidate-sandbox/home /workspace/candidate-sandbox/tmp \
             /workspace/candidate-sandbox/cache /workspace/candidate-sandbox/npm \
    && chown root:root /workspace/candidate-sandbox \
    && chmod 0711 /workspace/candidate-sandbox \
    && chown -R agent:agent /workspace/candidate-sandbox/home \
             /workspace/candidate-sandbox/tmp \
             /workspace/candidate-sandbox/cache \
             /workspace/candidate-sandbox/npm \
    && chmod 0700 /workspace/candidate-sandbox/home \
             /workspace/candidate-sandbox/tmp \
             /workspace/candidate-sandbox/cache \
             /workspace/candidate-sandbox/npm

RUN chmod 0755 /tests /tests/verifier /tests/deps \
    && chmod -R a+rX /tests/deps \
    && chmod 0700 /tests/assets \
    && chmod 0600 /tests/config.json /tests/verifier/test.patch /tests/assets/*.json \
    && chmod +x /tests/test.sh /tests/grader.py \
    /tests/verifier/verifier.py /tests/verifier/hardgate.py /tests/verifier/canary_suite.py

# Build-time permission probes (fail-closed). The namespace barrier and the
# capability drop are only worth anything if the candidate identity really
# cannot read the hidden payload or forge evaluator output; asserting that in
# prose is how it silently regresses, so the image refuses to build otherwise.
# `unshare` itself cannot be probed here (docker build has no CAP_SYS_ADMIN);
# tests/verifier/sandbox.py:check_sandbox_tools() fails closed at runtime.
RUN set -Eeuo pipefail; \
    command -v unshare >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1 \
      || { echo 'the namespace/privilege sandbox tools are missing (util-linux)' >&2; exit 1; }; \
    mkdir -p /logs/verifier; \
    for asset in /tests/config.json /tests/verifier/test.patch \
                 /tests/assets/migration_units.json /tests/assets/f2p_manifest.json \
                 /tests/assets/base_tree.sha256.json; do \
      if setpriv --reuid=agent --regid=agent --clear-groups test -r "$asset"; then \
        echo "candidate unexpectedly read evaluator asset: $asset" >&2; exit 1; \
      fi; \
    done; \
    if ! setpriv --reuid=agent --regid=agent --clear-groups \
         test -r /tests/deps/node_modules/koa/package.json; then \
      echo 'candidate cannot read the vendored runtime dependencies' >&2; exit 1; \
    fi; \
    if setpriv --reuid=agent --regid=agent --clear-groups \
         sh -c 'touch /logs/verifier/candidate-probe 2>/dev/null'; then \
      echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; \
    fi
