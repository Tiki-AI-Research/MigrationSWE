import codeHtml from '../code-html.cjs'

const { codeBlockMatches } = codeHtml

import tableAlignment from '../table-alignment.cjs'

const { effectiveAlignment, matchesTableAlignment } = tableAlignment

const TABLE = '| L | C | R |\n| :-- | :-: | --: |\n| a | b | c |'
const TABLE_SHAPE = '| H1 | H2 |\n| :-- | --: |\n| a | b |'
const CODE_ESCAPE = '```\n<div>x</div>\n```'
const RAW_HTML = '<div class="x">hi</div>\n\ninline <b>raw</b>'
const ABBR = '*[HTML]: HyperText Markup Language\n\nHTML XHTML'
const QUOTED_ABBR_TITLE = 'A "quoted" & detailed definition'

const abbreviationsMatch = (html, text, title, expectedText, count) => {
  const container = document.createElement('div')
  container.innerHTML = html
  const abbreviations = [...container.querySelectorAll('abbr')]
  // Compare decoded attributes so quote style and named/numeric HTML entities
  // are equivalent. An unescaped quote must not truncate the definition.
  return abbreviations.length === count && abbreviations.every((abbr) =>
    abbr.textContent === text && abbr.getAttribute('title') === title
  ) && container.textContent.trim().replace(/\s+/g, ' ') === expectedText
}

const taskBoundariesOk = async (api, processor) => {
  for (const source of ['- > [x] quoted', '- `prefix`[x] ordinary']) {
    const html = await api.processMarkdownToHtml(source, processor)
    const container = document.createElement('div')
    container.innerHTML = html
    // A task marker belongs at the start of the list item's first paragraph;
    // text inside a quote or after inline code is ordinary Markdown content.
    if (container.querySelector('input') ||
        !container.textContent.includes('[x]')) return false
  }
  return true
}

const trimEnd = (value) => String(value ?? '').replace(/\s+$/, '')

const tokenList = (processor, source) => {
  try {
    const tokens = processor.parse(source, {})
    return Array.isArray(tokens) ? tokens : []
  } catch {
    return []
  }
}

const flatTokens = (tokens) => {
  if (!Array.isArray(tokens)) return []
  return tokens.flatMap((token) => [
    token,
    ...(Array.isArray(token.children) ? flatTokens(token.children) : []),
  ])
}

const attrValue = (token, name) => {
  if (!token) return undefined
  if (typeof token.attrGet === 'function') {
    const value = token.attrGet(name)
    if (value !== null && value !== undefined) return value
  }
  if (Array.isArray(token.attrs)) {
    const pair = token.attrs.find((entry) => entry && entry[0] === name)
    if (pair) return pair[1]
  }
  return undefined
}

const tableTokenState = (processor) => {
  const tokens = flatTokens(tokenList(processor, TABLE))
  const alignment = (token) => effectiveAlignment(
    attrValue(token, 'align'), attrValue(token, 'style')
  )
  return {
    types: tokens.map((token) => token.type),
    headers: tokens.filter((token) => token.type === 'th_open').map(alignment),
    cells: tokens.filter((token) => token.type === 'td_open').map(alignment),
  }
}

const tableShapeOk = (html) =>
  html.includes('<thead') &&
  html.includes('<tbody') &&
  html.includes('H1') &&
  html.includes('H2') &&
  html.includes('>a<') &&
  html.includes('>b<') &&
  matchesTableAlignment(html, ['left', 'right'])

const alignHtmlOk = (html) =>
  matchesTableAlignment(html, ['left', 'center', 'right'])

const alignTokenOk = (processor) => {
  const state = tableTokenState(processor)
  return (
    ['table_open', 'table_close'].every((type) => state.types.includes(type)) &&
    JSON.stringify(state.headers) === JSON.stringify(['left', 'center', 'right']) &&
    JSON.stringify(state.cells) === JSON.stringify(['left', 'center', 'right'])
  )
}

const mathTokenOk = (tokens, type, htmlType, className) =>
  tokens.some((token) => token.type === type && String(token.content).trim() === 'x') ||
  tokens.some((token) =>
    token.type === htmlType &&
    />\s*x\s*</.test(String(token.content)) &&
    [...String(token.content).matchAll(/\bclass\s*=\s*(["'])(.*?)\1/gi)].some(
      (match) => match[2].split(/\s+/).includes(className)
    )
  )

const strikeRenderingOk = async (api, processor) => {
  if (!headingTokenOk(processor)) return false
  const html = await api.processMarkdownToHtml('~~x~~', processor)
  return html.includes('<del>x</del>') && !html.includes('<s>x</s>')
}

const fenceTokenOk = (processor, source = CODE_ESCAPE) =>
  flatTokens(tokenList(processor, source)).some((token) => token.type === 'fence')

const htmlBlockTokenOk = (processor, source = '<div>x</div>') =>
  flatTokens(tokenList(processor, source)).some(
    (token) => token.type === 'html_block' || token.type === 'html_inline'
  )

const softbreakTokenOk = (processor) =>
  flatTokens(tokenList(processor, 'a\nb')).some(
    (token) => token.type === 'softbreak' || token.type === 'hardbreak'
  )

const taskTokenOk = (processor) => {
  try {
    const env = {}
    const tokens = processor.parse('- [x] done\n- [ ] todo', env)
    // Task-list plugins may put attributes in a renderer rule rather than on
    // the token itself. Check the parsed stream's observable HTML contract.
    return Array.isArray(tokens) && taskMarkupOk(
      processor.renderer.render(tokens, processor.options, env)
    )
  } catch {
    return false
  }
}

const headingTokenOk = (processor, source = '# h') => {
  const tokens = tokenList(processor, source)
  return tokens[0] && tokens[0].type === 'heading_open'
}

const renderHeadingOk = (processor) => trimEnd(processor.render('# h', {})) === '<h1>h</h1>'

const tablePostconditionOk = (processor) => {
  const state = tableTokenState(processor)
  return (
    state.types.includes('table_open') &&
    state.types.includes('table_close') &&
    headingTokenOk(processor) &&
    renderHeadingOk(processor)
  )
}

const codeEscapeOk = async (api, processor) => {
  const codeHtml = await api.processMarkdownToHtml(CODE_ESCAPE, processor)
  const rawHtml = await api.processMarkdownToHtml('<div>x</div>', processor)
  return (
    codeBlockMatches(codeHtml, '&lt;div&gt;x&lt;/div&gt;') &&
    codeHtml.includes('</code></pre>') &&
    rawHtml.includes('<div>x</div>') &&
    fenceTokenOk(processor)
  )
}

const vnodeText = (value) => {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(vnodeText).join('')
  if (value && typeof value === 'object') {
    if (typeof value.children === 'string') return value.children
    if (Array.isArray(value.children)) return value.children.map(vnodeText).join('')
  }
  return ''
}

const vnodeHasTag = (value, tag) => {
  if (Array.isArray(value)) return value.some((child) => vnodeHasTag(child, tag))
  if (value && typeof value === 'object') {
    if (value.type === tag) return true
    if (Array.isArray(value.children)) {
      return value.children.some((child) => vnodeHasTag(child, tag))
    }
  }
  return false
}

const tagAttrs = (source) => {
  const attrs = new Map()
  const pattern = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
  let match
  while ((match = pattern.exec(source))) {
    attrs.set(match[1].toLowerCase(), match[2] ?? match[3] ?? match[4])
  }
  return attrs
}

const taskMarkupOk = (html) => {
  const items = [...String(html).matchAll(/<li\b([^>]*)>([\s\S]*?)<\/li>/gi)]
  if (items.length !== 2) return false
  return items.every((item, index) => {
    const inputs = [...item[2].matchAll(/<input\b([^>]*)\/?\s*>/gi)]
    if (inputs.length !== 1) return false
    const attrs = tagAttrs(inputs[0][1])
    return (
      attrs.get('type') === 'checkbox' &&
      attrs.has('disabled') &&
      attrs.has('checked') === (index === 0)
    )
  })
}

const middlewareOrderOk = (api) => {
  try {
    const entries = [
      { key: 'off', plugin: (md) => md.set({ breaks: false }) },
      {
        key: 'on',
        plugin: (md, options) => md.set({ breaks: options.breaks }),
        options: { breaks: true },
      },
    ]
    const forward = api.createBaseProcessor(entries).render('a\nb', {})
    const reverse = api.createBaseProcessor([...entries].reverse()).render('a\nb', {})
    return forward.includes('<br') && !reverse.includes('<br')
  } catch {
    return false
  }
}

export const checks = [
  {
    id: 'F2P-001',
    requirement_ids: ['R059'],
    async run(api) {
      return codeEscapeOk(api, api.createBaseProcessor(api.defaultCustomPlugins))
    },
  },
  {
    id: 'F2P-002',
    requirement_ids: ['R060'],
    async run(api) {
      const processor = api.createBaseProcessor([])
      return await strikeRenderingOk(api, processor)
    },
  },
  {
    id: 'F2P-004',
    requirement_ids: ['R049'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml(TABLE, processor)
      return middlewareOrderOk(api) && alignHtmlOk(html) && alignTokenOk(processor)
    },
  },
  {
    id: 'F2P-006',
    requirement_ids: ['R001'],
    async run(api) {
      try {
        const processor = api.createBaseProcessor([])
        const strike = await api.processMarkdownToHtml('~~text~~', processor)
        return (
          strike.includes('<del>text</del>') &&
          !strike.includes('<s>text</s>') &&
          tablePostconditionOk(processor)
        )
      } catch {
        return false
      }
    },
  },
  {
    id: 'F2P-009',
    requirement_ids: ['R054'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml(TABLE_SHAPE, processor)
      const strike = await api.processMarkdownToHtml('~~x~~', processor)
      return (
        tableShapeOk(html) &&
        strike.includes('<del>x</del>') &&
        !strike.includes('<s>x</s>') &&
        alignTokenOk(processor)
      )
    },
  },
  {
    id: 'F2P-011',
    requirement_ids: ['R002'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const rawHtml = await api.processMarkdownToHtml(RAW_HTML, processor)
      const base = api.createBaseProcessor([])
      return (
        rawHtml.includes('<div class="x">hi</div>') &&
        rawHtml.includes('<b>raw</b>') &&
        headingTokenOk(base) &&
        renderHeadingOk(base)
      )
    },
  },
  {
    id: 'F2P-012',
    requirement_ids: ['R051'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const inlineSource = '$x$'
      const blockSource = '$$\nx\n$$'
      const inline = flatTokens(tokenList(processor, inlineSource))
      const block = flatTokens(tokenList(processor, blockSource))
      // Blank lines are formula whitespace, not the end of a delimited block.
      // Check rendered behavior so equivalent markdown-it math plugins work.
      for (const source of ['$$\nx\n\n+ y\n$$', '$$\n\nx\n$$']) {
        const container = document.createElement('div')
        container.innerHTML = await api.processMarkdownToHtml(source, processor)
        if (!container.querySelector('.katex-display') ||
            container.querySelector('.katex-error') ||
            container.textContent.includes('$$')) return false
      }
      return (
        mathTokenOk(inline, 'math_inline', 'html_inline', 'katex') &&
        mathTokenOk(block, 'math_block', 'html_block', 'katex-display')
      )
    },
  },
  {
    id: 'F2P-016',
    requirement_ids: ['R056'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml(ABBR, processor)
      const quoted = await api.processMarkdownToHtml(
        `*[ABC]: ${QUOTED_ABBR_TITLE}\n\nABC XABC ABC`, processor
      )
      const reused = await api.processMarkdownToHtml('ABC', processor)
      const boundary = abbreviationsMatch(
        html, 'HTML', 'HyperText Markup Language', 'HTML XHTML', 1
      )
      return boundary && abbreviationsMatch(
        quoted, 'ABC', QUOTED_ABBR_TITLE, 'ABC XABC ABC', 2
      ) &&
        !/<abbr\b/i.test(reused) && headingTokenOk(processor)
    },
  },
  {
    id: 'F2P-017',
    requirement_ids: ['R053'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const task = await api.processMarkdownToHtml('- [x] done\n- [ ] todo', processor)
      const escapedProcessor = api.createBaseProcessor(
        api.defaultCustomPlugins.map((entry) =>
          entry.key === 'remark-rehype'
            ? { ...entry, options: { allowDangerousHtml: false } }
            : entry
        )
      )
      const rawProcessor = api.createBaseProcessor(
        api.defaultCustomPlugins.map((entry) =>
          entry.key === 'remark-rehype'
            ? { ...entry, options: { allowDangerousHtml: true } }
            : entry
        )
      )
      const escaped = await api.processMarkdownToHtml('<b>x</b>', escapedProcessor)
      const raw = await api.processMarkdownToHtml('<b>x</b>', rawProcessor)
      return (
        taskMarkupOk(task) &&
        (escaped.includes('&lt;b&gt;x&lt;/b&gt;') || /<p>\s*x\s*<\/p>/.test(escaped)) &&
        raw.includes('<b>x</b>') &&
        typeof escapedProcessor.set === 'function' &&
        taskTokenOk(processor) &&
        await taskBoundariesOk(api, processor)
      )
    },
  },
  {
    id: 'F2P-018',
    requirement_ids: ['R057'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml('a\nb', processor)
      const parts = await api.processMarkdownToParts('> ```js\n> x\n> ```', processor)
      return (
        html.includes('<br') &&
        parts.length === 1 &&
        parts[0].type === 'html' &&
        softbreakTokenOk(processor)
      )
    },
  },
  {
    id: 'F2P-019',
    requirement_ids: ['R050'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const rehype = api.defaultCustomPlugins.find((entry) => entry.key === 'remark-rehype')
      const katex = api.defaultCustomPlugins.find((entry) => entry.key === 'rehype-katex')
      const optionsOk =
        rehype &&
        rehype.options &&
        rehype.options.allowDangerousHtml === true &&
        katex &&
        katex.options &&
        katex.options.throwOnError === false &&
        katex.options.errorColor === '#cc0000' &&
        katex.options.strict === false &&
        katex.options.macros &&
        Object.keys(katex.options.macros).length > 0
      const raw = await api.processMarkdownToHtml('<b>x</b>', processor)
      const macro = await api.processMarkdownToHtml('$\\RR$', processor)
      const task = await api.processMarkdownToHtml('- [x] done\n- [ ] todo', processor)
      return (
        optionsOk &&
        raw.includes('<b>x</b>') &&
        macro.includes('mathbb') &&
        taskMarkupOk(task) &&
        headingTokenOk(processor)
      )
    },
  },
  {
    id: 'F2P-021',
    requirement_ids: ['R055'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      for (const language of ['js', 'math']) {
        // A language named "math" is still a fenced code block. Dollar math
        // extensions must not consume it or merge it into an HTML part.
        const source = '```' + language + '\nx\n```'
        const closed = await api.processMarkdownToParts(source, processor)
        const unclosed = await api.processMarkdownToParts(
          '```' + language + '\nx', processor
        )
        if (!(closed.length === 1 && closed[0].type === 'code' &&
              closed[0].content === 'x\n' && closed[0].language === language &&
              closed[0].loading === false &&
              unclosed.length === 1 && unclosed[0].type === 'code' &&
              unclosed[0].language === language &&
              unclosed[0].loading === true)) return false
      }
      return fenceTokenOk(processor, '```js\nx\n```')
    },
  },
  {
    id: 'F2P-003',
    requirement_ids: ['R060'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml('```js\nconst a=1\n```', processor)
      const mathFence = await api.processMarkdownToHtml(
        '```math\nx < y\n```', processor
      )
      const base = api.createBaseProcessor([])
      return (
        codeBlockMatches(html, 'const a=1', 'js') &&
        html.includes('</code></pre>') &&
        codeBlockMatches(mathFence, 'x &lt; y', 'math') &&
        headingTokenOk(base) &&
        await strikeRenderingOk(api, base)
      )
    },
  },
  {
    id: 'F2P-007',
    requirement_ids: ['R030'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const js = await api.processMarkdownToParts('```js\nx\n```', processor)
      const languageJs = await api.processMarkdownToParts('```language-js\nx\n```', processor)
      const plain = await api.processMarkdownToParts('```\nx\n```', processor)
      return (
        js[0] &&
        js[0].language === 'js' &&
        languageJs[0] &&
        languageJs[0].language === 'js' &&
        plain[0] &&
        plain[0].language === ''
      )
    },
  },
  {
    id: 'F2P-008',
    requirement_ids: ['R049'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const parts = await api.processMarkdownToParts('**bold**', processor)
      const content = parts[0] && parts[0].content
      const html = await api.processMarkdownToHtml(TABLE, processor)
      return (
        content &&
        typeof content === 'object' &&
        vnodeHasTag(content, 'strong') &&
        vnodeText(content).includes('bold') &&
        alignHtmlOk(html) &&
        alignTokenOk(processor)
      )
    },
  },
  {
    id: 'F2P-010',
    requirement_ids: ['R054'],
    async run(api) {
      try {
        const base = api.createBaseProcessor([])
        const hardBreak = await api.processMarkdownToHtml('a\nb', base)
        const table = await api.processMarkdownToHtml('| a |\n| - |\n| b |', base)
        const task = await api.processMarkdownToHtml('- [x] a', base)
        const strike = await api.processMarkdownToHtml('~~x~~', base)
        const single = await api.processMarkdownToHtml('~x~', base)
        const after = api
          .createBaseProcessor([{ key: 'custom', plugin: (md) => md.set({ breaks: false }) }])
          .render('a\nb', {})
        return (
          hardBreak.includes('<br') &&
          table.includes('<table') &&
          task.includes('type="checkbox"') &&
          task.includes('checked') &&
          strike.includes('<del>x</del>') &&
          !single.includes('<del>x</del>') &&
          !after.includes('<br') &&
          await strikeRenderingOk(api, base)
        )
      } catch {
        return false
      }
    },
  },
  {
    id: 'F2P-020',
    requirement_ids: ['R054'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const parts = await api.processMarkdownToParts('<div>x</div>', processor)
      const content = parts[0] && parts[0].content
      const strike = await api.processMarkdownToHtml('~~x~~', processor)
      return (
        parts.length === 1 &&
        parts[0].type === 'html' &&
        content &&
        typeof content === 'object' &&
        vnodeHasTag(content, 'div') &&
        vnodeText(content).includes('x') &&
        strike.includes('<del>x</del>') &&
        !strike.includes('<s>x</s>') &&
        htmlBlockTokenOk(processor) &&
        await strikeRenderingOk(api, processor)
      )
    },
  },
  {
    id: 'F2P-022',
    requirement_ids: ['R002'],
    async run(api) {
      let seen
      const entry = {
        key: 'probe',
        plugin: (_md, options) => {
          seen = options
        },
        options: { marker: 1 },
      }
      api.createBaseProcessor([entry])
      const base = api.createBaseProcessor([])
      return (
        entry.key === 'probe' &&
        entry.options &&
        entry.options.marker === 1 &&
        seen &&
        seen.marker === 1 &&
        headingTokenOk(base) &&
        renderHeadingOk(base)
      )
    },
  },
  {
    id: 'F2P-023',
    requirement_ids: ['R001'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const invalid = await api.processMarkdownToHtml('$\\frac{1}{$', processor)
      const invalidBlock = await api.processMarkdownToHtml('$$\n\\frac{1}{\n$$', processor)
      const blockClasses = [...invalidBlock.matchAll(/\bclass\s*=\s*(["'])(.*?)\1/g)]
        .map((match) => match[2].split(/\s+/))
      const base = api.createBaseProcessor([])
      return (
        invalid.includes('class="katex-error"') &&
        invalid.includes('color:#cc0000') &&
        blockClasses.some((classes) => classes.includes('katex-display')) &&
        blockClasses.some((classes) => classes.includes('katex-error')) &&
        invalidBlock.includes('color:#cc0000') &&
        tablePostconditionOk(base)
      )
    },
  },
  {
    id: 'F2P-005',
    requirement_ids: ['R049'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml('a\nb', processor)
      const table = await api.processMarkdownToHtml(TABLE, processor)
      return html.includes('<br') && alignHtmlOk(table) && alignTokenOk(processor)
    },
  },
  {
    id: 'F2P-013',
    requirement_ids: ['R001'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml(TABLE_SHAPE, processor)
      const base = api.createBaseProcessor([])
      return tableShapeOk(html) && tablePostconditionOk(base)
    },
  },
  {
    id: 'F2P-014',
    requirement_ids: ['R059'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const html = await api.processMarkdownToHtml(TABLE_SHAPE, processor)
      return tableShapeOk(html) && (await codeEscapeOk(api, processor))
    },
  },
  {
    id: 'F2P-015',
    requirement_ids: ['R059'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      return (
        middlewareOrderOk(api) &&
        (await codeEscapeOk(api, processor)) &&
        fenceTokenOk(processor)
      )
    },
  },
  {
    id: 'F2P-024',
    requirement_ids: ['R060'],
    async run(api) {
      const processor = api.createBaseProcessor(api.defaultCustomPlugins)
      const rawHtml = await api.processMarkdownToHtml(RAW_HTML, processor)
      const base = api.createBaseProcessor([])
      return (
        rawHtml.includes('<div class="x">hi</div>') &&
        rawHtml.includes('<b>raw</b>') &&
        headingTokenOk(base) &&
        await strikeRenderingOk(api, base)
      )
    },
  },
]
