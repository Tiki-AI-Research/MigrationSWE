FROM node@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates python3 make g++ \
 && rm -rf /var/lib/apt/lists/*

ENV npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false

RUN mkdir -p /opt/migration/dependencies/root
WORKDIR /opt/migration/dependencies/root

RUN npm init -y \
 && npm install --legacy-peer-deps --no-audit --no-fund \
    @babel/cli@^7.28.6 @babel/core@^7.29.0 @babel/polyfill@^7.12.1 @babel/preset-env@^7.29.2 @babel/register@^7.28.6 \
    @rollup/plugin-babel@^6.1.0 @rollup/plugin-commonjs@^24.1.0 @rollup/plugin-json@^6.1.0 @rollup/plugin-node-resolve@^15.3.1 \
    @rollup/plugin-replace@^5.0.7 @rollup/plugin-virtual@^3.0.2 @types/chai@^4.3.20 @types/mocha@^10.0.10 \
    babel-loader@^9.2.1 babel-plugin-add-module-exports@^1.0.4 babel-plugin-istanbul@^6.1.1 chai@^4.5.0 \
    cldr-data@^36.0.5 cldrjs@^0.5.5 coveralls@^3.1.1 ejs@^3.1.10 esdoc@^1.1.0 \
    esdoc-inject-script-plugin@^1.0.0 esdoc-standard-plugin@^1.0.0 eslint@^8.30.0 eslint-config-js-joda@^1.1.0 \
    json-loader@^0.5.7 karma@^6.4.4 karma-chai-plugins@^0.9.0 karma-chrome-launcher@^3.2.0 \
    karma-firefox-launcher@^2.1.3 karma-mocha@^2.0.1 karma-rollup-preprocessor@^7.0.8 karma-sourcemap-loader@^0.4.0 \
    lerna@^6.6.2 lerna-changelog@^2.2.0 markdown-toc@^1.2.0 mocha@^11.7.5 moment-timezone@^0.6.1 \
    npm-check-updates@^19.6.6 nyc@^15.1.0 ramda@^0.32.0 rollup@^3.30.0 rollup-plugin-minification@^0.2.0 \
    ts-node@^10.9.2 typescript@^4.9.5 yargs@^17.7.2 \
 && chmod -R a+rX /opt/migration/dependencies

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
    if [ -e /opt/migration/dependencies/root/node_modules ]; then mkdir -p "$(dirname /workspace/repo/node_modules)" && rm -rf /workspace/repo/node_modules && cp -a /opt/migration/dependencies/root/node_modules /workspace/repo/node_modules; if [ -d /workspace/repo/.git/info ]; then grep -qxF 'node_modules' /workspace/repo/.git/info/exclude 2>/dev/null || printf '%s\n' 'node_modules' >> /workspace/repo/.git/info/exclude; fi; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]
