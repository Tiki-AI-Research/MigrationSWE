const AGENTS_MARK = '@openai/agents'
const MAX_ROUNDS_MESSAGE = 'AI exceeded the maximum editor tool call rounds'
const INCOMPLETE_STREAM_MESSAGE = 'AI response stream ended before completion'

export const OBSERVATION_IDS = [
  'F2P-001', 'F2P-002', 'F2P-003', 'F2P-004', 'F2P-005', 'F2P-006',
  'F2P-007', 'F2P-008', 'F2P-015', 'F2P-016', 'F2P-017', 'F2P-019',
  'F2P-020', 'F2P-023', 'F2P-009', 'F2P-012', 'F2P-013', 'F2P-022',
  'F2P-010', 'F2P-011', 'F2P-014', 'F2P-018', 'F2P-021', 'F2P-024',
]

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

function assertEqual(actual, expected, label) {
  const left = JSON.stringify(actual)
  const right = JSON.stringify(expected)
  assert(left === right, `${label}: expected ${right}, received ${left}`)
}

function assertIncludes(haystack, needle, label) {
  assert(String(haystack).includes(needle), `${label}: expected ${JSON.stringify(needle)} in ${JSON.stringify(haystack)}`)
}

function usage() {
  return {
    input_tokens: 1,
    output_tokens: 1,
    total_tokens: 2,
    input_tokens_details: { cached_tokens: 0 },
    output_tokens_details: { reasoning_tokens: 0 },
  }
}

function messageItem(text, id = 'msg_1') {
  return {
    id,
    type: 'message',
    role: 'assistant',
    status: 'completed',
    content: [{ type: 'output_text', text, annotations: [] }],
  }
}

function functionCallItem({ callId, name, args, id = 'fc_1' }) {
  return {
    id,
    type: 'function_call',
    call_id: callId,
    name,
    arguments: typeof args === 'string' ? args : JSON.stringify(args ?? {}),
    status: 'completed',
  }
}

function response(text, { id = 'resp_1', output = [], status = 'completed' } = {}) {
  return {
    id,
    object: 'response',
    created_at: 1,
    status,
    model: 'gpt-4o-mini',
    output: [...output, messageItem(text)],
    output_text: text,
    parallel_tool_calls: false,
    tool_choice: 'auto',
    tools: [],
    usage: usage(),
  }
}

function toolResponse({ callId = 'call_1', name = 'search_document', args = { query: 'x' }, id = 'resp_tool' } = {}) {
  return response('', { id, output: [functionCallItem({ callId, name, args })] })
}

function createTransport(handler) {
  const requests = []
  const stacks = []
  const client = {
    responses: {
      async create(body, options) {
        requests.push({ body, options: options ?? {} })
        stacks.push(new Error().stack ?? '')
        return handler(body, options ?? {}, requests.length - 1)
      },
    },
  }
  return {
    client,
    requests,
    stacks,
    assertAgentsOnModelPath() {
      assert(requests.length > 0, 'no model request was issued')
      assert(
        stacks.some((stack) => stack.includes(AGENTS_MARK)),
        'model request did not traverse the @openai/agents execution path',
      )
    },
  }
}

function makeContext(overrides = {}) {
  const text = overrides.text ?? 'alpha beta gamma'
  const selectedText = overrides.selectedText ?? ''
  const selection = overrides.selection ?? { from: 1, to: 1 }
  const documentVersion = overrides.documentVersion ?? 1
  const doc = {
    textBetween(from, to) {
      if (typeof overrides.textBetween === 'function') return overrides.textBetween(from, to)
      const start = Math.max(0, Number(from) - 1)
      const end = Math.max(start, Number(to) - 1)
      return text.slice(start, end)
    },
    descendants() {
      return false
    },
  }
  const editor = {
    state: {
      doc,
      selection: {
        $from: {
          parent: { type: { name: 'paragraph' } },
          marks: () => [],
        },
      },
      storedMarks: [],
    },
    chain() {
      return {
        focus() { return this },
        insertContentAt() { return this },
        run() { return true },
      }
    },
  }
  return {
    editor: overrides.editor ?? editor,
    html: overrides.html ?? '<p>alpha beta gamma</p>',
    text,
    selectedText,
    selection,
    documentVersion,
  }
}

async function* streamOf(deltas, { complete = true, extraEvents = [] } = {}) {
  yield { type: 'response.created', response: { ...response(''), status: 'in_progress', output: [] } }
  for (const event of extraEvents) yield event
  if (deltas) {
    for (const delta of deltas) {
      yield {
        type: 'response.output_text.delta',
        item_id: 'msg_1',
        output_index: 0,
        content_index: 0,
        delta,
      }
    }
  }
  if (complete) yield { type: 'response.completed', response: response((deltas ?? []).join('')) }
}

async function* incompleteToolStream(callId = 'call_1') {
  const item = functionCallItem({ callId, name: 'search_document', args: { query: 'x' } })
  yield { type: 'response.created', response: { ...response(''), status: 'in_progress', output: [] } }
  yield { type: 'response.output_item.added', output_index: 0, item }
  yield { type: 'response.output_item.done', output_index: 0, item }
}

async function* abortableStream(signal) {
  yield { type: 'response.created', response: { ...response(''), status: 'in_progress', output: [] } }
  yield { type: 'response.output_text.delta', item_id: 'msg_1', output_index: 0, content_index: 0, delta: 'a' }
  await new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason instanceof Error ? signal.reason : new Error('aborted'))
      return
    }
    const onAbort = () => {
      reject(signal.reason instanceof Error ? signal.reason : new Error('aborted'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve(undefined)
    }, 1000)
  })
}

function makeService(api, config) {
  return new api.OpenAiService(config)
}

async function expectRejects(promise, expectedMessage) {
  try {
    await promise
  } catch (error) {
    assertIncludes(error?.message, expectedMessage, 'rejection message')
    return error
  }
  throw new Error(`expected rejection containing ${JSON.stringify(expectedMessage)}`)
}

function approvalAlternativeProbe() {
  const proposal = {
    id: 'p1',
    tool: 'replace_selection',
    title: 'Replace selection',
    description: 'Replace the selected text.',
    arguments: {},
    documentVersion: 1,
    status: 'pending',
  }
  const values = ['always', 'never', ['replace_selection'], (candidate) => candidate.status === 'pending']
  assertEqual(values.slice(0, 2), ['always', 'never'], 'approval string alternatives')
  assertEqual(values[2], ['replace_selection'], 'approval name-array alternative')
  assert(values[3](proposal) === true, 'approval predicate alternative')
}

function requestFieldSurface(api) {
  const transport = createTransport(async (body) => body.stream ? streamOf(['o', 'k']) : response('ok'))
  const controller = new AbortController()
  const service = makeService(api, {
    provider: 'openai',
    model: 'gpt-4o-mini',
    client: transport.client,
  })
  const request = {
    prompt: 'hello',
    instructions: 'Be brief',
    scope: 'none',
    metadata: { trace: 'f2p-003' },
    history: [{ role: 'user', content: 'earlier' }],
    stream: true,
    onChunk: () => undefined,
    editorTools: false,
    signal: controller.signal,
  }
  return service.generate(request, makeContext()).then((result) => {
    assertEqual(result.text, 'ok', 'request field surface text')
    assert(transport.requests.length === 1, 'request field surface must issue one model request')
  })
}

function configFieldSurface(api) {
  const transport = createTransport(async () => response('ok'))
  const service = makeService(api, {
    provider: 'openai',
    providers: [{ id: 'local', name: 'Local', model: 'local-model' }],
    model: 'gpt-4o-mini',
    apiKey: 'test-key',
    baseURL: 'https://example.test/v1',
    client: transport.client,
    organization: 'org',
    project: 'project',
    timeout: 1000,
    maxRetries: 0,
    defaultHeaders: { 'x-test': '1' },
    fetch: async () => { throw new Error('fetch should not be used when client is supplied') },
    fetchOptions: { cache: 'no-store' },
    dangerouslyAllowBrowser: false,
    response: { temperature: 0 },
    buildInput: () => 'custom-input',
  })
  return service.generate({ prompt: 'ignored' }, makeContext()).then((result) => {
    assertEqual(result.text, 'ok', 'config field surface text')
    assertIncludes(JSON.stringify(transport.requests[0].body.input), 'custom-input', 'buildInput override')
  })
}

function contextSnapshotSurface(api) {
  const transport = createTransport(async () => response('snapshot'))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const context = makeContext({ text: 'original document', selectedText: '', selection: { from: 1, to: 1 } })
  const pending = service.generate({ prompt: 'hi' }, context)
  context.text = 'mutated document'
  context.selectedText = 'mut'
  return pending.then((result) => {
    assertEqual(result.text, 'snapshot', 'context snapshot result')
    const serialized = JSON.stringify(transport.requests[0].body.input)
    assertIncludes(serialized, 'original document', 'request-time document snapshot')
    assert(!serialized.includes('mutated document'), 'request must not observe later editor mutations')
  })
}

function modelPrecedenceSurface(api) {
  const configTransport = createTransport(async () => response('config-model'))
  const configService = makeService(api, {
    provider: { id: 'p', name: 'P', model: 'provider-model' },
    model: 'config-model',
    client: configTransport.client,
  })
  return configService.generate({ prompt: 'hi' }, makeContext()).then(async () => {
    assertEqual(configTransport.requests[0].body.model, 'config-model', 'config model precedence')

    const providerTransport = createTransport(async () => response('provider-model'))
    const providerService = makeService(api, {
      provider: { id: 'p', name: 'P', model: 'provider-model' },
      client: providerTransport.client,
    })
    await providerService.generate({ prompt: 'hi' }, makeContext())
    assertEqual(providerTransport.requests[0].body.model, 'provider-model', 'provider model fallback')

    const blankTransport = createTransport(async () => response('unexpected'))
    let threw = false
    try {
      makeService(api, { provider: { id: 'p', name: 'P', model: 'provider-model' }, model: '   ', client: blankTransport.client })
    } catch (error) {
      threw = true
      assertIncludes(error.message, 'OpenAiService requires a model', 'blank model error')
    }
    assert(threw, 'blank model must fail construction')
    assertEqual(blankTransport.requests.length, 0, 'blank model must fail before a model request')
  })
}

function classContractSurface(api) {
  const transport = createTransport(async () => response('class-contract'))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  assert(service.client === transport.client, 'OpenAiService must expose the supplied client')
  assert(typeof service.generate === 'function', 'OpenAiService must expose generate')
  return service.generate({ prompt: 'hi' }, makeContext()).then((result) => {
    assertEqual(result.text, 'class-contract', 'class contract result')
  })
}

function applyResultSurface(api) {
  const result = api.applyEditorToolProposal(makeContext().editor, 1, {
    id: 'readonly-1',
    tool: 'search_document',
    title: 'Search',
    description: 'Read only',
    arguments: { query: 'x' },
    documentVersion: 1,
    status: 'pending',
  })
  assert(typeof result.ok === 'boolean', 'apply result must expose boolean ok')
  assert(typeof result.message === 'string', 'apply result must expose string message')
  assertEqual(result.ok, false, 'read-only apply must fail')
  assertIncludes(result.message, 'is read-only and cannot be applied.', 'read-only apply message')
}

function proposalShapeSurface(api) {
  const context = makeContext({ text: 'abcdef', selectedText: 'bcd', selection: { from: 1, to: 4 } })
  const execution = api.executeEditorFunctionTool(
    'replace_selection',
    JSON.stringify({ text: 'x' }),
    context,
  )
  assertEqual(execution.output.ok, true, 'proposal tool ok flag')
  assertEqual(execution.output.status, 'pending_user_approval', 'proposal tool status')
  assert(typeof execution.output.proposal_id === 'string', 'proposal tool proposal_id')
  const proposal = execution.proposal
  assert(proposal, 'proposal tool must return a proposal')
  for (const field of ['id', 'tool', 'title', 'description', 'arguments', 'documentVersion', 'status']) {
    assert(field in proposal, `proposal must expose ${field}`)
  }
  assertEqual(proposal.status, 'pending', 'proposal initial status')
  assertEqual(proposal.documentVersion, 1, 'proposal document version')
  assertEqual(proposal.tool, 'replace_selection', 'proposal tool name')
}

async function toolLoopTerminationSurface(api) {
  const transport = createTransport(async (body, _options, index) => {
    if (index === 0) return toolResponse({ callId: 'call_1', name: 'search_document', args: { query: 'x' } })
    return response('done')
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate(
    { prompt: 'go', editorTools: true },
    makeContext(),
  )
  assertEqual(result.text, 'done', 'tool loop final text')
  assert(Array.isArray(result.toolProposals), 'tool loop result must expose toolProposals')
  assertEqual(transport.requests.length, 2, 'tool loop must continue exactly one round')
  return result
}

async function accumulationSurface(api) {
  // First round also carries assistant text 'a'; build it explicitly so accumulation is observable.
  const stateful = createTransport(async (_body, _options, index) => {
    if (index === 0) {
      return {
        ...toolResponse({ callId: 'call_1', name: 'search_document', args: { query: 'x' } }),
        output: [messageItem('a'), functionCallItem({ callId: 'call_1', name: 'search_document', args: { query: 'x' } })],
        output_text: 'a',
      }
    }
    return response('b')
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: stateful.client })
  const result = await service.generate({ prompt: 'go', editorTools: true }, makeContext())
  assertEqual(result.text, 'ab', 'non-streaming cross-round accumulation')
  assertEqual(stateful.requests.length, 2, 'accumulation surface request count')
}

async function eightRoundSurface(api) {
  let requests = 0
  const transport = createTransport(async () => {
    requests += 1
    return toolResponse({ callId: `call_${requests}`, name: 'search_document', args: { query: 'x' } })
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  await expectRejects(
    service.generate({ prompt: 'go', editorTools: true }, makeContext()),
    MAX_ROUNDS_MESSAGE,
  )
  assertEqual(transport.requests.length, 8, 'tool loop must stop after eight model rounds')
}

function dependencyManifestSurface(api, packageJson) {
  assertEqual(packageJson?.dependencies?.['@openai/agents'], '0.1.0', 'target dependency version')
  assertEqual(packageJson?.engines?.node, '>=20.19.0', 'node engine floor')
  assertEqual(packageJson?.main, './dist/index.cjs', 'main entry point')
  assertEqual(packageJson?.module, './dist/index.js', 'module entry point')
  assertEqual(packageJson?.exports?.['.']?.import?.default, './dist/index.js', 'ESM export')
  assertEqual(packageJson?.exports?.['.']?.require?.default, './dist/index.cjs', 'CJS export')
  assert(!JSON.stringify(packageJson?.exports ?? {}).toLowerCase().includes('umd'), 'no UMD export may return')
}

async function thrownToolSurface(api) {
  const transport = createTransport(async (_body, _options, index) => {
    if (index === 0) return toolResponse({ callId: 'call_1', name: 'replace_selection', args: { text: 'x' } })
    return response('recovered')
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate(
    { prompt: 'go', editorTools: true },
    makeContext({ selection: { from: 1, to: 1 }, selectedText: '' }),
  )
  assertEqual(result.text, 'recovered', 'tool error must not abort generation')
  const outputs = (transport.requests[1].body.input ?? []).filter((item) => item.type === 'function_call_output')
  assertEqual(outputs.length, 1, 'thrown tool must produce one function_call_output')
  const parsed = JSON.parse(outputs[0].output)
  assertEqual(parsed.ok, false, 'thrown tool output ok flag')
  assertIncludes(parsed.error, 'active text selection', 'thrown tool output error')
}

async function streamingSurface(api) {
  const transport = createTransport(async () => streamOf(['a', 'b', 'c']))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const chunks = []
  const result = await service.generate(
    { prompt: 'go', stream: true, onChunk: (chunk) => chunks.push(chunk) },
    makeContext(),
  )
  assertEqual(chunks, ['a', 'b', 'c'], 'streamed chunk order')
  assertEqual(result.text, 'abc', 'streamed text')
}

async function instructionsSurface(api) {
  const transport = createTransport(async () => response('ok'))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  await service.generate({ prompt: 'p', instructions: 'Be brief' }, makeContext({ text: 'doc' }))
  const body = transport.requests[0].body
  assertEqual(body.instructions, 'Be brief', 'model-level instructions')
  assert(!JSON.stringify(body.input).includes('Be brief'), 'instructions must not be concatenated into user input')
}

async function incompleteStreamSurface(api) {
  const transport = createTransport(async () => incompleteToolStream())
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  await expectRejects(
    service.generate({ prompt: 'go', stream: true, editorTools: true }, makeContext()),
    INCOMPLETE_STREAM_MESSAGE,
  )
}

// ----- Target postcondition probes -----

async function* streamedToolRound() {
  const call = functionCallItem({ callId: 'call_1', name: 'search_document', args: { query: 'x' } })
  yield { type: 'response.created', response: { ...response(''), status: 'in_progress', output: [] } }
  yield { type: 'response.output_item.added', output_index: 0, item: call }
  yield { type: 'response.output_item.done', output_index: 0, item: call }
  yield { type: 'response.completed', response: response('', { output: [call], id: 'resp_tool' }) }
}

async function postR001(api) {
  const transport = createTransport(async (_body, _options, index) => {
    if (index === 0) return streamedToolRound()
    return streamOf(['d', 'e'])
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const chunks = []
  const result = await service.generate(
    { prompt: 'hi', stream: true, editorTools: true, onChunk: (chunk) => chunks.push(chunk) },
    makeContext(),
  )
  assertEqual(chunks, ['d', 'e'], 'R001 streamed deltas through target path')
  assertEqual(result.text, 'de', 'R001 streamed text through target path')
  assert(transport.requests.length >= 2, 'R001 must execute a tool round locally')
  const secondInput = JSON.stringify(transport.requests[transport.requests.length - 1].body.input)
  assertIncludes(secondInput, 'function_call_output', 'R001 local tool output in next round')
  transport.assertAgentsOnModelPath()
}

async function postR002(api) {
  const transport = createTransport(async () => response('target-path'))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate(
    { prompt: 'hi' },
    makeContext({ text: 'editor document', selectedText: 'selection text' }),
  )
  assertEqual(result.text, 'target-path', 'R002 target path supplies model text')
  assertEqual(transport.requests.length, 1, 'R002 must not call an out-of-scope AI transport')
  transport.assertAgentsOnModelPath()
}

async function postR046(api) {
  const transport = createTransport(async () => response('no-direct-responses'))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate({ prompt: 'hi' }, makeContext())
  assertEqual(result.text, 'no-direct-responses', 'R046 generation result')
  transport.assertAgentsOnModelPath()
}

async function postR048(api) {
  const childProcessModule = await import('node:child_process')
  const childProcess = childProcessModule.default ?? childProcessModule
  const originalExec = childProcess.exec
  const originalFork = childProcess.fork
  childProcess.exec = () => { throw new Error('subprocess delegation is forbidden') }
  childProcess.fork = () => { throw new Error('fork delegation is forbidden') }
  try {
    const transport = createTransport(async () => response('no-delegation'))
    const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
    const result = await service.generate({ prompt: 'hi' }, makeContext())
    assertEqual(result.text, 'no-delegation', 'R048 generation survives blocked subprocesses')
    transport.assertAgentsOnModelPath()
  } finally {
    childProcess.exec = originalExec
    childProcess.fork = originalFork
  }
}

async function postR049(api, packageJson) {
  assertEqual(packageJson?.dependencies?.['@openai/agents'], '0.1.0', 'R049 target dependency declaration')
  const transport = createTransport(async () => response('declared-target'))
  const service = makeService(api, {
    client: transport.client,
    provider: 'openai',
    model: 'gpt-4o-mini',
  })
  const result = await service.generate({ prompt: 'hi' }, makeContext())
  assertEqual(result.text, 'declared-target', 'R049 supplied client transport result')
  assertEqual(transport.requests.length, 1, 'R049 supplied client must carry the model request')
  transport.assertAgentsOnModelPath()
}

async function postR053(api) {
  const transport = createTransport(async () => response('alpha', { id: 'resp_1' }))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate({ prompt: 'write' }, makeContext())
  assertEqual(result.text, 'alpha', 'R053 final agent text')
  assertEqual(result.raw?.id, 'resp_1', 'R053 raw provider response id')
  transport.assertAgentsOnModelPath()
}

async function postR054(api) {
  const events = [
    { type: 'response.output_item.added', output_index: 0, item: messageItem('') },
    { type: 'response.in_progress' },
    { type: 'response.output_text.done', item_id: 'msg_1', output_index: 0, content_index: 0, text: 'abc' },
    { type: 'response.content_part.done', item_id: 'msg_1', output_index: 0, content_index: 0, part: { type: 'output_text', text: 'abc', annotations: [] } },
  ]
  const transport = createTransport(async () => streamOf(['a', 'b', 'c'], { extraEvents: events }))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const chunks = []
  const result = await service.generate(
    { prompt: 'x', stream: true, onChunk: (chunk) => chunks.push(chunk) },
    makeContext(),
  )
  assertEqual(chunks, ['a', 'b', 'c'], 'R054 filters non-text lifecycle events')
  assertEqual(result.text, 'abc', 'R054 streamed text order')
  transport.assertAgentsOnModelPath()
}

async function postR055(api) {
  const transport = createTransport(async (_body, _options, index) => {
    if (index === 0) return toolResponse({ callId: 'call_1', name: 'search_document', args: { query: 'x' } })
    return response('done')
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate({ prompt: 'go', editorTools: true }, makeContext())
  assertEqual(result.text, 'done', 'R055 final text after tool round')
  assertEqual(transport.requests.length, 2, 'R055 requires a second model round')
  const outputs = (transport.requests[1].body.input ?? []).filter((item) => item.type === 'function_call_output')
  assertEqual(outputs.length, 1, 'R055 second round carries the tool output')
  assertEqual(outputs[0].call_id, 'call_1', 'R055 original call identifier')
  assertIncludes(outputs[0].output, 'ok', 'R055 tool output JSON')
  transport.assertAgentsOnModelPath()
}

async function postR056(api) {
  let count = 0
  const transport = createTransport(async () => {
    count += 1
    return toolResponse({ callId: `call_${count}`, name: 'search_document', args: { query: 'x' } })
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  await expectRejects(service.generate({ prompt: 'go', editorTools: true }, makeContext()), MAX_ROUNDS_MESSAGE)
  assertEqual(transport.requests.length, 8, 'R056 target turn limit maps to eight rounds')
  transport.assertAgentsOnModelPath()
}

async function postR057(api) {
  const transport = createTransport(async () => incompleteToolStream())
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  await expectRejects(
    service.generate({ prompt: 'go', stream: true, editorTools: true }, makeContext()),
    INCOMPLETE_STREAM_MESSAGE,
  )
  transport.assertAgentsOnModelPath()
}

async function postR058(api) {
  const transport = createTransport(async (_body, _options, index) => {
    if (index === 0) {
      return {
        ...toolResponse({ callId: 'call_1', name: 'search_document', args: { query: 'x' } }),
        output: [messageItem('a'), functionCallItem({ callId: 'call_1', name: 'search_document', args: { query: 'x' } })],
        output_text: 'a',
      }
    }
    return response('b')
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate({ prompt: 'go', editorTools: true }, makeContext())
  assertEqual(result.text, 'ab', 'R058 text accumulates across target items')
  transport.assertAgentsOnModelPath()
}

async function postR062(api) {
  const transport = createTransport(async () => response('ok'))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  await service.generate({ prompt: 'p', instructions: 'Be terse' }, makeContext({ text: 'doc' }))
  const body = transport.requests[0].body
  assertEqual(body.instructions, 'Be terse', 'R062 separate target instructions')
  assert(!JSON.stringify(body.input).includes('Be terse'), 'R062 instructions must not enter user input')
  transport.assertAgentsOnModelPath()
}

async function postR063(api) {
  const controller = new AbortController()
  const transport = createTransport(async (_body, options) => abortableStream(options.signal))
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const pending = service.generate(
    { prompt: 'x', stream: true, signal: controller.signal },
    makeContext(),
  )
  setTimeout(() => controller.abort(new Error('stop')), 25)
  const error = await expectRejects(pending, 'stop')
  assert(error instanceof Error, 'R063 abort must reject with an Error')
  transport.assertAgentsOnModelPath()
}

async function postR064(api) {
  const transport = createTransport(async (_body, _options, index) => {
    if (index === 0) return toolResponse({ callId: 'call_1', name: 'replace_selection', args: { text: 'x' } })
    return response('continued')
  })
  const service = makeService(api, { provider: 'openai', model: 'gpt-4o-mini', client: transport.client })
  const result = await service.generate(
    { prompt: 'go', editorTools: true },
    makeContext({ selection: { from: 1, to: 1 }, selectedText: '' }),
  )
  assertEqual(result.text, 'continued', 'R064 generation continues after tool exception')
  const outputs = (transport.requests[1].body.input ?? []).filter((item) => item.type === 'function_call_output')
  assertEqual(outputs.length, 1, 'R064 exception produces model-visible output')
  const parsed = JSON.parse(outputs[0].output)
  assertEqual(parsed.ok, false, 'R064 error output ok flag')
  assertIncludes(parsed.error, 'active text selection', 'R064 error output message')
  transport.assertAgentsOnModelPath()
}

const PROBES = [
  ['F2P-001', async (api) => { approvalAlternativeProbe(); await postR002(api) }],
  ['F2P-002', async (api) => { await instructionsSurface(api); await postR054(api) }],
  ['F2P-003', async (api) => { await requestFieldSurface(api); await postR062(api) }],
  ['F2P-004', async (api) => { await configFieldSurface(api); await postR058(api) }],
  ['F2P-005', async (api) => { applyResultSurface(api); await postR055(api) }],
  ['F2P-006', async (api) => { await contextSnapshotSurface(api); await postR048(api) }],
  ['F2P-007', async (api) => { await modelPrecedenceSurface(api); await postR053(api) }],
  ['F2P-008', async (api) => { await classContractSurface(api); await postR056(api) }],
  ['F2P-015', async (api) => { await eightRoundSurface(api); await postR046(api) }],
  ['F2P-016', async (api) => { await contextSnapshotSurface(api); await postR049(api, api.packageJson) }],
  ['F2P-017', async (api) => { dependencyManifestSurface(api, api.packageJson); await postR001(api) }],
  ['F2P-019', async (api) => { await thrownToolSurface(api); await postR063(api) }],
  ['F2P-020', async (api) => { await configFieldSurface(api); await postR064(api) }],
  ['F2P-023', async (api) => { await eightRoundSurface(api); await postR057(api) }],
  ['F2P-009', async (api) => { await incompleteStreamSurface(api); await postR054(api) }],
  ['F2P-012', async (api) => { await toolLoopTerminationSurface(api); await postR048(api) }],
  ['F2P-013', async (api) => { await accumulationSurface(api); await postR002(api) }],
  ['F2P-022', async (api) => { await streamingSurface(api); await postR001(api) }],
  ['F2P-010', async (api) => { await modelPrecedenceSurface(api); await postR058(api) }],
  ['F2P-011', async (api) => { applyResultSurface(api); await postR053(api) }],
  ['F2P-014', async (api) => { await classContractSurface(api); await postR062(api) }],
  ['F2P-018', async (api) => { await instructionsSurface(api); await postR002(api) }],
  ['F2P-021', async (api) => { proposalShapeSurface(api); await postR046(api) }],
  ['F2P-024', async (api) => { await streamingSurface(api); await postR062(api) }],
]

export async function runAll(api) {
  const results = []
  for (const [observationId, probe] of PROBES) {
    try {
      await probe(api)
      results.push({ observation_id: observationId, status: 'PASS' })
    } catch (error) {
      results.push({
        observation_id: observationId,
        status: 'FAIL',
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }
  return results
}
