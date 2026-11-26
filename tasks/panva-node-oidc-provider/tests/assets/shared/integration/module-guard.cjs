// Runtime module guard (preload).
//
// Loaded with `--require` into every process the verifier harness starts
// (including children spawned by the candidate).  It records the *resolved*
// module specifier of every CommonJS `require()`/`createRequire()` call, and
// registers an ESM loader hook that records every `import()`/static import.
// Recording the runtime specifier -- rather than pattern-matching source text
// -- means obfuscated specifiers such as `[...].reverse().join('')` or
// `['koa'][0]` are observed exactly like a plain `import 'koa'`.
//
// It is inert unless MIGRATION_MODULE_GUARD_LOG is set.
'use strict';

const LOG = process.env.MIGRATION_MODULE_GUARD_LOG || '';

if (LOG) {
  const fs = require('node:fs');
  const path = require('node:path');
  const Module = require('node:module');

  const TOKENS = (process.env.MIGRATION_SOURCE_TOKENS || '')
    .split(',')
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
  const VERIFIER_ROOT = process.env.MIGRATION_VERIFIER_ROOT || '';

  const under = (root, file) => {
    if (!root || !file) return false;
    try {
      const relative = path.relative(root, file);
      return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
    } catch {
      return false;
    }
  };

  const matchesToken = (specifier) => {
    const value = String(specifier || '').toLowerCase();
    return TOKENS.some(
      (token) => value === token || value.startsWith(`${token}/`) || value.includes(`/${token}`),
    );
  };

  // Verifier-owned code (the probes live under /tests) is never a candidate
  // signal, so a probe that legitimately `require('koa')` cannot be mistaken
  // for a candidate fallback.
  const isVerifier = (file) => under(VERIFIER_ROOT, file);

  // `createRequire(<path in the candidate>)` makes `parent.filename` the
  // candidate's package.json, so the *caller* (stack) — not `parent` — decides
  // whether the load came from verifier code or from the candidate.
  const stackCaller = () => {
    const stack = new Error().stack || '';
    for (const line of stack.split('\n').slice(1)) {
      const match = line.match(/\(?(\/[^):\s]+?):\d+:\d+\)?\s*$/);
      if (match && !match[1].includes('module-guard')) return match[1];
    }
    return '';
  };

  const record = (kind, specifier, parent) => {
    try {
      fs.appendFileSync(
        LOG,
        JSON.stringify({
          kind,
          specifier: String(specifier || '').slice(0, 200),
          parent: String(parent || '').slice(0, 400),
          pid: process.pid,
        }) + '\n',
      );
    } catch {
      /* logging must never break the run */
    }
  };

  const originalLoad = Module._load;
  Module._load = function guardedLoad(request, parent, isMain) {
    try {
      if (matchesToken(request)) {
        const caller = stackCaller() || (parent && parent.filename) || '';
        if (!isVerifier(caller)) record('require', request, caller);
      }
    } catch {
      /* ignore */
    }
    return originalLoad.apply(this, arguments);
  };

  // ESM: `import(...)` and static imports bypass Module._load; a loader hook sees
  // the resolved specifier and its parent URL. It chains cleanly with a legacy
  // `--experimental-loader` when one is present.
  try {
    const hooksUrl = require('node:url').pathToFileURL(
      path.join(__dirname, 'module-guard-hooks.mjs'),
    ).href;
    Module.register(hooksUrl, { parentURL: hooksUrl });
  } catch {
    /* loader hooks unavailable: CJS coverage still applies */
  }
}
