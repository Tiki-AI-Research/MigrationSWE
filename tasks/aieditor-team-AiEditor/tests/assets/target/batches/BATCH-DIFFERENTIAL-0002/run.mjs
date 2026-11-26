import {createRequire} from 'node:module'
import {pathToFileURL, fileURLToPath} from 'node:url'
import {dirname, join, resolve} from 'node:path'

const SIDE = 'target'
const repoRoot = resolve(process.argv[2] ?? process.env.REPO_ROOT ?? '/workspace/repo')
const here = dirname(fileURLToPath(import.meta.url))
const assetRoot = resolve(here, '../../..')
const fixtureRoot = join(assetRoot, 'fixtures/batches/BATCH-DIFFERENTIAL-0002')
const requireFromRepo = createRequire(join(repoRoot, 'package.json'))
const {createServer} = await import(pathToFileURL(requireFromRepo.resolve('vite')).href)
const {runCorpus} = await import(pathToFileURL(join(assetRoot, 'shared/batches/BATCH-DIFFERENTIAL-0002/cases.mjs')).href)

const server = await createServer({
  root: repoRoot,
  configFile: false,
  server: {middlewareMode: true, watch: null},
  appType: 'custom',
  logLevel: 'error',
})

try {
  const ai = await server.ssrLoadModule('/src/ai/index.ts')
  const pm = await server.ssrLoadModule('/node_modules/@tiptap/pm/transform/index.ts')
  const result = await runCorpus({ai, StepMap: pm.StepMap, repoRoot, fixtureRoot})
  process.stdout.write(JSON.stringify({side: SIDE, repo_root: repoRoot, ...result}, null, 2) + '\n')
  process.exitCode = result.allPass ? 0 : 1
} finally {
  await server.close()
}
