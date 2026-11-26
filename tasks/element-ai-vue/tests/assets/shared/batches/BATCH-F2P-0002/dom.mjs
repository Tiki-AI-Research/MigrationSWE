import { JSDOM } from 'jsdom'

if (!globalThis.window || !globalThis.document) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>')
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  globalThis.Node = dom.window.Node
  globalThis.Element = dom.window.Element
  globalThis.HTMLElement = dom.window.HTMLElement
  globalThis.NodeFilter = dom.window.NodeFilter
}
