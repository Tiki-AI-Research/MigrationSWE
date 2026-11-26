FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d

ENV CI=1 PATH=/opt/pnpm/bin:$PATH

RUN npm install --global --prefix /opt/pnpm pnpm@10.18.2
RUN mkdir -p /opt/migration/dependencies

WORKDIR /opt/migration/build
COPY --from=migrationrepositorycontext package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY --from=migrationrepositorycontext packages ./packages
COPY --from=migrationrepositorycontext internal ./internal
COPY --from=migrationrepositorycontext scripts ./scripts
COPY --from=migrationrepositorycontext typings ./typings
COPY --from=migrationrepositorycontext tsconfig.base.json tsconfig.json tsconfig.node.json tsconfig.vitest.json tsconfig.web.json ./
RUN pnpm install --frozen-lockfile --ignore-scripts --node-linker=hoisted
RUN cp -a /opt/migration/build/node_modules /opt/migration/dependencies/node_modules
RUN rm -rf /opt/migration/build/packages /opt/migration/build/internal /opt/migration/build/scripts /opt/migration/build/typings \
    /opt/migration/build/tsconfig*.json /opt/migration/build/package.json /opt/migration/build/pnpm-workspace.yaml \
    && chmod -R a+rX /opt/pnpm /opt/migration/dependencies

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
    && MIGRATION_SECURITY_REGRESSION=1 python3 /tests/grader.py \
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
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
