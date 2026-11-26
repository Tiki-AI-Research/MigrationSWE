'use strict'

const DIRECTIONS = new Set(['left', 'center', 'right'])

function effectiveAlignment(align, style) {
  const declarations = [...String(style ?? '').matchAll(/(?:^|;)\s*text-align\s*:\s*([^;]+)/gi)]
  if (declarations.length > 0) {
    const important = declarations.filter((match) => /!important\s*$/i.test(match[1]))
    const value = (important.length > 0 ? important.at(-1) : declarations.at(-1))[1]
      .replace(/!important\s*$/i, '').trim().toLowerCase()
    return DIRECTIONS.has(value) ? value : null
  }
  const value = String(align ?? '').trim().toLowerCase()
  return DIRECTIONS.has(value) ? value : null
}

function htmlCellAlignments(html, tag) {
  const matches = [...String(html).matchAll(new RegExp(`<${tag}\\b([^>]*)>`, 'gi'))]
  return matches.map((match) => {
    const attrs = {}
    const pattern = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
    let attr
    while ((attr = pattern.exec(match[1]))) {
      attrs[attr[1].toLowerCase()] = attr[2] ?? attr[3] ?? attr[4] ?? ''
    }
    return effectiveAlignment(attrs.align, attrs.style)
  })
}

function matchesTableAlignment(html, expected) {
  const target = JSON.stringify(expected)
  return JSON.stringify(htmlCellAlignments(html, 'th')) === target &&
    JSON.stringify(htmlCellAlignments(html, 'td')) === target
}

module.exports = { effectiveAlignment, htmlCellAlignments, matchesTableAlignment }
