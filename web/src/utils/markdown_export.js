import MarkdownIt from 'markdown-it'
import { isKnowledgeImageProxyPath } from './knowledge_images.js'

/** 将生成内容中的知识库图片内嵌到 Markdown，图片读取失败时终止导出。 */
export async function inlineKnowledgeImages(markdown, loadImage, kbId = null) {
  const lines = markdown.split('\n')
  const offsets = [0]
  for (const line of lines) offsets.push(offsets.at(-1) + line.length + 1)
  const tokens = new MarkdownIt({ html: true }).parse(markdown, {})
  const excluded = tokens
    .filter((token) => ['fence', 'code_block'].includes(token.type) && token.map)
    .map((token) => [offsets[token.map[0]], offsets[token.map[1]]])
  for (const match of markdown.matchAll(/<(pre|code)\b[^>]*>[\s\S]*?<\/\1\s*>/gi)) {
    excluded.push([match.index, match.index + match[0].length])
  }
  const htmlBlocks = tokens.filter((token) => token.type === 'html_block' && token.map)
    .map((token) => [offsets[token.map[0]], offsets[token.map[1]]])
  const isExcluded = (index, ranges = excluded) =>
    ranges.some(([start, end]) => index >= start && index < end)
  // 行内代码只能在正文范围内配对，围栏内部的反引号不能吞掉后续真图。
  const nonInlineRanges = [...excluded, ...htmlBlocks].sort((left, right) => left[0] - right[0])
  let cursor = 0
  for (const [start, end] of [...nonInlineRanges, [markdown.length, markdown.length]]) {
    if (start > cursor) {
      const paragraph = markdown.slice(cursor, start)
      for (const match of paragraph.matchAll(/(?<![`\\])(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)/g)) {
        excluded.push([cursor + match.index, cursor + match.index + match[0].length])
      }
    }
    cursor = Math.max(cursor, end)
  }

  const pattern = /!\[[^\]\n]*\]\(\s*(\/api\/knowledge\/databases\/[^\s)]+\/images\/[^\s)]+)\s*\)/g
  const matches = [...markdown.matchAll(pattern)]
    .filter((match) => !isExcluded(match.index, [...excluded, ...htmlBlocks]) && isKnowledgeImageProxyPath(match[1], kbId))
    .map((match) => ({ index: match.index + match[0].indexOf(match[1]), text: match[1], source: match[1] }))
  // HTML 表格可以包含 img，但代码示例保持原样。
  for (const tag of markdown.matchAll(/<img\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi)) {
    if (isExcluded(tag.index)) continue
    const src = tag[0].match(/(?:^|\s)src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/i)
    const source = src?.[1] ?? src?.[2] ?? src?.[3]
    if (source && isKnowledgeImageProxyPath(source, kbId)) {
      matches.push({ index: tag.index + src.index + src[0].indexOf(source), text: source, source })
    }
  }
  matches.sort((left, right) => left.index - right.index)
  const images = new Map()
  for (const match of matches) {
    const source = match.source
    if (!images.has(source)) {
      images.set(
        source,
        (async () => {
          const blob = await loadImage(source)
          if (!/^image\/(png|jpeg|webp|gif|bmp)$/i.test(blob.type)) {
            throw new Error('知识库图片响应格式无效，已停止下载')
          }
          const bytes = new Uint8Array(await blob.arrayBuffer())
          if (!bytes.length) throw new Error('知识库图片为空，已停止下载')
          const chunks = []
          for (let index = 0; index < bytes.length; index += 32768) {
            chunks.push(String.fromCharCode(...bytes.subarray(index, index + 32768)))
          }
          return `data:${blob.type};base64,${btoa(chunks.join(''))}`
        })()
      )
    }
  }
  await Promise.all(images.values())

  let result = ''
  let position = 0
  for (const match of matches) {
    result += markdown.slice(position, match.index)
    result += match.text.replace(match.source, await images.get(match.source))
    position = match.index + match.text.length
  }
  return result + markdown.slice(position)
}
