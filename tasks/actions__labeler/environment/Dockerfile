# Prepare the same Go toolchain and offline modules as the separate verifier.
FROM golang@sha256:b970e6d47c09fdd34179acef5c4fecaf6410f0b597a759733b3cbea04b4e604a AS migration-go
WORKDIR /opt/migration/toolchain
COPY go.mod go.sum ./
RUN test "$(go env GOVERSION)" = go1.24.0 \
 && GOMODCACHE=/opt/migration/dependencies/gomodcache go mod download all \
 && GOMODCACHE=/opt/migration/dependencies/gomodcache go mod download github.com/dlclark/regexp2@v1.11.5

# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:actions__labeler-base-image-20261126@sha256:b15add0aacf0e5b5711b140ba172694efae57b68793920aa1594cb3872bca279
USER root
WORKDIR /workspace/repo

COPY --from=migration-go /usr/local/go /usr/local/go
COPY --from=migration-go --chown=agent:agent /opt/migration/dependencies /opt/migration/dependencies
COPY check-go-environment.sh /usr/local/bin/check-go-environment
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOROOT=/usr/local/go \
    GOPATH=/home/agent/go \
    GOMODCACHE=/opt/migration/dependencies/gomodcache \
    GOCACHE=/home/agent/.cache/go-build \
    GOFLAGS=-mod=mod \
    GOPROXY=off \
    GOSUMDB=off \
    GOTOOLCHAIN=local \
    CGO_ENABLED=0
RUN ln -sf /usr/local/go/bin/go /usr/local/bin/go \
 && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
 && mkdir -p /home/agent/go /home/agent/.cache/go-build \
 && chown -R agent:agent /home/agent/go /home/agent/.cache \
 && chmod 0755 /usr/local/bin/check-go-environment

# Compile and execute real dependency checks offline as the rollout user.
USER agent
RUN /usr/local/bin/check-go-environment
USER root

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

# Preinstall Codex and Claude Code using marwes-combine's pinned Node 22 setup.
# Keep the project runtime and candidate-only offline settings intact.
USER root
COPY install_agents.sh /tmp/install_agents.sh
RUN candidate_node_before="$(node --version 2>/dev/null || true)" \
 && bash /tmp/install_agents.sh \
 && test "$(node --version 2>/dev/null || true)" = "$candidate_node_before" \
 && rm -f /tmp/install_agents.sh

