import {spawnSync} from 'node:child_process'
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import {tmpdir} from 'node:os'
import {dirname, join, resolve} from 'node:path'
import {fileURLToPath, pathToFileURL} from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const assetRoot = resolve(here, '..', '..')
const inventoryPath = join(here, 'observations.json')

const BATCHES = [
  ['BATCH-P2P-0001', 'runP2P0001'],
  ['BATCH-P2P-0002', 'runP2P0002'],
  ['BATCH-F2P-0001', 'runF2P0001'],
  ['BATCH-F2P-0002', 'runF2P0002'],
  ['BATCH-F2P-0003', 'runF2P0003'],
  ['BATCH-DIFFERENTIAL-0001', 'runDIFF0001'],
  ['BATCH-DIFFERENTIAL-0002', 'runDIFF0002'],
]

function readInventory() {
  const document = JSON.parse(readFileSync(inventoryPath, 'utf8'))
  const observations = Array.isArray(document) ? document : document.observations
  return observations.map((entry) => ({
    id: entry.id,
    kind: entry.kind,
    batch_id: entry.batch_id,
  }))
}

function idsForBatch(inventory, batchId) {
  return inventory.filter((entry) => entry.batch_id === batchId).map((entry) => entry.id)
}

function uniqueTempDir() {
  return mkdtempSync(join(tmpdir(), 'migration-integration-'))
}

function parseJsonFile(path) {
  if (!existsSync(path)) return undefined
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return undefined
  }
}

function parseJsonOutput(stdout) {
  const text = String(stdout ?? '').trim()
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    const first = text.indexOf('{')
    const last = text.lastIndexOf('}')
    if (first >= 0 && last > first) {
      try {
        return JSON.parse(text.slice(first, last + 1))
      } catch {
        return undefined
      }
    }
    return undefined
  }
}

function stableValue(value) {
  if (Array.isArray(value)) return value.map(stableValue)
  if (!value || typeof value !== 'object') return value
  const result = {}
  for (const key of Object.keys(value).sort()) {
    if (/^(duration|elapsed|startedAt|finishedAt|timestamp)$/i.test(key)) continue
    result[key] = stableValue(value[key])
  }
  return result
}

function normalizeStatus(status) {
  return status === 'PASS' || status === 'passed' || status === 'pass' ? 'PASS' : 'FAIL'
}

function findVitest(repoRoot) {
  const candidates = [
    join(repoRoot, 'node_modules', 'vitest', 'vitest.mjs'),
    join(repoRoot, 'node_modules', 'vitest', 'dist', 'cli.js'),
  ]
  return candidates.find((candidate) => existsSync(candidate))
}

function baseEnv(repoRoot, extra = {}) {
  return {...process.env, MIGRATION_REPO_ROOT: repoRoot, ...extra}
}

function runCommand(command, args, options = {}) {
  return spawnSync(command, args, {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    timeout: options.timeout ?? 15 * 60 * 1000,
    ...options,
    env: options.env ?? process.env,
  })
}

function buildIfNeeded(repoRoot) {
  const entry = join(repoRoot, 'dist', 'index.js')
  if (existsSync(entry)) return {ok: true, built: false}
  const packagePath = join(repoRoot, 'package.json')
  if (!existsSync(packagePath)) return {ok: false, error: 'package.json is missing'}
  const execution = runCommand('npm', ['run', 'build'], {
    cwd: repoRoot,
    env: baseEnv(repoRoot),
    timeout: 20 * 60 * 1000,
  })
  if (execution.error) return {ok: false, error: execution.error.message}
  if (execution.status !== 0) {
    return {ok: false, error: (execution.stderr || execution.stdout || 'build failed').trim().slice(0, 4000)}
  }
  return {ok: existsSync(entry), built: true, error: existsSync(entry) ? undefined : 'build did not produce dist/index.js'}
}

function record(results, ids, status, value, error) {
  for (const id of ids) {
    results.set(id, {
      status,
      value: stableValue(value ?? {outcome: status}),
      error: error === undefined ? undefined : String(error).slice(0, 4000),
    })
  }
}

async function runP2P0001(context, ids) {
  const batchId = 'BATCH-P2P-0001'
  const scratch = join(context.repoRoot, 'tests', `p2p-batch-${batchId}`)
  try {
    const modulePath = join(assetRoot, 'shared/batches/BATCH-P2P-0001/runner-core.mjs')
    const module = await import(pathToFileURL(modulePath).href)
    const report = module.runSide({
      assetsRoot: assetRoot,
      side: context.side,
      batchId,
      repoRoot: context.repoRoot,
    })
    const inconclusive = Boolean(report.error && /not installed|ENOENT|Cannot find/i.test(report.error))
    for (const id of ids) {
      const status = inconclusive ? 'INCONCLUSIVE' : normalizeStatus(report.observations?.[id])
      context.results.set(id, {
        status,
        value: stableValue({outcome: status}),
        error: report.error === undefined ? undefined : String(report.error).slice(0, 4000),
      })
    }
  } finally {
    rmSync(scratch, {recursive: true, force: true})
  }
}

function runP2P0002(context, ids) {
  const script = join(assetRoot, 'shared/batches/BATCH-P2P-0002/run-probes.mjs')
  const resultFile = join(context.tempDir, 'p2p-0002.json')
  const execution = runCommand(process.execPath, [script, context.side, context.repoRoot, resultFile], {
    cwd: context.repoRoot,
    env: baseEnv(context.repoRoot),
  })
  const document = parseJsonFile(resultFile)
  if (!document?.observations) {
    const message = (execution.stderr || execution.stdout || 'P2P batch probes did not produce a result file').trim().slice(0, 4000)
    record(context.results, ids, 'INCONCLUSIVE', null, message)
    return
  }
  for (const id of ids) {
    const observation = document.observations[id]
    if (!observation) {
      record(context.results, [id], 'INCONCLUSIVE', null, 'observation was not recorded')
      continue
    }
    const status = normalizeStatus(observation.status)
    context.results.set(id, {
      status,
      value: stableValue({outcome: status}),
      error: observation.error === undefined ? undefined : String(observation.error).slice(0, 4000),
    })
  }
}

function runF2P0001(context, ids) {
  const build = buildIfNeeded(context.repoRoot)
  if (!build.ok) {
    record(context.results, ids, 'INCONCLUSIVE', null, build.error)
    return
  }
  const script = join(assetRoot, context.side, 'batches/BATCH-F2P-0001/run-f2p.mjs')
  const resultFile = join(context.tempDir, 'f2p-0001.json')
  const execution = runCommand(process.execPath, [script, resultFile], {
    cwd: context.repoRoot,
    env: baseEnv(context.repoRoot),
  })
  const document = parseJsonFile(resultFile) ?? parseJsonOutput(execution.stdout)
  if (!Array.isArray(document?.observations)) {
    const message = (execution.stderr || execution.stdout || 'F2P batch probes did not produce a report').trim().slice(0, 4000)
    record(context.results, ids, 'INCONCLUSIVE', null, message)
    return
  }
  const byId = new Map(document.observations.map((entry) => [entry.observation_id, entry]))
  for (const id of ids) {
    const observation = byId.get(id)
    if (!observation) {
      record(context.results, [id], 'INCONCLUSIVE', null, 'observation was not recorded')
      continue
    }
    const status = normalizeStatus(observation.status)
    context.results.set(id, {
      status,
      value: stableValue({outcome: status}),
      error: observation.error === undefined ? undefined : String(observation.error).slice(0, 4000),
    })
  }
}

function runVitest(context, testPath, outputFile, extraArgs = []) {
  const vitest = findVitest(context.repoRoot)
  if (!vitest) return {ok: false, error: 'vitest is not installed in the workspace'}
  rmSync(outputFile, {force: true})
  const execution = runCommand(process.execPath, [
    vitest,
    'run',
    testPath,
    ...extraArgs,
    '--reporter=json',
    `--outputFile=${outputFile}`,
  ], {
    cwd: context.repoRoot,
    env: baseEnv(context.repoRoot),
  })
  const report = parseJsonFile(outputFile)
  if (!report) {
    return {
      ok: false,
      error: (execution.stderr || execution.stdout || 'vitest did not produce a JSON report').trim().slice(0, 4000),
    }
  }
  return {ok: true, report, execution}
}

function collectAssertions(value, output = []) {
  if (!value || typeof value !== 'object') return output
  if (Array.isArray(value)) {
    for (const item of value) collectAssertions(item, output)
    return output
  }
  if (typeof value.title === 'string' && typeof value.status === 'string') output.push(value)
  if (typeof value.fullName === 'string' && typeof value.status === 'string') output.push(value)
  for (const nested of Object.values(value)) {
    if (nested && typeof nested === 'object') collectAssertions(nested, output)
  }
  return output
}

function findAssertion(assertions, id) {
  return assertions.find((entry) => entry.title === id
    || String(entry.title).startsWith(`${id} `)
    || String(entry.fullName).startsWith(`${id} `))
}

function recordAssertions(context, ids, execution) {
  const assertions = collectAssertions(execution.report)
  for (const id of ids) {
    const assertion = findAssertion(assertions, id)
    if (!assertion) {
      record(context.results, [id], 'INCONCLUSIVE', null, 'assertion was not recorded')
      continue
    }
    const status = normalizeStatus(assertion.status)
    context.results.set(id, {
      status,
      value: stableValue({outcome: status}),
      error: status === 'PASS' ? undefined : assertion.failureMessages?.join('\n'),
    })
  }
}

function runF2P0002(context, ids) {
  const relativeTest = join('tests', 'f2p', `BATCH-F2P-0002-${context.side}`, 'OpenAiService.test.ts')
  const testPath = join(context.repoRoot, relativeTest)
  const fixtureSource = join(assetRoot, 'fixtures/batches/BATCH-F2P-0002/fake-model.ts')
  const fixtureDir = join(context.repoRoot, 'fixtures', 'batches', 'BATCH-F2P-0002')
  const fixtureTarget = join(fixtureDir, 'fake-model.ts')
  const testSource = join(assetRoot, context.side, 'batches/BATCH-F2P-0002', `OpenAiService.${context.side}.test.ts`)
  const outputFile = join(context.tempDir, 'f2p-0002.json')
  try {
    if (!existsSync(testSource)) throw new Error(`missing probe ${testSource}`)
    mkdirSync(dirname(testPath), {recursive: true})
    mkdirSync(fixtureDir, {recursive: true})
    copyFileSync(testSource, testPath)
    if (existsSync(fixtureSource)) copyFileSync(fixtureSource, fixtureTarget)
    const execution = runVitest(context, relativeTest, outputFile)
    if (!execution.ok) {
      record(context.results, ids, 'INCONCLUSIVE', null, execution.error)
      return
    }
    recordAssertions(context, ids, execution)
  } catch (error) {
    record(context.results, ids, 'INCONCLUSIVE', null, error instanceof Error ? error.message : String(error))
  } finally {
    rmSync(dirname(testPath), {recursive: true, force: true})
    rmSync(fixtureDir, {recursive: true, force: true})
  }
}

function runF2P0003(context, ids) {
  const script = join(assetRoot, context.side, 'batches/BATCH-F2P-0003/run.mjs')
  const resultFile = join(context.tempDir, 'f2p-0003.json')
  const conditions = String(process.env.NODE_OPTIONS ?? '')
  const nodeOptions = conditions.includes('--conditions=require')
    ? conditions
    : `${conditions} --conditions=require`.trim()
  const execution = runCommand(process.execPath, [script, context.repoRoot, resultFile], {
    cwd: context.repoRoot,
    env: baseEnv(context.repoRoot, {NODE_OPTIONS: nodeOptions}),
  })
  const document = parseJsonFile(resultFile) ?? parseJsonOutput(execution.stdout)
  if (!Array.isArray(document?.results)) {
    const message = (execution.stderr || execution.stdout || 'F2P target probes did not produce a report').trim().slice(0, 4000)
    record(context.results, ids, 'INCONCLUSIVE', null, message)
    return
  }
  const byId = new Map(document.results.map((entry) => [entry.observation_id, entry]))
  for (const id of ids) {
    const observation = byId.get(id)
    if (!observation) {
      record(context.results, [id], 'INCONCLUSIVE', null, 'observation was not recorded')
      continue
    }
    const status = observation.status === 'INCONCLUSIVE' ? 'INCONCLUSIVE' : normalizeStatus(observation.status)
    context.results.set(id, {
      status,
      value: stableValue({outcome: status}),
      error: observation.message === undefined ? undefined : String(observation.message).slice(0, 4000),
    })
  }
}

function runDIFF0001(context, ids) {
  const relativeTest = join('tests', 'idiff', `BATCH-DIFFERENTIAL-0001-${context.side}.test.ts`)
  const testPath = join(context.repoRoot, relativeTest)
  const testSource = join(assetRoot, context.side, 'batches/BATCH-DIFFERENTIAL-0001/shared-cases.test.ts')
  const outputFile = join(context.tempDir, 'diff-0001.json')
  const configPath = join(context.tempDir, 'vitest-diff-0001.config.mjs')
  try {
    if (!existsSync(testSource)) throw new Error(`missing probe ${testSource}`)
    writeFileSync(configPath, "export default {test: {environment: 'node', setupFiles: []}}\n")
    mkdirSync(dirname(testPath), {recursive: true})
    copyFileSync(testSource, testPath)
    const execution = runVitest(context, relativeTest, outputFile, ['--config', configPath])
    if (!execution.ok) {
      record(context.results, ids, 'INCONCLUSIVE', null, execution.error)
      return
    }
    recordAssertions(context, ids, execution)
  } catch (error) {
    record(context.results, ids, 'INCONCLUSIVE', null, error instanceof Error ? error.message : String(error))
  } finally {
    rmSync(testPath, {force: true})
  }
}

function runDIFF0002(context, ids) {
  const script = join(assetRoot, context.side, 'batches/BATCH-DIFFERENTIAL-0002/run.mjs')
  const execution = runCommand(process.execPath, [script, context.repoRoot], {
    cwd: context.repoRoot,
    env: baseEnv(context.repoRoot),
  })
  const document = parseJsonOutput(execution.stdout)
  if (!Array.isArray(document?.results)) {
    const message = (execution.stderr || execution.stdout || 'DIFFERENTIAL corpus did not produce a report').trim().slice(0, 4000)
    record(context.results, ids, 'INCONCLUSIVE', null, message)
    return
  }
  const byId = new Map(document.results.map((entry) => [entry.observation_id, entry]))
  for (const id of ids) {
    const observation = byId.get(id)
    if (!observation) {
      record(context.results, [id], 'INCONCLUSIVE', null, 'observation was not recorded')
      continue
    }
    const status = normalizeStatus(observation.status)
    context.results.set(id, {
      status,
      value: stableValue({outcome: status}),
      error: observation.error === undefined ? undefined : String(observation.error).slice(0, 4000),
    })
  }
}

const RUNNERS = {
  runP2P0001,
  runP2P0002,
  runF2P0001,
  runF2P0002,
  runF2P0003,
  runDIFF0001,
  runDIFF0002,
}

function resolveRepoRoot() {
  return resolve(
    process.env.MIGRATION_REPO_ROOT
    ?? process.env.MIGRATION_REPO
    ?? process.env.REPO_ROOT
    ?? process.env.REPO
    ?? process.cwd(),
  )
}

export async function runSide({side, repoRoot, reportPath}) {
  const root = resolve(repoRoot ?? resolveRepoRoot())
  const inventory = readInventory()
  const tempDir = uniqueTempDir()
  const context = {side, repoRoot: root, tempDir, assetRoot, results: new Map()}
  try {
    for (const [batchId, runnerName] of BATCHES) {
      const ids = idsForBatch(inventory, batchId)
      try {
        await RUNNERS[runnerName](context, ids)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        for (const id of ids) {
          if (!context.results.has(id)) record(context.results, [id], 'INCONCLUSIVE', null, message)
        }
      }
    }
    const observations = inventory.map((entry) => {
      const result = context.results.get(entry.id) ?? {
        status: 'INCONCLUSIVE',
        value: {outcome: 'INCONCLUSIVE'},
        error: 'observation was not recorded',
      }
      const observation = {
        id: entry.id,
        kind: entry.kind,
        status: result.status,
        value: {type: 'json', value: result.value},
      }
      if (result.error !== undefined) observation.error = result.error
      return observation
    })
    const report = {
      schema_version: 'migration-observations:v6',
      batch_id: 'BATCH-INTEGRATION-0001',
      side,
      repo_root: root,
      observations,
    }
    const serialized = `${JSON.stringify(report, null, 2)}\n`
    if (reportPath) {
      mkdirSync(dirname(resolve(reportPath)), {recursive: true})
      writeFileSync(resolve(reportPath), serialized)
    }
    process.stdout.write(serialized)
    return report
  } finally {
    rmSync(tempDir, {recursive: true, force: true})
  }
}
