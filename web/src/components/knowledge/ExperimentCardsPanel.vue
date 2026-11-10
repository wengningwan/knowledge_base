<template>
  <div class="experiment-panel" :class="{ 'is-draft': snapshot?.status === 'draft' }">
    <div class="experiment-toolbar">
      <span>实验卡片</span>
      <a-button v-if="snapshot?.canonical_available" size="small" :loading="downloading" @click="downloadJson">
        下载结构化 JSON
      </a-button>
    </div>
    <a-spin v-if="loading && !snapshot" tip="正在读取实验卡片…" />
    <a-alert v-if="error" type="error" show-icon :message="error" />
    <template v-if="snapshot">
      <a-alert
        v-if="snapshot.status === 'draft' && !snapshot.canonical_available && snapshot.coverage?.partial_result"
        type="warning"
        show-icon
        message="部分完成"
        :description="snapshot.message"
      />
      <a-alert
        v-else-if="!snapshot.canonical_available && snapshot.status === 'draft' && !snapshot.review?.records?.length"
        type="info"
        show-icon
        :message="snapshot.message"
      />
      <template v-if="snapshot.status === 'draft' && snapshot.canonical_available">
        <a-alert v-if="canonicalError" type="error" show-icon :message="canonicalError">
          <template #action><a-button size="small" :loading="loading" @click="refresh">重新读取</a-button></template>
        </a-alert>
        <a-spin v-else-if="!canonical" tip="正在读取完整结构化数据…" />
        <CanonicalExperimentWorkspace
          v-else
          :key="snapshot.revision_id"
          :paper="canonical"
          :snapshot="snapshot"
          :can-manage="canManage"
          :save-review="saveReview"
        />
      </template>
      <ExperimentReviewWorkspace
        v-else-if="snapshot.status === 'draft'"
        :key="snapshot.revision_id"
        :snapshot="snapshot"
        :can-manage="canManage"
        :save-review="saveReview"
      />
      <template v-else>
        <a-alert :type="noticeType" show-icon :message="statusLabel" :description="snapshot.message" />
        <a-progress v-if="active" :percent="Math.round(snapshot.progress || 0)" size="small" />
        <div v-if="canRetry">
          <a-button :loading="retrying" @click="retry">重试失败任务</a-button>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { documentApi } from '@/apis/knowledge_api'
import ExperimentReviewWorkspace from './ExperimentReviewWorkspace.vue'
import CanonicalExperimentWorkspace from './CanonicalExperimentWorkspace.vue'

const props = defineProps({
  kbId: { type: [String, Number], required: true },
  fileId: { type: [String, Number], required: true },
  canManage: { type: Boolean, default: false }
})
const snapshot = ref(null)
const loading = ref(false)
const retrying = ref(false)
const downloading = ref(false)
const error = ref('')
const canonical = ref(null)
const canonicalError = ref('')
let sequence = 0
let retrySequence = 0
let timer = null
let disposed = false

const active = computed(() => ['pending', 'running'].includes(snapshot.value?.status))
const canRetry = computed(() => props.canManage && ['failed', 'cancelled'].includes(snapshot.value?.status))
const statusLabel = computed(() => ({
  not_generated: '尚未生成实验卡片', pending: '原文已入库，等待生成实验卡片',
  running: '原文已入库，正在生成实验卡片',
  failed: '原文已入库，实验卡片生成失败', cancelled: '实验卡片生成已取消', stale: '实验草稿对应旧正文'
}[snapshot.value?.status] || '实验卡片状态未知'))
const noticeType = computed(() => ['failed', 'cancelled', 'stale'].includes(snapshot.value?.status) ? 'warning' : 'info')

/** 每次读取废弃旧请求；轮询只在任务进行中继续。 */
const refresh = async () => {
  clearTimeout(timer)
  const request = ++sequence
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  loading.value = true
  error.value = ''
  canonicalError.value = ''
  try {
    const result = await documentApi.getExperimentCards(kbId, fileId)
    if (disposed || request !== sequence) return
    canonical.value = null
    snapshot.value = result
    if (['pending', 'running'].includes(result.status)) timer = setTimeout(refresh, 3000)
    if (result.status === 'draft' && result.canonical_available) {
      try {
        const record = await documentApi.getExperimentJson(kbId, fileId, result.revision_id)
        if (!disposed && request === sequence) canonical.value = record
      } catch (cause) {
        if (!disposed && request === sequence) canonicalError.value = cause.message || '完整结构化数据读取失败，请重新读取。'
      }
    }
  } catch (cause) {
    if (!disposed && request === sequence) {
      snapshot.value = null
      error.value = cause.message || '实验卡片读取失败，请重新打开卡片后再试。'
    }
  } finally {
    if (!disposed && request === sequence) loading.value = false
  }
}

/** 用服务端提交的记录替换本页投影，旧页签响应不能覆盖新文档。 */
const saveReview = async (change) => {
  const current = snapshot.value
  const result = await documentApi.saveExperimentReview(String(props.kbId), String(props.fileId), {
    ...change,
    revision_id: current.revision_id,
    source_sha256: current.source_sha256,
    version: current.review?.version || 0
  })
  if (disposed || snapshot.value !== current) return null
  const records = [...(current.review?.records || [])]
  const index = records.findIndex((record) => record.record_id === result.record.record_id)
  if (index === -1) records.push(result.record)
  else records[index] = result.record
  snapshot.value = { ...current, review: { version: result.version, records } }
  return result.record
}

/** 重试携带当前正文版本，服务端再次校验权限。 */
const retry = async () => {
  const retryRequest = ++retrySequence
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  const current = snapshot.value
  retrying.value = true
  try {
    await documentApi.retryExperimentCards(kbId, fileId, {
      revision_id: current.revision_id, source_sha256: current.source_sha256
    })
    if (!disposed && retryRequest === retrySequence) {
      retrying.value = false
      await refresh()
    }
  } catch (cause) {
    if (!disposed && retryRequest === retrySequence) error.value = cause.message || '重试失败，请重新打开卡片后再试。'
  } finally {
    if (!disposed && retryRequest === retrySequence) retrying.value = false
  }
}

/** 下载与当前页面同版本的规范 JSON。 */
const downloadJson = async () => {
  const revisionId = snapshot.value.revision_id
  downloading.value = true
  error.value = ''
  try {
    const record = await documentApi.getExperimentJson(String(props.kbId), String(props.fileId), revisionId)
    if (disposed || snapshot.value?.revision_id !== revisionId) return
    const blob = new Blob([JSON.stringify(record, null, 2) + '\n'], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `adsorption-${props.fileId}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (cause) {
    if (!disposed && snapshot.value?.revision_id === revisionId) error.value = cause.message || '结构化 JSON 下载失败。'
  } finally {
    if (!disposed && snapshot.value?.revision_id === revisionId) downloading.value = false
  }
}

watch(() => [props.kbId, props.fileId], () => {
  retrySequence += 1
  snapshot.value = null
  canonical.value = null
  canonicalError.value = ''
  retrying.value = false
  downloading.value = false
  refresh()
}, { immediate: true })

onBeforeUnmount(() => {
  disposed = true
  sequence += 1
  retrySequence += 1
  clearTimeout(timer)
})
</script>

<style scoped lang="less">
.experiment-panel { display: flex; flex: 1; flex-direction: column; min-height: 0; overflow: auto; padding: 16px; color: var(--color-text); }
.experiment-panel.is-draft { overflow: hidden; }
.experiment-toolbar { display: flex; flex-shrink: 0; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; font-weight: 600; }
.experiment-panel > :deep(.ant-alert) { margin-bottom: 12px; }
</style>
