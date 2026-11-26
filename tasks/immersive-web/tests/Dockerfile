FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

ENV NPM_CONFIG_CACHE=/opt/npm-cache \
    NPM_CONFIG_PREFER_OFFLINE=true \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        patch \
        python3 \
        python3-pip \
        ripgrep && \
    rm -rf /var/lib/apt/lists/*

ARG NPM_PREWARM_REV=20261027
COPY prewarm/ /opt/prewarm-locked/
RUN cd /opt/prewarm-locked && \
    npm ci --no-audit --no-fund

# Hidden tests use an evaluator-owned toolchain that candidate scripts cannot replace.
RUN mkdir -p /opt/evaluator && \
    cp /opt/prewarm-locked/package.json /opt/prewarm-locked/package-lock.json /opt/evaluator/ && \
    cd /opt/evaluator && \
    npm ci --offline --ignore-scripts --no-audit --no-fund && \
    chown -R root:root /opt/evaluator && \
    chmod -R a-w /opt/evaluator

RUN echo "npm-prewarm-rev=$NPM_PREWARM_REV" && \
    mkdir -p /opt/prewarm-ranges && \
    echo '{"name":"webvr-polyfill-prewarm","version":"0.0.0","private":true,"dependencies":{"cardboard-vr-display":"^1.0.19"},"devDependencies":{"jsdom":"^25.0.1","localStorage":"^1.0.3","vite":"^5.4.10","vitest":"^2.1.3"}}' > /opt/prewarm-ranges/package.json && \
    cd /opt/prewarm-ranges && \
    npm install --no-audit --no-fund && \
    cp -a /opt/npm-cache /opt/npm-cache-candidate

# Candidate lockfiles may pin older compatible releases inside the declared
# ranges. Cache the reference lockfile's exact tarballs in the candidate cache.
RUN cd /opt/prewarm-locked && \
    npm ci --cache /opt/npm-cache-candidate --no-audit --no-fund

# Also cache the upstream lockfile's legacy runtime tarballs. Candidate patches
# may legitimately retain these packages in the frozen runtime dependency tree.
RUN mkdir -p /opt/legacy-prewarm && \
    printf '%s\n' '{"name":"legacy-prewarm","version":"0.0.0","private":true,"dependencies":{"webvr-polyfill-dpdb":"1.0.17"}}' > /opt/legacy-prewarm/package.json && \
    cd /opt/legacy-prewarm && \
    npm install --cache /opt/npm-cache-candidate --no-audit --no-fund

COPY . /tests
COPY ./repo/ /workspace/repo/

RUN rm -rf /workspace/repo/.git && \
    git init -q /workspace/repo && \
    git -C /workspace/repo add -A && \
    GIT_AUTHOR_NAME=evaluator GIT_AUTHOR_EMAIL=evaluator@harbor.local \
    GIT_AUTHOR_DATE="1789182587 +0800" \
    GIT_COMMITTER_NAME=evaluator GIT_COMMITTER_EMAIL=evaluator@harbor.local \
    GIT_COMMITTER_DATE="1789182587 +0800" \
        git -C /workspace/repo commit -q -m baseline-test && \
    test "$(git -C /workspace/repo rev-parse HEAD)" = "a1efca7a94d05af5a048ab3b5d9c12fdc22bafae" && \
    test "$(git -C /workspace/repo rev-parse 'HEAD^{tree}')" = "6fba19571de6113b622e5fc1de1c25fc113b8df7"

# Seed the candidate cache from the packaged upstream lockfile as well as the
# target migration ranges. Candidate patches are allowed to remove or retain
# legacy packages; either case must be evaluated offline without cache misses.
RUN cd /workspace/repo && \
    npm ci --cache /opt/npm-cache-candidate --no-audit --no-fund

RUN python3 /tests/verifier/security_selftest.py
RUN groupadd --gid 10002 evaluator && \
    useradd --create-home --uid 10002 --gid evaluator evaluator && \
    useradd --create-home --uid 10001 candidate && \
    install -d -o evaluator -g evaluator -m 0700 /home/evaluator/tmp && \
    chown -R candidate:candidate /workspace/repo /opt/npm-cache-candidate && \
    chmod -R u+rwX,go+rX /workspace/repo && \
    git config --system --add safe.directory /workspace/repo && \
    chown -R root:evaluator /tests && \
    chmod -R u=rwX,g=rX,o= /tests && \
    chmod +x /tests/test.sh /tests/grader.py \
        /tests/verifier/verifier.py /tests/verifier/hardgate.py \
        /tests/verifier/security_selftest.py
