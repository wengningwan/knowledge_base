<template>
  <div class="experiment-panel" :class="{ 'is-draft': snapshot?.status === 'draft' }">
    <div class="experiment-toolbar">
      <span>实验卡片</span>
      <a-button v-if="canRetry" size="small" :loading="retrying" @click="confirmRetry">重新提取</a-button>
    </div>
    <a-spin v-if="(loading || retrying) && !snapshot" :tip="retrying ? '正在提交重新提取…' : '正在读取实验卡片…'" />
    <a-alert v-if="error" type="error" show-icon :message="error"><template #action><a-button size="small" :loading="loading" @click="refresh">重新读取</a-button></template></a-alert>
    <template v-if="snapshot">
      <a-alert
        v-if="!snapshot.canonical_available && snapshot.status === 'draft' && !snapshot.review?.records?.length"
        type="info"
        show-icon
        :message="snapshot.message"
      />
      <template v-if="snapshot.status === 'draft' && snapshot.canonical_available">
        <a-alert v-if="canonicalError" type="error" show-icon :message="canonicalError">
          <template #action><a-button size="small" :loading="loading" @click="refresh">重新读取</a-button></template>
        </a-alert>
        <CanonicalExperimentWorkspace
          :key="snapshot.revision_id"
          :paper="canonical || emptyPaper"
          :effective-available="Boolean(canonical)"
          :effective-json-available="snapshot.effective_available !== false"
          :snapshot="snapshot"
          :can-manage="canManage"
          :save-review="saveReview"
          :load-machine="loadMachine"
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
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Modal } from 'ant-design-vue'
import { documentApi } from '@/apis/knowledge_api'
import ExperimentReviewWorkspace from './ExperimentReviewWorkspace.vue'
import CanonicalExperimentWorkspace from './CanonicalExperimentWorkspace.vue'

const props = defineProps({
  kbId: { type: [String, Number], required: true },
  fileId: { type: [String, Number], required: true },
  canManage: { type: Boolean, default: false }
})
const emit = defineEmits(['snapshot', 'invalidate'])
const snapshot = ref(null)
const emptyPaper = { sample_states: [], sample_batches: [], synthesis_routes: [], experiment_runs: [], observations: [], series_sets: [], evidence: [] }
const loading = ref(false)
const retrying = ref(false)
const savingReview = ref(false)
const error = ref('')
const canonical = ref(null)
const canonicalError = ref('')
let sequence = 0
let retrySequence = 0
let timer = null
let disposed = false

const activeStatuses = ['indexing', 'pending', 'running']
const active = computed(() => activeStatuses.includes(snapshot.value?.status))
const canRetry = computed(() => props.canManage && ['draft', 'failed', 'cancelled'].includes(snapshot.value?.status) && !retrying.value)
const statusLabel = computed(() => ({
  not_generated: '尚未生成实验卡片', indexing: '正在重新入库，等待生成实验卡片', error_indexing: '重新入库失败，未生成实验卡片', pending: '原文已入库，等待生成实验卡片',
  running: '原文已入库，正在生成实验卡片',
  failed: '原文已入库，实验卡片生成失败', cancelled: '实验卡片生成已取消', stale: '实验草稿对应旧正文'
}[snapshot.value?.status] || '实验卡片状态未知'))
const noticeType = computed(() => ['failed', 'cancelled', 'stale', 'error_indexing'].includes(snapshot.value?.status) ? 'warning' : 'info')

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
    canonicalError.value = result.effective_error || ''
    if (activeStatuses.includes(result.status)) timer = setTimeout(refresh, 3000)
    else if (result.status === 'draft') timer = setTimeout(checkCurrentRevision, 10000)
    canonical.value = result.current_result || null
    snapshot.value = result
    emit('snapshot', result)
    return true
  } catch (cause) {
    if (!disposed && request === sequence) {
      snapshot.value = null
      canonical.value = null
      emit('snapshot', null)
      error.value = cause.message || '实验卡片读取失败，请重新打开卡片后再试。'
    }
    return false
  } finally {
    if (!disposed && request === sequence) loading.value = false
  }
}

/** 后台只检查本次结果是否被清理，不覆盖仍在编辑的同版字段。 */
const checkCurrentRevision = async () => {
  if (disposed) return
  if (savingReview.value || retrying.value) {
    timer = setTimeout(checkCurrentRevision, 10000)
    return
  }
  const request = sequence
  const current = snapshot.value
  try {
    const result = await documentApi.getExperimentCards(String(props.kbId), String(props.fileId))
    if (disposed || request !== sequence || current !== snapshot.value) return
    if (result.revision_id !== current?.revision_id || result.status !== current?.status ||
        result.source_sha256 !== current?.source_sha256) {
      canonical.value = null
      snapshot.value = null
      emit('invalidate', current)
      await refresh()
      return
    }
  } catch {
    // 短暂断线保留正在编辑的本地输入；保存与下载仍由服务端版本检查裁决。
  }
  if (!disposed && request === sequence) timer = setTimeout(checkCurrentRevision, 10000)
}

/** 保存后统一重读服务端投影与当前结果，不在浏览器合并人工记录。 */
const saveReview = async (change) => {
  const current = snapshot.value
  if (!current || savingReview.value) return null
  savingReview.value = true
  try {
    const result = await documentApi.saveExperimentReview(String(props.kbId), String(props.fileId), {
      ...change,
      revision_id: current.revision_id,
      source_sha256: current.source_sha256,
      version: current.review?.version || 0
    })
    if (disposed || snapshot.value !== current) return null
    const refreshed = await refresh()
    if (!refreshed) throw new Error('修改已保存，但当前结果读取失败，请重新读取。')
    return change.task_id || change.entity_change ? result : result.record
  } finally {
    savingReview.value = false
  }
}

/** 单独读取机器原稿，快照变化后不再交给旧页签。 */
const loadMachine = async () => {
  const current = snapshot.value
  const request = sequence
  const result = await documentApi.getExperimentJson(String(props.kbId), String(props.fileId), current.revision_id, {
    view: 'machine', sourceSha256: current.source_sha256, expectedReviewVersion: current.review?.version || 0
  })
  if (disposed || request !== sequence || snapshot.value !== current) return null
  return result
}

/** 明确提示重新提取会清理本次所有实验和人工修改。 */
const confirmRetry = () => Modal.confirm({
  title: '重新提取实验数据？',
  content: '将清除本次实验原稿、卡片、人工修改、证据和处理记录，再从整篇正文重新提取。失败或取消也不会恢复旧结果。',
  okText: '清除并重新提取', okType: 'danger', cancelText: '取消', onOk: retry
})

/** 清理请求立即废弃旧快照，迟到的读取和下载不能复活旧结果。 */
const retry = async () => {
  if (!canRetry.value) return
  const retryRequest = ++retrySequence
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  const current = snapshot.value
  sequence += 1
  clearTimeout(timer)
  snapshot.value = null
  canonical.value = null
  canonicalError.value = ''
  emit('invalidate', current)
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
    if (!disposed && retryRequest === retrySequence) {
      await refresh()
      error.value = cause.message || '重新提取失败，请重新读取状态。'
    }
  } finally {
    if (!disposed && retryRequest === retrySequence) retrying.value = false
  }
}

watch(() => [props.kbId, props.fileId], () => {
  retrySequence += 1
  snapshot.value = null
  canonical.value = null
  canonicalError.value = ''
  retrying.value = false
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
