FROM golang@sha256:81dc45d05a7444ead8c92a389621fafabc8e40f8fd1a19d7e5df14e61e98bc1a

ENV GOTOOLCHAIN=local
ENV GOBIN=/usr/local/bin

RUN GOBIN=/usr/local/bin GOMODCACHE=/tmp/tool-mod GOCACHE=/tmp/tool-build GOTOOLCHAIN=go1.22.12 go install gotest.tools/gotestsum@v1.12.0 && chmod 0755 /usr/local/bin/gotestsum && rm -rf /tmp/tool-mod /tmp/tool-build

WORKDIR /opt/build

COPY <<MIGRATION_GO_MOD go.mod
module github.com/thomas-mauran/chess-tui

go 1.25.0

require (
	github.com/BurntSushi/toml v1.6.0
	github.com/notnil/chess v1.10.0
)
MIGRATION_GO_MOD

COPY <<MIGRATION_GO_SUM go.sum
github.com/BurntSushi/toml v1.6.0 h1:dRaEfpa2VI55EwlIW72hMRHdWouJeRF7TPYhI+AUQjk=
github.com/BurntSushi/toml v1.6.0/go.mod h1:ukJfTF/6rtPPRCnwkur4qwRxa8vTRFBF0uk2lLoLwho=
github.com/ajstarks/svgo v0.0.0-20200320125537-f189e35d30ca/go.mod h1:K08gAheRH3/J6wwsYMMT4xOr94bZjxIelGM0+d/wbFw=
github.com/notnil/chess v1.10.0 h1:RR3MgS9G6zZmJ+VPTJolyxdaIgxoUPyUUY+2iaw35G0=
github.com/notnil/chess v1.10.0/go.mod h1:cRuJUIBFq9Xki05TWHJxHYkC+fFpq45IWwk94DdlCrA=
MIGRATION_GO_SUM

# Keep the declared dependency versions and also cache the compatible chess
# version selected by candidates; both must resolve with GOPROXY=off at runtime.
RUN GOMODCACHE=/opt/migration/dependencies/gomodcache GOCACHE=/tmp/build-cache GOPATH=/tmp/gopath go mod download all \
    && GOMODCACHE=/opt/migration/dependencies/gomodcache GOCACHE=/tmp/build-cache GOPATH=/tmp/gopath go mod download \
      github.com/notnil/chess@v1.9.0 github.com/notnil/chess@v1.3.0 \
      github.com/BurntSushi/toml@v1.4.0 \
      github.com/pelletier/go-toml/v2@v2.2.3 github.com/pelletier/go-toml/v2@v2.2.4 \
    && chmod -R a+rX /opt/migration/dependencies && rm -rf /tmp/build-cache

ENV GOFLAGS=""
ENV GOPROXY=off
ENV GOSUMDB=off
ENV GOMODCACHE=/workspace/repo/.gomodcache
ENV GOCACHE=/workspace/repo/.gocache
ENV GOPATH=/tmp/gopath

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
    chmod 0755 /tests/assets/target/runner /tests/assets/target/run.sh; \
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
    if [ -e /opt/migration/dependencies/gomodcache ]; then rm -rf /workspace/repo/.gomodcache && cp -a /opt/migration/dependencies/gomodcache /workspace/repo/.gomodcache; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
