import codeHtml from '../code-html.cjs'

const { codeBlockMatches } = codeHtml

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import tableAlignment from '../table-alignment.cjs'

const { matchesTableAlignment } = tableAlignment

const repo = process.argv[2]
const side = process.argv[3] || 'target'
const requested = (process.argv[4] || '').split(',').filter(Boolean)
const req = createRequire(path.join(repo, 'package.json'))

const has = (value: unknown, needle: string) =>
  typeof value === 'string' && value.includes(needle)

const engineIt = (processor: any) => {
  try {
    const tokens = processor.parse('# h', {})
    return (
      Array.isArray(tokens) &&
      tokens[0] &&
      tokens[0].type === 'heading_open' &&
      typeof processor.render === 'function'
    )
  } catch {
    return false
  }
}

const markdownItVersion = () => {
  try {
    return req('markdown-it/package.json').version
  } catch {
    return null
  }
}

const gitChangedPaths = () => {
  try {
    const run = (args: string[]) =>
      execFileSync('git', ['-C', repo, ...args], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      })
        .trim()
        .split(/\r?\n/)
        .filter(Boolean)
    return [
      ...run(['diff', '--name-only']), ...run(['diff', '--cached', '--name-only']),
      ...run(['ls-files', '--others', '--exclude-standard']),
    ].filter((file) => file !== '.harbor-result.json')
  } catch {
    return null
  }
}

const allTokens = (tokens: any[]) => {
  const out: any[] = []
  const walk = (items: any[]) => {
    for (const token of items || []) {
      out.push(token)
      if (token.children) walk(token.children)
    }
  }
  walk(tokens)
  return out
}

async function setupDom() {
  try {
    const { JSDOM } = await import('jsdom')
    const dom = new JSDOM('<!doctype html><html><body></body></html>')
    ;(globalThis as any).window = dom.window
    ;(globalThis as any).document = dom.window.document
    ;(globalThis as any).Node = dom.window.Node
    ;(globalThis as any).navigator = dom.window.navigator
  } catch {
    // The jsdom dependency is optional for checks that do not build VNodes.
  }
}

async function main() {
  await setupDom()

  let mod: any
  try {
    mod = await import(
      pathToFileURL(
        path.join(repo, 'packages/utils/markdown-parse/index.ts')
      ).href
    )
  } catch (error) {
    const result: Record<string, any> = {}
    for (const id of requested) {
      result[id] = {
        id,
        passed: false,
        status: 'INFRA_ERROR',
        error: String(error).slice(0, 500),
      }
    }
    console.log(JSON.stringify(result))
    return
  }

  const {
    createBaseProcessor,
    defaultCustomPlugins,
    processMarkdownToParts,
    processMarkdownToHtml,
    katexProcess,
  } = mod

  const base = () => createBaseProcessor([])
  const full = () => createBaseProcessor(defaultCustomPlugins)
  const engine = () =>
    engineIt(base()) && markdownItVersion() === '14.1.0'

  const checks: Record<string, () => Promise<boolean> | boolean> = {
    'F2P-049': async () => {
      const processor = full()
      const fenced = await processMarkdownToHtml(
        '```\n<div>x</div>\n```',
        processor
      )
      const raw = await processMarkdownToHtml('<div>x</div>', processor)
      return (
        engine() &&
        codeBlockMatches(fenced, '&lt;div&gt;x&lt;/div&gt;') &&
        has(raw, '<div>x</div>')
      )
    },
    'F2P-050': () => {
      const processor = base()
      return (
        engine() &&
        processor.parse('# h', {})[0].type === 'heading_open' &&
        processor.render('# h').trim() === '<h1>h</h1>'
      )
    },
    'F2P-051': () => engine(),
    'F2P-052': async () => {
      const processor = base()
      const html = await processMarkdownToHtml('~~x~~', processor)
      return (
        engine() &&
        has(html, '<del>x</del>') &&
        !has(html, '<s>x</s>')
      )
    },
    'F2P-054': async () => {
      const html = await processMarkdownToHtml(
        '| L | C | R |\n| :-- | :-: | --: |\n| a | b | c |',
        full()
      )
      return (
        engine() &&
        matchesTableAlignment(html, ['left', 'center', 'right'])
      )
    },
    'F2P-055': async () => {
      const parts = await processMarkdownToParts(
        '> ```js\n> x\n> ```',
        full()
      )
      return (
        engine() &&
        parts.length === 1 &&
        parts[0].type === 'html' &&
        parts.filter((part: any) => part.type === 'code').length === 0
      )
    },
    'F2P-056': async () => {
      const html = await processMarkdownToHtml(
        '<div class="x">hi</div>\n\ninline <b>raw</b>',
        full()
      )
      return (
        engine() &&
        has(html, '<div class="x">hi</div>') &&
        has(html, '<b>raw</b>') &&
        !has(html, '&lt;b&gt;raw')
      )
    },
    'F2P-057': async () => {
      const processor = full()
      const closed = await processMarkdownToParts('```js\nx\n```', processor)
      const open = await processMarkdownToParts('```js\nx', processor)
      return (
        engine() &&
        closed[0] &&
        closed[0].type === 'code' &&
        closed[0].loading === false &&
        open[0] &&
        open[0].type === 'code' &&
        open[0].loading === true
      )
    },
    'F2P-058': async () => {
      try {
        const html = await processMarkdownToHtml('$\\frac{1}{$', full())
        return (
          engine() &&
          has(html, 'class="katex-error"') &&
          has(html, 'color:#cc0000')
        )
      } catch {
        return false
      }
    },
    'F2P-059': async () => {
      const html = await processMarkdownToHtml('~text~', full())
      return engine() && !has(html, '<del>text</del>')
    },
    'F2P-060': async () => {
      const parts = await processMarkdownToParts(
        'before\n\n```js\na\n```\n\nmiddle\n\n```\nb\n```\n\nafter',
        base()
      )
      const code = parts.filter((part: any) => part.type === 'code')
      return (
        engine() &&
        code.length === 2 &&
        has(code[0].content, 'a') &&
        has(code[1].content, 'b')
      )
    },
    'F2P-061': async () => {
      const html = await processMarkdownToHtml('~~text~~', base())
      return (
        engine() &&
        has(html, '<del>text</del>') &&
        !has(html, '<s>text</s>')
      )
    },
    'F2P-062': () => {
      const processor = base()
      const tokens = processor
        .parse('| a |\n| - |\n| b |', {})
        .map((token: any) => token.type)
      return (
        engine() &&
        tokens.includes('table_open') &&
        tokens.includes('table_close') &&
        processor.render('# h').trim() === '<h1>h</h1>'
      )
    },
    'F2P-063': async () => {
      const processor = full()
      const first = await processMarkdownToParts('a', processor)
      const second = await processMarkdownToParts('b', processor)
      const mathX = await processMarkdownToHtml('$x$', processor)
      const mathY = await processMarkdownToHtml('$y$', processor)
      const table = await processMarkdownToHtml(
        '| L | C | R |\n| :-- | :-: | --: |\n| a | b | c |',
        processor
      )
      return (
        engine() &&
        first.length > 0 &&
        second.length > 0 &&
        has(mathX, 'katex') &&
        has(mathY, 'katex') &&
        matchesTableAlignment(table, ['left', 'center', 'right'])
      )
    },
    'F2P-064': async () => {
      const htmlOptions = defaultCustomPlugins.find(
        (entry: any) => entry.key === 'remark-rehype'
      )?.options
      const katexOptions = defaultCustomPlugins.find(
        (entry: any) => entry.key === 'rehype-katex'
      )?.options
      const html = await processMarkdownToHtml('$\\frac{1}{$', full())
      return (
        engine() &&
        htmlOptions &&
        htmlOptions.allowDangerousHtml === true &&
        katexOptions &&
        has(html, 'class="katex-error"') &&
        has(html, 'color:#cc0000')
      )
    },
    'F2P-065': async () => {
      const bracket = katexProcess('\\[x\\]')
      const paren = katexProcess('\\(x\\)')
      const single = katexProcess('$a\nb$')
      const double = katexProcess('$$a\nb$$')
      const parts = await processMarkdownToParts(
        '> ```js\n> x\n> ```',
        full()
      )
      return (
        engine() &&
        bracket === '$$x$$' &&
        paren === '$x$' &&
        single.includes('$$a\nb$$') &&
        double.includes('\n$$a\nb$$\n') &&
        parts.filter((part: any) => part.type === 'code').length === 0 &&
        parts.length === 1 &&
        parts[0].type === 'html'
      )
    },
    'F2P-066': async () => {
      const processor = base()
      const html = await processMarkdownToHtml('~~x~~', processor)
      return (
        engine() &&
        has(html, '<del>x</del>') &&
        !has(html, '<s>x</s>') &&
        processor.parse('# h', {})[0].type === 'heading_open'
      )
    },
    'F2P-067': async () => {
      const processor = full()
      const inline = await processMarkdownToHtml('$x$', processor)
      const block = await processMarkdownToHtml('$$\nx\n$$', processor)
      return (
        engine() &&
        has(inline, 'class="katex"') &&
        has(block, 'class="katex-display"')
      )
    },
    'F2P-068': () => {
      try {
        const pkg = req('./package.json')
        const lock = req('yaml').parse(fs.readFileSync(path.join(repo, 'pnpm-lock.yaml'), 'utf8'))
        const changed = gitChangedPaths()
        if (changed) {
          const allowed = (file: string) =>
            file === 'package.json' ||
            file === 'pnpm-lock.yaml' ||
            (file.startsWith('packages/utils/markdown-parse/') &&
              file !== 'packages/utils/markdown-parse/hast-to-vnode.ts')
          if (changed.some((file) => !allowed(file))) return false
        }
        return (
          engine() &&
          typeof pkg.dependencies?.['markdown-it'] === 'string' && Boolean(pkg.dependencies['markdown-it']) &&
          lock.importers?.['.']?.dependencies?.['markdown-it']?.specifier === pkg.dependencies['markdown-it'] &&
          lock.importers?.['.']?.dependencies?.['markdown-it']?.version === '14.1.0' &&
          fs.existsSync(path.join(repo, 'packages/components/markdown/index.vue')) &&
          fs.existsSync(path.join(repo, 'packages/hooks/index.ts'))
        )
      } catch {
        return false
      }
    },
    'F2P-069': async () => {
      const html = await processMarkdownToHtml('<div>x</div>', full())
      return engine() && html.trim() === '<div>x</div>'
    },
    'F2P-070': () => {
      try {
        const moduleRoot = path.join(repo, 'packages/utils/markdown-parse')
        const walk = (dir: string): string[] => fs.readdirSync(dir, { withFileTypes: true })
          .flatMap((entry) => entry.isDirectory() ? walk(path.join(dir, entry.name)) :
            entry.isFile() && /\.[cm]?[jt]sx?$/.test(entry.name) && entry.name !== 'hast-to-vnode.ts'
              ? [path.join(dir, entry.name)] : [])
        const files = walk(moduleRoot)
        const source = files.map((file) => fs.readFileSync(file, 'utf8')).join('\n')
        const banned =
          /from\s+['"](?:unified|remark-[^'"]*|rehype-katex|hast-util-to-html)['"]/
        return engine() && !banned.test(source)
      } catch {
        return false
      }
    },
    'F2P-071': async () => {
      const html = await processMarkdownToHtml(
        '*[HTML]: HyperText Markup Language\n\nHTML XHTML',
        full()
      )
      return (
        engine() &&
        has(html, '<abbr title="HyperText Markup Language">HTML</abbr>') &&
        !html.includes('XHTML</abbr>')
      )
    },
    'F2P-072': () => {
      try {
        const pkg = req('./package.json')
        const lock = req('yaml').parse(fs.readFileSync(path.join(repo, 'pnpm-lock.yaml'), 'utf8'))
        const root = lock.importers?.['.']
        const html = base().render('*x*')
        return (
          engine() &&
          typeof pkg.dependencies?.['markdown-it'] === 'string' && Boolean(pkg.dependencies['markdown-it']) &&
          Boolean(pkg.devDependencies?.['@types/markdown-it']) &&
          root?.dependencies?.['markdown-it']?.specifier === pkg.dependencies?.['markdown-it'] &&
          root?.dependencies?.['markdown-it']?.version === '14.1.0' &&
          root?.devDependencies?.['@types/markdown-it']?.specifier === pkg.devDependencies?.['@types/markdown-it'] &&
          Boolean(lock.packages?.['markdown-it@14.1.0']?.resolution?.integrity) &&
          markdownItVersion() === '14.1.0' &&
          html.trim() === '<p><em>x</em></p>'
        )
      } catch {
        return false
      }
    },
  }

  const result: Record<string, any> = {}
  for (const id of requested) {
    const check = checks[id]
    if (!check) {
      result[id] = { id, passed: false, status: 'INCONCLUSIVE', error: 'unknown observation' }
      continue
    }
    try {
      result[id] = { id, passed: !!(await check()), side }
    } catch (error) {
      result[id] = {
        id,
        passed: false,
        side,
        error: String(error).slice(0, 500),
      }
    }
  }
  console.log(JSON.stringify(result))
}

main()
