import assert from 'node:assert/strict'
import test from 'node:test'
import { createDocumentParseLoader, documentMarkdown, knowledgeAssetImageMarkdown } from '../../src/utils/document_parse_state.js'

const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

test('人工保存后刷新保留同 attempt 身份，立即停止旧正文下载，失败也不销毁其他草稿宿主', async () => {
  let version = 1
  let failed = false
  let projection
  let freshContent
  const history = []
  const loader = createDocumentParseLoader({
    loadContent: () => version === 1 ? { content: '旧正文', source_sha256: 'old', attempt_id: 'same' } : freshContent.promise,
    loadAssets: async () => {
      if (failed) throw new Error('暂时无法读取')
      return { attempt_id: 'same', metadata: { human_review_revision: version }, reviews: [{ id: 'row' }] }
    },
    onChange: (value) => { projection = value; history.push(value) }
  })
  loader.reset({ kbId: 'kb', fileId: 'file' })
  await loader.assets()
  version = 2
  freshContent = deferred()
  history.length = 0
  const refresh = loader.refresh()
  assert.equal(documentMarkdown(projection.content), '')
  assert.equal(projection.content.source_sha256, '')
  assert.equal(projection.assets.attempt_id, 'same')
  freshContent.resolve({ content: '新正文', source_sha256: 'new', attempt_id: 'same' })
  await refresh
  assert.equal(documentMarkdown(projection.content), '新正文')
  assert.equal(projection.assets.metadata.human_review_revision, 2)
  assert.ok(history.every((entry) => entry.assets.attempt_id === 'same'))
  failed = true
  await loader.refresh()
  assert.match(projection.assets.error, /暂时无法读取/)
  assert.equal(projection.assets.attempt_id, 'same')
  assert.ok(history.every((entry) => entry.assets.attempt_id === 'same'))
})

test('图片标签惰性读取产物，切换标签复用同版结果', async () => {
  let reads = 0
  let projection
  const loader = createDocumentParseLoader({
    loadContent: async () => ({ content: '正文', attempt_id: 'a' }),
    loadAssets: async () => { reads += 1; return { figures: [{ id: 'figure-1' }], attempt_id: 'a' } },
    onChange: (value) => { projection = value }
  })
  loader.reset({ kbId: 'kb', fileId: 'one' })
  await loader.content()
  assert.equal(reads, 0)
  await loader.assets()
  await loader.assets()
  assert.equal(reads, 1)
  assert.equal(projection.assets.figures[0].id, 'figure-1')
})

test('正文与产物 attempt 不同会清缓存重读，任何投影都不混合两版', async () => {
  let contentReads = 0
  let assetReads = 0
  const projections = []
  const loader = createDocumentParseLoader({
    loadContent: async () => ({ content: ++contentReads === 1 ? '旧正文' : '新正文', attempt_id: contentReads === 1 ? 'old' : 'new' }),
    loadAssets: async () => { assetReads += 1; return { figures: [{ id: 'new-figure' }], attempt_id: 'new' } },
    onChange: (value) => projections.push(value)
  })
  loader.reset({ kbId: 'kb', fileId: 'one' })
  await loader.content()
  await loader.assets()
  assert.equal(contentReads, 2)
  assert.equal(assetReads, 2)
  assert.equal(projections.at(-1).content.content, '新正文')
  for (const { content, assets } of projections) {
    if (content.loaded && assets.loaded) assert.equal(content.attempt_id, assets.attempt_id)
  }
})

test('持续版本变化显式失败，不无限重试或展示混合结果', async () => {
  let projection
  const loader = createDocumentParseLoader({
    loadContent: async () => ({ content: '不能混用', attempt_id: 'old' }),
    loadAssets: async () => ({ attempt_id: 'new' }),
    onChange: (value) => { projection = value }
  })
  loader.reset({ kbId: 'kb', fileId: 'one' })
  await loader.assets()
  assert.match(projection.assets.error, /解析版本已变化/)
  assert.equal(documentMarkdown(projection.content), '')
})

test('文件切换或关闭后迟到的正文与产物响应不污染当前文件', async () => {
  const oldContent = deferred()
  const oldAssets = deferred()
  let projection
  const loader = createDocumentParseLoader({
    loadContent: ({ fileId }) => fileId === 'old' ? oldContent.promise : Promise.resolve({ content: '新文件', attempt_id: 'new' }),
    loadAssets: ({ fileId }) => fileId === 'old' ? oldAssets.promise : Promise.resolve({ figures: [{ id: 'new' }], attempt_id: 'new' }),
    onChange: (value) => { projection = value }
  })
  loader.reset({ kbId: 'kb', fileId: 'old' })
  const pending = loader.assets()
  loader.reset({ kbId: 'kb', fileId: 'new' })
  await loader.assets()
  oldContent.resolve({ content: '旧文件', attempt_id: 'old' })
  oldAssets.resolve({ figures: [{ id: 'old' }], attempt_id: 'old' })
  await pending
  assert.equal(projection.content.content, '新文件')
  assert.equal(projection.assets.figures[0].id, 'new')
  loader.reset()
  await loader.assets()
  assert.equal(projection.content.content, '')
  assert.deepEqual(projection.assets.figures, [])
})

test('旧文件显示缺少产物，正文错误保留 chunks 且绝不回退成原文', async () => {
  let projection
  const lines = [{ content: '检索片段不是原文' }]
  const loader = createDocumentParseLoader({
    loadContent: async () => ({ lines, content: '不可用正文', content_error: '原文读取失败' }),
    loadAssets: async () => ({ legacy_without_assets: true, figures: [], reviews: [] }),
    onChange: (value) => { projection = value }
  })
  loader.reset({ kbId: 'kb', fileId: 'one' })
  await loader.assets()
  assert.equal(projection.assets.legacy_without_assets, true)
  assert.deepEqual(projection.content.lines, lines)
  assert.equal(projection.content.error, '原文读取失败')
  assert.equal(documentMarkdown(projection.content), '')
  assert.equal(documentMarkdown({ content: '', lines }), '')
  assert.equal(documentMarkdown({ content: '# 纯正文', lines }), '# 纯正文')
})

test('关闭后迟到的请求不能恢复图片或正文', async () => {
  const pendingContent = deferred()
  const pendingAssets = deferred()
  let projection
  const loader = createDocumentParseLoader({
    loadContent: () => pendingContent.promise,
    loadAssets: () => pendingAssets.promise,
    onChange: (value) => { projection = value }
  })
  loader.reset({ kbId: 'kb', fileId: 'closed' })
  const pending = loader.assets()
  loader.reset()
  pendingContent.resolve({ content: '已关闭文件', attempt_id: 'old' })
  pendingAssets.resolve({ figures: [{ id: 'old' }], attempt_id: 'old' })
  await pending
  assert.equal(projection.content.content, '')
  assert.deepEqual(projection.assets.figures, [])
  assert.equal(projection.content.loaded, false)
})

test('独立图片只接受当前知识库的鉴权代理地址', () => {
  const source = '/api/knowledge/databases/kb/images/figure.png'
  assert.equal(knowledgeAssetImageMarkdown(source, 'kb'), `![原图](${source})`)
  assert.equal(knowledgeAssetImageMarkdown(source, 'other'), '')
  assert.equal(knowledgeAssetImageMarkdown('https://example.com/a.png', 'kb'), '')
})

test('来源指纹随当前正文透传，读取失败或关闭后不保留旧指纹', async () => {
  let projection
  let fail = false
  const loader = createDocumentParseLoader({
    loadContent: async () => ({ content: '当前正文', source_sha256: 'current', content_error: fail ? '读取失败' : '' }),
    loadAssets: async () => ({}),
    onChange: (value) => { projection = value }
  })
  loader.reset({ kbId: 'kb', fileId: 'one' })
  await loader.content()
  assert.equal(projection.content.source_sha256, 'current')
  loader.reset()
  assert.equal(projection.content.source_sha256, '')
  fail = true
  loader.reset({ kbId: 'kb', fileId: 'two' })
  await loader.content()
  assert.equal(projection.content.source_sha256, '')
  assert.equal(projection.content.content, '')
})
