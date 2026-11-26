#!/usr/bin/env node
import {fileURLToPath} from 'node:url'
import {runSide} from '../../../shared/batches/BATCH-P2P-0001/runner-core.mjs'

const assetsRoot = fileURLToPath(new URL('../../../', import.meta.url))
const repoRoot = process.argv[2] ?? process.env.MIGRATION_REPO_ROOT ?? '/workspace/repo'
const reportPath = process.argv[3] ?? process.env.MIGRATION_REPORT_PATH

const result = runSide({
  assetsRoot,
  side: 'target',
  batchId: 'BATCH-P2P-0001',
  repoRoot,
  reportPath,
})
process.exitCode = result.success ? 0 : 1
