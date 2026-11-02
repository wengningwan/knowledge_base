export const emptyDocumentContent = () => ({
  loading: false, loaded: false, lines: [], content: '', source_sha256: '', error: '', attempt_id: null
})
export const emptyDocumentAssets = () => ({
  loading: false, loaded: false, figures: [], reviews: [], diagnostics: [], metadata: {},
  legacy_without_assets: false, error: '', attempt_id: null
})

/** 只向 Markdown 消费者提供解析原文，分块与模型输出不参与回退。 */
export const documentMarkdown = (state) => state.error ? '' : String(state.content || '')

/** 管理当前文档请求与解析版本；失效响应不能覆盖当前文档。 */
export function createDocumentParseLoader({ loadContent, loadAssets, onChange }) {
  let generation = 0
  let contentSequence = 0
  let assetSequence = 0
  let context = null
  let content = emptyDocumentContent()
  let assets = emptyDocumentAssets()
  let contentPending = null
  let assetsPending = null
  let reconciling = null
  const publish = () => onChange({ content: { ...content }, assets: { ...assets } })
  const reset = (nextContext = null) => {
    generation += 1
    contentSequence += 1
    assetSequence += 1
    context = nextContext
    content = emptyDocumentContent()
    assets = emptyDocumentAssets()
    contentPending = null
    assetsPending = null
    reconciling = null
    publish()
  }
  const mismatched = () => content.loaded && assets.loaded &&
    (content.attempt_id || null) !== (assets.attempt_id || null)

  const request = (kind) => {
    if (!context) return Promise.resolve()
    const isContent = kind === 'content'
    const current = isContent ? content : assets
    const pending = isContent ? contentPending : assetsPending
    if (pending) return pending
    if (current.loaded) return Promise.resolve()
    const epoch = generation
    const sequence = isContent ? ++contentSequence : ++assetSequence
    const requestContext = { ...context }
    const isCurrent = () => epoch === generation &&
      sequence === (isContent ? contentSequence : assetSequence)
    if (isContent) content = { ...content, loading: true, error: '' }
    else assets = { ...assets, loading: true, error: '' }
    publish()
    const promise = (async () => {
      try {
        const data = await Promise.resolve().then(() =>
          (isContent ? loadContent : loadAssets)(requestContext))
        if (!isCurrent()) return
        if (data?.status === 'failed') throw new Error(data.message || '加载解析结果失败')
        if (isContent) {
          content = {
            ...emptyDocumentContent(), loaded: true, lines: data?.lines || [],
            content: data?.content_error ? '' : String(data?.content || ''),
            source_sha256: data?.content_error ? '' : String(data?.source_sha256 || ''),
            error: data?.content_error || '', attempt_id: data?.attempt_id || null
          }
        } else {
          assets = { ...emptyDocumentAssets(), ...data, loading: false, loaded: true, error: '' }
        }
      } catch (error) {
        if (!isCurrent()) return
        const errorMessage = error.message || '加载解析结果失败'
        if (isContent) content = { ...emptyDocumentContent(), error: errorMessage }
        else assets = { ...emptyDocumentAssets(), error: errorMessage }
      } finally {
        if (isCurrent()) {
          if (isContent) contentPending = null
          else assetsPending = null
          // 两版结果不可出现在同一次界面投影中。
          if (mismatched()) {
            content = { ...emptyDocumentContent(), loading: true }
            assets = { ...emptyDocumentAssets(), loading: true }
          }
          publish()
        }
      }
    })()
    if (isContent) contentPending = promise
    else assetsPending = promise
    return promise
  }

  const reconcile = async () => {
    // mismatch 在 publish 前清空，但保留 loaded=false/loading=true 作为重读标记。
    if (!content.loading || !assets.loading || contentPending || assetsPending) return
    if (reconciling) return reconciling
    const epoch = generation
    reconciling = (async () => {
      await Promise.all([request('content'), request('assets')])
      if (epoch !== generation) return
      if (content.loading && assets.loading) {
        const error = '解析版本已变化，请重新打开文件后重试'
        content = { ...emptyDocumentContent(), error }
        assets = { ...emptyDocumentAssets(), error }
        publish()
      }
    })()
    try { await reconciling } finally { if (epoch === generation) reconciling = null }
  }
  return {
    reset,
    async content() { await request('content'); await reconcile() },
    async assets() {
      await Promise.all([request('content'), request('assets')])
      await reconcile()
    }
  }
}

/** 只显示当前知识库的鉴权图片，MarkdownPreview 负责安全渲染和 blob 回收。 */
export function knowledgeAssetImageMarkdown(source, kbId, label = '原图') {
  const prefix = `/api/knowledge/databases/${encodeURIComponent(kbId)}/images/`
  if (typeof source !== 'string' || !source.startsWith(prefix) || /[\s()<>?#]/.test(source)) return ''
  return `![${label}](${source})`
}
