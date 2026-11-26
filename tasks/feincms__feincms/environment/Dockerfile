# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:feincms__feincms-base-image-20261126@sha256:fff20f7b61b2f19dd725e8dca56914fc66b8c4f8c3f8652650c8964a855a0106
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

# Tox must be able to discover real interpreters for the retained support matrix.
# Provision them before resolving wheels so dependency markers use the actual
# interpreter rather than the build Python (pip --python-version does not do so).
RUN set -eux; \
    for python_version in 3.10 3.11 3.12 3.13; do \
      if ! command -v "python$python_version" >/dev/null 2>&1; then \
        UV_PYTHON_INSTALL_DIR=/opt/offline-deps/python uv python install "$python_version"; \
        python_executable="$(UV_PYTHON_INSTALL_DIR=/opt/offline-deps/python uv python find --managed-python "$python_version")"; \
        ln -s "$python_executable" "/usr/local/bin/python$python_version"; \
      fi; \
    done; \
    if [ -d /opt/offline-deps/python ]; then chmod -R a+rX /opt/offline-deps/python; fi

# Runtime egress is restricted to the API gateway. Resolve the complete runtime,
# tox build backend and public test dependencies under each supported Python.
# This includes conditional dependencies such as typing_extensions on Python 3.10.
COPY offline-requirements.txt /opt/offline-deps/requirements.txt
RUN set -eux; \
    mkdir -p /opt/offline-deps/wheels; \
    rm -f /opt/offline-deps/wheels/playwright-*.whl; \
    for python_version in 3.10 3.11 3.12 3.13; do \
      dependency_venv="/tmp/offline-deps-$python_version"; \
      uv venv --seed --python "python$python_version" "$dependency_venv"; \
      "$dependency_venv/bin/python" -m pip download --only-binary=:all: \
        --dest /opt/offline-deps/wheels -r /opt/offline-deps/requirements.txt; \
      "$dependency_venv/bin/python" -m pip install --dry-run --ignore-installed \
        --no-index --find-links=/opt/offline-deps/wheels \
        -r /opt/offline-deps/requirements.txt; \
      rm -rf "$dependency_venv"; \
    done; \
    chmod a+rx /opt/offline-deps; \
    chmod a+r /opt/offline-deps/requirements.txt; \
    chmod -R a+rX /opt/offline-deps/wheels

# Upgrade inherited packages to the cached versions, particularly Playwright,
# so its preinstalled browser revision matches fresh tox environments.
RUN python3 -m pip install --upgrade --no-index --find-links=/opt/offline-deps/wheels \
      -r /opt/offline-deps/requirements.txt

ENV PIP_NO_INDEX='1'
ENV PIP_FIND_LINKS='/opt/offline-deps/wheels'
ENV PLAYWRIGHT_BROWSERS_PATH='/home/agent/.cache/ms-playwright'

# A direct GitHub URL still needs build-time egress even with PIP_NO_INDEX.
# Warm tox as the runtime user with its PATH and final pip settings: tox records
# both the interpreter path and pip environment, and a mismatch reinstalls the
# URL dependency after runtime egress has been restricted. Keep the real upstream
# main branch, both interpreters and all test commands; generate only ignored
# tox state, without installing the project.
RUN if [ -d /workspace/repo/.tox ]; then chown -R agent:agent /workspace/repo/.tox; fi \
    && runuser -u agent -- sh -c 'export PATH="$HOME/.local/bin:$PATH"; exec tox -e py312-djmain,py313-djmain --notest --skip-pkg-install'
# Fail the image build if an unchanged runtime invocation still needs egress.
RUN --network=none runuser -u agent -- sh -c 'export PATH="$HOME/.local/bin:$PATH"; exec tox -e py312-djmain,py313-djmain --notest --skip-pkg-install'

# The e2e tox environment runs `playwright install chromium`; preinstall the
# matching browser and OS libraries while build-time network is available. Use
# the agent's default cache too, since tox may filter PLAYWRIGHT_BROWSERS_PATH.
RUN python3 -m playwright install --with-deps chromium \
    && chmod -R a+rX /home/agent/.cache/ms-playwright \
    && chown -R agent:agent /home/agent/.cache/ms-playwright


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh
