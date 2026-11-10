<template>
  <span class="experiment-text" v-html="experimentTextHtml(text)"></span>
</template>

<script setup>
import { experimentTextHtml } from '@/utils/experimentPresentation'
import 'katex/dist/katex.min.css'

defineProps({ text: { type: [String, Number], default: '' } })
</script>

<style scoped lang="less">
.experiment-text { overflow-wrap: anywhere; }
.experiment-text :deep(.katex) { font-size: 1em; }
.experiment-text :deep(.katex-display) { max-width: 100%; margin: 4px 0; overflow-x: auto; overflow-y: hidden; }
</style>
