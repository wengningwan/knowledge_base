<template>
  <div class="experiment-panel">
    <div class="experiment-toolbar">
      <span>实验卡片 · 机器草稿</span>
      <a-button size="small" :loading="loading" @click="refresh">刷新</a-button>
    </div>
    <a-spin v-if="loading && !snapshot" tip="正在读取实验卡片…" />
    <a-alert v-if="error" type="error" show-icon :message="error" />
    <template v-if="snapshot">
      <a-alert :type="noticeType" show-icon :message="statusLabel" :description="snapshot.message" />
      <a-progress v-if="active" :percent="Math.round(snapshot.progress || 0)" size="small" />
      <a-button v-if="canRetry" :loading="retrying" @click="retry">重试失败任务</a-button>
      <p v-if="snapshot.status === 'draft'" class="coverage-note">
        已扫描正文并生成 {{ snapshot.cards.length }} 张卡片。机器提取可能遗漏或关联错误，请对照原文核对。
        此版本尚不参与正式问答。
      </p>
      <div v-if="snapshot.cards.length" class="experiment-layout">
        <div class="experiment-cards">
          <section v-for="(card, index) in snapshot.cards" :key="index" class="experiment-card">
            <h3>{{ card.sample?.raw_text || `实验 ${index + 1}` }}</h3>
            <p v-for="(warning, warningIndex) in card.warnings" :key="warningIndex" class="warning">{{ warning }}</p>
            <dl>
              <div v-for="(row, rowIndex) in cardRows(card)" :key="rowIndex" class="experiment-row">
                <dt>{{ row.label }}</dt>
                <dd>
                  <span>{{ row.field?.raw_text || '未提取到明确值' }}</span>
                  <button v-if="row.evidenceIds?.length" type="button" @click="showSources(row.evidenceIds)">查看出处</button>
                </dd>
              </div>
            </dl>
          </section>
        </div>
        <aside class="experiment-sources" aria-label="字段原文出处" aria-live="polite">
          <h3>原文出处</h3>
          <p v-if="!selectedSources.length">点击字段旁的“查看出处”，核对对应段落或表格。</p>
          <section v-for="source in selectedSources" :key="source.evidence_id" class="source-quote">
            <p>Markdown 第 {{ source.line_start }}–{{ source.line_end }} 行<span v-if="source.page"> · PDF 第 {{ source.page }} 页</span></p>
            <MarkdownPreview :content="source.quote" />
          </section>
        </aside>
      </div>
      <a-empty v-else-if="snapshot.status === 'draft'" description="尚未形成可关联的实验卡片，请检查下方信息与原文。" />
      <details v-if="unassigned.length" class="unassigned">
        <summary>{{ unassigned.length }} 条信息尚未归入卡片</summary>
        <ul>
          <li v-for="(fact, index) in unassigned" :key="index">
            {{ fact.raw_text }}
            <button v-if="fact.evidence_ids?.length" type="button" @click="showSources(fact.evidence_ids)">查看出处</button>
          </li>
        </ul>
        <template v-if="!snapshot.cards.length">
          <section v-for="source in selectedSources" :key="source.evidence_id" class="source-quote">
            <p>Markdown 第 {{ source.line_start }}–{{ source.line_end }} 行</p>
            <MarkdownPreview :content="source.quote" />
          </section>
        </template>
      </details>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { documentApi } from '@/apis/knowledge_api'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'

const props = defineProps({
  kbId: { type: [String, Number], required: true },
  fileId: { type: [String, Number], required: true },
  canManage: { type: Boolean, default: false }
})
const snapshot = ref(null)
const loading = ref(false)
const retrying = ref(false)
const error = ref('')
const selectedSources = ref([])
let sequence = 0
let retrySequence = 0
let timer = null
let disposed = false

const active = computed(() => ['pending', 'running'].includes(snapshot.value?.status))
const canRetry = computed(() => props.canManage && ['failed', 'cancelled'].includes(snapshot.value?.status))
const unassigned = computed(() => snapshot.value?.coverage?.unassigned_facts || [])
const statusLabel = computed(() => ({
  not_generated: '尚未生成实验卡片', pending: '原文已入库，等待生成实验卡片',
  running: '原文已入库，正在生成实验卡片', draft: '实验草稿已生成，待人工核对',
  failed: '原文已入库，实验卡片生成失败', cancelled: '实验卡片生成已取消', stale: '实验草稿对应旧正文'
}[snapshot.value?.status] || '实验卡片状态未知'))
const noticeType = computed(() => ['failed', 'cancelled', 'stale'].includes(snapshot.value?.status) ? 'warning' : 'info')

/** 分别展示每一步和每一组测量，保留各自的条件归属。 */
const cardRows = (card) => {
  const rows = [{ label: '样品', field: card.sample, evidenceIds: card.sample?.evidence_ids }]
  for (const [index, step] of (card.preparation_steps || []).entries()) {
    rows.push({ label: `步骤 ${index + 1}`, field: step.operation, evidenceIds: step.operation?.evidence_ids })
    for (const condition of step.conditions || []) {
      rows.push({ label: `步骤 ${index + 1} · ${condition.name}`, field: condition.value, evidenceIds: condition.value?.evidence_ids })
    }
    rows.push({ label: `步骤 ${index + 1} · 样品归属`, field: { raw_text: '查看样品与该步骤的对应原文' }, evidenceIds: step.assignment_evidence_ids })
  }
  for (const [index, measurement] of (card.measurements || []).entries()) {
    for (const [key, label] of [['property', '指标'], ['method', '方法'], ['result', '结果']]) {
      rows.push({ label: `测量 ${index + 1} · ${label}`, field: measurement[key], evidenceIds: measurement[key]?.evidence_ids })
    }
    for (const condition of measurement.conditions || []) {
      rows.push({ label: `测量 ${index + 1} · ${condition.name}`, field: condition.value, evidenceIds: condition.value?.evidence_ids })
    }
    rows.push({ label: `测量 ${index + 1} · 样品归属`, field: { raw_text: '查看样品与该结果的对应原文' }, evidenceIds: measurement.assignment_evidence_ids })
  }
  return rows
}

/** 只展示当前接口快照中明确返回的证据。 */
const showSources = (ids) => {
  selectedSources.value = (snapshot.value?.evidence || []).filter((source) => ids.includes(source.evidence_id))
}

/** 每次刷新废弃旧请求；轮询只在页签存活且任务进行中时继续。 */
const refresh = async () => {
  clearTimeout(timer)
  const request = ++sequence
  const kbId = String(props.kbId)
  const fileId = String(props.fileId)
  loading.value = true
  error.value = ''
  try {
    const result = await documentApi.getExperimentCards(kbId, fileId)
    if (disposed || request !== sequence) return
    snapshot.value = { ...result, cards: result.cards || [], evidence: result.evidence || [] }
    selectedSources.value = []
    if (['pending', 'running'].includes(result.status)) timer = setTimeout(refresh, 3000)
  } catch (cause) {
    if (!disposed && request === sequence) {
      snapshot.value = null
      selectedSources.value = []
      error.value = cause.message || '实验卡片读取失败，请刷新重试。'
    }
  } finally {
    if (!disposed && request === sequence) loading.value = false
  }
}

/** 重试须携带用户当前看到的版本，服务端再次校验权限与正文。 */
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
    if (!disposed && retryRequest === retrySequence) error.value = cause.message || '重试失败，请刷新文档状态。'
  } finally {
    if (!disposed && retryRequest === retrySequence) retrying.value = false
  }
}

watch(() => [props.kbId, props.fileId], () => {
  retrySequence += 1
  snapshot.value = null
  selectedSources.value = []
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
.experiment-panel { height: 100%; overflow: auto; padding: 16px; color: var(--color-text); }
.experiment-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; font-weight: 600; }
.experiment-panel > :deep(.ant-alert) { margin-bottom: 12px; }
.coverage-note, .experiment-sources > p { color: var(--color-text-secondary); }
.experiment-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.experiment-card, .experiment-sources { padding: 14px; background: var(--gray-0); border: 1px solid var(--gray-150); border-radius: 8px; min-width: 0; }
.experiment-card + .experiment-card { margin-top: 12px; }
h3 { margin: 0 0 12px; font-size: 15px; }
dl { margin: 0; }
.experiment-row { padding: 8px 0; border-top: 1px solid var(--gray-100); }
dt { font-size: 12px; color: var(--color-text-secondary); }
dd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
button { color: var(--main-color); background: none; border: 0; padding: 2px 0; cursor: pointer; font: inherit; }
button:focus-visible { outline: 2px solid var(--main-color); outline-offset: 3px; }
.source-quote { overflow-wrap: anywhere; border-top: 1px solid var(--gray-100); }
.source-quote > p { font-size: 12px; color: var(--color-text-secondary); }
.warning { color: var(--color-warning-700); white-space: pre-wrap; }
.unassigned { margin-top: 16px; }
.unassigned summary { cursor: pointer; }
@media (max-width: 900px) { .experiment-layout { grid-template-columns: 1fr; } }
</style>
