FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:c6ad9e9c1d80f6eb607c26b4b0a132ff7a2344fad1c78ea3baa2a7d4c8de6d94

# System packages declared by the repository's own Dockerfile.
RUN apk add --no-cache bash curl tor python3 py3-pip make g++ gcc libc-dev gcompat

# Reachable toolchain caches: the corepack-managed pnpm payload and the
# Playwright browser bundle must live outside the build-time root HOME so the
# offline, unprivileged (uid 1000, HOME=/tmp) replay can reach them.
ENV COREPACK_HOME=/opt/corepack
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Node no longer bundles Corepack, so install it globally exactly as the
# repository's own Dockerfile does, then enable the shim and cache pnpm.
RUN npm install -g corepack
RUN corepack enable && corepack prepare pnpm@10.0.0 --activate

# Install the repository dependency tree with the build-time network and
# publish it as a copyable artifact for offline workspace materialization.
WORKDIR /opt/migration/build
COPY --from=migrationrepositorycontext . .
RUN printf '\npackages:\n  - .\n' >> pnpm-workspace.yaml \
 && pnpm install --frozen-lockfile --store-dir /opt/migration/pnpm-store
# Cache the target HTTP dependencies and supporting MIME package declared by
# candidates. Replays install each candidate's own lockfile offline rather than
# reuse the reference links.
RUN mkdir -p /opt/migration/mime-cache \
 && cd /opt/migration/mime-cache \
 && pnpm add --save-exact --ignore-scripts --store-dir /opt/migration/pnpm-store express@5.1.0 helmet@8.3.0 mime@3.0.0
RUN mkdir -p /opt/migration/dependencies \
 && cp -R node_modules /opt/migration/dependencies/node_modules \
 && chmod -R a+rX /opt/migration/dependencies
RUN chmod -R a+rX /opt/corepack /opt/ms-playwright

WORKDIR /workspace/repo

USER root
WORKDIR /app
COPY . /tests
RUN if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then \
      if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux && rm -rf /var/lib/apt/lists/*; \
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache git ca-certificates python3 util-linux; \
      else exit 1; fi; fi \
    && command -v python3 >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
    && chmod +x /tests/test.sh /tests/grader.py \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts
RUN useradd --system --uid 1000 --create-home --home-dir /opt/harbor-candidate harbor-candidate 2>/dev/null || true; \
    chown -R 1000:1000 /opt/migration/pnpm-store; \
    chmod -R go-rwx /tests; \
    chmod 0700 /tests; \
    chmod 0755 /tests/test.sh /tests/grader.py; \
    if command -v setpriv >/dev/null 2>&1; then \
      setpriv --reuid=1000 --regid=1000 --clear-groups test -r /tests/assets/assets-manifest.json && { echo 'candidate unexpectedly read evaluator assets' >&2; exit 1; }; \
      setpriv --reuid=1000 --regid=1000 --clear-groups sh -c 'touch /logs/candidate-probe' && { echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; }; \
      true; \
    fi
RUN git init /workspace/repo \
    && git -C /workspace/repo fetch /tests/assets/build-deps/repo/base-repo.bundle HEAD \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now
RUN printf '\npackages:\n  - .\n' >> /workspace/repo/pnpm-workspace.yaml
RUN set -e; \
    if [ -e /opt/migration/dependencies/node_modules ]; then rm -rf /workspace/repo/node_modules && cp -a /opt/migration/dependencies/node_modules /workspace/repo/node_modules; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
