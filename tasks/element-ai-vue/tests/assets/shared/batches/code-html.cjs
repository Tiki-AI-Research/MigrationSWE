// Compare the required code structure without imposing attribute ordering or
// forbidding unrelated attributes supplied by middleware.
const codeBlockMatches = (html, escapedContent, language = '') => {
  const blocks = String(html).matchAll(/<pre\b[^>]*>\s*<code\b([^>]*)>([\s\S]*?)<\/code>\s*<\/pre>/gi)
  for (const [, attributes, body] of blocks) {
    const attrs = new Map()
    for (const match of attributes.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
      attrs.set(match[1].toLowerCase(), match[2] ?? match[3] ?? match[4] ?? '')
    }
    if (language && !(attrs.get('class') || '').split(/\s+/).includes(`language-${language}`)) continue
    if (body.replace(/\r\n?/g, '\n').replace(/\n$/, '') === escapedContent) return true
  }
  return false
}

module.exports = { codeBlockMatches }
