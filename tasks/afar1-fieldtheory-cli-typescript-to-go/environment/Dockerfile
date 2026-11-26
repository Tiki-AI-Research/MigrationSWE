# Stage that ships the exact Go toolchain required by instruction.md;
# the agent image copies it below so both sides compile with the same compiler.
FROM golang:1.23.0-bookworm AS gotoolchain
RUN test "$(GOTOOLCHAIN=local go env GOVERSION)" = go1.23.0

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:afar1-fieldtheory-cli-typescript-to-go-base-image-20261126@sha256:770ee326de06a046ac1dc78a313d0d88cc89e6d1bf379faf200e1eea4f7521da
WORKDIR /workspace/repo

# --- Go toolchain (identical to the verifier image: go1.23.0) ---
# The base source environment is a TypeScript project and ships no Go, so the
# target language toolchain required by `instruction.md` is installed here.  The
# runtime environment mirrors the verifier's offline contract exactly
# (`GOTOOLCHAIN=local`, `GOPROXY=off`, `GOFLAGS=-mod=mod -vet=off`, shared
# `GOMODCACHE`) so a candidate that builds here also builds in the verifier.
COPY --from=gotoolchain /usr/local/go /usr/local/go
ENV GOROOT=/usr/local/go
ENV PATH=/usr/local/go/bin:$PATH
ENV GOTOOLCHAIN=local
ENV GOSUMDB=off
ENV GOMODCACHE=/opt/gomodcache
ENV GOPROXY=off
ENV GOFLAGS="-mod=mod -vet=off"
RUN set -eux; mkdir -p "$GOMODCACHE"; chmod -R 0777 "$GOMODCACHE"

# --- Offline dependency prewarm (build-time network only) ---
# The exact module graph the evaluator resolves for the target is downloaded
# into the shared, world-readable module cache while the build has network; the
# throwaway manifest is removed afterwards.  At run time the agent is offline
# (`GOPROXY=off`) and `go build ./...` / `go test ./...` resolve everything from
# this cache.
COPY offline-deps/go.mod offline-deps/go.sum /tmp/offline-prewarm/
RUN set -eux; \
    cd /tmp/offline-prewarm; \
    GOFLAGS= GOPROXY='https://proxy.golang.org,direct' GOSUMDB=sum.golang.org go mod download all; \
    test -d "$GOMODCACHE/cache/download/modernc.org/sqlite/@v"; \
    chmod -R a+rwX "$GOMODCACHE"; \
    rm -rf /tmp/offline-prewarm

# --- Align the buildable surface with the verifier image ---
# The verifier builds the target against the offline, pure-Go dependency set
# above and deliberately provides no native SQLite development files.  Remove
# the development header and link artifacts from the agent image too, so an
# implementation that links the system SQLite library cannot build here yet
# fail in the verifier (which would be a false negative).
RUN set -eux; \
    rm -f /usr/include/sqlite3.h \
          /usr/lib/x86_64-linux-gnu/libsqlite3.a \
          /usr/lib/x86_64-linux-gnu/libsqlite3.so; \
    test ! -e /usr/include/sqlite3.h

# --- Fail the image build (not every trial) when the offline path the agent
# --- will use is broken; the probe runs as the unprivileged `agent` user.
RUN set -eux; \
    probe='d="$(mktemp -d)"; cd "$d"; go mod init probe >/dev/null 2>&1; printf "package main\n\nimport (\n\t_ \"github.com/spf13/cobra\"\n\t_ \"modernc.org/sqlite\"\n)\n\nfunc main() {}\n" > main.go; go get modernc.org/sqlite@v1.34.5 github.com/spf13/cobra@v1.8.1 >/dev/null; go build ./...; rm -rf "$d"'; \
    if command -v su >/dev/null 2>&1 && id agent >/dev/null 2>&1; then \
      su agent -s /bin/sh -c "set -e; $probe"; \
    else \
      sh -c "set -e; $probe"; \
    fi; \
    go version; \
    echo 'offline Go build verified for the agent user'

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
  && HOME=/home/agent uv python install 3.12 \
  && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi --with mcp \
  && chown -R agent:agent /home/agent

# Migration eval harness: keep the mcp extra when Harbor reinstalls mini-swe-agent.
RUN set -eux; \
    if ! command -v uv >/dev/null 2>&1; then \
      curl -LsSf https://astral.sh/uv/install.sh | sh; \
      install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; \
    fi; \
    if [ ! -x /usr/local/bin/uv.real ]; then \
      uv_path="$(command -v uv)"; \
      if [ "$uv_path" = "/usr/local/bin/uv" ] && grep -q 'Migration eval harness minimal uv wrapper' "$uv_path"; then \
        echo 'uv wrapper exists without uv.real' >&2; exit 1; \
      fi; \
      install -m 0755 "$uv_path" /usr/local/bin/uv.real; \
    fi; \
    printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      '# Migration eval harness minimal uv wrapper' \
      real_uv=/usr/local/bin/uv.real \
      saw_tool=0 \
      saw_install=0 \
      saw_agent=0 \
      saw_mcp=0 \
      previous= \
      'for argument in "$@"; do' \
      '    case "$argument" in' \
      '        tool) saw_tool=1 ;;' \
      '        install) if [ "$saw_tool" -eq 1 ]; then saw_install=1; fi ;;' \
      '        mini-swe-agent|mini-swe-agent==*|mini-swe-agent@*) saw_agent=1 ;;' \
      '        --with=mcp) saw_mcp=1 ;;' \
      '        mcp) if [ "$previous" = "--with" ]; then saw_mcp=1; fi ;;' \
      '    esac' \
      '    previous=$argument' \
      done \
      'if [ "$saw_tool" -eq 1 ] && [ "$saw_install" -eq 1 ] && [ "$saw_agent" -eq 1 ] && [ "$saw_mcp" -eq 0 ]; then' \
      '    exec "$real_uv" "$@" --with mcp' \
      fi \
      'exec "$real_uv" "$@"' \
      > /usr/local/bin/uv; \
    chmod 0755 /usr/local/bin/uv


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# Harbor installs and runs agents as the task's unprivileged `agent` user.
# The source image inherits a Firefox HOME that this user cannot write.
ENV HOME=/home/agent
ENV PATH=/home/agent/.local/bin:$PATH
RUN set -eux; \
    test "$(getent passwd agent | cut -d: -f6)" = /home/agent; \
    touch /home/agent/.bashrc; \
    chown agent:agent /home/agent /home/agent/.bashrc; \
    chmod u+rwx /home/agent; \
    chmod u+rw /home/agent/.bashrc; \
    su agent -s /bin/sh -c 'test -w "$HOME" && test -w "$HOME/.bashrc" && test -x "$HOME/.local/bin/mini-swe-agent"'
