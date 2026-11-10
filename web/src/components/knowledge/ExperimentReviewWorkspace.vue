<template>
  <div class="review-workspace" :class="{ 'editor-only': editorOnly }">
    <div v-if="!editorOnly" class="workspace-toolbar">
      <a-segmented v-model:value="mode" :options="views" :disabled="Boolean(editor)" @change="clearSelection" />
      <span class="muted">
        {{ samples.length }} 张样品卡 · {{ usable.length }} 条实验记录
        <template v-if="pending.length || !rejectedProposalCount"> · {{ pending.length }} 条待审核</template>
        <template v-if="rejectedProposalCount"> · {{ rejectedProposalCount }} 条提案未通过校验</template>
      </span>
      <a-button v-if="canManage" size="small" :disabled="Boolean(editor)" @click="editRecord()">新增记录</a-button>
    </div>
    <details v-if="!editorOnly && incompleteBatchCount" class="diagnostics">
      <summary>生成详情</summary>
      <span>{{ incompleteBatchCount }} 批提取未完成</span>
    </details>
    <p v-if="notice" class="review-notice" role="status">{{ notice }}</p>
    <div class="workspace-columns">
      <nav v-if="!editorOnly" class="workspace-column directory" :aria-label="mode === 'samples' ? '样品目录' : '审核队列'">
        <template v-if="mode === 'samples'">
          <h3>样品目录</h3>
          <button
            v-for="sample in samples" :key="sample" type="button" :disabled="Boolean(editor)"
            :aria-pressed="sample === activeSample" @click="chooseSample(sample)"
          >
            {{ sample }}
          </button>
          <p v-if="!samples.length" class="muted">当前没有已组装的样品卡。</p>
        </template>
        <template v-else>
          <h3>审核队列</h3>
          <a-segmented v-model:value="queueMode" :options="queueOptions" :disabled="Boolean(editor)" @change="clearSelection" />
          <template v-if="queueRecords.length">
            <a-select v-model:value="sampleFilter" :options="sampleOptions" :disabled="Boolean(editor)" aria-label="筛选样品" />
            <a-select v-if="queueMode === 'pending'" v-model:value="issueFilter" :options="issueOptions" :disabled="Boolean(editor)" aria-label="筛选问题" />
          </template>
          <button
            v-for="record in queue" :key="record.record_id" type="button" :disabled="Boolean(editor)"
            :aria-pressed="!fragment && record.record_id === activeRecord?.record_id" @click="chooseRecord(record)"
          >
            {{ record.sample || '样品待确定' }}
            <small>{{ record.metric || '指标待确定' }}</small>
          </button>
          <details v-if="fragments.length && queueMode === 'pending'" :open="!pending.length" class="fragments">
            <summary>未归属片段 · {{ fragments.length }}</summary>
            <div v-for="fact in fragments" :key="fact.fact_id" class="fragment">
              <span>{{ fact.raw_text }}</span>
              <div class="fragment-actions">
                <a-button type="link" size="small" :disabled="Boolean(editor)" @click="chooseFragment(fact)">查看出处</a-button>
                <a-button v-if="canManage" type="link" size="small" :disabled="Boolean(editor)" @click="editRecord(null, fact)">整理为记录</a-button>
              </div>
            </div>
          </details>
        </template>
      </nav>

      <section ref="recordScroll" class="workspace-column record-column" aria-label="实验记录">
        <template v-if="mode === 'samples' && activeSample && !editor">
          <p class="muted">样品卡</p>
          <h2>{{ activeSample }}</h2>
          <h3 class="section-title">实验记录</h3>
          <div class="record-table">
            <table>
              <thead><tr><th class="record-kind">类别</th><th>操作或指标</th><th>测试条件</th><th>结果</th></tr></thead>
              <tbody>
                <tr
                  v-for="record in sampleRecords" :key="record.record_id"
                  :class="{ selected: activeRecord?.record_id === record.record_id }"
                >
                  <td class="record-kind">{{ kindLabels[record.kind] }}</td>
                  <td class="record-metric">
                    <button
                      type="button" class="record-source" title="查看原文出处"
                      :aria-pressed="activeRecord?.record_id === record.record_id"
                      @click="chooseRecord(record, record.metric ? 'metric' : 'result')"
                    >
                      <strong>{{ record.metric || '待补充' }}</strong>
                      <small v-if="record.test">{{ record.test }}</small>
                    </button>
                  </td>
                  <td>{{ record.conditions || '未记录' }}</td>
                  <td>{{ record.result || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <template v-if="editor">
          <div class="record-heading">
            <h3>{{ editor.recordId ? '调整实验记录' : '新增实验记录' }}</h3>
            <a-button size="small" :disabled="saving" @click="cancelEditor">取消</a-button>
          </div>
          <div class="edit-fields">
            <label>记录类型<a-select v-model:value="editor.fields.kind" :options="kindOptions" :disabled="saving" /></label>
            <label>归属样品<a-input v-model:value="editor.fields.sample" :maxlength="1000" :disabled="saving" /></label>
            <label>操作或指标<a-input v-model:value="editor.fields.metric" :maxlength="1000" :disabled="saving" /></label>
            <label>测试方法<a-input v-model:value="editor.fields.test" :maxlength="1000" :disabled="saving" /></label>
            <label class="full-field">条件<a-textarea v-model:value="editor.fields.conditions" :maxlength="16000" :auto-size="{ minRows: 2 }" :disabled="saving" /></label>
            <label v-if="editor.fields.kind === 'measurement'" class="full-field">
              结果（保留原文限定词与单位）
              <a-input v-model:value="editor.fields.result" :maxlength="1000" :disabled="saving" />
            </label>
            <label class="full-field">
              选择出处
              <a-select
                v-model:value="editor.fields.evidence_ids" mode="multiple" show-search
                :options="sourceOptions" :disabled="saving" option-filter-prop="label" placeholder="从当前草稿选择原文出处"
              />
            </label>
          </div>
          <a-checkbox v-model:checked="editor.verified" class="section-title" :disabled="saving">已对照出处核对样品、条件及结果</a-checkbox>
          <a-alert v-if="error" type="error" :message="error" show-icon />
          <div class="record-actions">
            <a-button :disabled="saving" @click="save('save')">{{ editorOnly ? '保存待确认' : '保存为待审核' }}</a-button>
            <a-button type="primary" :loading="saving" :disabled="!editor.verified" @click="save('confirm')">确认本条</a-button>
            <a-button
              v-if="editor.recordId && editor.status !== 'excluded'" danger :disabled="saving" @click="save('exclude')"
            >排除</a-button>
          </div>
        </template>
        <template v-else-if="fragment">
          <h3>未归属片段</h3>
          <p class="section-title">{{ fragment.raw_text }}</p>
          <p v-if="canManage" class="muted section-title">先看右侧原文。能确定样品和实验内容时，可把这个片段整理为一条记录。</p>
          <a-button v-if="canManage" size="small" class="section-title" @click="editRecord(null, fragment)">整理为记录</a-button>
        </template>
        <template v-else-if="activeRecord">
          <div class="record-heading">
            <h3>{{ mode === 'samples' ? '选中记录' : '当前审核记录' }}</h3>
            <a-tag>{{ statusLabels[activeRecord.status] }}</a-tag>
          </div>
          <dl class="record-facts">
            <div><dt>归属样品</dt><dd>{{ activeRecord.sample || '待确定' }}</dd></div>
            <div><dt>操作或指标</dt><dd>{{ activeRecord.metric || '待补充' }}</dd></div>
            <div v-if="activeRecord.test"><dt>测试方法</dt><dd>{{ activeRecord.test }}</dd></div>
            <div><dt>测试条件</dt><dd>{{ activeRecord.conditions || '未记录' }}</dd></div>
            <div v-if="activeRecord.kind === 'measurement'"><dt>结果</dt><dd>{{ activeRecord.result || '待补充' }}</dd></div>
          </dl>
          <p v-if="activeRecord.issue" class="muted">{{ issueLabels[activeRecord.issue] }}</p>
          <p v-if="canManage && activeRecord.status === 'pending'" class="muted section-title">
            对照右侧原文，点「调整数据」补全、确认或排除这条记录。
          </p>
          <a-button v-if="canManage" size="small" class="section-title" @click="editRecord(activeRecord)">调整数据</a-button>
        </template>
        <template v-else-if="mode === 'review'">
          <h3 v-if="queueRecords.length">没有符合筛选条件的记录</h3>
          <h3 v-else-if="queueMode === 'processed'">暂无已处理记录</h3>
          <template v-else>
            <h3>当前没有待审核记录</h3>
            <p v-if="fragments.length" class="muted section-title">
              左侧还有 {{ fragments.length }} 个未归属片段，是尚未整理成实验记录的零散条件或结果。点「查看出处」对照原文。
              <template v-if="canManage">能确定归属和含义的，可以点「整理为记录」。</template>
            </p>
          </template>
        </template>
        <p v-else class="muted">当前没有可查看的实验记录。</p>
      </section>

      <aside ref="evidenceScroll" class="workspace-column evidence-column" aria-label="原文出处">
        <h3>原文出处</h3>
        <p v-if="sourceSample" class="section-title">
          {{ activeRecord?.status === 'pending' || editor ? '拟归属样品' : '归属样品' }} <strong>{{ sourceSample }}</strong>
        </p>
        <a-segmented v-if="highlightOptions.length > 1 && sources.length" v-model:value="highlightField" :options="highlightOptions" size="small" />
        <div v-if="!sources.length" class="muted section-title">{{ editor ? '请在编辑区选择出处。' : '选择有出处的记录或片段后，这里会显示原文。' }}</div>
        <section v-for="source in sources" :key="source.evidence_id" class="source">
          <p class="muted">
            {{ source.evidence_id }} · Markdown 第 {{ source.line_start === source.line_end ? source.line_start : `${source.line_start}–${source.line_end}` }} 行
            <span v-if="source.page"> · PDF 第 {{ source.page }} 页</span>
          </p>
          <MarkdownPreview :content="experimentEvidenceMarkdown(source.quote, sourceRecord)" :highlight-text="highlightText" compact />
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { experimentEvidenceMarkdown } from '@/utils/experimentPresentation'

const props = defineProps({
  snapshot: { type: Object, required: true },
  canManage: { type: Boolean, default: false },
  saveReview: { type: Function, required: true },
  editorOnly: { type: Boolean, default: false }
})
const emit = defineEmits(['saved', 'cancel'])

const kindLabels = { preparation: '制备', measurement: '测量' }
const statusLabels = { machine: '机器草稿', pending: '待审核', confirmed: '已确认', excluded: '已排除' }
const issueLabels = {
  missing_evidence: '部分字段缺少可定位出处',
  different_source: '需要核对不同原文位置间的归属',
  table_preparation: '需要人工核对表格制备步骤',
  table_alignment: '样品或指标与表格行列未对齐'
}
const views = [{ label: '样品卡', value: 'samples' }, { label: '待审核', value: 'review' }]
const kindOptions = Object.entries(kindLabels).map(([value, label]) => ({ value, label }))
const highlightLabels = { result: '结果', metric: '指标或操作', sample: '样品', test: '方法' }
const records = computed(() => props.snapshot.review?.records || [])
const pending = computed(() => records.value.filter((record) => record.status === 'pending'))
const processed = computed(() => records.value.filter((record) => ['confirmed', 'excluded'].includes(record.status)))
const usable = computed(() => records.value.filter((record) => ['machine', 'confirmed'].includes(record.status)))
const mode = ref(usable.value.length ? 'samples' : 'review')
const queueMode = ref('pending')
const selectedSample = ref('')
const selectedId = ref(null)
const sampleFilter = ref('')
const issueFilter = ref('')
const highlightField = ref('result')
const fragment = ref(null)
const editor = ref(null)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const evidenceScroll = ref(null)
const recordScroll = ref(null)

const queueOptions = computed(() => [
  { label: `待审核 ${pending.value.length}`, value: 'pending' },
  { label: `已处理 ${processed.value.length}`, value: 'processed' }
])
const samples = computed(() => [...new Set(usable.value.map((record) => record.sample).filter(Boolean))])
const activeSample = computed(() => samples.value.includes(selectedSample.value) ? selectedSample.value : samples.value[0])
const sampleRecords = computed(() => usable.value.filter((record) => record.sample === activeSample.value))
const queueRecords = computed(() => queueMode.value === 'pending' ? pending.value : processed.value)
const sampleOptions = computed(() => [
  { label: '所有样品', value: '' },
  ...[...new Set(queueRecords.value.map((record) => record.sample).filter(Boolean))].map((value) => ({ value, label: value }))
])
const issueOptions = [{ value: '', label: '所有问题' }, ...Object.entries(issueLabels).map(([value, label]) => ({ value, label }))]
const queue = computed(() => queueRecords.value.filter((record) =>
  (!sampleFilter.value || record.sample === sampleFilter.value) &&
  (queueMode.value !== 'pending' || !issueFilter.value || record.issue === issueFilter.value)
))
const activeRecord = computed(() => {
  const visible = mode.value === 'samples' ? sampleRecords.value : queue.value
  return visible.find((record) => record.record_id === selectedId.value) || visible[0]
})
const fragments = computed(() => {
  const used = new Set(records.value.map((record) => record.fragment_id).filter(Boolean))
  return (props.snapshot.coverage?.unassigned_facts || []).filter((fact) => !used.has(fact.fact_id))
})
const rejectedProposalCount = computed(() => props.snapshot.coverage?.rejected_relation_count || 0)
const incompleteBatchCount = computed(() => props.snapshot.coverage?.incomplete_batches?.length || 0)
const allSources = computed(() => [...(props.snapshot.evidence || [])].sort((left, right) =>
  left.line_start - right.line_start || left.line_end - right.line_end
))
const sourceOptions = computed(() => allSources.value.map((source) => ({
  value: source.evidence_id,
  label: `${source.evidence_id} · 第 ${source.line_start} 行 · ${source.quote.slice(0, 48)}`
})))
const sourceRecord = computed(() => editor.value?.fields || (fragment.value ? null : activeRecord.value))
const sourceSample = computed(() => sourceRecord.value?.sample)
const highlightOptions = computed(() => Object.entries(highlightLabels)
  .filter(([value]) => sourceRecord.value?.[value]).map(([value, label]) => ({ value, label })))
const sources = computed(() => {
  const ids = sourceRecord.value?.evidence_ids || fragment.value?.evidence_ids || []
  return allSources.value.filter((source) => ids.includes(source.evidence_id))
})
const highlightText = computed(() => sourceRecord.value?.[highlightField.value] || fragment.value?.raw_text)

watch(highlightOptions, (options) => {
  if (!options.some(({ value }) => value === highlightField.value)) highlightField.value = options[0]?.value || 'result'
}, { immediate: true })

watch([activeRecord, fragment, sources], () => {
  if (evidenceScroll.value) evidenceScroll.value.scrollTop = 0
}, { flush: 'post' })

watch(editor, () => {
  if (recordScroll.value) recordScroll.value.scrollTop = 0
}, { flush: 'post' })

/** 切换列表或完成保存后清空选择。 */
const clearSelection = () => {
  editor.value = null
  fragment.value = null
  selectedId.value = null
  highlightField.value = 'result'
  sampleFilter.value = ''
  issueFilter.value = ''
  error.value = ''
}

const chooseSample = (sample) => {
  clearSelection()
  selectedSample.value = sample
}

const chooseRecord = (record, field = 'result') => {
  editor.value = null
  fragment.value = null
  selectedId.value = record.record_id
  highlightField.value = field
  error.value = ''
}

const chooseFragment = (fact) => {
  editor.value = null
  fragment.value = fact
  highlightField.value = 'result'
}

/** 从一条现有记录或可回查片段开始编辑。 */
const editRecord = (record, fact) => {
  fragment.value = fact || null
  if (record) selectedId.value = record.record_id
  editor.value = {
    recordId: record?.record_id || null,
    status: record?.status,
    fragmentId: fact?.fact_id || null,
    verified: false,
    fields: {
      kind: record?.kind || 'measurement',
      sample: record?.sample ?? (props.editorOnly ? '' : mode.value === 'samples' ? activeSample.value || '' : ''),
      metric: record?.metric || '',
      test: record?.test || '',
      conditions: record?.conditions || (fact?.kind === 'condition' ? fact.raw_text : ''),
      result: record?.result || (fact?.kind === 'result' ? fact.raw_text : ''),
      evidence_ids: [...(record?.evidence_ids || fact?.evidence_ids || [])]
    }
  }
  highlightField.value = 'result'
  error.value = ''
}

/** 弹窗取消交回父工作台，普通审核页保留原交互。 */
const cancelEditor = () => {
  if (saving.value) return
  editor.value = null
  emit('cancel')
}

/** 只在服务器提交成功后切换到最新记录状态。 */
const save = async (action) => {
  if (saving.value || !editor.value || !props.canManage) return
  const current = editor.value
  saving.value = true
  error.value = ''
  try {
    const record = await props.saveReview({
      action,
      record_id: current.recordId,
      fragment_id: current.fragmentId,
      fields: action === 'exclude' ? null : { ...current.fields, result: current.fields.kind === 'preparation' ? '' : current.fields.result },
      verified: current.verified
    })
    if (!record) return
    notice.value = action === 'confirm' ? '本条记录已确认。' : action === 'exclude' ? '本条记录已排除。' : '已保存为待审核。'
    if (action === 'save') {
      mode.value = 'review'
      queueMode.value = 'pending'
    } else if (mode.value === 'samples') {
      selectedSample.value = record.sample
    }
    clearSelection()
    selectedId.value = record.record_id
    emit('saved', record)
  } catch (cause) {
    error.value = cause.message || '审核记录保存失败。'
  } finally {
    saving.value = false
  }
}

defineExpose({ editRecord, saving })
</script>

<style scoped lang="less">
.review-workspace { display: flex; flex: 1; flex-direction: column; gap: 10px; min-height: 0; overflow: hidden; }
.workspace-toolbar, .record-heading, .record-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.workspace-toolbar > .muted { flex: 1; }
.diagnostics { color: var(--color-text-secondary); font-size: 12px; }
.diagnostics summary { cursor: pointer; }
.review-notice { margin: 0; color: var(--main-color); }
.workspace-columns { display: grid; flex: 1; min-height: 0; gap: 12px; grid-template-columns: minmax(138px, .8fr) minmax(0, 2.25fr) minmax(0, 1.35fr); }
.workspace-column { min-width: 0; min-height: 0; padding: 12px; overflow: auto; overscroll-behavior: contain; border: 1px solid var(--gray-150); border-radius: 8px; overflow-wrap: anywhere; }
.editor-only { min-height: 300px; max-height: 75vh; }
.editor-only .workspace-columns { grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); }
.workspace-column h2, .workspace-column h3, .workspace-column p { margin: 0; }
.workspace-column h3 { font-size: 14px; }
.muted, small, dt { color: var(--color-text-secondary); font-size: 12px; }
.directory { display: flex; flex-direction: column; gap: 7px; }
.directory > button { text-align: start; padding: 6px 8px; color: var(--color-text); background: var(--gray-0); border: 1px solid var(--gray-150); border-radius: 5px; cursor: pointer; }
.directory > button[aria-pressed='true'], tr.selected { background: var(--main-50); }
.directory > button[aria-pressed='true'] { border-color: var(--main-color); }
small { display: block; }
.fragment { padding: 6px 0; border-top: 1px solid var(--gray-100); }
.fragment-actions { display: flex; flex-wrap: wrap; }
.fragments summary { cursor: pointer; }
.record-table { overflow: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 7px 4px; text-align: start; vertical-align: top; border-bottom: 1px solid var(--gray-150); }
th { font-size: 12px; white-space: nowrap; }
.record-kind { width: 3.5rem; padding-inline: 8px; white-space: nowrap; }
.record-metric { padding: 0; }
.record-source {
  display: block;
  width: 100%;
  padding: 7px 4px;
  border: 0;
  background: transparent;
  color: var(--main-color);
  font: inherit;
  text-align: start;
  overflow-wrap: anywhere;
  cursor: pointer;
}
.record-source:hover strong, .record-source:focus-visible strong { text-decoration: underline; }
.record-heading { margin: 12px 0 8px; justify-content: space-between; }
.record-facts, .edit-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; }
dd { margin: 0; }
.edit-fields label { display: grid; gap: 4px; min-width: 0; }
.full-field { grid-column: 1 / -1; }
.workspace-column .section-title, .section-title, .record-actions { margin-top: 10px; }
.source { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--gray-150); }
.source :deep(.markdown-text-highlight) { color: inherit; background: var(--color-warning-100); border-radius: 2px; }
@media (max-width: 900px) {
  .workspace-columns { grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .directory { grid-column: 1 / -1; max-height: 148px; }
}
@media (max-width: 620px) {
  .review-workspace { overflow: auto; }
  .workspace-columns { display: flex; flex: initial; flex-direction: column; }
  .workspace-column { overflow: visible; }
  .directory { overflow: auto; }
  .editor-only .workspace-columns { display: flex; }
}
</style>
