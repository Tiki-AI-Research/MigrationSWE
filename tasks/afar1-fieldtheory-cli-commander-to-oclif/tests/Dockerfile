FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d

ENV npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git=1:2.39.5-0+deb12u3 \
        ca-certificates=20250419~deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/migration/dependencies
COPY --from=migrationrepositorycontext package.json package-lock.json ./
RUN npm ci --include=dev --no-audit --no-fund \
    && chmod -R a+rX /opt/migration/dependencies

WORKDIR /workspace/repo

USER root
WORKDIR /app
COPY . /tests
RUN if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then \
      if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends \
        git=1:2.39.5-0+deb12u3 \
        ca-certificates=20250419~deb12u2 \
        python3=3.11.2-1+b1 \
        util-linux=2.38.1-5+deb12u3 \
        && rm -rf /var/lib/apt/lists/*; \
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache git ca-certificates python3 util-linux; \
      else exit 1; fi; fi \
    && command -v python3 >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
    && chmod +x /tests/test.sh /tests/grader.py \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts
# The evaluator's own safety properties are proven at image-build time: the
# startup self-check (reward branches, L1/L2 hard gate, bridge scan, dependency
# probe, bundle policy) and the end-to-end security regression (hard-gate ->
# verifier -> reward artifacts, cheating == -1).  A broken evaluator must never
# reach a scoring run.
RUN python3 /tests/verifier/self_check.py --bundle /tests \
    && python3 /tests/verifier/test_security_regression.py \
    && rm -rf /tests/verifier/__pycache__
RUN useradd --system --uid 1000 --create-home --home-dir /opt/harbor-candidate harbor-candidate 2>/dev/null || true; \
    mkdir -p /tmp/FirefoxHome; \
    chown 1000:1000 /tmp/FirefoxHome; \
    chmod 0755 /tmp/FirefoxHome; \
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
