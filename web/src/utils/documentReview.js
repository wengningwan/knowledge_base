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
    const rank = (record) => record.protocol_errors?.length ? 7 : record.remaining_conflicts?.length
      ? Math.max(5, statusRank[record.status] || 5) : statusRank[record.status] || 5
    const primary = group.records.reduce((best, record) => rank(record) > rank(best) ? record : best)
    const content = group.records.filter((record) => record.classification !== 'excluded')
    return {
      ...group, primary,
      attention: content.some((record) => record.remaining_conflicts?.length ||
        !['machine_corrected', 'checked', 'proven_equivalent', 'technical_failed', 'pending', 'running'].includes(record.status)),
      incomplete: group.records.some((record) =>
        ['technical_failed', 'pending', 'running'].includes(record.status) || record.protocol_errors?.length),
      modified: content.some((record) => record.status === 'machine_corrected'),
      critical: group.records.some((record) => record.critical)
    }
  })
}

/** 明确核对来源，系统回执不能表达为人工确认。 */
export function reviewStatusLabel(record) {
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

/** 按 Unicode 字符对照差异；大范围变化整体突出以限制比较开销。 */
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
  const coarse = (a.length + 1) * (b.length + 1) > 250000
  if (coarse) {
    append('delete', a.join(''))
    append('insert', b.join(''))
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
  return { parts, coarse }
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
