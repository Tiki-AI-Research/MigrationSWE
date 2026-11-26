import codeHtml from '../code-html.cjs'

const { codeBlockMatches } = codeHtml

import { createRequire, syncBuiltinESMExports } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
import './dom.mjs'
import tableAlignment from '../table-alignment.cjs'

const nodeRequire = createRequire(import.meta.url)
const { matchesTableAlignment } = tableAlignment

const runtimeSourceViolations = () => {
  const root = path.resolve(process.cwd(), 'packages/utils/markdown-parse')
  if (!fs.existsSync(root)) return []
  const violations = []
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name)
      if (entry.isDirectory()) visit(file)
      else if (/\.[cm]?[jt]sx?$/.test(entry.name)) {
        const source = fs.readFileSync(file, 'utf8')
        if (/\b(?:child_process|worker_threads)\b/.test(source) ||
            /process\s*(?:\.\s*getBuiltinModule|\[['"]getBuiltinModule['"]\])/.test(source) ||
            /\bReflect\s*\.\s*(?:apply|construct)\s*\(/.test(source) ||
            /\bcreateRequire\s*\(/.test(source) ||
            /\.(?:spawn|spawnSync|fork)\s*\(/.test(source)) {
          violations.push(path.relative(root, file))
        }
      }
    }
  }
  visit(root)
  return violations.sort()
}

const MARKDOWN_TABLE = '| L | C | R |\n| :-- | :-: | --: |\n| a | b | c |'
const MARKDOWN_PLAIN_TABLE = '| a |\n| - |\n| b |'
const MARKDOWN_SIMPLE_TABLE = '| a |\n| - |\n| b |'
const MARKDOWN_TASK_LIST = '- [x] done\n- [ ] todo'
const MARKDOWN_LINK = '[label](https://example.test "title")'
const MARKDOWN_MATH_INLINE = '$\\RR$'
const MARKDOWN_RAW = '<div class="x">hi</div>\n\ninline <b>raw</b>'
const MIDDLEWARE = [
  { key: 'off', plugin: (md) => md.set({ breaks: false }) },
  {
    key: 'on',
    plugin: (md, options) => md.set({ breaks: options.breaks }),
    options: { breaks: true },
  },
]

const defaultProcessor = (mod) =>
  mod.createBaseProcessor(mod.defaultCustomPlugins)

const baseProcessor = (mod) => mod.createBaseProcessor([])

const tokenList = (tokens) => {
  const result = []
  const visit = (items) => {
    for (const item of items || []) {
      result.push(item)
      if (item && item.children) visit(item.children)
    }
  }
  visit(tokens)
  return result
}

const hasTargetProcessor = (mod) => {
  try {
    const processor = baseProcessor(mod)
    if (!processor || typeof processor.render !== 'function') return false
    const tokens = processor.parse('# h', {})
    if (!Array.isArray(tokens)) return false
    if (!tokens[0] || tokens[0].type !== 'heading_open') return false
    return /MarkdownIt/.test(String(processor.constructor?.name || ''))
  } catch {
    return false
  }
}

const render = async (mod, content, plugins) =>
  mod.processMarkdownToHtml(
    content,
    plugins ? mod.createBaseProcessor(plugins) : defaultProcessor(mod)
  )

const parts = async (mod, content, plugins) =>
  mod.processMarkdownToParts(
    content,
    plugins ? mod.createBaseProcessor(plugins) : defaultProcessor(mod)
  )

const checks = {
  async targetProcessor({ mod }) {
    return hasTargetProcessor(mod)
  },

  async middleware({ mod }) {
    const off = MIDDLEWARE[0]
    const on = MIDDLEWARE[1]
    const enabled = await render(mod, 'a\nb', [off, on])
    const reversed = await render(mod, 'a\nb', [on, off])
    return enabled.includes('<br>') && !reversed.includes('<br>')
  },

  async katexConfigured({ mod }) {
    const html = await render(mod, MARKDOWN_MATH_INLINE)
    const entry = mod.defaultCustomPlugins.find(
      (item) => item.key === 'rehype-katex'
    )
    const options = entry?.options || {}
    const hasMath = /class="[^"]*katex|mathvariant="double-struck"|ℝ/.test(
      html
    )
    const hasConfig =
      options.throwOnError === false &&
      options.errorColor === '#cc0000' &&
      options.strict === false &&
      !!options.macros &&
      options.macros['\\RR'] === '\\mathbb{R}'
    return hasMath && hasConfig
  },

  async strikethrough({ mod }) {
    const html = await render(mod, '~~x~~')
    return html.includes('<del>x</del>') && !html.includes('<s>x</s>')
  },

  async singleTilde({ mod }) {
    const html = await render(mod, '~text~')
    return !html.includes('<del>text</del>')
  },

  async tableAlignment({ mod }) {
    const html = await render(mod, MARKDOWN_TABLE)
    return matchesTableAlignment(html, ['left', 'center', 'right'])
  },

  async nestedFence({ mod }) {
    const result = await parts(mod, '> ```js\n> x\n> ```')
    return result.length === 1 && result[0].type === 'html'
  },

  async tokenShape({ mod }) {
    const processor = baseProcessor(mod)
    const heading = processor.parse('# h', {})
    const html = await mod.processMarkdownToHtml('~~x~~', processor)
    return (
      heading[0]?.type === 'heading_open' &&
      html.includes('<del>x</del>') &&
      !html.includes('<s>x</s>')
    )
  },

  async emptyParts({ mod }) {
    const result = await parts(mod, '')
    return Array.isArray(result) && result.length === 0
  },

  async codePart({ mod }) {
    const result = await parts(mod, '```js\nconst a=1\n```')
    return (
      result.length === 1 &&
      result[0].type === 'code' &&
      result[0].content === 'const a=1\n' &&
      result[0].language === 'js' &&
      result[0].loading === false
    )
  },

  async segmentation({ mod }) {
    const result = await parts(mod, 'text\n\n```js\ncode\n```\n\ntail')
    return JSON.stringify(result.map((part) => part.type)) ===
      JSON.stringify(['html', 'code', 'html'])
  },

  async pluginOrder({ mod }) {
    const expected = [
      'remark-math',
      'remark-a-target-blank',
      'remark-mark-highlight',
      'remark-sub-super',
      'remark-abbr',
      'remark-code-block-status',
      'remark-rehype',
      'rehype-katex',
    ]
    const actual = mod.defaultCustomPlugins.map((item) => item.key)
    const shaped = mod.defaultCustomPlugins.every(
      (item) => typeof item.key === 'string' && 'plugin' in item
    )
    return JSON.stringify(actual) === JSON.stringify(expected) && shaped
  },

  async htmlGrouping({ mod }) {
    const result = await parts(mod, '```js\na\n```\ntext\n```js\nb\n```')
    return JSON.stringify(result.map((part) => part.type)) ===
      JSON.stringify(['code', 'html', 'code'])
  },

  async loadingStatus({ mod }) {
    const closed = await parts(mod, '```js\nx\n```')
    const unclosed = await parts(mod, '```js\nx')
    if (!(closed[0]?.type === 'code' && closed[0]?.loading === false &&
          unclosed[0]?.type === 'code' && unclosed[0]?.loading === true)) return false
    // Opening and closing fences must be distinct; a closing marker must be
    // at least as long as its opener and may have at most three spaces of indent.
    const cases = [
      ['```', true],
      ['```\n```', false],
      ['````js\nx\n```', true],
      ['````js\nx\n````', false],
      ['~~~\nx\n~~~~', false],
      ['```js\n    ```', true],
      ['```js\n\t```', true],
    ]
    for (const [source, loading] of cases) {
      const result = await parts(mod, source)
      if (result.length !== 1 || result[0]?.type !== 'code' ||
          result[0]?.loading !== loading) return false
    }
    return true
  },

  async fencedHtml({ mod }) {
    const html = await render(mod, '```js\nconst a=1\n```')
    return (
      codeBlockMatches(html, 'const a=1', 'js') &&
      html.includes('</code></pre>')
    )
  },

  async rawHtml({ mod }) {
    const html = await render(mod, MARKDOWN_RAW)
    return (
      html.includes('<div class="x">hi</div>') &&
      html.includes('<b>raw</b>') &&
      !html.includes('&lt;div class=&quot;x&quot;&gt;')
    )
  },

  async codeEscaping({ mod }) {
    const code = await render(mod, '```\n<div>x</div>\n```')
    const raw = await render(mod, '<div>x</div>')
    return (
      codeBlockMatches(code, '&lt;div&gt;x&lt;/div&gt;') &&
      raw.includes('<div>x</div>')
    )
  },

  async taskList({ mod }) {
    const html = await render(mod, MARKDOWN_TASK_LIST)
    const container = document.createElement('div')
    container.innerHTML = html
    const items = [...container.querySelectorAll('li')]
    const boxes = [...container.querySelectorAll('input[type="checkbox"]')]
    return (
      items.length === 2 &&
      boxes.length === 2 &&
      boxes.every((box, index) => box.disabled && items[index].contains(box)) &&
      boxes[0].checked &&
      !boxes[1].checked
    )
  },

  async parseTable({ mod }) {
    const processor = baseProcessor(mod)
    const tokens = processor.parse(MARKDOWN_PLAIN_TABLE, {})
    const types = Array.isArray(tokens) ? tokens.map((token) => token.type) : []
    return (
      types.includes('table_open') &&
      types.includes('table_close') &&
      processor.render('# h') === '<h1>h</h1>\n'
    )
  },

  async headingRender({ mod }) {
    const processor = baseProcessor(mod)
    const tokens = processor.parse('# h', {})
    return (
      tokens[0]?.type === 'heading_open' &&
      processor.render('# h') === '<h1>h</h1>\n'
    )
  },

  async htmlPartContent({ mod }) {
    const result = await parts(mod, '**bold**')
    return (
      result.length === 1 &&
      result[0].type === 'html' &&
      result[0].content !== null &&
      typeof result[0].content === 'object' &&
      typeof result[0].content !== 'string'
    )
  },

  async linkTarget({ mod }) {
    const html = await render(mod, MARKDOWN_LINK)
    return (
      html.includes('href="https://example.test"') &&
      html.includes('title="title"') &&
      html.includes('target="_blank"')
    )
  },

  async runtimeIsolation({ mod }) {
    const originals = []
    const bridges = []
    const block = (owner, name, moduleName) => {
      if (typeof owner[name] !== 'function') return
      originals.push([owner, name, owner[name]])
      owner[name] = function () {
        bridges.push(`${moduleName}.${name}`)
        throw new Error('production runtime bridge blocked')
      }
    }
    for (const [moduleName, names] of [
      ['node:child_process', ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']],
      ['node:worker_threads', ['Worker']],
      ['node:vm', ['Script', 'runInContext', 'runInNewContext', 'runInThisContext', 'compileFunction', 'SourceTextModule', 'SyntheticModule']],
      ['node:http', ['request', 'get']],
      ['node:https', ['request', 'get']],
      ['node:net', ['connect', 'createConnection']],
      ['node:tls', ['connect']],
      ['node:dgram', ['createSocket']],
    ]) {
      const owner = nodeRequire(moduleName)
      for (const name of names) block(owner, name, moduleName)
    }
    for (const name of ['fetch', 'WebSocket', 'Worker', 'SharedWorker']) block(globalThis, name, 'globalThis')
    block(process, 'dlopen', 'process')
    syncBuiltinESMExports()
    try {
      const fragments = [
          '*x*', MARKDOWN_MATH_INLINE, '$$x^2$$', MARKDOWN_TABLE,
          MARKDOWN_TASK_LIST, MARKDOWN_LINK, MARKDOWN_RAW,
          '```ts\nconst answer = 42\n```', '*[HTML]: markup\n\nHTML',
          '@@legacy-render@@',
      ]
      const contents = new Set(fragments)
      for (const left of fragments) {
        for (const right of fragments) contents.add(`${left}\n\n${right}`)
      }
      for (const delimiter of ['$', '$$', '\\(', '\\[']) {
        const close = { '$': '$', '$$': '$$', '\\(': '\\)', '\\[': '\\]' }[delimiter]
        contents.add(`${delimiter}a\nb${close}`)
      }
      for (const content of contents) {
        for (const invoke of [
          () => render(mod, content, mod.defaultCustomPlugins),
          () => mod.processMarkdownToParts(content, mod.createBaseProcessor(mod.defaultCustomPlugins)),
        ]) {
          try { await invoke() } catch {}
        }
      }
    } finally {
      for (const [owner, name, original] of originals.reverse()) owner[name] = original
      syncBuiltinESMExports()
    }

    const loaded = Object.keys(nodeRequire.cache).filter((key) =>
      /node_modules/.test(key) &&
      /(?:^|[/\\])(?:unified|remark-[^/\\]+|rehype-[^/\\]+)(?:[/\\])/.test(key)
    )
    const sourceViolations = runtimeSourceViolations()
    return { passed: bridges.length === 0 && loaded.length === 0 && sourceViolations.length === 0, bridges, loaded, sourceViolations }
  },

  async katexProcess({ mod }) {
    const brackets = mod.katexProcess('\\[x\\]')
    const parens = mod.katexProcess('\\(x\\)')
    const multilineInline = mod.katexProcess('$a\nb$')
    const multilineBlock = mod.katexProcess('$$a\nb$$')
    return (
      brackets === '$$x$$' &&
      parens === '$x$' &&
      multilineInline.includes('$$a\nb$$') &&
      multilineBlock.includes('\n$$a\nb$$\n')
    )
  },
}

export const OBSERVATION_CHECKS = {
  'F2P-025': ['targetProcessor', 'strikethrough', 'middleware'],
  'F2P-036': ['runtimeIsolation'],
  'F2P-026': ['targetProcessor', 'katexConfigured', 'strikethrough'],
  'F2P-027': ['targetProcessor', 'singleTilde', 'middleware'],
  'F2P-030': ['targetProcessor', 'emptyParts', 'tableAlignment'],
  'F2P-033': ['targetProcessor', 'katexProcess', 'nestedFence'],
  'F2P-034': ['targetProcessor', 'segmentation', 'tokenShape'],
  'F2P-038': ['targetProcessor', 'codePart', 'tableAlignment'],
  'F2P-041': ['targetProcessor', 'pluginOrder', 'middleware'],
  'F2P-047': ['targetProcessor', 'htmlGrouping', 'codeEscaping'],
  'F2P-048': ['targetProcessor', 'loadingStatus', 'strikethrough'],
  'F2P-029': ['targetProcessor', 'fencedHtml', 'middleware'],
  'F2P-031': ['targetProcessor', 'katexConfigured', 'tableAlignment'],
  'F2P-032': ['targetProcessor', 'fencedHtml', 'codeEscaping'],
  'F2P-035': ['targetProcessor', 'fencedHtml', 'strikethrough'],
  'F2P-037': ['targetProcessor', 'rawHtml', 'middleware'],
  'F2P-039': ['targetProcessor', 'strikethrough'],
  'F2P-040': ['targetProcessor', 'fencedHtml', 'tableAlignment'],
  'F2P-042': ['targetProcessor', 'htmlPartContent', 'parseTable'],
  'F2P-043': ['targetProcessor', 'fencedHtml', 'headingRender'],
  'F2P-044': ['targetProcessor', 'codePart', 'codeEscaping'],
  'F2P-045': ['targetProcessor', 'taskList'],
  'F2P-046': ['targetProcessor', 'middleware', 'parseTable'],
}

export const OBSERVATION_IDS = Object.keys(OBSERVATION_CHECKS)

const cache = new WeakMap()

const evaluate = async (mod, name) => {
  let perModule = cache.get(mod)
  if (!perModule) {
    perModule = new Map()
    cache.set(mod, perModule)
  }
  if (!perModule.has(name)) {
    let value
    try {
      const result = await checks[name]({ mod })
      value = result && typeof result === 'object' && typeof result.passed === 'boolean'
        ? result : { passed: !!result }
    } catch (error) {
      value = {
        passed: false,
        error: String(error && error.message ? error.message : error),
      }
    }
    perModule.set(name, value)
  }
  return perModule.get(name)
}

export const evaluateObservation = async (mod, observationId) => {
  const names = OBSERVATION_CHECKS[observationId]
  if (!names) throw new Error(`Unknown observation ${observationId}`)
  const results = {}
  for (const name of names) results[name] = await evaluate(mod, name)
  const passed = Object.values(results).every((result) => result.passed)
  return {
    observation_id: observationId,
    status: passed ? 'PASS' : 'FAIL',
    checks: results,
  }
}

export const runAll = async (mod) => {
  const results = []
  for (const observationId of OBSERVATION_IDS) {
    results.push(await evaluateObservation(mod, observationId))
  }
  return results
}
