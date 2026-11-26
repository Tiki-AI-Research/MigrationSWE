FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM python@sha256:0de818129b26ed8f46fd772f540c80e277b67a28229531a1ba0fdacfaed19bcb

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv --copies /opt/migration/dependencies/venv \
 && /opt/migration/dependencies/venv/bin/python -m pip install --no-cache-dir "mcp==1.30.0" \
 && /opt/migration/dependencies/venv/bin/python -m pip download --only-binary=:all: --dest /opt/migration/wheels "mcp==1.30.0" \
 && /opt/migration/dependencies/venv/bin/python -m pip download --only-binary=:all: --dest /opt/migration/wheels "mcp==2.3.0" "mcp-types==2.3.0" \
 && chmod -R a+rX /opt/migration/dependencies /opt/migration/wheels

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
    if [ -e /opt/migration/dependencies/venv ]; then rm -rf /workspace/repo/mcp-server/.venv && cp -a /opt/migration/dependencies/venv /workspace/repo/mcp-server/.venv; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
