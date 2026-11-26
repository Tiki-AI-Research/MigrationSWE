import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ASSETS_ROOT = path.resolve(HERE, '../..')
const INVENTORY_FILE = path.join(HERE, 'observations.json')
const F2P_PROBE = path.join(HERE, 'probe-f2p.mjs')
const RESOLVE_BOOTSTRAP = path.join(HERE, 'register-resolve.mjs')

const MAX_BUFFER = 64 * 1024 * 1024
const REPO_ENV_KEYS = ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO']

const readInventory = () => {
  const parsed = JSON.parse(fs.readFileSync(INVENTORY_FILE, 'utf8'))
  const observations = Array.isArray(parsed)
    ? parsed
    : Array.isArray(parsed.observations)
      ? parsed.observations
      : []
  return observations.map((entry) => ({
    id: entry.id,
    kind: entry.kind,
    batch_id: entry.batch_id,
  }))
}

const resolveRepo = () => {
  for (const key of REPO_ENV_KEYS) {
    const value = process.env[key]
    if (value && value.trim()) return path.resolve(value)
  }
  return '/workspace/repo'
}

const findTsxLoader = (repo) => {
  const candidates = [
    path.join(repo, 'node_modules/tsx/dist/loader.mjs'),
    path.join(repo, 'node_modules/tsx/dist/esm/index.mjs'),
  ]
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  try {
    const repoRequire = createRequire(path.join(repo, 'package.json'))
    const resolved = repoRequire.resolve('tsx')
    if (resolved && fs.existsSync(resolved)) return resolved
  } catch {
    return null
  }
  return null
}

const parseJson = (text) => {
  const source = String(text || '')
  const trimmed = source.trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed)
  } catch {
    const start = trimmed.indexOf('{')
    const end = trimmed.lastIndexOf('}')
    if (start >= 0 && end > start) {
      try {
        return JSON.parse(trimmed.slice(start, end + 1))
      } catch {
        return null
      }
    }
  }
  return null
}

const runProcess = (command, args, { repo, env = {}, timeout = 300000 } = {}) =>
  spawnSync(command, args, {
    cwd: repo || ASSETS_ROOT,
    encoding: 'utf8',
    timeout,
    maxBuffer: MAX_BUFFER,
    env: { ...process.env, ...env },
  })

const tsxArgs = (repo) => {
  const loader = findTsxLoader(repo)
  return loader ? ['--import', loader] : ['--import', 'tsx']
}

const withTsx = (repo, script, args, options = {}) =>
  runProcess(process.execPath, [...tsxArgs(repo), script, ...args], options)

const nodeSearchPath = (repo) =>
  [
    path.join(repo, 'node_modules'),
    path.join(repo, 'node_modules/.pnpm/node_modules'),
    process.env.NODE_PATH,
  ]
    .filter(Boolean)
    .join(path.delimiter)

const withTsxAndResolver = (repo, script, args, options = {}) =>
  runProcess(
    process.execPath,
    [...tsxArgs(repo), '--import', RESOLVE_BOOTSTRAP, script, ...args],
    {
      ...options,
      env: {
        REPO_ROOT: repo,
        NODE_PATH: nodeSearchPath(repo),
        ...(options.env || {}),
      },
    }
  )

const makeEntries = (inventory, batchId, mapper) => {
  const entries = {}
  for (const observation of inventory) {
    if (observation.batch_id !== batchId) continue
    entries[observation.id] = mapper(observation)
  }
  return entries
}

const infraEntries = (inventory, batchId, child) =>
  makeEntries(inventory, batchId, () => ({
    status: 'INFRA_ERROR',
    value: child ? {
      error: String(child.error || child.stderr || child.stdout || 'probe returned no results').slice(0, 4000),
      exitCode: child.status,
      signal: child.signal,
    } : null,
  }))

const failEntries = (inventory, batchId) =>
  makeEntries(inventory, batchId, () => ({ status: 'FAIL', value: null }))

const runP2P0001 = (inventory, side, repo) => {
  const batchId = 'BATCH-P2P-0001'
  const root = path.join(ASSETS_ROOT, side, 'batches', batchId)
  const probe = path.join(
    root,
    side === 'source' ? 'source-probe.cjs' : 'target-probe.cjs'
  )
  const child = runProcess(process.execPath, [probe, repo], {
    repo,
    env: { NODE_PATH: nodeSearchPath(repo) },
  })
  const report = parseJson(child.stdout)
  if (!report || !report.results) return infraEntries(inventory, batchId, child)
  return makeEntries(inventory, batchId, (observation) => {
    const value = report.results[observation.id]
    if (value === undefined) return { status: 'INFRA_ERROR', value: null }
    return { status: 'PASS', value }
  })
}

const runP2P0002 = (inventory, side, repo) => {
  const batchId = 'BATCH-P2P-0002'
  const sharedRun = path.join(ASSETS_ROOT, 'shared/batches', batchId, 'run.mjs')
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'p2p-0002-'))
  const outPath = path.join(outDir, `${side}.json`)
  const child = runProcess(
    process.execPath,
    [sharedRun, '--side', side, '--repo', repo, '--out', outPath],
    {
      repo,
      env: {
        NODE_PATH: nodeSearchPath(repo),
        MIGRATION_SIDE: side,
      },
    }
  )
  let report = null
  try {
    if (fs.existsSync(outPath)) {
      report = JSON.parse(fs.readFileSync(outPath, 'utf8'))
    }
  } catch {
    report = null
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true })
  }
  if (!report && child.stdout) report = parseJson(child.stdout)
  if (!report || !report.observations) return infraEntries(inventory, batchId, child)
  return makeEntries(inventory, batchId, (observation) => {
    const entry = report.observations[observation.id]
    if (!entry) return { status: 'INFRA_ERROR', value: null }
    const status = ['PASS', 'FAIL', 'NA', 'INFRA_ERROR', 'INCONCLUSIVE', 'TIMEOUT'].includes(entry.status)
      ? entry.status
      : entry.status === true
        ? 'PASS'
        : 'FAIL'
    return {
      status,
      value: entry.value === undefined ? null : entry.value,
    }
  })
}

const runF2PSmall = (inventory, batchId, side, repo) => {
  const child = withTsxAndResolver(repo, F2P_PROBE, [batchId, side, repo], { repo })
  const report = parseJson(child.stdout)
  if (!report || !report.results) return infraEntries(inventory, batchId, child)
  return makeEntries(inventory, batchId, (observation) => {
    const entry = report.results[observation.id]
    if (!entry) return { status: 'INFRA_ERROR', value: null }
    return {
      status: entry.status || 'FAIL',
      value: entry.value === undefined ? null : entry.value,
    }
  })
}

const runF2P0003 = (inventory, side, repo) => {
  const batchId = 'BATCH-F2P-0003'
  const probe = path.join(ASSETS_ROOT, 'shared/batches', batchId, 'probe.ts')
  const ids = inventory
    .filter((entry) => entry.batch_id === batchId)
    .map((entry) => entry.id)
  const child = withTsxAndResolver(repo, probe, [repo, side, ids.join(',')], { repo })
  const report = parseJson(child.stdout)
  if (!report) return infraEntries(inventory, batchId, child)
  return makeEntries(inventory, batchId, (observation) => {
    const entry = report[observation.id]
    if (!entry) return { status: 'INFRA_ERROR', value: null }
    const passed = entry.passed === true
    return {
      status: passed ? 'PASS' : entry.status === 'INFRA_ERROR' ? 'INFRA_ERROR' : 'FAIL',
      value: { passed, ...(entry.error ? { error: entry.error } : {}) },
    }
  })
}

const runDifferential0001 = (inventory, side, repo) => {
  const batchId = 'BATCH-DIFFERENTIAL-0001'
  const probe = path.join(ASSETS_ROOT, side, 'batches', batchId, 'probe.ts')
  const child = withTsxAndResolver(repo, probe, [repo], {
    repo,
    env: {
      REPO_ROOT: repo,
      NODE_MODULES_ROOT: path.join(repo, 'node_modules'),
    },
  })
  const report = parseJson(child.stdout)
  if (!report || !report.results) return infraEntries(inventory, batchId, child)
  return makeEntries(inventory, batchId, (observation) => {
    const entry = report.results[observation.id]
    if (!entry) return { status: 'INFRA_ERROR', value: null }
    return {
      status: entry.status === 'PASS' ? 'PASS' : 'FAIL',
      value: entry.normalized === undefined ? (entry.error ? { error: entry.error } : null) : entry.normalized,
    }
  })
}

const runDifferential0002 = (inventory, side, repo) => {
  const batchId = 'BATCH-DIFFERENTIAL-0002'
  const probe = path.join(ASSETS_ROOT, side, 'batches', batchId, 'probe.mjs')
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'diff-0002-'))
  const outPath = path.join(outDir, `${side}.json`)
  const child = withTsxAndResolver(repo, probe, [repo, `--report=${outPath}`], {
    repo,
    env: {
      REPO_ROOT: repo,
      NODE_PATH: nodeSearchPath(repo),
    },
  })
  let report = null
  try {
    if (fs.existsSync(outPath)) {
      report = JSON.parse(fs.readFileSync(outPath, 'utf8'))
    }
  } catch {
    report = null
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true })
  }
  if (!report && child.stdout) report = parseJson(child.stdout)
  // A missing differential report means the candidate probe crashed or
  // produced no comparable result. That is a candidate-side differential
  // failure, not an evaluator infrastructure error.
  if (!report || !report.results) return failEntries(inventory, batchId)
  return makeEntries(inventory, batchId, (observation) => {
    const value = report.results[observation.id]
    if (value === undefined) return { status: 'FAIL', value: null }
    const check = report.observation_checks
      ? report.observation_checks[observation.id]
      : undefined
    const passed = check === undefined ? true : check === true
    return { status: passed ? 'PASS' : 'FAIL', value }
  })
}

const RUNNERS = [
  runP2P0001,
  runP2P0002,
  (inventory, side, repo) => runF2PSmall(inventory, 'BATCH-F2P-0001', side, repo),
  (inventory, side, repo) => runF2PSmall(inventory, 'BATCH-F2P-0002', side, repo),
  runF2P0003,
  runDifferential0001,
  runDifferential0002,
]

export async function runSide(side, repo = resolveRepo()) {
  const inventory = readInventory()
  const values = {}
  for (const runner of RUNNERS) {
    let produced = null
    try {
      produced = runner(inventory, side, repo)
    } catch {
      produced = null
    }
    if (produced) Object.assign(values, produced)
  }
  return inventory.map((observation) => {
    const result = values[observation.id] || { status: 'INFRA_ERROR', value: null }
    return {
      id: observation.id,
      kind: observation.kind,
      status: result.status,
      value: { type: 'json', value: result.value },
    }
  })
}

export async function main(side, argv = process.argv.slice(2)) {
  let reportPath = '/logs/result.json'
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--report' || arg === '--out') {
      reportPath = argv[index + 1] || reportPath
      index += 1
    } else if (arg.startsWith('--report=')) {
      reportPath = arg.slice('--report='.length)
    } else if (arg.startsWith('--out=')) {
      reportPath = arg.slice('--out='.length)
    } else if (!arg.startsWith('--')) {
      reportPath = arg
    }
  }
  const repo = resolveRepo()
  let observations
  try {
    observations = await runSide(side, repo)
  } catch {
    observations = readInventory().map((observation) => ({
      id: observation.id,
      kind: observation.kind,
      status: 'INFRA_ERROR',
      value: { type: 'json', value: null },
    }))
  }
  const report = {
    schema_version: 'migration-observations:v6',
    observations,
  }
  const text = `${JSON.stringify(report, null, 2)}\n`
  try {
    fs.mkdirSync(path.dirname(path.resolve(reportPath)), { recursive: true })
    fs.writeFileSync(reportPath, text)
  } catch {
    // stdout remains the canonical report when the report path is not writable.
  }
  process.stdout.write(text)
  return report
}

export { ASSETS_ROOT, readInventory, resolveRepo }
