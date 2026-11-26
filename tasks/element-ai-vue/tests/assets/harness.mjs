#!/usr/bin/env node
import { main } from './shared/integration/entry.mjs'

const argv = process.argv.slice(2)
const sideIndex = argv.findIndex((arg) => arg === '--side')
let side = 'target'
if (sideIndex >= 0) {
  side = argv[sideIndex + 1] || side
  argv.splice(sideIndex, 2)
} else {
  const sideFlag = argv.find((arg) => arg.startsWith('--side='))
  if (sideFlag) {
    side = sideFlag.slice('--side='.length)
    argv.splice(argv.indexOf(sideFlag), 1)
  }
}

const reportIndex = argv.findIndex((arg) => !arg.startsWith('--'))
const reportPath =
  reportIndex >= 0
    ? argv[reportIndex]
    : argv.find((arg) => arg.startsWith('--report='))
      ? argv.find((arg) => arg.startsWith('--report=')).slice('--report='.length)
      : '/logs/result.json'

main(side, [reportPath]).catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`)
  process.exitCode = 1
})
