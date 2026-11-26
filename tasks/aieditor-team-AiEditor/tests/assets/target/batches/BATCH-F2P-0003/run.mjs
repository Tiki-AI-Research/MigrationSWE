#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import {spawnSync} from 'node:child_process'
import {fileURLToPath} from 'node:url'
import {tmpdir} from 'node:os'

const self = fileURLToPath(import.meta.url)
const repoRoot = path.resolve(process.argv[2] ?? '/workspace/repo')
process.env.MIGRATION_REPO_ROOT = repoRoot

// Keep candidate uncaught exceptions (notably SDK cancellation failures) local
// to their observation. No probe assertions are changed or skipped.
if (process.argv[3] === '--observation') {
  const observationId = process.argv[4]
  const outputPath = process.argv[5]
  const {runObservation} = await import('../../../shared/batches/BATCH-F2P-0003/probe-suite.mjs')
  let result
  try {
    result = await runObservation(observationId, repoRoot)
  } catch (error) {
    result = {
      observation_id: observationId,
      status: 'FAIL',
      message: error instanceof Error ? error.message : String(error),
      side: 'target',
    }
  }
  fs.writeFileSync(outputPath, JSON.stringify(result))
  process.exit(result.status === 'PASS' ? 0 : 1)
}

const inventory = JSON.parse(fs.readFileSync(new URL('../../../fixtures/batches/BATCH-F2P-0003/observation-probes.json', import.meta.url), 'utf8'))
const OBSERVATIONS = inventory.observations.map((entry) => entry.id)
const reportPath = process.argv[3]
const results = []
const scratch = fs.mkdtempSync(path.join(tmpdir(), 'f2p-0003-observations-'))
const loader = fileURLToPath(new URL('../../../shared/batches/BATCH-F2P-0003/loader.mjs', import.meta.url))
const deadline = Date.now() + 15 * 60 * 1000
try {
  for (const observationId of OBSERVATIONS) {
    const outputPath = path.join(scratch, `${observationId}.json`)
    const child = spawnSync(process.execPath, [
      '--experimental-strip-types', '--experimental-loader', loader,
      self, repoRoot, '--observation', observationId, outputPath,
    ], {
      encoding: 'utf8', env: process.env,
      timeout: Math.max(1, deadline - Date.now()),
      maxBuffer: 64 * 1024 * 1024,
    })
    let result
    if (fs.existsSync(outputPath)) {
      try { result = JSON.parse(fs.readFileSync(outputPath, 'utf8')) } catch { /* invalid worker report */ }
    }
    if (result?.observation_id === observationId && child.status === 0 && result.status === 'PASS') {
      results.push(result)
    } else {
      results.push({
        observation_id: observationId,
        status: child.error ? 'INCONCLUSIVE' : 'FAIL',
        message: String(child.error?.message || result?.message || child.stderr || child.stdout || `probe process exited ${child.status} (${child.signal ?? 'no signal'})`).slice(-4000),
        side: 'target',
      })
    }
  }
} finally {
  fs.rmSync(scratch, {recursive: true, force: true})
}
const report = {
  batch_id: 'BATCH-F2P-0003',
  side: 'target',
  repo_root: repoRoot,
  expectations: 'PASS',
  observations: OBSERVATIONS,
  results,
}
const serialized = `${JSON.stringify(report, null, 2)}\n`
if (reportPath) {
  fs.mkdirSync(path.dirname(reportPath), {recursive: true})
  fs.writeFileSync(reportPath, serialized)
}
process.stdout.write(serialized)
process.exit(results.every((result) => result.status === 'PASS') ? 0 : 1)
