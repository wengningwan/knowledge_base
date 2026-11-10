<template>
  <details v-if="value !== null && typeof value === 'object'" class="structure-branch">
    <summary><code>{{ name }}</code><span>{{ Array.isArray(value) ? '列表' : '对象' }} · {{ Object.keys(value).length }}</span></summary>
    <div class="structure-children">
      <ExperimentStructureFields v-for="(item, key) in value" :key="key" :name="String(key)" :value="item" />
    </div>
  </details>
  <div v-else class="structure-leaf"><code>{{ name }}</code><span>{{ JSON.stringify(value) }}</span></div>
</template>

<script setup>
defineProps({
  name: { type: String, required: true },
  value: { default: null }
})
</script>

<style scoped lang="less">
.structure-branch { padding: 5px 0; }
summary { cursor: pointer; overflow-wrap: anywhere; }
summary > span { margin-left: 6px; color: var(--color-text-secondary); font-size: 12px; }
.structure-children { margin: 6px 0 0 8px; padding-left: 10px; border-left: 1px solid var(--gray-150); }
.structure-leaf { display: grid; grid-template-columns: minmax(90px, .8fr) minmax(0, 1.2fr); gap: 8px; padding: 5px 0; border-bottom: 1px solid var(--gray-100); }
code, .structure-leaf span { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
