FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM node@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

ENV CI=true

WORKDIR /opt/migration/build

COPY --from=migrationrepositorycontext package.json package-lock.json .npmrc .yarnrc.yml tsconfig.json babel.config.js jest.config.cjs ./
COPY --from=migrationrepositorycontext src ./src

RUN npm install --save-dev --save-exact --legacy-peer-deps react-native@0.85.0
RUN npm install --no-save --legacy-peer-deps --ignore-scripts jest-junit@16.0.0
RUN node -e 'const assert = require("node:assert/strict"); for (const name of ["react-native", "@react-native/codegen"]) assert.equal(require(name + "/package.json").version, "0.85.0", name)'

RUN mkdir -p /opt/migration/dependencies \
    && cp -a ./node_modules /opt/migration/dependencies/node_modules \
    && chmod -R a+rX /opt/migration/dependencies

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
    if [ -e /opt/migration/dependencies/node_modules ]; then mkdir -p "$(dirname /workspace/repo/node_modules)" && rm -rf /workspace/repo/node_modules && cp -a /opt/migration/dependencies/node_modules /workspace/repo/node_modules; if [ -d /workspace/repo/.git/info ]; then grep -qxF 'node_modules' /workspace/repo/.git/info/exclude 2>/dev/null || printf '%s\n' 'node_modules' >> /workspace/repo/.git/info/exclude; fi; fi; \
    true
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && python3 -m pip install --no-cache-dir --target /opt/migration/native-parser \
         tree-sitter==0.25.2 tree-sitter-java==0.23.5 tree-sitter-c-sharp==0.23.1 \
    && chmod -R a+rX /opt/migration/native-parser \
    && rm -rf /var/lib/apt/lists/*
RUN PYTHONDONTWRITEBYTECODE=1 python3 /tests/assets/shared/test-native-validation.py \
    && node /tests/assets/shared/test-bridge-contract.cjs
ENTRYPOINT []
CMD ["/tests/test.sh"]
