import { main } from './entry.mjs'

const argv = process.argv.slice(2)
const reportIndex = argv.findIndex((arg) => !arg.startsWith('--'))
const reportPath =
  reportIndex >= 0
    ? argv[reportIndex]
    : argv.find((arg) => arg.startsWith('--report='))
      ? argv.find((arg) => arg.startsWith('--report=')).slice('--report='.length)
      : '/logs/result.json'

main('source', [reportPath]).catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`)
  process.exitCode = 1
})
