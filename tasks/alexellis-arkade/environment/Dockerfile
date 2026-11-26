# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:alexellis-arkade-base-image-20261126@sha256:89e8964b8bcac50bd2334c08e36f5aef55eb4429845e22fb1ae5839a819f2135
WORKDIR /workspace/repo

# The module cache is shared by root and by the unprivileged `agent` user.
# Go defaults to `$HOME/go/pkg/mod`, so a prewarm performed as root lands in a
# directory the agent cannot read and every offline build then tries to
# download.  One explicit, world-readable location avoids that class of failure
# for both the source module graph and the target framework.
ENV GOMODCACHE=/opt/gomodcache

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent uv python install 3.12 \
 && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi \
 && chown -R agent:agent /home/agent

# Offline dependency prewarm (contract-declared target deps; build-time network).
# The repository at the base commit is the Cobra source, so downloading its own
# module graph does not bring the *target* framework in.  The task requires
# migrating onto Kong 1.8.1, so that module is fetched here as well, while the
# build still has network; the runtime stays offline (`GOPROXY=off`) and
# `go mod vendor`/`go build` resolve everything from the shared cache.
RUN set -eux; mkdir -p /opt/offline-deps "$GOMODCACHE"; chmod 0777 "$GOMODCACHE"
RUN (go mod download) || echo 'offline prewarm skipped: go' >&2
# Warm exactly the module graph the agent will need: a throwaway copy of the
# repository with the target framework required (so `go mod tidy` keeps it), plus
# the test dependencies of dependencies that tidy resolves.  Working on a copy
# keeps `/workspace/repo` byte-identical to the base commit.
RUN set -eux; \
    rm -rf /tmp/prewarm-repo; cp -a /workspace/repo /tmp/prewarm-repo; \
    cd /tmp/prewarm-repo; \
    mkdir -p zz_prewarm; \
    printf 'package zz_prewarm\n\nimport (\n\t_ "github.com/alecthomas/kong"\n)\n' > zz_prewarm/prewarm.go; \
    GOFLAGS= GOPROXY='https://proxy.golang.org,direct' GOSUMDB=sum.golang.org \
      go mod edit -require='github.com/alecthomas/kong@v1.8.1'; \
    GOFLAGS= GOPROXY='https://proxy.golang.org,direct' GOSUMDB=sum.golang.org \
      go mod tidy; \
    GOFLAGS= GOPROXY='https://proxy.golang.org,direct' GOSUMDB=sum.golang.org \
      go mod download all; \
    grep -q 'github.com/alecthomas/kong v1.8.1' go.mod; \
    cd /; rm -rf /tmp/prewarm-repo; \
    test -f "$GOMODCACHE/cache/download/github.com/alecthomas/kong/@v/v1.8.1.zip"; \
    chmod -R a+rwX "$GOMODCACHE"; \
    test -z "$(git -C /workspace/repo status --porcelain)"

# Fail the build (not every trial) when the offline path the *agent* will use is
# broken: the agent runs as an unprivileged user, so the checks below run as
# `agent` against the shared cache.  Without them a broken prewarm silently
# turns into "every candidate invents a local look-alike framework".
RUN set -eux; \
    su agent -s /bin/sh -c ' \
      set -e; \
      probe="$(mktemp -d)"; cd "$probe"; \
      go mod init probe >/dev/null 2>&1; \
      GOPROXY=off GOFLAGS=-mod=mod go get github.com/alecthomas/kong@v1.8.1 >/dev/null; \
      GOPROXY=off GOFLAGS=-mod=mod go list -deps github.com/alecthomas/kong >/dev/null; \
      cd /workspace/repo; \
      GOPROXY=off GOFLAGS=-mod=mod go list -deps ./... >/dev/null; \
      rm -rf "$probe"'; \
    echo 'offline prewarm verified for the agent user: source graph + kong v1.8.1'
# `go list -deps` above runs with `-mod=mod`, which is allowed to rewrite
# `go.mod` (it drops stale `// indirect` markers).  The probe therefore leaves
# the repository dirty, and an oracle run then refuses to start because
# `solution/solve.sh` requires a clean baseline worktree.  Restore the tree and
# assert the invariant here, so a dirty image fails the build instead of every
# oracle trial.
# Root's checkout recreates changed files (including go.mod) as root-owned.
# Restore the agent's ownership after checkout so offline dependency updates
# can write those files without replacing them or attempting privilege changes.
RUN set -eux; \
    git -C /workspace/repo checkout -- .; \
    git -C /workspace/repo clean -fd; \
    chown -R agent:agent /workspace/repo; \
    su agent -s /bin/sh -c ' \
      set -e; cd /workspace/repo; \
      test -w .; test -w .git; \
      test -w go.mod; test -w go.sum; \
      git config --local user.name "Harbor Agent"; \
      git config --local user.email "harbor-agent@localhost.invalid"; \
      test "$(git config --local user.name)" = "Harbor Agent"; \
      test "$(git config --local user.email)" = "harbor-agent@localhost.invalid"; \
      test -z "$(git status --porcelain)"; \
      test "$(git rev-parse HEAD)" = "0a0a800fd7554d4eddb1856f9ef8a21214e95bab"'; \
    echo 'workspace verified clean and writable by the agent at the base commit'
RUN go version || echo 'offline verify skipped: go' >&2
ENV GOFLAGS='-mod=mod'
ENV GOPROXY='off'

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh
