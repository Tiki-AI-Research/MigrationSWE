#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { pathToFileURL, fileURLToPath } from 'node:url'
import {
  BATCH_ID,
  OBSERVATION_IDS,
  assertObservation,
  runCorpus,
} from '../../../shared/batches/BATCH-DIFFERENTIAL-0002/corpus.mjs'

const SIDE = 'target'

const args = process.argv.slice(2)
const positional = args.filter((arg) => !arg.startsWith('--'))
const reportArgument = args.find((arg) => arg.startsWith('--report='))
const repoRoot = path.resolve(
  process.env.REPO_ROOT || positional[0] || '/workspace/repo'
)
const reportPath =
  process.env.PROBE_REPORT ||
  (reportArgument ? reportArgument.slice('--report='.length) : '')

const repoRequire = createRequire(path.join(repoRoot, 'package.json'))

const tsxLoaded = process.execArgv.some((argument) =>
  argument.replaceAll('\\', '/').includes('tsx')
)
if (!tsxLoaded) {
  const tsxLoader = pathToFileURL(repoRequire.resolve('tsx')).href
  const child = spawnSync(
    process.execPath,
    ['--import', tsxLoader, fileURLToPath(import.meta.url), ...args],
    { cwd: process.cwd(), env: process.env, stdio: 'inherit' }
  )
  process.exit(child.status === null ? 1 : child.status)
}

const { JSDOM } = repoRequire('jsdom')
const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/',
})
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.Node = dom.window.Node
globalThis.DOMPurify = undefined

const { renderToString } = repoRequire('@vue/server-renderer')

const loadRepositoryModule = async (relativePath) => {
  const module = await import(
    pathToFileURL(path.join(repoRoot, relativePath)).href
  )
  return module.default ?? module
}

const api = await loadRepositoryModule(
  'packages/utils/markdown-parse/index.ts'
)
const constants = await loadRepositoryModule('packages/constants/markdown.ts')

const results = await runCorpus({
  repoRoot,
  api,
  katexConfig: constants.katexConfig,
  renderVNode: renderToString,
})

const observationChecks = Object.fromEntries(
  OBSERVATION_IDS.map((observationId) => [
    observationId,
    assertObservation(observationId, results[observationId]),
  ])
)
const allPassed = Object.values(observationChecks).every(Boolean)

const payload = {
  schema_version: 'migration-differential-probe:v1',
  batch_id: BATCH_ID,
  side: SIDE,
  observations: OBSERVATION_IDS,
  observation_checks: observationChecks,
  all_passed: allPassed,
  results,
}

if (reportPath) {
  fs.mkdirSync(path.dirname(path.resolve(reportPath)), { recursive: true })
  fs.writeFileSync(path.resolve(reportPath), JSON.stringify(payload, null, 2))
}

console.log(JSON.stringify(payload, null, 2))

if (!allPassed) process.exitCode = 1
