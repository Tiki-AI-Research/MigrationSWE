#!/usr/bin/env node
import {spawnSync} from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'

const batchId = 'BATCH-P2P-0002'
const here = path.dirname(fileURLToPath(import.meta.url))
const assetsRoot = path.resolve(here, '../../..')
const [side, repoArg, resultArg] = process.argv.slice(2)
if (!side || !repoArg) {
  console.error('usage: run-probes.mjs <source|target> <repo-root> [result-file]')
  process.exit(2)
}
const repoRoot = path.resolve(repoArg)
const probeSource = path.join(here, 'probe.test.ts')
const probeTargetDir = path.join(repoRoot, 'tests', 'p2p')
const probeTarget = path.join(probeTargetDir, `${batchId}.test.ts`)
const resultFile = resultArg
  ? path.resolve(resultArg)
  : path.join(assetsRoot, side, 'batches', batchId, `${side}-results.json`)
const rawResultFile = path.join(repoRoot, 'tests', 'p2p', `${batchId}.raw-results.json`)

const modulePaths = {
  P2P_REPO_ROOT: repoRoot,
  P2P_AI_MODULE: path.join(repoRoot, 'src/ai/index.ts'),
  P2P_TINYUI_MODULE: path.join(repoRoot, 'src/tinyui/index.ts'),
  P2P_BLOCK_DRAG_MODULE: path.join(repoRoot, 'src/features/block-drag/BlockDragMenu.ts'),
  P2P_EDITOR_HELPER: path.join(repoRoot, 'tests/helpers/editor.ts'),
  P2P_ROOT_INDEX_MODULE: path.join(repoRoot, 'src/index.ts'),
  P2P_RESULT_FILE: rawResultFile,
}

const vitest = path.join(repoRoot, 'node_modules', '.bin', 'vitest')
const command = fs.existsSync(vitest) ? vitest : 'npx'
const args = fs.existsSync(vitest)
  ? ['run', 'tests/p2p/BATCH-P2P-0002.test.ts', '--reporter=dot']
  : ['vitest', 'run', 'tests/p2p/BATCH-P2P-0002.test.ts', '--reporter=dot']

fs.mkdirSync(probeTargetDir, {recursive: true})
fs.copyFileSync(probeSource, probeTarget)
try {
  fs.rmSync(rawResultFile, {force: true})
  const run = spawnSync(command, args, {
    cwd: repoRoot,
    env: {...process.env, ...modulePaths},
    stdio: 'inherit',
  })
  const observations = fs.existsSync(rawResultFile)
    ? JSON.parse(fs.readFileSync(rawResultFile, 'utf8'))
    : {}
  const expected = JSON.parse(fs.readFileSync(path.join(here, 'observations.json'), 'utf8')).observations
  const missing = expected.map((item) => item.id).filter((id) => !observations[id])
  const failed = Object.entries(observations)
    .filter(([, value]) => value?.status !== 'PASS')
    .map(([id]) => id)
  const document = {
    schema_version: 'migration-asset-probe-result:v1',
    batch_id: batchId,
    kind: 'P2P',
    side,
    repo_root: repoRoot,
    probe_file: 'tests/p2p/BATCH-P2P-0002.test.ts',
    command: [command, ...args],
    status: run.status === 0 && missing.length === 0 && failed.length === 0 ? 'PASS' : 'FAIL',
    observations,
  }
  fs.mkdirSync(path.dirname(resultFile), {recursive: true})
  fs.writeFileSync(resultFile, `${JSON.stringify(document, null, 2)}\n`)
  if (document.status !== 'PASS') {
    if (missing.length) console.error(`missing observations: ${missing.join(', ')}`)
    if (failed.length) console.error(`failed observations: ${failed.join(', ')}`)
    process.exitCode = 1
  }
} finally {
  fs.rmSync(probeTarget, {force: true})
  fs.rmSync(rawResultFile, {force: true})
}
