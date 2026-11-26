FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da

ENV COREPACK_HOME=/opt/migration/dependencies/corepack
ENV COREPACK_DEFAULT_TO_LATEST=0
ENV COREPACK_ENABLE_NETWORK=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/migration/dependencies/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p "$COREPACK_HOME" "$PLAYWRIGHT_BROWSERS_PATH"
RUN COREPACK_ENABLE_NETWORK=1 corepack install --global pnpm@10.26.0
RUN corepack enable --install-directory /usr/local/bin pnpm
RUN chmod -R a+rX "$COREPACK_HOME"

WORKDIR /opt/migration/build
COPY --from=migrationrepositorycontext package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY --from=migrationrepositorycontext console/package.json console/package.json
COPY --from=migrationrepositorycontext contracts/package.json contracts/package.json
COPY --from=migrationrepositorycontext javascript-app/package.json javascript-app/package.json
COPY --from=migrationrepositorycontext labs/package.json labs/package.json
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pnpm install --frozen-lockfile
# Keep the declared HTTP injection utility available to implementations that
# adapt tests with light-my-request; it does not provide a Fastify runtime.
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pnpm --filter @cua-sample/runner add --ignore-scripts --save-exact light-my-request@6.6.0

RUN pnpm --filter @cua-sample/runner playwright:install:with-deps
RUN chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

RUN mkdir -p /opt/migration/dependencies
RUN cp -a node_modules /opt/migration/dependencies/node_modules
RUN cp -a console/node_modules /opt/migration/dependencies/console-node_modules
RUN cp -a contracts/node_modules /opt/migration/dependencies/contracts-node_modules
RUN cp -a javascript-app/node_modules /opt/migration/dependencies/javascript-app-node_modules
RUN cp -a labs/node_modules /opt/migration/dependencies/labs-node_modules

RUN chmod -R a+rX /opt/migration/dependencies

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
RUN set -e; \
    if [ -e /opt/migration/dependencies/node_modules ]; then rm -rf /workspace/repo/node_modules && cp -a /opt/migration/dependencies/node_modules /workspace/repo/node_modules; fi; \
    if [ -e /opt/migration/dependencies/console-node_modules ]; then rm -rf /workspace/repo/console/node_modules && cp -a /opt/migration/dependencies/console-node_modules /workspace/repo/console/node_modules; fi; \
    if [ -e /opt/migration/dependencies/contracts-node_modules ]; then rm -rf /workspace/repo/contracts/node_modules && cp -a /opt/migration/dependencies/contracts-node_modules /workspace/repo/contracts/node_modules; fi; \
    if [ -e /opt/migration/dependencies/javascript-app-node_modules ]; then rm -rf /workspace/repo/javascript-app/node_modules && cp -a /opt/migration/dependencies/javascript-app-node_modules /workspace/repo/javascript-app/node_modules; fi; \
    if [ -e /opt/migration/dependencies/labs-node_modules ]; then rm -rf /workspace/repo/labs/node_modules && cp -a /opt/migration/dependencies/labs-node_modules /workspace/repo/labs/node_modules; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
