import katex from 'katex'
// \ce 化学式渲染依赖 mhchem；本模块渲染摘要公式，必须显式声明而不是借 MarkdownPreview 的副作用导入
import 'katex/contrib/mhchem'

import { escapeHtml } from './html.js'

const statusRank = {
  technical_failed: 7, pending: 6, running: 6, unresolved: 5,
  not_checked: 4, machine_corrected: 3, checked: 2, proven_equivalent: 1
}

/** 使用后端原问题身份归并分片，保留全部记录与未完成信号。 */
export function groupDocumentReviews(reviews) {
  const groups = new Map()
  reviews.forEach((record, index) => {
    const key = record.parent_issue_id || record.id || `record-${index}`
    if (!groups.has(key)) groups.set(key, { key, records: [] })
    groups.get(key).records.push(record)
  })
  return [...groups.values()].map((group) => {
    const rank = (record) => record.human_review ? 0 : record.protocol_errors?.length ? 7 : record.remaining_conflicts?.length
      ? Math.max(5, statusRank[record.status] || 5) : statusRank[record.status] || 5
    const primary = group.records.reduce((best, record) => rank(record) > rank(best) ? record : best)
    const content = group.records.filter((record) => record.classification !== 'excluded' && !record.human_review)
    return {
      ...group, primary,
      attention: content.some((record) => record.remaining_conflicts?.length ||
        !['machine_corrected', 'checked', 'proven_equivalent', 'technical_failed', 'pending', 'running'].includes(record.status)),
      incomplete: group.records.some((record) => !record.human_review &&
        (['technical_failed', 'pending', 'running'].includes(record.status) || record.protocol_errors?.length)),
      modified: content.some((record) => record.status === 'machine_corrected'),
      confirmed: group.records.every((record) => record.human_review),
      critical: group.records.some((record) => record.critical)
    }
  })
}

/** 问题对比指纹：与详情页对比视图同源选取，屏幕呈现一致才可能同簇。 */
function reviewIssueFingerprint(record) {
  const normalize = (text) => String(text ?? '').trim().replace(/\s+/g, ' ')
  const comparison = reviewComparison(record ?? {})
  return { before: normalize(comparison.before), after: normalize(comparison.after) }
}

/** 把类型、说明与对比内容全部一致的待确认组聚为一簇；具体内容不同即不同问题，禁止按模板文案冒认合并。 */
export function clusterDocumentReviewGroups(groups) {
  const buckets = new Map()
  for (const group of groups) {
    if (!(group.attention || group.incomplete)) continue
    // 簇键对齐详情页可见呈现：reason 是模板文案不能单独定性，缺失/差异文字不同必须拆分；
    // reason 漂移后同一原问题的分片可能分属不同簇，属预期行为。
    const primary = group.primary
    const kind = primary?.kind || ''
    const reason = String(primary?.reason ?? '').trim().replace(/\s+/g, ' ')
    const fingerprint = reviewIssueFingerprint(primary)
    const key = (reason || fingerprint.before || fingerprint.after)
      ? `${kind}::${reason}::${fingerprint.before}::${fingerprint.after}`
      : `solo:${group.key}`
    if (!buckets.has(key)) {
      buckets.set(key, {
        key, kind,
        reason: reason || primary?.reason || '',
        excerpt: fingerprint.before || fingerprint.after || reason || '',
        groups: [], records: []
      })
    }
    const bucket = buckets.get(key)
    bucket.groups.push(group)
    bucket.records.push(...group.records)
  }
  return [...buckets.values()].map((bucket) => {
    const confirmedCount = bucket.records.filter((record) => record.human_review).length
    const pageDistribution = {}
    for (const record of bucket.records) {
      const page = Number.isInteger(record.page) && record.page > 0 ? record.page : '页码未知'
      pageDistribution[page] = (pageDistribution[page] || 0) + 1
    }
    return {
      ...bucket,
      count: bucket.records.length - confirmedCount,
      confirmedCount,
      critical: bucket.records.some((record) => record.critical),
      pageDistribution
    }
  }).sort((left, right) => right.count - left.count || (left.key < right.key ? -1 : left.key > right.key ? 1 : 0))
}

/** 生成簇的页码分布摘要，超过三段截断为等 N 页。 */
export function clusterPageSummary(distribution) {
  const entries = Object.entries(distribution)
  const summary = entries.slice(0, 3).map(([page, total]) => `${page === '页码未知' ? '页码未知' : `第${page}页`}×${total}`).join(' · ')
  return entries.length > 3 ? `${summary} 等${entries.length}页` : summary
}

/** 明确核对来源，系统回执不能表达为人工确认。 */
export function reviewStatusLabel(record) {
  if (record.human_review) return '已确认'
  if (record.protocol_errors?.length) return '复核未完成'
  if (record.remaining_conflicts?.length) return '仍有差异待核对'
  return {
    machine_corrected: '系统已修改', checked: record.source === 'model' ? '模型已核对' : '系统已核对',
    unresolved: '待人工核对', not_checked: '尚未核对', technical_failed: '复核未完成',
    proven_equivalent: '排印表示等价', pending: '等待复核', running: '正在复核'
  }[record.status] || '状态待核实'
}

/** 将问题类型转换为用户可以识别的用途。 */
export function reviewKindLabel(kind) {
  return {
    publication_element: '出版元素', table_math: '表格与公式', prose_whitespace: '正文空白',
    math_delimiter: '公式标记', damaged_glyph: '异常字形', native_region_unavailable: '缺少对照文字',
    ocr_duplicate: '重复识别', local_word_repair: '词语识别', representation_equivalent: '排印表示',
    critical_mismatch: '关键内容差异', text_mismatch: '文字差异', math_syntax: '公式格式',
    title_hierarchy: '标题层级', missing_region: '疑似正文遗漏', reading_flow: '阅读顺序',
    native_evidence_unavailable: '原文证据缺失', missing_page: '疑似缺页',
    formula_fragment_cluster: '疑似漏识别公式区域',
    scientific_notation: '科学符号核对',
    date_body_split: '日期与正文', same_line_join: '同行文字', high_risk: '图表与公式',
    weak_reference: '缺少可靠对照'
  }[kind] || '内容核对'
}

/** 整块快照不代表本条应用事实；未修改项优先对照未应用候选。 */
export function reviewComparison(record) {
  const before = record.before ?? record.candidates?.ocr ?? ''
  const corrected = record.status === 'machine_corrected' && record.applied !== false
  if (!corrected && record.candidates?.native && record.candidates.native !== before) {
    return { before, after: record.candidates.native, label: 'PDF提取文字', kind: 'candidate' }
  }
  if (typeof record.after === 'string') return { before, after: record.after, label: '处理后文字', kind: 'snapshot' }
  return { before, after: null, label: '', kind: 'none' }
}

/** 双向搜索最短编辑路径的交点，长文本只保留线性大小的搜索前沿。 */
function splitReviewText(a, b) {
  const characters = new Set(a)
  if (!b.some((char) => characters.has(char))) return null

  const depth = Math.ceil((a.length + b.length) / 2)
  const offset = depth + 1
  const forward = new Int32Array(2 * depth + 3).fill(-1)
  const reverse = new Int32Array(2 * depth + 3).fill(-1)
  forward[offset + 1] = reverse[offset + 1] = 0
  const delta = a.length - b.length
  const odd = delta % 2 !== 0
  let forwardStart = 0
  let forwardEnd = 0
  let reverseStart = 0
  let reverseEnd = 0
  for (let d = 0; d < depth; d++) {
    for (let k = -d + forwardStart; k <= d - forwardEnd; k += 2) {
      const index = offset + k
      let x = k === -d || (k !== d && forward[index - 1] < forward[index + 1])
        ? forward[index + 1] : forward[index - 1] + 1
      let y = x - k
      while (x < a.length && y < b.length && a[x] === b[y]) { x++; y++ }
      forward[index] = x
      if (x > a.length) forwardEnd += 2
      else if (y > b.length) forwardStart += 2
      else if (odd) {
        const opposite = offset + delta - k
        if (opposite >= 0 && opposite < reverse.length && reverse[opposite] !== -1 &&
          x >= a.length - reverse[opposite]) return [x, y]
      }
    }
    for (let k = -d + reverseStart; k <= d - reverseEnd; k += 2) {
      const index = offset + k
      let x = k === -d || (k !== d && reverse[index - 1] < reverse[index + 1])
        ? reverse[index + 1] : reverse[index - 1] + 1
      let y = x - k
      while (x < a.length && y < b.length && a[a.length - x - 1] === b[b.length - y - 1]) { x++; y++ }
      reverse[index] = x
      if (x > a.length) reverseEnd += 2
      else if (y > b.length) reverseStart += 2
      else if (!odd) {
        const opposite = offset + delta - k
        if (opposite >= 0 && opposite < forward.length && forward[opposite] !== -1 &&
          forward[opposite] >= a.length - x) {
          const split = forward[opposite]
          return [split, split - (delta - k)]
        }
      }
    }
  }
  return null
}

/** 按 Unicode 字符精确对照差异，长文本拆分后继续保留内部共同文字。 */
export function diffReviewText(before, after) {
  const left = Array.from(before)
  const right = Array.from(after)
  let start = 0
  let end = 0
  while (start < left.length && start < right.length && left[start] === right[start]) start++
  while (end < left.length - start && end < right.length - start &&
    left[left.length - end - 1] === right[right.length - end - 1]) end++
  const a = left.slice(start, left.length - end)
  const b = right.slice(start, right.length - end)
  const parts = []
  const append = (type, text) => {
    if (!text) return
    const previous = parts[parts.length - 1]
    if (previous?.type === type) previous.text += text
    else parts.push({ type, text })
  }
  append('equal', left.slice(0, start).join(''))
  if (!a.length || !b.length) {
    append('delete', a.join(''))
    append('insert', b.join(''))
  } else if ((a.length + 1) * (b.length + 1) > 250000) {
    const split = splitReviewText(a, b)
    if (split) {
      const [x, y] = split
      for (const part of diffReviewText(a.slice(0, x).join(''), b.slice(0, y).join('')).parts) append(part.type, part.text)
      for (const part of diffReviewText(a.slice(x).join(''), b.slice(y).join('')).parts) append(part.type, part.text)
    } else {
      append('delete', a.join(''))
      append('insert', b.join(''))
    }
  } else {
    const width = b.length + 1
    const lengths = new Uint32Array((a.length + 1) * width)
    for (let i = a.length - 1; i >= 0; i--) {
      for (let j = b.length - 1; j >= 0; j--) {
        lengths[i * width + j] = a[i] === b[j]
          ? 1 + lengths[(i + 1) * width + j + 1]
          : Math.max(lengths[(i + 1) * width + j], lengths[i * width + j + 1])
      }
    }
    let i = 0
    let j = 0
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) {
        append('equal', a[i++])
        j++
      } else if (i < a.length && (j === b.length || lengths[(i + 1) * width + j] >= lengths[i * width + j + 1])) append('delete', a[i++])
      else append('insert', b[j++])
    }
  }
  append('equal', end ? left.slice(left.length - end).join('') : '')
  return { parts }
}

/** 缩略共同上下文，差异字符完整保留，全文由调用者按需展开。 */
export function compactReviewSegments(parts) {
  return parts.map((part, index) => {
    const chars = Array.from(part.text)
    if (part.type !== 'equal' || chars.length <= 120) return part
    if (parts.length === 1) return { ...part, text: `${chars.slice(0, 120).join('')}…` }
    const head = index > 0 ? chars.slice(0, 48).join('') : ''
    const tail = index < parts.length - 1 ? chars.slice(-48).join('') : ''
    return { ...part, text: `${head} … ${tail}` }
  })
}

const MATH_DELIMITERS = [
  { opener: '$$', closer: '$$', displayMode: true },
  { opener: '\\(', closer: '\\)', displayMode: false },
  { opener: '\\[', closer: '\\]', displayMode: true },
  { opener: '$', closer: '$', displayMode: false }
]

const matchesAt = (chars, index, token) =>
  token.split('').every((char, offset) => chars[index + offset] === char)

const findMathCloser = (chars, from, closer) => {
  for (let index = from; index < chars.length; index += 1) {
    if (matchesAt(chars, index, closer)) return index
    // 反斜杠与后一字符构成转义或 LaTeX 命令，不能参与定界匹配。
    if (chars[index] === '\\') index += 1
  }
  return -1
}

/** 按码点扫描完整数学定界区间；转义、未闭合与空内容不当公式。 */
export function mathSpans(text) {
  const chars = Array.from(String(text ?? ''))
  const spans = []
  let index = 0
  while (index < chars.length) {
    if (chars[index] === '\\' && chars[index + 1] === '$') { index += 2; continue }
    const delimiter = MATH_DELIMITERS.find((item) => matchesAt(chars, index, item.opener))
    if (!delimiter) { index += 1; continue }
    const closer = findMathCloser(chars, index + delimiter.opener.length, delimiter.closer)
    if (closer === -1 || closer === index + delimiter.opener.length) {
      index += delimiter.opener.length
      continue
    }
    const end = closer + delimiter.closer.length
    spans.push({ start: index, end, displayMode: delimiter.displayMode, opener: delimiter.opener, closer: delimiter.closer })
    index = end
  }
  return spans
}

const appendSegment = (segments, segment) => {
  if (!segment.text) return
  const previous = segments[segments.length - 1]
  if (previous?.type === segment.type) previous.text += segment.text
  else segments.push(segment)
}

/** 将 diff 片段按公式区间对齐：跨定界的片段归并为完整公式段，区间外保持原样。 */
export function alignReviewMathParts(parts, source) {
  const sourceText = String(source ?? '')
  const spans = mathSpans(sourceText)
  if (!spans.length) return parts.map((part) => ({ ...part }))
  const chars = Array.from(sourceText)
  const boundaries = []
  let total = 0
  for (const part of parts) {
    const length = Array.from(part.text).length
    boundaries.push({ start: total, end: total + length, part })
    total += length
  }
  const segmentsInRange = (from, to) => {
    const segments = []
    for (const boundary of boundaries) {
      const start = Math.max(from, boundary.start)
      const end = Math.min(to, boundary.end)
      if (start >= end) continue
      appendSegment(segments, { type: boundary.part.type, text: chars.slice(start, end).join('') })
    }
    return segments
  }
  const diffInRange = (from, to) => {
    let result = 'equal'
    for (const boundary of boundaries) {
      if (boundary.start >= to) break
      if (boundary.end <= from) continue
      if (boundary.part.type === 'insert') result = 'insert'
      else if (boundary.part.type === 'delete' && result !== 'insert') result = 'delete'
    }
    return result
  }
  const output = []
  let cursor = 0
  for (const span of spans) {
    segmentsInRange(cursor, span.start).forEach((segment) => appendSegment(output, segment))
    output.push({
      type: 'math',
      text: chars.slice(span.start, span.end).join(''),
      latex: chars.slice(span.start + span.opener.length, span.end - span.closer.length).join(''),
      diff: diffInRange(span.start, span.end),
      displayMode: span.displayMode
    })
    cursor = span.end
  }
  segmentsInRange(cursor, total).forEach((segment) => appendSegment(output, segment))
  return output
}

/** 将整段文本切分为文字与公式段，供无 diff 的全文展示使用。 */
export function splitMathSegments(text) {
  const sourceText = String(text ?? '')
  if (!sourceText) return []
  return alignReviewMathParts([{ type: 'equal', text: sourceText }], sourceText)
}

/** 渲染公式段为 KaTeX HTML。ParseError 由 throwOnError:false 回退红色原文；其余异常
 * （如深嵌套导致的栈溢出）以转义原文兜底——校对文本是不可信解析产物，单条畸形公式
 * 只能单段降级，不允许中断整个面板渲染。 */
export function renderMathHtml(part) {
  const latex = String(part.latex ?? '')
  try {
    return katex.renderToString(latex, {
      throwOnError: false,
      trust: false,
      errorColor: '#cc0000',
      displayMode: part.displayMode === true
    })
  } catch (error) {
    return `<span class="katex-render-fallback" title="${escapeHtml(String(error?.message ?? ''))}">${escapeHtml(latex)}</span>`
  }
}

/** 列表摘要 HTML：文字段折叠空白并转义，公式段整段行内渲染不撕裂；超出预算截断加省略号。
 * 供问题列表与锚点卡片等小卡片使用；正文区仍走 splitMathSegments + renderMathHtml 管线。 */
export function renderExcerptHtml(content, maxLength = 120) {
  const segments = splitMathSegments(String(content ?? '').trim())
  let used = 0
  let truncated = false
  const html = []
  for (const part of segments) {
    if (truncated) break
    if (part.type === 'math') {
      // 公式整段渲染，源码长度计入预算：截半公式比舍弃更不可读
      html.push(renderMathHtml({ ...part, displayMode: false }))
      used += Array.from(part.text).length
      continue
    }
    const chars = Array.from(part.text.replace(/\s+/g, ' '))
    const remaining = maxLength - used
    if (chars.length > remaining) {
      html.push(escapeHtml(chars.slice(0, Math.max(0, remaining)).join('')))
      truncated = true
    } else {
      used += chars.length
      html.push(escapeHtml(chars.join('')))
    }
  }
  return `${html.join('')}${truncated ? '…' : ''}`
}

/** 非正文类型锚点只作兜底：公式/图表块内容与补入文字不同构。 */
const ANCHOR_NON_PROSE_TYPES = new Set(['picture', 'table', 'formula', 'chemical_structure', 'chemical_equation', 'code_block'])
const ANCHOR_TYPE_LABELS = {
  text: '正文段落', title: '标题', caption: '图表标题', footnote: '脚注',
  formula: '公式', picture: '插图', table: '表格', code_block: '代码块'
}

/** 本页正文上下文块：按页过滤（排除页眉页脚）并按阅读顺序排序。 */
export function pageContextBlocks(blocks, page) {
  return (blocks || [])
    .filter((block) => block.page === page && !['page_header', 'page_footer'].includes(block.type))
    .sort((first, second) => (first.order ?? 0) - (second.order ?? 0))
}

/** 推荐补入锚点：与缺失区域版面距离最小的正文块，平局取阅读顺序靠前；
 * 同页只剩图/表块时兜底推荐，缺 bbox 或无带坐标候选时不推荐。 */
export function recommendAnchorBlock(blocks, page, bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return null
  const candidates = pageContextBlocks(blocks, page)
    .filter((block) => Array.isArray(block.bbox) && block.bbox.length === 4)
  if (!candidates.length) return null
  const distance = (block) => Math.max(block.bbox[1] - bbox[3], bbox[1] - block.bbox[3], 0)
    + 0.5 * Math.max(block.bbox[0] - bbox[2], bbox[0] - block.bbox[2], 0)
  const prose = candidates.filter((block) => !ANCHOR_NON_PROSE_TYPES.has(block.type))
  const pool = prose.length ? prose : candidates
  return [...pool].sort((first, second) => distance(first) - distance(second) || (first.order ?? 0) - (second.order ?? 0))[0].id
}

/** 锚点卡片块类型标签，未知类型回退原值。 */
export function reviewBlockTypeLabel(type) {
  return ANCHOR_TYPE_LABELS[type] || String(type || '未知类型')
}
