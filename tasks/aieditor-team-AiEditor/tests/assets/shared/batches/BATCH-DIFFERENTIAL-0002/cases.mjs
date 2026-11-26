import {createHash} from 'node:crypto'
import {existsSync, readFileSync, readdirSync, statSync} from 'node:fs'
import {join, relative, sep} from 'node:path'

const OBSERVATION_IDS = [
  'DIFF-033', 'DIFF-034', 'DIFF-035', 'DIFF-036', 'DIFF-037',
  'DIFF-038', 'DIFF-039', 'DIFF-040', 'DIFF-041', 'DIFF-042',
  'DIFF-043', 'DIFF-044', 'DIFF-045', 'DIFF-046',
]
const ALLOWED_CHANGE_PATHS = new Set([
  'src/ai/OpenAiService.ts',
  'src/ai/index.ts',
  'src/ai/Providers.ts',
  'src/ai/types.ts',
  'src/ai/editor-tools.ts',
  'package.json',
  'package-lock.json',
])
const SKIP_DIRS = new Set(['.git', 'node_modules', 'dist', 'coverage', '.vite'])

function fail(message) {
  throw new Error(message)
}

function equal(actual, expected, label) {
  const left = JSON.stringify(actual)
  const right = JSON.stringify(expected)
  if (left !== right) fail(`${label}: expected ${right}, received ${left}`)
}

function truth(value, label) {
  if (!value) fail(label)
}

function proposal(overrides = {}) {
  return {
    id: 'proposal-diff',
    tool: 'insert_content',
    title: 'Insert content',
    description: 'Insert AI-generated content.',
    arguments: {position: 'document_end', content: 'x', _insert_position: 1},
    documentVersion: 1,
    status: 'pending',
    ...overrides,
  }
}

function context(overrides = {}) {
  return {
    editor: {},
    html: '<p>doc</p>',
    text: 'doc',
    selectedText: '',
    selection: {from: 1, to: 1},
    documentVersion: 1,
    ...overrides,
  }
}

function response(text, overrides = {}) {
  return {
    id: `resp_${Math.random().toString(16).slice(2)}`,
    object: 'response',
    created_at: 0,
    status: 'completed',
    model: 'fake-model',
    output_text: text,
    output: [{
      id: 'msg_1',
      type: 'message',
      role: 'assistant',
      status: 'completed',
      content: [{type: 'output_text', text, annotations: []}],
    }],
    usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2},
    ...overrides,
  }
}

function fakeClient(handler) {
  const calls = []
  return {
    calls,
    client: {
      responses: {
        async create(request, options) {
          calls.push({request, options})
          return handler(request, options, calls)
        },
      },
    },
  }
}

function walkFiles(root, base = root, out = []) {
  for (const entry of readdirSync(root, {withFileTypes: true})) {
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue
      walkFiles(join(root, entry.name), base, out)
    } else if (entry.isFile()) {
      out.push(relative(base, join(root, entry.name)).split(sep).join('/'))
    }
  }
  return out.sort()
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

function isRejected(promise) {
  return promise.then(
    () => ({rejected: false}),
    (error) => ({rejected: true, error}),
  )
}

async function diff033(ai) {
  const applied = proposal({status: 'applied'})
  const discarded = proposal({status: 'discarded'})
  const editor = {changed: false}
  const appliedResult = ai.applyEditorToolProposal(editor, 1, applied)
  const discardedResult = ai.applyEditorToolProposal(editor, 1, discarded)
  equal(appliedResult, {ok: false, message: 'This proposal is no longer pending.'}, 'applied proposal result')
  equal(discardedResult, {ok: false, message: 'This proposal is no longer pending.'}, 'discarded proposal result')
  equal(editor.changed, false, 'non-pending proposals must not touch the editor')
  equal([applied.status, discarded.status], ['applied', 'discarded'], 'non-pending statuses must be preserved')
  return {appliedResult, discardedResult}
}

async function diff034(ai) {
  const pending = proposal({documentVersion: 2, status: 'pending'})
  const editor = {changed: false}
  const result = ai.applyEditorToolProposal(editor, 3, pending)
  equal(result, {ok: false, message: 'The document changed. Ask AI to create a new proposal.'}, 'stale documentVersion result')
  equal(editor.changed, false, 'stale proposals must not touch the editor')
  equal(pending.status, 'pending', 'stale proposal must remain pending')
  return {result}
}

async function diff035(ai) {
  const applied = {text: 'original'}
  const validTool = {
    name: 'insert_content',
    definition: {type: 'function', name: 'insert_content', description: '', parameters: {type: 'object'}},
    execute() { return {output: {ok: true}} },
    apply(editor) { editor.text = 'changed' },
  }
  const validRegistry = new ai.EditorToolRegistry([validTool])
  const validProposal = proposal({documentVersion: 3, status: 'pending'})
  const validResult = validRegistry.apply(applied, 3, validProposal)
  equal(validResult, {ok: true, message: 'Applied to the document.'}, 'valid proposal result')
  equal(validProposal.status, 'applied', 'valid proposal status')
  equal(applied.text, 'changed', 'valid proposal must be applied')

  const throwingTool = {
    name: 'insert_content',
    definition: {type: 'function', name: 'insert_content', description: '', parameters: {type: 'object'}},
    execute() { return {output: {ok: true}} },
    apply() { throw new Error('boom') },
  }
  const throwingRegistry = new ai.EditorToolRegistry([throwingTool])
  const errorResult = throwingRegistry.apply({}, 3, proposal({documentVersion: 3}))
  equal(errorResult, {ok: false, message: 'boom'}, 'Error tool result')

  const nonErrorTool = {
    name: 'insert_content',
    definition: {type: 'function', name: 'insert_content', description: '', parameters: {type: 'object'}},
    execute() { return {output: {ok: true}} },
    apply() { throw 'boom' },
  }
  const nonErrorRegistry = new ai.EditorToolRegistry([nonErrorTool])
  const nonErrorResult = nonErrorRegistry.apply({}, 3, proposal({documentVersion: 3}))
  equal(nonErrorResult, {ok: false, message: 'Could not apply this proposal.'}, 'non-Error tool result')
  return {validResult, errorResult, nonErrorResult}
}

async function diff036(ai, StepMap) {
  const subject = proposal({
    arguments: {
      _selection_from: 8,
      _selection_to: 13,
      _insert_position: 13,
      _range_from: 8,
      _range_to: 13,
      edits: [{from: 8, to: 13}],
    },
    documentVersion: 1,
  })
  ai.rebaseEditorToolProposal(subject, new StepMap([1, 5, 1]), 3)
  equal(subject.arguments, {
    _selection_from: 4,
    _selection_to: 9,
    _insert_position: 9,
    _range_from: 4,
    _range_to: 9,
    edits: [{from: 4, to: 9}],
  }, 'rebased coordinates')
  equal(subject.documentVersion, 3, 'rebased documentVersion')
  return {arguments: subject.arguments, documentVersion: subject.documentVersion}
}

async function diff037(ai) {
  const custom = ai.createAiService({provider: 'custom', generate: async () => 'ok'})
  const stringResult = await custom.generate({prompt: 'hi'}, context())
  equal(stringResult, {text: 'ok'}, 'string custom result')

  const objectResult = await ai.createAiService({provider: 'custom', generate: async () => ({text: 'x'})})
    .generate({prompt: 'hi'}, context())
  equal(objectResult, {text: 'x'}, 'object custom result')

  const existing = {generate: async () => ({text: 'existing'})}
  const resolved = ai.createAiService(existing)
  truth(resolved === existing, 'existing AiService must be returned unchanged')
  return {stringResult, objectResult, sameExisting: resolved === existing}
}

async function diff038(ai) {
  const {client, calls} = fakeClient(() => response('ok'))
  const service = ai.createAiService({
    provider: 'local',
    providers: [{id: 'local', name: 'L', model: 'm'}],
    model: 'm',
    client,
  })
  truth(service instanceof ai.OpenAiService, 'provider configuration must return OpenAiService')
  equal(ai.getAiProvider('local')?.model, 'm', 'providers must be registered before resolution')
  await service.generate({prompt: 'hi'}, context())
  equal(calls[0]?.request.model, 'm', 'generate request model')
  return {model: calls[0]?.request.model}
}

async function diff039(ai) {
  const withConfig = fakeClient(() => response('ok'))
  const configService = new ai.OpenAiService({
    provider: {id: 'p', name: 'P', model: 'provider-model'},
    model: 'config-model',
    client: withConfig.client,
  })
  await configService.generate({prompt: 'hi'}, context())

  const withoutConfig = fakeClient(() => response('ok'))
  const providerService = new ai.OpenAiService({
    provider: {id: 'p', name: 'P', model: 'provider-model'},
    client: withoutConfig.client,
  })
  await providerService.generate({prompt: 'hi'}, context())

  const blank = fakeClient(() => response('ok'))
  const {rejected, error} = await isRejected(Promise.resolve().then(() => new ai.OpenAiService({
    provider: {id: 'p', name: 'P', model: 'provider-model'},
    model: '   ',
    client: blank.client,
  })))
  truth(rejected, 'blank model must throw')
  equal(error?.message, 'OpenAiService requires a model', 'blank model error')
  equal(blank.calls.length, 0, 'blank model must fail before a model request')
  equal(withConfig.calls[0]?.request.model, 'config-model', 'config model precedence')
  equal(withoutConfig.calls[0]?.request.model, 'provider-model', 'provider model fallback')
  return {configModel: withConfig.calls[0]?.request.model, providerModel: withoutConfig.calls[0]?.request.model, blankError: error?.message}
}

async function diff040(ai) {
  const supplied = {responses: {create: async () => response('ok')}, marker: 'supplied'}
  const reused = new ai.OpenAiService({
    provider: 'deepseek',
    model: 'm',
    client: supplied,
  })
  truth(reused.client === supplied, 'supplied client must be reused')

  const overridden = new ai.OpenAiService({
    provider: 'deepseek',
    model: 'm',
    apiKey: 'test-key',
    baseURL: 'https://override.test/v1',
  })
  equal(overridden.client.baseURL, 'https://override.test/v1', 'explicit baseURL must override provider default')
  return {clientReused: reused.client === supplied, baseURL: overridden.client.baseURL}
}

async function diff041(ai) {
  const blankId = await isRejected(Promise.resolve().then(() => ai.registerAiProvider({id: '', name: 'n', model: 'm'})))
  const blankName = await isRejected(Promise.resolve().then(() => ai.registerAiProvider({id: 'x', name: ' ', model: 'm'})))
  const blankModel = await isRejected(Promise.resolve().then(() => ai.registerAiProvider({id: 'x', name: 'X', model: ''})))
  equal(blankId.error?.message, 'AI provider id cannot be empty', 'blank provider id')
  equal(blankName.error?.message, 'AI provider name cannot be empty', 'blank provider name')
  equal(blankModel.error?.message, 'AI provider model cannot be empty', 'blank provider model')

  const registered = {id: 'diff-041-x', name: 'X', model: 'm'}
  ai.registerAiProvider(registered)
  const frozen = ai.getAiProvider('diff-041-x')
  registered.model = 'mutated'
  equal(frozen?.model, 'm', 'registered provider must be a frozen copy')
  truth(Object.isFrozen(frozen), 'registered provider must be frozen')
  return {blankId: blankId.error?.message, blankName: blankName.error?.message, blankModel: blankModel.error?.message, frozenModel: frozen?.model}
}

async function diff042(ai) {
  const snapshot = ai.getAiProviders()
  const snapshotLength = snapshot.length
  ai.registerAiProvider({id: 'diff-042-x', name: 'X', model: 'm'})
  equal(snapshot.length, snapshotLength, 'snapshot must not be affected by later registration')
  truth(!snapshot.some((provider) => provider.id === 'diff-042-x'), 'snapshot must not contain later registrations')
  equal(ai.getAiProvider('deepseek')?.model, 'deepseek-chat', 'deepseek provider model')
  equal(ai.getAiProvider('missing-provider-id'), undefined, 'missing provider lookup')
  return {snapshotLength, deepseekModel: ai.getAiProvider('deepseek')?.model}
}

async function diff043(ai) {
  let requests = 0
  const abortReason = new Error('stop')
  const abortingClient = {
    responses: {
      async create(_request, options) {
        const signal = options?.signal
        if (signal?.aborted) throw signal.reason
        requests += 1
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(signal.reason), {once: true})
        })
      },
    },
  }
  const service = new ai.OpenAiService({provider: 'deepseek', model: 'm', client: abortingClient})

  const controller = new AbortController()
  const pending = service.generate({prompt: 'hi', signal: controller.signal}, context())
  await Promise.resolve()
  controller.abort(abortReason)
  const during = await isRejected(pending)
  truth(during.rejected, 'aborted request must reject')
  truth(during.error === abortReason, 'mid-request abort must propagate the abort reason')

  const already = new AbortController()
  already.abort(abortReason)
  const before = await isRejected(service.generate({prompt: 'hi', signal: already.signal}, context()))
  truth(before.rejected, 'already-aborted request must reject')
  truth(before.error === abortReason, 'already-aborted request must propagate the abort reason')
  return {requests, midRequestReason: during.error?.message, alreadyAbortedReason: before.error?.message}
}

async function diff044(ai, repoRoot, fixtureRoot) {
  const baseline = JSON.parse(readFileSync(join(fixtureRoot, 'baseline-digests.json'), 'utf8'))
  const failures = []
  for (const [file, expected] of Object.entries(baseline.files)) {
    const absolute = join(repoRoot, file)
    if (!existsSync(absolute)) {
      failures.push(`${file} is missing`)
      continue
    }
    const actual = sha256(absolute)
    if (actual !== expected) failures.push(`${file} changed`)
  }
  const known = new Set(Object.keys(baseline.files))
  for (const file of walkFiles(repoRoot)) {
    if (known.has(file) || ALLOWED_CHANGE_PATHS.has(file)) continue
    failures.push(`${file} is outside the allowed migration change set`)
  }
  equal(failures, [], 'out-of-scope migration changes')
  const allowed = [...ALLOWED_CHANGE_PATHS]
  for (const file of allowed) {
    if (!existsSync(join(repoRoot, file))) failures.push(`${file} is missing`)
  }
  equal(failures, [], 'allowed migration files must exist')
  return {checkedFiles: Object.keys(baseline.files).length, allowedPaths: allowed}
}

async function diff045(_ai, repoRoot) {
  const pkg = JSON.parse(readFileSync(join(repoRoot, 'package.json'), 'utf8'))
  equal(pkg.engines?.node, '>=20.19.0', 'Node engine floor')
  truth(process.versions.node >= '22.12.0', 'runtime must be at least Node 22.12.0')
  return {engine: pkg.engines?.node, runtime: process.versions.node}
}

async function diff046(_ai, repoRoot) {
  const pkg = JSON.parse(readFileSync(join(repoRoot, 'package.json'), 'utf8'))
  equal(pkg.main, './dist/index.cjs', 'package main')
  equal(pkg.module, './dist/index.js', 'package module')
  equal(pkg.exports?.['.']?.import, {types: './dist/index.d.ts', default: './dist/index.js'}, 'package import export')
  equal(pkg.exports?.['.']?.require, {types: './dist/index.d.cts', default: './dist/index.cjs'}, 'package require export')
  const dist = join(repoRoot, 'dist')
  const distFiles = existsSync(dist) ? walkFiles(dist) : []
  truth(!distFiles.some((file) => /\.umd\./i.test(file)), 'built package must not contain a UMD bundle')
  const globalConstructor = distFiles.filter((file) => file.endsWith('.js') || file.endsWith('.cjs') || file.endsWith('.mjs'))
    .some((file) => readFileSync(join(dist, file), 'utf8').includes('window.AiEditor'))
  truth(!globalConstructor, 'built package must not expose a browser-global AiEditor constructor')
  return {
    main: pkg.main,
    module: pkg.module,
    import: pkg.exports?.['.']?.import,
    require: pkg.exports?.['.']?.require,
    distFiles,
  }
}

export async function runCorpus({ai, StepMap, repoRoot, fixtureRoot}) {
  const cases = {
    'DIFF-033': () => diff033(ai),
    'DIFF-034': () => diff034(ai),
    'DIFF-035': () => diff035(ai),
    'DIFF-036': () => diff036(ai, StepMap),
    'DIFF-037': () => diff037(ai),
    'DIFF-038': () => diff038(ai),
    'DIFF-039': () => diff039(ai),
    'DIFF-040': () => diff040(ai),
    'DIFF-041': () => diff041(ai),
    'DIFF-042': () => diff042(ai),
    'DIFF-043': () => diff043(ai),
    'DIFF-044': () => diff044(ai, repoRoot, fixtureRoot),
    'DIFF-045': () => diff045(ai, repoRoot),
    'DIFF-046': () => diff046(ai, repoRoot),
  }
  const results = []
  for (const id of OBSERVATION_IDS) {
    try {
      const actual = await cases[id]()
      results.push({observation_id: id, status: 'PASS', actual})
    } catch (error) {
      results.push({observation_id: id, status: 'FAIL', error: error instanceof Error ? error.message : String(error)})
    }
  }
  return {allPass: results.every((result) => result.status === 'PASS'), results}
}
