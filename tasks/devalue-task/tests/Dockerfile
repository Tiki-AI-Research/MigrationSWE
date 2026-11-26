# syntax=docker/dockerfile:1.7
FROM node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6

ARG REPO_URL="https://github.com/sveltejs/devalue"
ARG BASE_COMMIT="3ba57a6a8e60b171f624b6343be704b23a34af6e"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        patch \
        python3 \
        python3-yaml \
        ripgrep \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

ENV COREPACK_HOME=/opt/migration/corepack
# Corepack dispatches before pnpm reads its configuration. Ignore a candidate's
# packageManager pin (including the baseline's pnpm@8.5.1) and use the prepared
# pnpm@10.33.4, without downloading into the root-owned cache at runtime.
ENV COREPACK_ENABLE_PROJECT_SPEC=0
# devalue 4.3.3 pins "packageManager": "pnpm@8.5.1". pnpm 10 otherwise tries to
# download that version on every invocation, which fails without a registry in
# the scored run. The verifier image must use its pinned pnpm 10.33.4 instead.
ENV npm_config_manage_package_manager_versions=false
# Never create bytecode under /tests: the candidate identity can traverse the
# one allowed verifier directory for test.patch, and .pyc files would leak the
# hidden verifier implementation.
ENV PYTHONDONTWRITEBYTECODE=1
RUN corepack enable \
    && corepack prepare pnpm@10.33.4 --activate \
    && chmod -R a+rX /opt/migration/corepack

RUN mkdir -p /opt/migration/dependencies/build

# The dependency set and its resolved graph are delivered together.  The
# build never re-resolves caret ranges.
COPY environment/dependencies/package.json environment/dependencies/pnpm-lock.yaml /opt/migration/dependencies/build/
RUN printf 'onlyBuiltDependencies:\n  - esbuild\n' > /opt/migration/dependencies/build/pnpm-workspace.yaml \
    && cd /opt/migration/dependencies/build \
    && pnpm install --frozen-lockfile \
         --store-dir /opt/migration/pnpm-store \
         --package-import-method=copy \
    && mv node_modules /opt/migration/dependencies/node_modules \
    && rm -rf /opt/migration/dependencies/build \
    && chmod -R a+rX /opt/migration/pnpm-store \
    && mkdir -p /opt/migration/pnpm-store/v10/projects \
    && chmod 0777 /opt/migration/pnpm-store/v10/projects

RUN git init /app \
    && git -C /app remote add origin "${REPO_URL}" \
    && for attempt in 1 2 3 4 5; do \
         git -C /app -c http.version=HTTP/1.1 \
           -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=120 \
           fetch --depth=1 origin "${BASE_COMMIT}" && break; \
         test "${attempt}" -lt 5 || exit 1; \
         sleep 5; \
       done \
    && git -C /app checkout --detach FETCH_HEAD \
    && test "$(git -C /app rev-parse HEAD)" = "${BASE_COMMIT}" \
    && git -C /app remote remove origin \
    && git -C /app config core.hooksPath /dev/null \
    && git -C /app reflog expire --expire=now --all \
    && git -C /app gc --prune=now

RUN cp -a /opt/migration/dependencies/node_modules /app/node_modules \
    && git config --system --add safe.directory /app

RUN groupadd --gid 65532 candidate \
    && useradd --uid 65532 --gid 65532 --no-create-home \
         --home-dir /tmp/candidate-home --shell /usr/sbin/nologin candidate

USER root
WORKDIR /app
COPY . /tests
# The candidate identity may inspect exactly one evaluator-owned artefact:
# /tests/verifier/test.patch.  Everything else under /tests stays root-only.
# Merely changing /tests to 0711 would still leave the copied files at 0755,
# so explicitly remove group/other read bits before re-opening that one path.
RUN command -v python3 >/dev/null 2>&1 \
    && command -v git >/dev/null 2>&1 \
    && command -v setpriv >/dev/null 2>&1 \
    && chmod +x /tests/test.sh /tests/grader.py \
    && chown -R root:root /tests \
    && find /tests -type d -exec chmod 0700 {} + \
    && find /tests -type f -exec chmod 0600 {} + \
    && chmod 0711 /tests /tests/verifier \
    && chmod 0700 /tests/test.sh /tests/grader.py \
    && chmod 0644 /tests/verifier/test.patch \
    && python3 /tests/verifier/acceptance_checks.py \
    && python3 /tests/verifier/adversarial_checks.py \
    && python3 /tests/verifier/security_regression.py \
    && mkdir -p /logs/verifier /logs/artifacts /logs/candidate \
    && chmod 0755 /logs \
    && chmod 0700 /logs/verifier \
    && chmod 0777 /logs/artifacts /logs/candidate
ENTRYPOINT []
CMD ["/tests/test.sh"]
