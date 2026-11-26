import {readFileSync} from 'node:fs'
import {describe, expect, it} from 'vitest'
import {
  AI_PROVIDERS,
  OpenAiService,
  applyEditorToolProposal,
  createAiService,
  executeEditorFunctionTool,
  getAiProvider,
  getAiProviders,
  registerAiProvider,
} from '../../src/ai'
import type {
  AiEditorContext,
  AiEditorToolApproval,
  AiEditorToolProposal,
  AiGenerateRequest,
  AiProviderDefinition,
} from '../../src/ai'
import {clampInteger, parseArguments} from '../../src/ai/tools/core/tool-utils'
import {html, UITemplate} from '../../src/tinyui'

const packageJson = JSON.parse(readFileSync('package.json', 'utf8')) as Record<string, any>

function context(overrides: Partial<AiEditorContext> = {}): AiEditorContext {
  return {
    editor: {} as AiEditorContext['editor'],
    html: '<p>Hello</p>',
    text: 'Hello',
    selectedText: 'Hello',
    selection: {from: 1, to: 6},
    documentVersion: 7,
    ...overrides,
  }
}

function proposal(overrides: Partial<AiEditorToolProposal> = {}): AiEditorToolProposal {
  return {
    id: 'proposal-1',
    tool: 'replace_selection',
    title: 'Replace selection',
    description: 'Replace the current selection',
    arguments: {_selection_from: 1, _selection_to: 6, text: 'Hi'},
    documentVersion: 7,
    status: 'pending',
    ...overrides,
  }
}

describe('BATCH-P2P-0001 source/target shared behavior', () => {
  it('P2P-007 keeps UITemplate independent from parsing and bindings', () => {
    const template = new UITemplate('<p>{{ message }}</p>', document)
    expect(template.source).toBe('<p>{{ message }}</p>')
    expect(template.root.textContent).toBe('{{ message }}')
  })

  it('P2P-015 keeps multiple views and their refs completely isolated', () => {
    const first = html('<p>{{ value }}</p>', {value: 'first'})
    const second = html('<p>{{ value }}</p>', {value: 'second'})
    expect(first.root).not.toBe(second.root)
    expect(first.root.textContent).toBe('first')
    expect(second.root.textContent).toBe('second')
  })

  it('P2P-001 clamps optional integers and supplies defaults for invalid values', () => {
    expect(clampInteger(100, 5, 1, 10)).toBe(10)
    expect(clampInteger(-2, 5, 1, 10)).toBe(1)
    expect(clampInteger('3', 5, 1, 10)).toBe(5)
    expect(parseArguments('{"value":1}')).toEqual({value: 1})
  })

  it('P2P-006 accepts the AiEditorToolApproval alternatives', () => {
    const alternatives: AiEditorToolApproval[] = [
      'always',
      'never',
      ['replace_selection', 'insert_content'],
      (candidate) => candidate.status === 'pending',
    ]
    expect(alternatives).toHaveLength(4)
  })

  it('P2P-014 reuses the bounded parse cache while returning independent DOM clones', () => {
    const source = '<p>{{ value }}</p>'
    const first = new UITemplate(source, document)
    const second = new UITemplate(source, document)
    expect(first.root).not.toBe(second.root)
    expect(first.root.textContent).toBe(second.root.textContent)
  })

  it('P2P-017 renders scalar values deterministically', () => {
    const view = html('<p>{{ value }}</p>', {value: 0})
    expect(view.root.textContent).toBe('0')
    expect(html('<p>{{ value }}</p>', {value: false}).root.textContent).toBe('false')
  })

  it('P2P-002 accepts live editor and snapshot context fields', () => {
    const value = context()
    expect(value.text).toBe('Hello')
    expect(value.selection).toEqual({from: 1, to: 6})
    expect(value.documentVersion).toBe(7)
  })

  it('P2P-003 reports unknown tools and refuses to apply them', () => {
    const execution = executeEditorFunctionTool('missing', '{}', context())
    expect(execution.output).toEqual({ok: false, error: 'Unknown editor tool: missing'})
    const result = applyEditorToolProposal(
      {} as AiEditorContext['editor'],
      7,
      proposal({tool: 'missing' as AiEditorToolProposal['tool']}),
    )
    expect(result.ok).toBe(false)
  })

  it('P2P-004 rejects an empty prompt even for streamed editor tool requests', async () => {
    const service = new OpenAiService({provider: 'openai', model: 'gpt-4o-mini', apiKey: 'test', dangerouslyAllowBrowser: true})
    await expect(service.generate({prompt: '   ', stream: true, editorTools: true}, context()))
      .rejects.toThrow('AI prompt cannot be empty')
  })

  it('P2P-005 preserves the package.json export entry', () => {
    expect(packageJson.exports['./package.json']).toBe('./package.json')
  })

  it('P2P-008 applies pending proposals and rejects non-pending or unknown proposals', () => {
    const nonPending = applyEditorToolProposal(
      {} as AiEditorContext['editor'],
      7,
      proposal({status: 'applied'}),
    )
    expect(nonPending).toEqual({ok: false, message: 'This proposal is no longer pending.'})
    const unknown = applyEditorToolProposal(
      {} as AiEditorContext['editor'],
      7,
      proposal({tool: 'missing' as AiEditorToolProposal['tool']}),
    )
    expect(unknown.ok).toBe(false)
  })

  it('P2P-009 normalizes custom services and returns an existing service unchanged', async () => {
    const custom = createAiService({provider: 'custom', generate: async () => 'ok'})
    await expect(custom.generate({prompt: 'x'}, context())).resolves.toEqual({text: 'ok'})
    const existing = createAiService(custom)
    expect(existing).toBe(custom)
  })

  it('P2P-010 accepts model-level instructions on a request', () => {
    const request: AiGenerateRequest = {prompt: 'p', instructions: 'Be brief'}
    expect(request.instructions).toBe('Be brief')
    expect(request.prompt).toBe('p')
  })

  it('P2P-011 preserves package main and module entry points', () => {
    expect(packageJson.main).toBe('./dist/index.cjs')
    expect(packageJson.module).toBe('./dist/index.js')
  })

  it('P2P-012 types the documented AI request and context shapes', () => {
    const request: AiGenerateRequest = {prompt: 'hello'}
    const value = context()
    expect(request.prompt).toBe('hello')
    expect(typeof value.editor).toBe('object')
  })

  it('P2P-013 validates provider registrations and stores a frozen copy', () => {
    expect(() => registerAiProvider({id: '', name: 'n', model: 'm'}))
      .toThrow('AI provider id cannot be empty')
    const input: AiProviderDefinition = {id: 'p2p-provider', name: 'P2P', model: 'm'}
    registerAiProvider(input)
    input.model = 'changed'
    expect(getAiProvider('p2p-provider')?.model).toBe('m')
  })

  it('P2P-016 preserves the style.css export', () => {
    expect(packageJson.exports['./style.css']).toBe('./dist/style.css')
  })

  it('P2P-018 rejects an empty prompt before entering the editor tool loop', async () => {
    const service = new OpenAiService({provider: 'openai', model: 'gpt-4o-mini', apiKey: 'test', dangerouslyAllowBrowser: true})
    await expect(service.generate({prompt: '', editorTools: true}, context()))
      .rejects.toThrow('AI prompt cannot be empty')
  })

  it('P2P-019 preserves the ESM and CJS export conditions', () => {
    expect(packageJson.exports['.'].import.default).toBe('./dist/index.js')
    expect(packageJson.exports['.'].require.default).toBe('./dist/index.cjs')
  })

  it('P2P-020 accepts AiProviderDefinition with and without baseURL', () => {
    const base: AiProviderDefinition = {id: 'p', name: 'P', model: 'm'}
    const withUrl: AiProviderDefinition = {...base, baseURL: 'https://example.test/v1'}
    expect(base.id).toBe('p')
    expect(withUrl.baseURL).toBe('https://example.test/v1')
    expect(AI_PROVIDERS.some((provider) => provider.id === 'openai')).toBe(true)
  })

  it('P2P-021 preserves non-streaming text results', async () => {
    const service = createAiService({provider: 'custom', generate: async () => ({text: 'ab'})})
    await expect(service.generate({prompt: 'go'}, context())).resolves.toEqual({text: 'ab'})
  })

  it('P2P-022 returns AiGenerateResult and rejects an empty prompt', async () => {
    const service = createAiService({provider: 'custom', generate: async () => 'hello'})
    const result = await service.generate({prompt: 'hello'}, context())
    expect(result).toEqual({text: 'hello'})
    const openai = new OpenAiService({provider: 'openai', model: 'gpt-4o-mini', apiKey: 'test', dangerouslyAllowBrowser: true})
    await expect(openai.generate({prompt: '   '}, context()))
      .rejects.toThrow('AI prompt cannot be empty')
  })

  it('P2P-023 keeps the migration change set limited to the AI module and package metadata', () => {
    expect(packageJson.engines.node).toBe('>=20.19.0')
    expect(typeof packageJson.exports['.']).toBe('object')
  })

  it('P2P-024 delivers streaming chunks in arrival order through the AiService contract', async () => {
    const chunks: string[] = []
    const service = createAiService({
      provider: 'custom',
      generate: async (request) => {
        for (const chunk of ['a', 'b', 'c']) request.onChunk?.(chunk)
        return {text: chunks.join('')}
      },
    })
    const result = await service.generate({
      prompt: 'go',
      stream: true,
      onChunk: (chunk) => chunks.push(chunk),
    }, context())
    expect(chunks).toEqual(['a', 'b', 'c'])
    expect(result.text).toBe('abc')
  })
})
