import {describe, it, expect} from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

const repo = process.env.AIEDITOR_REPO ?? process.cwd()
const ai: any = await import(path.join(repo, 'src/ai/index.ts'))

function read(rel: string): string {
  return fs.readFileSync(path.join(repo, rel), 'utf8')
}

function interfaceBlock(src: string, name: string): string {
  const start = src.search(new RegExp(`(?:interface|type)\\s+${name}\\b`))
  if (start < 0) return ''
  const open = src.indexOf('{', start)
  if (open < 0) {
    const end = src.indexOf('\n', start)
    return src.slice(start, end < 0 ? undefined : end)
  }
  let depth = 0
  for (let index = open; index < src.length; index += 1) {
    if (src[index] === '{') depth += 1
    if (src[index] === '}') {
      depth -= 1
      if (depth === 0) return src.slice(open, index + 1)
    }
  }
  return src.slice(open)
}

function typeBlock(src: string, name: string): string {
  const match = new RegExp(`export\\s+type\\s+${name}\\b`).exec(src)
  if (!match) return ''
  const rest = src.slice(match.index)
  const end = rest.search(/\n\s*\n/)
  return end < 0 ? rest : rest.slice(0, end)
}

function fields(src: string, name: string): string[] {
  const block = interfaceBlock(src, name)
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/.*$/gm, '')
  return [...block.matchAll(/^\s*([A-Za-z_$][\w$]*)\??\s*:/gm)].map((m) => m[1])
}

function messageItem(text: string, id = 'msg_1') {
  return {
    id,
    type: 'message',
    role: 'assistant',
    status: 'completed',
    content: [{type: 'output_text', text, annotations: []}],
  }
}

function functionCallItem(name: string, args: string, callId = 'call_1') {
  return {
    id: `fc_${callId}`,
    type: 'function_call',
    call_id: callId,
    name,
    arguments: args,
    status: 'completed',
  }
}

function responseObject(output: any[], text: string, id = 'resp_1') {
  return {
    id,
    object: 'response',
    created_at: 0,
    status: 'completed',
    model: 'gpt-4o-mini',
    output,
    output_text: text,
    usage: {
      input_tokens: 1,
      output_tokens: 1,
      total_tokens: 2,
      input_tokens_details: {cached_tokens: 0},
      output_tokens_details: {reasoning_tokens: 0},
    },
    parallel_tool_calls: true,
    tool_choice: 'auto',
    tools: [],
    error: null,
    incomplete_details: null,
    instructions: null,
    max_output_tokens: null,
    previous_response_id: null,
    reasoning: null,
    store: true,
    temperature: 1,
    text: {format: {type: 'text'}},
    top_p: 1,
    truncation: 'disabled',
    user: null,
    metadata: {},
  }
}

function textResponse(text: string, id = 'resp_1') {
  return responseObject([messageItem(text)], text, id)
}

function toolResponse(name: string, args: string, text = '', callId = 'call_1', id = 'resp_1') {
  const output: any[] = []
  if (text) output.push(messageItem(text))
  output.push(functionCallItem(name, args, callId))
  return responseObject(output, text, id)
}

function streamFrom(events: any[]) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const event of events) yield event
    },
  }
}

function streamEvents(deltas: string[], finalText = deltas.join(''), complete = true) {
  const events: any[] = [{type: 'response.created', response: textResponse('', 'resp_1')}]
  deltas.forEach((delta, index) => {
    events.push({
      type: 'response.output_text.delta',
      delta,
      item_id: 'msg_1',
      output_index: 0,
      content_index: 0,
      sequence_number: index + 1,
    })
  })
  if (complete) events.push({type: 'response.completed', response: textResponse(finalText, 'resp_1')})
  return events
}

function makeClient(respond: (request: any, index: number) => any) {
  const requests: any[] = []
  const client = {
    responses: {
      create: async (request: any) => {
        requests.push(request)
        return respond(request, requests.length - 1)
      },
    },
  }
  return {client, requests}
}

function makeEditor(text = 'Hello world') {
  const calls = {transactions: 0}
  const textNode = {isText: true, text}
  const paragraph: any = {
    type: {name: 'paragraph'},
    isTextblock: true,
    textContent: text,
    nodeSize: text.length + 2,
    descendants(callback: (node: any, position: number) => void) {
      callback(textNode, 0)
    },
  }
  const doc = {
    textContent: text,
    descendants(callback: (node: any, position: number) => void) {
      callback(paragraph, 0)
    },
    textBetween(from: number, to: number) {
      return text.slice(Math.max(0, from - 1), Math.max(0, to - 1))
    },
  }
  const chain: any = {
    focus() {
      return chain
    },
    insertContentAt() {
      calls.transactions += 1
      return chain
    },
    run() {
      return true
    },
  }
  const editor: any = {
    state: {
      doc,
      storedMarks: null,
      selection: {$from: {parent: paragraph, marks: () => []}},
    },
    chain() {
      return chain
    },
  }
  return {editor, calls}
}

function baseContext(text = 'Hello world', selectedText = '', from = 1, to = 1, version = 1) {
  const {editor} = makeEditor(text)
  return {
    editor,
    html: `<p>${text}</p>`,
    text,
    selectedText,
    selection: {from, to},
    documentVersion: version,
  }
}

function inputText(request: any): string {
  const input = request?.input
  if (typeof input === 'string') return input
  if (Array.isArray(input)) {
    return input
      .map((item: any) => {
        if (typeof item?.content === 'string') return item.content
        if (Array.isArray(item?.content)) {
          return item.content.map((part: any) => part?.text ?? '').join('')
        }
        return ''
      })
      .join('\n')
  }
  return JSON.stringify(input ?? '')
}

function collectCallIds(value: any, out: string[] = []): string[] {
  if (Array.isArray(value)) {
    value.forEach((item) => collectCallIds(item, out))
  } else if (value && typeof value === 'object') {
    if (typeof value.call_id === 'string') out.push(value.call_id)
    Object.values(value).forEach((item) => collectCallIds(item, out))
  }
  return out
}

function toolOutputs(value: any, out: any[] = []): any[] {
  if (Array.isArray(value)) {
    value.forEach((item) => toolOutputs(item, out))
  } else if (value && typeof value === 'object') {
    if (value.type === 'function_call_output') out.push(value)
    Object.values(value).forEach((item) => toolOutputs(item, out))
  }
  return out
}

const EXPORT_NAMES = [
  'OpenAiService', 'createAiService', 'AI_PROVIDERS', 'registerAiProvider', 'getAiProviders',
  'getAiProvider', 'editorTools', 'editorFunctionTools', 'executeEditorFunctionTool',
  'applyEditorToolProposal', 'defaultEditorToolRegistry', 'defaultEditorTools',
  'EditorToolRegistry', 'EditorTool', 'EditorMutationTool', 'ApplyFormattingTool',
  'ApplyTextEditsTool', 'GetEditorContextTool', 'InsertContentTool', 'ReplaceSelectionTool',
  'SearchDocumentTool',
]

const TYPE_NAMES = [
  'AiContentScope', 'AiChatMessage', 'AiEditorContext', 'AiEditorToolApplyResult',
  'AiEditorToolApproval', 'AiEditorToolName', 'AiEditorToolProposal', 'AiGenerateRequest',
  'AiGenerateResult', 'AiService', 'AiServiceConfig', 'AiProviderServiceConfig',
  'CustomAiServiceConfig', 'OpenAiServiceConfig', 'AiProviderDefinition', 'EditorToolExecution',
]

const TOOL_NAMES = [
  'get_editor_context', 'search_document', 'replace_selection', 'insert_content',
  'apply_formatting', 'apply_text_edits',
]

const tests: Array<[string, () => Promise<any>, any]> = []
function observation(id: string, run: () => Promise<any>, expected: any) {
  tests.push([id, run, expected])
}

observation('DIFF-001', async () => {
  const root: any = await import(path.join(repo, 'src/index.ts'))
  const missingRoot = EXPORT_NAMES.filter((name) => !(name in root)).sort()
  const missingAi = ['rebaseEditorToolProposal'].filter((name) => !(name in ai)).sort()
  return {missingRoot, missingAi}
}, {missingRoot: [], missingAi: []})

observation('DIFF-002', async () => {
  const sources = ['src/ai/types.ts', 'src/ai/OpenAiService.ts', 'src/ai/Providers.ts', 'src/ai/tools/index.ts', 'src/ai/tools/core/EditorTool.ts']
    .map(read).join('\n')
  const declared = new Set([...sources.matchAll(/export\s+(?:interface|type)\s+([A-Za-z0-9_]+)/g)].map((m) => m[1]))
  const index = read('src/ai/index.ts')
  return {
    missingDeclarations: TYPE_NAMES.filter((name) => !declared.has(name)).sort(),
    missingReexports: TYPE_NAMES.filter((name) => !new RegExp(`\\b${name}\\b`).test(index)).sort(),
  }
}, {missingDeclarations: [], missingReexports: []})

observation('DIFF-003', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const existing = {generate: async () => ({text: 'existing'})}
  const same = ai.createAiService(existing) === existing
  const customString = await ai.createAiService({provider: 'custom', generate: async () => 'ok'})
    .generate({prompt: 'x'}, baseContext())
  const customObject = await ai.createAiService({provider: 'custom', generate: async () => ({text: 'x'})})
    .generate({prompt: 'x'}, baseContext())
  const configured = ai.createAiService({provider: 'openai', model: 'gpt-4o-mini'})
  return {
    same,
    customString: customString.text,
    customObject: customObject.text,
    openaiInstance: configured instanceof ai.OpenAiService,
  }
}, {same: true, customString: 'ok', customObject: 'x', openaiInstance: true})

observation('DIFF-004', async () => {
  const source = read('src/ai/OpenAiService.ts')
  const keys = fields(source, 'OpenAiServiceConfig').sort()
  const expectedKeys = [
    'provider', 'providers', 'model', 'apiKey', 'baseURL', 'client', 'organization', 'project',
    'timeout', 'maxRetries', 'defaultHeaders', 'fetch', 'fetchOptions', 'dangerouslyAllowBrowser',
    'response', 'buildInput',
  ].sort()
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client} = makeClient(() => textResponse('ok'))
  const service = new ai.OpenAiService({
    provider: {id: 'p', name: 'P', model: 'm'},
    providers: [{id: 'p', name: 'P', model: 'm'}],
    model: 'm',
    apiKey: 'key',
    baseURL: 'https://example.test/v1',
    client,
    organization: 'org',
    project: 'project',
    timeout: 1000,
    maxRetries: 1,
    defaultHeaders: {x: 'y'},
    fetch: async () => { throw new Error('unused') },
    fetchOptions: {},
    dangerouslyAllowBrowser: true,
    response: {temperature: 0.1},
    buildInput: () => 'custom',
  })
  await service.generate({prompt: 'x'}, baseContext())
  return {missingKeys: expectedKeys.filter((key) => !keys.includes(key)), constructed: true}
}, {missingKeys: [], constructed: true})

observation('DIFF-005', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client} = makeClient(() => textResponse('ok'))
  const service = new ai.OpenAiService({provider: 'openai', model: 'gpt-4o-mini', client})
  const pending = service.generate({prompt: 'x'}, baseContext())
  const isPromise = pending instanceof Promise
  const result = await pending
  return {
    instance: service instanceof ai.OpenAiService,
    clientReadable: 'client' in service && service.client === client,
    generatePromise: isPromise,
    text: result.text,
  }
}, {instance: true, clientReadable: true, generatePromise: true, text: 'ok'})

observation('DIFF-006', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client} = makeClient(() => textResponse('hello'))
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  const result = await service.generate({prompt: 'hello'}, baseContext())
  let emptyMessage = ''
  try {
    await service.generate({prompt: '   '}, baseContext())
  } catch (error: any) {
    emptyMessage = error.message
  }
  return {
    text: result.text,
    hasRaw: result.raw !== undefined,
    hasProposals: result.toolProposals !== undefined,
    emptyMessage,
  }
}, {text: 'hello', hasRaw: true, hasProposals: false, emptyMessage: 'AI prompt cannot be empty'})

observation('DIFF-007', async () => {
  const keys = fields(read('src/ai/types.ts'), 'AiGenerateRequest').sort()
  const expected = ['prompt', 'instructions', 'scope', 'metadata', 'history', 'stream', 'onChunk', 'editorTools', 'signal'].sort()
  return {missing: expected.filter((key) => !keys.includes(key))}
}, {missing: []})

observation('DIFF-008', async () => {
  const keys = fields(read('src/ai/types.ts'), 'AiEditorContext').sort()
  const expected = ['editor', 'html', 'text', 'selectedText', 'selection', 'documentVersion'].sort()
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client, requests} = makeClient(async () => {
    return textResponse('ok')
  })
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  const selectionContext = baseContext('original', 'ori')
  await service.generate({prompt: 'p'}, selectionContext)
  const selectionInput = inputText(requests[0])
  const documentContext = baseContext('original', '')
  await service.generate({prompt: 'p'}, documentContext)
  const documentInput = inputText(requests[1])
  return {
    missing: expected.filter((key) => !keys.includes(key)),
    originalPresent: documentInput.includes('original'),
    selectedPresent: selectionInput.includes('ori'),
    mutatedPresent: documentInput.includes('mutated'),
  }
}, {missing: [], originalPresent: true, selectedPresent: true, mutatedPresent: false})

observation('DIFF-009', async () => {
  const source = read('src/ai/types.ts')
  const keys = fields(source, 'AiEditorToolProposal').sort()
  const expected = ['id', 'tool', 'title', 'description', 'arguments', 'documentVersion', 'status', 'message'].sort()
  const union = interfaceBlock(source, 'AiEditorToolProposal')
  const statuses = [...new Set([...union.matchAll(/'([a-z_]+)'/g)].map((m) => m[1]))].sort()
  return {missing: expected.filter((key) => !keys.includes(key)), statuses}
}, {missing: [], statuses: ['applied', 'discarded', 'pending']})

observation('DIFF-010', async () => {
  const source = read('src/ai/types.ts')
  const keys = fields(source, 'AiEditorToolApplyResult').sort()
  const {editor} = makeEditor('abc')
  const result = ai.applyEditorToolProposal(editor, 3, {
    id: 'p', tool: 'search_document', title: '', description: '', arguments: {},
    documentVersion: 3, status: 'pending',
  })
  return {keys, okType: typeof result.ok, messageType: typeof result.message}
}, {keys: ['message', 'ok'], okType: 'boolean', messageType: 'string'})

observation('DIFF-011', async () => {
  const union = typeBlock(read('src/ai/types.ts'), 'AiEditorToolApproval')
  return {
    always: union.includes("'always'"),
    never: union.includes("'never'"),
    array: /AiEditorToolName\[\]/.test(union),
    predicate: /\(proposal:\s*AiEditorToolProposal\)\s*=>\s*boolean/.test(union),
  }
}, {always: true, never: true, array: true, predicate: true})

observation('DIFF-012', async () => {
  const union = typeBlock(read('src/ai/types.ts'), 'AiEditorToolName')
  const names = [...new Set([...union.matchAll(/'([a-z_]+)'/g)].map((m) => m[1]))].sort()
  return {names}
}, {names: [...TOOL_NAMES].sort()})

observation('DIFF-013', async () => {
  const source = read('src/ai/Providers.ts')
  const keys = fields(source, 'AiProviderDefinition').sort()
  return {missing: ['id', 'name', 'baseURL', 'model'].filter((key) => !keys.includes(key))}
}, {missing: []})

observation('DIFF-014', async () => {
  const ids = ['openai', 'deepseek', 'qwen', 'zhipu', 'moonshot']
  const providers = ids.map((id) => {
    const provider = ai.getAiProvider(id)
    return {id, baseURL: provider?.baseURL ?? null, model: provider?.model ?? null}
  })
  return {providers, missing: ai.getAiProvider('missing') === undefined}
}, {
  providers: [
    {id: 'openai', baseURL: null, model: 'gpt-4o-mini'},
    {id: 'deepseek', baseURL: 'https://api.deepseek.com', model: 'deepseek-chat'},
    {id: 'qwen', baseURL: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus'},
    {id: 'zhipu', baseURL: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash'},
    {id: 'moonshot', baseURL: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k'},
  ],
  missing: true,
})

observation('DIFF-015', async () => {
  return {
    tools: ai.editorFunctionTools.map((tool: any) => ({
      type: tool.type, name: tool.name, description: tool.description, parameters: tool.parameters,
    })),
  }
}, {
  tools: ai.editorFunctionTools.map((tool: any) => ({
    type: tool.type, name: tool.name, description: tool.description, parameters: tool.parameters,
  })),
})

observation('DIFF-016', async () => {
  const context = baseContext('x', '', 1, 1, 1)
  const result = ai.executeEditorFunctionTool('search_document', '{"query":"x"}', context)
  return {
    output: result.output,
    hasProposal: result.proposal !== undefined,
  }
}, {
  output: {ok: true, query: 'x', count: 1, matches: [{from: 1, to: 2, text: 'x', excerpt: 'x'}]},
  hasProposal: false,
})

observation('DIFF-017', async () => {
  const {editor, calls} = makeEditor('abc')
  const proposal = {
    id: 'p', tool: 'replace_selection', title: 'Replace selection', description: '',
    arguments: {text: 'z', _selection_from: 1, _selection_to: 3, _selection_text: 'ab'},
    documentVersion: 1, status: 'pending',
  }
  const result = ai.applyEditorToolProposal(editor, 1, proposal)
  return {result, status: proposal.status, transactions: calls.transactions}
}, {
  result: {ok: true, message: 'Applied to the document.'}, status: 'applied', transactions: 1,
})

observation('DIFF-018', async () => {
  const pkg = JSON.parse(read('package.json'))
  return {
    main: pkg.main,
    module: pkg.module,
    importTypes: pkg.exports['.'].import.types,
    importDefault: pkg.exports['.'].import.default,
    requireTypes: pkg.exports['.'].require.types,
    requireDefault: pkg.exports['.'].require.default,
  }
}, {
  main: './dist/index.cjs',
  module: './dist/index.js',
  importTypes: './dist/index.d.ts',
  importDefault: './dist/index.js',
  requireTypes: './dist/index.d.cts',
  requireDefault: './dist/index.cjs',
})

observation('DIFF-019', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  async function run(requestExtras: any) {
    const {client} = makeClient(() => streamFrom(streamEvents(['a', 'b', 'c'])))
    const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
    const chunks: string[] = []
    const result = await service.generate({prompt: 'go', onChunk: (chunk: string) => chunks.push(chunk), ...requestExtras}, baseContext())
    return {text: result.text, chunks}
  }
  return {
    stream: await run({stream: true}),
    onChunkOnly: await run({}),
    withTools: await run({stream: true, editorTools: true}),
  }
}, {
  stream: {text: 'abc', chunks: ['a', 'b', 'c']},
  onChunkOnly: {text: 'abc', chunks: ['a', 'b', 'c']},
  withTools: {text: 'abc', chunks: ['a', 'b', 'c']},
})

observation('DIFF-020', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client} = makeClient(() => textResponse('final'))
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  const plain = await service.generate({prompt: 'go'}, baseContext())
  const {client: toolClient} = makeClient((_request, index) => {
    if (index === 0) return toolResponse('get_editor_context', '{}', 'a')
    return textResponse('b')
  })
  const toolService = new ai.OpenAiService({client: toolClient, provider: 'openai', model: 'gpt-4o-mini'})
  const accumulated = await toolService.generate({prompt: 'go', editorTools: true}, baseContext())
  return {plain: plain.text, accumulated: accumulated.text}
}, {plain: 'final', accumulated: 'ab'})

observation('DIFF-021', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  let release: () => void = () => undefined
  const gate = new Promise<void>((resolve) => { release = resolve })
  const {client, requests} = makeClient(async () => {
    await gate
    return textResponse('ok')
  })
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  const context = baseContext('original', 'ori')
  const pending = service.generate({prompt: 'p'}, context)
  context.editor.state.doc.textContent = 'mutated'
  release()
  await pending
  const input = inputText(requests[0])
  return {
    originalPresent: input.includes('original'),
    selectedPresent: input.includes('ori'),
    mutatedPresent: input.includes('mutated'),
  }
}, {originalPresent: false, selectedPresent: true, mutatedPresent: false})

observation('DIFF-022', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  async function capture(requestExtras: any, text = 'doc', selectedText = '', buildInput?: any) {
    const {client, requests} = makeClient(() => textResponse('ok'))
    const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini', buildInput})
    await service.generate({prompt: 'p', ...requestExtras}, baseContext(text, selectedText))
    return inputText(requests[0])
  }
  return {
    history: await capture({history: [{role: 'user', content: 'u'}, {role: 'assistant', content: 'a'}]}, 'doc'),
    defaultWithSelection: await capture({}, 'doc', 'sel'),
    defaultDocument: await capture({}, 'doc'),
    selection: await capture({scope: 'selection'}, 'doc', 'sel'),
    document: await capture({scope: 'document'}, 'doc', 'sel'),
    none: await capture({scope: 'none'}, 'doc', 'sel'),
    override: await capture({}, 'doc', 'sel', () => 'OVERRIDE'),
  }
}, {
  history: 'User: u\nAssistant: a\nUser: p\n\nDocument:\ndoc',
  defaultWithSelection: 'p\n\nSelected text:\nsel',
  defaultDocument: 'p\n\nDocument:\ndoc',
  selection: 'p\n\nSelected text:\nsel',
  document: 'p\n\nDocument:\ndoc',
  none: 'p',
  override: 'OVERRIDE',
})

observation('DIFF-023', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client, requests} = makeClient(() => textResponse('ok'))
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  await service.generate({prompt: 'p', instructions: 'Be brief'}, baseContext('doc'))
  return {
    instructions: requests[0].instructions,
    userText: inputText(requests[0]),
  }
}, {instructions: 'Be brief', userText: 'p\n\nDocument:\ndoc'})

observation('DIFF-024', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client, requests} = makeClient((_request, index) => {
    if (index === 0) return toolResponse('replace_selection', '{"text":"z"}', 'a')
    return textResponse('b')
  })
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  const context = baseContext('abc', 'ab', 1, 3, 1)
  const result = await service.generate({prompt: 'go', editorTools: true}, context)
  return {
    text: result.text,
    requestCount: requests.length,
    proposals: result.toolProposals?.map((proposal: any) => ({tool: proposal.tool, status: proposal.status, documentVersion: proposal.documentVersion})),
    hasRaw: result.raw !== undefined,
  }
}, {
  text: 'ab',
  requestCount: 2,
  proposals: [{tool: 'replace_selection', status: 'pending', documentVersion: 1}],
  hasRaw: true,
})

observation('DIFF-025', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client, requests} = makeClient((_request, index) => {
    if (index === 0) return toolResponse('get_editor_context', '{}', '', 'call_1')
    return textResponse('done')
  })
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  await service.generate({prompt: 'go', editorTools: true}, baseContext())
  const second = requests[1]
  return {
    requestCount: requests.length,
    callIds: collectCallIds(second),
    outputs: toolOutputs(second).map((item: any) => ({call_id: item.call_id, type: item.type})),
  }
}, {
  requestCount: 2,
  callIds: ['call_1', 'call_1'],
  outputs: [{call_id: 'call_1', type: 'function_call_output'}],
})

observation('DIFF-026', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client, requests} = makeClient(() => toolResponse('get_editor_context', '{}', '', 'call_1'))
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  let message = ''
  try {
    await service.generate({prompt: 'go', editorTools: true}, baseContext())
  } catch (error: any) {
    message = error.message
  }
  return {requestCount: requests.length, message}
}, {requestCount: 8, message: 'AI exceeded the maximum editor tool call rounds'})

observation('DIFF-027', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  const {client} = makeClient(() => streamFrom(streamEvents(['a'], 'a', false)))
  const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
  let message = ''
  let returned = false
  try {
    await service.generate({prompt: 'go', stream: true, editorTools: true}, baseContext())
    returned = true
  } catch (error: any) {
    message = error.message
  }
  return {returned, message}
}, {returned: false, message: 'AI response stream ended before completion'})

observation('DIFF-028', async () => {
  process.env.OPENAI_API_KEY ||= 'test-key'
  async function run(thrown: any) {
    const {client, requests} = makeClient((_request, index) => {
      if (index === 0) return toolResponse('get_editor_context', '{}', '', 'call_1')
      return textResponse('done')
    })
    const service = new ai.OpenAiService({client, provider: 'openai', model: 'gpt-4o-mini'})
    const context = baseContext()
    context.editor.state.doc.descendants = () => { throw thrown }
    const result = await service.generate({prompt: 'go', editorTools: true}, context)
    const output = toolOutputs(requests[1])[0]?.output
    return {text: result.text, output: typeof output === 'string' ? JSON.parse(output) : output}
  }
  return {
    error: await run(new Error('bad args')),
    nonError: await run('bad args'),
  }
}, {
  error: {text: 'done', output: {ok: false, error: 'bad args'}},
  nonError: {text: 'done', output: {ok: false, error: 'Tool execution failed'}},
})

observation('DIFF-029', async () => {
  const context = baseContext('abc', 'ab', 1, 3, 7)
  const result = ai.executeEditorFunctionTool('replace_selection', '{"text":"x"}', context)
  const output = result.output
  return {
    ok: output.ok,
    status: output.status,
    hasProposalId: typeof output.proposal_id === 'string' && output.proposal_id.length > 0,
    proposal: result.proposal && {
      tool: result.proposal.tool,
      status: result.proposal.status,
      documentVersion: result.proposal.documentVersion,
    },
  }
}, {
  ok: true,
  status: 'pending_user_approval',
  hasProposalId: true,
  proposal: {tool: 'replace_selection', status: 'pending', documentVersion: 7},
})

observation('DIFF-030', async () => {
  const {editor} = makeEditor('unchanged')
  const result = ai.applyEditorToolProposal(editor, 1, {
    id: 'p', tool: 'search_document', title: '', description: '', arguments: {},
    documentVersion: 1, status: 'pending',
  })
  const readOnly = ai.applyEditorToolProposal(editor, 1, {
    id: 'p', tool: 'get_editor_context', title: '', description: '', arguments: {},
    documentVersion: 1, status: 'pending',
  })
  return {search: result, getContext: readOnly, text: editor.state.doc.textContent}
}, {
  search: {ok: false, message: 'search_document is read-only and cannot be applied.'},
  getContext: {ok: false, message: 'get_editor_context is read-only and cannot be applied.'},
  text: 'unchanged',
})

observation('DIFF-031', async () => {
  let duplicate = ''
  try {
    new ai.EditorToolRegistry([new ai.ReplaceSelectionTool(), new ai.ReplaceSelectionTool()])
  } catch (error: any) {
    duplicate = error.message
  }
  const registry = new ai.EditorToolRegistry([new ai.ReplaceSelectionTool(), new ai.SearchDocumentTool()])
  return {duplicate, uniqueCount: registry.tools.length}
}, {duplicate: 'Duplicate editor tool: replace_selection', uniqueCount: 2})

observation('DIFF-032', async () => {
  const execution = ai.executeEditorFunctionTool('missing', '{}', baseContext())
  const {editor} = makeEditor('unchanged')
  const applied = ai.applyEditorToolProposal(editor, 1, {
    id: 'p', tool: 'missing', title: '', description: '', arguments: {},
    documentVersion: 1, status: 'pending',
  })
  return {output: execution.output, applied, text: editor.state.doc.textContent}
}, {
  output: {ok: false, error: 'Unknown editor tool: missing'},
  applied: {ok: false, message: 'Unknown editor tool: missing'},
  text: 'unchanged',
})

describe('BATCH-DIFFERENTIAL-0001 shared case corpus', () => {
  const results: Record<string, any> = {}
  for (const [id, run, expected] of tests) {
    it(`${id} shared-case-corpus`, async () => {
      const actual = await run()
      results[id] = actual
      expect(actual).toEqual(expected)
      if (process.env.PROBE_OUT) {
        fs.writeFileSync(process.env.PROBE_OUT, JSON.stringify(results, null, 2))
      }
    })
  }
})
