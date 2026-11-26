import codeHtml from '../code-html.cjs'

const { codeBlockMatches } = codeHtml

import tableAlignment from '../table-alignment.cjs'

const { htmlCellAlignments } = tableAlignment

export const BATCH_ID = 'BATCH-DIFFERENTIAL-0001'

export const cases = [
  {
    id: 'DIFF-001',
    requirement_ids: ['R003'],
    surface: 'createBaseProcessor base rules before middleware',
    normalization: 'boolean feature matrix',
  },
  {
    id: 'DIFF-002',
    requirement_ids: ['R004'],
    surface: 'createBaseProcessor middleware ordering and options',
    normalization: 'ordered break toggles',
  },
  {
    id: 'DIFF-003',
    requirement_ids: ['R005'],
    surface: 'MiddlewarePluginItem type',
    normalization: 'runtime entry shape plus compile diagnostics',
  },
  {
    id: 'DIFF-004',
    requirement_ids: ['R006'],
    surface: 'defaultCustomPlugins ordering',
    normalization: 'ordered plugin keys',
  },
  {
    id: 'DIFF-005',
    requirement_ids: ['R007'],
    surface: 'defaultCustomPlugins feature effects',
    normalization: 'feature matrix',
  },
  {
    id: 'DIFF-006',
    requirement_ids: ['R008'],
    surface: 'defaultCustomPlugins options effects',
    normalization: 'option values and rendered effects',
  },
  {
    id: 'DIFF-007',
    requirement_ids: ['R009'],
    surface: 'processMarkdownToParts segmentation',
    normalization: 'part type sequence',
  },
  {
    id: 'DIFF-008',
    requirement_ids: ['R010'],
    surface: 'processMarkdownToHtml raw HTML allowance',
    normalization: 'trimmed HTML string',
  },
  {
    id: 'DIFF-009',
    requirement_ids: ['R011'],
    surface: 'katexProcess delimiter normalization',
    normalization: 'normalized delimiter strings',
  },
  {
    id: 'DIFF-010',
    requirement_ids: ['R012'],
    surface: 'MarkdownPart type',
    normalization: 'runtime part shape plus compile diagnostics',
  },
  {
    id: 'DIFF-011',
    requirement_ids: ['R013'],
    surface: 'ProcessorType alias',
    normalization: 'processor runtime shape plus compile diagnostics',
  },
  {
    id: 'DIFF-012',
    requirement_ids: ['R014'],
    surface: 'markdown-parse type exports',
    normalization: 'public export path and compile diagnostics',
  },
  {
    id: 'DIFF-013',
    requirement_ids: ['R015'],
    surface: 'Markdown and document editor remarkPlugins prop',
    normalization: 'prop declaration plus middleware effect',
  },
  {
    id: 'DIFF-014',
    requirement_ids: ['R016'],
    surface: 'processMarkdownToParts HTML part content',
    normalization: 'VNode tree signature',
  },
  {
    id: 'DIFF-015',
    requirement_ids: ['R017'],
    surface: 'processMarkdownToParts code part content',
    normalization: 'typed code part',
  },
  {
    id: 'DIFF-016',
    requirement_ids: ['R018'],
    surface: '@element-ai-vue/utils exports',
    normalization: 'public namespace values',
  },
  {
    id: 'DIFF-017',
    requirement_ids: ['R019'],
    surface: 'processMarkdownToParts empty input',
    normalization: 'empty part list',
  },
  {
    id: 'DIFF-018',
    requirement_ids: ['R020'],
    surface: 'processMarkdownToHtml hard line breaks',
    normalization: 'hard break presence',
  },
  {
    id: 'DIFF-019',
    requirement_ids: ['R021'],
    surface: 'processMarkdownToHtml GFM tables',
    normalization: 'table structure and effective cell alignment',
  },
  {
    id: 'DIFF-020',
    requirement_ids: ['R022'],
    surface: 'processMarkdownToHtml GFM task lists',
    normalization: 'checkbox state matrix',
  },
  {
    id: 'DIFF-021',
    requirement_ids: ['R023'],
    surface: 'processMarkdownToHtml GFM strikethrough',
    normalization: 'del tag presence',
  },
  {
    id: 'DIFF-022',
    requirement_ids: ['R024'],
    surface: 'processMarkdownToHtml single-tilde handling',
    normalization: 'absence of del tag',
  },
  {
    id: 'DIFF-023',
    requirement_ids: ['R025'],
    surface: 'processMarkdownToHtml link attributes',
    normalization: 'anchor attribute map',
  },
  {
    id: 'DIFF-024',
    requirement_ids: ['R026'],
    surface: 'processMarkdownToHtml mark highlight',
    normalization: 'mark tag presence',
  },
  {
    id: 'DIFF-025',
    requirement_ids: ['R027'],
    surface: 'processMarkdownToHtml sub and superscript',
    normalization: 'sub and sup tag presence',
  },
  {
    id: 'DIFF-026',
    requirement_ids: ['R028'],
    surface: 'processMarkdownToHtml abbreviations',
    normalization: 'abbr tag and consumed definition',
  },
  {
    id: 'DIFF-027',
    requirement_ids: ['R029'],
    surface: 'processMarkdownToParts top-level fenced code parts',
    normalization: 'ordered code payloads',
  },
  {
    id: 'DIFF-028',
    requirement_ids: ['R030'],
    surface: 'processMarkdownToParts code language',
    normalization: 'raw language identifiers for ordinary fences',
  },
  {
    id: 'DIFF-029',
    requirement_ids: ['R031'],
    surface: 'processMarkdownToParts code loading status',
    normalization: 'loading flags',
  },
  {
    id: 'DIFF-030',
    requirement_ids: ['R032'],
    surface: 'processMarkdownToParts HTML grouping',
    normalization: 'part type sequence without empty groups',
  },
  {
    id: 'DIFF-031',
    requirement_ids: ['R033'],
    surface: 'processMarkdownToHtml fenced code HTML',
    normalization: 'pre code language class',
  },
  {
    id: 'DIFF-032',
    requirement_ids: ['R034'],
    surface: 'katexProcess normalization rules',
    normalization: 'normalized delimiter strings',
  },
]

const assert = (condition, message) => {
  if (!condition) throw new Error(message)
}

const normalizeHtml = (value) =>
  String(value ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/>\s+</g, '><')
    .trim()

const booleanAttrs = new Set(['checked', 'disabled'])

const parseAttrs = (raw) => {
  const attrs = {}
  const source = String(raw || '')
  const pattern = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
  let match
  while ((match = pattern.exec(source))) {
    const name = match[1].toLowerCase()
    const value = match[2] ?? match[3] ?? match[4]
    attrs[name] = booleanAttrs.has(name) || value === undefined ? true : value
  }
  return attrs
}

const tagAttrs = (html, tag) => {
  const pattern = new RegExp(`<${tag}\\b([^>]*)>`, 'gi')
  const found = []
  let match
  while ((match = pattern.exec(String(html)))) found.push(parseAttrs(match[1]))
  return found
}

const hasTag = (html, tag) =>
  new RegExp(`<${tag}\\b`, 'i').test(String(html))

const textOf = (html) =>
  String(html)
    .replace(/<[^>]*>/g, '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, ' ')
    .trim()

const vnodeSummary = (node, depth = 0) => {
  if (node === null || node === undefined) return null
  if (typeof node === 'string' || typeof node === 'number') return node
  if (Array.isArray(node)) return node.map((item) => vnodeSummary(item, depth + 1))
  if (typeof node !== 'object') return null
  const children = node.children
  return {
    type: typeof node.type === 'string' ? node.type : node.type?.name ?? null,
    children: vnodeSummary(children, depth + 1),
  }
}

const vnodeContains = (summary, type, text) => {
  if (!summary) return false
  if (Array.isArray(summary)) {
    return summary.some((item) => vnodeContains(item, type, text))
  }
  if (summary.type === type) {
    const flat = JSON.stringify(summary.children ?? [])
    if (flat.includes(JSON.stringify(text).slice(1, -1))) return true
  }
  return vnodeContains(summary.children, type, text)
}

const codeParts = (parts) =>
  parts.filter((part) => part && part.type === 'code')

const htmlParts = (parts) =>
  parts.filter((part) => part && part.type === 'html')

export async function runSuite(adapter) {
  const results = {}

  const run = async (id, fn) => {
    try {
      results[id] = { status: 'PASS', normalized: await fn() }
    } catch (error) {
      results[id] = {
        status: 'FAIL',
        error: error instanceof Error ? error.message : String(error),
      }
    }
  }

  await run('DIFF-001', async () => {
    const base = await adapter.baseProcessor([])
    const hardBreak = normalizeHtml(await adapter.render(base, 'a\nb'))
    const table = normalizeHtml(
      await adapter.render(base, '| a |\n| - |\n| b |')
    )
    const task = normalizeHtml(await adapter.render(base, '- [x] a'))
    const strike = normalizeHtml(await adapter.render(base, '~~x~~'))
    const singleTilde = normalizeHtml(await adapter.render(base, '~x~'))
    const overridden = await adapter.baseProcessor([
      adapter.makeBreakMiddleware(false),
    ])
    const noBreak = normalizeHtml(await adapter.render(overridden, 'a\nb'))

    const checkbox = tagAttrs(task, 'input').find(
      (attrs) => attrs.type === 'checkbox'
    )
    assert(/<br\b/i.test(hardBreak), 'base hard break missing')
    assert(
      hasTag(table, 'table') && hasTag(table, 'thead') && hasTag(table, 'tbody'),
      'base GFM table missing'
    )
    assert(checkbox && checkbox.checked && checkbox.disabled, 'base task list missing')
    assert(/<del>x<\/del>/.test(strike), 'double-tilde strikethrough missing')
    assert(!/<del>x<\/del>/.test(singleTilde), 'single tilde became strikethrough')
    assert(!/<br\b/i.test(noBreak), 'supplied middleware did not run after base rules')

    return {
      hardBreak: true,
      table: true,
      taskCheckbox: true,
      strikethrough: true,
      singleTildeSafe: true,
      middlewareAfterBase: true,
    }
  })

  await run('DIFF-002', async () => {
    const on = adapter.makeBreakMiddleware(true)
    const off = adapter.makeBreakMiddleware(false)
    const offThenOn = normalizeHtml(
      await adapter.render(await adapter.baseProcessor([off, on]), 'a\nb')
    )
    const onThenOff = normalizeHtml(
      await adapter.render(await adapter.baseProcessor([on, off]), 'a\nb')
    )
    assert(/<br\b/i.test(offThenOn), 'off then on did not keep hard breaks')
    assert(!/<br\b/i.test(onThenOff), 'on then off did not disable hard breaks')
    return {
      entryOrder: 'honored',
      offThenOn: true,
      onThenOff: false,
      optionsDelivered: true,
    }
  })

  await run('DIFF-003', async () => {
    const observed = await adapter.observeMiddleware()
    const checks = await adapter.typecheck([
      {
        name: 'valid',
        expect: 'pass',
        code: [
          "import type { MiddlewarePluginItem } from './packages/utils/markdown-parse/index'",
          "const item: MiddlewarePluginItem = { key: 'k', plugin: () => {}, options: { x: 1 } }",
          'export default item',
        ].join('\n'),
      },
      {
        name: 'missing-key',
        expect: 'fail',
        code: [
          "import type { MiddlewarePluginItem } from './packages/utils/markdown-parse/index'",
          'const item: MiddlewarePluginItem = { plugin: () => {} }',
          'export default item',
        ].join('\n'),
      },
    ])
    assert(observed.keyRetained, 'middleware key was not retained')
    assert(observed.optionsRetained, 'middleware options were not retained')
    assert(observed.optionsDelivered, 'middleware options were not delivered to plugin')
    assert(checks.valid.ok, 'MiddlewarePluginItem rejected a valid entry')
    assert(!checks['missing-key'].ok, 'MiddlewarePluginItem accepted an entry without key')
    return {
      keyRetained: true,
      optionsRetained: true,
      optionsDelivered: true,
      typeAccepts: true,
      typeRejectsMissingKey: true,
    }
  })

  await run('DIFF-004', async () => {
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
    const entries = adapter.api.defaultCustomPlugins
    const keys = entries.map((entry) => entry.key)
    assert(
      JSON.stringify(keys) === JSON.stringify(expected),
      `defaultCustomPlugins keys differ: ${JSON.stringify(keys)}`
    )
    assert(
      entries.every(
        (entry) =>
          entry &&
          typeof entry.key === 'string' &&
          Object.prototype.hasOwnProperty.call(entry, 'plugin')
      ),
      'defaultCustomPlugins entries must expose key and plugin'
    )
    return { keys, entries: entries.length }
  })

  await run('DIFF-005', async () => {
    const processor = await adapter.processorWithDefaults()
    const inlineMath = normalizeHtml(await adapter.render(processor, '$x$'))
    const link = normalizeHtml(await adapter.render(processor, '[a](https://e)'))
    const mark = normalizeHtml(await adapter.render(processor, '==x=='))
    const subSuper = normalizeHtml(await adapter.render(processor, '~x~ ^x^'))
    const abbr = normalizeHtml(
      await adapter.render(processor, '*[HTML]: HyperText Markup Language\n\nHTML')
    )
    const unclosed = await adapter.parts(processor, '```js\nx')
    const raw = normalizeHtml(await adapter.render(processor, '<b>x</b>'))
    const block = normalizeHtml(await adapter.render(processor, '$$\nx\n$$'))

    assert(/class="katex"/.test(inlineMath), 'inline math missing katex class')
    assert(
      tagAttrs(link, 'a').some((attrs) => attrs.target === '_blank'),
      'default link target handling missing'
    )
    assert(/<mark>x<\/mark>/.test(mark), 'mark highlight missing')
    assert(/<sub>x<\/sub>/.test(subSuper), 'subscript handling missing')
    assert(/<sup>x<\/sup>/.test(subSuper), 'superscript handling missing')
    assert(
      /<abbr title="HyperText Markup Language">HTML<\/abbr>/.test(abbr),
      'abbreviation handling missing'
    )
    assert(
      codeParts(unclosed).some((part) => part.loading === true),
      'unclosed fence loading status missing'
    )
    assert(/<b>x<\/b>/.test(raw), 'raw HTML passthrough missing')
    assert(/katex-display/.test(block), 'block math wrapper missing')

    return {
      inlineMath: true,
      linkTarget: true,
      mark: true,
      sub: true,
      sup: true,
      abbreviation: true,
      unclosedFenceLoading: true,
      rawHtml: true,
      blockMath: true,
    }
  })

  await run('DIFF-006', async () => {
    const entries = adapter.api.defaultCustomPlugins
    const rehype = entries.find((entry) => entry.key === 'remark-rehype')
    const katex = entries.find((entry) => entry.key === 'rehype-katex')
    assert(rehype, 'remark-rehype entry missing')
    assert(katex, 'rehype-katex entry missing')
    assert(
      rehype.options && rehype.options.allowDangerousHtml === true,
      'remark-rehype allowance option missing'
    )
    const config = katex.options || {}
    assert(config.throwOnError === false, 'katex throwOnError option missing')
    assert(config.errorColor === '#cc0000', 'katex errorColor option missing')
    assert(config.strict === false, 'katex strict option missing')
    assert(
      config.macros && config.macros['\\RR'],
      'katex macros option missing'
    )

    const processor = await adapter.processorWithDefaults()
    const raw = normalizeHtml(await adapter.render(processor, '<b>x</b>'))
    const macro = normalizeHtml(await adapter.render(processor, '$\\RR$'))
    assert(/<b>x<\/b>/.test(raw), 'default raw HTML effect missing')
    assert(
      /mathvariant="double-struck"|mathbb/.test(macro),
      'configured RR macro was not applied'
    )
    return {
      allowDangerousHtml: true,
      katexConfig: true,
      rawHtmlEffect: true,
      macroEffect: true,
    }
  })

  await run('DIFF-007', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(
      processor,
      'text\n\n```js\ncode\n```\n\ntail'
    )
    const types = parts.map((part) => part.type)
    assert(
      JSON.stringify(types) === JSON.stringify(['html', 'code', 'html']),
      `unexpected part types: ${JSON.stringify(types)}`
    )
    return { types }
  })

  await run('DIFF-008', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(await adapter.render(processor, '<div>x</div>'))
    assert(html === '<div>x</div>', `raw HTML was changed: ${html}`)
    return { html }
  })

  await run('DIFF-009', async () => {
    const api = adapter.api
    const output = {
      bracket: api.katexProcess('\\[x\\]'),
      paren: api.katexProcess('\\(x\\)'),
      multilineSingle: api.katexProcess('$a\nb$'),
      multilineDouble: api.katexProcess('$$a\nb$$'),
    }
    assert(output.bracket === '$$x$$', 'bracket delimiter normalization failed')
    assert(output.paren === '$x$', 'paren delimiter normalization failed')
    assert(output.multilineSingle === '\n$$a\nb$$\n', 'single-dollar promotion failed')
    assert(
      output.multilineDouble === '\n$$a\nb$$\n',
      'double-dollar block padding failed'
    )
    return output
  })

  await run('DIFF-010', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(processor, '```js\nx\n```')
    const code = parts.find((part) => part.type === 'code')
    assert(code, 'code part missing')
    assert(typeof code.content === 'string', 'code content is not text')
    const checks = await adapter.typecheck([
      {
        name: 'valid',
        expect: 'pass',
        code: [
          "import type { MarkdownPart } from './packages/utils/markdown-parse/index'",
          "const part: MarkdownPart = { type: 'code', content: 'x', language: 'js', loading: false }",
          'export default part',
        ].join('\n'),
      },
      {
        name: 'invalid-type',
        expect: 'fail',
        code: [
          "import type { MarkdownPart } from './packages/utils/markdown-parse/index'",
          "const part: MarkdownPart = { type: 'text', content: 'x' }",
          'export default part',
        ].join('\n'),
      },
    ])
    assert(checks.valid.ok, 'MarkdownPart rejected a valid part')
    assert(!checks['invalid-type'].ok, 'MarkdownPart accepted an invalid type')
    return {
      runtimeShape: true,
      typeAccepts: true,
      typeRejectsInvalidType: true,
    }
  })

  await run('DIFF-011', async () => {
    const processor = await adapter.baseProcessor([])
    const processorLike =
      typeof processor.parse === 'function' &&
      (typeof processor.render === 'function' ||
        typeof processor.run === 'function')
    const checks = await adapter.typecheck([
      {
        name: 'processor-type',
        expect: 'pass',
        code: [
          "import { createBaseProcessor } from './packages/utils/markdown-parse/index'",
          "import type { ProcessorType } from './packages/utils/markdown-parse/index'",
          'const processor: ProcessorType = createBaseProcessor([])',
          'export default processor',
        ].join('\n'),
      },
    ])
    assert(processorLike, 'createBaseProcessor result is not a processor')
    assert(checks['processor-type'].ok, 'createBaseProcessor is not assignable to ProcessorType')
    return { processorLike: true, processorTypeCompiles: true }
  })

  await run('DIFF-012', async () => {
    const checks = await adapter.typecheck([
      {
        name: 'type-exports',
        expect: 'pass',
        code: [
          "import type { MiddlewarePluginItem, MarkdownPart, ProcessorType, ProcessMarkdownOptions } from './packages/utils/markdown-parse/index'",
          'const item: MiddlewarePluginItem = { key: "k", plugin: () => {} }',
          "const part: MarkdownPart = { type: 'html', content: null }",
          'const processor: ProcessorType = null as unknown as ProcessorType',
          'const options: ProcessMarkdownOptions = {}',
          'export { item, part, processor, options }',
        ].join('\n'),
      },
    ])
    const indexText = await adapter.sourceText('packages/utils/index.ts')
    assert(checks['type-exports'].ok, 'markdown-parse type exports are missing')
    assert(
      /export\s+\*\s+from\s+['"]\.\/markdown-parse['"]/.test(indexText),
      'packages/utils/index.ts does not re-export markdown-parse'
    )
    return { typeExportsCompile: true, utilsReExport: true }
  })

  await run('DIFF-013', async () => {
    const markdownProps = await adapter.sourceText(
      'packages/components/markdown/props.ts'
    )
    const docProps = await adapter.sourceText(
      'packages/components/m-doc-editor/props.ts'
    )
    const propInfo = (text) => {
      const index = text.indexOf('remarkPlugins')
      const chunk = index >= 0 ? text.slice(index, index + 260) : ''
      return {
        present: index >= 0,
        arrayType: /Array\s+as\s+PropType<\s*MiddlewarePluginItem\[\]\s*>/.test(
          chunk
        ),
        defaultArray: /default:\s*\(\)\s*=>\s*\[\]/.test(chunk),
      }
    }
    const markdown = propInfo(markdownProps)
    const documentEditor = propInfo(docProps)
    assert(markdown.present && documentEditor.present, 'remarkPlugins prop missing')
    assert(markdown.arrayType && documentEditor.arrayType, 'remarkPlugins is not MiddlewarePluginItem[]')
    assert(
      markdown.defaultArray && documentEditor.defaultArray,
      'remarkPlugins default array missing'
    )
    const custom = await adapter.processorWithDefaults([
      adapter.makeBreakMiddleware(false),
    ])
    const rendered = normalizeHtml(await adapter.render(custom, 'a\nb'))
    assert(!/<br\b/i.test(rendered), 'custom remarkPlugins entry was not applied')
    return {
      markdownProp: true,
      documentEditorProp: true,
      middlewareCompatible: true,
    }
  })

  await run('DIFF-014', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(processor, '**bold**')
    const htmlPart = htmlParts(parts)[0]
    assert(htmlPart, 'html part missing')
    assert(
      htmlPart.content && typeof htmlPart.content === 'object',
      'html part content is not a VNode tree'
    )
    const summary = vnodeSummary(htmlPart.content)
    assert(vnodeContains(summary, 'strong', 'bold'), 'VNode tree does not render <strong>bold</strong>')
    return { vnode: true, strong: true }
  })

  await run('DIFF-015', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(processor, '```js\nconst a=1\n```')
    const code = parts[0]
    assert(code && code.type === 'code', 'first part is not code')
    assert(code.content === 'const a=1\n', `unexpected code content: ${JSON.stringify(code.content)}`)
    assert(code.language === 'js', `unexpected language: ${JSON.stringify(code.language)}`)
    assert(code.loading === false, 'code loading must be false')
    return {
      type: code.type,
      content: code.content,
      language: code.language,
      loading: code.loading,
    }
  })

  await run('DIFF-016', async () => {
    const names = [
      'createBaseProcessor',
      'processMarkdownToParts',
      'processMarkdownToHtml',
      'katexProcess',
      'defaultCustomPlugins',
    ]
    const missing = names.filter(
      (name) => typeof adapter.publicApi[name] === 'undefined'
    )
    assert(missing.length === 0, `missing public exports: ${missing.join(', ')}`)
    const indexText = await adapter.sourceText('packages/utils/index.ts')
    assert(
      /export\s+\*\s+from\s+['"]\.\/markdown-parse['"]/.test(indexText),
      'utils index does not re-export markdown-parse'
    )
    return { exports: names.length, reExported: true }
  })

  await run('DIFF-017', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(processor, '')
    assert(Array.isArray(parts) && parts.length === 0, 'empty input must return []')
    return { parts: 0 }
  })

  await run('DIFF-018', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(await adapter.render(processor, 'a\nb'))
    assert(/a<br\b[^>]*>[\s\S]*b/.test(html), 'hard line break missing')
    return { hardBreak: true }
  })

  await run('DIFF-019', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(
      await adapter.render(processor, '| H1 | H2 |\n| :-- | --: |\n| a | b |')
    )
    const headers = htmlCellAlignments(html, 'th')
    const cells = htmlCellAlignments(html, 'td')
    assert(hasTag(html, 'thead') && hasTag(html, 'tbody'), 'table sections missing')
    assert(headers.length === 2 && cells.length === 2, 'unexpected table cell count')
    assert(headers[0] === 'left', 'header left alignment missing')
    assert(headers[1] === 'right', 'header right alignment missing')
    assert(cells[0] === 'left', 'body left alignment missing')
    assert(cells[1] === 'right', 'body right alignment missing')
    assert(textOf(html).includes('H1') && textOf(html).includes('b'), 'table text missing')
    return {
      headers,
      cells,
    }
  })

  await run('DIFF-020', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(
      await adapter.render(processor, '- [x] done\n- [ ] todo')
    )
    const checkboxes = tagAttrs(html, 'input').filter(
      (attrs) => attrs.type === 'checkbox'
    )
    const items = tagAttrs(html, 'li')
    assert(checkboxes.length === 2, 'expected two task checkboxes')
    assert(checkboxes[0].checked && !checkboxes[1].checked, 'checkbox states are wrong')
    assert(
      checkboxes.every((attrs) => attrs.disabled === true),
      'task checkboxes must be disabled'
    )
    assert(items.length === 2, 'expected two task list items')
    return {
      checkboxes: checkboxes.length,
      checked: [true, false],
      disabled: true,
    }
  })

  await run('DIFF-021', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(await adapter.render(processor, '~~text~~'))
    assert(/<del>text<\/del>/.test(html), 'del element missing')
    assert(!/<s>text<\/s>/.test(html), 'unexpected s element')
    return { del: true, s: false }
  })

  await run('DIFF-022', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(await adapter.render(processor, '~text~'))
    assert(!/<del>text<\/del>/.test(html), 'single tilde rendered as strikethrough')
    assert(textOf(html).includes('text'), 'single tilde content disappeared')
    return { del: false, contentPreserved: true }
  })

  await run('DIFF-023', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(
      await adapter.render(processor, '[docs](https://example.com "Title")')
    )
    const anchor = tagAttrs(html, 'a').find(
      (attrs) => attrs.href === 'https://example.com'
    )
    assert(anchor, 'link anchor missing')
    assert(anchor.title === 'Title', 'link title missing')
    assert(anchor.target === '_blank', 'link target missing')
    return {
      href: anchor.href,
      title: anchor.title,
      target: anchor.target,
    }
  })

  await run('DIFF-024', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(await adapter.render(processor, '==text=='))
    assert(/<mark>text<\/mark>/.test(html), 'mark element missing')
    return { mark: true }
  })

  await run('DIFF-025', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(await adapter.render(processor, '~x~ ^x^'))
    assert(/<sub>x<\/sub>/.test(html), 'sub element missing')
    assert(/<sup>x<\/sup>/.test(html), 'sup element missing')
    return { sub: true, sup: true }
  })

  await run('DIFF-026', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(
      await adapter.render(processor, '*[ABBR]: definition\n\nABBR')
    )
    assert(
      /<abbr title="definition">ABBR<\/abbr>/.test(html),
      'abbr element missing'
    )
    assert(!html.includes('*[ABBR]'), 'abbreviation definition was not consumed')
    assert(!textOf(html).includes('definition'), 'definition text leaked into output')
    return { abbr: true, definitionConsumed: true }
  })

  await run('DIFF-027', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(
      processor,
      'before\n\n```js\na\n```\n\nmiddle\n\n```js\nb\n```\n\nafter'
    )
    const codes = codeParts(parts)
    assert(codes.length === 2, `expected two code parts, got ${codes.length}`)
    assert(codes[0].content === 'a\n' && codes[1].content === 'b\n', 'code payload order is wrong')
    return { codeParts: codes.length, order: ['a\n', 'b\n'] }
  })

  await run('DIFF-028', async () => {
    const processor = await adapter.processorWithDefaults()
    const inputs = ['```js\nx\n```', '```python\nx\n```', '```\nx\n```']
    const languages = []
    for (const input of inputs) {
      const part = (await adapter.parts(processor, input)).find(
        (candidate) => candidate.type === 'code'
      )
      assert(part, `code part missing for ${JSON.stringify(input)}`)
      languages.push(part.language)
    }
    assert(
      JSON.stringify(languages) === JSON.stringify(['js', 'python', '']),
      `unexpected languages: ${JSON.stringify(languages)}`
    )
    return { languages }
  })

  await run('DIFF-029', async () => {
    const processor = await adapter.processorWithDefaults()
    const closed = (await adapter.parts(processor, '```js\nx\n```')).find(
      (part) => part.type === 'code'
    )
    const unclosed = (await adapter.parts(processor, '```js\nx')).find(
      (part) => part.type === 'code'
    )
    assert(closed && closed.loading === false, 'closed fence must not be loading')
    assert(unclosed && unclosed.loading === true, 'unclosed fence must be loading')
    return { closed: false, unclosed: true }
  })

  await run('DIFF-030', async () => {
    const processor = await adapter.processorWithDefaults()
    const parts = await adapter.parts(
      processor,
      '```js\na\n```\ntext\n```js\nb\n```'
    )
    const types = parts.map((part) => part.type)
    assert(
      JSON.stringify(types) === JSON.stringify(['code', 'html', 'code']),
      `unexpected grouping: ${JSON.stringify(types)}`
    )
    return { types, emptyHtmlGroups: false }
  })

  await run('DIFF-031', async () => {
    const processor = await adapter.processorWithDefaults()
    const html = normalizeHtml(
      await adapter.render(processor, '```js\nconst a=1\n```')
    )
    assert(codeBlockMatches(html, 'const a=1', 'js'), 'pre/code language class or code content missing')
    assert(/const a=1[\s\S]*<\/code><\/pre>/.test(html), 'code content missing from pre')
    return { pre: true, languageClass: 'language-js' }
  })

  await run('DIFF-032', async () => {
    const api = adapter.api
    const output = {
      bracket: api.katexProcess('\\[x\\]'),
      paren: api.katexProcess('\\(x\\)'),
      multilineSingle: api.katexProcess('$a\nb$'),
      multilineDouble: api.katexProcess('$$a\nb$$'),
    }
    assert(output.bracket === '$$x$$', 'bracket conversion failed')
    assert(output.paren === '$x$', 'paren conversion failed')
    assert(output.multilineSingle === '\n$$a\nb$$\n', 'multi-line promotion failed')
    assert(
      output.multilineDouble.startsWith('\n') &&
        output.multilineDouble.endsWith('\n'),
      'multi-line block padding failed'
    )
    return output
  })

  return {
    batch_id: BATCH_ID,
    observations: cases.map((entry) => entry.id),
    results,
  }
}
