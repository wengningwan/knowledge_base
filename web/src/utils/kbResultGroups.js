/** 按知识库文件身份聚合检索片段。 */
export function groupKnowledgeChunks(chunks) {
  const groups = new Map()

  // 兜底名指 source 缺失、未知或直接用了 file_id 的条目；真实文件名优先作为组显示名
  const isFallbackName = (name, fileId) => !name || name === '未知来源' || name === fileId

  for (const item of chunks) {
    const source = item?.metadata?.source || '未知来源'
    const kbId = item?.kb_id || ''
    const fileId = item?.file_id || ''
    // 文件身份只由 kb_id + file_id 决定：同一文件的兜底名与真实文件名必须落在同一组；
    // 无 file_id 的条目无法确认身份，退化为按来源名分组避免互相吞并
    const key = fileId ? `${kbId}\u0000${fileId}` : `${kbId}\u0000name\u0000${source}`

    if (!groups.has(key)) {
      groups.set(key, {
        key,
        filename: source,
        kb_id: kbId,
        file_id: fileId,
        chunks: []
      })
    }
    const group = groups.get(key)
    if (isFallbackName(group.filename, fileId) && !isFallbackName(source, fileId)) {
      group.filename = source
    }
    group.chunks.push(item)
  }

  return Array.from(groups.values()).sort((a, b) => a.filename.localeCompare(b.filename))
}
