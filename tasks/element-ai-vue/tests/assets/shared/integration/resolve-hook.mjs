import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

const repo = path.resolve(
  process.env.REPO_ROOT ||
    process.env.MIGRATION_REPO_ROOT ||
    process.env.MIGRATION_REPO ||
    process.env.REPO ||
    '/workspace/repo'
)

const repoRequire = createRequire(path.join(repo, 'package.json'))
const aliasPackages = {
  components: 'packages/components',
  utils: 'packages/utils',
  hooks: 'packages/hooks',
  directives: 'packages/directives',
  constants: 'packages/constants',
  locale: 'packages/locale',
  'element-ai-vue': 'packages/element-ai-vue',
}

const fileCandidates = (base) => [
  base,
  `${base}.ts`,
  `${base}.tsx`,
  `${base}.mts`,
  `${base}.js`,
  `${base}.mjs`,
  `${base}.cjs`,
  path.join(base, 'index.ts'),
  path.join(base, 'index.tsx'),
  path.join(base, 'index.mts'),
  path.join(base, 'index.js'),
  path.join(base, 'index.mjs'),
]

const findFile = (base) => {
  for (const candidate of fileCandidates(base)) {
    try {
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate
    } catch {
      return null
    }
  }
  return null
}

const aliasTarget = (specifier) => {
  if (specifier.startsWith('@element-ai-vue/')) {
    const rest = specifier.slice('@element-ai-vue/'.length)
    const [name, ...tail] = rest.split('/')
    const base = aliasPackages[name]
    if (!base) return null
    return findFile(path.join(repo, base, ...tail))
  }
  if (specifier === 'element-ai-vue') {
    return findFile(path.join(repo, aliasPackages['element-ai-vue']))
  }
  return null
}

const isBare = (specifier) =>
  !specifier.startsWith('.') &&
  !specifier.startsWith('/') &&
  !specifier.startsWith('file:') &&
  !specifier.startsWith('node:') &&
  !specifier.startsWith('data:')

export async function resolve(specifier, context, nextResolve) {
  const alias = aliasTarget(specifier)
  if (alias) return { url: pathToFileURL(alias).href, shortCircuit: true }
  try {
    return await nextResolve(specifier, context)
  } catch (error) {
    if (!isBare(specifier)) throw error
    const resolved = repoRequire.resolve(specifier)
    return { url: pathToFileURL(resolved).href, shortCircuit: true }
  }
}
