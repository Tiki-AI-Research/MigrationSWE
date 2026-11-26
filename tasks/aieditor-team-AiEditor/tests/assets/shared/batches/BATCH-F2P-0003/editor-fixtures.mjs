export function createFakeEditor(options = {}) {
  const text = options.text ?? 'hello world'
  const size = text.length + 2
  const textNode = {isText: true, text, nodeSize: text.length}
  const textblock = {
    isTextblock: true,
    isText: false,
    type: {name: 'paragraph'},
    textContent: text,
    nodeSize: size,
    descendants(callback) {
      callback(textNode, 0)
      return true
    },
  }
  const throwOnRead = options.throwOnRead ?? null
  return {
    state: {
      doc: {
        content: {size},
        textBetween(from, to) {
          if (throwOnRead) throw throwOnRead
          return text.slice(Math.max(0, from - 1), Math.max(0, to - 1))
        },
        descendants(callback) {
          if (throwOnRead) throw throwOnRead
          callback(textblock, 0)
          return true
        },
      },
      storedMarks: null,
      selection: {
        $from: {
          parent: {type: {name: 'paragraph'}},
          marks() {
            return []
          },
        },
        from: options.selection?.from ?? 1,
        to: options.selection?.to ?? 1,
      },
    },
  }
}

export function createContext(options = {}) {
  const editor = options.editor ?? createFakeEditor(options)
  const text = options.text ?? 'hello world'
  return {
    editor,
    html: options.html ?? `<p>${text}</p>`,
    text,
    selectedText: options.selectedText ?? '',
    selection: options.selection ?? {from: 1, to: 1},
    documentVersion: options.documentVersion ?? 1,
  }
}
