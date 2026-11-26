import fs from 'node:fs'
import path from 'node:path'
import {pathToFileURL} from 'node:url'
import {
  functionCallItem,
  makeFakeClient,
  messageItem,
  responseObject,
  streamEvents,
} from './fake-responses.mjs'
import {createContext, createFakeEditor} from './editor-fixtures.mjs'

export const OBSERVATIONS = [
  'F2P-054', 'F2P-059', 'F2P-060', 'F2P-061', 'F2P-062', 'F2P-063',
  'F2P-064', 'F2P-065', 'F2P-066', 'F2P-067', 'F2P-068', 'F2P-069',
  'F2P-070', 'F2P-071', 'F2P-072', 'F2P-049', 'F2P-050', 'F2P-051',
  'F2P-052', 'F2P-053', 'F2P-055', 'F2P-056', 'F2P-057', 'F2P-058',
]

const AGENT_STACK = /@openai[\\/]agents(?:-openai|-core)?[\\/]/
const DIRECT_STACK = /[\\/]src[\\/]ai[\\/]OpenAiService\.(?:ts|js)/

const tracker = {available: false, calls: 0, patched: false, error: null}

async function installTargetTracker() {
  try {
    const agents = await import('@openai/agents')
    const Runner = agents.Runner
    if (typeof Runner !== 'function' || typeof Runner.prototype?.run !== 'function') {
      throw new Error('@openai/agents does not expose a usable Runner')
    }
    const originalRun = Runner.prototype.run
    Runner.prototype.run = function trackedRun(...args) {
      tracker.calls += 1
      return originalRun.apply(this, args)
    }
    tracker.available = true
    tracker.patched = true
  } catch (error) {
    tracker.error = error instanceof Error ? error.message : String(error)
  }
}

await installTargetTracker()

function resetTargetCalls() {
  tracker.calls = 0
}

function requireTarget() {
  if (!tracker.available) {
    throw new Error(`@openai/agents target technology unavailable: ${tracker.error ?? 'unknown error'}`)
  }
  if (tracker.calls < 1) {
    throw new Error('@openai/agents Runner was not used on the real generate execution path')
  }
}

function requireProvenance(stack) {
  if (!AGENT_STACK.test(stack)) {
    throw new Error('model transport did not execute through @openai/agents')
  }
  // Walk the stack from the innermost frame outward. A candidate frame only
  // proves a direct Responses call when it appears before the target
  // framework: if @openai/agents is the immediate caller of the client, the
  // request is driven by the target even when the candidate wraps the model
  // inside OpenAiService.ts (a legitimate Model adapter).
  for (const frame of String(stack).split('\n').slice(1)) {
    if (AGENT_STACK.test(frame)) break
    if (DIRECT_STACK.test(frame)) {
      throw new Error('scoped OpenAiService code called the source Responses API directly')
    }
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

async function importFrom(repoRoot, relativePath) {
  const target = path.join(repoRoot, relativePath)
  return import(pathToFileURL(target).href)
}

function context(options = {}) {
  return createContext(options)
}

function clientScript(steps) {
  return (body, index) => {
    const step = Array.isArray(steps) ? steps[Math.min(index, steps.length - 1)] : steps(body, index)
    return step
  }
}

async function withService(repoRoot, config, script, callback) {
  const {OpenAiService} = await importFrom(repoRoot, 'src/ai/OpenAiService.ts')
  const {client, calls} = makeFakeClient(clientScript(script))
  const service = new OpenAiService({...config, client: config.client ?? client})
  try {
    return await callback({service, client, calls, OpenAiService})
  } finally {
    resetTargetCalls()
  }
}

async function probeFinalTextAndRaw(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_1', [messageItem('alpha')])},
  ], async ({service, calls}) => {
    const result = await service.generate({prompt: 'write'}, context())
    assert(result.text === 'alpha', `expected final text alpha, received ${JSON.stringify(result.text)}`)
    assert(result.raw?.id === 'resp_1', 'expected raw to retain the final provider response')
    assert(calls.length === 1, 'expected exactly one model request')
    requireTarget()
  })
}

async function probeNonStreamingText(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_1', [messageItem('agent')])},
  ], async ({service}) => {
    const result = await service.generate({prompt: 'hi'}, context())
    assert(result.text === 'agent', `expected agent text, received ${JSON.stringify(result.text)}`)
    requireTarget()
  })
}

async function probeStreamingText(repoRoot) {
  const events = streamEvents('resp_stream', ['a', 'b', 'c'])
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {events},
  ], async ({service}) => {
    const chunks = []
    const result = await service.generate({prompt: 'go', stream: true, onChunk: (chunk) => chunks.push(chunk)}, context())
    assert(JSON.stringify(chunks) === JSON.stringify(['a', 'b', 'c']), `unexpected chunk order ${JSON.stringify(chunks)}`)
    assert(result.text === 'abc', `expected streamed text abc, received ${JSON.stringify(result.text)}`)
    requireTarget()
  })
}

async function probeToolOrchestration(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_tool', [
      messageItem('a'),
      functionCallItem({id: 'fc_1', callId: 'call_1', name: 'search_document', args: {query: 'hello'}}),
    ])},
    {response: responseObject('resp_done', [messageItem('agent')])},
  ], async ({service, calls}) => {
    const ctx = context()
    const result = await service.generate({prompt: 'hi', editorTools: true}, ctx)
    assert(result.text === 'aagent', `expected accumulated agent text, received ${JSON.stringify(result.text)}`)
    assert(calls.length === 2, 'expected a second model round after local tool execution')
    assert(calls[1].body.input.some((item) => item.type === 'function_call_output' && item.call_id === 'call_1'),
      'expected the local tool output to be sent back with call id call_1')
    requireTarget()
  })
}

async function probeNestedClientProvenance(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_1', [messageItem('x')])},
  ], async ({service, calls}) => {
    await service.generate({prompt: 'hi'}, context())
    requireTarget()
    requireProvenance(calls[0]?.stack ?? '')
  })
}

async function probeSuppliedClient(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_1', [messageItem('agent')])},
  ], async ({service, client, calls}) => {
    assert(service.client === client, 'service.client must be the caller supplied client')
    const result = await service.generate({prompt: 'hi'}, context())
    assert(result.text === 'agent', 'generation did not use the supplied client result')
    assert(calls.length === 1, 'expected the supplied client to receive the model request')
    requireTarget()
  })
}

async function probeTurnLimit(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, (body, index) => ({
    response: responseObject(`resp_${index}`, [
      functionCallItem({id: `fc_${index}`, callId: `call_${index}`, name: 'search_document', args: {query: 'hello'}}),
    ]),
  }), async ({service, calls}) => {
    let message = ''
    try {
      await service.generate({prompt: 'go', editorTools: true}, context())
    } catch (error) {
      message = error instanceof Error ? error.message : String(error)
    }
    assert(message === 'AI exceeded the maximum editor tool call rounds', `unexpected turn limit error ${JSON.stringify(message)}`)
    assert(calls.length === 8, `expected eight model rounds, observed ${calls.length}`)
    requireTarget()
  })
}

async function probeDependencyDeclaration(repoRoot) {
  const manifest = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'))
  assert(manifest.dependencies?.['@openai/agents'] === '0.1.0', 'package.json must declare @openai/agents 0.1.0')
  return probeNestedClientProvenance(repoRoot)
}

async function probeResponseOptions(repoRoot) {
  return withService(repoRoot, {
    provider: 'openai',
    model: 'gpt-4o-mini',
    response: {
      temperature: 0.1,
      top_p: 0.8,
      max_output_tokens: 64,
      parallel_tool_calls: false,
      store: false,
    },
  }, [{response: responseObject('resp_1', [messageItem('x')])}], async ({service, calls}) => {
    await service.generate({prompt: 'x'}, context())
    const body = calls[0].body
    assert(body.temperature === 0.1, `temperature was ${body.temperature}`)
    assert(body.top_p === 0.8, `top_p was ${body.top_p}`)
    assert(body.max_output_tokens === 64, `max_output_tokens was ${body.max_output_tokens}`)
    assert(body.parallel_tool_calls === false, `parallel_tool_calls was ${body.parallel_tool_calls}`)
    assert(body.store === false, `store was ${body.store}`)
    requireTarget()
  })
}

async function probeCrossRoundAccumulation(repoRoot) {
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_a', [
      messageItem('a'),
      functionCallItem({id: 'fc_1', callId: 'call_1', name: 'search_document', args: {query: 'hello'}}),
    ])},
    {response: responseObject('resp_b', [messageItem('b')])},
  ], async ({service, calls}) => {
    const result = await service.generate({prompt: 'go', editorTools: true}, context())
    assert(result.text === 'ab', `expected accumulated text ab, received ${JSON.stringify(result.text)}`)
    assert(calls.length === 2, 'expected two model rounds')
    requireTarget()
  })
}

async function probeProviderResolution(repoRoot) {
  const {OpenAiService} = await importFrom(repoRoot, 'src/ai/OpenAiService.ts')
  const provider = {id: 'q', name: 'Q', baseURL: 'https://example.test/v1', model: 'm1'}
  const endpointService = new OpenAiService({provider, apiKey: 'test-key'})
  assert(String(endpointService.client.baseURL).startsWith('https://example.test/v1'),
    `provider baseURL was ${String(endpointService.client.baseURL)}`)

  const first = await withService(repoRoot, {provider}, [
    {response: responseObject('resp_1', [messageItem('x')])},
  ], async ({service, calls}) => {
    await service.generate({prompt: 'hi'}, context())
    assert(calls[0].body.model === 'm1', `provider model was ${calls[0].body.model}`)
    requireTarget()
  })

  await withService(repoRoot, {provider, model: 'm2'}, [
    {response: responseObject('resp_2', [messageItem('y')])},
  ], async ({service, calls}) => {
    await service.generate({prompt: 'hi'}, context())
    assert(calls[0].body.model === 'm2', `configured model was ${calls[0].body.model}`)
    requireTarget()
  })

  let blankMessage = ''
  try {
    new OpenAiService({provider, model: '   '})
  } catch (error) {
    blankMessage = error instanceof Error ? error.message : String(error)
  }
  assert(blankMessage === 'OpenAiService requires a model', `unexpected blank model error ${JSON.stringify(blankMessage)}`)
  return first
}

async function probeAbort(repoRoot) {
  const {makeFakeClient: makeClient} = await import('./fake-responses.mjs')
  const calls = []
  const client = {
    responses: {
      create: async (body, options) => {
        calls.push({body, options})
        return (async function* stream() {
          yield {type: 'response.created', response: {id: 'resp_abort', object: 'response', status: 'in_progress', output: []}}
          yield {type: 'response.output_text.delta', item_id: 'msg', output_index: 0, content_index: 0, delta: 'a'}
          await new Promise((resolve, reject) => {
            const signal = options?.signal
            if (!signal) {
              setTimeout(resolve, 10000)
              return
            }
            if (signal.aborted) {
              reject(signal.reason)
              return
            }
            signal.addEventListener('abort', () => reject(signal.reason), {once: true})
          })
          yield {type: 'response.completed', response: responseObject('resp_abort', [messageItem('a')])}
        })()
      },
    },
  }
  const {OpenAiService} = await importFrom(repoRoot, 'src/ai/OpenAiService.ts')
  const service = new OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  const controller = new AbortController()
  const chunks = []
  const pending = service.generate({
    prompt: 'x',
    stream: true,
    onChunk: (chunk) => chunks.push(chunk),
    signal: controller.signal,
  }, context())
  setTimeout(() => controller.abort(new Error('stop')), 25)
  let failure = null
  try {
    await pending
  } catch (error) {
    failure = error
  }
  assert(failure instanceof Error && failure.message === 'stop', 'abort reason was not propagated')
  assert(calls.length === 1, `expected no model request after abort, observed ${calls.length}`)
  assert(chunks[0] === 'a', 'expected the delivered delta before abort')
  resetTargetCalls()
  const satisfied = await probeNestedClientProvenance(repoRoot)
  return satisfied
}

async function probeTargetDependencyBoundary(repoRoot) {
  await probeStreamingText(repoRoot)
  await probeToolOrchestration(repoRoot)
  resetTargetCalls()
  await probeNestedClientProvenance(repoRoot)
  return true
}

async function probeToolExceptionOutput(repoRoot) {
  const editor = createFakeEditor({throwOnRead: new Error('boom')})
  return withService(repoRoot, {provider: 'openai', model: 'gpt-4o-mini'}, [
    {response: responseObject('resp_call', [
      functionCallItem({id: 'fc_1', callId: 'call_1', name: 'search_document', args: {query: 'hello'}}),
    ])},
    {response: responseObject('resp_done', [messageItem('done')])},
  ], async ({service, calls}) => {
    const result = await service.generate({prompt: 'go', editorTools: true}, context({editor}))
    assert(result.text === 'done', `expected final text done, received ${JSON.stringify(result.text)}`)
    const output = calls[1].body.input.find((item) => item.type === 'function_call_output' && item.call_id === 'call_1')
    assert(output, 'expected function_call_output for call_1 in the next model round')
    assert(output.output === JSON.stringify({ok: false, error: 'boom'}), `unexpected tool output ${output.output}`)
    requireTarget()
  })
}

async function surfaceEditorFunctionTools(repoRoot) {
  const {editorFunctionTools} = await importFrom(repoRoot, 'src/ai/editor-tools.ts')
  const names = editorFunctionTools.map((tool) => tool.name).sort()
  assert(JSON.stringify(names) === JSON.stringify([
    'apply_formatting', 'apply_text_edits', 'get_editor_context',
    'insert_content', 'replace_selection', 'search_document',
  ]), `unexpected editorFunctionTools ${JSON.stringify(names)}`)
}

async function surfaceRegistryTools(repoRoot) {
  const {defaultEditorToolRegistry} = await importFrom(repoRoot, 'src/ai/tools/index.ts')
  const names = defaultEditorToolRegistry.definitions.map((tool) => tool.name).sort()
  assert(names.length === 6, `expected six registry tools, observed ${names.length}`)
}

async function surfaceReadOnlyApply(repoRoot) {
  const {applyEditorToolProposal} = await importFrom(repoRoot, 'src/ai/editor-tools.ts')
  const proposal = {
    id: 'p_read_only',
    tool: 'search_document',
    title: 'Search',
    description: 'Search the document',
    arguments: {query: 'hello'},
    documentVersion: 1,
    status: 'pending',
  }
  const result = applyEditorToolProposal(createFakeEditor(), 1, proposal)
  assert(result.ok === false, 'read-only proposal unexpectedly applied')
  assert(result.message === 'search_document is read-only and cannot be applied.', `unexpected read-only message ${result.message}`)
}

async function surfacePackageContract(repoRoot) {
  const manifest = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'))
  assert(manifest.exports?.['.']?.import?.types === './dist/index.d.ts', 'missing ESM declaration export')
  assert(manifest.exports?.['.']?.require?.types === './dist/index.d.cts', 'missing CJS declaration export')
  assert(!('umd' in (manifest.exports ?? {})), 'UMD export must not be present')
}

async function surfaceNonPendingApply(repoRoot) {
  const {applyEditorToolProposal} = await importFrom(repoRoot, 'src/ai/editor-tools.ts')
  const proposal = {
    id: 'p_applied',
    tool: 'replace_selection',
    title: 'Replace',
    description: 'Replace selection',
    arguments: {text: 'x'},
    documentVersion: 1,
    status: 'applied',
  }
  const result = applyEditorToolProposal(createFakeEditor(), 1, proposal)
  assert(result.ok === false && result.message === 'This proposal is no longer pending.', 'non-pending proposal was not rejected')
}

async function surfaceProposalTool(repoRoot) {
  const {executeEditorFunctionTool} = await importFrom(repoRoot, 'src/ai/editor-tools.ts')
  const ctx = context({selection: {from: 1, to: 3}, documentVersion: 7})
  const execution = executeEditorFunctionTool('replace_selection', '{"text":"x"}', ctx)
  assert(execution.output?.ok === true, 'proposal tool did not report success')
  assert(execution.output?.status === 'pending_user_approval', 'proposal tool did not report pending approval')
  assert(typeof execution.output?.proposal_id === 'string', 'proposal tool did not return a proposal id')
  assert(execution.proposal?.status === 'pending', 'proposal initial status was not pending')
  assert(execution.proposal?.documentVersion === 7, 'proposal did not retain the request document version')
}

async function surfaceReactivation(repoRoot) {
  const {executeEditorFunctionTool} = await importFrom(repoRoot, 'src/ai/editor-tools.ts')
  const execution = executeEditorFunctionTool('search_document', '{"query":"hello"}', context())
  assert(execution && typeof execution === 'object', 'executeEditorFunctionTool did not return an execution')
  assert(execution.output?.ok === true, 'search_document did not return an ok output')
  assert(!execution.proposal, 'read-only tool unexpectedly produced a proposal')
}

async function surfaceApplySignature(repoRoot) {
  const {applyEditorToolProposal} = await importFrom(repoRoot, 'src/ai/editor-tools.ts')
  assert(applyEditorToolProposal.length === 3, 'applyEditorToolProposal must accept editor, documentVersion, proposal')
}

const PRE_CHECKS = {
  'F2P-061': surfaceEditorFunctionTools,
  'F2P-062': surfaceRegistryTools,
  'F2P-063': surfaceReadOnlyApply,
  'F2P-064': surfacePackageContract,
  'F2P-065': surfaceNonPendingApply,
  'F2P-066': surfaceReadOnlyApply,
  'F2P-068': surfaceProposalTool,
  'F2P-069': surfaceReactivation,
  'F2P-070': surfaceApplySignature,
  'F2P-071': surfaceReactivation,
  'F2P-072': surfaceProposalTool,
}

const PROBES = {
  'F2P-049': probeFinalTextAndRaw,
  'F2P-050': probeNestedClientProvenance,
  'F2P-051': probeSuppliedClient,
  'F2P-052': probeTurnLimit,
  'F2P-053': probeDependencyDeclaration,
  'F2P-054': probeResponseOptions,
  'F2P-055': probeCrossRoundAccumulation,
  'F2P-056': probeProviderResolution,
  'F2P-057': probeAbort,
  'F2P-058': probeTargetDependencyBoundary,
  'F2P-059': probeToolExceptionOutput,
  'F2P-060': probeCrossRoundAccumulation,
  'F2P-061': probeToolExceptionOutput,
  'F2P-062': probeToolExceptionOutput,
  'F2P-063': probeToolExceptionOutput,
  'F2P-064': probeToolExceptionOutput,
  'F2P-065': probeToolExceptionOutput,
  'F2P-066': probeToolExceptionOutput,
  'F2P-067': async (repoRoot) => {
    const manifest = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'))
    assert(manifest.engines?.node === '>=20.19.0', `unexpected engines.node ${manifest.engines?.node}`)
    return probeDependencyDeclaration(repoRoot)
  },
  'F2P-068': probeToolExceptionOutput,
  'F2P-069': probeToolExceptionOutput,
  'F2P-070': probeToolExceptionOutput,
  'F2P-071': probeToolExceptionOutput,
  'F2P-072': probeToolExceptionOutput,
}

export async function runObservation(observationId, repoRoot) {
  const probe = PROBES[observationId]
  if (!probe) throw new Error(`No probe registered for ${observationId}`)
  resetTargetCalls()
  const precheck = PRE_CHECKS[observationId]
  if (precheck) await precheck(repoRoot)
  await probe(repoRoot)
  return {observation_id: observationId, status: 'PASS', message: 'assertion satisfied'}
}

export async function runAll({repoRoot, side}) {
  const results = []
  for (const observationId of OBSERVATIONS) {
    try {
      const result = await runObservation(observationId, repoRoot)
      results.push(result)
    } catch (error) {
      results.push({
        observation_id: observationId,
        status: 'FAIL',
        message: error instanceof Error ? error.message : String(error),
        side,
      })
    }
  }
  return results
}
