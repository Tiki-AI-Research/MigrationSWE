const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { isDeepStrictEqual } = require('node:util')
const { execFileSync } = require('node:child_process')
const { createRequire } = require('node:module')
const ts = require('/opt/migration/dependencies/node_modules/typescript')
const yaml = require('/opt/migration/dependencies/node_modules/yaml')

const root = path.resolve(process.argv[2])
const moduleRoot = 'packages/utils/markdown-parse/'
const excluded = `${moduleRoot}hast-to-vnode.ts`
const sourcePackage = (name) => /^(?:unified|remark(?:-.+)?|rehype(?:-.+)?|hast-util-to-html)(?:\/|$)/.test(name)
const sourcePackagePath = (name) => {
  if (typeof name !== 'string') return false
  const normalized = name.replace(/^file:\/\//, '').replaceAll('\\', '/')
  const match = normalized.match(/(?:^|\/)node_modules\/(?:@[^/]+\/)?([^/]+)/)
  return sourcePackage(match ? `${normalized.includes('/node_modules/@') ? normalized.split('/node_modules/').pop() : match[1]}` : name)
}
const bridgePackage = (name) => typeof name === 'string' &&
  (/^(?:node:)?(?:child_process|worker_threads|vm|http|https|net|tls|dgram)$/.test(name) || /^(?:ffi|ffi-napi|koffi)$/.test(name))
const typeOnlyDeclaration = (node) => {
  const clause = ts.isImportDeclaration(node) ? node.importClause : node
  if (clause?.isTypeOnly) return true
  const named = ts.isImportDeclaration(node) ? clause?.namedBindings : node.exportClause
  return !clause?.name && named && (ts.isNamedImports(named) || ts.isNamedExports(named)) &&
    named.elements.length > 0 && named.elements.every((item) => item.isTypeOnly)
}
const read = (file) => fs.readFileSync(file, 'utf8')
const relative = (file) => path.relative(root, file).split(path.sep).join('/')
const codeFile = (file) => /\.[cm]?[jt]sx?$/.test(file)
const allowed = (file) => ['package.json', 'pnpm-lock.yaml'].includes(file) ||
  (file.startsWith(moduleRoot) && file !== excluded)

function walk(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    if (['.git', 'node_modules'].includes(entry.name)) return []
    const file = path.join(directory, entry.name)
    return entry.isDirectory() ? walk(file) : [file]
  })
}

function changes() {
  const listing = execFileSync('git', ['-C', root, 'ls-tree', '-rz', 'HEAD'], { encoding: 'utf8' })
  const tracked = new Map(listing.split('\0').filter(Boolean).map((record) => {
    const [metadata, file] = record.split('\t')
    const [mode, , hash] = metadata.split(' ')
    return [file, { mode, hash }]
  }))
  const changed = new Set()
  for (const file of walk(root)) {
    const name = relative(file)
    const previous = tracked.get(name)
    tracked.delete(name)
    const stat = fs.lstatSync(file)
    const data = stat.isSymbolicLink() ? Buffer.from(fs.readlinkSync(file)) : fs.readFileSync(file)
    const hash = crypto.createHash('sha1').update(`blob ${data.length}\0`).update(data).digest('hex')
    const mode = stat.isSymbolicLink() ? '120000' : stat.mode & 0o111 ? '100755' : '100644'
    if (!previous || previous.hash !== hash || previous.mode !== mode) changed.add(name)
  }
  for (const name of tracked.keys()) changed.add(name)
  return [...changed].sort()
}

function dependencies() {
  const errors = []
  try {
    const lock = yaml.parse(read(path.join(root, 'pnpm-lock.yaml')))
    const trusted = yaml.parse(read('/opt/migration/build/pnpm-lock.yaml'))
    if (!lock.importers?.['.']) errors.push('missing root lockfile importer')
    for (const [name, entry] of Object.entries(lock.packages || {})) {
      const resolution = trusted.packages?.[name]?.resolution
      if (!resolution || !isDeepStrictEqual(entry.resolution, resolution)) {
        errors.push(`${name}: resolution does not match authenticated offline package metadata`)
      }
    }
    if (!Object.keys(lock.packages || {}).length) errors.push('missing locked packages')
    const manifest = JSON.parse(read(path.join(root, 'package.json')))
    const specifier = manifest.dependencies?.['markdown-it']
    const locked = lock.importers?.['.']?.dependencies?.['markdown-it']
    // A compatible declaration is valid when the frozen lock resolves the
    // required engine version. The execution probes also verify its version.
    if (typeof specifier !== 'string' || !specifier ||
        locked?.specifier !== specifier || locked?.version !== '14.1.0' ||
        !manifest.devDependencies?.['@types/markdown-it']) {
      errors.push('required target dependencies missing or not locked to markdown-it 14.1.0')
    }
  } catch (error) {
    errors.push(String(error))
  }
  process.stdout.write(JSON.stringify({ errors }))
  process.exitCode = errors.length ? 1 : 0
}

function sources() {
  const changed = changes()
  const ordinary = []
  const deceptive = []
  const syntax = []
  const visited = new Set()
  const roots = [...walk(path.join(root, moduleRoot)), ...changed.map((file) => path.join(root, file))]
    .filter((file) => (relative(file).startsWith(moduleRoot) || codeFile(file)) && relative(file) !== excluded && fs.existsSync(file))
  const capabilities = []
  const hit = (file, node, token, kind) => ({
    path: relative(file), token, kind, rule_id: 'typescript-module-graph',
    line: ts.getLineAndCharacterOfPosition(node.getSourceFile(), node.getStart()).line + 1,
  })
  function inspect(file) {
    if (visited.has(file) || relative(file) === excluded) return
    visited.add(file)
    if (fs.lstatSync(file).isSymbolicLink()) {
      deceptive.push({ path: relative(file), token: 'symlinked production module', kind: 'module-graph' })
      return
    }
    const tree = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true,
      codeFile(file) ? ts.getScriptKindFromFileName(file) : ts.ScriptKind.TS)
    if (codeFile(file)) syntax.push(...tree.parseDiagnostics.map((diagnostic) => ({
      path: relative(file), message: ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'),
    })))
    const bindings = new Map()
    const loaders = new Set(['require'])
    const factories = new Set()
    const moduleNamespaces = new Set()
    const bridgeBindings = new Set()
    const destructured = []
    function collect(node) {
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) bindings.set(node.name.text, node.initializer)
      if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer) destructured.push(node)
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier) && !typeOnlyDeclaration(node)) {
        if (bridgePackage(node.moduleSpecifier.text) && !node.importClause?.isTypeOnly) {
          if (node.importClause?.name) bridgeBindings.add(node.importClause.name.text)
          const named = node.importClause?.namedBindings
          if (named && ts.isNamedImports(named)) {
            for (const item of named.elements) if (!item.isTypeOnly) bridgeBindings.add(item.name.text)
          } else if (named) bridgeBindings.add(named.name.text)
        }
        if (['module', 'node:module'].includes(node.moduleSpecifier.text)) {
          if (node.importClause?.name) moduleNamespaces.add(node.importClause.name.text)
          const named = node.importClause?.namedBindings
          if (named && ts.isNamedImports(named)) {
            for (const item of named.elements) if ((item.propertyName || item.name).text === 'createRequire') factories.add(item.name.text)
          } else if (named) moduleNamespaces.add(named.name.text)
        }
      }
      ts.forEachChild(node, collect)
    }
    collect(tree)
    const property = (node) => ts.isPropertyAccessExpression(node) ? node.name.text :
      ts.isElementAccessExpression(node) ? value(node.argumentExpression) : undefined
    function value(node, seen = new Set()) {
      if (!node) return undefined
      if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) return node.text
      if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isNonNullExpression(node)) return value(node.expression, seen)
      if (ts.isIdentifier(node) && bindings.has(node.text) && !seen.has(node.text)) return value(bindings.get(node.text), new Set([...seen, node.text]))
      if (ts.isArrayLiteralExpression(node)) return node.elements.map((item) => value(item, seen))
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
        const left = value(node.left, seen), right = value(node.right, seen)
        if (typeof left === 'string' && typeof right === 'string') return left + right
      }
      if (ts.isTemplateExpression(node)) {
        let result = node.head.text
        for (const span of node.templateSpans) {
          const part = value(span.expression, seen)
          if (typeof part !== 'string') return undefined
          result += part + span.literal.text
        }
        return result
      }
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'join') {
        const items = value(node.expression.expression, seen), separator = value(node.arguments[0], seen) ?? ','
        if (Array.isArray(items) && items.every((item) => typeof item === 'string')) return items.join(separator)
      }
      return undefined
    }
    function namespace(node) {
      return (ts.isIdentifier(node) && moduleNamespaces.has(node.text)) ||
        (ts.isCallExpression(node) && loader(node.expression) && ['module', 'node:module'].includes(value(node.arguments[0])))
    }
    function factory(node) {
      return (ts.isIdentifier(node) && factories.has(node.text)) ||
        ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) && property(node) === 'createRequire' && namespace(node.expression))
    }
    function reflectMethod(node, name) {
      return ts.isPropertyAccessExpression(node) &&
        ts.isIdentifier(node.expression) && node.expression.text === 'Reflect' && node.name.text === name
    }
    function indirectLoader(node) {
      if (!ts.isCallExpression(node)) return undefined
      if (reflectMethod(node.expression, 'apply') || reflectMethod(node.expression, 'construct')) {
        return loader(node.arguments[0]) ? node.arguments[2] : undefined
      }
      if (ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression)) {
        const method = property(node.expression)
        if (['call', 'apply'].includes(method) && loader(node.expression.expression)) {
          return method === 'apply' ? node.arguments[1] : node.arguments[1]
        }
      }
      return undefined
    }
    function loader(node) {
      return (ts.isIdentifier(node) && loaders.has(node.text)) ||
        ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) && property(node) === 'require' && ts.isIdentifier(node.expression) && node.expression.text === 'module') ||
        (ts.isCallExpression(node) && factory(node.expression)) ||
        (ts.isPropertyAccessExpression(node) && property(node) === 'bind' && loader(node.expression))
    }
    function builtinModule(node) {
      return ts.isCallExpression(node) &&
        ts.isPropertyAccessExpression(node.expression) &&
        ts.isIdentifier(node.expression.expression) && node.expression.expression.text === 'process' &&
        node.expression.name.text === 'getBuiltinModule'
    }
    function bridge(node) {
      return (ts.isIdentifier(node) && bridgeBindings.has(node.text)) ||
        ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) && bridge(node.expression)) ||
        (ts.isCallExpression(node) && loader(node.expression) && bridgePackage(value(node.arguments[0]))) ||
        (builtinModule(node) && bridgePackage(value(node.arguments[0])))
    }
    for (let pass = 0; pass <= bindings.size + destructured.length; pass += 1) {
      for (const node of destructured) {
        if (bridge(node.initializer)) {
          for (const item of node.name.elements) if (ts.isIdentifier(item.name)) bridgeBindings.add(item.name.text)
        }
        if (!namespace(node.initializer)) continue
        for (const item of node.name.elements) {
          if (ts.isIdentifier(item.name) && (item.propertyName || item.name).text === 'createRequire') factories.add(item.name.text)
        }
      }
      for (const [name, node] of bindings) {
        if (namespace(node)) moduleNamespaces.add(name)
        if (factory(node)) factories.add(name)
        if (loader(node)) loaders.add(name)
        if (bridge(node)) bridgeBindings.add(name)
      }
    }
    function dependency(name, node, dynamic) {
      if (typeof name !== 'string') {
        deceptive.push(hit(file, node, 'unresolved runtime module loader', 'dynamic-loader'))
        return
      }
      if (sourcePackage(name) || sourcePackagePath(name)) (dynamic ? deceptive : ordinary).push(hit(file, node, name, dynamic ? 'dynamic-source-loader' : 'source-import'))
      if (bridgePackage(name)) {
        capabilities.push(hit(file, node, name, 'runtime-bridge'))
      }
      if (name.startsWith('.')) {
        const base = path.resolve(path.dirname(file), name)
        const target = [base, ...['.ts', '.tsx', '.js', '.mjs', '.cjs', '/index.ts', '/index.js'].map((suffix) => base + suffix)]
          .find((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile())
        if (target && !relative(target).startsWith('../')) inspect(target)
      }
    }
    function visit(node) {
      if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
        if (!typeOnlyDeclaration(node)) dependency(value(node.moduleSpecifier), node, false)
      }
      if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
        const callee = node.expression
        const indirectArguments = ts.isCallExpression(node) ? indirectLoader(node) : undefined
        if (ts.isCallExpression(node) && (callee.kind === ts.SyntaxKind.ImportKeyword || loader(callee) || indirectArguments)) {
          const staticRequire = ts.isIdentifier(callee) && callee.text === 'require' && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])
          let argument = node.arguments[0]
          if (indirectArguments) {
            argument = ts.isArrayLiteralExpression(indirectArguments) ? indirectArguments.elements[0] : indirectArguments
          }
          dependency(value(argument), node, !staticRequire || Boolean(indirectArguments))
        }
        if (bridge(callee) ||
            (ts.isIdentifier(callee) && ['eval', 'Function', 'fetch', 'WebSocket', 'Worker', 'SharedWorker'].includes(callee.text)) ||
            (ts.isPropertyAccessExpression(callee) && callee.expression.getText(tree) === 'process' && ['dlopen', 'getBuiltinModule'].includes(callee.name.text))) {
          capabilities.push({
            ...hit(file, node, callee.getText(tree), 'runtime-bridge'),
            arguments: Array.from(node.arguments || [], (argument) => value(argument) ?? argument.getText(tree)),
          })
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(tree)
  }
  for (const file of roots) inspect(file)
  deceptive.push(...capabilities)
  const candidateRequire = createRequire(path.join(root, 'package.json'))
  const source_runtime = ['unified', 'remark-parse', 'remark-rehype', 'rehype-katex', 'hast-util-to-html'].map((name) => {
    try { return { name, available: true, resolved: candidateRequire.resolve(name) } }
    catch { return { name, available: false } }
  })
  process.stdout.write(JSON.stringify({ ordinary, deceptive, capabilities, source_runtime, scanned: [...visited].map(relative).sort(), syntax, changed, out_of_scope: changed.filter((file) => !allowed(file)) }))
  process.exitCode = syntax.length ? 1 : 0
}

if (process.argv.includes('--dependencies')) dependencies()
else sources()
