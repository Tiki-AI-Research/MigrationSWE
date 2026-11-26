# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:afar1-fieldtheory-cli-commander-to-oclif-base-image-20261126@sha256:770ee326de06a046ac1dc78a313d0d88cc89e6d1bf379faf200e1eea4f7521da
WORKDIR /workspace/repo

# Grading runs the public suite on Node 22 (tests/Dockerfile), where Node's test
# runner expands `tests/**/*.test.ts` itself.  This image pins Node 20, where the
# repository's own `npm test` fails with `Could not find
# '/workspace/repo/tests/**/*.test.ts'` and runs zero tests, so the documented
# validation command (`instruction.md` §Validation) could never work here.  Install
# the verifier's exact Node and repoint the runtime symlink: login shells prepend
# /opt/envpipe/runtime/bin to PATH and non-login shells take it from the image
# PATH, so both must resolve to the one runtime that grading actually uses.
ARG NODE_VERSION=22.23.4
RUN set -eux; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    curl -fsSLo SHASUMS256.txt "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " node-v${NODE_VERSION}-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c -; \
    mkdir -p /opt/node22; \
    tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /opt/node22 --strip-components=1; \
    rm -f "node-v${NODE_VERSION}-linux-x64.tar.xz" SHASUMS256.txt; \
    rm -f /opt/envpipe/runtime; \
    ln -s /opt/node22 /opt/envpipe/runtime; \
    node --version; \
    npm --version

# Remove a base-image test preload that is not present in the verifier runtime.
# This keeps local validation behavior aligned with the grading environment.
ENV NODE_OPTIONS=""
RUN rm -f /usr/local/lib/fieldtheory-test-preload.mjs

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent uv python install 3.12 \
 && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi --with mcp \
 && chown -R agent:agent /home/agent

# The base image exports HOME=/tmp/FirefoxHome but ships no such directory, so
# agent setup (`echo ... >> "$HOME/.bashrc"`) and any $HOME write failed.  Keep
# the authored HOME value (the repository's own config test asserts the firefox
# user-data dir contains "Firefox") and point it at the agent home that already
# holds the tool above, so nothing has to be reinstalled offline.
# Keep the link owned by root: /tmp is world-writable with the sticky bit set and
# the kernel has fs.protected_symlinks=1, so a symlink in /tmp that is owned by
# anyone else cannot be followed by root (EACCES) and `$HOME` breaks for the
# root-side verifier; owning it as root lets both root and agent resolve it.
RUN mkdir -p /tmp/FirefoxHome \
 && [ -d /home/agent ] \
 && rmdir /tmp/FirefoxHome \
 && ln -s /home/agent /tmp/FirefoxHome \
 && [ "$(stat -c %U /tmp/FirefoxHome)" = root ] \
 && su agent -c 'test -w "$HOME"' 
ENV HOME=/tmp/FirefoxHome

# Offline dependency prewarm (contract-declared target deps; build-time network).
RUN set -eux; mkdir -p /opt/offline-deps
RUN cd /opt/offline-deps && npm install --no-save --no-audit --no-fund --cache /opt/offline-deps/npm @oclif/core@4.0.0
RUN node --version && npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# Both dependency prewarming and CLI installation write this cache as root.
# Hand it to the runtime user after the last root-side npm operation, then
# verify metadata, tarballs and cache writes without registry access.
RUN set -eux; \
    chown -R agent:agent /opt/offline-deps/npm; \
    chmod -R u+rwX /opt/offline-deps/npm; \
    su agent -s /bin/sh -c 'set -eu; \
        scratch=$(mktemp -d /tmp/offline-npm-check.XXXXXX); \
        trap "rm -rf \"$scratch\"" EXIT; \
        cd "$scratch"; \
        npm install --offline --full-metadata --ignore-scripts --no-audit --no-fund --save-exact @oclif/core@4.0.0; \
        npm ls --depth=0'
