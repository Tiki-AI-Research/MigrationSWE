'use strict'

const path = require('node:path')
const { run } = require('../../../shared/batches/BATCH-P2P-0001/p2p-probe.cjs')

const root = path.resolve(
  process.argv[2] || process.env.P2P_TARGET_ROOT || process.cwd()
)

run(root)
  .then((results) => {
    process.stdout.write(JSON.stringify({ side: 'target', root, results, result: 'PASS' }, null, 2) + '\n')
  })
  .catch((error) => {
    process.stderr.write(JSON.stringify({ side: 'target', root, result: 'FAIL', error: error && error.stack ? error.stack : String(error) }, null, 2) + '\n')
    process.exitCode = 1
  })
