<template>
  <div class="entity-editor">
    <template v-if="kind === 'sample_state'">
      <label>样品名称<a-input v-model:value="fields.reported_name" :disabled="saving" :maxlength="1000" /></label>
      <label>样品状态<a-input v-model:value="fields.state" :disabled="saving" :maxlength="1000" /></label>
      <label>形态（可空）<a-input v-model:value="fields.physical_form" :disabled="saving" :maxlength="1000" /></label>
      <label>所属批次（可空）<a-select v-model:value="fields.sample_batch_id" :options="batchOptions" :disabled="saving" allow-clear /></label>
      <template v-if="!entity">
        <label>制备路线（可空）<a-select v-model:value="fields.preparation_route_id" :options="routeOptions" :disabled="saving" allow-clear /></label>
      </template>
    </template>
    <template v-else-if="kind === 'sample_batch'">
      <label>批次名称<a-input v-model:value="fields.reported_name" :disabled="saving" :maxlength="1000" /></label>
      <label>批号（可空，保留前导零）<a-input v-model:value="fields.batch_number" :disabled="saving" :maxlength="1000" /></label>
    </template>
    <template v-else-if="kind === 'synthesis_route'">
      <label>产率原文（可空）<a-input v-model:value="fields.yield_text" :disabled="saving" :maxlength="1000" /></label>
      <label>重复次数（可空）<a-input-number v-model:value="fields.replicate_count" :disabled="saving" :min="1" :precision="0" /></label>
    </template>
    <template v-else-if="kind === 'experiment_run'">
      <template v-if="!entity || entity.run_type === 'reported_experiment'">
        <label>实验类型<a-select v-model:value="fields.experiment_type" :options="experimentTypeOptions" :disabled="saving" /></label>
        <label>归属样品<a-select v-model:value="fields.sample_state_ids" :options="sampleOptions" :disabled="saving" mode="multiple" /></label>
        <label>被测物种（原文未报告时可空）<a-select v-model:value="fields.species_entity_ids" :options="speciesOptions" :disabled="saving" mode="multiple" /></label>
      </template>
      <label>测试方法<a-input v-model:value="fields.method" :disabled="saving" :maxlength="1000" /></label>
      <section><h4>实验条件</h4><ExperimentParametersEditor v-model="fields.parameters" :disabled="saving" @update:model-value="parametersChanged = true" @validity="parametersValid = $event" /></section>
    </template>
    <label>原文出处<a-select v-model:value="fields.evidence_ids" :options="sourceOptions" :disabled="saving" mode="multiple" show-search option-filter-prop="label" /></label>
    <a-alert v-if="error" type="error" show-icon :message="error" />
    <div class="entity-actions"><a-button type="primary" :loading="saving" :disabled="!parametersValid" @click="save">{{ entity ? '保存修改' : '保存登记' }}</a-button><a-button :disabled="saving" @click="$emit('cancel')">取消</a-button></div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { experimentValue } from '@/utils/experimentPresentation'
import ExperimentParametersEditor from './ExperimentParametersEditor.vue'
const props = defineProps({ kind: { type: String, required: true }, entity: { type: Object, default: null },
  paper: { type: Object, required: true }, snapshot: { type: Object, required: true }, saveReview: { type: Function, required: true } })
const emit = defineEmits(['saved', 'cancel'])
const saving = ref(false)
const error = ref('')
const parametersValid = ref(true)
const parametersChanged = ref(false)
const reviewEvidenceIds = ids => ids.map(id => (props.snapshot.evidence || []).find(item =>
  item.evidence_id === id || item.canonical_evidence_id === id)?.evidence_id).filter(Boolean)
const originalConditions = {
  ...(props.entity?.conditions || {}),
  ...Object.fromEntries(['temperature', 'pressure', 'pressure_domain', 'loading_domain', 'condition_context'].filter(key => props.entity?.[key] != null)
    .map(key => [key, props.entity[key]])),
  ...(props.entity?.temperatures?.length === 1 ? { temperature: props.entity.temperatures[0] } : {}),
  ...(props.entity?.probe?.temperature ? { temperature: props.entity.probe.temperature } : {})
}
const initialParameters = Object.fromEntries(Object.entries(originalConditions).map(([key, value]) => [key, {
  raw_text: experimentValue(value), value: value?.kind === 'scalar' ? value.value : null,
  unit: value?.unit_raw || value?.unit || null
}]))
const fields = ref({ reported_name: props.entity?.reported_name ?? '', state: props.entity?.state ?? '',
  physical_form: props.entity?.physical_form ?? '', batch_number: props.entity?.batch_number ?? '',
  yield_text: props.entity?.yield_text ?? props.entity?.yield?.raw_text ?? '', replicate_count: props.entity?.replicate_count ?? null,
  method: props.entity?.method ?? props.entity?.measurement_method ?? props.entity?.technique ?? '',
  experiment_type: props.entity?.experiment_type || 'measurement',
  sample_state_ids: [...(props.entity?.sample_state_ids || [])], species_entity_ids: [...(props.entity?.species_entity_ids || [])],
  parameters: initialParameters,
  sample_batch_id: props.entity?.sample_batch_id || null, preparation_route_id: null,
  evidence_ids: reviewEvidenceIds(props.entity?.evidence_ids || []) })
const batchOptions = computed(() => props.paper.sample_batches.map(item => ({ value: item.sample_batch_id,
  label: [item.reported_name, item.batch_number, item.sample_batch_id].filter(Boolean).join(' · ') })))
const routeOptions = computed(() => props.paper.synthesis_routes.map(item => ({ value: item.route_id, label: item.route_name || item.route_id })))
const sampleOptions = computed(() => (props.snapshot.sample_options || []).map(item => ({ value: item.sample_state_id, label: item.label || item.reported_name })))
const speciesOptions = computed(() => props.paper.chemical_entities.map(item => ({ value: item.entity_id, label: item.reported_name + ' · ' + item.entity_id })))
const experimentTypeOptions = [{ value: 'measurement', label: '实测或表征' }, { value: 'fitting', label: '拟合' },
  { value: 'prediction', label: '预测' }, { value: 'derivation', label: '推导' }, { value: 'computation', label: '计算' }]
const sourceOptions = computed(() => (props.snapshot.evidence || []).map(item => ({ value: item.evidence_id,
  label: (item.page ? 'PDF ' + item.page + ' 页 · ' : '') + item.quote.replace(/<[^>]*>/g, ' ').slice(0, 110) })))

/** 固定实体分别提交允许的字段；空值不转为零或默认次数。 */
async function save() {
  if (saving.value || !parametersValid.value) return
  const value = fields.value
  const nullable = text => text.trim() || null
  let change
  if (props.kind === 'sample_state') {
    change = { reported_name: value.reported_name.trim(), state: value.state.trim(), physical_form: nullable(value.physical_form),
      sample_batch_id: value.sample_batch_id || null }
    if (!props.entity) change.preparation_route_id = value.preparation_route_id || null
  } else if (props.kind === 'sample_batch') change = { reported_name: value.reported_name.trim(), batch_number: nullable(value.batch_number) }
  else if (props.kind === 'synthesis_route') change = { yield_text: nullable(value.yield_text), replicate_count: value.replicate_count ?? null }
  else change = { method: nullable(value.method), ...(parametersChanged.value ? { parameters: value.parameters } : {}) }
  if (props.kind === 'experiment_run' && (!props.entity || props.entity.run_type === 'reported_experiment')) {
    Object.assign(change, { experiment_type: value.experiment_type, sample_state_ids: [...value.sample_state_ids], species_entity_ids: [...value.species_entity_ids] })
  }
  saving.value = true
  error.value = ''
  try {
    const entityId = props.entity?.sample_state_id || props.entity?.sample_batch_id || props.entity?.route_id || props.entity?.run_id || null
    const result = await props.saveReview({ action: 'save', entity_change: { kind: props.kind, entity_id: entityId, ...change, evidence_ids: [...value.evidence_ids] } })
    if (result) emit('saved', result)
  } catch (cause) { error.value = cause.message || '保存失败，请重试。' }
  finally { saving.value = false }
}
defineExpose({ saving })
</script>

<style scoped lang="less">
.entity-editor, label { display: grid; gap: 8px; }
.entity-editor { gap: 14px; }
label { gap: 4px; }
h4 { margin: 0 0 6px; }
.entity-actions { display: flex; gap: 8px; }
.ant-input-number { width: 100%; }
</style>
