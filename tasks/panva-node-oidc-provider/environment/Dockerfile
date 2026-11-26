# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:panva-node-oidc-provider-base-image-20261126@sha256:c7bf34f3f7e5969affc41cf8922278bf21cb67e9d4644dff2280dcb61c25b19d
WORKDIR /workspace/repo

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

# Warm runtime dependency metadata and tarballs while build-time egress is available.
# test-dist installs the packed provider into an empty project, so Fastify alone
# is insufficient: the source runtime dependencies (including debug/jose) are needed.
# Cache both the required exact version and the versions selected by patch/major
# ranges. Registry metadata can otherwise select a newer, uncached Fastify tarball.
# Resolve each range from a clean install tree while retaining the shared cache.
RUN set -eux; \
    mkdir -p /opt/offline-deps; \
    for fastify_spec in '5.12.0' '~5.12.0' '^5.12.0'; do \
      node -e 'const fs = require("node:fs"); const source = JSON.parse(fs.readFileSync("/workspace/repo/package.json", "utf8")); fs.writeFileSync("/opt/offline-deps/package.json", JSON.stringify({name: "harbor-offline-deps", version: "1.0.0", private: true, files: [], dependencies: {...source.dependencies, fastify: process.argv[1]}}));' "$fastify_spec"; \
      cd /opt/offline-deps; \
      rm -rf node_modules package-lock.json; \
      NPM_CONFIG_OFFLINE=false npm install --package-lock=false --ignore-scripts \
        --no-audit --no-fund --prefer-online --registry=https://registry.npmjs.org \
        --cache /opt/offline-deps/npm; \
      offline_probe="$(mktemp -d)"; \
      npm pack --offline --ignore-scripts --cache /opt/offline-deps/npm --pack-destination "$offline_probe"; \
      mkdir "$offline_probe/isolated"; \
      cd "$offline_probe/isolated"; \
      printf '%s\n' '{"name":"offline-cache-probe","private":true}' > package.json; \
      npm install --offline --install-strategy=nested --omit=dev --ignore-scripts \
        --no-audit --no-fund --cache /opt/offline-deps/npm \
        "$offline_probe/harbor-offline-deps-1.0.0.tgz"; \
      cd /opt/offline-deps; \
      rm -rf "$offline_probe"; \
    done; \
    node --version; \
    npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'

# Case-carried migration guidance: codex (the run agent) auto-loads AGENTS.md from the
# repository root as project instructions, so this delivers the acceptance criteria into the
# agent's context without any run-time instruction flag.  It is listed in the repo's
# .git/info/exclude so the agent's `git add -A` collect step does not fold this
# harness-provided file into the candidate patch.
COPY AGENTS.md /workspace/repo/AGENTS.md
RUN chmod 0644 /workspace/repo/AGENTS.md \
 && printf 'AGENTS.md\n' >> /workspace/repo/.git/info/exclude

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# Agent installation also writes to NPM_CONFIG_CACHE as root. Hand the warmed
# cache to the runtime user afterwards, so npm pack/install can create entries.
RUN chown -R agent:agent /opt/offline-deps/npm
