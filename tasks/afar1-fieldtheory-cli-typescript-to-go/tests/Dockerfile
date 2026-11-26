FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM golang:1.23.0-bookworm
RUN test "$(GOTOOLCHAIN=local go env GOVERSION)" = go1.23.0
ENV GOTOOLCHAIN=local GOPATH=/opt/migration/dependencies/gopath GOMODCACHE=/opt/migration/dependencies/gomodcache GOSUMDB=off
WORKDIR /src
RUN mkdir -p /opt/migration/dependencies
COPY --from=migrationrepositorycontext go.mod go.sum ./
RUN go mod download
RUN GOBIN=/usr/local/bin GOMODCACHE=/root/go/pkg/mod go install gotest.tools/gotestsum@v1.12.0 && gotestsum --version
RUN chmod -R a+rwX /opt/migration/dependencies
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
    && find /tests/assets -type f -name '*.sh' -exec chmod 0755 {} + \
    && chmod 0755 /tests/assets/target/runner \
                 /tests/assets/fixtures/batches/BATCH-CONTRACT-0001/engine-claude-stub \
                 /tests/assets/fixtures/batches/BATCH-CONTRACT-0001/engine-codex-stub \
    && test -x /tests/assets/target/runner \
    && test -x /tests/assets/target/batches/BATCH-STATE-0001/state-001.sh \
    && test -x /tests/assets/target/batches/BATCH-STATE-0001/state-004.sh \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts
RUN useradd --system --uid 1001 --create-home --home-dir /opt/harbor-candidate harbor-candidate 2>/dev/null || true; \
    chmod -R go-rwx /tests; \
    chmod 0700 /tests; \
    chmod 0755 /tests/test.sh /tests/grader.py; \
    if command -v setpriv >/dev/null 2>&1; then \
      setpriv --reuid=1001 --regid=1001 --clear-groups test -r /tests/assets/assets-manifest.json && { echo 'candidate unexpectedly read evaluator assets' >&2; exit 1; }; \
      setpriv --reuid=1001 --regid=1001 --clear-groups sh -c 'touch /logs/candidate-probe' && { echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; }; \
      true; \
    fi
RUN git init /workspace/repo \
    && git -C /workspace/repo fetch /tests/assets/build-deps/repo/base-repo.bundle HEAD \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now \
    && git -C /workspace/repo rev-parse --git-dir >/dev/null 2>&1 \
    && printf '%s\n' '.harbor-result.json' '__pycache__/' '*.pyc' '*.pyo' '.pytest_cache/' '.mypy_cache/' '.ruff_cache/' '.coverage' '.cache/' >> /workspace/repo/.git/info/exclude
RUN set -e; \
    if [ -e /opt/migration/dependencies/gomodcache ]; then mkdir -p "$(dirname /workspace/repo/.gomodcache)" && rm -rf /workspace/repo/.gomodcache && cp -a /opt/migration/dependencies/gomodcache /workspace/repo/.gomodcache; if [ -d /workspace/repo/.git/info ]; then grep -qxF '.gomodcache' /workspace/repo/.git/info/exclude 2>/dev/null || printf '%s\n' '.gomodcache' >> /workspace/repo/.git/info/exclude; fi; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
