<template>
  <div class="canonical-workspace">
    <div v-if="effectiveAvailable" class="paper-counts" aria-label="结构化记录数量">
      <button v-for="fact in counts" :key="fact.field" type="button" @click="showJson(fact.field)">
        <strong>{{ fact.count }}</strong> {{ fact.label }}
      </button>
    </div>
    <nav class="paper-nav" aria-label="实验数据视图">
      <button v-for="(label, key) in views" :key="key" type="button" :aria-pressed="view === key" @click="changeView(key)">{{ label }}<span v-if="key === 'review' && pendingCorrectionCount"> · {{ pendingCorrectionCount }}</span></button>
      <button v-if="canManage && effectiveAvailable" type="button" @click="startNew()">新增记录</button>
      <button v-if="canManage && effectiveAvailable && view === 'samples'" type="button" @click="openEntity('sample_state')">新增样品</button>
      <button v-if="canManage && effectiveAvailable && view === 'samples'" type="button" @click="openEntity('sample_batch')">新增批次</button>
      <button v-if="canManage && effectiveAvailable && view === 'runs'" type="button" @click="openEntity('experiment_run')">补录实验</button>
    </nav>
    <form v-if="view === 'samples' || view === 'runs'" class="paper-search" @submit.prevent="search">
      <label for="experiment-search">查结果</label>
      <a-input id="experiment-search" v-model:value="searchInput" placeholder="输入样品、指标或数值，例如 C-700-3 BET" allow-clear />
      <a-button html-type="submit" type="primary">搜索</a-button>
      <a-button v-if="query" @click="clearSearch">清除搜索</a-button>
    </form>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <ExperimentCorrectionQueue v-if="view === 'review'" :paper="paper" :snapshot="snapshot" :can-manage="canManage" :save-review="saveReview" />
    <div v-else class="paper-columns">
      <nav class="paper-pane directory" aria-label="记录目录">
        <template v-if="view === 'samples'">
          <h3>{{ query ? '筛选样品' : '样品目录' }}</h3>
          <button v-if="query" type="button" :aria-pressed="searchSample === ''" @click="chooseSearchSample('')">全部样品</button>
          <button v-for="sample in samples" :key="sample.sample_state_id" type="button" :aria-pressed="(query ? searchSample : activeSampleId) === sample.sample_state_id" @click="query ? chooseSearchSample(sample.sample_state_id) : chooseSample(sample.sample_state_id)">
            <strong>{{ sampleLabel(sample.sample_state_id) }}</strong><small v-if="sampleNote(sample)">{{ sampleNote(sample) }}</small>
          </button>
          <p v-if="!samples.length" class="empty">当前没有已登记样品。</p>
        </template>
        <template v-else-if="view === 'runs'">
          <h3>实验目录</h3>
          <button v-for="run in paper.experiment_runs" :key="run.run_id" type="button" :aria-pressed="activeRun?.run_id === run.run_id" @click="chooseRun(run)">
            <strong>{{ runTitle(run) }}</strong><small>{{ runNote(run) }}</small>
          </button>
          <p v-if="!paper.experiment_runs.length" class="empty">当前没有实验登记。</p>
        </template>
        <template v-else>
          <h3>JSON 顶层字段</h3>
          <button type="button" :aria-pressed="jsonField === ''" @click="jsonField = ''">查看全文</button>
          <button v-for="key in Object.keys(jsonPaper || {})" :key="key" type="button" :aria-pressed="jsonField === key" @click="jsonField = key">
            {{ experimentLabel(key) }}<small>{{ key }}</small>
          </button>
        </template>
      </nav>

      <section ref="mainPane" class="paper-pane main-pane" aria-label="实验内容">
        <template v-if="view === 'samples' && query">
          <header class="pane-heading"><h2>搜索结果</h2><p class="muted">“{{ query }}” · {{ searchResults.length }} 项结果</p></header>
          <div class="result-head"><span>样品与指标</span><span>结果</span><span>原文出处</span></div>
          <button v-for="record in searchResults" :key="record.record_id" type="button" class="result-row" :aria-pressed="inspection.record?.record_id === record.record_id" @click="chooseRecord(record)">
            <span><ExperimentText :text="metricTitle(record)" /><small>{{ recordSampleLabel(record) }} · {{ experimentLabel(record.test) }}</small><small v-if="record.conditions"><ExperimentText :text="record.conditions" /></small></span>
            <ExperimentText :text="recordResult(record)" /><span class="muted">{{ recordPage(record) }}</span>
          </button>
          <p v-if="!searchResults.length" class="empty">没有匹配的结果。可以试试样品编号、BET 或总孔容。</p>
        </template>
        <template v-else-if="view === 'samples'">
          <header class="pane-heading">
            <h2>{{ activeSampleName || '样品与结果' }}</h2>
            <p v-if="activeSample?.physical_form" class="muted">形态 · {{ activeSample.physical_form }}</p>
            <p v-if="sampleNote(activeSample)" class="muted">制备条件 · {{ sampleNote(activeSample) }}</p>
            <p v-if="activeBatch?.batch_number" class="muted">批号 · {{ activeBatch.batch_number }}</p>
            <div v-if="canManage && activeSample" class="button-group">
              <button type="button" class="small-button" @click="openEntity('sample_state', activeSample)">编辑样品</button>
              <button v-if="activeBatch" type="button" class="small-button" @click="openEntity('sample_batch', activeBatch)">编辑批次</button>
            </div>
            <div class="button-group">
              <a-button :type="sampleMode === 'results' ? 'primary' : 'default'" @click="chooseSampleMode('results')">测量结果</a-button>
              <a-button :type="sampleMode === 'preparation' ? 'primary' : 'default'" @click="chooseSampleMode('preparation')">制备步骤</a-button>
            </div>
            <div v-if="sampleMode === 'results' && availableCategories.length > 1" class="button-group">
              <button v-for="category in [{ key: '', label: '全部' }, ...availableCategories]" :key="category.key" type="button" class="small-button" :aria-pressed="filter === category.key" @click="chooseFilter(category.key)">{{ category.label }}</button>
            </div>
          </header>
          <template v-if="sampleMode === 'results'">
            <div v-if="measurementGroups.length" class="result-head"><span>指标与条件</span><span>结果</span><span>原文出处</span></div>
            <section v-for="group in measurementGroups" :key="group.key" class="method-group">
              <div class="group-heading">
                <strong>{{ experimentLabel(group.method) || '未注明测试方法' }}</strong>
                <small v-if="group.run">{{ runNote(group.run) }}</small>
                <button v-if="group.run" type="button" class="small-button" :aria-expanded="expandedMethod === group.key" @click="toggleMethod(group.key)">{{ expandedMethod === group.key ? '收起方法与条件' : '方法与条件' }}</button>
              </div>
              <div v-if="expandedMethod === group.key && group.run" class="method-details">
                <section>
                  <dl class="facts"><template v-for="[label, value] in experimentMethodFacts(group.run)" :key="label"><dt>{{ label }}</dt><dd><ExperimentText :text="value" /></dd></template></dl>
                  <div class="button-group">
                    <button type="button" class="small-button" @click="chooseCanonical(group.run, runTitle(group.run))">完整实验字段</button>
                    <button v-if="canManage" type="button" class="small-button" @click="openEntity('experiment_run', group.run)">编辑实验</button>
                    <button v-for="curve in runCurves(group.run)" :key="curve.series_set_id" type="button" class="small-button" @click="chooseCanonical(curve, experimentLabel(curve.series_kind))">{{ curveTitle(curve) }}</button>
                  </div>
                </section>
              </div>
              <button v-for="record in group.numeric" :key="record.record_id" type="button" class="result-row" :aria-pressed="inspection.record?.record_id === record.record_id" @click="chooseRecord(record)">
                <span><ExperimentText :text="metricTitle(record)" /><small v-if="record.scope === 'group'">整组 · {{ recordSampleLabel(record) }}</small><small v-if="!group.run && record.test">{{ experimentLabel(record.test) }}</small><small v-if="recordConditions(record)"><ExperimentText :text="recordConditions(record)" /></small><small v-if="['edited', 'confirmed'].includes(record.status)">{{ statusLabels[record.status] }}</small></span>
                <strong><ExperimentText :text="recordResult(record)" /></strong><span class="muted">{{ recordPage(record) }}</span>
              </button>
              <details v-if="group.written.length" class="written-results">
                <summary>文字观察（{{ group.written.length }}）</summary>
                <button v-for="record in group.written" :key="record.record_id" type="button" class="result-row" :aria-pressed="inspection.record?.record_id === record.record_id" @click="chooseRecord(record)">
                  <span><ExperimentText :text="metricTitle(record)" /><small v-if="record.scope === 'group'">整组 · {{ recordSampleLabel(record) }}</small><small v-if="!group.run && record.test">{{ experimentLabel(record.test) }}</small><small v-if="recordConditions(record)"><ExperimentText :text="recordConditions(record)" /></small><small v-if="['edited', 'confirmed'].includes(record.status)">{{ statusLabels[record.status] }}</small></span>
                  <ExperimentText :text="recordResult(record)" /><span class="muted">{{ recordPage(record) }}</span>
                </button>
              </details>
            </section>
            <p v-if="!measurementGroups.length" class="empty">当前样品没有这一类已组装的测量结果。</p>
          </template>
          <template v-else>
            <div v-if="sampleRoute && (canManage || sampleRoute.yield || sampleRoute.replicate_count != null)" class="section-heading">
              <p v-if="sampleRoute.yield">产率 · {{ experimentValue(sampleRoute.yield) }}</p>
              <p v-if="sampleRoute.replicate_count != null">重复次数 · {{ sampleRoute.replicate_count }}</p>
              <button v-if="canManage" type="button" class="small-button" @click="openEntity('synthesis_route', sampleRoute)">编辑制备路线</button>
            </div>
            <button v-for="({ operation, record }, index) in preparationSteps" :key="operation.operation_id" type="button" class="step-row" :aria-pressed="(inspection.canonicalObject || inspection.object)?.operation_id === operation.operation_id" @click="chooseStep(operation)">
              <span class="step-number">{{ operation.sequence ?? index + 1 }}</span>
              <span><strong>{{ experimentLabel(record?.metric || operation.operation_type) }}</strong><small v-if="reagentText(operation)">{{ reagentText(operation) }}</small><small><ExperimentText :text="record && record.status !== 'machine' ? recordConditions(record) : experimentStepConditions(operation, activeSample)" /></small><small v-if="record && ['edited', 'confirmed'].includes(record.status)">{{ statusLabels[record.status] }}</small></span>
            </button>
            <p v-if="!preparationSteps.length" class="empty">这个样品没有关联的制备步骤。</p>
            <template v-if="additionalPreparation.length">
              <h3 class="section-heading">其他制备记录</h3>
              <button v-for="record in additionalPreparation" :key="record.record_id" type="button" class="record-button" @click="chooseRecord(record)"><strong>{{ experimentLabel(record.metric) }}</strong><small><ExperimentText :text="record.conditions" /></small></button>
            </template>
          </template>
        </template>
        <template v-else-if="view === 'runs'">
          <header class="pane-heading">
            <h2>{{ activeRun ? runTitle(activeRun) : '实验与曲线' }}</h2>
            <dl v-if="activeRun" class="facts"><template v-for="[label, value] in experimentMethodFacts(activeRun)" :key="label"><dt>{{ label }}</dt><dd><ExperimentText :text="value" /></dd></template></dl>
            <div class="button-group">
              <button v-for="id in runSampleIds(activeRun)" :key="id" type="button" class="small-button" @click="chooseSample(id, true)">{{ sampleLabel(id) }} 的结果</button>
              <button v-if="activeRun && canManage" type="button" class="small-button" @click="openEntity('experiment_run', activeRun)">编辑实验</button>
            </div>
          </header>
          <h3 class="section-heading">关联曲线</h3>
          <button v-for="curve in runCurves(activeRun)" :key="curve.series_set_id" type="button" class="record-button" @click="chooseCanonical(curve, experimentLabel(curve.series_kind))">
            <strong>{{ curveTitle(curve) }}</strong><small>{{ curvePointCount(curve) ? '已提取 ' + curvePointCount(curve) + ' 个数值点' : '已记录曲线信息，尚无数字化点' }}</small>
          </button>
          <p v-if="!runCurves(activeRun).length" class="empty">这个实验没有关联曲线。</p>
          <h3 class="section-heading">关联观测</h3>
          <button v-for="observation in runObservations" :key="observation.observation_id" type="button" class="record-button" @click="chooseCanonical(observation, experimentLabel(observation.observation_type))">
            <strong>{{ observationNames(observation) }} · {{ experimentLabel(observation.observation_type) }}</strong>
            <small><ExperimentText :text="observationSummary(observation)" /></small>
          </button>
          <p v-if="!runObservations.length" class="empty">这个实验没有关联观测。</p>
        </template>
        <template v-else>
          <header class="pane-heading">
            <h2>{{ jsonField ? experimentLabel(jsonField) : jsonView === 'machine' ? '机器原稿' : '当前结构化 JSON' }}</h2>
            <p class="muted">{{ jsonView === 'machine' ? '本次机器提取的原稿。' : '包含本次已生效的人工修改。' }}</p>
            <div class="button-group"><button type="button" class="small-button" :aria-pressed="jsonView === 'machine'" @click="chooseJsonView('machine')">机器原稿</button><button v-if="canViewEffectiveJson" type="button" class="small-button" :aria-pressed="jsonView === 'effective'" @click="chooseJsonView('effective')">当前结果</button></div>
            <div class="button-group"><button type="button" class="small-button" :aria-pressed="jsonMode === 'code'" @click="jsonMode = 'code'">JSON 代码</button><button type="button" class="small-button" :aria-pressed="jsonMode === 'tree'" @click="jsonMode = 'tree'">字段树</button></div>
          </header>
          <a-alert v-if="jsonView === 'machine' && machineError" type="error" show-icon :message="machineError"><template #action><a-button size="small" @click="readMachine">重新读取</a-button></template></a-alert>
          <a-spin v-else-if="jsonView === 'machine' && machineLoading" tip="正在读取机器原稿…" />
          <pre v-else-if="jsonPaper && jsonMode === 'code'" class="json-code">{{ JSON.stringify(jsonValue, null, 2) }}</pre>
          <div v-else-if="jsonPaper" class="field-content"><ExperimentStructureFields :name="jsonField || 'root'" :value="jsonValue" /></div>
        </template>
      </section>

      <aside ref="detailPane" class="paper-pane inspector" aria-label="记录详情">
        <template v-if="view === 'json'">
          <h3>格式版本</h3><dl v-if="jsonPaper" class="facts"><dt>格式标识</dt><dd>{{ jsonPaper.schema_id }}</dd><dt>版本</dt><dd>{{ jsonPaper.schema_version }}</dd><dt>记录编号</dt><dd>{{ jsonPaper.record_id }}</dd></dl>
        </template>
        <template v-else>
          <div class="inspector-heading"><h3>记录详情</h3><div class="button-group"><button type="button" class="small-button" :aria-pressed="detailMode === 'source'" @click="detailMode = 'source'">原文出处</button><button type="button" class="small-button" :aria-pressed="detailMode === 'fields'" @click="detailMode = 'fields'">结构字段</button></div></div>
          <button type="button" class="small-button mobile-return" @click="mainPane?.scrollIntoView({ block: 'start' })">↑ 返回结果</button>
          <template v-if="inspection.object">
            <p v-if="inspection.sample" class="muted">{{ inspection.sample }}</p>
            <h3 class="detail-title"><ExperimentText :text="inspection.heading" /></h3>
            <p v-if="inspection.record?.result"><strong><ExperimentText :text="recordResult(inspection.record)" /></strong></p>
            <p v-if="inspection.record && recordConditions(inspection.record)" class="muted"><ExperimentText :text="recordConditions(inspection.record)" /></p>
            <p v-if="inspection.record && inspection.record.status !== 'machine'" class="record-status">{{ statusLabels[inspection.record.status] }}</p>
            <div v-if="canManage" class="button-group">
              <a-button v-if="inspection.record" type="primary" size="small" @click="openEditor(inspection.record)">修改</a-button>
            </div>
            <template v-if="detailMode === 'fields'">
              <div class="field-content"><ExperimentStructureFields name="record" :value="inspection.object" /><ExperimentStructureFields v-if="inspection.canonicalObject" name="canonical_operation" :value="inspection.canonicalObject" /></div>
            </template>
            <template v-else>
              <div v-if="inspection.sources.length > 1" class="button-group">
                <button v-for="source in inspection.sources" :key="source.evidence_id" type="button" class="small-button" :aria-pressed="activeSource?.evidence_id === source.evidence_id" @click="sourceId = source.evidence_id">{{ sourceLabel(source) }}</button>
              </div>
              <p v-if="inspection.record?.kind === 'measurement' && (!activeSource || sourceHasHighlight === false)" class="empty" role="status">未定位到这条结果的原文出处。请核对数值及所选出处。</p>
              <section v-if="activeSource" class="source-content">
                <p class="source-location"><strong>{{ sourceLabel(activeSource) }}</strong><span v-if="activeSource.printed_page"> · 期刊第 {{ activeSource.printed_page }} 页</span><small>{{ activeSource.location }}</small></p>
                <MarkdownPreview :key="(inspection.record?.record_id || '') + ':' + activeSource.evidence_id" :content="activePreview.content" :highlight-text="activePreview.table ? '' : inspection.record?.result || ''" compact @rendered="onSourceRendered" />
              </section>
              <p v-else-if="inspection.record?.kind !== 'measurement'" class="empty">这条记录未关联可展示的原文出处，可在结构字段中查看完整内容。</p>
            </template>
          </template>
          <p v-else class="empty">选择一条记录后查看原文和结构字段。</p>
        </template>
      </aside>
    </div>
    <a-modal :open="editorOpen" title="编辑记录" :width="1000" :footer="null" :mask-closable="false" :closable="!reviewEditor?.saving" :keyboard="!reviewEditor?.saving" destroy-on-close @cancel="closeEditor">
      <ExperimentReviewWorkspace v-if="editorOpen" ref="reviewEditor" :paper="paper" :snapshot="snapshot" :can-manage="canManage" :save-review="saveReview" editor-only @saved="onSaved" @cancel="closeEditor" />
    </a-modal>
    <a-modal :open="Boolean(entityEdit)" :title="entityTitle" :footer="null" :width="640" :mask-closable="false" :closable="!entityEditor?.saving" :keyboard="!entityEditor?.saving" destroy-on-close @cancel="closeEntity">
      <ExperimentEntityEditor v-if="entityEdit" ref="entityEditor" :kind="entityEdit.kind" :entity="entityEdit.entity" :paper="paper" :snapshot="snapshot" :save-review="saveReview" @saved="onEntitySaved" @cancel="closeEntity" />
    </a-modal>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import ExperimentReviewWorkspace from './ExperimentReviewWorkspace.vue'
import ExperimentCorrectionQueue from './ExperimentCorrectionQueue.vue'
import ExperimentEntityEditor from './ExperimentEntityEditor.vue'
import ExperimentStructureFields from './ExperimentStructureFields.vue'
import ExperimentText from './ExperimentText.vue'
import {
  experimentCategory, experimentEvidenceMatches, experimentEvidencePreview, experimentLabel, experimentMethodFacts, experimentObservationValues,
  experimentStepConditions, experimentUnit, experimentValue
} from '@/utils/experimentPresentation'

const props = defineProps({
  paper: { type: Object, required: true },
  snapshot: { type: Object, required: true },
  effectiveAvailable: { type: Boolean, default: true },
  effectiveJsonAvailable: { type: Boolean, default: true },
  canManage: { type: Boolean, default: false },
  saveReview: { type: Function, required: true },
  loadMachine: { type: Function, required: true }
})

const views = computed(() => ({ ...(props.effectiveAvailable ? { samples: '样品与结果', runs: '实验与曲线' } : {}), review: '人工校对', json: '原始 JSON' }))
const canViewEffectiveJson = computed(() => props.effectiveAvailable && props.effectiveJsonAvailable)
const statusLabels = { machine: '机器草稿', pending: '待确认', edited: '已修改', confirmed: '已人工确认', excluded: '已排除' }
const categoryLabels = { character: '材料性质', adsorption: '吸附与选择性', breakthrough: '穿透与计算' }
const view = ref(props.effectiveAvailable ? 'samples' : 'review')
const selectedSample = ref('')
const sampleMode = ref('results')
const filter = ref('')
const selectedRun = ref('')
const expandedMethod = ref('')
const jsonField = ref('')
const jsonMode = ref('code')
const jsonView = ref('machine')
const machine = ref(null)
const machineLoading = ref(false)
const machineError = ref('')
let machineRequest = 0
const detailMode = ref('source')
const sourceId = ref('')
const sourceHasHighlight = ref(null)
const selection = ref(null)
const searchInput = ref('')
const query = ref('')
const searchSample = ref('')
const editorOpen = ref(false)
const reviewEditor = ref(null)
const entityEdit = ref(null)
const entityEditor = ref(null)
const entityTitle = computed(() => ({ sample_state: entityEdit.value?.entity ? '编辑样品' : '新增样品',
  sample_batch: entityEdit.value?.entity ? '编辑批次' : '新增批次', synthesis_route: '编辑制备路线',
  experiment_run: entityEdit.value?.entity ? '编辑实验' : '补录实验' }[entityEdit.value?.kind] || ''))
const notice = ref('')
const mainPane = ref(null)
const detailPane = ref(null)

const records = computed(() => props.snapshot.review?.records || [])
const pendingCorrectionCount = computed(() => (props.snapshot.review?.tasks || []).filter(task => !task.resolution).length)
const usable = computed(() => records.value.filter(record => ['machine', 'edited', 'confirmed'].includes(record.status)))
const samples = computed(() => props.paper.sample_states)
const sampleOptions = computed(() => new Map((props.snapshot.sample_options || []).map(option => [option.sample_state_id, option])))
const activeSample = computed(() => samples.value.find(sample => sample.sample_state_id === selectedSample.value) || samples.value[0])
const activeSampleId = computed(() => activeSample.value?.sample_state_id || '')
const activeSampleName = computed(() => sampleLabel(activeSampleId.value))
const activeBatch = computed(() => props.paper.sample_batches.find(batch => batch.sample_batch_id === activeSample.value?.sample_batch_id))
const sampleRoute = computed(() => props.paper.synthesis_routes.find(route => route.route_id === activeSample.value?.preparation_route_id))
const sampleMeasurements = computed(() => usable.value.filter(record => record.kind === 'measurement' && hasSample(record, activeSampleId.value)))
const availableCategories = computed(() => [...new Set(sampleMeasurements.value.map(record => experimentCategory(record.test)))].map(key => ({ key, label: categoryLabels[key] })))
const activeRun = computed(() => props.paper.experiment_runs.find(run => run.run_id === selectedRun.value) || props.paper.experiment_runs[0])
const jsonPaper = computed(() => jsonView.value === 'machine' ? machine.value : canViewEffectiveJson.value ? props.paper : null)
const jsonValue = computed(() => jsonField.value ? jsonPaper.value?.[jsonField.value] : jsonPaper.value)
const counts = computed(() => [
  ['样品', 'sample_states'], ['实验', 'experiment_runs'], ['观测', 'observations'], ['曲线', 'series_sets'], ['证据', 'evidence']
].map(([label, field]) => ({ label, field, count: props.paper[field].length })))
const sourceMap = computed(() => new Map((props.snapshot.evidence || []).map(source => [source.evidence_id, {
  ...source, location: 'Markdown 第 ' + source.line_start + (source.line_end !== source.line_start ? '–' + source.line_end : '') + ' 行'
}])))
const canonicalEvidence = computed(() => new Map(props.paper.evidence.map(source => [source.evidence_id, {
  ...source, quote: source.quote_raw, page: source.pdf_page
}])))
const resultValues = computed(() => new Map(props.paper.observations.flatMap(observation =>
  experimentObservationValues(observation).map(item => [item.resultId, item.value]))))
const measurementGroups = computed(() => {
  const groups = new Map()
  for (const record of sampleMeasurements.value) {
    if (filter.value && experimentCategory(record.test) !== filter.value) continue
    const runIds = record.run_id ? [record.run_id] : record.run_ids || []
    const key = runIds.length ? 'run:' + [...runIds].sort().join(',') : 'unlinked'
    if (!groups.has(key)) {
      const runs = props.paper.experiment_runs.filter(item => runIds.includes(item.run_id))
      const run = runs.length === 1 ? runs[0] : null
      groups.set(key, { key, method: runs.map(runTitle).join(' / ') || '未关联实验', numeric: [], written: [], run })
    }
    const group = groups.get(key)
    const value = resultValues.value.get(record.result_id)
    group[value?.kind === 'text' || (!value && !/^\s*(?:[<>≤≥≈~+−-]\s*)?\d/.test(record.result)) ? 'written' : 'numeric'].push(record)
  }
  return [...groups.values()]
})
const searchResults = computed(() => {
  const terms = query.value.normalize('NFKC').toLowerCase().trim().split(/\s+/).filter(Boolean)
  return usable.value.filter(record => record.kind === 'measurement' && (!searchSample.value || hasSample(record, searchSample.value)) &&
    terms.every(term => [recordSampleLabel(record), metricTitle(record), record.metric, recordResult(record), record.test, recordConditions(record)].join(' ').normalize('NFKC').toLowerCase().includes(term)))
})
const runObservations = computed(() => props.paper.observations.filter(observation =>
  observation.run_id === activeRun.value?.run_id || observation.run_ids?.includes(activeRun.value?.run_id)
))
const preparationSteps = computed(() => props.paper.synthesis_routes.flatMap(route => route.operations)
  .filter(operation => operation.applies_to_sample_state_ids?.includes(activeSampleId.value))
  .map(operation => ({ operation, record: stepRecord(operation) })))
const additionalPreparation = computed(() => usable.value.filter(record =>
  record.kind === 'preparation' && !record.operation_id && hasSample(record, activeSampleId.value)))
const inspection = computed(() => {
  if (selection.value?.recordId) {
    const record = usable.value.find(item => item.record_id === selection.value.recordId)
    const operation = preparationSteps.value.find(item => item.operation.operation_id === record?.operation_id)?.operation
    if (record) return { ...recordInspection(record), canonicalObject: operation }
  }
  if (selection.value) return selection.value
  if (view.value === 'samples') {
    if (query.value) return searchResults.value[0] ? recordInspection(searchResults.value[0]) : { sources: [] }
    if (sampleMode.value === 'preparation') {
      if (preparationSteps.value[0]) return stepInspection(preparationSteps.value[0].operation)
      if (additionalPreparation.value[0]) return recordInspection(additionalPreparation.value[0])
      return activeSample.value ? canonicalInspection(activeSample.value, '样品信息') : { sources: [] }
    }
    const record = measurementGroups.value.flatMap(group => [...group.numeric, ...group.written])[0]
    if (record) return recordInspection(record)
    return activeSample.value ? canonicalInspection(activeSample.value, '样品信息') : { sources: [] }
  }
  if (view.value === 'runs' && activeRun.value) return canonicalInspection(activeRun.value, runTitle(activeRun.value))
  return { sources: [] }
})
const activeSource = computed(() => {
  const { sources, record } = inspection.value
  return sources.find(source => source.evidence_id === sourceId.value) ||
    (record?.kind === 'measurement' ? preferredSource(sources.filter(source => experimentEvidenceMatches(source.quote, record))) : null) || preferredSource(sources)
})
const activePreview = computed(() => experimentEvidencePreview(activeSource.value?.quote, inspection.value.record))

watch([() => activePreview.value.content, () => activeSource.value?.evidence_id, () => inspection.value.record?.record_id], () => {
  sourceHasHighlight.value = null
})
watch(() => props.paper, resetSelection)
watch(() => props.effectiveAvailable, available => {
  if (!available && ['samples', 'runs'].includes(view.value)) changeView('review')
})
watch(canViewEffectiveJson, available => {
  if (!available && view.value === 'json' && jsonView.value === 'effective') chooseJsonView('machine')
})
watch(() => [props.snapshot.revision_id, props.snapshot.source_sha256, props.snapshot.review?.version], () => {
  machineRequest += 1
  machine.value = null
  machineLoading.value = false
  machineError.value = ''
  if (view.value === 'json' && jsonView.value === 'machine') readMachine()
})

/** 默认出处和结果行页码共同使用可定位的原表。 */
function preferredSource(sources) {
  return sources.find(source => /^\s*<table\b/i.test(source.quote)) || sources[0]
}

/** 优先显示可定位结果的出处，人工修改后仍保留所选原文。 */
function recordSources(record) {
  return (record.evidence_ids || []).map(id => sourceMap.value.get(id) || canonicalEvidence.value.get(id)).filter(Boolean)
}

/** 根据当前预览的实际高亮反馈定位状态。 */
function onSourceRendered(content) {
  if (content !== activePreview.value.content) return
  sourceHasHighlight.value = Boolean(detailPane.value?.querySelector('.source-content mark'))
  revealSourceResult()
}

/** 宽屏滚动详情面板，窄屏沿实际外层容器显示高亮。 */
function revealSourceResult() {
  if (detailMode.value !== 'source') return
  const pane = detailPane.value
  const mark = pane?.querySelector('.source-content mark')
  if (!mark) return
  if (window.matchMedia('(max-width: 760px)').matches) {
    mark.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    return
  }
  const paneRect = pane.getBoundingClientRect()
  const markRect = mark.getBoundingClientRect()
  if (markRect.top < paneRect.top || markRect.bottom > paneRect.bottom) {
    pane.scrollTop += markRect.top - paneRect.top - pane.clientHeight / 2
  }
  if (markRect.left < paneRect.left || markRect.right > paneRect.right) {
    pane.scrollLeft += markRect.left - paneRect.left - pane.clientWidth / 2
  }
}

/** 按操作和样品身份关联记录，允许重复类型的制备步骤。 */
function stepRecord(operation) {
  return usable.value.find(record => record.kind === 'preparation' && record.operation_id === operation.operation_id && hasSample(record, activeSampleId.value))
}

/** 审核结果和机器原稿分别读取，选择后始终跟随服务端的新值。 */
function recordInspection(record) {
  return { recordId: record.record_id, object: record, record, sample: recordSampleLabel(record),
    heading: metricTitle(record), sources: recordSources(record) }
}

/** 规范记录的出处只使用同一份规范 JSON 中的证据。 */
function canonicalInspection(object, heading) {
  return { object, heading, sample: view.value === 'samples' ? activeSampleName.value : '',
    sources: (object.evidence_ids || []).map(id => canonicalEvidence.value.get(id)).filter(Boolean) }
}

/** 制备详情同时保留人工记录与规范步骤。 */
function stepInspection(operation) {
  const record = stepRecord(operation)
  return record ? { ...recordInspection(record), canonicalObject: operation } : canonicalInspection(operation, experimentLabel(operation.operation_type))
}

/** 同页选择只更新详情，不切换导航。 */
function select(detail) {
  selection.value = detail
  sourceId.value = ''
  detailMode.value = 'source'
  nextTick(() => {
    if (!detailPane.value) return
    detailPane.value.scrollTop = 0
    if (window.matchMedia('(max-width: 760px)').matches) detailPane.value.scrollIntoView({ block: 'start' })
    revealSourceResult()
  })
}

/** 清理跨目录的选择，保持当前样品与视图一致。 */
function resetSelection() {
  selection.value = null
  sourceId.value = ''
  detailMode.value = 'source'
}

/** 页面级切换清除搜索上下文。 */
function changeView(key) {
  view.value = key
  query.value = ''
  resetSelection()
  if (key === 'json' && jsonView.value === 'machine' && !machine.value) readMachine()
}

/** 选择样品时保留正在阅读的结果或制备模式。 */
function chooseSample(id, navigate = false) {
  selectedSample.value = id
  if (navigate) {
    view.value = 'samples'
    sampleMode.value = 'results'
    query.value = ''
  }
  filter.value = ''
  expandedMethod.value = ''
  resetSelection()
}

/** 样品内的制备和测量保持同一页签。 */
function chooseSampleMode(mode) {
  sampleMode.value = mode
  resetSelection()
}

/** 结果分类改变后废弃不在列表中的详情。 */
function chooseFilter(key) {
  filter.value = key
  expandedMethod.value = ''
  resetSelection()
}

/** 就地展开测试条件。 */
function toggleMethod(method) {
  expandedMethod.value = expandedMethod.value === method ? '' : method
}

/** 从审核记录读取可编辑的当前结果。 */
function chooseRecord(record) {
  select(recordInspection(record))
}

/** 在当前样品内查看制备详情。 */
function chooseStep(operation) {
  select(stepInspection(operation))
}

/** 选择结构化记录时保留全量对象。 */
function chooseCanonical(object, heading) {
  select(canonicalInspection(object, heading))
}

/** 实验目录切换后默认查看该次实验。 */
function chooseRun(run) {
  selectedRun.value = run.run_id
  resetSelection()
}

/** 数量入口定位完整 JSON 字段。 */
function showJson(field) {
  if (!canViewEffectiveJson.value) return
  jsonView.value = 'effective'
  changeView('json')
  jsonField.value = field
}

/** 明确切换机器原稿和已合并的当前结果。 */
function chooseJsonView(value) {
  jsonView.value = value
  jsonField.value = ''
  if (value === 'machine' && !machine.value) readMachine()
}

/** 机器原稿按当前快照独立读取，版本切换后丢弃旧响应。 */
async function readMachine() {
  if (machineLoading.value) return
  const request = ++machineRequest
  machineLoading.value = true
  machineError.value = ''
  try {
    const result = await props.loadMachine()
    if (request === machineRequest && result) machine.value = result
  } catch (cause) {
    if (request === machineRequest) machineError.value = cause.message || '机器原稿读取失败，请重试。'
  } finally {
    if (request === machineRequest) machineLoading.value = false
  }
}

/** 搜索当前服务端可用的实验记录。 */
function search() {
  view.value = 'samples'
  query.value = searchInput.value.trim()
  searchSample.value = ''
  resetSelection()
}

/** 清除搜索后回到当前样品。 */
function clearSearch() {
  query.value = ''
  searchInput.value = ''
  resetSelection()
}

/** 搜索范围改变时重置右侧详情。 */
function chooseSearchSample(id) {
  searchSample.value = id
  resetSelection()
}

/** 弹窗复用既有审核表单和真实保存回调。 */
async function openEditor(record = null) {
  if (!props.canManage) return
  editorOpen.value = true
  await nextTick()
  reviewEditor.value?.editRecord(record)
}

/** 补录默认绑定用户正在阅读的样品。 */
function startNew() {
  openEditor({ sample: view.value === 'samples' ? activeSample.value?.reported_name || '' : '',
    sample_state_id: view.value === 'samples' ? activeSampleId.value : null, kind: 'measurement' })
}

/** 保存期间保留表单，避免截断提交结果。 */
function closeEditor() {
  if (!reviewEditor.value?.saving) editorOpen.value = false
}

/** 保存反馈来自服务器，待确认内容留在核对页可继续处理。 */
function onSaved(record) {
  editorOpen.value = false
  notice.value = statusLabels[record.status] + '，重新打开文件后仍可查看。'
  if (record.status === 'pending') {
    changeView('review')
  }
  const current = usable.value.find(item => item.record_id === record.record_id)
  if (current && !query.value) {
    const id = hasSample(current, activeSampleId.value) ? activeSampleId.value : (current.sample_state_id || current.sample_state_ids?.[0])
    if (id) chooseSample(id, true)
    chooseSampleMode(current.kind === 'preparation' ? 'preparation' : 'results')
    chooseRecord(current)
  }
}

/** 元信息编辑只接收当前结构中登记的对象。 */
function openEntity(kind, entity = null) { entityEdit.value = { kind, entity } }

/** 保存期间保留固定表单。 */
function closeEntity() { if (!entityEditor.value?.saving) entityEdit.value = null }

/** 保存后只使用重新读取的实体。 */
function onEntitySaved(result) {
  const added = entityEdit.value?.kind === 'sample_state' && !entityEdit.value.entity
  entityEdit.value = null
  notice.value = added ? '样品已新增。' : '修改已保存。'
  if (added && result.entity_id) chooseSample(result.entity_id, true)
}

/** 样品归属只通过状态编号判断。 */
function hasSample(record, id) {
  return Boolean(id && (record.sample_state_id === id || record.sample_state_ids?.includes(id)))
}

/** 同名样品的状态和批次由服务端标签区分。 */
function sampleLabel(id) {
  const option = sampleOptions.value.get(id)
  return option?.label || samples.value.find(sample => sample.sample_state_id === id)?.reported_name || ''
}

/** 组观察显示实际整组成员，记录原始显示名仅用于出处定位。 */
function recordSampleLabel(record) {
  const ids = record.sample_state_ids?.length ? record.sample_state_ids : [record.sample_state_id]
  return ids.map(sampleLabel).filter(Boolean).join('、') || record.sample
}

/** 样品概览只使用已报告的批次因素。 */
function sampleNote(sample) {
  const factors = props.paper.sample_batches.find(item => item.sample_batch_id === sample?.sample_batch_id)?.factor_assignments || {}
  return Object.entries(factors).filter(([, value]) => value != null)
    .map(([key, value]) => experimentLabel(key) + ' ' + experimentValue(value)).join(' · ')
}

/** 指标单位随标签显示，结果保留原始取值限定词。 */
function metricTitle(record) {
  return experimentUnit(experimentLabel(record.metric)) + (record.metric?.endsWith('_percent') ? '（%）' : '')
}

/** 结果从当前 JSON 的结果编号取得，展示完整数值限定和单位。 */
function recordResult(record) {
  const value = resultValues.value.get(record.result_id)
  return value != null ? experimentValue(value) : experimentUnit(record.result)
}

/** 条件保留人工填写的原文及结构化参数。 */
function recordConditions(record) {
  const parameters = Object.entries(record.parameters || {}).map(([key, value]) => `${experimentLabel(key)} ${experimentValue(value)}`)
  return [...new Set([record.conditions, ...parameters].filter(Boolean))].join(' · ')
}

/** 当前记录的页码来自它关联的证据。 */
function recordPage(record) {
  const sources = recordSources(record)
  const source = preferredSource(record.kind === 'measurement' ? sources.filter(item => experimentEvidenceMatches(item.quote, record)) : sources)
  if (!source && sources.length) return '查看出处'
  if (!source && record.kind === 'measurement') return '未定位出处'
  return source?.page ? 'PDF ' + source.page + ' 页' : '查看出处'
}

/** 实验名称优先显示实际测试技术。 */
function runTitle(run) {
  return experimentLabel(run.technique || run.abbreviation || run.method || run.run_type)
}

/** 实验目录展示可区分条件的摘要。 */
function runNote(run) {
  return [
    run.temperature && experimentValue(run.temperature),
    run.feed?.map(item => chemicalName(item.entity_id) + ' ' + (item.reported_ratio_part ?? item.mole_fraction ?? '')).join(' / '),
    runSampleIds(run).map(sampleLabel).join('、')
  ].filter(Boolean).join(' · ')
}

/** 实验只列出明确引用的样品。 */
function runSampleIds(run) {
  return run?.sample_state_ids || (run?.sample_state_id ? [run.sample_state_id] : [])
}

/** 关联曲线只沿实验登记中的编号读取。 */
function runCurves(run) {
  return (run?.series_set_ids || []).map(id => props.paper.series_sets.find(curve => curve.series_set_id === id)).filter(Boolean)
}

/** 曲线名称带上原图编号。 */
function curveTitle(curve) {
  return [experimentLabel(curve.series_kind), curve.source?.figure].filter(Boolean).join(' · ')
}

/** 数字化点计数只统计实际存在的数据点。 */
function curvePointCount(curve) {
  return (curve.series_members || []).reduce((sum, member) => sum + (Array.isArray(member.data_points) ? member.data_points.length : 0), 0)
}

/** 观测归属只沿显式样品编号读取。 */
function observationNames(observation) {
  return recordSampleLabel(observation) || '未关联样品'
}

/** 不同观测变体使用相同的完整结果读取规则。 */
function observationSummary(observation) {
  const values = experimentObservationValues(observation)
  return values.length ? values.map(item => experimentLabel(item.metric) + ' ' + experimentValue(item.value)).join(' · ') :
    '在结构字段中查看这条观测的完整内容。'
}

/** 化学物质展示名称来自规范登记。 */
function chemicalName(id) {
  const chemical = props.paper.chemical_entities.find(item => item.entity_id === id)
  return chemical?.reported_name || id
}

/** 用料比例读取当前样品批次，避免套用其他样品。 */
function reagentText(operation) {
  return (operation.reagent_uses || []).map(usage => {
    const amount = usage.amount || usage.concentration
    const ratio = usage.ratio?.per_sample_values?.[activeSample.value?.sample_batch_id]
    return [chemicalName(usage.entity_id), amount && experimentValue(amount), ratio != null && '用量比 ' + ratio].filter(Boolean).join(' ')
  }).join(' · ')
}

/** 出处切换按钮清楚区分原表与正文。 */
function sourceLabel(source) {
  const kind = /^\s*<table\b/i.test(source.quote) ? '原表' : source.extraction_origin === 'figure' ? '图表说明' : '原文'
  return kind + (source.page ? ' · PDF 第 ' + source.page + ' 页' : '')
}
</script>

<style scoped lang="less">
.canonical-workspace { display: flex; flex: 1; flex-direction: column; min-height: 0; color: var(--color-text); }
.paper-counts, .paper-nav, .paper-search, .button-group, .inspector-heading, .group-heading { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.paper-counts { gap: 0; padding: 4px 0 12px; }
.paper-counts button { padding: 0 14px; border: 0; border-right: 1px solid var(--gray-150); background: transparent; color: var(--color-text-secondary); cursor: pointer; }
.paper-counts button:first-child { padding-left: 0; }
.paper-counts button:last-child { border-right: 0; }
.paper-counts strong { margin-right: 4px; color: var(--color-text); font-size: 18px; }
.paper-nav { flex-wrap: nowrap; flex-shrink: 0; gap: 18px; overflow-x: auto; border-block: 1px solid var(--gray-150); }
.paper-nav button { padding: 10px 0; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--color-text-secondary); white-space: nowrap; cursor: pointer; }
.paper-nav button[aria-pressed='true'] { border-bottom-color: var(--main-color); color: var(--main-color); font-weight: 600; }
.paper-search { flex-shrink: 0; padding: 10px 0; }
.paper-search .ant-input-affix-wrapper { flex: 1; max-width: 540px; min-width: 160px; }
.notice { margin: 0 0 8px; color: var(--main-color); }
.paper-columns { display: grid; flex: 1; min-height: 0; gap: 12px; grid-template-columns: minmax(146px, .75fr) minmax(0, 2.6fr) minmax(250px, 1.5fr); }
.paper-pane { min-width: 0; min-height: 0; overflow: auto; overscroll-behavior: contain; border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-0); overflow-wrap: anywhere; }
.paper-pane h2, .paper-pane h3, .paper-pane p { margin: 0; }
.paper-pane h2 { font-size: 22px; }
.paper-pane h3 { font-size: 14px; }
.directory { display: flex; flex-direction: column; padding: 10px; gap: 4px; }
.directory h3 { padding: 2px 4px 10px; }
.directory > button { padding: 8px; border: 1px solid transparent; border-radius: 5px; background: transparent; text-align: left; color: var(--color-text); cursor: pointer; }
.directory > button[aria-pressed='true'] { background: var(--main-50); border-color: var(--main-color); }
small { display: block; margin-top: 3px; color: var(--color-text-secondary); font-size: 12px; font-weight: 400; }
.count { float: right; margin-left: 8px; color: var(--color-text-secondary); font-size: 12px; }
.pane-heading { padding: 14px; border-bottom: 1px solid var(--gray-150); }
.pane-heading .muted { margin-top: 3px; }
.pane-heading > .ant-btn { margin-top: 10px; }
.button-group { margin-top: 10px; gap: 6px; }
.small-button { padding: 4px 7px; border: 1px solid var(--gray-150); border-radius: 5px; background: var(--gray-0); color: var(--color-text-secondary); font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.small-button[aria-pressed='true'] { border-color: var(--main-color); background: var(--main-50); color: var(--main-color); }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--main-color); outline-offset: 2px; }
.directory > button:hover, .small-button:hover, .record-button:hover, .step-row:hover, .result-row:hover { background: var(--gray-25); }
.result-head, .result-row { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1.1fr) minmax(62px, .55fr); gap: 10px; align-items: center; padding: 9px 12px; }
.result-head { color: var(--color-text-secondary); font-size: 12px; }
.result-row { width: 100%; border: 0; border-bottom: 1px solid var(--gray-150); background: transparent; color: var(--color-text); font: inherit; text-align: left; cursor: pointer; }
.result-row[aria-pressed='true'], .step-row[aria-pressed='true'] { background: var(--main-50); box-shadow: inset 3px 0 var(--main-color); }
.group-heading { justify-content: space-between; padding: 7px 12px; background: var(--gray-25); border-block: 1px solid var(--gray-150); color: var(--main-color); }
.group-heading strong { font-size: 12px; }
.method-details { padding: 10px 12px; border-bottom: 1px solid var(--gray-150); }
.method-details > section + section { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--gray-150); }
.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 5px 10px; margin: 10px 0 0; font-size: 12px; }
.facts dt, .muted { color: var(--color-text-secondary); }
.facts dd { margin: 0; }
.written-results summary { padding: 8px 12px; border-bottom: 1px solid var(--gray-150); color: var(--color-text-secondary); cursor: pointer; }
.step-row { display: flex; gap: 10px; width: 100%; padding: 12px; border: 0; border-bottom: 1px solid var(--gray-150); background: transparent; color: var(--color-text); font: inherit; text-align: left; cursor: pointer; }
.step-number { flex-shrink: 0; color: var(--main-color); font-weight: 600; }
.record-button { display: block; width: 100%; padding: 12px; border: 0; border-bottom: 1px solid var(--gray-150); background: transparent; color: var(--color-text); text-align: left; cursor: pointer; }
.section-heading { padding: 12px; border-bottom: 1px solid var(--gray-150); }
.empty { padding: 16px 12px; color: var(--color-text-secondary); }
.inspector { padding: 12px; }
.inspector-heading { justify-content: space-between; padding-bottom: 12px; border-bottom: 1px solid var(--gray-150); }
.inspector-heading .button-group { margin-top: 0; }
.detail-title { margin-top: 10px !important; margin-bottom: 6px !important; }
.record-status { margin-top: 6px !important; color: var(--main-color); font-size: 12px; }
.source-content { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--gray-150); }
.source-location { margin-bottom: 8px !important; font-size: 12px; }
.source-content :deep(mark), .source-content :deep(.markdown-text-highlight) { color: inherit; background: var(--color-warning-100); }
.source-content :deep(table) { font-size: 12px; }
.field-content { padding: 10px; }
.json-code { margin: 0; padding: 14px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--color-text); font-size: 12px; }
.mobile-return { display: none; }
@media (max-width: 1100px) {
  .paper-columns { grid-template-columns: minmax(130px, .65fr) minmax(0, 1.7fr) minmax(235px, 1.2fr); gap: 8px; }
}
@media (max-width: 900px) {
  .paper-columns { grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .directory { grid-column: 1 / -1; max-height: 150px; }
}
@media (max-width: 760px) {
  .canonical-workspace { overflow: auto; }
  .paper-columns { display: flex; flex: initial; flex-direction: column; }
  .paper-pane { overflow: visible; }
  .directory { overflow: auto; }
  .mobile-return { display: inline-block; margin-top: 10px; }
  .result-head, .result-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 62px; gap: 6px; }
}
</style>
