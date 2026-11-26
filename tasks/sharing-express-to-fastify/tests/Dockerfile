ARG PYPI_INDEX_URL=https://pypi.org/simple

FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

USER root

ARG NPM_REGISTRY=https://registry.npmjs.org
ARG APT_MIRROR=http://deb.debian.org
ARG NODE14_DIST_URL=https://nodejs.org/dist/v14.18.0/node-v14.18.0-linux-x64.tar.gz
ARG NODE14_DIST_SHA256=f411b8aee36d6dc6a5435906f42bd4ea59d6f678894cf562beaf115b58a318ee

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_CACHE=/opt/npm-cache \
    HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
    ALL_PROXY="" all_proxy="" NO_PROXY="*" no_proxy="*"

RUN sed -i "s|http://deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
        python3-pip \
        curl \
        iproute2 \
        util-linux \
        tar && \
    rm -rf /var/lib/apt/lists/*

# Candidate behavior is exercised on the minimum supported runtime.  Keep the
# evaluator/tooling Node 20 separate, and verify the official Node 14.18.0
# archive before installing the candidate runtime.
RUN curl -fsSL "${NODE14_DIST_URL}" -o /tmp/node14.tar.gz && \
    printf '%s  %s\n' "${NODE14_DIST_SHA256}" /tmp/node14.tar.gz | sha256sum -c - && \
    mkdir -p /opt/node14 && \
    tar -xzf /tmp/node14.tar.gz -C /opt/node14 --strip-components=1 && \
    test "$(/opt/node14/bin/node --version)" = "v14.18.0" && \
    rm -f /tmp/node14.tar.gz

# The pinned candidate runtime must ship a package manager that can read the
# lockfileVersion 2/3 lockfiles this task uses; the bundled npm 6 cannot.
RUN npm install -g npm@9.2.0 --prefix /opt/node14 && \
    test "$(/opt/node14/bin/npm --version)" = "9.2.0"

# Keep npm's cache stable across project dependency stages.  Harbor scoring is
# offline, so the verifier must consume only tarballs fetched during image
# construction.
RUN npm config set fetch-retries 5 && \
    npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000

ARG OFFLINE_NPM_REGISTRY=https://registry.npmjs.org

COPY assets/wheels /tmp/harbor-wheels
RUN sha256sum /tmp/harbor-wheels/harbor_migration_toolkit-0.1.0-py3-none-any.whl | \
    grep -F "93932ed01824b3035a5e6b63eeb4f5371ba0749eb322b383349952a35ca805a9" && \
    python3 -m pip install --no-index --find-links=/tmp/harbor-wheels --break-system-packages harbor-migration-toolkit==0.1.0

COPY assets/base-repo.tar.gz /tmp/base-repo.tar.gz
COPY target-package.json /tmp/target-package.json
COPY target-package-lock.json /tmp/target-package-lock.json
COPY npm-cache-extra-package.json /tmp/npm-cache-extra-package.json
COPY npm-cache-extra-package-lock.json /tmp/npm-cache-extra-package-lock.json
COPY prewarm_agent_npm.sh /tmp/prewarm_agent_npm.sh
RUN sha256sum /tmp/base-repo.tar.gz | \
    grep -F "96c7fc220214dbec9dd781bbb5863560053c4f5028da81cd848662eb480fdee0"

# Warm the exact target lock, including unchanged transitive dependencies.
RUN mkdir -p /opt/npm-cache /tmp/target-prewarm && \
    tar -xzf /tmp/base-repo.tar.gz -C /tmp/target-prewarm && \
    rm -f /tmp/target-prewarm/package.json /tmp/target-prewarm/package-lock.json && \
    cp /tmp/target-package.json /tmp/target-prewarm/package.json && \
    cp /tmp/target-package-lock.json /tmp/target-prewarm/package-lock.json && \
    npm ci --prefix /tmp/target-prewarm --ignore-scripts --no-audit --no-fund \
        --registry="${NPM_REGISTRY}" --noproxy="*" && \
    node -e "for (const name of ['archiver','zip-stream','fastify','@fastify/multipart']) require.resolve('/tmp/target-prewarm/node_modules/'+name)"

RUN --network=none npm ci --prefix /tmp/target-prewarm --offline --ignore-scripts --no-audit --no-fund && \
    rm -rf /tmp/target-prewarm /tmp/target-package.json /tmp/target-package-lock.json

RUN mkdir -p /tmp/npm-cache-extra && \
    cp /tmp/npm-cache-extra-package.json /tmp/npm-cache-extra/package.json && \
    cp /tmp/npm-cache-extra-package-lock.json /tmp/npm-cache-extra/package-lock.json && \
    npm ci --prefix /tmp/npm-cache-extra --ignore-scripts --no-audit --no-fund \
        --registry="${NPM_REGISTRY}" --replace-registry-host=always --noproxy="*"

RUN --network=none npm ci --prefix /tmp/npm-cache-extra --offline --ignore-scripts --no-audit --no-fund \
        --registry="${OFFLINE_NPM_REGISTRY}" --replace-registry-host=always && \
    rm -rf /tmp/npm-cache-extra /tmp/npm-cache-extra-package.json /tmp/npm-cache-extra-package-lock.json

RUN mkdir -p /workspace/repo && \
    tar -xzf /tmp/base-repo.tar.gz -C /workspace/repo && \
    find /workspace/repo -type f \( -name "*.js" -o -name "*.json" -o -name "*.md" -o -name "*.html" -o -name "*.css" -o -name "*.svg" -o -name "*.yml" -o -name "*.yaml" -o -name ".gitignore" -o -name "LICENSE" \) \
        -exec sed -i 's/\r$//' {} + && \
    git -C /workspace/repo init -q && \
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
    git -C /workspace/repo reflog --all > /workspace/repo/.git-reflog && \
    cd /workspace/repo && \
    npm ci --ignore-scripts --no-audit --no-fund --registry="${OFFLINE_NPM_REGISTRY}" --replace-registry-host=always --noproxy="*" && \
    node -e "for (const name of ['archiver','zip-stream']) require.resolve(name)"

RUN NPM_CONFIG_REGISTRY="${NPM_REGISTRY}" AGENT_NPM_NODE_ROOTS="/opt/node14/bin:/usr/local/bin" \
    bash /tmp/prewarm_agent_npm.sh online
RUN --network=none NPM_CONFIG_REGISTRY="${NPM_REGISTRY}" AGENT_NPM_NODE_ROOTS="/opt/node14/bin:/usr/local/bin" \
    bash /tmp/prewarm_agent_npm.sh offline && \
    rm -f /tmp/prewarm_agent_npm.sh

# Deterministic verifier and candidate dependency checks run offline.
ENV NPM_CONFIG_OFFLINE=true

RUN --network=none npm ci --prefix /workspace/repo --offline --ignore-scripts --no-audit --no-fund

RUN mkdir -p /opt/sharing-base && \
    tar -xzf /tmp/base-repo.tar.gz -C /opt/sharing-base && \
    find /opt/sharing-base -type f \( -name "*.js" -o -name "*.json" -o -name "*.md" -o -name "*.html" -o -name "*.css" -o -name "*.svg" -o -name "*.yml" -o -name "*.yaml" -o -name ".gitignore" -o -name "LICENSE" \) \
        -exec sed -i 's/\r$//' {} + && \
    git -C /opt/sharing-base init -q && \
    git -C /opt/sharing-base config user.name evaluator && \
    git -C /opt/sharing-base config user.email evaluator@harbor.local && \
    git -C /opt/sharing-base config core.autocrlf false && \
    chmod 755 /opt/sharing-base/bin/index.js && \
    git -C /opt/sharing-base add -A && \
    GIT_AUTHOR_DATE=2000-01-01T00:00:00Z GIT_COMMITTER_DATE=2000-01-01T00:00:00Z \
        git -C /opt/sharing-base commit -qm "baseline: easy-sharing Express 4" && \
    git -C /opt/sharing-base rev-parse HEAD > /opt/sharing-base/.base_commit && \
    git -C /opt/sharing-base rev-parse HEAD^{tree} > /opt/sharing-base/.base_tree_hash && \
    printf '%s\n' 'e103cc3124227f42ea1e31f138560cbe7c031904' > /opt/sharing-base/.source_base_commit && \
    printf '%s\n' '.base_commit' '.base_tree_hash' '.source_base_commit' '.git-fsck-unreachable' '.git-object-inventory' '.git-reflog' >> /opt/sharing-base/.git/info/exclude && \
    git -C /opt/sharing-base fsck --full --unreachable > /opt/sharing-base/.git-fsck-unreachable && \
    git -C /opt/sharing-base cat-file --batch-all-objects --batch-check > /opt/sharing-base/.git-object-inventory && \
    git -C /opt/sharing-base reflog --all > /opt/sharing-base/.git-reflog && \
    cd /opt/sharing-base && \
    npm ci --ignore-scripts --no-audit --no-fund --registry="${OFFLINE_NPM_REGISTRY}" --replace-registry-host=always --noproxy="*" && \
    node -e "for (const name of ['archiver','zip-stream']) require.resolve(name)" && \
    rm -f /tmp/base-repo.tar.gz

WORKDIR /tests
COPY . /tests
RUN useradd --system --uid 1001 --create-home --home-dir /home/harbor-candidate harbor-candidate && \
    chmod -R go-rwx /tests && \
    chmod 700 /tests /tests/test.sh && \
    mkdir -p /logs/verifier /logs/artifacts /workspace/candidate-sandboxes /var/lib/harbor-evaluator && \
    chmod 755 /workspace /workspace/repo && \
    chmod 711 /workspace/candidate-sandboxes && \
    chmod 700 /var/lib/harbor-evaluator && \
    chmod 700 /opt/sharing-base && \
    chmod 700 /logs /logs/verifier /logs/artifacts && \
    chmod 700 /workspace/repo/.git && \
    chmod 600 /workspace/repo/.base_commit /workspace/repo/.base_tree_hash \
        /workspace/repo/.source_base_commit /workspace/repo/.git-fsck-unreachable \
        /workspace/repo/.git-object-inventory /workspace/repo/.git-reflog && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /tests/assets/migration-contract.json; then \
        echo 'candidate unexpectedly read evaluator assets' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /tests/verifier/hardgate.py; then \
        echo 'candidate unexpectedly read verifier source' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /opt/sharing-base/bin/app.js; then \
        echo 'candidate unexpectedly read evaluator baseline' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /workspace/repo/.git/HEAD; then \
        echo 'candidate unexpectedly read candidate Git metadata' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /workspace/repo/.base_commit; then \
        echo 'candidate unexpectedly read evaluator baseline marker' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /logs/verifier; then \
        echo 'candidate unexpectedly read evaluator logs' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups sh -c 'touch /logs/candidate-probe'; then \
        echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; \
    fi && \
    if setpriv --reuid=1001 --regid=1001 --clear-groups sh -c 'touch /workspace/repo/.candidate-probe'; then \
        echo 'candidate unexpectedly wrote evaluator repository' >&2; exit 1; \
    fi && \
    python3 -B /tests/verifier/legacy_require_selftest.py && \
    python3 -B /tests/verifier/hardgate_priority_selftest.py && \
    /opt/node14/bin/node /tests/verifier/http_request_selftest.js && \
    python3 -B /tests/verifier/baseline_refresh_selftest.py

CMD ["/tests/test.sh"]
