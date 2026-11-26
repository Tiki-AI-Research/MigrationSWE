import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const [batchId, side = 'target', repoArg] = process.argv.slice(2)
const repo = path.resolve(repoArg || process.env.REPO_ROOT || '/workspace/repo')
const checksFile = path.join(HERE, '..', 'batches', batchId, 'checks.mjs')
const entryFile = path.join(repo, 'packages/utils/markdown-parse/index.ts')

const load = async (file) => import(pathToFileURL(file).href)

const describeError = (error) =>
  String((error && error.message) || error).slice(0, 500)

const setupDom = async () => {
  if (globalThis.window && globalThis.document) return
  try {
    const { JSDOM } = await import('jsdom')
    const dom = new JSDOM('<!doctype html><html><body></body></html>', {
      url: 'http://localhost/',
    })
    globalThis.window = dom.window
    globalThis.document = dom.window.document
    globalThis.Node = dom.window.Node
    globalThis.Element = dom.window.Element
    globalThis.HTMLElement = dom.window.HTMLElement
    globalThis.NodeList = dom.window.NodeList
    globalThis.DOMParser = dom.window.DOMParser
  } catch {
    globalThis.DOMPurify = undefined
  }
}

const results = {}

await setupDom()

try {
  if (!fs.existsSync(checksFile)) throw new Error(`missing checks module: ${checksFile}`)
  const checksModule = await load(checksFile)
  const api = await load(entryFile)

  if (Array.isArray(checksModule.checks)) {
    for (const check of checksModule.checks) {
      try {
        const passed = !!(await check.run(api))
        results[check.id] = { status: passed ? 'PASS' : 'FAIL', value: { passed } }
      } catch (error) {
        results[check.id] = {
          status: 'FAIL',
          value: { passed: false, error: describeError(error) },
        }
      }
    }
  } else if (typeof checksModule.runAll === 'function') {
    const batchResults = await checksModule.runAll(api)
    for (const result of batchResults) {
      const status = result.status || (result.passed ? 'PASS' : 'FAIL')
      results[result.observation_id] = {
        status,
        value: {
          passed: status === 'PASS',
          ...(result.checks ? { checks: result.checks } : {}),
          ...(result.error ? { error: result.error } : {}),
        },
      }
    }
  } else {
    throw new Error('checks module exposes neither checks nor runAll')
  }
} catch (error) {
  const message = describeError(error)
  for (const id of (process.env.F2P_IDS || '').split(',').filter(Boolean)) {
    results[id] = { status: 'INFRA_ERROR', value: { passed: false, error: message } }
  }
}

console.log(JSON.stringify({ batch_id: batchId, side, results }, null, 2))
