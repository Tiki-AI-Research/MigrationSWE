import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

export const resolveRepo = (explicit) =>
  explicit ||
  process.env.MIGRATION_REPO ||
  process.env.MIGRATION_REPO_ROOT ||
  process.env.REPO ||
  process.env.REPO_ROOT ||
  '/workspace/repo'

const tsxCandidates = (repo) => [
  path.join(repo, 'node_modules', '.bin', 'tsx'),
  path.join(repo, 'node_modules', '.bin', 'tsx.cmd'),
]

const findRunner = (repo) => {
  for (const candidate of tsxCandidates(repo)) {
    if (fs.existsSync(candidate)) return { command: candidate, args: [] }
  }
  return { command: 'pnpm', args: ['exec', 'tsx'] }
}

const parseOutput = (stdout) => {
  const lines = String(stdout || '')
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      return JSON.parse(lines[index])
    } catch {
      // Keep looking for the JSON line emitted by probe.ts.
    }
  }
  return null
}

export function runAll(repo, side, ids) {
  const runner = findRunner(repo)
  const probe = path.join(here, 'probe.ts')
  const args = [...runner.args, probe, repo, side, ids.join(',')]
  const child = spawnSync(runner.command, args, {
    cwd: repo,
    encoding: 'utf8',
    env: { ...process.env },
    maxBuffer: 1024 * 1024 * 16,
  })

  const parsed = parseOutput(child.stdout)
  if (child.error || !parsed) {
    const error = child.error
      ? String(child.error)
      : String(child.stderr || 'probe did not return JSON')
    return ids.map((id) => ({
      id,
      passed: false,
      status: 'INFRA_ERROR',
      error: error.slice(0, 500),
    }))
  }

  return ids.map((id) => {
    const entry = parsed[id] || {}
    return {
      id,
      passed: entry.passed === true,
      ...(entry.status ? { status: entry.status } : {}),
      ...(entry.error ? { error: entry.error } : {}),
    }
  })
}

export function runObservation(repo, side, id) {
  const [result] = runAll(resolveRepo(repo), side, [id])
  return result
}
