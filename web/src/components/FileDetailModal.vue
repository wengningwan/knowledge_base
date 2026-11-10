<template>
  <a-modal
    v-model:open="visible"
    :width="['review', 'experiments'].includes(viewMode) ? '1200px' : '800px'"
    :footer="null"
    :closable="false"
    wrap-class-name="file-detail"
    @after-open-change="afterOpenChange"
    :bodyStyle="{ height: '80vh', padding: '0' }"
  >
    <template #title>
      <div class="modal-title-wrapper">
        <!-- 左侧：文件名和图标 -->
        <div class="file-title">
          <FileTypeIcon :name="file?.filename" :size="18" />
          <span class="file-name">{{ file?.filename || '文件详情' }}</span>
        </div>

        <div class="header-controls">
          <!-- 字符数/片段数显示在 segment 左边 -->
          <span v-if="viewInfoText" class="view-info">{{ viewInfoText }}</span>

          <!-- 视图模式切换 -->
          <div class="view-controls" v-if="file && viewModeOptions.length > 1">
            <a-segmented v-model:value="viewMode" :options="viewModeOptions" />
          </div>

          <!-- 下载按钮下拉菜单 -->
          <a-dropdown trigger="click" v-if="file" @open-change="loadStructureDownloadState">
            <a-button type="default" class="download-btn" title="下载" aria-label="下载">
              <Download :size="16" />
              <ChevronDown :size="14" />
            </a-button>
            <template #overlay>
              <a-menu @click="handleDownloadMenuClick">
                <a-menu-item key="original" :disabled="!file.file_id">
                  <template #icon><Download :size="16" /></template>
                  下载原文
                </a-menu-item>
                <a-menu-item
                  key="markdown"
                  :disabled="downloadingMarkdown || contentState.loading || Boolean(contentState.error) || !mergedContent"
                >
                  <template #icon><FileText :size="16" /></template>
                  下载 Markdown
                </a-menu-item>
                <a-menu-item
                  key="structured"
                  :disabled="checkingStructure || downloadingStructured || !structureRevisionId || (viewMode === 'experiments' && !experimentPanel?.canonicalRevisionId)"
                  :title="checkingStructure ? '正在读取结构化状态' : !structureRevisionId ? '当前没有可下载的结构化 JSON' : ''"
                >
                  <template #icon><FlaskConical :size="16" /></template>
                  下载结构化 JSON
                </a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>

          <!-- 自定义关闭按钮 -->
          <button class="custom-close-btn" aria-label="关闭文件详情" @click="visible = false">
            <X :size="16" />
          </button>
        </div>
      </div>
    </template>
    <div v-if="basicLoading" class="loading-container">
      <a-spin tip="正在加载文档内容..." />
    </div>
    <div v-else-if="detailError" class="empty-content">
      <p>{{ detailError }}</p>
    </div>
    <div v-else-if="file && hasAvailableView" class="file-detail-content">
      <a-alert
        v-if="viewMode !== 'source' && (file.markdown_file || file.has_parsed_markdown) && ['parsing', 'error_parsing'].includes(file.status)"
        type="warning"
        show-icon
        message="当前显示上一版解析结果"
        :description="file.error || '新一轮解析尚未成功，正文、图片解析和校对记录属于上一版。'"
        class="previous-parse-notice"
      />
      <div v-if="viewMode === 'source'" class="content-panel source-panel">
        <AgentFilePreview
          :file="sourcePreviewFile"
          :file-path="file?.filename || ''"
          :status="sourcePreview.loading ? 'loading' : ''"
          loading-message="正在加载文件内容..."
          :show-header="false"
          :show-download="false"
          :show-inline-html-controls="true"
          :full-height="true"
          :borderless="true"
          container-class="source-preview-container"
          content-class="source-preview-content"
        />
      </div>

      <!-- Markdown 模式 -->
      <div v-else-if="viewMode === 'markdown'" class="content-panel flat-md-preview">
        <div v-if="contentState.loading" class="loading-container">
          <a-spin tip="正在加载解析内容..." />
        </div>
        <MarkdownPreview
          v-else-if="mergedContent"
          :content="mergedContent"
          class="markdown-content"
        />
        <div v-else class="empty-content">
          <p>{{ contentState.error || '暂无文件内容' }}</p>
        </div>
      </div>

      <FigureAnalysisPanel
        v-else-if="viewMode === 'figures'"
        :key="`figures-${file.file_id}-${assetsState.attempt_id || ''}`"
        :state="assetsState"
        :kb-id="kbId"
      />
      <!-- Chunks 模式：使用 Grid 布局 -->
      <div v-else-if="viewMode === 'chunks'" class="chunks-panel">
        <div v-if="contentState.loading" class="loading-container">
          <a-spin tip="正在加载分块内容..." />
        </div>
        <ChunkPreviewPanel
          v-else
          :key="`${kbId}:${fileId}`"
          :chunks="contentState.lines"
          :source-content="contentState.content"
          :source-sha256="contentState.source_sha256"
          :source-error="contentState.error"
        />
        <div v-if="!contentState.loading && chunkCount === 0" class="empty-content">
          <p>{{ contentState.error || '暂无分块信息' }}</p>
        </div>
      </div>
      <ExperimentCardsPanel
        v-if="viewMode === 'experiments'"
        ref="experimentPanel"
        :key="`experiments-${file.file_id}`"
        :kb-id="kbId"
        :file-id="file.file_id"
        :can-manage="canManage"
      />
      <KeepAlive :max="1">
        <DocumentReviewPanel
          v-if="viewMode === 'review'"
          :key="`review-${file.file_id}-${assetsState.attempt_id || ''}`"
          :state="assetsState"
          :kb-id="kbId"
          :source-sha256="contentState.source_sha256"
          :has-indexes="['indexed', 'done', 'error_indexing'].includes(file.status) || chunkCount > 0"
          :save-review="canManage ? saveReview : null"
          :pdf-url="reviewPdfUrl"
        />
      </KeepAlive>
    </div>

    <div v-else-if="file" class="empty-content">
      <p>暂无文件内容</p>
    </div>
  </a-modal>
</template>

<script setup>
import { computed, h, onBeforeUnmount, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { documentApi } from '@/apis/knowledge_api'
import { getWorkspaceKnowledgeFileContent } from '@/apis/workspace_api'
import { getPreviewTypeByPath, normalizePreviewResponse } from '@/utils/file_preview'
import { parseDownloadFilename } from '@/utils/file_utils'
import { inlineKnowledgeImages } from '@/utils/markdown_export'
import { clusterDocumentReviewGroups, groupDocumentReviews } from '@/utils/documentReview'
import {
  canPreviewChunks,
  canPreviewOriginal,
  canPreviewParsed,
  canPreviewParseAssets,
  getDefaultDetailView
} from '@/utils/knowledge_file_policy'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import FigureAnalysisPanel from '@/components/knowledge/FigureAnalysisPanel.vue'
import DocumentReviewPanel from '@/components/knowledge/DocumentReviewPanel.vue'
import ExperimentCardsPanel from '@/components/knowledge/ExperimentCardsPanel.vue'
import ChunkPreviewPanel from '@/components/knowledge/ChunkPreviewPanel.vue'
import {
  createDocumentParseLoader, emptyDocumentContent, emptyDocumentAssets, documentMarkdown
} from '@/utils/document_parse_state'
import FileTypeIcon from '@/components/common/FileTypeIcon.vue'
import AgentFilePreview from '@/components/AgentFilePreview.vue'
import { Download, ChevronDown, FileSearch, FileText, Rows3, Image, ScanText, FlaskConical, X } from '@lucide/vue'

const props = defineProps({
  open: {
    type: Boolean,
    default: false
  },
  kbId: {
    type: [String, Number],
    default: ''
  },
  fileId: {
    type: [String, Number],
    default: ''
  },
  canManage: { type: Boolean, default: false }
})

const emit = defineEmits(['update:open', 'closed', 'review-saved'])

const visible = computed({
  get: () => props.open,
  set: (value) => emit('update:open', value)
})

const file = ref(null)
const basicLoading = ref(false)
const detailError = ref('')
const downloadingOriginal = ref(false)
const downloadingMarkdown = ref(false)
const downloadingStructured = ref(false)
const checkingStructure = ref(false)
const structureRevisionId = ref('')
const experimentPanel = ref(null)
const contentState = ref(emptyDocumentContent())
const assetsState = ref(emptyDocumentAssets())
const parseLoader = createDocumentParseLoader({
  loadContent: ({ kbId, fileId }) => documentApi.getDocumentContent(kbId, fileId),
  loadAssets: ({ kbId, fileId }) => documentApi.getDocumentParseAssets(kbId, fileId),
  onChange: ({ content, assets }) => {
    contentState.value = content
    assetsState.value = assets
  }
})

/** 保存完成后从后端重读正文和回执，旧文档响应不更新当前窗口。 */
const saveReview = async (changes) => {
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  const sequence = basicRequestSeq
  const result = await documentApi.confirmDocumentReview(kbId, fileId, changes)
  if (!props.open || sequence !== basicRequestSeq || kbId !== String(props.kbId) || fileId !== String(props.fileId)) return
  emit('review-saved')
  message.success(result.content_changed
    ? (result.index_invalidated ? '已同步到 Markdown，旧索引已清理，请重新入库。' : '已确认并同步到 Markdown。')
    : '已确认。')
  // 指针已发布，先废弃旧正文，不能因后续信息读取失败继续提供旧版下载。
  await parseLoader.refresh()
  if (!props.open || sequence !== basicRequestSeq) return
  try {
    const data = await documentApi.getDocumentBasicInfo(kbId, fileId)
    ensureApiSuccess(data, '加载文件状态失败')
    if (sequence === basicRequestSeq && props.open) file.value = normalizeFileMeta(data?.meta || data)
  } catch {
    if (sequence === basicRequestSeq && result.content_changed) file.value = { ...file.value, status: 'parsed', chunk_count: 0 }
  }
}
const sourcePreview = ref({
  loading: false,
  url: '',
  content: '',
  type: '',
  message: '',
  supported: true
})

let basicRequestSeq = 0
let sourceRequestSeq = 0

const revokeSourcePreviewUrl = () => {
  if (sourcePreview.value.url) {
    window.URL.revokeObjectURL(sourcePreview.value.url)
    sourcePreview.value.url = ''
  }
}

const resetSourcePreview = () => {
  sourceRequestSeq += 1
  revokeSourcePreviewUrl()
  sourcePreview.value = {
    loading: false,
    url: '',
    content: '',
    type: '',
    message: '',
    supported: true
  }
}

const resetLocalState = () => {
  basicRequestSeq += 1
  file.value = null
  basicLoading.value = false
  detailError.value = ''
  downloadingOriginal.value = false
  downloadingMarkdown.value = false
  downloadingStructured.value = false
  checkingStructure.value = false
  structureRevisionId.value = ''
  parseLoader.reset()
  resetSourcePreview()
  viewMode.value = 'markdown'
}

const normalizeFileMeta = (meta = {}) => ({
  ...meta,
  file_id: meta.file_id || String(props.fileId || ''),
  kb_id: meta.kb_id || String(props.kbId || ''),
  filename: meta.filename || meta.original_filename || String(props.fileId || ''),
  file_size: meta.file_size ?? meta.size ?? 0,
  has_original_file:
    'has_original_file' in meta
      ? Boolean(meta.has_original_file)
      : Boolean(meta.minio_url || meta.path),
  has_parsed_markdown:
    'has_parsed_markdown' in meta ? Boolean(meta.has_parsed_markdown) : Boolean(meta.markdown_file)
})

const ensureApiSuccess = (data, fallbackMessage) => {
  if (data?.status === 'failed') {
    throw new Error(data.message || fallbackMessage)
  }
}

// 视图模式
const viewMode = ref('markdown')
const hasContent = computed(
  () =>
    (contentState.value.lines && contentState.value.lines.length > 0) || contentState.value.content
)
const sourcePreviewCandidateType = computed(() => getPreviewTypeByPath(file.value?.filename || ''))
const sourcePreviewDisplayType = computed(
  () => sourcePreview.value.type || sourcePreviewCandidateType.value
)
const sourceContentLength = computed(() =>
  typeof sourcePreview.value.content === 'string' ? sourcePreview.value.content.length : 0
)
const sourcePreviewFile = computed(() => {
  if (!file.value) return null
  const isError = Boolean(
    sourcePreview.value.message &&
    !sourcePreview.value.supported &&
    !sourcePreview.value.content &&
    !sourcePreview.value.url
  )
  return {
    ...file.value,
    content: sourcePreview.value.content,
    previewType: sourcePreviewDisplayType.value,
    previewUrl: sourcePreview.value.url,
    supported: sourcePreview.value.supported,
    status: sourcePreview.value.loading
      ? 'loading'
      : isError
        ? 'error'
        : sourcePreview.value.supported === false
          ? 'unsupported'
          : 'ready',
    errorMessage: sourcePreview.value.message,
    loadingMessage: '正在加载文件内容...',
    message: sourcePreview.value.message
  }
})
const hasSourcePreview = computed(() => canPreviewOriginal(file.value))
/** 校对视图的原文页定位只复用文件详情既有的授权预览路径，不另开访问入口。 */
const reviewPdfUrl = computed(() =>
  hasSourcePreview.value && sourcePreviewCandidateType.value === 'pdf' ? sourcePreview.value.url : ''
)
const hasMarkdownPreview = computed(() => canPreviewParsed(file.value) || hasContent.value)
const hasChunkPreview = computed(() => canPreviewChunks(file.value))
const availableViewModes = computed(() => {
  const modes = []
  if (hasSourcePreview.value) modes.push('source')
  if (hasMarkdownPreview.value) modes.push('markdown')
  if (hasChunkPreview.value) modes.push('chunks')
  if (canPreviewParseAssets(file.value)) modes.push('figures', 'review')
  if (hasMarkdownPreview.value) modes.push('experiments')
  return modes
})
const hasAvailableView = computed(() => availableViewModes.value.length > 0)

const makeViewModeOption = (label, value, icon) => ({
  label: h(
    'span',
    {
      class: 'view-option-icon',
      title: label,
      'aria-label': label
    },
    [h(icon, { size: 15 })]
  ),
  value
})

const viewModeOptions = computed(() => {
  const optionMap = {
    source: makeViewModeOption('源文件', 'source', FileSearch),
    markdown: makeViewModeOption('Markdown', 'markdown', FileText),
    chunks: makeViewModeOption('Chunks', 'chunks', Rows3),
    figures: makeViewModeOption('图片解析', 'figures', Image),
    review: makeViewModeOption('校对结果', 'review', ScanText),
    experiments: makeViewModeOption('实验卡片', 'experiments', FlaskConical)
  }
  return availableViewModes.value.map((mode) => optionMap[mode])
})

const loadBasicInfo = async () => {
  const kbId = String(props.kbId || '')
  const fileId = String(props.fileId || '')
  if (!kbId || !fileId) {
    resetLocalState()
    return
  }

  const requestId = ++basicRequestSeq
  downloadingStructured.value = false
  checkingStructure.value = false
  structureRevisionId.value = ''
  file.value = null
  detailError.value = ''
  parseLoader.reset({ kbId, fileId })
  resetSourcePreview()
  viewMode.value = 'markdown'
  basicLoading.value = true

  try {
    const data = await documentApi.getDocumentBasicInfo(kbId, fileId)
    if (requestId !== basicRequestSeq) return
    ensureApiSuccess(data, '加载文件信息失败')

    const nextFile = normalizeFileMeta(data?.meta || data)
    if (nextFile.is_folder) {
      detailError.value = '文件夹不支持详情预览'
      return
    }

    file.value = nextFile
    viewMode.value = getDefaultDetailView(nextFile)
  } catch (error) {
    if (requestId !== basicRequestSeq) return
    console.error('加载文件基本信息失败:', error)
    detailError.value = error.message || '加载文件信息失败'
    message.error(detailError.value)
  } finally {
    if (requestId === basicRequestSeq) {
      basicLoading.value = false
    }
  }
}

watch(
  () => [props.open, props.kbId, props.fileId],
  ([open]) => {
    if (!open) {
      resetLocalState()
      return
    }
    loadBasicInfo()
  },
  { immediate: true }
)

watch(
  availableViewModes,
  (modes) => {
    if (modes.length > 0 && !modes.includes(viewMode.value)) {
      viewMode.value = modes[0]
    }
  },
  { immediate: true }
)

watch(
  [visible, file, viewMode],
  async ([open, currentFile, currentViewMode]) => {
    if (!open || !currentFile) return
    if (
      (currentViewMode === 'markdown' && canPreviewParsed(currentFile)) ||
      (currentViewMode === 'chunks' && canPreviewChunks(currentFile))
    ) {
      await parseLoader.content()
    }
    if (['figures', 'review'].includes(currentViewMode) && canPreviewParseAssets(currentFile)) {
      await parseLoader.assets()
    }
  },
  { immediate: true }
)

watch(
  [visible, file, viewMode],
  async ([open, currentFile, currentViewMode]) => {
    // 校对面板的原文页定位复用同一授权预览路径：PDF 文件的校对视图也需预载源预览
    const needsSourcePreview = open && currentFile && hasSourcePreview.value &&
      (currentViewMode === 'source' || (currentViewMode === 'review' && sourcePreviewCandidateType.value === 'pdf'))
    if (!needsSourcePreview) {
      if (!open || !hasSourcePreview.value) {
        resetSourcePreview()
      }
      return
    }

    await loadSourcePreview()
  },
  { immediate: true }
)

// 统计信息
const mergedContent = computed(() => documentMarkdown(contentState.value))
const charCount = computed(() => mergedContent.value.length)
const chunkCount = computed(
  () => contentState.value.lines?.length || 0
)
const viewInfoText = computed(() => {
  if (viewMode.value === 'figures') return assetsState.value.loading ? '' : `${assetsState.value.figures.length} 张图片`
  if (viewMode.value === 'review') {
    if (assetsState.value.loading) return ''
    // 统计改簇口径：重复问题归并后待确认按类计数，处数为簇内未确认总数。
    const clusters = clusterDocumentReviewGroups(groupDocumentReviews(assetsState.value.reviews))
    return `待确认 ${clusters.length} 类 · 共 ${clusters.reduce((total, cluster) => total + cluster.count, 0)} 处`
  }
  if (viewMode.value === 'chunks') {
    if (contentState.value.loading) return ''
    return `${chunkCount.value} 个片段`
  }
  if (viewMode.value === 'source') {
    if (sourcePreview.value.loading) return ''
    if (sourceContentLength.value > 0) return `${formatTextLength(sourceContentLength.value)} 字符`
    if (sourcePreview.value.url) return '源文件预览'
    return ''
  }
  if (contentState.value.loading) return ''
  return `${formatTextLength(charCount.value)} 字符`
})

// 格式化文本长度
function formatTextLength(length) {
  if (!length && length !== 0) return '0 字符'

  if (length < 1000) {
    return `${length}`
  } else {
    return `${(length / 1000).toFixed(1)}k`
  }
}

const afterOpenChange = (open) => {
  if (!open) {
    resetLocalState()
    emit('closed')
  }
}

const loadSourcePreview = async () => {
  if (!file.value?.file_id || !props.kbId || !props.fileId || !hasSourcePreview.value) return
  if (sourcePreview.value.url || sourcePreview.value.content || sourcePreview.value.message) return

  const requestId = ++sourceRequestSeq
  sourcePreview.value.loading = true
  try {
    const response = await getWorkspaceKnowledgeFileContent(props.kbId, props.fileId)
    const preview = await normalizePreviewResponse(response)
    if (requestId !== sourceRequestSeq) {
      if (preview.previewUrl) {
        window.URL.revokeObjectURL(preview.previewUrl)
      }
      return
    }
    revokeSourcePreviewUrl()
    sourcePreview.value.type = preview.previewType || sourcePreviewCandidateType.value
    sourcePreview.value.message = preview.message || ''
    sourcePreview.value.supported = preview.supported !== false
    sourcePreview.value.url = preview.previewUrl || ''
    sourcePreview.value.content = preview.content || ''
  } catch (error) {
    if (requestId !== sourceRequestSeq) return
    console.error('加载源文件预览失败:', error)
    sourcePreview.value.message = error.message || '加载源文件预览失败'
    sourcePreview.value.supported = false
    message.error(sourcePreview.value.message)
  } finally {
    if (requestId === sourceRequestSeq) {
      sourcePreview.value.loading = false
    }
  }
}

/** 打开下载菜单时读取当前文件的可下载结构化版本。 */
const loadStructureDownloadState = async (open) => {
  if (!open || !file.value || checkingStructure.value || downloadingStructured.value) return
  const requestId = basicRequestSeq
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  checkingStructure.value = true
  structureRevisionId.value = ''
  try {
    const snapshot = await documentApi.getExperimentCards(kbId, fileId)
    if (requestId !== basicRequestSeq || !props.open || kbId !== String(props.kbId) || fileId !== String(props.fileId)) return
    if (snapshot.status === 'draft' && snapshot.canonical_available) structureRevisionId.value = snapshot.revision_id
  } catch (error) {
    if (requestId === basicRequestSeq && props.open) message.error(error.message || '结构化下载状态读取失败，请重新打开下载菜单。')
  } finally {
    if (requestId === basicRequestSeq) checkingStructure.value = false
  }
}

// 下载菜单点击处理
const handleDownloadMenuClick = ({ key }) => {
  if (key === 'original') {
    handleDownloadOriginal()
  } else if (key === 'markdown') {
    handleDownloadMarkdown()
  } else if (key === 'structured') {
    handleDownloadStructured()
  }
}

/** 下载由服务端校验正文和版本的完整机器 JSON。 */
const handleDownloadStructured = async () => {
  if (downloadingStructured.value || checkingStructure.value || !structureRevisionId.value) return
  const requestId = basicRequestSeq
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  const revisionId = viewMode.value === 'experiments' ? experimentPanel.value?.canonicalRevisionId : structureRevisionId.value
  if (!revisionId) return
  downloadingStructured.value = true
  try {
    const record = await documentApi.getExperimentJson(kbId, fileId, revisionId)
    if (requestId !== basicRequestSeq || !props.open || kbId !== String(props.kbId) || fileId !== String(props.fileId)) return
    const blob = new Blob([JSON.stringify(record, null, 2) + '\n'], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'adsorption-' + fileId + '.json'
    link.click()
    setTimeout(() => window.URL.revokeObjectURL(url), 1000)
    message.success('下载成功')
  } catch (error) {
    if (requestId !== basicRequestSeq || !props.open) return
    structureRevisionId.value = ''
    message.error(error.message || '结构化 JSON 下载失败，请重新打开下载菜单后再试。')
  } finally {
    if (requestId === basicRequestSeq) downloadingStructured.value = false
  }
}

// 下载原文
const handleDownloadOriginal = async () => {
  if (!file.value || !props.kbId || !props.fileId) {
    message.error('文件信息不完整')
    return
  }

  const requestId = basicRequestSeq
  downloadingOriginal.value = true
  try {
    const response = await documentApi.downloadDocument(props.kbId, props.fileId)

    const contentDisposition = response.headers.get('content-disposition')
    const filename = parseDownloadFilename(contentDisposition) || file.value.filename
    // 创建blob并下载
    const blob = await response.blob()
    if (requestId !== basicRequestSeq || !props.open) return
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.style.display = 'none'
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
    message.success('下载成功')
  } catch (error) {
    if (requestId !== basicRequestSeq) return
    console.error('下载文件时出错:', error)
    message.error(error.message || '下载文件失败')
  } finally {
    if (requestId === basicRequestSeq) downloadingOriginal.value = false
  }
}

// 下载 Markdown
const handleDownloadMarkdown = async () => {
  if (downloadingMarkdown.value) return
  const content = mergedContent.value

  if (!content || contentState.value.error) {
    message.error(contentState.value.error || '没有可下载的 Markdown 内容')
    return
  }

  const requestId = basicRequestSeq
  downloadingMarkdown.value = true
  try {
    // 生成文件名（如果原文件没有 .md 扩展名，则添加）
    let filename = file.value.filename || 'document.md'
    if (!filename.toLowerCase().endsWith('.md')) {
      // 移除原扩展名，添加 .md
      const lastDotIndex = filename.lastIndexOf('.')
      if (lastDotIndex > 0) {
        filename = filename.substring(0, lastDotIndex) + '.md'
      } else {
        filename = filename + '.md'
      }
    }

    // 创建 blob 并下载
    const portableContent = await inlineKnowledgeImages(content, documentApi.getImageBlob, props.kbId)
    if (requestId !== basicRequestSeq || !props.open) return
    const blob = new Blob([portableContent], { type: 'text/markdown;charset=utf-8' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.style.display = 'none'
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
    message.success('下载成功')
  } catch (error) {
    if (requestId !== basicRequestSeq) return
    console.error('下载 Markdown 时出错:', error)
    message.error(error.message || '下载 Markdown 失败')
  } finally {
    if (requestId === basicRequestSeq) downloadingMarkdown.value = false
  }
}

onBeforeUnmount(resetLocalState)
</script>

<style scoped>
.previous-parse-notice { flex-shrink: 0; margin: 0 0 12px; }

.file-detail-content {
  height: 100%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.content-panel,
.chunks-panel {
  flex: 1;
  overflow-y: auto;
  padding: 0;
  min-height: 0;
}

.source-panel {
  overflow: hidden;
}

:deep(.source-preview-container) {
  height: 100%;
  max-height: none;
}

:deep(.source-preview-content) {
  flex: 1 1 auto;
  max-height: none;
  min-height: 0;
}

:deep(.source-preview-content .html-preview),
:deep(.source-preview-content .pdf-preview) {
  display: block;
  height: 100%;
  min-height: 100%;
}

.markdown-content {
  min-height: 100%;
}

.loading-container {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 200px;
}

.empty-content {
  text-align: center;
  padding: 40px 0;
  color: var(--gray-400);
  width: 100%;
}

</style>

<style lang="less">
.file-detail {
  .ant-modal {
    top: 20px;
  }

  .ant-modal-header {
    .ant-modal-title {
      width: 100%;
    }
  }
}

.modal-title-wrapper {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-width: 0;
}

/* 文件标题样式 */
.file-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1 1 auto;
  min-width: 0;

  svg {
    flex: 0 0 auto;
  }
}

.file-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  font-size: 15px;
  color: var(--gray-900);
}

.title-info {
  font-size: 13px;
  color: var(--gray-600);
  font-weight: 500;
}

.header-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  margin-left: auto;
  min-width: 0;
}

@media (max-width: 600px) {
  .modal-title-wrapper { flex-wrap: wrap; }
  .file-title { flex-basis: 100%; }
  .header-controls { flex-wrap: wrap; }
}

/* 下载按钮样式 */
.download-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: auto;
  min-width: 48px;
  padding: 0 10px;
  height: 28px;
  line-height: 1;
  border-radius: 6px;
  gap: 4px;

  svg {
    flex: 0 0 auto;
    vertical-align: middle;
  }
}

/* 自定义关闭按钮 */
.custom-close-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  color: var(--gray-500);
  transition: all 0.2s;

  &:hover {
    background: var(--gray-100);
    color: var(--gray-700);
  }
}

/* 视图切换控件 */
.view-controls {
  display: flex;
  align-items: center;
  flex: 0 0 auto;

  .ant-segmented {
    padding: 2px;
  }

  .ant-segmented-item {
    min-width: 30px;
  }

  .ant-segmented-item-label {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 24px;
    min-height: 24px;
    padding: 0 7px;
    line-height: 24px;
  }
}

.view-option-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
}

.view-info {
  flex: 0 0 auto;
  font-size: 12px;
  color: var(--gray-500);
  white-space: nowrap;
}

/* 下拉菜单样式 */
.ant-dropdown-menu {
  border-radius: 8px;
  padding: 4px;
}

.ant-dropdown-menu-item {
  border-radius: 6px;
  display: flex;
  align-items: center;
  padding: 8px 12px;

  // svg {
  //   margin-right: 8px;
  // }
}
</style>
