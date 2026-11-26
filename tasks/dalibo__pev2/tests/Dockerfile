FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM rust@sha256:a45bf1f5d9af0a23b26703b3500d70af1abff7f984a7abef5a104b42c02a292b

# Harness/runtime helpers: the grading harness is executed with python3; git and
# ca-certificates keep the image usable without root-only state at replay time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/*

# Prefetch the exact Cargo.lock dependency set into a world-readable CARGO_HOME so
# the offline, unprivileged (uid 1000, HOME=/tmp) replay needs no registry access.
WORKDIR /opt/migration/dependencies/prefetch
COPY --from=migrationrepositorycontext Cargo.toml Cargo.lock ./
COPY --from=migrationrepositorycontext crates ./crates
RUN CARGO_HOME=/opt/migration/dependencies/cargo CARGO_NET_OFFLINE=false cargo fetch --locked \
    && chmod -R a+rX /opt/migration/dependencies

ENV CARGO_NET_OFFLINE=true
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
    if [ -e /opt/migration/dependencies/cargo ]; then mkdir -p "$(dirname /workspace/repo/cargo-home)" && rm -rf /workspace/repo/cargo-home && cp -a /opt/migration/dependencies/cargo /workspace/repo/cargo-home; if [ -d /workspace/repo/.git/info ]; then grep -qxF 'cargo-home' /workspace/repo/.git/info/exclude 2>/dev/null || printf '%s\n' 'cargo-home' >> /workspace/repo/.git/info/exclude; fi; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
