FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203

RUN apt-get update \
  && apt-get install -y --no-install-recommends bash ca-certificates git procps \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/migration/build

COPY --from=migrationrepositorycontext package.json package-lock.json .npmrc ./
COPY --from=migrationrepositorycontext lib/node-runtime.js ./lib/node-runtime.js
COPY --from=migrationrepositorycontext lib/scripts/check-node-runtime.js ./lib/scripts/check-node-runtime.js

RUN npm ci --no-audit --no-fund \
  && mkdir -p /opt/migration/dependencies \
  && mv node_modules /opt/migration/dependencies/node_modules \
  && chmod -R a+rX /opt/migration/dependencies

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
RUN PYTHONDONTWRITEBYTECODE=1 python3 /tests/verifier/selfcheck.py --bundle-root /tests
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
