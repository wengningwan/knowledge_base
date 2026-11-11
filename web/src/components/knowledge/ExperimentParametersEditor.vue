<template>
  <div class="parameter-editor">
    <div v-for="(row, index) in rows" :key="row.id" class="parameter-row">
      <label>条件名称<a-input v-model:value="row.name" :disabled="disabled" @change="publish" /></label>
      <label>原文值<a-input v-model:value="row.raw_text" :disabled="disabled" @change="publish" /></label>
      <label>数值（可空）<a-input-number v-model:value="row.value" :disabled="disabled" @change="publish" /></label>
      <label>单位（可空）<a-input v-model:value="row.unit" :disabled="disabled" @change="publish" /></label>
      <a-button :disabled="disabled" aria-label="删除这条条件" @click="remove(index)">删除</a-button>
    </div>
    <p v-if="error" class="parameter-error" role="alert">{{ error }}</p>
    <a-button size="small" :disabled="disabled" @click="add">添加条件</a-button>
  </div>
</template>

<script setup>
import { ref, toRaw, watch } from 'vue'
const props = defineProps({ modelValue: { type: Object, default: () => ({}) }, disabled: Boolean })
const emit = defineEmits(['update:modelValue', 'validity'])
const rows = ref([])
const error = ref('')
let nextId = 0
let published = null
watch(() => props.modelValue, value => {
  if (toRaw(value) === published) return
  rows.value = Object.entries(value || {}).map(([name, item]) => ({ id: ++nextId, name,
    raw_text: item?.raw_text ?? '', value: item?.value ?? null, unit: item?.unit ?? '' }))
  error.value = ''
  emit('validity', true)
}, { immediate: true })

/** 条件名称唯一；空数值和空单位保留为明确缺失。 */
function publish() {
  const names = rows.value.map(row => row.name.trim())
  error.value = names.some(name => !name) || rows.value.some(row => !row.raw_text.trim()) ? '请填写每条条件的名称和原文值。' :
    new Set(names).size !== names.length ? '条件名称不能重复。' : ''
  emit('validity', !error.value)
  if (error.value) return
  published = Object.fromEntries(rows.value.map(row => [row.name.trim(), {
    raw_text: row.raw_text, value: row.value ?? null, unit: row.unit.trim() || null
  }]))
  emit('update:modelValue', published)
}

/** 新行等待填写后再发布，避免覆盖已有条件。 */
function add() {
  rows.value.push({ id: ++nextId, name: '', raw_text: '', value: null, unit: '' })
  publish()
}

/** 删除条件后同步明确的剩余集合。 */
function remove(index) {
  rows.value.splice(index, 1)
  publish()
}
</script>

<style scoped lang="less">
.parameter-editor { display: grid; gap: 8px; }
.parameter-row { display: grid; grid-template-columns: 1fr 1.5fr 1fr 1fr auto; gap: 8px; align-items: end; }
label { display: grid; gap: 4px; min-width: 0; color: var(--color-text-secondary); font-size: 12px; }
.ant-input-number { width: 100%; }
.parameter-error { margin: 0; color: var(--color-error-700); }
.parameter-editor > .ant-btn { justify-self: start; }
@media (max-width: 760px) { .parameter-row { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
