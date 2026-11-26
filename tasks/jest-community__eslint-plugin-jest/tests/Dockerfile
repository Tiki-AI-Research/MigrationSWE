FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

# Node runtime is pinned to the agent environment's task-pool Node (v20.20.2) so
# the verifier reproduces the candidate's toolchain exactly (S6). The repository
# declares engines.node "^20.12.0 || ^22.0.0 || >=24.0.0", so 20.x is supported.
FROM node@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

WORKDIR /opt/migration/dependencies/build

COPY --from=migrationrepositorycontext package.json yarn.lock .yarnrc.yml ./
COPY --from=migrationrepositorycontext .yarn/releases/yarn-4.18.0.cjs ./.yarn/releases/yarn-4.18.0.cjs

RUN CI=true node .yarn/releases/yarn-4.18.0.cjs install --immutable \
    && mkdir -p /opt/migration/dependencies \
    && mv node_modules /opt/migration/dependencies/node_modules \
    && find /opt/migration/dependencies/node_modules -type d -exec chmod 0755 {} + \
    && find /opt/migration/dependencies/node_modules -type f -perm /111 -exec chmod 0755 {} + \
    && find /opt/migration/dependencies/node_modules -type f ! -perm /111 -exec chmod 0644 {} +

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
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
