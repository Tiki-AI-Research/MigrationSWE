// Module resolution shim for the integration harness.
//
// The per-batch probe modules are published under the asset root, which is not
// the repository that owns the installed runtime dependencies. This loader only
// intervenes when the normal ESM resolution of a bare specifier fails, in which
// case it retries through the repository's own package resolution. It never
// overrides a successful resolution, so the repository's `lib/` module graph
// keeps resolving exactly as it would in production.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.env.MIGRATION_REPO_ROOT
  || process.env.MIGRATION_REPO
  || process.env.REPO_ROOT
  || process.env.REPO
  || '/workspace/repo';

let require;
try {
  require = createRequire(path.join(root, 'package.json'));
} catch {
  require = undefined;
}

function isBare(specifier) {
  return /^[^./]/.test(specifier)
    && !specifier.startsWith('node:')
    && !specifier.startsWith('data:')
    && !specifier.startsWith('file:');
}

export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context);
  } catch (error) {
    if (require && isBare(specifier)) {
      try {
        return { url: pathToFileURL(require.resolve(specifier)).href, shortCircuit: true };
      } catch {
        // fall through to the original resolution failure
      }
    }
    throw error;
  }
}
