'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { createRequire } = require('node:module')
const { htmlCellAlignments } = require('../table-alignment.cjs')

const OBSERVATION_IDS = [
  'P2P-001', 'P2P-002', 'P2P-003', 'P2P-004', 'P2P-005',
  'P2P-011', 'P2P-012', 'P2P-013', 'P2P-014', 'P2P-016',
  'P2P-017', 'P2P-018', 'P2P-019', 'P2P-022', 'P2P-024',
  'P2P-006', 'P2P-007', 'P2P-008', 'P2P-009', 'P2P-010',
  'P2P-015', 'P2P-020', 'P2P-021', 'P2P-023',
]

const EXPORT_CASES = {
  'P2P-009': {
    key: './lib',
    expected: { types: './lib/index.d.ts', require: './lib/index.js' },
  },
  'P2P-010': {
    key: './*',
    expected: './*',
  },
  'P2P-015': {
    key: '.',
    expected: {
      types: './es/index.d.ts',
      import: './es/index.mjs',
      require: './lib/index.js',
    },
  },
  'P2P-021': {
    key: './es/*',
    expected: {
      types: ['./es/*.d.ts', './es/*/index.d.ts'],
      import: './es/*.mjs',
    },
  },
  'P2P-023': {
    key: './lib/*',
    expected: {
      types: ['./lib/*.d.ts', './lib/*/index.d.ts'],
      require: './lib/*.js',
    },
  },
}

function repositoryRequire(root) {
  return createRequire(path.join(root, 'package.json'))
}

function registerTypeScriptLoader(root, repoRequire) {
  if (require.extensions && require.extensions['.ts']) return
  const candidates = [
    () => repoRequire('tsx/cjs/api'),
    () => require('tsx/cjs/api'),
  ]
  for (const load of candidates) {
    try {
      const api = load()
      if (api && typeof api.register === 'function') {
        api.register()
        return
      }
    } catch {
      // Try the next resolution strategy.
    }
  }
  throw new Error('unable to register the tsx CommonJS TypeScript loader')
}

function loadDependency(repoRequire, name) {
  try {
    return repoRequire(name)
  } catch {
    return require(name)
  }
}

function installDom(repoRequire) {
  if (globalThis.window && globalThis.document) return
  const { JSDOM } = loadDependency(repoRequire, 'jsdom')
  const dom = new JSDOM('<!doctype html><html><body></body></html>')
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  globalThis.Node = dom.window.Node
  globalThis.NodeList = dom.window.NodeList
  globalThis.DOMParser = dom.window.DOMParser
  globalThis.Element = dom.window.Element
  globalThis.HTMLElement = dom.window.HTMLElement
}

function normalizeOptions(value) {
  if (value === undefined) return null
  return JSON.parse(JSON.stringify(value))
}

function tagAttributes(html, tagName) {
  const pattern = new RegExp(`<${tagName}\\b[^>]*>`, 'i')
  const match = pattern.exec(html)
  if (!match) return null
  const attrs = {}
  const attrPattern = /([:\w-]+)(?:="([^"]*)")?/g
  let part
  while ((part = attrPattern.exec(match[0])) !== null) {
    if (part[1] === tagName || part[1] === 'input') continue
    attrs[part[1]] = part[2] === undefined ? '' : part[2]
  }
  return attrs
}

function readText(root, relativePath) {
  return fs.readFileSync(path.join(root, relativePath), 'utf8')
}

function readJson(root, relativePath) {
  return JSON.parse(readText(root, relativePath))
}

function exportedTypeNames(source) {
  const names = new Set()
  const declarationPattern =
    /export\s+(?:interface|type|class|enum)\s+([A-Za-z_$][\w$]*)/g
  let match
  while ((match = declarationPattern.exec(source)) !== null) {
    names.add(match[1])
  }
  return names
}

async function run(root) {
  const repoRequire = repositoryRequire(root)
  registerTypeScriptLoader(root, repoRequire)
  installDom(repoRequire)

  const markdownPath = path.join(
    root,
    'packages/utils/markdown-parse/index.ts'
  )
  const markdown = repoRequire(markdownPath)
  const markdownSource = readText(
    root,
    'packages/utils/markdown-parse/index.ts'
  )
  const utilsIndexSource = readText(root, 'packages/utils/index.ts')
  const packageJson = readJson(root, 'packages/element-ai-vue/package.json')
  const typewriter = repoRequire(
    path.join(root, 'packages/utils/typewriter.ts')
  )
  const katexConfig = repoRequire(
    path.join(root, 'packages/constants/markdown.ts')
  ).katexConfig
  const { renderToString } = loadDependency(
    repoRequire,
    'vue/server-renderer'
  )

  const defaultProcessor = markdown.createBaseProcessor(
    markdown.defaultCustomPlugins
  )
  const html = async (content, processor = defaultProcessor) =>
    markdown.processMarkdownToHtml(content, processor)
  const parts = async (content, processor = defaultProcessor) =>
    markdown.processMarkdownToParts(content, processor)

  const results = {}

  results['P2P-001'] = await (async () => {
    const invocationLog = []
    const middlewareProbe = function (first, second) {
      invocationLog.push({
        targetStyle: !!(first && typeof first.use === 'function'),
        options: normalizeOptions(
          first && typeof first.use === 'function' ? second : first
        ),
      })
      return (tree) => tree
    }
    const processor = markdown.createBaseProcessor([
      ...markdown.defaultCustomPlugins,
      { key: 'base-rules-probe', plugin: middlewareProbe },
    ])
    const breaks = await html('a\nb', processor)
    const table = await html('| a |\n| - |\n| b |', processor)
    const task = await html('- [x] a', processor)
    const strike = await html('~~x~~', processor)
    const singleTilde = await html('~x~', processor)
    const checkbox = tagAttributes(task, 'input')
    return {
      breaks: /<br\s*\/?>/i.test(breaks),
      table: /<table\b/i.test(table) && /<td\b/i.test(table),
      task:
        !!checkbox &&
        checkbox.type === 'checkbox' &&
        Object.prototype.hasOwnProperty.call(checkbox, 'disabled') &&
        Object.prototype.hasOwnProperty.call(checkbox, 'checked'),
      strike: /<del>x<\/del>/i.test(strike),
      singleTilde: !/<del>x<\/del>/i.test(singleTilde),
      middlewareRunsAfterBaseRules: invocationLog.length === 1,
    }
  })()

  results['P2P-002'] = await (async () => {
    const invocationLog = []
    const makePlugin = (key) =>
      function (first, second) {
        const targetStyle = !!(first && typeof first.use === 'function')
        invocationLog.push({
          key,
          options: normalizeOptions(targetStyle ? second : first),
        })
        return (tree) => tree
      }
    const processor = markdown.createBaseProcessor([
      { key: 'off', plugin: makePlugin('off') },
      {
        key: 'on',
        plugin: makePlugin('on'),
        options: { breaks: true },
      },
    ])
    processor.parse('', {})
    return {
      order: invocationLog.map((entry) => entry.key),
      options: invocationLog.map((entry) => entry.options),
    }
  })()

  results['P2P-003'] = await (async () => {
    const output = await html('~~text~~')
    return {
      del: /<del>text<\/del>/i.test(output),
      sTag: /<s>text<\/s>/i.test(output),
    }
  })()

  results['P2P-004'] = {
    exported: /export\s+const\s+createBaseProcessor\b/.test(markdownSource),
    processorTypeAlias:
      /export\s+type\s+ProcessorType\s*=\s*ReturnType<\s*typeof\s+createBaseProcessor\s*>/.test(
        markdownSource
      ),
  }

  results['P2P-005'] = (() => {
    const instance = typewriter.createTypewriter()
    instance.setConfig({ typingSpeed: 10, interval: 200 })
    const info = instance.getInfo()
    return {
      typingSpeed: info.config.typingSpeed,
      interval: info.config.interval,
    }
  })()

  results['P2P-011'] = await (async () => {
    const output = await html('[docs](https://example.com "Title")')
    const anchor = tagAttributes(output, 'a')
    return {
      href: anchor && anchor.href === 'https://example.com',
      title: anchor && anchor.title === 'Title',
      targetBlank: anchor && anchor.target === '_blank',
    }
  })()

  results['P2P-012'] = await (async () => {
    const output = await html('~text~')
    return { del: /<del>text<\/del>/i.test(output) }
  })()

  results['P2P-013'] = await (async () => {
    const output = await html('| H1 | H2 |\n| :-- | --: |\n| a | b |')
    const headers = htmlCellAlignments(output, 'th')
    const cells = htmlCellAlignments(output, 'td')
    return {
      thead: /<thead\b/i.test(output) && /\bH1\b/.test(output) && /\bH2\b/.test(output),
      tbody: /<tbody\b/i.test(output) && />\s*a\s*</i.test(output) && />\s*b\s*</i.test(output),
      left: headers[0] === 'left' && cells[0] === 'left',
      right: headers[1] === 'right' && cells[1] === 'right',
    }
  })()

  results['P2P-014'] = await (async () => {
    const output = await html('==text==')
    return { mark: /<mark>text<\/mark>/i.test(output) }
  })()

  results['P2P-016'] = {
    keys: markdown.defaultCustomPlugins.map((entry) => entry.key),
    entriesShaped: markdown.defaultCustomPlugins.every(
      (entry) =>
        entry &&
        typeof entry === 'object' &&
        typeof entry.key === 'string' &&
        entry.plugin !== undefined
    ),
  }

  results['P2P-017'] = await (async () => {
    const inline = await html('$x$')
    const block = await html('$$\nx\n$$')
    return {
      inlineKatex: /class="katex"/i.test(inline),
      blockKatexDisplay: /class="katex-display"/i.test(block),
    }
  })()

  results['P2P-018'] = await (async () => {
    const math = await html('$x$')
    const link = await html('[a](https://e)')
    const mark = await html('==x==')
    const subSuper = await html('~x~ ^x^')
    const sub = subSuper
    const sup = subSuper
    const abbr = await html('*[HTML]: HyperText Markup Language\n\nHTML')
    const raw = await html('<b>x</b>')
    const blockMath = await html('$$\nx\n$$')
    const unclosedParts = await parts('```js\nx')
    return {
      math: /class="katex"/i.test(math),
      linkTarget: /target="_blank"/i.test(link),
      mark: /<mark>x<\/mark>/i.test(mark),
      sub: /<sub>x<\/sub>/i.test(sub),
      sup: /<sup>x<\/sup>/i.test(sup),
      abbr: /<abbr title="HyperText Markup Language">HTML<\/abbr>/.test(abbr),
      unclosedCodePart:
        unclosedParts.some(
          (part) => part.type === 'code' && part.loading === true
        ),
      rawHtml: /<b>x<\/b>/.test(raw),
      blockMath: /class="katex-display"/i.test(blockMath),
    }
  })()

  results['P2P-019'] = await (async () => {
    const result = await parts('<div>x</div>')
    const htmlParts = result.filter((part) => part.type === 'html')
    const rendered =
      htmlParts.length === 1 ? await renderToString(htmlParts[0].content) : ''
    return {
      partType: result.length === 1 ? result[0].type : null,
      rawHtml: /<div>x<\/div>/.test(rendered),
      escaped: /&lt;div&gt;x&lt;\/div&gt;/.test(rendered),
    }
  })()

  results['P2P-022'] = await (async () => {
    const output = await html('*[ABBR]: definition\n\nABBR')
    return {
      abbr: /<abbr title="definition">ABBR<\/abbr>/.test(output),
      definitionConsumed: !/\*\[ABBR\]:\s*definition/.test(output),
    }
  })()

  results['P2P-024'] = await (async () => {
    const output = await html('$\\RR$')
    const entry = markdown.defaultCustomPlugins.find(
      (item) => item.key === 'rehype-katex'
    )
    return {
      macroRendered: /class="mord mathbb"/i.test(output),
      katexConfig:
        !!entry &&
        JSON.stringify(entry.options) === JSON.stringify(katexConfig) &&
        entry.options.throwOnError === false &&
        entry.options.errorColor === '#cc0000' &&
        entry.options.strict === false,
    }
  })()

  results['P2P-006'] = await (async () => {
    const result = await parts('```js\nconst a=1\n```')
    const codePart = result.find((part) => part.type === 'code')
    return {
      codePart: codePart
        ? {
            type: codePart.type,
            language: codePart.language,
            loading: codePart.loading,
            contentIsString: typeof codePart.content === 'string',
          }
        : null,
      typeUnion:
        /type\s*:\s*['"]html['"]\s*\|\s*['"]code['"]/.test(markdownSource),
      optionalFields:
        /language\?\s*:\s*string/.test(markdownSource) &&
        /loading\?\s*:\s*boolean/.test(markdownSource),
      rejectsTextType:
        !/type\s*:\s*['"]html['"]\s*\|\s*['"]code['"]\s*\|\s*['"]text['"]/.test(
          markdownSource
        ),
    }
  })()

  results['P2P-007'] = await (async () => {
    const result = await parts(
      'before\n\n```js\na\n```\n\nmiddle\n\n```js\nb\n```\n\nafter'
    )
    const codeParts = result.filter((part) => part.type === 'code')
    return {
      codeCount: codeParts.length,
      contents: codeParts.map((part) => String(part.content).trim()),
    }
  })()

  results['P2P-008'] = (() => {
    const names = exportedTypeNames(markdownSource)
    return {
      names: [
        'MiddlewarePluginItem',
        'MarkdownPart',
        'ProcessorType',
        'ProcessMarkdownOptions',
      ].filter((name) => names.has(name)),
      utilsReexport:
        /export\s+\*\s+from\s+['"]\.\/markdown-parse['"]/.test(
          utilsIndexSource
        ),
    }
  })()

  for (const [observationId, spec] of Object.entries(EXPORT_CASES)) {
    const actual = packageJson.exports && packageJson.exports[spec.key]
    results[observationId] = {
      key: spec.key,
      present: actual !== undefined,
      matches:
        actual !== undefined &&
        (typeof spec.expected === 'string'
          ? actual === spec.expected
          : Object.entries(spec.expected).every(([field, value]) =>
              Array.isArray(value)
                ? JSON.stringify(actual[field]) === JSON.stringify(value)
                : actual[field] === value
            )),
    }
  }

  results['P2P-020'] = await (async () => {
    const closed = await parts('```js\nx\n```')
    const unclosed = await parts('```js\nx')
    const closedPart = closed.find((part) => part.type === 'code')
    const unclosedPart = unclosed.find((part) => part.type === 'code')
    return {
      closedLoading: closedPart ? closedPart.loading : null,
      unclosedLoading: unclosedPart ? unclosedPart.loading : null,
    }
  })()

  const ordered = {}
  for (const observationId of OBSERVATION_IDS) {
    if (!(observationId in results)) {
      throw new Error(`missing probe result for ${observationId}`)
    }
    ordered[observationId] = results[observationId]
  }
  return ordered
}

module.exports = { OBSERVATION_IDS, run }

if (require.main === module) {
  const root = path.resolve(process.argv[2] || process.cwd())
  run(root)
    .then((results) => {
      process.stdout.write(
        JSON.stringify({ root, results, result: 'PASS' }, null, 2) + '\n'
      )
    })
    .catch((error) => {
      process.stderr.write(
        JSON.stringify(
          {
            root,
            result: 'FAIL',
            error: error && error.stack ? error.stack : String(error),
          },
          null,
          2
        ) + '\n'
      )
      process.exitCode = 1
    })
}
