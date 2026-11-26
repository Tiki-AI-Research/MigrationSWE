import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { checks } from '../../../shared/batches/BATCH-F2P-0001/checks.mjs'

const ROOT_ENV = 'MIGRATION_TARGET_ROOT'

export async function loadTargetApi(root = process.env[ROOT_ENV] || process.cwd()) {
  const entry = path.join(root, 'packages/utils/markdown-parse/index.ts')
  return import(pathToFileURL(entry).href)
}

export async function runTargetProbe(api) {
  const results = []
  for (const check of checks) {
    try {
      const ok = await check.run(api)
      results.push({
        observation_id: check.id,
        requirement_ids: check.requirement_ids,
        status: ok ? 'PASS' : 'FAIL',
      })
    } catch (error) {
      results.push({
        observation_id: check.id,
        requirement_ids: check.requirement_ids,
        status: 'FAIL',
        error: String((error && error.message) || error),
      })
    }
  }
  return results
}

const isMain = process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url

if (isMain) {
  const api = await loadTargetApi()
  const results = await runTargetProbe(api)
  console.log(JSON.stringify({ side: 'target', results }, null, 2))
}
