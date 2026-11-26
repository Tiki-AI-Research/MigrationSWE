FROM golang@sha256:32096e84705b30bb39cc9c65ef2896efacc4268203b7876049847763cefc934d

RUN GOBIN=/usr/local/bin go install gotest.tools/gotestsum@v1.12.0

WORKDIR /workspace/repo

USER root
WORKDIR /app
COPY . /tests
RUN if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then \
      if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux && rm -rf /var/lib/apt/lists/*; \
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache git ca-certificates python3 util-linux; \
      else exit 1; fi; fi \
    && command -v python3 >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
    && chmod +x /tests/test.sh /tests/grader.py /tests/assets/target/runner \
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
RUN cd /tests && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest verifier.test_http_mechanisms verifier.test_go_skips verifier.test_url_paths verifier.test_semantic_scoring
ENTRYPOINT []
CMD ["/tests/test.sh"]
