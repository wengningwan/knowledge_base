import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { pid } from 'node:process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick, ref, KeepAlive } from 'vue'

/** 为真实 Vue 模板提供事件和响应更新宿主。 */
function hostNode(type, text = '') {
  const node = { type, text, props: {}, children: [], parent: null, addEventListener() {}, removeEventListener() {}, getRootNode: () => ({}) }
  Object.defineProperty(node, 'options', { get: () => node.children.filter((child) => child.type === 'option') })
  return node
}
const renderer = createRenderer({
  createElement: hostNode, createText: (text) => hostNode('text', text), createComment: (text) => hostNode('comment', text),
  insert(child, parent, anchor = null) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(child)
    else parent.children.splice(index, 0, child)
  },
  remove(child) { child.parent.children.splice(child.parent.children.indexOf(child), 1) },
  setText: (node, text) => { node.text = text },
  setElementText(node, text) { node.text = text; node.children = [] },
  parentNode: (node) => node.parent,
  nextSibling(node) { return node.parent?.children[node.parent.children.indexOf(node) + 1] || null },
  patchProp(node, key, _previous, value) { node.props[key] = value; if (key === 'value') node._value = value }
})

/** 从渲染树读取用户可见内容。 */
function nodes(root, predicate) {
  return [root, ...root.children.flatMap((child) => nodes(child, () => true))].filter(predicate)
}
const byClass = (root, name) => nodes(root, (node) => String(node.props.class || '').split(' ').includes(name))
const textOf = (root) => root.text + root.children.filter((node) => node.type !== 'comment').map(textOf).join('')

test('真实校对组件筛选、分页、分片、差异、原图与版本切换及往返保持，无 PDF 跳转入口', async () => {
  const source = new URL('../../src/components/knowledge/DocumentReviewPanel.vue', import.meta.url)
  const path = fileURLToPath(new URL(`../../.review-runtime-${pid}.mjs`, import.meta.url))
  const { descriptor } = parse(readFileSync(source, 'utf8'))
  const compiled = compileScript(descriptor, { id: 'document-review-test', inlineTemplate: true })
  writeFileSync(path, compiled.content
    .replace("import MarkdownPreview from '@/components/common/MarkdownPreview.vue'", `
      import { h } from 'vue'
      const MarkdownPreview = { props: ['content'], setup: (props) => () => h('image-preview', { content: props.content }) }
    `)
    .replace('@/utils/document_parse_state', './src/utils/document_parse_state.js')
    .replace('@/utils/documentReview', './src/utils/documentReview.js'))
  let app
  const previousDocument = globalThis.Document
  const previousShadowRoot = globalThis.ShadowRoot
  globalThis.Document = class Document {}
  globalThis.ShadowRoot = class ShadowRoot {}
  try {
    const { default: Panel } = await import(pathToFileURL(path).href)
    const reviews = [
      { id: 'ok', status: 'checked', source: 'model', before: '正常内容', after: '正常内容' },
      ...Array.from({ length: 10 }, (_, index) => ({ id: `issue-${index}`, kind: 'text_mismatch', page: index + 1, status: 'unresolved', before: `片段 ${index} 旧`, candidates: { native: `片段 ${index} 新` } })),
      { id: 'part1', parent_issue_id: 'split', status: 'machine_corrected', applied: true, before: '甲旧，乙旧', after: '甲新，乙旧', page: 12,
        targets: [{ id: 'a', old: '甲旧', context_after: '，乙旧' }] },
      { id: 'part2', parent_issue_id: 'split', status: 'technical_failed', applied: false, before: '甲旧，乙旧', after: '甲新，乙旧', source_image: '/api/knowledge/databases/kb/images/crop.png', page: 12,
        targets: [{ id: 'b', old: '乙旧', context_before: '甲旧，' }] },
      { id: 'deleted', status: 'machine_corrected', before: '移除内容', after: '' }
    ]
    const state = ref({ reviews, metadata: { review_status: 'needs_review', auto_index_enabled: true },
      diagnostics: [{ reason: '诊断内容不应显示' }], attempt_id: 'v1' })
    const visible = ref(true)
    const saveReview = ref(null)
    const root = hostNode('root')
    app = renderer.createApp(() => h(KeepAlive, null, {
      // 携带旧版能力输入，恢复旧按钮时“无入口”断言必须失败。
      default: () => visible.value ? h(Panel, { state: state.value, kbId: 'kb', canOpenPdf: true,
        sourceSha256: 'a'.repeat(64), hasIndexes: true, saveReview: saveReview.value }) : null
    }))
    for (const name of ['a-alert', 'a-tag', 'a-spin', 'a-empty']) {
      app.component(name, { props: ['message', 'description', 'tip'], setup: (props, { slots }) => () => h(name, null, [props.message, props.description, props.tip, slots.default?.()]) })
    }
    app.component('a-modal', { setup: (_props, { slots }) => () => h('dialog', null, slots.default?.()) })
    app.mount(root)
    const click = async (node) => { node.props.onClick(); await nextTick() }
    const button = (text) => nodes(root, (node) => node.type === 'button' && textOf(node).includes(text))[0]
    assert.equal(byClass(root, 'review-item').length, 8)
    assert.match(textOf(byClass(root, 'review-filters')[0]), /待人工核对 10/)
    assert.equal(byClass(root, 'review-detail').length, 1)
    assert.match(textOf(byClass(root, 'review-detail')[0]), /OCR识别文字/)
    assert.match(textOf(byClass(root, 'review-detail')[0]), /PDF提取文字/)
    assert.match(textOf(root), /暂不进行自动入库操作/)
    assert.deepEqual(byClass(root, 'review-difference').map(textOf), ['旧', '新'])
    assert.equal(nodes(root, (node) => ['del', 'ins'].includes(node.type)).length, 0)
    assert.doesNotMatch(textOf(root), /候选文本与校对依据|解析诊断|诊断内容不应显示/)
    for (let i = 0; i < 8; i++) await click(button('下一项'))
    assert.match(textOf(byClass(root, 'review-navigation')[0]), /第 9 \/ 10 项/)
    assert.match(textOf(byClass(root, 'review-pagination')[0]), /2 \/ 2 页/)
    assert.equal(Boolean(button('查看 PDF')), false, '校对面板不得生成 PDF 跳转按钮')
    visible.value = false
    await nextTick()
    visible.value = true
    await nextTick()
    assert.match(textOf(byClass(root, 'review-navigation')[0]), /第 9 \/ 10 项/)
    await click(button('复核未完成'))
    assert.equal(byClass(root, 'review-item').length, 1)
    assert.match(textOf(byClass(root, 'review-detail')[0]), /可能包含其他问题的修改/)
    assert.match(textOf(byClass(root, 'review-target')[0]), /本片段核对范围甲旧，乙旧/)
    assert.equal(textOf(nodes(byClass(root, 'review-target')[0], (node) => node.type === 'mark')[0]), '乙旧')
    assert.match(textOf(byClass(root, 'review-detail-tags')[0]), /复核未完成/)
    assert.equal(nodes(root, (node) => node.type === 'image-preview').length, 1)
    await click(button('放大'))
    assert.equal(nodes(root, (node) => node.type === 'dialog').length, 1)
    const select = nodes(root, (node) => node.type === 'select')[0]
    select.props['onUpdate:modelValue'](0)
    await nextTick()
    assert.match(textOf(byClass(root, 'review-detail')[0]), /处理后文字/)
    assert.equal(textOf(nodes(byClass(root, 'review-target')[0], (node) => node.type === 'mark')[0]), '甲旧')
    assert.equal(nodes(root, (node) => node.type === 'dialog').length, 0)
    state.value = { ...state.value, reviews: [{ id: 'new', status: 'machine_corrected', before: '已删除', after: '' }], attempt_id: 'v2' }
    await nextTick()
    assert.match(textOf(byClass(root, 'review-detail')[0]), /内容已移除/)
    await click(button('待人工核对'))
    assert.match(textOf(root), /当前没有待人工核对的问题/)
    await click(button('查看全部记录'))
    assert.equal(byClass(root, 'review-item').length, 1)
    for (const [metadata, blocked] of [
      [{ review_status: 'needs_review', auto_index_enabled: true }, true],
      [{ review_status: 'incomplete', auto_index_enabled: true }, true],
      [{ review_status: 'needs_review', auto_index_enabled: false }, false],
      [{ review_status: 'incomplete', auto_index_enabled: false }, false],
      [{ review_status: 'needs_review' }, false],
      [{ review_status: 'completed', auto_index_enabled: true }, false]
    ]) {
      state.value = { reviews: [], diagnostics: [], metadata }
      await nextTick()
      assert.equal(textOf(root).includes('暂不进行自动入库操作'), blocked)
      assert.doesNotMatch(textOf(root), /自动入库已跳过/)
    }
    for (const [extra, expected] of [[{ loading: true }, '正在加载校对结果'], [{ error: '读取失败' }, '读取失败'], [{ legacy_without_assets: true }, '重新解析'], [{ reviews: [] }, '本次解析没有校对记录']]) {
      state.value = { reviews: [], metadata: {}, diagnostics: [], ...extra }
      await nextTick()
      assert.match(textOf(root), new RegExp(expected))
    }
    state.value = { attempt_id: 'manual', metadata: { review_status: 'needs_review' },
      blocks: [{ id: 'b1', type: 'text', content: '当前段落含其他已修改文字。' }],
      reviews: [
        { id: 'manual-r1', block_id: 'b1', status: 'unresolved', before: '原始 OCR', candidates: { native: 'PDF 正确段落' } },
        { id: 'manual-r2', block_id: null, status: 'unresolved', kind: 'missing_region' }
      ] }
    await nextTick()
    assert.equal(nodes(root, (node) => node.type === 'textarea').length, 0, '只读消费者没有保存入口')
    const submitted = []
    saveReview.value = async (changes) => { submitted.push(changes); throw new Error('保存失败，输入已保留') }
    await nextTick()
    let textarea = nodes(root, (node) => node.type === 'textarea')[0]
    assert.equal(textarea.value, '当前段落含其他已修改文字。', '编辑当前块而不是原 OCR 快照')
    await click(button('采用 PDF 提取文字'))
    assert.equal(textarea.value, 'PDF 正确段落')
    textarea.props['onUpdate:modelValue']('人工校对后的正文 $x_1$。')
    await nextTick()
    await button('确认整段并同步到 MD').props.onClick()
    await nextTick()
    assert.equal(textarea.value, '人工校对后的正文 $x_1$。')
    assert.equal(textOf(byClass(root, 'review-save-error')[0]), '保存失败')
    assert.match(textOf(root), /保存后需重新入库/)
    assert.deepEqual(submitted[0], { attempt_id: 'manual', review_revision: null, source_sha256: 'a'.repeat(64),
      review_index: 0, action: 'replace', content: '人工校对后的正文 $x_1$。', anchor_block_id: null })
    await click(button('下一项'))
    const insertButton = button('确认补入并同步到 MD')
    assert.equal(insertButton.props.disabled, true)
    textarea = nodes(root, (node) => node.type === 'textarea')[0]
    textarea.props['onUpdate:modelValue']('遗漏段落。')
    const anchor = nodes(root, (node) => node.type === 'select')[0]
    anchor.props['onUpdate:modelValue']('b1')
    await nextTick()
    assert.equal(button('确认补入并同步到 MD').props.disabled, false)
    await button('确认补入并同步到 MD').props.onClick()
    assert.equal(submitted.at(-1).anchor_block_id, 'b1')
    assert.equal(submitted.at(-1).review_index, 1)
    await click(button('上一项'))
    assert.equal(nodes(root, (node) => node.type === 'textarea')[0].value, '人工校对后的正文 $x_1$。', '切换问题仍保留未保存草稿')
    let finishSave
    saveReview.value = (changes) => new Promise((resolve) => { submitted.push(changes); finishSave = resolve })
    await nextTick()
    const pendingSave = button('保留正文并确认').props.onClick()
    await nextTick()
    assert.equal(button('下一项').props.disabled, true)
    assert.match(textOf(root), /正在同步/)
    finishSave()
    await pendingSave
    assert.equal(submitted.at(-1).action, 'keep')
    assert.equal(submitted.at(-1).content, '')
    state.value = { ...state.value, metadata: { review_status: 'completed', human_review_revision: 'r2' },
      reviews: state.value.reviews.map((row) => ({ ...row, human_review: { confirmed_by: 'human' } })) }
    await nextTick()
    assert.doesNotMatch(textOf(byClass(root, 'review-filters')[0]), /待人工核对 [1-9]/)
    await click(button('人工已确认'))
    assert.equal(byClass(root, 'review-item').length, 2)
    assert.match(textOf(byClass(root, 'review-detail-tags')[0]), /人工已确认/)
    state.value = { attempt_id: 'two-drafts', metadata: { review_status: 'needs_review' },
      blocks: [{ id: 'a', type: 'text', content: 'A 原文' }, { id: 'b', type: 'text', content: 'B 原文' }],
      reviews: [{ id: 'ra', block_id: 'a', status: 'unresolved' }, { id: 'rb', block_id: 'b', status: 'unresolved' }] }
    await nextTick()
    nodes(root, (node) => node.type === 'textarea')[0].props['onUpdate:modelValue']('A 草稿')
    await click(button('下一项'))
    nodes(root, (node) => node.type === 'textarea')[0].props['onUpdate:modelValue']('B 未提交草稿')
    await click(button('上一项'))
    saveReview.value = async () => {
      state.value = { ...state.value, loading: true }
      await nextTick()
      state.value = { ...state.value, loading: false, metadata: { review_status: 'needs_review', human_review_revision: 'after-a' },
        blocks: state.value.blocks.map((block) => block.id === 'a' ? { ...block, content: 'A 草稿' } : block),
        reviews: state.value.reviews.map((row) => row.id === 'ra' ? { ...row, human_review: { block_id: 'a' } } : row) }
      await nextTick()
    }
    await nextTick()
    await button('确认整段并同步到 MD').props.onClick()
    await nextTick()
    assert.equal(nodes(root, (node) => node.type === 'textarea')[0].value, 'B 未提交草稿', '保存 A 并刷新后仍保留 B 草稿')
    state.value = { ...state.value, metadata: { ...state.value.metadata, human_review_revision: 'external-b' },
      blocks: state.value.blocks.map((block) => block.id === 'b' ? { ...block, content: 'B 外部新版正文' } : block) }
    await nextTick()
    assert.equal(nodes(root, (node) => node.type === 'textarea')[0].value, 'B 未提交草稿')
    assert.equal(button('确认整段并同步到 MD').props.disabled, true)
    assert.match(textOf(root), /该段正文已更新/)
    await click(button('放弃草稿，载入当前段落'))
    assert.equal(nodes(root, (node) => node.type === 'textarea')[0].value, 'B 外部新版正文')
    assert.equal(button('确认整段并同步到 MD').props.disabled, false)
  } finally {
    app?.unmount()
    unlinkSync(path)
    if (previousDocument) globalThis.Document = previousDocument
    else delete globalThis.Document
    if (previousShadowRoot) globalThis.ShadowRoot = previousShadowRoot
    else delete globalThis.ShadowRoot
  }
})
