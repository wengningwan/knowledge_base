<template>
  <div class="experiment-panel" :class="{ 'is-draft': snapshot?.status === 'draft' }">
    <div class="experiment-toolbar">
      <span>实验卡片</span>
      <a-button size="small" :loading="loading" @click="refresh">刷新</a-button>
    </div>
    <a-spin v-if="loading && !snapshot" tip="正在读取实验卡片…" />
    <a-alert v-if="error" type="error" show-icon :message="error" />
    <template v-if="snapshot">
      <template v-if="snapshot.status === 'draft'">
        <section class="experiment-summary" aria-label="实验卡片生成结果">
          <div class="summary-heading">
            <strong v-if="snapshot.cards.length">{{ snapshot.cards.length }} 张样品卡 · {{ recordCount }} 条实验记录</strong>
            <strong v-else>本次未形成实验卡片</strong>
            <span>机器草稿，暂不参与正式问答</span>
          </div>
          <p v-if="draftDetail">{{ draftDetail }}</p>
        </section>
        <div class="experiment-layout" :class="{ 'has-selection': selection }">
          <div class="experiment-cards" role="region" aria-label="样品卡片与待确认取值" tabindex="0">
            <section v-for="(card, index) in snapshot.cards" :key="index" class="experiment-card">
              <h3>{{ card.sample?.raw_text || `实验 ${index + 1}` }}</h3>
              <p v-for="(warning, warningIndex) in card.warnings" :key="warningIndex" class="warning">{{ warning }}</p>
              <dl>
                <div
                  v-for="(row, rowIndex) in cardRows(card)"
                  :key="rowIndex"
                  class="experiment-row"
                  :class="{ 'is-selected': selection?.key === `card-${index}-${rowIndex}` }"
                >
                  <dt>{{ row.label }}</dt>
                  <dd>
                    <span>{{ row.field?.raw_text || '未提取到明确值' }}</span>
                    <button
                      v-if="row.evidenceIds?.length"
                      type="button"
                      :aria-pressed="selection?.key === `card-${index}-${rowIndex}`"
                      @click="showSources(`card-${index}-${rowIndex}`, row.evidenceIds, row.label, row.field?.raw_text, card.sample?.raw_text)"
                    >
                      查看出处
                    </button>
                  </dd>
                </div>
              </dl>
            </section>
            <a-empty
              v-if="!snapshot.cards.length"
              :description="unassigned.length ? '可在下方查看候选值和原文出处。' : '当前草稿没有可查看的实验记录。'"
            />
            <details v-if="unassigned.length" class="unassigned">
              <summary>{{ unassigned.length }} 个待确认的条件或结果</summary>
              <ul>
                <li
                  v-for="(fact, index) in unassigned"
                  :key="fact.fact_id || index"
                  :class="{ 'is-selected': selection?.key === `candidate-${index}` }"
                >
                  {{ fact.raw_text }}
                  <button
                    v-if="fact.evidence_ids?.length"
                    type="button"
                    :aria-pressed="selection?.key === `candidate-${index}`"
                    @click="showSources(`candidate-${index}`, fact.evidence_ids, fact.kind === 'condition' ? '待确认条件' : '待确认结果', fact.raw_text)"
                  >
                    查看出处
                  </button>
                </li>
              </ul>
            </details>
          </div>
          <aside class="experiment-sources" aria-label="字段原文出处">
            <header class="source-header">
              <div class="source-heading">
                <h3>原文出处</h3>
                <button v-if="selection" type="button" @click="selection = null">关闭出处</button>
              </div>
              <div v-if="selection" class="source-selection" aria-live="polite">
                <span :title="selectionTitle">{{ selectionTitle }}</span>
                <strong v-if="selection.value" :title="selection.value">{{ selection.value }}</strong>
              </div>
            </header>
            <div ref="sourceScroll" class="source-scroll" role="region" tabindex="0" aria-label="原文内容">
              <p v-if="!selectedSources.length" class="source-empty">
                {{ selection ? '该项没有可显示的原文出处。' : '点击卡片字段旁的“查看出处”，在这里对照原文。' }}
              </p>
              <section v-for="source in selectedSources" :key="source.evidence_id" class="source-quote">
                <p>
                  Markdown 第 {{ source.line_start === source.line_end ? source.line_start : `${source.line_start}–${source.line_end}` }} 行
                  <span v-if="source.page"> · PDF 第 {{ source.page }} 页</span>
                </p>
                <MarkdownPreview :content="source.quote" compact />
              </section>
            </div>
          </aside>
        </div>
      </template>
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
const selection = ref(null)
const sourceScroll = ref(null)
let sequence = 0
let retrySequence = 0
let timer = null
let disposed = false

const active = computed(() => ['pending', 'running'].includes(snapshot.value?.status))
const canRetry = computed(() => props.canManage && ['failed', 'cancelled'].includes(snapshot.value?.status))
const unassigned = computed(() => (snapshot.value?.coverage?.unassigned_facts || [])
  .filter((fact) => ['condition', 'result'].includes(fact.kind)))
const recordCount = computed(() => (snapshot.value?.cards || [])
  .reduce((total, card) => total + (card.preparation_steps?.length || 0) + (card.measurements?.length || 0), 0))
const draftDetail = computed(() => {
  const coverage = snapshot.value?.coverage
  const details = []
  if (unassigned.value.length) details.push(`${unassigned.value.length} 个条件或结果待确认`)
  if (coverage?.rejected_relation_count) details.push(`${coverage.rejected_relation_count} 条记录未通过取值或归属校验`)
  if (coverage?.incomplete_batches?.length) details.push(`${coverage.incomplete_batches.length} 批提取未完成`)
  if (!details.length && coverage?.source_coverage === 'partial') details.push('部分字段未通过自动校验')
  return details.join(' · ')
})
const selectedSources = computed(() => (snapshot.value?.evidence || [])
  .filter((source) => selection.value?.ids.includes(source.evidence_id)))
const selectionTitle = computed(() => [selection.value?.sample, selection.value?.label].filter(Boolean).join(' · '))
const statusLabel = computed(() => ({
  not_generated: '尚未生成实验卡片', pending: '原文已入库，等待生成实验卡片',
  running: '原文已入库，正在生成实验卡片',
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
      if (key === 'method' && !measurement.method) continue
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
const showSources = (key, ids, label, value, sample) => {
  selection.value = { key, ids, label, value, sample }
}

watch(selection, () => {
  if (sourceScroll.value) sourceScroll.value.scrollTop = 0
}, { flush: 'post' })

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
    if (snapshot.value?.revision_id !== result.revision_id) selection.value = null
    snapshot.value = { ...result, cards: result.cards || [], evidence: result.evidence || [] }
    if (['pending', 'running'].includes(result.status)) timer = setTimeout(refresh, 3000)
  } catch (cause) {
    if (!disposed && request === sequence) {
      snapshot.value = null
      selection.value = null
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
  selection.value = null
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
.experiment-toolbar { display: flex; flex-shrink: 0; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; font-weight: 600; }
.experiment-panel > :deep(.ant-alert) { margin-bottom: 12px; }
.experiment-summary { flex-shrink: 0; padding: 10px 14px; margin-bottom: 12px; border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-25); }
.summary-heading { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; }
.summary-heading span, .experiment-summary p, .source-empty { color: var(--color-text-secondary); font-size: 12px; }
.experiment-summary p { margin: 4px 0 0; }
.experiment-layout {
  display: grid;
  flex: 1;
  min-height: 0;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  gap: 12px;
}
.experiment-cards, .source-scroll { min-height: 0; overflow: auto; overscroll-behavior: contain; }
.experiment-cards { padding-inline-end: 4px; }
.experiment-card, .experiment-sources { background: var(--gray-0); border: 1px solid var(--gray-150); border-radius: 8px; min-width: 0; }
.experiment-card { padding: 14px; }
.experiment-card + .experiment-card { margin-top: 12px; }
h3 { margin: 0 0 12px; font-size: 15px; overflow-wrap: anywhere; }
dl { margin: 0; }
.experiment-row { padding: 8px 0; border-top: 1px solid var(--gray-100); }
.is-selected { background: var(--main-50); }
dt { font-size: 12px; color: var(--color-text-secondary); }
dd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
button { color: var(--main-color); background: none; border: 0; padding: 2px 0; cursor: pointer; font: inherit; }
button[aria-pressed='true'] { font-weight: 600; }
button:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--main-color); outline-offset: -2px; }
.experiment-sources { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.source-header { flex-shrink: 0; padding: 12px 14px; background: var(--gray-25); border-bottom: 1px solid var(--gray-150); }
.source-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.source-heading h3 { margin: 0; }
.source-heading button { flex-shrink: 0; font-size: 12px; }
.source-selection { margin-top: 6px; overflow-wrap: anywhere; }
.source-selection span { display: block; overflow: hidden; color: var(--color-text-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.source-selection strong {
  display: -webkit-box;
  margin-top: 3px;
  overflow: hidden;
  white-space: pre-wrap;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.source-scroll { flex: 1; padding: 0 14px 14px; }
.source-quote { overflow-wrap: anywhere; border-top: 1px solid var(--gray-100); }
.source-quote:first-child { border-top: 0; }
.source-quote > p, .source-empty { margin: 12px 0 8px; }
.source-quote > p { font-size: 12px; color: var(--color-text-secondary); }
.warning { color: var(--color-warning-700); white-space: pre-wrap; }
.unassigned { margin: 12px 0 0; padding: 12px 14px; border: 1px solid var(--gray-150); border-radius: 8px; overflow-wrap: anywhere; }
.unassigned summary { cursor: pointer; }
@media (max-width: 900px) {
  .experiment-layout { grid-template-columns: minmax(0, 1fr); }
  .experiment-layout.has-selection { grid-template-rows: minmax(0, 1fr) minmax(0, 46%); }
  .experiment-layout:not(.has-selection) .experiment-sources { display: none; }
}
</style>
