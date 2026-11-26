import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

export function dependencyDetails(repo) {
  const require = createRequire(path.join(repo, 'package.json'));
  // semver is present in the frozen evaluator dependency tree. Evaluate the
  // whole range, accepting equivalent bounds rather than one spelling.
  const semver = require('semver');
  const manifest = JSON.parse(readFileSync(path.join(repo, 'package.json'), 'utf8'));
  const dependencies = manifest.dependencies || {};
  const spec = dependencies.fastify;
  const range = typeof spec === 'string' ? spec.replace(/^npm:fastify@/, '') : '';
  let fastifyPinned = false;
  try {
    const minimum = semver.minVersion(range);
    fastifyPinned = Boolean(minimum && minimum.major === 5 && minimum.minor === 12
      && semver.subset(range, '>=5.12.0 <5.13.0'));
  } catch { /* a missing or invalid dependency is not pinned */ }
  const koaRuntime = Object.keys(dependencies).some((name) => (
    name === 'koa' || name.startsWith('@koa/') || name.startsWith('koa-')
  ));
  return { fastifyPinned, koaRuntime };
}

export function dependencyPolicy(repo) {
  const { fastifyPinned, koaRuntime } = dependencyDetails(repo);
  return fastifyPinned && !koaRuntime;
}
