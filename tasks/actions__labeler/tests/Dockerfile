# syntax=docker/dockerfile:1
FROM golang@sha256:b970e6d47c09fdd34179acef5c4fecaf6410f0b597a759733b3cbea04b4e604a
RUN mkdir -p /src
RUN cat > /src/go.mod <<'EOF'
module github.com/actions/labeler

go 1.24.0

require (
	github.com/dlclark/regexp2 v1.12.0
	gopkg.in/yaml.v3 v3.0.1
)
EOF
RUN cat > /src/go.sum <<'EOF'
github.com/dlclark/regexp2 v1.11.5 h1:Q/sSnsKerHeCkc/jSTNq1oCm7KiVgUMZRDUoRu0JQZQ=
github.com/dlclark/regexp2 v1.11.5/go.mod h1:DHkYz0B9wPfa6wondMfaivmHpzrQ3v9q8cnmRbL6yW8=
github.com/dlclark/regexp2 v1.12.0 h1:0j4c5qQmnC6XOWNjP3PIXURXN2gWx76rd3KvgdPkCz8=
github.com/dlclark/regexp2 v1.12.0/go.mod h1:DHkYz0B9wPfa6wondMfaivmHpzrQ3v9q8cnmRbL6yW8=
gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405 h1:yhCVgyC4o1eVCa2tZl7eS0r+SDo693bJlVdllGtEeKM=
gopkg.in/check.v1 v0.0.0-20161208181325-20d25e280405/go.mod h1:Co6ibVJAznAaIkqp8huTwlJQCZ016jof/cbN4VW5Yz0=
gopkg.in/yaml.v3 v3.0.1 h1:fxVm/GzAzEWqLHuvctI91KS9hhNmmWOoWu0XTYJS7CA=
gopkg.in/yaml.v3 v3.0.1/go.mod h1:K4uyk7z7BCEPqu6E+C64Yfv1cQ7kz7rIZviUmN+EgEM=
EOF
RUN cd /src \
 && GOMODCACHE=/opt/migration/dependencies/gomodcache go mod download all \
 && GOMODCACHE=/opt/migration/dependencies/gomodcache go mod download github.com/dlclark/regexp2@v1.11.5 \
 && chmod -R a+rX /opt/migration/dependencies
RUN GOBIN=/usr/local/bin GOMODCACHE=/opt/migration/dependencies/gomodcache go install gotest.tools/gotestsum@v1.12.0
RUN cat > /usr/local/bin/migration-harness <<'EOF'
#!/bin/sh
set -eu
assets=${1:-}
report=${2:?usage: migration-harness ASSETS_DIR REPORT_PATH}
cd /workspace/repo
if [ -n "$assets" ] && [ -d "$assets" ]; then
  cp -a "$assets"/. /workspace/repo/
fi
exec gotestsum --junitfile="$report" -- ./...
EOF
RUN chmod 0755 /usr/local/bin/migration-harness
ENV GOMODCACHE=/workspace/repo/.gomodcache GOCACHE=/workspace/repo/.gocache GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local CGO_ENABLED=0
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
RUN if ! python3 -c 'import yaml' 2>/dev/null; then \
      if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends python3-yaml && rm -rf /var/lib/apt/lists/*; \
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache py3-yaml; \
      else exit 1; fi; fi
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
