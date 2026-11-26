import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WORKER = path.join(HERE, 'worker.mjs')

const argv = process.argv.slice(2)
const valueOf = (name) => {
  const index = argv.indexOf(name)
  return index >= 0 ? argv[index + 1] : undefined
}

const repo = path.resolve(
  valueOf('--repo') || process.env.REPO_ROOT || process.cwd()
)
const out = valueOf('--out') || process.env.REPORT_PATH || ''
const side = valueOf('--side') || process.env.MIGRATION_SIDE || 'unknown'

const loaders = [
  path.join(repo, 'node_modules/tsx/dist/loader.mjs'),
  path.join(repo, 'node_modules/tsx/dist/esm/index.mjs'),
]
const loader = loaders.find((candidate) => fs.existsSync(candidate))

const workerArgs = [WORKER, '--repo', repo, '--side', side]
if (out) workerArgs.push('--out', out)

const nodeArgs = loader
  ? ['--import', loader, ...workerArgs]
  : ['--import', 'tsx', ...workerArgs]

const result = spawnSync(process.execPath, nodeArgs, {
  cwd: repo,
  stdio: 'inherit',
  env: { ...process.env, P2P_TSX_ACTIVE: '1' },
})
process.exit(result.status ?? 1)
