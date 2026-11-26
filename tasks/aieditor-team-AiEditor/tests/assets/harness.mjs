import {runSide} from './shared/integration/run-side.mjs'

await runSide({side: 'target', reportPath: process.argv[2]})
