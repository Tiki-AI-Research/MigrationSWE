import {
  OBSERVATION_IDS,
  runAll,
} from '../../../shared/batches/BATCH-F2P-0002/checks.mjs'

const mod = await import('../../../packages/utils/markdown-parse/index.ts')
const results = await runAll(mod)
const passed = results.filter((result) => result.status === 'PASS')
const failed = results.filter((result) => result.status !== 'PASS')

const report = {
  batch_id: 'BATCH-F2P-0002',
  side: 'target',
  expected: 'PASS',
  observation_ids: OBSERVATION_IDS,
  passed: passed.map((result) => result.observation_id),
  failed: failed.map((result) => ({
    observation_id: result.observation_id,
    checks: result.checks,
  })),
}

console.log(JSON.stringify(report, null, 2))

if (failed.length > 0) {
  process.exitCode = 1
}
