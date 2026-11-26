import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'

export const BATCH_ID = 'BATCH-DIFFERENTIAL-0002'

export const OBSERVATION_IDS = [
  'DIFF-033',
  'DIFF-034',
  'DIFF-035',
  'DIFF-036',
  'DIFF-037',
  'DIFF-038',
  'DIFF-039',
  'DIFF-040',
  'DIFF-041',
  'DIFF-042',
]

export const CASE_CORPUS = {
  'DIFF-033': {
    inline: '$x$',
    block: '$$\nx\n$$',
  },
  'DIFF-034': {
    formula: '$\\RR$',
  },
  'DIFF-035': {
    formula: '$\\frac{1}{$',
  },
  'DIFF-036': {
    content: '<div class="x">hi</div>\n\ninline <b>raw</b>',
  },
  'DIFF-037': {
    content: '<div>x</div>',
  },
  'DIFF-038': {
    parts: ['a', 'b'],
    html: ['$x$', '$y$'],
  },
  'DIFF-039': {
    bracketInput: '\\[x\\]',
    parenInput: '\\(x\\)',
  },
  'DIFF-040': {
    roots: [
      'packages/utils/markdown-parse/hast-to-vnode.ts',
      'packages/utils/code-highlight.ts',
      'packages/utils/code-mermaid.ts',
      'packages/components',
      'packages/hooks',
      'packages/theme-chalk',
      'docs',
    ],
    expectedFileCount: 262,
    expectedTreeHash:
      '59009974dadb278ef5b3132bfa1570d3e236ad9a86f8c12b44a09a3cc211ca0a',
  },
  'DIFF-041': {
    expectedFileCount: 1,
    expectedTreeHash:
      '157a09abffa9951870860135f7c61082ff2cef3c310bdf5191d3bf2a79c80537',
    expectedSkippedAssertions: 0,
  },
  'DIFF-042': {
    files: {
      markdown: 'packages/components/markdown/props.ts',
      mdocEditor: 'packages/components/m-doc-editor/props.ts',
    },
  },
}

const RUNTIME_EXPORTS = [
  'createBaseProcessor',
  'defaultCustomPlugins',
  'katexProcess',
  'processMarkdownToHtml',
  'processMarkdownToParts',
]

const TYPE_EXPORTS = [
  'MiddlewarePluginItem',
  'MarkdownPart',
  'ProcessorType',
  'ProcessMarkdownOptions',
]

const TEST_FILE_PATTERN = /\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$/
const SKIPPED_ASSERTION_PATTERN = /\b(?:it|test|describe|suite)\.(?:skip|todo|only)\s*\(/g

const normalizeText = (value) => String(value).replace(/\r\n/g, '\n').trim()

const stripVueFragments = (value) =>
  String(value)
    .replace(/<!--\[-->/g, '')
    .replace(/<!--\]-->/g, '')

const classTokens = (html) => {
  const tokens = []
  const pattern = /class="([^"]*)"/g
  let match
  while ((match = pattern.exec(String(html))) !== null) {
    tokens.push(...match[1].split(/\s+/).filter(Boolean))
  }
  return tokens
}

const hasClass = (html, token) => classTokens(html).includes(token)

const sortValue = (value) => {
  if (Array.isArray(value)) return value.map(sortValue)
  if (value && typeof value === 'object') {
    return Object.keys(value)
      .sort()
      .reduce((result, key) => {
        result[key] = sortValue(value[key])
        return result
      }, {})
  }
  return value
}

const stableJson = (value) => JSON.stringify(sortValue(value))

const readText = (repoRoot, relativePath) =>
  fs.readFileSync(path.join(repoRoot, relativePath), 'utf8')

const walkFiles = (repoRoot, relativePath, skipDirectories) => {
  const absolutePath = path.join(repoRoot, relativePath)
  if (!fs.existsSync(absolutePath)) return []

  const stat = fs.statSync(absolutePath)
  if (stat.isFile()) return [relativePath]
  if (!stat.isDirectory()) return []

  const files = []
  const entries = fs
    .readdirSync(absolutePath, { withFileTypes: true })
    .sort((left, right) => left.name.localeCompare(right.name))

  for (const entry of entries) {
    if (entry.isDirectory() && skipDirectories.has(entry.name)) continue
    files.push(
      ...walkFiles(
        repoRoot,
        path.join(relativePath, entry.name),
        skipDirectories
      )
    )
  }
  return files
}

const hashFileList = (repoRoot, relativeFiles) => {
  const sortedFiles = [...new Set(relativeFiles)].sort()
  const hash = crypto.createHash('sha256')
  for (const relativePath of sortedFiles) {
    hash.update(relativePath.replaceAll('\\', '/'))
    hash.update('\0')
    hash.update(fs.readFileSync(path.join(repoRoot, relativePath)))
    hash.update('\0')
  }
  return {
    fileCount: sortedFiles.length,
    treeHash: hash.digest('hex'),
  }
}

const hashRoots = (repoRoot, roots) => {
  const files = []
  for (const root of roots) {
    files.push(...walkFiles(repoRoot, root, new Set(['node_modules', '.git'])))
  }
  return hashFileList(repoRoot, files)
}

const findTestEvidence = (repoRoot) => {
  const files = walkFiles(
    repoRoot,
    '.',
    new Set(['node_modules', '.git', 'dist', 'coverage'])
  ).filter((relativePath) => TEST_FILE_PATTERN.test(relativePath))

  const skippedAssertions = files.reduce((count, relativePath) => {
    const content = readText(repoRoot, relativePath)
    const matches = content.match(SKIPPED_ASSERTION_PATTERN)
    return count + (matches ? matches.length : 0)
  }, 0)

  return {
    ...hashFileList(repoRoot, files),
    files: files.map((file) => file.replaceAll('\\', '/')).sort(),
    skippedAssertions,
  }
}

const matchesMiddlewareProp = (source) =>
  /import\s*\{[^}]*\bMiddlewarePluginItem\b[^}]*\}\s*from\s*['"]@element-ai-vue\/utils['"]/.test(
    source
  ) &&
  /remarkPlugins\s*:\s*\{[\s\S]*?type\s*:\s*Array\s+as\s+PropType<\s*MiddlewarePluginItem\[\]\s*>/.test(
    source
  )

export const assertObservation = (observationId, result) => {
  const expected = {
    'DIFF-033': (value) =>
      value.inlineMathHasKatexClass === true &&
      value.blockMathHasKatexDisplayWrapper === true,
    'DIFF-034': (value) =>
      value.rrMacroRendersBlackboardR === true &&
      value.katexOptionsEqualConfig === true &&
      value.throwOnError === false &&
      value.errorColor === '#cc0000' &&
      value.strict === false,
    'DIFF-035': (value) =>
      value.invalidFormulaResolved === true &&
      value.hasKatexErrorClass === true &&
      value.hasConfiguredErrorColor === true,
    'DIFF-036': (value) =>
      value.rawHtmlBlockUnchanged === true &&
      value.rawInlineHtmlUnchanged === true &&
      value.containsEscapedRawHtml === false,
    'DIFF-037': (value) =>
      value.partCount === 1 &&
      value.firstPartType === 'html' &&
      value.contentIsVNode === true &&
      value.rawHtmlPreserved === true &&
      value.containsEscapedRawHtml === false,
    'DIFF-038': (value) =>
      value.firstPartsCallIndependent === true &&
      value.secondPartsCallIndependent === true &&
      value.firstHtmlCallHasXMath === true &&
      value.secondHtmlCallHasYMath === true,
    'DIFF-039': (value) =>
      value.runtimeExportsPresent === true &&
      value.typeExportsPresent === true &&
      value.packageReexportPresent === true &&
      value.katexProcessBracket === '$$x$$' &&
      value.katexProcessParen === '$x$',
    'DIFF-040': (value) =>
      value.fileCount === CASE_CORPUS['DIFF-040'].expectedFileCount &&
      value.treeHash === CASE_CORPUS['DIFF-040'].expectedTreeHash,
    'DIFF-041': (value) =>
      value.testFileCount === CASE_CORPUS['DIFF-041'].expectedFileCount &&
      value.testTreeHash === CASE_CORPUS['DIFF-041'].expectedTreeHash &&
      value.skippedAssertions ===
        CASE_CORPUS['DIFF-041'].expectedSkippedAssertions,
    'DIFF-042': (value) =>
      value.markdownRemarkPluginsProp === true &&
      value.mdocEditorRemarkPluginsProp === true &&
      value.renamedMarkdownItPropAbsent === true,
  }

  return Boolean(expected[observationId] && expected[observationId](result))
}

export const runCorpus = async ({
  repoRoot,
  api,
  katexConfig,
  renderVNode,
}) => {
  const results = {}
  const createProcessor = () => api.createBaseProcessor(api.defaultCustomPlugins)

  const renderPart = async (part) => {
    if (!part) return ''
    return normalizeText(
      stripVueFragments(await renderVNode(part.content))
    )
  }

  {
    const processor = createProcessor()
    const inlineHtml = await api.processMarkdownToHtml(
      CASE_CORPUS['DIFF-033'].inline,
      processor
    )
    const blockHtml = await api.processMarkdownToHtml(
      CASE_CORPUS['DIFF-033'].block,
      processor
    )
    results['DIFF-033'] = {
      inlineMathHasKatexClass: hasClass(inlineHtml, 'katex'),
      blockMathHasKatexDisplayWrapper:
        hasClass(blockHtml, 'katex-display') && hasClass(blockHtml, 'katex'),
    }
  }

  {
    const processor = createProcessor()
    const html = await api.processMarkdownToHtml(
      CASE_CORPUS['DIFF-034'].formula,
      processor
    )
    const katexEntry = api.defaultCustomPlugins.find(
      (entry) => entry.key === 'rehype-katex'
    )
    const options = (katexEntry && katexEntry.options) || {}
    results['DIFF-034'] = {
      rrMacroRendersBlackboardR:
        html.includes('mathvariant="double-struck"') ||
        html.includes('mord mathbb'),
      katexOptionsEqualConfig:
        stableJson(options) === stableJson(katexConfig),
      throwOnError: options.throwOnError,
      errorColor: options.errorColor,
      strict: options.strict,
    }
  }

  {
    const processor = createProcessor()
    let resolved = true
    let html = ''
    try {
      html = await api.processMarkdownToHtml(
        CASE_CORPUS['DIFF-035'].formula,
        processor
      )
    } catch {
      resolved = false
    }
    results['DIFF-035'] = {
      invalidFormulaResolved: resolved,
      hasKatexErrorClass: hasClass(html, 'katex-error'),
      hasConfiguredErrorColor: /style="[^"]*color\s*:\s*#cc0000/i.test(html),
    }
  }

  {
    const processor = createProcessor()
    const html = await api.processMarkdownToHtml(
      CASE_CORPUS['DIFF-036'].content,
      processor
    )
    results['DIFF-036'] = {
      rawHtmlBlockUnchanged: html.includes('<div class="x">hi</div>'),
      rawInlineHtmlUnchanged: html.includes('<b>raw</b>'),
      containsEscapedRawHtml:
        html.includes('&lt;div') || html.includes('&lt;b&gt;'),
    }
  }

  {
    const processor = createProcessor()
    const parts = await api.processMarkdownToParts(
      CASE_CORPUS['DIFF-037'].content,
      processor
    )
    const rendered = await renderPart(parts[0])
    results['DIFF-037'] = {
      partCount: parts.length,
      firstPartType: parts[0] ? parts[0].type : null,
      contentIsVNode: Boolean(
        parts[0] && parts[0].content && typeof parts[0].content === 'object'
      ),
      rawHtmlPreserved: rendered.includes('<div>x</div>'),
      containsEscapedRawHtml: rendered.includes('&lt;div&gt;'),
    }
  }

  {
    const processor = createProcessor()
    const firstParts = await api.processMarkdownToParts(
      CASE_CORPUS['DIFF-038'].parts[0],
      processor
    )
    const secondParts = await api.processMarkdownToParts(
      CASE_CORPUS['DIFF-038'].parts[1],
      processor
    )
    const firstHtml = await api.processMarkdownToHtml(
      CASE_CORPUS['DIFF-038'].html[0],
      processor
    )
    const secondHtml = await api.processMarkdownToHtml(
      CASE_CORPUS['DIFF-038'].html[1],
      processor
    )
    const firstRendered = await renderPart(firstParts[0])
    const secondRendered = await renderPart(secondParts[0])
    results['DIFF-038'] = {
      firstPartsCallIndependent:
        firstRendered.includes('<p>a</p>') &&
        !firstRendered.includes('<p>b</p>'),
      secondPartsCallIndependent:
        secondRendered.includes('<p>b</p>') &&
        !secondRendered.includes('<p>a</p>'),
      firstHtmlCallHasXMath:
        firstHtml.includes('application/x-tex">x<') &&
        hasClass(firstHtml, 'katex'),
      secondHtmlCallHasYMath:
        secondHtml.includes('application/x-tex">y<') &&
        hasClass(secondHtml, 'katex'),
    }
  }

  {
    const indexSource = readText(
      repoRoot,
      'packages/utils/markdown-parse/index.ts'
    )
    const utilsIndexSource = readText(repoRoot, 'packages/utils/index.ts')
    results['DIFF-039'] = {
      runtimeExportsPresent: RUNTIME_EXPORTS.every(
        (name) => typeof api[name] !== 'undefined'
      ),
      typeExportsPresent: TYPE_EXPORTS.every((name) =>
        new RegExp(`export\\s+(?:interface|type)\\s+${name}\\b`).test(
          indexSource
        )
      ),
      packageReexportPresent:
        /export\s+\*\s+from\s+['"]\.\/markdown-parse['"]/.test(
          utilsIndexSource
        ),
      katexProcessBracket: api.katexProcess(
        CASE_CORPUS['DIFF-039'].bracketInput
      ),
      katexProcessParen: api.katexProcess(CASE_CORPUS['DIFF-039'].parenInput),
    }
  }

  {
    results['DIFF-040'] = hashRoots(
      repoRoot,
      CASE_CORPUS['DIFF-040'].roots
    )
  }

  {
    const testEvidence = findTestEvidence(repoRoot)
    results['DIFF-041'] = {
      testFileCount: testEvidence.fileCount,
      testTreeHash: testEvidence.treeHash,
      skippedAssertions: testEvidence.skippedAssertions,
      testFiles: testEvidence.files,
    }
  }

  {
    const markdownSource = readText(
      repoRoot,
      CASE_CORPUS['DIFF-042'].files.markdown
    )
    const mdocEditorSource = readText(
      repoRoot,
      CASE_CORPUS['DIFF-042'].files.mdocEditor
    )
    results['DIFF-042'] = {
      markdownRemarkPluginsProp: matchesMiddlewareProp(markdownSource),
      mdocEditorRemarkPluginsProp: matchesMiddlewareProp(mdocEditorSource),
      renamedMarkdownItPropAbsent:
        !/\bmarkdownItPlugins\s*:/.test(markdownSource) &&
        !/\bmarkdownItPlugins\s*:/.test(mdocEditorSource),
    }
  }

  return results
}
