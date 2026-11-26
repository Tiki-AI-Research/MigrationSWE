import {
  createBaseProcessor,
  processMarkdownToParts,
  processMarkdownToHtml,
  katexProcess,
  defaultCustomPlugins,
} from '__MODULE_PATH__'
import type {
  MiddlewarePluginItem,
  MarkdownPart,
  ProcessorType,
  ProcessMarkdownOptions,
} from '__MODULE_PATH__'

const options: ProcessMarkdownOptions = { onError: () => {} }
const item: MiddlewarePluginItem = {
  key: 'k',
  plugin: () => {},
  options: { x: 1 },
}
// @ts-expect-error MiddlewarePluginItem requires key
const missingKey: MiddlewarePluginItem = { plugin: () => {} }
const codePart: MarkdownPart = {
  type: 'code',
  content: 'x',
  language: 'js',
  loading: false,
}
// @ts-expect-error MarkdownPart only allows html or code
const invalidPart: MarkdownPart = { type: 'text', content: 'x' }
const processor: ProcessorType = createBaseProcessor([item])
const parts: Promise<MarkdownPart[]> = processMarkdownToParts('x', processor)
const html: Promise<string> = processMarkdownToHtml('x', processor)
const normalized: string = katexProcess('x')
const plugins: MiddlewarePluginItem[] = defaultCustomPlugins

void [
  options,
  missingKey,
  codePart,
  invalidPart,
  parts,
  html,
  normalized,
  plugins,
]
