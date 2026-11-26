# syntax=docker/dockerfile:1.7
# Candidate runtime for the linked-shiv Click 6.7 -> 8.1.7 migration.
#
# The base is the original generated environment
# (ci-tox-python311-linux, Python 3.11.16, repo checked out at BASE_COMMIT).
# It is pinned by digest. The layers below fix the candidate toolchain so it
# matches the task statement and works without PyPI access:
#   * source Click 6.7 is installed (the project's current dependency);
#   * Click 8.1.7 and the PEP 517 build wheels are available from a local
#     wheelhouse for the migration target;
#   * pytest/mypy/flake8 are present so the candidate can run the project's
#     own test suite offline.
#
# Original generator provenance (before it was replaced by this pinned base):
#   Generator: dockerfile-generator.v1
#   Candidate: ci-tox-python311-linux
#   FROM docker.io/blacktt94/harbor-bundle:codex-0.153.4@sha256:20cd21055a368cc29af8a214e335e7df939611eda135a54c3e7a40197246841d
FROM --platform=linux/amd64 docker.io/tikiaiteam/migration:linked-shiv-base-image-20261126@sha256:e1e2f4480d68315218f07b46acb896c5a10ba7c97f0d0b47ea97a4f1c0a50f65

ARG REPO_URL="https://github.com/linkedin/shiv"
ARG BASE_COMMIT="ff542cbe75ea832df3a989d07c7fdf5214f727fa"

LABEL org.opencontainers.image.source="$REPO_URL" \
      org.opencontainers.image.revision="$BASE_COMMIT" \
      io.envpipe.image.generator="dockerfile-generator.v1+candidate-fix" \
      org.opencontainers.image.title="Repository runtime environment" \
      io.envpipe.project.runtime.family="python" \
      io.envpipe.project.runtime.version="3.11.16" \
      io.envpipe.candidate.click.source="6.7" \
      io.envpipe.candidate.click.target="8.1.7"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /workspace/repo

# Test tooling for the candidate. pytest is deliberately installed before the
# Click downgrade so no dependency can silently move Click away from 6.7.
RUN python -m pip install --no-cache-dir pytest mypy flake8

# Offline wheelhouse: source Click 6.7, target Click 8.1.7, and the wheels
# needed by an offline PEP 517 editable install of this project.
RUN mkdir -p /opt/migration/wheels \
    && python -m pip download --no-deps --only-binary=:all: --dest /opt/migration/wheels "click==6.7" \
    && python -m pip download --no-deps --only-binary=:all: --dest /opt/migration/wheels "click==8.1.7" \
    && python -m pip download --no-deps --only-binary=:all: --dest /opt/migration/wheels setuptools wheel packaging pip \
    && printf '[global]\nno-index = true\nfind-links = /opt/migration/wheels\n' > /etc/pip.conf

# The project currently depends on Click 6.7. Reinstall it last so the image
# starts from the source behaviour, not from a newer Click.
RUN python -m pip install --no-cache-dir --no-deps --force-reinstall "click==6.7" \
    && python -c 'import click; assert click.__version__ == "6.7", click.__version__'

# Preinstall the complete agent toolchain while the image is being built. The
# agent container is allowlisted to the model endpoint at runtime and must not
# attempt to reach PyPI, GitHub, or the uv installer after startup.
#
# IMPORTANT: the install spec below MUST match Harbor's runtime bootstrap
# exactly -- `uv tool install --python 3.12 mini-swe-agent --with litellm
# --with orjson --with fastapi` -- so that the runtime install is a no-op.
# Adding version pins or extra `--with` entries here changes the recorded
# requirements, which makes `uv tool install` re-resolve at runtime, reach
# PyPI, and fail in the offline sandbox.
#
# LiteLLM is pinned with UV_CONSTRAINT instead of a version in `--with` so the
# recorded requirement stays identical to Harbor's while the resolver still
# uses the known-good release. LiteLLM 1.85.0+ imports the optional `mcp`
# package from its tool-call path without depending on it, which makes every
# mini-swe-agent request fail with `ModuleNotFoundError: No module named
# 'mcp'`. 1.80.0 predates that import and still satisfies mini-swe-agent's
# `litellm>=1.75.5` requirement.
RUN runuser -u agent -- env HOME=/home/agent bash -lc '\
        set -eux; \
        export PATH="$HOME/.local/bin:$PATH"; \
        printf "litellm==1.80.0\n" > "$HOME/.uv-constraints.txt"; \
        export UV_CONSTRAINT="$HOME/.uv-constraints.txt"; \
        curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh; \
        uv python install 3.12; \
        uv tool install --python 3.12 mini-swe-agent \
            --with litellm --with orjson --with fastapi; \
        mini-swe-agent --help >/dev/null'

# The pinned LiteLLM content-part schema calls reasoning text `reasoning`,
# while the streaming endpoint supplies `text`. Accept both field names in
# the agent's SDK and validate the failing event offline during the build.
# Keep Harbor's recorded uv requirements unchanged so runtime setup is a no-op.
COPY patch_litellm_streaming.py /tmp/patch_litellm_streaming.py
RUN runuser -u agent -- env HOME=/home/agent \
        LITELLM_LOCAL_MODEL_COST_MAP=true PYTHONDONTWRITEBYTECODE=1 \
        /home/agent/.local/share/uv/tools/mini-swe-agent/bin/python \
        /tmp/patch_litellm_streaming.py \
    && rm -f /tmp/patch_litellm_streaming.py

# Expose the agent toolchain on the default PATH so the bootstrap probe
# (`command -v uv`) succeeds before it would attempt a network install.
RUN ln -sf /home/agent/.local/bin/uv /usr/local/bin/uv \
    && ln -sf /home/agent/.local/bin/uvx /usr/local/bin/uvx \
    && ln -sf /home/agent/.local/bin/mini-swe-agent /usr/local/bin/mini-swe-agent \
    && command -v uv \
    && command -v mini-swe-agent \
    && runuser -u agent -- env HOME=/home/agent PATH=/home/agent/.local/bin:$PATH \
       mini-swe-agent --help >/dev/null

# Do not let package managers or LiteLLM's remote cost-map refresh create
# runtime egress outside Harbor's AI-provider allowlist.
ENV UV_OFFLINE=1 \
    UV_CONSTRAINT=/home/agent/.uv-constraints.txt \
    PIP_NO_INDEX=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=true

# The candidate runs as the unprivileged `agent` user. Give that user write
# access to the interpreter's site-packages so `pip install click==8.1.7` and
# `pip install -e .` work without `--user` and without leaving a half-removed
# `~lick` distribution behind.
RUN chown -R agent:agent /opt/envpipe/runtime/lib/python3.11/site-packages \
    /opt/envpipe/runtime/bin

RUN cd /workspace/repo \
    && test "$(git rev-parse HEAD)" = "$BASE_COMMIT" \
    && test -z "$(git status --porcelain)" \
    && chown -R agent:agent /workspace/repo

ENV \
    ENVPIPE_BASE_COMMIT="$BASE_COMMIT" \
    ENVPIPE_REPO_PATH="/workspace/repo" \
    SWEBENCH_REPO_PATH="/workspace/repo"

WORKDIR /workspace/repo

# Preinstall Codex and Claude Code using marwes-combine's pinned Node 22 setup.
# Keep the project runtime and candidate-only offline settings intact.
USER root
COPY install_agents.sh /tmp/install_agents.sh
RUN candidate_node_before="$(node --version 2>/dev/null || true)" \
 && bash /tmp/install_agents.sh \
 && test "$(node --version 2>/dev/null || true)" = "$candidate_node_before" \
 && rm -f /tmp/install_agents.sh

CMD ["/bin/bash"]
