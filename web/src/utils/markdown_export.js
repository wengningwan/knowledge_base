import MarkdownIt from 'markdown-it'

/** 将生成内容中的知识库图片内嵌到 Markdown，图片读取失败时终止导出。 */
export async function inlineKnowledgeImages(markdown, loadImage) {
  const lines = markdown.split('\n')
  const offsets = [0]
  for (const line of lines) offsets.push(offsets.at(-1) + line.length + 1)
  const excluded = new MarkdownIt()
    .parse(markdown, {})
    .filter((token) => ['fence', 'code_block', 'html_block'].includes(token.type) && token.map)
    .map((token) => [offsets[token.map[0]], offsets[token.map[1]]])
  for (const match of markdown.matchAll(/(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)/g)) {
    excluded.push([match.index, match.index + match[0].length])
  }
  const pattern = /!\[[^\]\n]*\]\(\s*(\/api\/knowledge\/databases\/[^\s)]+\/images\/[^\s)]+)\s*\)/g
  const matches = [...markdown.matchAll(pattern)].filter(
    (match) => !excluded.some(([start, end]) => match.index >= start && match.index < end)
  )
  const images = new Map()
  for (const match of matches) {
    const source = match[1]
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
    result += match[0].replace(match[1], await images.get(match[1]))
    position = match.index + match[0].length
  }
  return result + markdown.slice(position)
}
