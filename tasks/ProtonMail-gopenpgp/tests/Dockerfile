FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM golang@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db

ENV GOTOOLCHAIN=local

RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*

RUN GOBIN=/usr/local/bin GOMODCACHE=/tmp/tool-mod GOCACHE=/tmp/tool-build go install gotest.tools/gotestsum@v1.12.0 && chmod 0755 /usr/local/bin/gotestsum && rm -rf /tmp/tool-mod /tmp/tool-build

WORKDIR /opt/build
COPY --from=migrationrepositorycontext go.mod go.sum ./
RUN GOMODCACHE=/opt/migration/dependencies/gomodcache GOCACHE=/tmp/build-cache go mod download && chmod -R a+rX /opt/migration/dependencies && rm -rf /tmp/build-cache

ENV GOFLAGS=-mod=mod
ENV GOPROXY=off
ENV GOSUMDB=off
ENV GOMODCACHE=/workspace/repo/.gomodcache
ENV GOCACHE=/workspace/repo/.gocache

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
RUN cd /tests && PYTHONDONTWRITEBYTECODE=1 python3 verifier/security_self_check.py \
    && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest verifier.test_mime_mechanisms verifier.test_regrade_regressions

RUN git init /workspace/repo \
    && git -C /workspace/repo fetch /tests/assets/build-deps/repo/base-repo.bundle HEAD \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now
RUN set -e; \
    if [ -e /opt/migration/dependencies/gomodcache ]; then rm -rf /workspace/repo/.gomodcache && cp -a /opt/migration/dependencies/gomodcache /workspace/repo/.gomodcache; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
