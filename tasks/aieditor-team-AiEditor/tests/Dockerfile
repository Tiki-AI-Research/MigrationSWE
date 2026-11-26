FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

RUN npm install -g npm@10.9.8

WORKDIR /opt/migration/dependencies
COPY --from=migrationrepositorycontext package.json package-lock.json ./
RUN npm ci --no-audit --no-fund --cache /opt/migration/npm-cache
RUN chmod -R a+rwX /opt/migration/dependencies
# Candidate manifests may change the dependency layout (for example, the SDK
# nests OpenAI 5.x while the baseline declares 7.x). Retain the locked tarballs
# so the verifier can install the submitted lockfile without network access.
RUN chown -R 1000:1000 /opt/migration/npm-cache

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
RUN cd /tests && PYTHONDONTWRITEBYTECODE=1 python3 -c \
      "from verifier.security_regression import run_security_regression; run_security_regression(); print('verifier security regression: OK')"
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

# The reference lockfile above and a candidate's offline SDK migration can resolve
# different transitive versions. Warm the same baseline-to-SDK install used by
# the agent, keeping both dependency layouts without changing the candidate repo.
RUN set -eux; \
    prewarm_dir="$(mktemp -d)"; \
    cp /workspace/repo/package.json /workspace/repo/package-lock.json "$prewarm_dir/"; \
    cd "$prewarm_dir"; \
    npm ci --ignore-scripts --no-audit --no-fund --cache /opt/migration/npm-cache; \
    npm install --ignore-scripts --no-audit --no-fund --cache /opt/migration/npm-cache --save-exact @openai/agents@0.1.0; \
    npm cache add ws@8.22.2 --cache /opt/migration/npm-cache; \
    rm -rf node_modules; \
    chown -R 1000:1000 /opt/migration/npm-cache "$prewarm_dir"; \
    setpriv --reuid=1000 --regid=1000 --clear-groups sh -ec \
      'cd "$1"; npm ci --offline --no-audit --no-fund --cache /opt/migration/npm-cache; npm cache add --offline --cache /opt/migration/npm-cache ws@8.22.2' \
      sh "$prewarm_dir"; \
    cd /app; \
    rm -rf "$prewarm_dir"

# Lockfiles authored against earlier registry metadata can select these SDK
# transitive versions. npm ci --offline silently drops the optional MCP tree
# if even one tarball is absent, although agents-core imports that tree at load
# time. Cache the supported locked versions without rewriting candidate locks
# or substituting the reference dependency installation.
RUN set -eux; \
    for package in \
      @hono/node-server@2.1.6 @hono/node-server@2.1.7 @hono/node-server@2.1.8 @hono/node-server@2.1.9 \
      encodeurl@2.0.1 encodeurl@2.0.2 encodeurl@2.0.3 \
      express-rate-limit@8.7.3 express-rate-limit@8.7.4 \
      hono@4.13.17 hono@4.13.18 hono@4.13.19 ip-address@10.7.6 \
      jose@6.2.16 jose@6.2.17 openai@5.23.2 qs@6.17.1 \
      router@2.3.0 ws@8.22.2 ws@8.23.0; do \
      npm cache add "$package" --cache /opt/migration/npm-cache; \
    done; \
    chown -R 1000:1000 /opt/migration/npm-cache
ENTRYPOINT []
CMD ["/tests/test.sh"]
