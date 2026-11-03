import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { pid } from 'node:process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick, ref, KeepAlive } from 'vue'

/** 为真实 Vue 模板提供事件和响应更新宿主。 */
function hostNode(type, text = '') {
  const node = { type, text, props: {}, children: [], parent: null, addEventListener() {}, removeEventListener() {} }
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

test('真实校对组件筛选、分页、分片、差异、原图与版本切换及往返保持', async () => {
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
    const state = ref({ reviews, metadata: { review_status: 'needs_review' }, diagnostics: [], attempt_id: 'v1' })
    const visible = ref(true)
    const events = []
    const root = hostNode('root')
    app = renderer.createApp(() => h(KeepAlive, null, {
      default: () => visible.value ? h(Panel, { state: state.value, kbId: 'kb', canOpenPdf: true, onOpenPdf: (page) => events.push(page) }) : null
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
    assert.match(textOf(byClass(root, 'review-detail')[0]), /文字层内容仅供对照，尚未应用/)
    assert.deepEqual(nodes(root, (node) => node.type === 'del').map(textOf), ['旧'])
    assert.deepEqual(nodes(root, (node) => node.type === 'ins').map(textOf), ['新'])
    for (let i = 0; i < 8; i++) await click(button('下一项'))
    assert.match(textOf(byClass(root, 'review-navigation')[0]), /第 9 \/ 10 项/)
    assert.match(textOf(byClass(root, 'review-pagination')[0]), /2 \/ 2 页/)
    await click(button('查看 PDF'))
    assert.deepEqual(events, [9])
    visible.value = false
    await nextTick()
    visible.value = true
    await nextTick()
    assert.match(textOf(byClass(root, 'review-navigation')[0]), /第 9 \/ 10 项/)
    await click(button('复核未完成'))
    assert.equal(byClass(root, 'review-item').length, 1)
    assert.match(textOf(byClass(root, 'review-detail')[0]), /整块快照可能包含同块其他问题的修改/)
    assert.match(textOf(byClass(root, 'review-target')[0]), /本片段核对范围甲旧，乙旧/)
    assert.equal(textOf(nodes(root, (node) => node.type === 'mark')[0]), '乙旧')
    assert.match(textOf(byClass(root, 'review-detail-tags')[0]), /复核未完成/)
    assert.equal(nodes(root, (node) => node.type === 'image-preview').length, 1)
    await click(button('放大'))
    assert.equal(nodes(root, (node) => node.type === 'dialog').length, 1)
    const select = nodes(root, (node) => node.type === 'select')[0]
    select.props['onUpdate:modelValue'](0)
    await nextTick()
    assert.match(textOf(byClass(root, 'review-detail')[0]), /当前块正文快照/)
    assert.equal(textOf(nodes(root, (node) => node.type === 'mark')[0]), '甲旧')
    assert.equal(nodes(root, (node) => node.type === 'dialog').length, 0)
    state.value = { ...state.value, reviews: [{ id: 'new', status: 'machine_corrected', before: '已删除', after: '' }], attempt_id: 'v2' }
    await nextTick()
    assert.match(textOf(byClass(root, 'review-detail')[0]), /内容已移除/)
    await click(button('待人工核对'))
    assert.match(textOf(root), /当前没有待人工核对的问题/)
    await click(button('查看全部记录'))
    assert.equal(byClass(root, 'review-item').length, 1)
    for (const [extra, expected] of [[{ loading: true }, '正在加载校对结果'], [{ error: '读取失败' }, '读取失败'], [{ legacy_without_assets: true }, '重新解析'], [{ reviews: [] }, '本次解析没有校对记录']]) {
      state.value = { reviews: [], metadata: {}, diagnostics: [], ...extra }
      await nextTick()
      assert.match(textOf(root), new RegExp(expected))
    }
  } finally {
    app?.unmount()
    unlinkSync(path)
  }
})
