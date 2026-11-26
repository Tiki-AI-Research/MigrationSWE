import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SHARED_RUN = path.resolve(
  HERE,
  '../../../shared/batches/BATCH-P2P-0002/run.mjs'
)

const result = spawnSync(
  process.execPath,
  [SHARED_RUN, '--side', 'target', ...process.argv.slice(2)],
  { stdio: 'inherit', env: { ...process.env, MIGRATION_SIDE: 'target' } }
)
process.exit(result.status ?? 1)
