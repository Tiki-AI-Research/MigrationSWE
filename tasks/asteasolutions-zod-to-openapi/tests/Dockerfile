FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

# Node major pinned to the agent source environment (v24.x) so the candidate is
# built, linted and tested on the same runtime it develops against; a Node 20
# verifier produced cross-runtime false negatives/positives.
FROM node@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9

WORKDIR /opt/migration/build

COPY --from=migrationrepositorycontext package.json package-lock.json ./

RUN npm ci --ignore-scripts && mkdir -p /opt/migration/dependencies /opt/migration/npm-cache && mv node_modules /opt/migration/dependencies/node_modules && chmod -R a+rwX /opt/migration/dependencies /opt/migration/npm-cache

ENV NPM_CONFIG_CACHE=/opt/migration/npm-cache

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
# Collect serialization order from the frozen source before scoring any
# candidate. The normal dependency layer contains target Zod 4.1.0, so install
# the source lockfile separately; never infer source order from target outputs.
# Refresh stages the probes, checks all legacy values and publishes their
# baselines/manifests together. Any failure aborts the image build.
RUN git clone --quiet /tests/assets/build-deps/repo/base-repo.bundle /opt/migration/source-order \
    && cd /opt/migration/source-order \
    && npm ci --ignore-scripts --no-audit --no-fund \
    && python3 /tests/verifier/refresh_order_baseline.py --dependencies /opt/migration/source-order/node_modules \
    && rm -rf /opt/migration/source-order
RUN python3 /tests/verifier/selfcheck.py \
    && cp -a /tests /opt/migration/order-tests \
    && chmod 0700 /opt/migration/order-tests
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
