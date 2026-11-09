const BLOCK_TAGS = 'p, div, li, td, th, tr, section, blockquote, pre, h1, h2, h3, h4, h5, h6, hr'
const SKIP_TAGS = 'script, style, iframe, svg, .html-preview-render, [aria-hidden="true"]'

/** 在已清洗的 Markdown 节点中标记逐字取值，保留原有行内格式和公式结构。 */
export function highlightMarkdownText(root, rawText) {
  const keyword = rawText.trim().replace(
    /\$\$([^$]+)\$\$|\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]/g,
    (_, display, inline, bracket) => '$' + (display ?? inline ?? bracket) + '$'
  )
  if (!keyword) return

  const textPattern = keyword.split(/\s+/).map((part) => part.replace(/[.*+?^$(){}|[\]\\]/g, '\\$&')).join('\\s+')
  const numeric = /^[+−-]?(?:\d|\.\d)/.test(keyword)
  const startBoundary = numeric ? '(?<![\\w+−-])(?<!\\d[.,])' : /^\w/.test(keyword) ? '(?<!\\w)' : ''
  const endBoundary = numeric && /\d$/.test(keyword) ? '(?!\\w|[.,]\\d|%)' : /\w$/.test(keyword) ? '(?!\\w)' : ''
  const pattern = new RegExp(startBoundary + textPattern + endBoundary, 'g')

  let text = ''
  const parts = []
  const addPart = (node, value, formula = false) => {
    const start = text.length
    text += value
    parts.push({ node, value, start, end: text.length, formula })
  }
  const visit = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.data) addPart(node, node.data)
      return
    }
    if (node.nodeType !== Node.ELEMENT_NODE || node.matches(SKIP_TAGS)) return
    if (node.matches('.katex')) {
      const latex = node.querySelector('annotation[encoding="application/x-tex"]')?.textContent
      if (latex) addPart(node, '$' + latex + '$', true)
      else text += '\0'
      return
    }
    if (node.tagName === 'BR') {
      text += ' '
      return
    }
    const block = node.matches(BLOCK_TAGS)
    if (block) text += '\0'
    for (const child of node.childNodes) visit(child)
    if (block) text += '\0'
  }
  for (const child of root.childNodes) visit(child)

  const formulas = parts.filter((part) => part.formula)
  const matches = Array.from(text.matchAll(pattern), (match) => ({
    start: match.index,
    end: match.index + match[0].length
  })).filter(({ start, end }) => formulas.every((part) =>
    !(part.start < start && start < part.end) && !(part.start < end && end < part.end)
  ))

  let matchIndex = 0
  for (const part of parts) {
    while (matches[matchIndex]?.end <= part.start) matchIndex++
    if (matches[matchIndex] === undefined) break
    if (matches[matchIndex].start >= part.end) continue
    markPart(part, matches, matchIndex)
  }
}

/** 只包裹命中的文本范围，公式始终作为整体标记。 */
function markPart(part, matches, matchIndex) {
  const fragment = document.createDocumentFragment()
  let offset = 0
  for (let index = matchIndex; index < matches.length && matches[index].start < part.end; index++) {
    const mark = document.createElement('mark')
    mark.className = 'markdown-text-highlight'
    if (part.formula) {
      part.node.before(mark)
      mark.append(part.node)
      return
    }
    const start = Math.max(0, matches[index].start - part.start)
    const end = Math.min(part.value.length, matches[index].end - part.start)
    mark.textContent = part.value.slice(start, end)
    fragment.append(part.value.slice(offset, start), mark)
    offset = end
  }
  fragment.append(part.value.slice(offset))
  part.node.replaceWith(fragment)
}
