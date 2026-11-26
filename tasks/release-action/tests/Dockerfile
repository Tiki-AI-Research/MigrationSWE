FROM golang@sha256:60deed95d3888cc5e4d9ff8a10c54e5edc008c6ae3fba6187be6fb592e19e8c0

WORKDIR /workspace/repo

RUN mkdir -p /opt/migration/dependencies/gomodcache /tmp/gomod-warm
RUN printf '%s\n' 'module github.com/ncipollo/release-action' '' 'go 1.23.0' '' 'require github.com/bmatcuk/doublestar/v4 v4.10.4' > /tmp/gomod-warm/go.mod
RUN printf '%s\n' 'github.com/bmatcuk/doublestar/v4 v4.10.4 h1:oTKkRmRuOsKMqFe1AQazd9gGBFNEpOmgKp/xzB97dnA=' 'github.com/bmatcuk/doublestar/v4 v4.10.4/go.mod h1:xBQ8jztBU6kakFMg+8WGxn0c6z1fTSPVIjEY1Wr7jzc=' > /tmp/gomod-warm/go.sum
RUN cd /tmp/gomod-warm && GOMODCACHE=/opt/migration/dependencies/gomodcache go mod download && chmod -R a+rX /opt/migration/dependencies && rm -rf /tmp/gomod-warm
RUN GOBIN=/usr/local/bin go install gotest.tools/gotestsum@v1.12.0 && chmod a+rx /usr/local/bin/gotestsum
ENV GOMODCACHE=/workspace/repo/.gomodcache
ENV GOCACHE=/workspace/repo/.gocache
ENV GOFLAGS=
ENV GOPROXY=off
ENV GOTOOLCHAIN=local

USER root
WORKDIR /app
COPY . /tests
RUN go build -o /usr/local/bin/migration-go-process-scan /tests/verifier/detectors/go_process_scan.go
RUN if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then \
      if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux && rm -rf /var/lib/apt/lists/*; \
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache git ca-certificates python3 util-linux; \
      else exit 1; fi; fi \
    && command -v python3 >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
    && chmod +x /tests/test.sh /tests/grader.py \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts
# Reject an inconsistent evaluator bundle at image build time, before it can
# turn every candidate (including the reference solution) into an infra error.
RUN python3 -B -c 'import sys; from pathlib import Path; sys.path.insert(0, "/tests"); from verifier.integrity import validate_public_integrity; validate_public_integrity(Path("/tests"))'
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
    if [ -e /opt/migration/dependencies/gomodcache ]; then mkdir -p "$(dirname /workspace/repo/.gomodcache)" && rm -rf /workspace/repo/.gomodcache && cp -a /opt/migration/dependencies/gomodcache /workspace/repo/.gomodcache; if [ -d /workspace/repo/.git/info ]; then grep -qxF '.gomodcache' /workspace/repo/.git/info/exclude 2>/dev/null || printf '%s\n' '.gomodcache' >> /workspace/repo/.git/info/exclude; fi; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
