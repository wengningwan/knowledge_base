/** 建立当前原文的 Unicode 码点与行索引，供全部分块复用。 */
export function createChunkSourceDocument(content, sourceSha256 = '', error = '') {
  const text = typeof content === 'string' ? content : ''
  const characters = Array.from(text)
  const lineStarts = [0]
  characters.forEach((character, index) => {
    if (character === '\n') lineStarts.push(index + 1)
  })
  return { characters, lineStarts, lines: text.split('\n'), sourceSha256, available: Boolean(text) && !error }
}

/** 按后端块序排列；重复正文也属于独立事实。 */
export function sortedPreviewChunks(chunks = []) {
  return [...chunks].sort((left, right) => left.chunk_order_index - right.chunk_order_index)
}

/** 仅在预览中移除 Paper 生成的上下文前缀，保留证据正文和原始块。 */
export function chunkPreviewContent(chunk) {
  const content = chunk.content ?? ''
  const metadata = chunk.source_metadata
  if (!/^paper_rules_v[1-9]\d*$/.test(metadata?.chunker_version || '') || !Array.isArray(metadata.section_path)) return content
  const prefix = /^论文：[^\n]*\n章节：([^\n]*)\n\n/.exec(content)
  const section = metadata.section_path.join(' > ') || '正文'
  return prefix?.[1] === section ? content.slice(prefix[0].length) : content
}

/** 查找字符所属的一基行号。 */
function lineAtOffset(source, offset) {
  let left = 0
  let right = source.lineStarts.length
  while (left < right) {
    const middle = Math.floor((left + right) / 2)
    if (source.lineStarts[middle] <= offset) left = middle + 1
    else right = middle
  }
  return left
}

/** 从已验证的字符范围生成一基闭区间行号。 */
function characterSpan(source, start, end) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start ||
      end > source.characters.length) return null
  return { start_char: start, end_char: end,
    line_start: lineAtOffset(source, start), line_end: lineAtOffset(source, end - 1) }
}

/** 将分块提示转换为审查者可理解的状态。 */
function chunkNotices(metadata) {
  const warningLabels = {
    over_target_tokens: '完整证据超过目标长度，已保留全文',
    ambiguous_caption: '图表说明归属需核对',
    unresolved_caption: '图表说明尚未找到唯一对应图表，请核对原文',
    unresolved_formula_reference: '公式说明关联需核对',
    unclosed_formula: '公式标记未闭合，请检查原文',
    unclosed_table: '表格标记未闭合，请检查原文'
  }
  const notices = (metadata.warnings || []).map((warning) =>
    warningLabels[warning] || '分块包含需检查的提示')
  return [...new Set(notices)]
}

/** 核对来源指纹和坐标；旧块只有逐字匹配当前原文时才可定位。 */
export function describeChunkSource(chunk, source) {
  const metadata = chunk.source_metadata || {}
  const section = Array.isArray(metadata.section_path) ? metadata.section_path.join(' / ') : ''
  const result = { section, notices: chunkNotices(metadata), spans: [], pages: [] }
  const unavailable = (status, label) => ({ ...result, status, label })
  if (!source.available) return unavailable('unavailable', '原文不可用，暂不能核对来源')

  let spans
  if (chunk.source_metadata) {
    if (!metadata.source_sha256 || !source.sourceSha256) {
      return unavailable('unverified', '缺少来源指纹，暂不能核对位置')
    }
    if (metadata.source_sha256 !== source.sourceSha256) {
      return unavailable('outdated', '原文已变化，请重新入库后核对来源')
    }
    spans = metadata.source_spans
    if (!Array.isArray(spans) || !spans.length || spans.some((span) => {
      const actual = characterSpan(source, span?.start_char, span?.end_char)
      return !actual || actual.line_start !== span.line_start || actual.line_end !== span.line_end
    })) return unavailable('unknown', '来源位置未知或无效')
  } else {
    const span = characterSpan(source, chunk.start_char_pos, chunk.end_char_pos)
    if (!span || source.characters.slice(span.start_char, span.end_char).join('') !== chunk.content) {
      return unavailable('unknown', '来源位置未知，旧分块未能与当前原文核对')
    }
    spans = [span]
  }

  const previewContent = chunkPreviewContent(chunk)
  if (previewContent !== chunk.content) {
    // Paper 前缀的来源属于检索上下文，预览只定位实际展示的正文。
    spans = spans.filter((span) =>
      previewContent.includes(source.characters.slice(span.start_char, span.end_char).join('')))
    if (!spans.length) return unavailable('unknown', '来源位置未知或无效')
  }

  spans = spans.map(({ page, ...span }) => Number.isInteger(page) && page > 0 ? { ...span, page } : span)
  const ranges = [...new Set(spans.map((span) => span.line_start === span.line_end
    ? String(span.line_start) : `${span.line_start}–${span.line_end}`))]
  const pages = [...new Set(spans.map((span) => span.page).filter((page) => Number.isInteger(page) && page > 0))]
  const label = `原文第 ${ranges.join('、')} 行${pages.length ? ` · PDF 第 ${pages.join('、')} 页` : ''}`
  return { ...result, status: 'located', label, spans, pages }
}

/** 为每个独立来源保留单独行窗口，不把中间正文当成证据。 */
export function chunkSourceWindows(spans, source, contextLines = 2) {
  return spans.map((span) => {
    const start = Math.max(1, span.line_start - contextLines)
    const end = Math.min(source.lines.length, span.line_end + contextLines)
    return { span, lines: source.lines.slice(start - 1, end).map((text, index) => ({
      number: start + index, text,
      highlighted: start + index >= span.line_start && start + index <= span.line_end
    })) }
  })
}
