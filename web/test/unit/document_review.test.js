import assert from 'node:assert/strict'
import test from 'node:test'
import {
  groupDocumentReviews, reviewStatusLabel, reviewComparison, diffReviewText, compactReviewSegments
} from '../../src/utils/documentReview.js'

test('按原问题归并分片，系统修改后的残余冲突与失败回执均保留', () => {
  const reviews = [
    { id: 'part1', parent_issue_id: 'original', status: 'machine_corrected', remaining_conflicts: ['target'] },
    { id: 'part2', parent_issue_id: 'original', status: 'technical_failed' },
    { id: 'ok', status: 'checked', source: 'model' },
    { id: 'excluded', status: 'unresolved', classification: 'excluded' },
    { id: 'protocol', status: 'checked', protocol_errors: ['unknown_issue_id'] },
    { id: 'unknown', status: 'unexpected' }
  ]
  const groups = groupDocumentReviews(reviews)
  assert.equal(groups.length, 5)
  assert.equal(groups[0].records.length, 2)
  assert.equal(groups[0].primary.id, 'part2')
  assert.deepEqual(groups.filter((group) => group.attention).map((group) => group.key), ['original', 'unknown'])
  assert.deepEqual(groups.filter((group) => group.incomplete).map((group) => group.key), ['original', 'protocol'])
  assert.deepEqual(groups.filter((group) => group.modified).map((group) => group.key), ['original'])
  assert.equal(reviews[0].status, 'machine_corrected')
})

test('相同正文和缺失身份不能合并不同问题，未知状态不推断成功', () => {
  const groups = groupDocumentReviews([{ before: '相同' }, { before: '相同' }])
  assert.equal(groups.length, 2)
  assert.ok(groups.every((group) => group.attention))
  assert.equal(reviewStatusLabel({ status: 'checked', source: 'model' }), '模型已核对')
  assert.equal(reviewStatusLabel({ status: 'checked', source: 'rule' }), '系统已核对')
  assert.equal(reviewStatusLabel({ status: 'machine_corrected', remaining_conflicts: ['x'] }), '仍有差异待核对')
  assert.equal(reviewStatusLabel({ status: 'checked', protocol_errors: ['error'] }), '复核未完成')
})

test('缺失 after 不视为删除，未应用的文字候选和真正空正文分别表达', () => {
  assert.deepEqual(reviewComparison({ before: '原文' }), { before: '原文', after: null, label: '', kind: 'none' })
  const candidate = reviewComparison({ before: 'OCR', candidates: { native: '文字层' } })
  assert.equal(candidate.after, '文字层')
  assert.equal(candidate.kind, 'candidate')
  assert.equal(candidate.label, 'PDF提取文字')
  const removed = reviewComparison({ status: 'machine_corrected', before: '删除内容', after: '', candidates: { native: '不得回退' } })
  assert.equal(removed.after, '')
  assert.equal(removed.kind, 'snapshot')
  assert.equal(removed.label, '处理后文字')
})

test('共享块快照不推断本条已应用，失败片段优先展示未应用候选', () => {
  const record = { before: '甲旧，乙旧', after: '甲新，乙旧', status: 'technical_failed', applied: false }
  const snapshot = reviewComparison(record)
  assert.equal(snapshot.kind, 'snapshot')
  assert.equal(snapshot.label, '处理后文字')
  assert.equal(snapshot.applied, undefined)
  const candidate = reviewComparison({ ...record, candidates: { native: '甲旧，乙新' } })
  assert.equal(candidate.kind, 'candidate')
  assert.equal(candidate.after, '甲旧，乙新')
})

test('多处增删保留共同文本、换行、公式、空格和 Unicode 字符', () => {
  for (const [before, after] of [
    ['浓度 10，温度 25。', '浓度 20，温度 35。'],
    ['😀A\n$x_1$ <b>原文</b>', '😀AB\n$x_2$ <b>原文</b>'],
    ['', '新增'], ['移除', ''], ['完全一致', '完全一致'], ['A  B', 'A B']
  ]) {
    const { parts } = diffReviewText(before, after)
    assert.equal(parts.filter((part) => part.type !== 'insert').map((part) => part.text).join(''), before)
    assert.equal(parts.filter((part) => part.type !== 'delete').map((part) => part.text).join(''), after)
  }
  const { parts } = diffReviewText('浓度 10，温度 25。', '浓度 20，温度 35。')
  assert.deepEqual(parts.filter((part) => part.type === 'delete').map((part) => part.text), ['1', '2'])
  assert.ok(parts.some((part) => part.type === 'equal' && part.text.includes('温度')))
})

test('长文本对照限制矩阵规模，共同上下文可缩略而差异不丢失', () => {
  const { parts, coarse } = diffReviewText('A'.repeat(10000), 'B'.repeat(10000))
  assert.equal(coarse, true)
  assert.equal(parts[0].text.length, 10000)
  const compact = compactReviewSegments(diffReviewText(`${'原'.repeat(200)}旧${'文'.repeat(200)}`, `${'原'.repeat(200)}新${'文'.repeat(200)}`).parts)
  assert.deepEqual(compact.filter((part) => part.type !== 'equal').map((part) => part.text), ['旧', '新'])
  assert.ok(compact.filter((part) => part.type === 'equal').every((part) => part.text.includes('…')))
})
