import fs from 'node:fs'
import {afterAll, describe, expect, it} from 'vitest'
import {Editor} from '@tiptap/core'
import Document from '@tiptap/extension-document'
import Heading from '@tiptap/extension-heading'
import HorizontalRule from '@tiptap/extension-horizontal-rule'
import {BulletList, ListItem} from '@tiptap/extension-list'
import Paragraph from '@tiptap/extension-paragraph'
import Text from '@tiptap/extension-text'
import {Mapping, StepMap} from '@tiptap/pm/transform'

const env = process.env
const repoRoot = env.P2P_REPO_ROOT as string
const resultFile = env.P2P_RESULT_FILE as string
const ai = await import(env.P2P_AI_MODULE as string)
const tiny = await import(env.P2P_TINYUI_MODULE as string)
const blockDrag = await import(env.P2P_BLOCK_DRAG_MODULE as string)
const helper = await import(env.P2P_EDITOR_HELPER as string)
const root = await import(env.P2P_ROOT_INDEX_MODULE as string)

const results: Record<string, unknown> = {}
function record(id: string, value: unknown): void {
  results[id] = {status: 'PASS', value}
  fs.writeFileSync(resultFile, JSON.stringify(results, null, 2))
}

const response = (text: string, extra: Record<string, unknown> = {}) => ({
  id: (extra.id as string) ?? `resp_${Math.random().toString(16).slice(2)}`,
  object: 'response',
  created_at: 0,
  status: 'completed',
  model: 'test-model',
  output_text: text,
  output: [
    {
      id: `msg_${Math.random().toString(16).slice(2)}`,
      type: 'message',
      role: 'assistant',
      status: 'completed',
      content: [{type: 'output_text', text, annotations: [], logprobs: []}],
    },
  ],
  usage: {
    input_tokens: 1,
    output_tokens: 1,
    total_tokens: 2,
    input_tokens_details: {cached_tokens: 0},
    output_tokens_details: {reasoning_tokens: 0},
  },
  ...extra,
})

const functionCall = (callId: string, name: string, args: string, text = '') => response(text, {
  output: [
    ...(text ? [{id: `msg_${callId}`, type: 'message', role: 'assistant', status: 'completed', content: [{type: 'output_text', text}]}] : []),
    {id: `fc_${callId}`, type: 'function_call', call_id: callId, name, arguments: args, status: 'completed'},
  ],
})

const reasonOf = (signal: AbortSignal) => signal.reason instanceof Error ? signal.reason : new Error('aborted')

class FakeClient {
  calls: Array<{params: Record<string, unknown>; options: Record<string, unknown>}> = []
  responses: {create: (params: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown>}
  constructor(public handler: (params: Record<string, unknown>, index: number, options: Record<string, unknown>) => unknown) {
    this.responses = {
      create: (params, options = {}) => {
        this.calls.push({params, options})
        const signal = options.signal as AbortSignal | undefined
        const run = () => Promise.resolve(this.handler(params, this.calls.length - 1, options))
        if (signal?.aborted) return Promise.reject(reasonOf(signal))
        return new Promise((resolve, reject) => {
          const onAbort = () => reject(reasonOf(signal!))
          signal?.addEventListener('abort', onAbort, {once: true})
          run().then((value) => {
            signal?.removeEventListener('abort', onAbort)
            resolve(value)
          }, (error) => {
            signal?.removeEventListener('abort', onAbort)
            reject(error)
          })
        })
      },
    }
  }
}

function inputText(input: unknown): string {
  if (typeof input === 'string') return input
  if (Array.isArray(input) && input.length === 1 && input[0] && typeof input[0] === 'object') {
    const content = (input[0] as Record<string, unknown>).content
    if (typeof content === 'string') return content
  }
  return JSON.stringify(input)
}

function contextFor(editor: Editor, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    editor,
    html: '<p>hello</p>',
    text: 'hello',
    selectedText: '',
    selection: {from: 1, to: 3},
    documentVersion: 2,
    ...overrides,
  }
}

function baseConfig(client: FakeClient, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {provider: 'openai', model: 'gpt-4o-mini', client, ...extra}
}

describe('BATCH-P2P-0002 probes', () => {
  it('P2P-045 UIResolver nested, missing, shadowing', () => {
    const resolver = new tiny.UIResolver()
    const scope = {
      context: {user: {profile: {name: 'Context'}}, nullable: null},
      locals: {user: {profile: {name: 'Local'}}},
    }
    record('P2P-045', {
      local: resolver.resolve('user.profile.name', scope),
      nullableUndefined: resolver.resolve('nullable.value', scope) === undefined,
      missingUndefined: resolver.resolve('missing.value', scope) === undefined,
      dependency: resolver.dependency(' user.profile.name '),
    })
  })

  it('P2P-046 clears pending bindings without updating', () => {
    class Binding extends tiny.UIBinding {
      dependencies = new Set(['value'])
      count = 0
      update = () => { this.count += 1 }
      destroy = () => undefined
    }
    const queue = new tiny.UIUpdateQueue()
    const binding = new Binding()
    queue.enqueue(binding)
    queue.clear()
    queue.flush(null)
    record('P2P-046', {updateCount: binding.count})
  })

  it('P2P-033 closes quick insert when clicking outside', () => {
    const host = document.createElement('div')
    document.body.append(host)
    const editor = new Editor({
      element: host,
      extensions: [Document, Paragraph, Text, Heading, HorizontalRule, ListItem, BulletList],
      content: '<p>First</p><p>Second</p>',
    })
    try {
      editor.registerPlugin(blockDrag.createBlockDragMenuPlugin(editor, {}))
      const first = editor.view.dom.firstElementChild as HTMLElement
      first.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}))
      const menu = editor.view.dom.parentElement?.querySelector('.aieditor__block-drag-menu') as HTMLElement
      const quick = menu.querySelector('.aieditor__block-quick-insert') as HTMLElement
      ;(menu.querySelector('.aieditor__block-quick-trigger') as HTMLButtonElement).click()
      const openedHidden = quick.hidden
      editor.view.dom.dispatchEvent(new MouseEvent('click', {bubbles: true}))
      const closedHidden = quick.hidden
      record('P2P-033', {openedHidden, closedHidden})
    } finally {
      editor.destroy()
      host.remove()
    }
  })

  it('P2P-025 apply result shape', () => {
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const proposal = {
        id: 'p1', tool: 'replace_selection', title: 'Replace', description: 'Replace',
        arguments: {text: 'hi', _selection_from: 1, _selection_to: 6},
        documentVersion: 2, status: 'pending',
      }
      const applied = ai.applyEditorToolProposal(editor, 2, proposal)
      const rejected = ai.applyEditorToolProposal(editor, 2, proposal)
      record('P2P-025', {
        appliedOk: applied.ok, appliedMessage: applied.message,
        rejectedOk: rejected.ok, rejectedHasMessage: typeof rejected.message === 'string' && rejected.message.length > 0,
      })
    } finally { editor.destroy() }
  })

  it('P2P-026 createAiService results', async () => {
    const existing = {generate: async () => ({text: 'existing'})}
    const same = ai.createAiService(existing)
    const custom = ai.createAiService({provider: 'custom', generate: async () => 'ok'})
    const customResult = await custom.generate({prompt: 'x'}, contextFor(helper.createTestEditor('<p>x</p>')))
    const openai = ai.createAiService({provider: 'openai', model: 'gpt-4o-mini', client: new FakeClient(() => response('ok'))})
    record('P2P-026', {
      existingSame: same === existing,
      customText: customResult.text,
      openaiIsService: openai instanceof ai.OpenAiService,
    })
  })

  it('P2P-027 OpenAiServiceConfig accepted fields', () => {
    const client = new FakeClient(() => response('ok'))
    let constructed = false
    try {
      const service = new ai.OpenAiService({
        provider: 'openai', providers: [{id: 'openai', name: 'OpenAI', model: 'gpt-4o-mini'}],
        model: 'gpt-4o-mini', apiKey: 'k', baseURL: 'https://example.test/v1', client,
        organization: 'org', project: 'proj', timeout: 100, maxRetries: 1,
        defaultHeaders: {'x-test': '1'}, fetch: async () => response('ok') as never,
        fetchOptions: {}, dangerouslyAllowBrowser: true, response: {temperature: 0.2},
        buildInput: () => 'custom',
      })
      constructed = service.client === client
    } catch { constructed = false }
    record('P2P-027', {constructed, clientReused: constructed})
  })

  it('P2P-028 package exports map', () => {
    const pkg = JSON.parse(fs.readFileSync(`${repoRoot}/package.json`, 'utf8'))
    record('P2P-028', {
      main: pkg.main, module: pkg.module,
      importTypes: pkg.exports['.'].import.types, importDefault: pkg.exports['.'].import.default,
      requireTypes: pkg.exports['.'].require.types, requireDefault: pkg.exports['.'].require.default,
    })
  })

  it('P2P-029 rebase coordinate mapping', () => {
    const proposal = {
      id: 'p1', tool: 'replace_selection', title: 'Replace', description: 'Replace',
      documentVersion: 2, status: 'pending',
      arguments: {
        _selection_from: 8, _selection_to: 13, _insert_position: 13,
        _range_from: 8, _range_to: 13, edits: [{from: 8, to: 13, old_text: 'Hello', new_text: 'Hi'}],
      },
    }
    ai.rebaseEditorToolProposal(proposal, new Mapping([new StepMap([1, 5, 1])]), 3)
    record('P2P-029', {
      documentVersion: proposal.documentVersion,
      _selection_from: proposal.arguments._selection_from,
      _selection_to: proposal.arguments._selection_to,
      _insert_position: proposal.arguments._insert_position,
      _range_from: proposal.arguments._range_from,
      _range_to: proposal.arguments._range_to,
      edit: {from: proposal.arguments.edits[0].from, to: proposal.arguments.edits[0].to},
    })
  })

  it('P2P-030 tool output call id continuity', async () => {
    const client = new FakeClient((params, index) => index === 0
      ? functionCall('call_1', 'search_document', '{"query":"x"}')
      : response('done'))
    const service = new ai.OpenAiService(baseConfig(client) as never)
    const editor = helper.createTestEditor('<p>x</p>')
    try {
      const result = await service.generate({prompt: 'go', editorTools: true}, contextFor(editor) as never)
      const secondInput = client.calls[1]?.params.input
      const outputs = Array.isArray(secondInput)
        ? secondInput.filter((item: Record<string, unknown>) => item.type === 'function_call_output')
          .map((item: Record<string, unknown>) => ({type: item.type, call_id: item.call_id, output: JSON.parse(String(item.output))}))
        : []
      record('P2P-030', {
        text: result.text,
        outputCount: outputs.length,
        callIds: outputs.map((item: Record<string, unknown>) => item.call_id),
        outputOk: outputs[0]?.output?.ok,
      })
    } finally { editor.destroy() }
  })

  it('P2P-031 default input composition', async () => {
    const client = new FakeClient(() => response('ok'))
    const service = new ai.OpenAiService(baseConfig(client) as never)
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const ctx = contextFor(editor, {text: 'doc', selectedText: '', selection: {from: 1, to: 1}})
      await service.generate({prompt: 'p', history: [{role: 'user', content: 'u'}, {role: 'assistant', content: 'a'}]}, ctx as never)
      const documentInput = inputText(client.calls[0].params.input)
      await service.generate({prompt: 'p', scope: 'selection'}, contextFor(editor, {text: 'doc', selectedText: 'sel'}) as never)
      const selectionInput = inputText(client.calls[1].params.input)
      await service.generate({prompt: 'p', scope: 'none'}, contextFor(editor, {text: 'doc', selectedText: 'sel'}) as never)
      const noneInput = inputText(client.calls[2].params.input)
      const overrideService = new ai.OpenAiService(baseConfig(client, {buildInput: () => 'CUSTOM'}) as never)
      await overrideService.generate({prompt: 'p'}, contextFor(editor, {text: 'doc', selectedText: 'sel'}) as never)
      const overrideInput = inputText(client.calls[3].params.input)
      record('P2P-031', {documentInput, selectionInput, noneInput, overrideInput})
    } finally { editor.destroy() }
  })

  it('P2P-032 duplicate editor tools', () => {
    let duplicateMessage = ''
    try { new ai.EditorToolRegistry([new ai.ReplaceSelectionTool(), new ai.ReplaceSelectionTool()]) } catch (error) { duplicateMessage = (error as Error).message }
    let uniqueConstructs = false
    try { new ai.EditorToolRegistry([new ai.ReplaceSelectionTool(), new ai.SearchDocumentTool()]); uniqueConstructs = true } catch { uniqueConstructs = false }
    record('P2P-032', {duplicateMessage, uniqueConstructs})
  })

  it('P2P-034 proposal tool output and initial status', () => {
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const ctx = contextFor(editor, {selection: {from: 1, to: 3}, documentVersion: 7})
      const execution = ai.executeEditorFunctionTool('replace_selection', '{"text":"x"}', ctx as never)
      record('P2P-034', {
        outputOk: execution.output.ok,
        status: execution.output.status,
        hasProposalId: typeof execution.output.proposal_id === 'string' && execution.output.proposal_id.length > 0,
        proposalStatus: execution.proposal?.status,
        proposalDocumentVersion: execution.proposal?.documentVersion,
      })
    } finally { editor.destroy() }
  })

  it('P2P-035 editor tool loop accumulated result', async () => {
    const client = new FakeClient((_params, index) => index === 0
      ? functionCall('call_1', 'replace_selection', '{"text":"X"}', 'a')
      : response('b'))
    const service = new ai.OpenAiService(baseConfig(client) as never)
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const result = await service.generate({prompt: 'go', editorTools: true}, contextFor(editor, {selection: {from: 1, to: 3}}) as never)
      record('P2P-035', {
        text: result.text,
        proposalCount: result.toolProposals?.length ?? 0,
        proposalTool: result.toolProposals?.[0]?.tool,
        hasRaw: Boolean(result.raw),
        calls: client.calls.length,
      })
    } finally { editor.destroy() }
  })

  it('P2P-036 request-time context snapshot isolation', async () => {
    const inputs: string[] = []
    let release: () => void = () => undefined
    let gate = new Promise<void>((resolve) => { release = resolve })
    const client = new FakeClient(async (params) => {
      inputs.push(inputText(params.input))
      await gate
      return response('ok')
    })
    const service = new ai.OpenAiService(baseConfig(client) as never)
    const editor = helper.createTestEditor('<p>original</p>')
    try {
      const ctx = contextFor(editor, {text: 'original', selectedText: 'ori'})
      const pendingSelection = service.generate({prompt: 'go'}, ctx as never)
      while (client.calls.length < 1) await new Promise((r) => setTimeout(r, 1))
      editor.commands.setContent('<p>mutated</p>')
      release()
      await pendingSelection
      const selectionInput = inputs[0]

      gate = new Promise<void>((resolve) => { release = resolve })
      const ctx2 = contextFor(editor, {text: 'original', selectedText: 'ori'})
      const pendingDocument = service.generate({prompt: 'go', scope: 'document'}, ctx2 as never)
      while (client.calls.length < 2) await new Promise((r) => setTimeout(r, 1))
      editor.commands.setContent('<p>mutated-again</p>')
      release()
      await pendingDocument
      const documentInput = inputs[1]
      record('P2P-036', {
        selectionInput,
        documentInput,
        selectionHasSnapshot: selectionInput.includes('ori') && !selectionInput.includes('mutated'),
        documentHasSnapshot: documentInput.includes('original') && !documentInput.includes('mutated'),
      })
    } finally { editor.destroy() }
  })

  it('P2P-037 model precedence and blank model', async () => {
    const client = new FakeClient(() => response('ok'))
    const configService = new ai.OpenAiService({provider: {id: 'p', name: 'P', model: 'provider-model'}, model: 'config-model', client} as never)
    const editor = helper.createTestEditor('<p>x</p>')
    try {
      await configService.generate({prompt: 'go'}, contextFor(editor) as never)
      const configModel = client.calls[0].params.model
      const providerService = new ai.OpenAiService({provider: {id: 'p', name: 'P', model: 'provider-model'}, client} as never)
      await providerService.generate({prompt: 'go'}, contextFor(editor) as never)
      const providerModel = client.calls[1].params.model
      let blankError = ''
      const callsBefore = client.calls.length
      try { new ai.OpenAiService({provider: {id: 'p', name: 'P', model: 'm'}, model: '   ', client} as never) } catch (error) { blankError = (error as Error).message }
      record('P2P-037', {configModel, providerModel, blankError, blankCalls: client.calls.length - callsBefore})
    } finally { editor.destroy() }
  })

  it('P2P-038 stale document version rejection', () => {
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const proposal = {
        id: 'p1', tool: 'replace_selection', title: 'Replace', description: 'Replace',
        arguments: {text: 'hi', _selection_from: 1, _selection_to: 6},
        documentVersion: 2, status: 'pending',
      }
      const result = ai.applyEditorToolProposal(editor, 3, proposal)
      record('P2P-038', {ok: result.ok, message: result.message, text: editor.getText()})
    } finally { editor.destroy() }
  })

  it('P2P-039 abort propagation', async () => {
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const preClient = new FakeClient(() => response('partial'))
      const preService = new ai.OpenAiService(baseConfig(preClient) as never)
      const preAborted = new AbortController()
      preAborted.abort(new Error('stop'))
      let preMessage = ''
      let preResolved = false
      try {
        const result = await preService.generate({prompt: 'go', signal: preAborted.signal}, contextFor(editor) as never)
        preResolved = Boolean(result)
      } catch (error) { preMessage = (error as Error).message }

      let release: () => void = () => undefined
      const gate = new Promise<void>((resolve) => { release = resolve })
      const inClient = new FakeClient(async () => {
        await gate
        return response('partial')
      })
      const inService = new ai.OpenAiService(baseConfig(inClient) as never)
      const controller = new AbortController()
      let inMessage = ''
      let inResolved = false
      const pending = inService.generate({prompt: 'go', signal: controller.signal}, contextFor(editor) as never)
      while (inClient.calls.length < 1) await new Promise((r) => setTimeout(r, 1))
      controller.abort(new Error('stop'))
      try {
        const result = await pending
        inResolved = Boolean(result)
      } catch (error) { inMessage = (error as Error).message }
      release()
      record('P2P-039', {preResolved, preMessage, inResolved, inMessage})
    } finally { editor.destroy() }
  })

  it('P2P-040 public exports', () => {
    const names = [
      'OpenAiService', 'createAiService', 'AI_PROVIDERS', 'registerAiProvider', 'getAiProviders', 'getAiProvider',
      'editorTools', 'editorFunctionTools', 'executeEditorFunctionTool', 'applyEditorToolProposal',
      'defaultEditorToolRegistry', 'defaultEditorTools', 'EditorToolRegistry', 'EditorTool', 'EditorMutationTool',
      'ApplyFormattingTool', 'ApplyTextEditsTool', 'GetEditorContextTool', 'InsertContentTool', 'ReplaceSelectionTool', 'SearchDocumentTool',
    ]
    record('P2P-040', {
      aiMissing: names.filter((name) => !(name in ai)),
      rootMissing: names.filter((name) => !(name in root)),
      rebaseMissingFromAi: !('rebaseEditorToolProposal' in ai),
    })
  })

  it('P2P-041 read-only apply rejection', () => {
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const messages: string[] = []
      for (const tool of ['search_document', 'get_editor_context']) {
        const result = ai.applyEditorToolProposal(editor, 2, {
          id: 'p1', tool, title: 't', description: 'd', arguments: {},
          documentVersion: 2, status: 'pending',
        })
        messages.push(result.message)
      }
      record('P2P-041', {messages, text: editor.getText()})
    } finally { editor.destroy() }
  })

  it('P2P-042 non-pending apply rejection', () => {
    const editor = helper.createTestEditor('<p>hello</p>')
    try {
      const results: unknown[] = []
      for (const status of ['applied', 'discarded']) {
        const result = ai.applyEditorToolProposal(editor, 2, {
          id: 'p1', tool: 'replace_selection', title: 't', description: 'd',
          arguments: {text: 'x', _selection_from: 1, _selection_to: 6},
          documentVersion: 2, status,
        })
        results.push({ok: result.ok, message: result.message})
      }
      record('P2P-042', {results, text: editor.getText()})
    } finally { editor.destroy() }
  })

  it('P2P-043 editor tool names', () => {
    record('P2P-043', {
      names: ai.editorFunctionTools.map((definition: {name: string}) => definition.name),
      toolNames: ai.editorTools.map((tool: {name: string}) => tool.name),
    })
  })

  it('P2P-044 editor function tool definitions', () => {
    const expected = {
      get_editor_context: ['type', 'properties', 'required', 'additionalProperties'],
      search_document: ['type', 'properties', 'required', 'additionalProperties'],
      replace_selection: ['type', 'properties', 'required', 'additionalProperties'],
      insert_content: ['type', 'properties', 'required', 'additionalProperties'],
      apply_formatting: ['type', 'properties', 'required', 'additionalProperties'],
      apply_text_edits: ['type', 'properties', 'required', 'additionalProperties'],
    }
    const definitions = ai.editorFunctionTools.map((definition: {name: string; description: string; strict: boolean; parameters: Record<string, unknown>}) => ({
      name: definition.name,
      description: definition.description,
      strict: definition.strict,
      schemaKeys: Object.keys(definition.parameters),
      propertyKeys: Object.keys(definition.parameters.properties ?? {}),
      required: definition.parameters.required,
    }))
    record('P2P-044', {definitions, expectedSchemaKeys: expected})
  })
})

afterAll(() => {
  fs.writeFileSync(resultFile, JSON.stringify(results, null, 2))
})
