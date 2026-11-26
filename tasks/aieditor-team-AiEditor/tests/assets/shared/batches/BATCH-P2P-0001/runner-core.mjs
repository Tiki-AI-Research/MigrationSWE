#!/usr/bin/env node
import {spawnSync} from 'node:child_process'
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import {join, resolve} from 'node:path'

function readManifest(assetsRoot, batchId) {
  const manifestPath = join(assetsRoot, 'fixtures', 'batches', batchId, 'scope-manifest.json')
  return JSON.parse(readFileSync(manifestPath, 'utf8'))
}

function findVitest(repoRoot) {
  const candidates = [
    join(repoRoot, 'node_modules', 'vitest', 'vitest.mjs'),
    join(repoRoot, 'node_modules', 'vitest', 'dist', 'cli.js'),
  ]
  return candidates.find((candidate) => existsSync(candidate))
}

function collectAssertions(value, output = []) {
  if (!value || typeof value !== 'object') return output
  if (Array.isArray(value)) {
    for (const item of value) collectAssertions(item, output)
    return output
  }
  if (typeof value.title === 'string' && typeof value.status === 'string') output.push(value)
  for (const nested of Object.values(value)) {
    if (nested && typeof nested === 'object') collectAssertions(nested, output)
  }
  return output
}

function normalizeStatus(status) {
  return status === 'passed' || status === 'pass' || status === 'PASS' ? 'PASS' : 'FAIL'
}

export function runSide({assetsRoot, side, batchId, repoRoot, reportPath}) {
  const root = resolve(repoRoot ?? process.cwd())
  const manifest = readManifest(assetsRoot, batchId)
  const observationIds = manifest.observations.map((observation) => observation.id)
  const scratch = join(root, 'tests', `p2p-batch-${batchId}`)
  const report = {
    batch_id: batchId,
    side,
    success: false,
    observations: {},
    error: undefined,
  }

  try {
    mkdirSync(scratch, {recursive: true})
    const shared = join(assetsRoot, 'shared', 'batches', batchId)
    for (const file of ['p2p.test.ts', 'types.fixture.ts', 'tsconfig.fixture.json']) {
      const source = join(shared, file)
      if (existsSync(source)) copyFileSync(source, join(scratch, file))
    }

    const vitest = findVitest(root)
    if (!vitest) throw new Error('vitest is not installed in the workspace')

    const jsonPath = join(scratch, 'vitest-results.json')
    rmSync(jsonPath, {force: true})
    const execution = spawnSync(
      process.execPath,
      [vitest, 'run', join(scratch, 'p2p.test.ts'), '--reporter=json', `--outputFile=${jsonPath}`],
      {cwd: root, encoding: 'utf8', env: {...process.env}},
    )

    let assertions = []
    if (existsSync(jsonPath)) {
      assertions = collectAssertions(JSON.parse(readFileSync(jsonPath, 'utf8')))
    }

    for (const id of observationIds) {
      const match = assertions.find((assertion) => assertion.title === id || assertion.title.startsWith(`${id} `))
      report.observations[id] = match ? normalizeStatus(match.status) : 'FAIL'
    }

    report.success = observationIds.every((id) => report.observations[id] === 'PASS')
    if (!report.success && execution.status !== 0 && !assertions.length) {
      report.error = (execution.stderr || execution.stdout || 'test runner failed').trim().slice(0, 4000)
    }
  } catch (error) {
    for (const id of observationIds) report.observations[id] = 'FAIL'
    report.success = false
    report.error = error instanceof Error ? error.message : String(error)
  }

  if (reportPath) {
    mkdirSync(join(reportPath, '..'), {recursive: true})
    writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`)
  }
  return report
}
