// ESM loader hook paired with module-guard.cjs.  Records the resolved specifier
// of dynamic/static ESM imports so obfuscated module references are observed at
// runtime.  Inert unless MIGRATION_MODULE_GUARD_LOG is set.
import { appendFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const LOG = process.env.MIGRATION_MODULE_GUARD_LOG || '';
const VERIFIER_ROOT = process.env.MIGRATION_VERIFIER_ROOT || '';
const TOKENS = (process.env.MIGRATION_SOURCE_TOKENS || '')
  .split(',')
  .map((value) => value.trim().toLowerCase())
  .filter(Boolean);

function under(root, file) {
  if (!root || !file) return false;
  try {
    const relative = path.relative(root, file);
    return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
  } catch {
    return false;
  }
}

function matchesToken(specifier) {
  const value = String(specifier || '').toLowerCase();
  return TOKENS.some(
    (token) => value === token || value.startsWith(`${token}/`) || value.includes(`/${token}`),
  );
}

export async function resolve(specifier, context, nextResolve) {
  try {
    if (LOG && matchesToken(specifier)) {
      const parentUrl = context && context.parentURL;
      let parent = '';
      try {
        parent = parentUrl && parentUrl.startsWith('file:') ? fileURLToPath(parentUrl) : String(parentUrl || '');
      } catch {
        parent = String(parentUrl || '');
      }
      if (!under(VERIFIER_ROOT, parent)) {
        appendFileSync(
          LOG,
          JSON.stringify({ kind: 'import', specifier: String(specifier).slice(0, 200), parent: parent.slice(0, 400), pid: process.pid }) + '\n',
        );
      }
    }
  } catch {
    /* ignore */
  }
  return nextResolve(specifier, context);
}
