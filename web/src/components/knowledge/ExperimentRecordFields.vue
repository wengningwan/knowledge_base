<template>
  <div class="record-edit-fields">
    <label>记录类型<a-select :value="modelValue.kind" :options="kindOptions" :disabled="disabled || group || Boolean(allowedSampleIds)" @update:value="update('kind', $event)" /></label>
    <label v-if="!group">归属样品<a-select :value="modelValue.sample_state_id || undefined" :options="sampleOptions" show-search option-filter-prop="label" :disabled="disabled" placeholder="选择已登记样品" @update:value="chooseSample" /></label>
    <label v-else class="full-field">整组观察的全部样品
      <a-select :value="modelValue.sample_state_ids || []" :options="sampleOptions" mode="multiple" disabled />
      <span>本次修改作用于以上全部样品。</span>
    </label>
    <label>操作或指标<a-input :value="modelValue.metric" :maxlength="1000" :disabled="disabled" @update:value="update('metric', $event)" /></label>
    <label v-if="!linkedRunIds.length">测试方法<a-input :value="modelValue.test" :maxlength="1000" :disabled="disabled || Boolean(modelValue.run_id)" @update:value="update('test', $event)" /></label>
    <template v-if="modelValue.kind === 'measurement'">
      <label v-if="linkedRunIds.length">所属实验<a-select :value="linkedRunIds" :options="runOptions" mode="multiple" disabled /></label>
      <template v-else>
        <label>所属实验<a-select :value="modelValue.run_id || undefined" :options="runOptions" allow-clear :disabled="disabled" placeholder="选择实验，或明确缺失原因" @update:value="chooseRun" /></label>
        <label v-if="!modelValue.run_id">实验关联缺失原因<a-select :value="modelValue.run_missing_reason" :options="missingOptions" :disabled="disabled" @update:value="update('run_missing_reason', $event)" /></label>
        <p v-if="!modelValue.run_id && modelValue.run_missing_reason === 'unresolved'" class="full-field muted">实验关联尚未确定，保存后仍留在待处理区。</p>
      </template>
      <label class="full-field">结果（保留原文限定词与单位）<a-input :value="modelValue.result" :maxlength="1000" :disabled="disabled" @update:value="update('result', $event)" /></label>
    </template>
    <label class="full-field">条件原文<a-textarea :value="modelValue.conditions" :maxlength="16000" :auto-size="{ minRows: 2 }" :disabled="disabled" @update:value="update('conditions', $event)" /></label>
    <section class="full-field"><h4>条件参数</h4><ExperimentParametersEditor :model-value="modelValue.parameters || {}" :disabled="disabled" @update:model-value="update('parameters', $event)" @validity="$emit('validity', $event)" /></section>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { experimentLabel, experimentMethodFacts } from '@/utils/experimentPresentation'
import ExperimentParametersEditor from './ExperimentParametersEditor.vue'
const props = defineProps({ modelValue: { type: Object, required: true }, snapshot: { type: Object, required: true },
  paper: { type: Object, default: () => ({}) }, allowedSampleIds: { type: Array, default: null },
  linkedRunIds: { type: Array, default: () => [] }, group: Boolean, disabled: Boolean })
const emit = defineEmits(['update:modelValue', 'validity'])
const kindOptions = [{ value: 'measurement', label: '测量' }, { value: 'preparation', label: '制备' }]
const missingOptions = [{ value: 'not_reported', label: '原文未报告实验关联' }, { value: 'unresolved', label: '实验关联尚未确定' }]
const sampleOptions = computed(() => (props.snapshot.sample_options || []).filter(sample =>
  !props.allowedSampleIds || props.allowedSampleIds.includes(sample.sample_state_id)).map(sample => ({
  value: sample.sample_state_id, label: sample.label || [sample.reported_name, sample.state, sample.sample_batch_id].filter(Boolean).join(' · ')
})))
const runOptions = computed(() => {
  // 推导实验及计算实验有各自的上游/对象契约，最终归属由服务端沿真实引用校验。
  const options = (props.paper.experiment_runs || []).map(run => ({ value: run.run_id,
    label: [experimentLabel(run.technique || run.method || run.run_type), ...experimentMethodFacts(run)
      .filter(([label]) => ['测试温度', '测试压力'].includes(label)).map(([label, value]) => `${label} ${value}`)].join(' · ') }))
  return options.map(option => ({ ...option, label: options.some(other => other.value !== option.value && other.label === option.label)
    ? `${option.label} · ${option.value}` : option.label }))
})

/** 固定表单只提交可编辑字段，目标身份由服务端记录固定。 */
function update(key, value) { emit('update:modelValue', { ...props.modelValue, [key]: value }) }

/** 显示名称跟随所选身份，不参与归属匹配。 */
function chooseSample(id) {
  const sample = (props.snapshot.sample_options || []).find(item => item.sample_state_id === id)
  const run = (props.paper.experiment_runs || []).find(item => item.run_id === props.modelValue.run_id)
  const members = run?.sample_state_ids || (run?.sample_state_id ? [run.sample_state_id] : null)
  const stillApplies = !members || members.includes(id)
  emit('update:modelValue', { ...props.modelValue, sample_state_id: id, sample_state_ids: [], sample: sample?.reported_name || '',
    ...(stillApplies ? {} : { run_id: null, run_missing_reason: 'unresolved' }) })
}

/** 清空实验时显式要求待处理，直到用户说明原文未报告。 */
function chooseRun(id) {
  const run = (props.paper.experiment_runs || []).find(item => item.run_id === id)
  emit('update:modelValue', { ...props.modelValue, run_id: id || null, run_missing_reason: id ? null : 'unresolved',
    ...(run ? { test: run.method || run.measurement_method || run.technique || run.run_type } : {}) })
}
</script>

<style scoped lang="less">
.record-edit-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
label { display: grid; gap: 4px; min-width: 0; }
h4 { margin: 0 0 6px; font-size: 13px; }
.full-field { grid-column: 1 / -1; }
.muted, label > span { margin: 0; color: var(--color-text-secondary); font-size: 12px; }
@media (max-width: 620px) { .record-edit-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
