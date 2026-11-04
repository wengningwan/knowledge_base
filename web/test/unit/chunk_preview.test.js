import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createChunkSourceDocument, describeChunkSource, chunkSourceWindows, sortedPreviewChunks
} from '../../src/utils/chunkPreview.js'

const text = '# 论文\n\n😀定义\n$$\nx=1\n$$\n\n条件 A\n结果 A'
const source = createChunkSourceDocument(text, 'current')
const span = (start, end, lineStart, lineEnd, extra = {}) => ({
  start_char: start, end_char: end, line_start: lineStart, line_end: lineEnd, ...extra
})
const first = span(6, 10, 3, 3)
const last = span(21, 25, 8, 8, { page: 4 })
const chunk = (spans = [first, last], metadata = {}) => ({
  content: '定义和条件的组合正文',
  source_metadata: { source_sha256: 'current', source_spans: spans, ...metadata }
})

test('预览按后端顺序排列，完全相同正文的不同块仍保留', () => {
  const chunks = [{ id: 'b', chunk_order_index: 2, content: '相同正文' },
    { id: 'a', chunk_order_index: 1, content: '相同正文' }]
  assert.deepEqual(sortedPreviewChunks(chunks).map((item) => item.id), ['a', 'b'])
  assert.equal(chunks[0].id, 'b')
})

test('已核对指纹的来源分别显示非连续范围与真实页码', () => {
  const result = describeChunkSource(chunk(), source)
  assert.equal(result.status, 'located')
  assert.equal(result.label, '原文第 3、8 行 · PDF 第 4 页')
  assert.deepEqual(result.spans, [first, last])
  const windows = chunkSourceWindows(result.spans, source, 1)
  assert.equal(windows.length, 2)
  assert.deepEqual(windows[0].lines.filter((line) => line.highlighted).map((line) => line.number), [3])
  assert.deepEqual(windows[1].lines.filter((line) => line.highlighted).map((line) => line.number), [8])
  assert.equal(windows[1].lines[0].number, 7)
})

test('指纹过期或当前原文不可用时不能打开来源，也不能展示页码', () => {
  const outdated = describeChunkSource(chunk(undefined, { source_sha256: 'old' }), source)
  assert.equal(outdated.status, 'outdated')
  assert.deepEqual(outdated.spans, [])
  assert.doesNotMatch(outdated.label, /PDF/)
  assert.equal(describeChunkSource(chunk(), createChunkSourceDocument(text, '')).status, 'unverified')
  assert.equal(describeChunkSource(chunk(), createChunkSourceDocument('', 'current', '读取失败')).status, 'unavailable')
})

test('来源坐标越界、行号矛盾或缺失时明确不可定位，不回退猜测', () => {
  for (const spans of [[{ ...first, line_start: 2 }], [{ ...first, end_char: 999 }],
    [{ ...first, start_char: -1 }], [], [{ ...first, start_char: null }]]) {
    const result = describeChunkSource({ ...chunk(spans), start_char_pos: 0, end_char_pos: 2 }, source)
    assert.equal(result.status, 'unknown')
    assert.deepEqual(result.spans, [])
  }
})

test('非法页码不影响已核对的原文行，但不显示为 PDF 位置', () => {
  for (const page of [0, -1, 1.5, '4']) {
    const result = describeChunkSource(chunk([{ ...first, page }]), source)
    assert.equal(result.status, 'located')
    assert.equal(result.label, '原文第 3 行')
    assert.equal(result.spans[0].page, undefined)
  }
})

test('旧块字符位置按Unicode码点逐字核对后才可定位', () => {
  const old = { content: '😀定义', start_char_pos: 6, end_char_pos: 9 }
  const located = describeChunkSource(old, source)
  assert.equal(located.status, 'located')
  assert.equal(located.label, '原文第 3 行')
  assert.equal(describeChunkSource({ ...old, content: '不同正文' }, source).status, 'unknown')
  assert.equal(describeChunkSource({ ...old, start_char_pos: 7, end_char_pos: 10 }, source).status, 'unknown')
  assert.equal(describeChunkSource({ content: '😀定义' }, source).status, 'unknown')
})

test('章节和需要人工检查的超长、歧义关联提示使用人类术语', () => {
  const result = describeChunkSource(chunk(undefined, {
    section_path: ['论文', '实验结果'], warnings: ['over_target_tokens', 'ambiguous_caption', 'unresolved_formula_reference'],
    associations: [{ identifier: 'A', status: 'ambiguous' }, { identifier: 'B', status: 'unresolved' }]
  }), source)
  assert.equal(result.section, '论文 / 实验结果')
  assert.deepEqual(result.notices, ['完整证据超过目标长度，已保留全文', '图表说明归属需核对', '公式说明关联需核对',
    '实验条件存在多个候选，未自动关联', '部分实验条件未找到明确来源'])
})

test('v3 正文来源只读取 source_spans，标题上下文不进入标签或高亮', () => {
  const result = describeChunkSource(chunk([last], {
    chunker_version: 'paper_rules_v3', context_source_spans: [span(0, 4, 1, 1), first]
  }), source)
  assert.equal(result.label, '原文第 8 行 · PDF 第 4 页')
  assert.deepEqual(result.spans, [last])
})

test('旧 Paper 只过滤经过前缀和原文核对的前置上下文，保留正文重复标题', () => {
  const original = '\n\n# 合成论文\n\n## 实验\n\n合成论文\n\n实验\n\n实际结果'
  const document = createChunkSourceDocument(original, 'current')
  const range = (needle) => {
    const start = original.indexOf(needle)
    return { start_char: start, end_char: start + needle.length,
      line_start: original.slice(0, start).split('\n').length,
      line_end: original.slice(0, start + needle.length - 1).split('\n').length }
  }
  const title = range('# 合成论文')
  const heading = range('## 实验')
  const body = range('合成论文\n\n实验\n\n实际结果')
  for (const version of ['paper_rules_v1', 'paper_rules_v2']) {
    const record = { content: '论文：合成论文\n章节：实验\n\n合成论文\n\n实验\n\n实际结果',
      source_metadata: { source_sha256: 'current', chunker_version: version,
        section_path: ['实验'], source_spans: [title, heading, body] } }
    const result = describeChunkSource(record, document)
    assert.equal(result.label, '原文第 7–11 行')
    assert.deepEqual(result.spans, [body])
    assert.deepEqual(chunkSourceWindows(result.spans, document, 0)[0].lines.map((line) => line.text),
      ['合成论文', '', '实验', '', '实际结果'])
    const uncertain = describeChunkSource({ ...record, source_metadata: {
      ...record.source_metadata, section_path: ['其他章节']
    } }, document)
    assert.equal(uncertain.status, 'unknown')
    assert.match(uncertain.label, /重新入库/)
    assert.deepEqual(uncertain.spans, [])
  }
})
