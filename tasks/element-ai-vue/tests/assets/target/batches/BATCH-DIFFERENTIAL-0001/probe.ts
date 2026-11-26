import '../../../shared/batches/BATCH-F2P-0002/dom.mjs'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { runSuite } from '../../../shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs'

const require = createRequire(import.meta.url)

const copyDirectory = (source: string, target: string) => {
  fs.mkdirSync(target, { recursive: true })
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name)
    const to = path.join(target, entry.name)
    if (entry.isDirectory()) copyDirectory(from, to)
    else if (entry.isFile()) fs.copyFileSync(from, to)
  }
}

async function main() {
  const repoRoot =
    process.env.REPO_ROOT || process.argv[2] || '/workspace/repo'
  const moduleRoot =
    process.env.NODE_MODULES_ROOT || path.join(repoRoot, 'node_modules')
  const api = await import(
    pathToFileURL(path.join(repoRoot, 'packages/utils/markdown-parse/index.ts')).href
  )
  const publicApi = await import(
    pathToFileURL(path.join(repoRoot, 'packages/utils/index.ts')).href
  )

  const makeBreakMiddleware = (enabled: boolean) => ({
    key: enabled ? 'on' : 'off',
    options: { breaks: enabled },
    plugin: (md: any, options: any) => {
      const breaks = options && 'breaks' in options ? options.breaks : enabled
      md.set({ breaks })
    },
  })

  const baseProcessor = async (extra: any[] = []) =>
    api.createBaseProcessor(extra)

  const processorWithDefaults = async (extra: any[] = []) =>
    api.createBaseProcessor([...api.defaultCustomPlugins, ...extra])

  const observeMiddleware = async () => {
    let seen: any = null
    const entry = {
      key: 'k',
      plugin: (md: any, options: any) => {
        seen = options
        md.set({ breaks: false })
      },
      options: { x: 1 },
    }
    api.createBaseProcessor([entry])
    return {
      keyRetained: entry.key === 'k',
      optionsRetained: entry.options?.x === 1,
      optionsDelivered: seen?.x === 1,
    }
  }

  const typecheck = async (
    snippets: Array<{ name: string; expect: string; code: string }>
  ) => {
    const ts = require('typescript')
    const output: Record<string, any> = {}
    for (const snippet of snippets) {
      const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'md-diff-tgt-'))
      try {
        copyDirectory(
          path.join(repoRoot, 'packages/utils/markdown-parse'),
          path.join(workdir, 'packages/utils/markdown-parse')
        )
        copyDirectory(
          path.join(repoRoot, 'packages/constants'),
          path.join(workdir, 'packages/constants')
        )
        try {
          fs.symlinkSync(moduleRoot, path.join(workdir, 'node_modules'), 'dir')
        } catch {
          // A pre-existing node_modules link is usable as-is.
        }
        const file = path.join(workdir, 'probe.ts')
        fs.writeFileSync(file, snippet.code)
        const options = {
          target: ts.ScriptTarget.ES2020,
          module: ts.ModuleKind.ESNext,
          moduleResolution: ts.ModuleResolutionKind.Node10,
          strict: true,
          noEmit: true,
          skipLibCheck: true,
          esModuleInterop: true,
          allowSyntheticDefaultImports: true,
          types: [],
          baseUrl: workdir,
          paths: { '@element-ai-vue/*': ['packages/*'] },
        }
        const program = ts.createProgram([file], options)
        const diagnostics = ts
          .getPreEmitDiagnostics(program)
          .filter((diagnostic: any) => {
            if (!diagnostic.file) return true
            return path.resolve(diagnostic.file.fileName) === path.resolve(file)
          })
          .map((diagnostic: any) =>
            ts.flattenDiagnosticMessageText(diagnostic.messageText, ' ')
          )
        output[snippet.name] = { ok: diagnostics.length === 0, diagnostics }
      } finally {
        fs.rmSync(workdir, { recursive: true, force: true })
      }
    }
    return output
  }

  const sourceText = async (relativePath: string) =>
    fs.readFileSync(path.join(repoRoot, relativePath), 'utf8')

  const adapter = {
    api,
    publicApi,
    baseProcessor,
    processorWithDefaults,
    render: (processor: any, input: string) =>
      api.processMarkdownToHtml(input, processor),
    parts: (processor: any, input: string) =>
      api.processMarkdownToParts(input, processor),
    makeBreakMiddleware,
    observeMiddleware,
    typecheck,
    sourceText,
    side: 'target',
  }

  const report = await runSuite(adapter)
  report.repo_root = repoRoot
  const text = JSON.stringify(report, null, 2)
  if (process.env.REPORT_PATH) {
    fs.writeFileSync(process.env.REPORT_PATH, `${text}\n`)
  }
  process.stdout.write(`${text}\n`)
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`)
  process.exitCode = 1
})
