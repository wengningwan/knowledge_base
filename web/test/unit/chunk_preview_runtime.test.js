import assert from 'node:assert/strict'
import { readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { pid } from 'node:process'
import { setImmediate } from 'node:timers'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick, ref } from 'vue'

/** 创建可执行事件与响应更新的组件宿主。 */
function hostNode(type, text = '') {
  const node = { type, text, props: {}, children: [], parent: null, style: {}, clientWidth: 800 }
  node.getContext = () => ({})
  node.scrollIntoView = () => { node.scrolled = true }
  node.querySelector = (selector) => nodes(node, (child) =>
    child.props['data-page-number'] === Number(selector.match(/"(\d+)"/)[1]))[0]
  return node
}
const renderer = createRenderer({
  createElement: hostNode,
  createText: (text) => hostNode('text', text),
  createComment: (text) => hostNode('comment', text),
  insert(child, parent, anchor = null) {
    if (child.parent) {
      const previousIndex = child.parent.children.indexOf(child)
      if (previousIndex >= 0) child.parent.children.splice(previousIndex, 1)
    }
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index >= 0) parent.children.splice(index, 0, child)
    else parent.children.push(child)
  },
  remove(child) {
    const index = child.parent?.children.indexOf(child) ?? -1
    if (index >= 0) child.parent.children.splice(index, 1)
    child.parent = null
  },
  setText: (node, text) => { node.text = text },
  setElementText(node, text) { node.text = text; node.children = [] },
  parentNode: (node) => node.parent,
  nextSibling(node) {
    const siblings = node.parent?.children || []
    return siblings[siblings.indexOf(node) + 1] || null
  },
  patchProp: (node, key, _previous, value) => { node.props[key] = value }
})

test('普通 PDF 预览继续渲染全部页面', async () => {
  const sourcePath = new URL('../../src/components/common/PdfPreview.vue', import.meta.url)
  const compiledPath = fileURLToPath(new URL(`../../.pdf-page-test-${pid}.mjs`, import.meta.url))
  const { descriptor } = parse(readFileSync(sourcePath, 'utf8'))
  const compiled = compileScript(descriptor, { id: 'pdf-page-test', inlineTemplate: true })
  writeFileSync(compiledPath, compiled.content
    .replace('@/utils/pdfPreviewErrors', './src/utils/pdfPreviewErrors.js')
    .replace("import('pdfjs-dist/legacy/build/pdf.mjs')", `Promise.resolve({
      GlobalWorkerOptions: {}, getDocument: () => ({ destroy() {}, promise: Promise.resolve({
        numPages: 3, getPage: async () => ({ getViewport: () => ({ width: 600, height: 800 }),
          render: () => ({ promise: Promise.resolve(), cancel() {} }) })
      }) })
    })`)
    .replace("import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url')", "Promise.resolve({ default: 'worker.js' })"))
  const previousWindow = globalThis.window
  globalThis.window = { devicePixelRatio: 1, innerWidth: 800 }
  let app
  try {
    const { default: Preview } = await import(pathToFileURL(compiledPath).href)
    const defaultRoot = hostNode('root')
    app = renderer.createApp(() => h(Preview, { url: 'default.pdf' }))
    app.mount(defaultRoot)
    await new Promise(setImmediate)
    assert.equal(byClass(defaultRoot, 'pdf-page-card').length, 3)
    assert.ok(byClass(defaultRoot, 'pdf-page-card').every((page) => !page.scrolled))
    assert.equal(nodes(defaultRoot, (node) => node.props.role === 'status').length, 0)
    app.unmount()
  } finally {
    app?.unmount()
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
    unlinkSync(compiledPath)
  }
})

test('论文策略显式选择与沿用知识库时均显示组合目标，其余策略保持最大值语义', async () => {
  const sourcePath = new URL('../../src/components/ChunkParamsConfig.vue', import.meta.url)
  const compiledPath = fileURLToPath(new URL(`../../.chunk-params-test-${pid}.mjs`, import.meta.url))
  const { descriptor } = parse(readFileSync(sourcePath, 'utf8'))
  const compiled = compileScript(descriptor, { id: 'chunk-params-test', inlineTemplate: true })
  writeFileSync(compiledPath, compiled.content
    .replace("import { useChunkPresetOptions } from '@/composables/useChunkPresetOptions'", `
      const useChunkPresetOptions = () => ({
        chunkPresetSelectOptions: ref([]), chunkPresetLabelMap: ref({}), chunkPresetLoading: ref(false),
        loadChunkPresetOptions: () => {}, getChunkPresetDescription: () => ''
      })`)
    .replace('@/utils/chunkUtils', './src/utils/chunkUtils.js'))
  try {
    const { default: Config } = await import(pathToFileURL(compiledPath).href)
    const props = ref({ tempChunkParams: { chunk_preset_id: 'paper' }, databasePresetId: 'general' })
    const root = hostNode('root')
    const app = renderer.createApp(() => h(Config, props.value))
    for (const name of ['a-form', 'a-form-item', 'a-select', 'a-input-number', 'a-input']) {
      app.component(name, { setup: (_props, { slots, attrs }) => () => h(name, attrs, [slots.label?.(), slots.default?.()]) })
    }
    app.component('a-tooltip', { props: ['title'], setup: (props, { slots }) => () => h('tooltip', { title: props.title }, slots.default?.()) })
    app.mount(root)
    for (const [preset, database, label] of [
      ['paper', 'general', '目标 Token 数'], ['', 'paper', '目标 Token 数'],
      ['general', 'paper', '最大 Token 数'], ['separator', 'paper', '最大 Token 数']
    ]) {
      props.value = { tempChunkParams: { chunk_preset_id: preset }, databasePresetId: database }
      await nextTick()
      const labels = nodes(root, (node) => node.type === 'text').map((node) => node.text).join('')
      assert.ok(labels.includes(label), `${preset || '沿用'} / ${database}: ${labels}`)
      const tooltip = nodes(root, (node) => node.type === 'tooltip' && node.props.title.includes('token 数'))[0]
      assert.equal(tooltip.props.title.includes('完整公式或表格可能超过目标'), label === '目标 Token 数')
    }
    app.unmount()
  } finally {
    unlinkSync(compiledPath)
  }
})

/** 读取渲染结果而非源码字符串。 */
function nodes(root, predicate) {
  return [root, ...root.children.flatMap((child) => nodes(child, () => true))].filter(predicate)
}
const byClass = (root, name) => nodes(root, (node) => String(node.props.class || '').split(' ').includes(name))

test('真实分块组件保留重复块、换行和全文，并响应展开、来源与版本切换', async () => {
  const sourcePath = new URL('../../src/components/knowledge/ChunkPreviewPanel.vue', import.meta.url)
  const compiledPath = fileURLToPath(new URL(`../../.chunk-preview-test-${pid}.mjs`, import.meta.url))
  const { descriptor } = parse(readFileSync(sourcePath, 'utf8'))
  const compiled = compileScript(descriptor, { id: 'chunk-preview-test', inlineTemplate: true })
  writeFileSync(compiledPath, compiled.content.replace('@/utils/chunkPreview', './src/utils/chunkPreview.js'))
  try {
    const { default: Panel } = await import(pathToFileURL(compiledPath).href)
    const content = '😀定义\n$$\nx = 1\n$$\n最后一行完整保留'
    const props = ref({
      sourceContent: content, sourceSha256: 'current', chunks: [
        { id: 'second', chunk_order_index: 1, content },
        { id: 'first', chunk_order_index: 0, content, source_metadata: {
          source_sha256: 'current', section_path: ['公式'],
          source_spans: [{ line_start: 1, line_end: 1, start_char: 0, end_char: 3 },
            { line_start: 5, line_end: 5, start_char: 16, end_char: 24, page: 4 }]
        } }
      ]
    })
    const root = hostNode('root')
    const app = renderer.createApp(() => h(Panel, props.value))
    app.component('a-modal', {
      props: ['open', 'title'], emits: ['cancel'],
      setup: (modalProps, { slots, emit }) => () => modalProps.open
        ? h('dialog', { title: modalProps.title }, [
          h('button', { class: 'dialog-close', onClick: () => emit('cancel') }), slots.default?.()
        ]) : null
    })
    app.mount(root)
    assert.equal(byClass(root, 'chunk-card').length, 2)
    assert.equal(byClass(root, 'chunk-order')[0].text, '分块 #0')
    assert.equal(byClass(root, 'chunk-card-content')[0].text, content)
    const expand = byClass(root, 'chunk-action')[0]
    assert.equal(expand.type, 'button')
    assert.equal(expand.props['aria-expanded'], false)
    expand.props.onClick()
    await nextTick()
    assert.equal(expand.props['aria-expanded'], true)
    assert.equal(byClass(root, 'chunk-card-content')[0].props.class.includes('is-collapsed'), false)
    assert.match(byClass(root, 'chunk-card-content')[0].text, /最后一行完整保留$/)
    assert.equal(nodes(root, (node) => node.props['aria-label'] === '查看 PDF 第 4 页').length, 0)
    const openedPdfPages = []
    props.value = { ...props.value, canOpenPdf: true, onOpenPdf: (page) => openedPdfPages.push(page) }
    await nextTick()
    assert.equal(nodes(root, (node) => node.type === 'button' && /查看 PDF/.test(node.props['aria-label'] || '')).length, 0)
    assert.deepEqual(openedPdfPages, [])
    expand.props.onClick()
    await nextTick()
    assert.equal(expand.props['aria-expanded'], false)

    const sourceButton = nodes(root, (node) => node.props['aria-label'] === '查看分块 0 的原文位置')[0]
    assert.ok(sourceButton)
    sourceButton.props.onClick()
    await nextTick()
    assert.equal(byClass(root, 'source-window').length, 2)
    assert.deepEqual(nodes(root, (node) => node.props['data-source-line'] != null)
      .map((node) => node.props['data-source-line']), [1, 5])
    byClass(root, 'dialog-close')[0].props.onClick()
    await nextTick()
    assert.equal(nodes(root, (node) => node.type === 'dialog').length, 0)

    expand.props.onClick()
    sourceButton.props.onClick()
    await nextTick()
    props.value = { ...props.value, sourceSha256: 'changed' }
    await nextTick()
    assert.equal(nodes(root, (node) => node.type === 'dialog').length, 0)
    assert.equal(byClass(root, 'chunk-action')[0].props['aria-expanded'], false)
    assert.equal(nodes(root, (node) => Boolean(node.props['aria-label'])).length, 0)
    assert.match(byClass(root, 'chunk-source')[0].children[0].text, /原文已变化/)

    const body = '😀第一段正文\n\n$$\nx = 1\n$$\n\n论文：这是正文引用的名称\n章节：这是原文中的说明\n最后一行完整保留'
    const storedContent = `论文：合成论文\n章节：1. INTRODUCTION\n\n${body}`
    const paperChunk = { content: storedContent, source_metadata: {
      chunker_version: 'paper_rules_v1', section_path: ['1. INTRODUCTION']
    } }
    props.value = { ...props.value, chunks: [paperChunk] }
    await nextTick()
    assert.equal(byClass(root, 'chunk-section').length, 1)
    assert.equal(byClass(root, 'chunk-section')[0].text, '1. INTRODUCTION')
    assert.equal(byClass(root, 'chunk-card-content')[0].text, body)
    byClass(root, 'chunk-action')[0].props.onClick()
    await nextTick()
    assert.equal(byClass(root, 'chunk-card-content')[0].text, body)
    assert.equal(paperChunk.content, storedContent)

    for (const [content, metadata, expected] of [
      [storedContent, { chunker_version: 'ragflow_like_v1' }, storedContent],
      [storedContent, { chunker_version: 'paper_rules_v2', section_path: ['1. INTRODUCTION'] }, body],
      [storedContent, { chunker_version: 'paper_rules_v3', section_path: ['1. INTRODUCTION'] }, body],
      [storedContent, { chunker_version: 'paper_rules_unknown', section_path: ['1. INTRODUCTION'] }, storedContent],
      [storedContent, { chunker_version: 'paper_rules_v1', section_path: '1. INTRODUCTION' }, storedContent],
      [storedContent, { chunker_version: 'paper_rules_v1', section_path: ['另一章节'] }, storedContent],
      [body, { chunker_version: 'paper_rules_v1', section_path: ['1. INTRODUCTION'] }, body],
      [`论文：合成论文\n章节：正文\n\n${body}`, { chunker_version: 'paper_rules_v1', section_path: [] }, body],
      [`论文：合成论文\n章节：实验 > 结果\n\n${body}`, { chunker_version: 'paper_rules_v1', section_path: ['实验', '结果'] }, body]
    ]) {
      props.value = { ...props.value, chunks: [{ content, source_metadata: metadata }] }
      await nextTick()
      assert.equal(byClass(root, 'chunk-card-content')[0].text, expected)
    }
    const original = '\n# 合成论文\n\n## 实验\n\n合成论文\n\n实验'
    const range = (needle) => {
      const start = original.indexOf(needle)
      return { start_char: start, end_char: start + needle.length,
        line_start: original.slice(0, start).split('\n').length,
        line_end: original.slice(0, start + needle.length - 1).split('\n').length }
    }
    const contextSpans = [range('# 合成论文'), range('## 实验')]
    const bodySpan = range('合成论文\n\n实验')
    for (const version of ['paper_rules_v1', 'paper_rules_v2', 'paper_rules_v3']) {
      props.value = { sourceContent: original, sourceSha256: 'current', chunks: [{ chunk_order_index: 0,
        content: '论文：合成论文\n章节：实验\n\n合成论文\n\n实验', source_metadata: {
          chunker_version: version, section_path: ['实验'], source_sha256: 'current',
          source_spans: version === 'paper_rules_v3' ? [bodySpan] : [...contextSpans, bodySpan],
          context_source_spans: contextSpans
        } }] }
      await nextTick()
      assert.equal(byClass(root, 'chunk-card-content')[0].text, '合成论文\n\n实验')
      assert.equal(byClass(root, 'chunk-source')[0].children[0].text, '原文第 6–8 行')
      nodes(root, (node) => node.props['aria-label'] === '查看分块 0 的原文位置')[0].props.onClick()
      await nextTick()
      assert.equal(byClass(root, 'source-window').length, 1)
      assert.deepEqual(nodes(root, (node) => node.props['data-source-line'] != null)
        .map((node) => node.props['data-source-line']), [6, 7, 8])
    }
    app.unmount()
  } finally {
    unlinkSync(compiledPath)
  }
})
