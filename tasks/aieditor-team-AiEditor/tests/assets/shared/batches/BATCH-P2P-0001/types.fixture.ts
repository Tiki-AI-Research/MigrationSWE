import type {
  AiChatMessage,
  AiContentScope,
  AiEditorContext,
  AiEditorToolApplyResult,
  AiEditorToolApproval,
  AiEditorToolName,
  AiEditorToolProposal,
  AiGenerateRequest,
  AiGenerateResult,
  AiProviderDefinition,
  AiService,
  AiServiceConfig,
  CustomAiServiceConfig,
  OpenAiServiceConfig,
} from '../../src/ai'

const scope: AiContentScope = 'selection'
const message: AiChatMessage = {role: 'user', content: 'hello'}
const toolName: AiEditorToolName = 'replace_selection'
const approval: AiEditorToolApproval = 'always'
const applyResult: AiEditorToolApplyResult = {ok: true, message: 'Applied to the document.'}
const result: AiGenerateResult = {text: 'ok'}
const request: AiGenerateRequest = {
  prompt: 'hello',
  instructions: 'Be brief',
  scope,
  metadata: {source: 'fixture'},
  history: [message],
  stream: true,
  onChunk: () => undefined,
  editorTools: true,
}
const editorToolProposal: AiEditorToolProposal = {
  id: 'fixture',
  tool: toolName,
  title: 'Replace selection',
  description: 'Replace the current selection',
  arguments: {text: 'hello'},
  documentVersion: 1,
  status: 'pending',
}
const provider: AiProviderDefinition = {id: 'fixture', name: 'Fixture', model: 'fixture-model'}
const custom: CustomAiServiceConfig = {provider: 'custom', generate: async () => result}
const config: AiServiceConfig = custom
const openAiConfig: OpenAiServiceConfig = {
  provider: 'openai',
  providers: [provider],
  model: 'gpt-4o-mini',
  apiKey: 'test',
  baseURL: 'https://example.test/v1',
  organization: 'org',
  project: 'project',
  timeout: 1000,
  maxRetries: 1,
  defaultHeaders: {'x-test': '1'},
  dangerouslyAllowBrowser: false,
  response: {temperature: 0.1},
  buildInput: () => 'input',
}
const service: AiService = {generate: async () => result}
const context = {} as AiEditorContext

void approval
void applyResult
void request
void editorToolProposal
void config
void openAiConfig
void service
void context
