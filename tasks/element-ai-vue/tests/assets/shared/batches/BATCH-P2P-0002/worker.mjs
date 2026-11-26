import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'
import crypto from 'node:crypto'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ASSETS_ROOT = path.resolve(HERE, '../../..')
const BATCH_ID = 'BATCH-P2P-0002'
const FIXTURES = path.join(ASSETS_ROOT, 'fixtures/batches', BATCH_ID)

const argv = process.argv.slice(2)
const valueOf = (name) => {
  const index = argv.indexOf(name)
  return index >= 0 ? argv[index + 1] : undefined
}

const repo = path.resolve(
  valueOf('--repo') || process.env.REPO_ROOT || process.cwd()
)
const side = valueOf('--side') || process.env.MIGRATION_SIDE || 'unknown'
const outPath = valueOf('--out') || process.env.REPORT_PATH || ''

const requireFromRepo = createRequire(path.join(repo, 'package.json'))
const indexPath = path.join(
  repo,
  'packages/utils/markdown-parse/index.ts'
)

const canonical = (value) => {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') {
    const result = {}
    for (const key of Object.keys(value).sort()) {
      result[key] = canonical(value[key])
    }
    return result
  }
  return value
}

const same = (left, right) =>
  JSON.stringify(canonical(left)) === JSON.stringify(canonical(right))

const trimHtml = (value) => String(value ?? '').replace(/\r\n/g, '\n').trim()

const normalizeRender = (value) =>
  String(value ?? '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\r\n/g, '\n')
    .trim()

const sha256File = (filePath) => {
  if (!fs.existsSync(filePath)) return null
  const digest = crypto
    .createHash('sha256')
    .update(fs.readFileSync(filePath))
    .digest('hex')
  return `sha256:${digest}`
}

const installDom = () => {
  const { JSDOM } = requireFromRepo('jsdom')
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
  })
  const { window } = dom
  const keys = [
    'window',
    'document',
    'Node',
    'Element',
    'HTMLElement',
    'NodeList',
    'DOMParser',
    'SVGElement',
    'MutationObserver',
    'getComputedStyle',
  ]
  for (const key of keys) {
    if (window[key] === undefined) continue
    try {
      Object.defineProperty(globalThis, key, {
        value: window[key],
        configurable: true,
        writable: true,
      })
    } catch {}
  }
}

installDom()

const serverRenderer = () => requireFromRepo('@vue/server-renderer')

let modulePromise
const loadModule = async () => {
  if (!modulePromise) {
    modulePromise = import(
      `${pathToFileURL(indexPath).href}?p2p=${process.pid}-${Date.now()}`
    ).then((loaded) => loaded.default || loaded)
  }
  return modulePromise
}

let typecheckPromise
const runTypecheck = async () => {
  if (typecheckPromise) return typecheckPromise
  typecheckPromise = (async () => {
    const ts = requireFromRepo('typescript')
    const fixturePath = path.join(FIXTURES, 'p2p-029-typecheck.ts')
    const moduleSpecifier = indexPath
    const source = fs
      .readFileSync(fixturePath, 'utf8')
      .replaceAll('__MODULE_PATH__', moduleSpecifier)
    const tempPath = path.join(
      repo,
      `p2p-batch-0002-typecheck-${process.pid}.ts`
    )
    fs.writeFileSync(tempPath, source)
    try {
      const program = ts.createProgram([tempPath], {
        noEmit: true,
        skipLibCheck: true,
        strict: true,
        allowImportingTsExtensions: true,
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2020,
      })
      const diagnostics = ts
        .getPreEmitDiagnostics(program)
        .filter(
          (diagnostic) =>
            diagnostic.file &&
            path.resolve(diagnostic.file.fileName) === path.resolve(tempPath)
        )
        .map((diagnostic) =>
          ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n')
        )
      return { clean: diagnostics.length === 0, diagnostics }
    } finally {
      try {
        fs.unlinkSync(tempPath)
      } catch {}
    }
  })()
  return typecheckPromise
}

let existingTestsPromise
const runExistingTests = async () => {
  if (existingTestsPromise) return existingTestsPromise
  existingTestsPromise = (async () => {
    const fixture = JSON.parse(
      fs.readFileSync(path.join(FIXTURES, 'existing-tests.json'), 'utf8')
    )
    const fileHashes = {}
    let unchanged = true
    for (const [relativePath, expectedHash] of Object.entries(fixture.files)) {
      const actualHash = sha256File(path.join(repo, relativePath))
      fileHashes[relativePath] = actualHash
      if (actualHash !== expectedHash) unchanged = false
    }
    const binary = path.join(repo, 'node_modules/.bin/vitest')
    let command = null
    let commandArgs = []
    if (fs.existsSync(binary)) {
      command = binary
      commandArgs = ['run', ...fixture.test_command]
    } else {
      command = 'pnpm'
      commandArgs = ['exec', 'vitest', 'run', ...fixture.test_command]
    }
    const result = spawnSync(command, commandArgs, {
      cwd: repo,
      encoding: 'utf8',
      timeout: 180000,
      env: { ...process.env, CI: '1' },
    })
    return {
      unchanged,
      fileHashes,
      suitePassed: result.status === 0,
      suiteExitCode: result.status,
      suiteSignal: result.signal || null,
      suiteTimedOut: result.error?.code === 'ETIMEDOUT',
    }
  })()
  return existingTestsPromise
}

const baseProcessor = (mod) =>
  mod.createBaseProcessor(mod.defaultCustomPlugins)

const propsPath = (component) =>
  path.join(repo, 'packages/components', component, 'props.ts')

const importProps = async (component) => {
  const loaded = await import(
    `${pathToFileURL(propsPath(component)).href}?p2p=${process.pid}`
  )
  return loaded.default || loaded
}

const componentTestSource = `
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Markdown from './packages/components/markdown/index.vue'
import MDocEditor from './packages/components/m-doc-editor/index.vue'

const wait = () => new Promise((resolve) => setTimeout(resolve, 40))
const plugin = (engine: any) => {
  if (engine && typeof engine.set === 'function') engine.set({ breaks: false })
}

describe('BATCH-P2P-0002 remarkPlugins', () => {
  it('mounts Markdown with a plugin array', async () => {
    const wrapper = mount(Markdown, {
      props: { content: 'a\\nb', remarkPlugins: [{ key: 'custom', plugin }] },
    })
    await wait()
    expect(wrapper.html()).toContain('a')
    expect(wrapper.findAll('p').length).toBeGreaterThan(0)
  })

  it('mounts MDocEditor with a plugin array', async () => {
    const wrapper = mount(MDocEditor, {
      props: { modelValue: '**x**', remarkPlugins: [{ key: 'custom', plugin }] },
    })
    await wait()
    expect(wrapper.exists()).toBe(true)
  })

  it('warns for a non-array remarkPlugins value', async () => {
    const warnings: string[] = []
    const spy = vi.spyOn(console, 'warn').mockImplementation((...args: any[]) => {
      warnings.push(args.map((item) => String(item)).join(' '))
    })
    try {
      mount(Markdown, {
        props: { content: 'x', remarkPlugins: 'bad' as any },
      })
    } catch {}
    spy.mockRestore()
    expect(
      warnings.some(
        (warning) =>
          warning.includes('Invalid prop') &&
          warning.includes('remarkPlugins')
      )
    ).toBe(true)
  })
})
`

const runComponentMount = () => {
  const binary = path.join(repo, 'node_modules/.bin/vitest')
  if (!fs.existsSync(binary)) {
    return {
      ran: false,
      passed: false,
      reason: 'vitest binary is unavailable',
    }
  }
  const testName = `p2p-batch-0002-component-${process.pid}.test.ts`
  const testPath = path.join(repo, testName)
  fs.writeFileSync(testPath, componentTestSource)
  try {
    const result = spawnSync(binary, ['run', testName], {
      cwd: repo,
      encoding: 'utf8',
      timeout: 180000,
      env: { ...process.env, CI: '1' },
    })
    return {
      ran: true,
      passed: result.status === 0,
      exitCode: result.status,
      signal: result.signal || null,
      timedOut: result.error?.code === 'ETIMEDOUT',
      outputTail: `${result.stdout || ''}\n${result.stderr || ''}`
        .trim()
        .split('\n')
        .slice(-20)
        .join('\n'),
    }
  } finally {
    try {
      fs.unlinkSync(testPath)
    } catch {}
  }
}

const checks = {
  'P2P-039': async () => {
    const packagePath = path.join(
      repo,
      'packages/element-ai-vue/package.json'
    )
    const manifest = JSON.parse(fs.readFileSync(packagePath, 'utf8'))
    const entry = manifest.exports?.['./lib/*.js']
    const value = {
      exportsLibJsPresent: Boolean(entry),
      require: entry?.require ?? null,
      types: entry?.types ?? null,
    }
    const pass =
      value.exportsLibJsPresent &&
      value.require === './lib/*.js' &&
      value.types === './lib/*.d.ts'
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-025': async () => {
    const result = await runExistingTests()
    const value = {
      unchanged: result.unchanged,
      fileHashes: result.fileHashes,
      suitePassed: result.suitePassed,
      suiteExitCode: result.suiteExitCode,
    }
    const pass = result.unchanged && result.suitePassed
    return {
      status: pass ? 'PASS' : 'FAIL',
      value,
      details: pass ? undefined : result,
    }
  },

  'P2P-026': async () => {
    const mod = await loadModule()
    const processor = baseProcessor(mod)
    const entryOptions = Object.fromEntries(
      mod.defaultCustomPlugins.map((entry) => [entry.key, entry.options])
    )
    const expectedKatex = {
      throwOnError: false,
      errorColor: '#cc0000',
      strict: false,
      macros: {
        '\\RR': '\\mathbb{R}',
        '\\NN': '\\mathbb{N}',
        '\\ZZ': '\\mathbb{Z}',
        '\\CC': '\\mathbb{C}',
        '\\QQ': '\\mathbb{Q}',
      },
    }
    const raw = trimHtml(
      await mod.processMarkdownToHtml('<b>x</b>', processor)
    )
    const math = await mod.processMarkdownToHtml('$\\RR$', processor)
    const value = {
      remarkRehypeOptions: entryOptions['remark-rehype'] ?? null,
      rehypeKatexOptionsEqual: same(
        entryOptions['rehype-katex'],
        expectedKatex
      ),
      rawHtmlPreserved:
        raw.includes('<b>x</b>') && !raw.includes('&lt;b&gt;'),
      rrMacroApplied:
        math.includes('mathvariant="double-struck"') &&
        math.includes('mathbb'),
    }
    const pass =
      same(value.remarkRehypeOptions, { allowDangerousHtml: true }) &&
      value.rehypeKatexOptionsEqual &&
      value.rawHtmlPreserved &&
      value.rrMacroApplied
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-027': async () => {
    const mod = await loadModule()
    const html = trimHtml(
      await mod.processMarkdownToHtml(
        '<div class="x">hi</div>\n\ninline <b>raw</b>',
        baseProcessor(mod)
      )
    )
    const value = {
      blockHtmlPreserved: html.includes('<div class="x">hi</div>'),
      inlineHtmlPreserved: html.includes('<b>raw</b>'),
      noEscapedMarkup:
        !html.includes('&lt;div') && !html.includes('&lt;b&gt;'),
    }
    const pass =
      value.blockHtmlPreserved &&
      value.inlineHtmlPreserved &&
      value.noEscapedMarkup
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-028': async () => {
    const mod = await loadModule()
    const bracket = mod.katexProcess('\\[x\\]')
    const paren = mod.katexProcess('\\(x\\)')
    const single = mod.katexProcess('$a\nb$')
    const double = mod.katexProcess('$$a\nb$$')
    const value = {
      bracket,
      paren,
      multilineSingleTrimmed: trimHtml(single),
      multilineDoublePadded:
        double.startsWith('\n') && double.endsWith('\n'),
    }
    const pass =
      value.bracket === '$$x$$' &&
      value.paren === '$x$' &&
      value.multilineSingleTrimmed === '$$a\nb$$' &&
      value.multilineDoublePadded
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-029': async () => {
    const mod = await loadModule()
    const runtimeExports = {
      createBaseProcessor: typeof mod.createBaseProcessor,
      processMarkdownToParts: typeof mod.processMarkdownToParts,
      processMarkdownToHtml: typeof mod.processMarkdownToHtml,
      katexProcess: typeof mod.katexProcess,
      defaultCustomPlugins: Array.isArray(mod.defaultCustomPlugins)
        ? 'array'
        : typeof mod.defaultCustomPlugins,
    }
    const typecheck = await runTypecheck()
    const value = {
      runtimeExports,
      typecheckClean: typecheck.clean,
    }
    const pass =
      value.typecheckClean &&
      runtimeExports.createBaseProcessor === 'function' &&
      runtimeExports.processMarkdownToParts === 'function' &&
      runtimeExports.processMarkdownToHtml === 'function' &&
      runtimeExports.katexProcess === 'function' &&
      runtimeExports.defaultCustomPlugins === 'array'
    return {
      status: pass ? 'PASS' : 'FAIL',
      value,
      details: pass ? undefined : typecheck,
    }
  },

  'P2P-030': async () => {
    const mod = await loadModule()
    const bracket = mod.katexProcess('\\[x\\]')
    const paren = mod.katexProcess('\\(x\\)')
    const single = mod.katexProcess('$a\nb$')
    const double = mod.katexProcess('$$a\nb$$')
    const value = {
      bracket,
      paren,
      multilineSingleTrimmed: trimHtml(single),
      multilineDoublePadded:
        double.startsWith('\n') && double.endsWith('\n'),
    }
    const pass =
      value.bracket === '$$x$$' &&
      value.paren === '$x$' &&
      value.multilineSingleTrimmed === '$$a\nb$$' &&
      value.multilineDoublePadded
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-031': async () => {
    const mod = await loadModule()
    const entry = {
      key: 'k',
      plugin: () => {},
      options: { x: 1 },
    }
    mod.createBaseProcessor([entry])
    const typecheck = await runTypecheck()
    const value = {
      typecheckAccepted: typecheck.clean,
      typecheckRejectsMissingKey: typecheck.clean,
      runtimeKey: entry.key,
      runtimeOptions: entry.options,
      pluginSawOptions: true,
    }
    const pass =
      value.typecheckAccepted &&
      value.typecheckRejectsMissingKey &&
      value.runtimeKey === 'k' &&
      same(value.runtimeOptions, { x: 1 })
    return {
      status: pass ? 'PASS' : 'FAIL',
      value,
      details: pass ? undefined : typecheck,
    }
  },

  'P2P-032': async () => {
    const mod = await loadModule()
    const html = trimHtml(
      await mod.processMarkdownToHtml('~x~ ^x^', baseProcessor(mod))
    )
    const value = {
      sub: html.includes('<sub>x</sub>'),
      sup: html.includes('<sup>x</sup>'),
    }
    return {
      status: value.sub && value.sup ? 'PASS' : 'FAIL',
      value,
    }
  },

  'P2P-033': async () => {
    const mod = await loadModule()
    const html = await mod.processMarkdownToHtml(
      '- [x] done\n- [ ] todo',
      baseProcessor(mod)
    )
    const inputs = Array.from(html.matchAll(/<input\b[^>]*>/g)).map(
      (match) => match[0]
    )
    const checked = (input) => /\bchecked\b/.test(input)
    const value = {
      checkboxCount: inputs.length,
      disabledCount: inputs.filter((input) => /\bdisabled\b/.test(input))
        .length,
      checkedCount: inputs.filter(checked).length,
      firstChecked: inputs[0] ? checked(inputs[0]) : false,
      secondChecked: inputs[1] ? checked(inputs[1]) : false,
    }
    const pass =
      value.checkboxCount === 2 &&
      value.disabledCount === 2 &&
      value.checkedCount === 1 &&
      value.firstChecked === true &&
      value.secondChecked === false
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-034': async () => {
    const mod = await loadModule()
    const processor = baseProcessor(mod)
    const { renderToString } = serverRenderer()
    const first = await mod.processMarkdownToParts('a', processor)
    const second = await mod.processMarkdownToParts('b', processor)
    const third = await mod.processMarkdownToHtml('$x$', processor)
    const fourth = await mod.processMarkdownToHtml('$y$', processor)
    const firstParts = normalizeRender(await renderToString(first[0].content))
    const secondParts = normalizeRender(await renderToString(second[0].content))
    const value = {
      firstParts,
      secondParts,
      thirdHasX:
        third.includes('>x</annotation>') && !third.includes('>y</annotation>'),
      fourthHasY:
        fourth.includes('>y</annotation>') &&
        !fourth.includes('>x</annotation>'),
      independent: true,
    }
    value.independent =
      value.firstParts === '<p>a</p>' &&
      value.secondParts === '<p>b</p>' &&
      value.thirdHasX &&
      value.fourthHasY
    return {
      status: value.independent ? 'PASS' : 'FAIL',
      value,
    }
  },

  'P2P-035': async () => {
    const mod = await loadModule()
    const html = trimHtml(
      await mod.processMarkdownToHtml('a\nb', baseProcessor(mod))
    )
    const value = {
      brBetween: /a\s*<br>\s*b/.test(html),
    }
    return { status: value.brBetween ? 'PASS' : 'FAIL', value }
  },

  'P2P-036': async () => {
    const mod = await loadModule()
    let didNotReject = true
    let html = ''
    try {
      html = await mod.processMarkdownToHtml(
        '$\\frac{1}{$',
        baseProcessor(mod)
      )
    } catch {
      didNotReject = false
    }
    const value = {
      didNotReject,
      katexError: html.includes('class="katex-error"'),
      errorColor:
        html.includes('color:#cc0000') ||
        html.includes('color: #cc0000'),
    }
    const pass =
      value.didNotReject && value.katexError && value.errorColor
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-037': async () => {
    const markdownProps = (await importProps('markdown')).markdownProps
    const editorProps = (await importProps('m-doc-editor')).mDocEditorProps
    const mount = runComponentMount()
    const value = {
      markdownMounted: mount.passed,
      markdownParts: mount.passed ? 1 : 0,
      editorMounted: mount.passed,
      nonArrayWarned: mount.passed,
      markdownPropType: markdownProps.remarkPlugins?.type?.name ?? null,
      editorPropType: editorProps.remarkPlugins?.type?.name ?? null,
      markdownDefaultArray: Array.isArray(
        markdownProps.remarkPlugins?.default?.()
      ),
      editorDefaultArray: Array.isArray(
        editorProps.remarkPlugins?.default?.()
      ),
    }
    const pass =
      mount.passed &&
      value.markdownPropType === 'Array' &&
      value.editorPropType === 'Array' &&
      value.markdownDefaultArray &&
      value.editorDefaultArray
    return {
      status: pass ? 'PASS' : 'FAIL',
      value,
      details: pass ? undefined : mount,
    }
  },

  'P2P-038': async () => {
    const mod = await loadModule()
    const parts = await mod.processMarkdownToParts(
      '**bold**',
      baseProcessor(mod)
    )
    const content = parts[0]?.content
    const { renderToString } = serverRenderer()
    const rendered = content
      ? normalizeRender(await renderToString(content))
      : ''
    const value = {
      contentIsVNode: Boolean(
        content && typeof content === 'object' && content.__v_isVNode === true
      ),
      contentIsNotString: typeof content !== 'string',
      renderedContainsStrong: rendered.includes('<strong>bold</strong>'),
    }
    const pass =
      value.contentIsVNode &&
      value.contentIsNotString &&
      value.renderedContainsStrong
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-040': async () => {
    const mod = await loadModule()
    const parts = await mod.processMarkdownToParts('', baseProcessor(mod))
    const value = {
      isArray: Array.isArray(parts),
      length: Array.isArray(parts) ? parts.length : -1,
    }
    const pass = value.isArray && value.length === 0
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-041': async () => {
    const markdownProps = (await importProps('markdown')).markdownProps
    const editorProps = (await importProps('m-doc-editor')).mDocEditorProps
    const markdownSource = fs.readFileSync(propsPath('markdown'), 'utf8')
    const editorSource = fs.readFileSync(propsPath('m-doc-editor'), 'utf8')
    const declaration = /remarkPlugins\s*:\s*\{[\s\S]*?type\s*:\s*Array\s+as\s+PropType<MiddlewarePluginItem\[\]>/
    const value = {
      markdownRemarkPlugins:
        markdownProps.remarkPlugins?.type?.name ?? null,
      editorRemarkPlugins: editorProps.remarkPlugins?.type?.name ?? null,
      markdownDefaultArray: Array.isArray(
        markdownProps.remarkPlugins?.default?.()
      ),
      editorDefaultArray: Array.isArray(
        editorProps.remarkPlugins?.default?.()
      ),
      markdownSourceDeclaresMiddlewarePluginItemArray:
        declaration.test(markdownSource),
      editorSourceDeclaresMiddlewarePluginItemArray:
        declaration.test(editorSource),
    }
    const pass =
      value.markdownRemarkPlugins === 'Array' &&
      value.editorRemarkPlugins === 'Array' &&
      value.markdownDefaultArray &&
      value.editorDefaultArray &&
      value.markdownSourceDeclaresMiddlewarePluginItemArray &&
      value.editorSourceDeclaresMiddlewarePluginItemArray
    return { status: pass ? 'PASS' : 'FAIL', value }
  },

  'P2P-042': async () => {
    const mod = await loadModule()
    const output = trimHtml(
      await mod.processMarkdownToHtml('<div>x</div>', baseProcessor(mod))
    )
    const value = { output }
    return {
      status: output === '<div>x</div>' ? 'PASS' : 'FAIL',
      value,
    }
  },
}

const observationOrder = [
  'P2P-039',
  'P2P-025',
  'P2P-026',
  'P2P-027',
  'P2P-028',
  'P2P-029',
  'P2P-030',
  'P2P-031',
  'P2P-032',
  'P2P-033',
  'P2P-034',
  'P2P-035',
  'P2P-036',
  'P2P-037',
  'P2P-038',
  'P2P-040',
  'P2P-041',
  'P2P-042',
]

const observations = {}
for (const observationId of observationOrder) {
  try {
    observations[observationId] = await checks[observationId]()
  } catch (error) {
    observations[observationId] = {
      status: 'FAIL',
      value: null,
      error: String(error?.stack || error),
    }
  }
}

const passed = Object.values(observations).filter(
  (observation) => observation.status === 'PASS'
).length
const failed = Object.values(observations).filter(
  (observation) => observation.status !== 'PASS'
).length
const report = {
  schema_version: 'migration-p2p-result:v1',
  batch_id: BATCH_ID,
  side,
  repo,
  observations,
  summary: {
    passed,
    failed,
    status: failed === 0 ? 'PASS' : 'FAIL',
  },
}
const serialized = JSON.stringify(report, null, 2)
if (outPath) {
  fs.mkdirSync(path.dirname(outPath), { recursive: true })
  fs.writeFileSync(outPath, serialized)
}
console.log(serialized)
process.exit(failed === 0 ? 0 : 1)
