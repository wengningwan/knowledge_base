import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

test('重解析标记属于请求顶层且旧调用默认关闭，产物使用鉴权 API', async () => {
  const previousFetch = globalThis.fetch
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: (key) => key === 'user_token' ? 'test-token' : null, setItem() {}, removeItem() {} }
  const requests = []
  const headers = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, body: options.body ? JSON.parse(options.body) : undefined })
    headers.push(options.headers)
    return new Response(JSON.stringify({ status: 'queued' }), { status: 200, headers: { 'content-type': 'application/json' } })
  }
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    useUserStore().userRole = 'superadmin'
    const { documentApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const params = { ocr_engine: 'oxr_ocr', image_analysis_model: '', document_review_model: '' }
    await documentApi.parseDocuments('kb', ['one'], params)
    await documentApi.parseDocuments('kb', ['one'], params, true)
    await documentApi.getDocumentParseAssets('kb', 'one')
    const confirmation = { attempt_id: 'attempt', review_index: 0, source_sha256: 'a'.repeat(64), action: 'keep' }
    await documentApi.confirmDocumentReview('kb', 'one', confirmation)
    assert.deepEqual(requests, [
      { url: '/api/knowledge/databases/kb/documents/parse', body: { file_ids: ['one'], params, reparse: false } },
      { url: '/api/knowledge/databases/kb/documents/parse', body: { file_ids: ['one'], params, reparse: true } },
      { url: '/api/knowledge/databases/kb/documents/one/parse-assets', body: undefined },
      { url: '/api/knowledge/databases/kb/documents/one/review-confirmations', body: confirmation }
    ])
    assert.ok(headers.every((value) => value.Authorization === 'Bearer test-token'))
    assert.equal(Object.hasOwn(params, 'reparse'), false)
  } finally {
    await server.close()
    globalThis.fetch = previousFetch
    globalThis.localStorage = previousStorage
  }
})
