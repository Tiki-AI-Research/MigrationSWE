import fs from 'node:fs'
import path from 'node:path'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'

const dependencyRoots = [
  process.env.MIGRATION_DEPENDENCY_ROOT,
  process.env.MIGRATION_REPO_ROOT ? path.join(process.env.MIGRATION_REPO_ROOT, 'node_modules') : undefined,
  '/opt/migration/dependencies/node_modules',
  '/assets/dependencies/node_modules',
].filter(Boolean)

const dependencyRequires = dependencyRoots.map((root) => createRequire(path.join(root, 'package.json')))

function fileExists(value) {
  try {
    return fs.statSync(value).isFile()
  } catch {
    return false
  }
}

function resolveDependency(specifier) {
  for (const requireFromDependency of dependencyRequires) {
    try {
      return requireFromDependency.resolve(specifier)
    } catch {
      continue
    }
  }
  return null
}

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context)
  } catch (error) {
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const base = new URL(specifier, context.parentURL)
      const candidates = [
        base.pathname,
        `${base.pathname}.ts`,
        `${base.pathname}.mjs`,
        `${base.pathname}.js`,
        path.join(base.pathname, 'index.ts'),
        path.join(base.pathname, 'index.js'),
      ]
      for (const candidate of candidates) {
        if (fileExists(candidate)) {
          return {url: pathToFileURL(candidate).href, shortCircuit: true}
        }
      }
    }
    if (!specifier.startsWith('.') && !specifier.startsWith('/') && !specifier.startsWith('file:')) {
      const resolved = resolveDependency(specifier)
      if (resolved) {
        return {url: pathToFileURL(resolved).href, shortCircuit: true}
      }
    }
    throw error
  }
}
