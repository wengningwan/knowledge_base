<template>
  <section class="correction-workspace" aria-label="人工校对">
    <header class="correction-heading">
      <div class="queue-filters" aria-label="处理进度">
        <button type="button" :disabled="saving" :aria-pressed="mode === 'pending'" @click="changeMode('pending')">待处理 <strong>{{ pending.length }}</strong></button>
        <button type="button" :disabled="saving" :aria-pressed="mode === 'processed'" @click="changeMode('processed')">已处理 <strong>{{ processed.length }}</strong></button>
      </div>
    </header>
    <div class="correction-columns">
      <nav class="correction-list" aria-label="校对待办">
        <button v-for="task in queue" :key="task.task_id" type="button" :disabled="saving" :aria-pressed="activeTask?.task_id === task.task_id" @click="chooseTask(task.task_id)">
          <small>{{ kindLabels[task.kind] }}</small>
          <strong>{{ task.fields.sample || '样品待确定' }}</strong>
          <span>{{ experimentLabel(task.fields.metric) || (task.kind === 'source' ? '原文内容需确认' : '指标待确定') }}</span>
          <small v-if="task.fields.result" class="candidate-preview">{{ task.fields.result }}</small>
          <small v-if="task.resolution">{{ outcomeLabels[task.resolution.outcome] }}</small>
        </button>
        <p v-if="!queue.length" class="queue-empty">{{ mode === 'pending' ? '当前没有待处理项' : '还没有处理记录' }}</p>
      </nav>
      <template v-if="activeTask">
        <section class="correction-source" aria-label="对应原文">
          <header><h3>对应原文</h3><span>{{ selectedSources.length ? selectedSources.length + ' 处' : '尚未定位' }}</span></header>
          <p v-if="!selectedSources.length" class="source-empty">模型没有保留可定位的出处。可在右侧选择当前正文中的出处后继续。</p>
          <article v-for="source in selectedSources" :key="source.evidence_id" class="correction-quote">
            <strong>{{ source.page ? 'PDF 第 ' + source.page + ' 页' : '正文出处' }}</strong>
            <MarkdownPreview :content="experimentEvidenceMarkdown(source.quote, fields)" :highlight-text="/^\s*<table\b/i.test(source.quote) ? '' : fields.result" compact />
          </article>
        </section>
        <section class="correction-editor" aria-label="处理当前待办">
          <header><h3>{{ activeTask.resolution ? outcomeLabels[activeTask.resolution.outcome] : kindLabels[activeTask.kind] }}</h3><span>{{ queue.indexOf(activeTask) + 1 }} / {{ queue.length }}</span></header>
          <p v-if="activeTask.resolution?.outcome === 'checked'" class="correction-outcome">核对状态和所选出处已保存，原文和实验数据保留原值。</p>
          <p class="correction-reason">{{ activeTask.reason }}</p>
          <a-button v-if="activeTask.kind === 'missing' && !recordingMissing && canManage" :disabled="saving" @click="recordingMissing = true">填写数据</a-button>
          <div v-if="showRecordFields" class="correction-fields">
            <label>样品<a-auto-complete v-model:value="fields.sample" :options="sampleOptions" :disabled="saving || !canManage" :filter-option="filterSample" placeholder="选择或填写样品名称" /></label>
            <label>指标或操作<a-input v-model:value="fields.metric" :maxlength="1000" :disabled="saving || !canManage" placeholder="待补充" /></label>
            <label v-if="fields.kind === 'measurement'" class="result-field">结果<a-input v-model:value="fields.result" :maxlength="1000" :disabled="saving || !canManage" placeholder="保留原文数值、单位和限定词" /></label>
            <label>条件<a-textarea v-model:value="fields.conditions" :maxlength="16000" :auto-size="{ minRows: 2, maxRows: 5 }" :disabled="saving || !canManage" placeholder="原文未注明可留空" /></label>
          </div>
          <details v-if="showRecordFields || activeTask.kind === 'source'" class="source-picker" :open="!selectedSources.length">
            <summary>{{ selectedSources.length ? '更换或补充出处' : '选择原文出处' }}</summary>
            <a-select v-model:value="fields.evidence_ids" mode="multiple" show-search :options="sourceOptions" :filter-option="filterSource" :disabled="saving || !canManage" placeholder="按页码或原文内容查找" />
          </details>
          <details v-if="showRecordFields" class="more-fields">
            <summary>记录类型与测试方法</summary>
            <label>记录类型<a-select v-model:value="fields.kind" :options="kindOptions" :disabled="saving || !canManage" /></label>
            <label>测试方法<a-input v-model:value="fields.test" :maxlength="1000" :disabled="saving || !canManage" /></label>
          </details>
          <a-alert v-if="error" type="error" :message="error" show-icon />
          <p v-if="canManage && showRecordFields && !readyToSave" class="save-hint">补齐样品、指标或操作{{ fields.kind === 'measurement' ? '、结果' : '' }}，并选择出处后即可保存。</p>
          <div v-if="canManage" class="correction-actions">
            <a-button v-if="showRecordFields" type="primary" :loading="saving" :disabled="!readyToSave" @click="submit('save')">{{ activeTask.resolution ? '保存修改' : '保存并下一条' }}</a-button>
            <a-button v-if="activeTask.kind === 'source'" :loading="saving" :disabled="!selectedSources.length" @click="submit('resolve', 'checked')">{{ activeTask.resolution?.outcome === 'checked' ? '保存核对出处' : '完成核对，保留现状' }}</a-button>
            <a-button v-if="activeTask.kind === 'missing'" :type="recordingMissing ? 'default' : 'primary'" :disabled="saving" @click="submit('resolve', 'unreported')">原文未提供</a-button>
            <a-button :disabled="saving" @click="submit('resolve', 'ignored')">{{ activeTask.record_id ? '排除记录' : '不采用此项' }}</a-button>
          </div>
        </section>
      </template>
      <section v-else class="correction-finished">
        <h3>{{ mode === 'pending' ? '当前没有可处理的待办' : '处理结果会保存在这里' }}</h3>
        <p>{{ mode === 'pending' ? '已处理项可随时打开查看和修改。' : '保存数据、保持缺失或不采用后，可在这里回看。' }}</p>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { experimentEvidenceMarkdown, experimentLabel } from '@/utils/experimentPresentation'

const props = defineProps({
  snapshot: { type: Object, required: true },
  paper: { type: Object, required: true },
  canManage: { type: Boolean, default: false },
  saveReview: { type: Function, required: true }
})
const kindLabels = { candidate: '确认提取结果', missing: '确认资料缺口', source: '核对原文疑点' }
const outcomeLabels = { saved: '已保存数据', unreported: '原文未提供', ignored: '已不采用', checked: '已核对，保留现状' }
const kindOptions = [{ value: 'measurement', label: '测量结果' }, { value: 'preparation', label: '制备操作' }]
const mode = ref('pending')
const selectedId = ref('')
const fields = ref({ evidence_ids: [] })
const drafts = new Map()
const saving = ref(false)
const error = ref('')
const recordingMissing = ref(false)
const tasks = computed(() => props.snapshot.review?.tasks || [])
const pending = computed(() => tasks.value.filter(task => !task.resolution))
const processed = computed(() => tasks.value.filter(task => task.resolution))
const queue = computed(() => mode.value === 'pending' ? pending.value : processed.value)
const activeTask = computed(() => queue.value.find(task => task.task_id === selectedId.value) || queue.value[0])
const canRecord = computed(() => activeTask.value && (activeTask.value.kind !== 'source' || activeTask.value.record_id))
const showRecordFields = computed(() => canRecord.value && (activeTask.value.kind !== 'missing' || recordingMissing.value))
const selectedSources = computed(() => (props.snapshot.evidence || []).filter(source => fields.value.evidence_ids.includes(source.evidence_id)))
const readyToSave = computed(() => fields.value.sample?.trim() && fields.value.metric?.trim() &&
  (fields.value.kind === 'preparation' || fields.value.result?.trim()) && selectedSources.value.length)
const sampleOptions = computed(() => [...new Set([
  ...props.paper.sample_states.map(item => item.reported_name),
  ...(props.snapshot.review?.records || []).map(item => item.sample).filter(Boolean)
])].map(value => ({ value })))
const sourceOptions = computed(() => (props.snapshot.evidence || []).map(source => ({
  value: source.evidence_id,
  label: (source.page ? 'PDF 第 ' + source.page + ' 页 · ' : '') + source.quote.replace(/<[^>]*>/g, ' ').slice(0, 110)
})))

watch(activeTask, task => {
  error.value = ''
  if (!task) return
  const savedDraft = drafts.get(task.task_id)
  fields.value = savedDraft?.fields || { ...task.fields, metric: experimentLabel(task.fields.metric), evidence_ids: [...task.fields.evidence_ids] }
  recordingMissing.value = savedDraft ? savedDraft.recordingMissing : task.resolution?.outcome === 'saved'
}, { immediate: true })

/** 目录切换保留尚未提交的局部输入。 */
function chooseTask(id) {
  if (saving.value) return
  if (activeTask.value) drafts.set(activeTask.value.task_id, {
    fields: { ...fields.value, evidence_ids: [...fields.value.evidence_ids] },
    recordingMissing: recordingMissing.value
  })
  selectedId.value = id
}

/** 切换处理状态时保留当前输入。 */
function changeMode(value) {
  chooseTask('')
  mode.value = value
}

/** 样品选择支持按已有名称筛选。 */
function filterSample(input, option) {
  return option.value.toLowerCase().includes(input.toLowerCase())
}

/** 出处搜索覆盖完整原文，选项摘要只用于列表阅读。 */
function filterSource(input, option) {
  const source = (props.snapshot.evidence || []).find(item => item.evidence_id === option.value)
  return (option.label + ' ' + (source?.quote || '')).toLowerCase().includes(input.toLowerCase())
}

/** 以服务器提交结果推进队列，失败时保留输入。 */
async function submit(action, resolution) {
  if (saving.value || !props.canManage || !activeTask.value) return
  const task = activeTask.value
  const index = queue.value.indexOf(task)
  const nextId = queue.value[index + 1]?.task_id || queue.value.find(item => item.task_id !== task.task_id)?.task_id || ''
  saving.value = true
  error.value = ''
  try {
    const result = await props.saveReview({
      task_id: task.task_id, action, resolution,
      fields: action === 'save' || resolution === 'checked' ? { ...fields.value, result: fields.value.kind === 'preparation' ? '' : fields.value.result } : null
    })
    if (!result) return
    drafts.delete(task.task_id)
    selectedId.value = nextId
    let successText = action === 'save' ? '数据已保存。' : outcomeLabels[resolution] + '。'
    if (resolution === 'checked') {
      successText = task.resolution ? '核对状态和出处已更新，数据保持原值。' : '已移到「已处理」，原文和实验数据保持原值。'
    }
    message.success(successText)
  } catch (cause) {
    error.value = cause.message || '保存失败，请重试。'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped lang="less">
.correction-workspace { display: flex; flex: 1; flex-direction: column; min-height: 0; color: var(--color-text); }
.correction-heading { display: flex; justify-content: flex-end; padding: 10px 0 14px; }
h3, p { margin: 0; }
h3 { font-size: 14px; }
.correction-finished p { margin-top: 5px; color: var(--color-text-secondary); }
.queue-filters { display: flex; gap: 5px; padding: 4px; border-radius: 7px; background: var(--gray-50); }
.queue-filters button { padding: 6px 10px; border: 0; border-radius: 4px; background: transparent; color: var(--color-text-secondary); cursor: pointer; white-space: nowrap; }
.queue-filters button[aria-pressed='true'] { color: var(--main-color); background: var(--gray-0); }
.queue-filters strong { margin-left: 8px; }
.correction-columns { display: grid; flex: 1; min-height: 0; grid-template-columns: minmax(150px, .7fr) minmax(0, 1.6fr) minmax(285px, 1.3fr); gap: 12px; }
.correction-list, .correction-source, .correction-editor { min-height: 0; min-width: 0; overflow: auto; border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-0); }
.correction-list { padding: 5px; }
.correction-list > button { display: flex; flex-direction: column; gap: 5px; width: 100%; padding: 11px; margin-bottom: 4px; border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--color-text); text-align: left; overflow-wrap: anywhere; cursor: pointer; }
.correction-list > button[aria-pressed='true'] { border-color: var(--main-color); background: var(--main-50); }
.correction-list > button:hover { background: var(--gray-50); }
.correction-list small { font-size: 12px; color: var(--color-text-secondary); }
.candidate-preview { max-height: 3em; overflow: hidden; }
.correction-source, .correction-editor { padding: 14px; overflow-wrap: anywhere; }
.correction-source > header, .correction-editor > header { display: flex; justify-content: space-between; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid var(--gray-150); }
header > span { color: var(--color-text-secondary); font-size: 12px; }
.correction-quote { margin-top: 16px; }
.correction-quote > strong { display: block; margin-bottom: 8px; color: var(--main-color); font-size: 12px; }
.correction-quote :deep(table) { font-size: 12px; }
.source-empty, .queue-empty { padding: 18px 6px; color: var(--color-text-secondary); }
.correction-reason { padding: 12px 0; line-height: 1.65; }
.correction-outcome { margin-top: 12px; color: var(--main-color); font-size: 12px; }
.correction-fields, .more-fields { display: flex; flex-direction: column; gap: 12px; }
label { display: flex; flex-direction: column; gap: 5px; color: var(--color-text-secondary); font-size: 12px; }
.result-field :deep(input) { font-weight: 600; }
details { margin-top: 15px; }
summary { cursor: pointer; color: var(--color-text-secondary); font-size: 12px; }
.source-picker .ant-select, .more-fields .ant-select { width: 100%; }
.source-picker .ant-select, .more-fields label { margin-top: 10px; }
.save-hint { margin-top: 12px; color: var(--color-text-secondary); font-size: 12px; }
.correction-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 18px; }
.correction-finished { grid-column: 2 / -1; display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 180px; border: 1px solid var(--gray-150); border-radius: 8px; padding: 20px; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--main-color); outline-offset: 2px; }
button:disabled { cursor: default; opacity: .6; }
@media (max-width: 1000px) { .correction-columns { grid-template-columns: minmax(130px, .65fr) minmax(0, 1.2fr) minmax(245px, 1.2fr); gap: 8px; } }
@media (max-width: 760px) {
  .correction-workspace { overflow: auto; }
  .correction-columns { display: flex; flex-direction: column; flex: none; }
  .correction-list { display: flex; overflow: auto; max-height: 150px; flex-shrink: 0; }
  .correction-list > button { min-width: 165px; }
  .correction-source { max-height: 40vh; }
  .correction-editor { overflow: visible; }
}
</style>
