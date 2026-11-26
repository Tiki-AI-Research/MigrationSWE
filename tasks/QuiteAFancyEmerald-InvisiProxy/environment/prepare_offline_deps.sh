#!/usr/bin/env bash
set -euo pipefail

cd /workspace/repo
pnpm_cache_root=/opt/offline-deps/cache
pnpm_cache_dir="$pnpm_cache_root/pnpm"
pnpm_state_dir=/opt/offline-deps/pnpm/state
mkdir -p "$npm_config_store_dir" "$pnpm_cache_dir" "$pnpm_state_dir" "$NPM_CONFIG_CACHE" \
    "$COREPACK_HOME" "$PLAYWRIGHT_BROWSERS_PATH"

# pnpm's registry metadata cache and supply-chain state are separate from
# its package store. Share all three across root's warm-up and agent's offline
# installs, including when su changes HOME to /home/agent.
# This image's pnpm supports --state-dir, but rejects --cache-dir. Use the
# XDG cache location for registry metadata and explicit store/state options.
# Keep the original launcher in place (Corepack shims depend on its location),
# and use this wrapper during both image preparation and agent execution.
pnpm_command="$(command -v pnpm)"
if [ "$pnpm_command" != /opt/offline-deps/bin/pnpm ]; then
    mkdir -p /opt/offline-deps/bin
    printf '%s\n' '#!/bin/sh' 'set -eu' \
        "export XDG_CACHE_HOME=\"$pnpm_cache_root\"" \
        '# pnpm does not reliably honor the uppercase npm offline setting.' \
        '# Translate it for dependency commands; leave script arguments intact.' \
        'pnpm_subcommand=' \
        'skip_value=0' \
        'for argument in "$@"; do' \
        '    if [ "$skip_value" -eq 1 ]; then skip_value=0; continue; fi' \
        '    case "$argument" in' \
        '        --dir|-C|--filter|-F|--store-dir|--state-dir|--registry|--userconfig)' \
        '            skip_value=1 ;;' \
        '        --) continue ;;' \
        '        -*) ;;' \
        '        *) pnpm_subcommand=$argument; break ;;' \
        '    esac' \
        'done' \
        'case "${npm_config_offline:-${NPM_CONFIG_OFFLINE:-false}}" in' \
        '    true|TRUE|1)' \
        '        case "$pnpm_subcommand" in' \
        '            install|i|add|update|up|remove|rm|uninstall|un|prune|fetch|import|install-test|it)' \
        '                set -- --offline "$@" ;;' \
        '        esac ;;' \
        'esac' \
        "exec \"$pnpm_command\" --store-dir \"$npm_config_store_dir\" --state-dir \"$pnpm_state_dir\" \"\$@\"" \
        > /opt/offline-deps/bin/pnpm
    chmod 0755 /opt/offline-deps/bin/pnpm
fi
export PATH="/opt/offline-deps/bin:$PATH"

case "$(pnpm store path)" in
    "$npm_config_store_dir"|"$npm_config_store_dir"/*) ;;
    *) echo 'pnpm did not select the shared offline store' >&2; exit 1 ;;
esac

# Preserve the pinned image's packages and browser downloads without exposing
# root's home. Reinstall with the new store path so .modules.yaml agrees with
# the store that pnpm will use as the unprivileged agent.
if [ -d /root/.local/share/pnpm/store ]; then
    cp -a /root/.local/share/pnpm/store/. "$npm_config_store_dir/"
fi
if [ -d /root/.cache/pnpm ]; then
    cp -a /root/.cache/pnpm/. "$pnpm_cache_dir/"
fi
if [ -d /root/.local/state/pnpm ]; then
    cp -a /root/.local/state/pnpm/. "$pnpm_state_dir/"
fi
if [ -d /root/.cache/ms-playwright ]; then
    cp -a /root/.cache/ms-playwright/. "$PLAYWRIGHT_BROWSERS_PATH/"
fi
pnpm_version="$(pnpm --version)"
# The pinned image may provide pnpm directly without a Corepack executable.
# Only warm Corepack's cache when that manager is actually installed; the
# existing pnpm launcher is also used for all offline checks below.
if command -v corepack >/dev/null 2>&1; then
    corepack prepare "pnpm@$pnpm_version" --activate
fi
CI=true pnpm install --offline --frozen-lockfile

# Warm the complete target dependency graph in pnpm's own store, as well as
# npm's cache. Keep these temporary manifests outside the candidate repository.
mkdir -p /opt/offline-deps/express
printf '%s\n' '{"name":"harbor-offline-express","private":true}' \
    > /opt/offline-deps/express/package.json
cp pnpm-workspace.yaml /opt/offline-deps/express/pnpm-workspace.yaml
pnpm --dir /opt/offline-deps/express add --save-exact --ignore-scripts express@5.1.0
npm install --prefix /opt/offline-deps --no-save --no-audit --no-fund express@5.1.0
npm view express@5.1.0 dist.integrity >/dev/null

chown -R agent:agent /opt/offline-deps "$COREPACK_HOME" \
    "$PLAYWRIGHT_BROWSERS_PATH" /workspace/repo/node_modules
chmod -R a+rX /opt/offline-deps "$COREPACK_HOME" "$PLAYWRIGHT_BROWSERS_PATH"

# Fail the image build if the default wrapper cannot resolve and install a
# fresh target manifest offline under the same uid as the actual agent.
# Dropping the temporary lockfile exercises metadata resolution too.
rm -rf /opt/offline-deps/express/node_modules
rm -f /opt/offline-deps/express/pnpm-lock.yaml
su -s /bin/sh agent -c \
    'cd /opt/offline-deps/express && NPM_CONFIG_OFFLINE=true CI=true pnpm install --no-frozen-lockfile --ignore-scripts'
su -s /bin/sh agent -c \
    'cd /workspace/repo && NPM_CONFIG_OFFLINE=true CI=true pnpm install --frozen-lockfile'
