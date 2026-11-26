#!/usr/bin/env node
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { runAll } from '../../../shared/batches/BATCH-F2P-0001/probes.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(process.env.MIGRATION_REPO_ROOT ?? join(here, '..', '..', '..'))
const entry = join(repoRoot, 'dist', 'index.js')
if (!existsSync(entry)) {
  console.error(`target F2P probes require ${entry}; run npm run build first`)
  process.exit(2)
}

const module = await import(pathToFileURL(entry).href)
const packageJson = JSON.parse(readFileSync(join(repoRoot, 'package.json'), 'utf8'))
const results = await runAll({ ...module, packageJson, repoRoot, side: 'target' })
const report = { side: 'target', repo_root: repoRoot, observations: results }
const output = JSON.stringify(report, null, 2)

if (process.argv[2]) writeFileSync(process.argv[2], `${output}\n`)
process.stdout.write(`${output}\n`)
