<template>
  <div class="chunk-preview-panel">
    <div class="chunk-grid">
      <article
        v-for="(entry, index) in entries"
        :key="index"
        class="chunk-card"
        :class="{ 'is-expanded': expanded.has(index) }"
      >
        <div class="chunk-card-header">
          <span class="chunk-order">分块 #{{ entry.chunk.chunk_order_index ?? index }}</span>
          <button
            type="button"
            class="chunk-action"
            :aria-expanded="expanded.has(index)"
            :aria-controls="`chunk-content-${index}`"
            @click="toggleExpanded(index)"
          >{{ expanded.has(index) ? '收起全文' : '展开全文' }}</button>
        </div>
        <p v-if="entry.source.section" class="chunk-section">{{ entry.source.section }}</p>
        <div
          :id="`chunk-content-${index}`"
          class="chunk-card-content"
          :class="{ 'is-collapsed': !expanded.has(index) }"
        >{{ entry.content }}</div>
        <div class="chunk-source">
          <span>{{ entry.source.label }}</span>
          <button
            v-if="entry.source.status === 'located'"
            type="button"
            class="chunk-action"
            :aria-label="`查看分块 ${entry.chunk.chunk_order_index ?? index} 的原文位置`"
            @click="selectedIndex = index"
          >查看原文位置</button>
          <template v-if="canOpenPdf && entry.source.status === 'located'">
            <button
              v-for="page in entry.source.pages"
              :key="page"
              type="button"
              class="chunk-action"
              :aria-label="`查看 PDF 第 ${page} 页`"
              @click="emit('open-pdf', page)"
            >查看 PDF 第 {{ page }} 页</button>
          </template>
        </div>
        <p v-for="notice in entry.source.notices" :key="notice" class="chunk-notice">{{ notice }}</p>
      </article>
    </div>
    <a-modal
      :open="selectedIndex !== null"
      :title="`分块 #${selectedEntry?.chunk.chunk_order_index ?? selectedIndex} 的原文位置`"
      width="760px"
      :footer="null"
      wrap-class-name="chunk-source-dialog"
      @cancel="selectedIndex = null"
    >
      <p class="source-help">以下为解析后的 Markdown 原文。高亮行为实际来源，其余行为上下文，各个来源范围分别展示。</p>
      <div class="source-windows">
        <section v-for="(window, index) in sourceWindows" :key="index" class="source-window">
          <h3 class="source-window-title">
            来源 {{ index + 1 }} · 原文第 {{ window.span.line_start }}<template v-if="window.span.line_end !== window.span.line_start">–{{ window.span.line_end }}</template> 行<template v-if="window.span.page > 0"> · PDF 第 {{ window.span.page }} 页</template>
          </h3>
          <div
            v-for="line in window.lines"
            :key="line.number"
            class="source-line"
            :class="{ 'is-highlighted': line.highlighted }"
            :data-source-line="line.highlighted ? line.number : null"
          >
            <span class="source-line-number" aria-hidden="true">{{ line.number }}</span>
            <span class="source-line-text">{{ line.text || '\u00a0' }}</span>
          </div>
        </section>
      </div>
    </a-modal>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import {
  createChunkSourceDocument, describeChunkSource, chunkSourceWindows, sortedPreviewChunks, chunkPreviewContent
} from '@/utils/chunkPreview'

const props = defineProps({
  chunks: { type: Array, default: () => [] },
  sourceContent: { type: String, default: '' },
  sourceSha256: { type: String, default: '' },
  sourceError: { type: String, default: '' },
  canOpenPdf: { type: Boolean, default: false }
})
const emit = defineEmits(['open-pdf'])

const expanded = ref(new Set())
const selectedIndex = ref(null)
const source = computed(() => createChunkSourceDocument(props.sourceContent, props.sourceSha256, props.sourceError))
const entries = computed(() => sortedPreviewChunks(props.chunks).map((chunk) => ({
  chunk, content: chunkPreviewContent(chunk), source: describeChunkSource(chunk, source.value)
})))
const selectedEntry = computed(() => selectedIndex.value === null ? null : entries.value[selectedIndex.value])
const sourceWindows = computed(() => chunkSourceWindows(selectedEntry.value?.source.spans || [], source.value))

/** 原文或事实块更新后，不沿用上一版的审查窗口。 */
watch([() => props.chunks, source], () => {
  expanded.value = new Set()
  selectedIndex.value = null
})

/** 切换一个分块的完整正文。 */
function toggleExpanded(index) {
  const next = new Set(expanded.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  expanded.value = next
}
</script>

<style scoped lang="less">
.chunk-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
  gap: 12px;
  align-items: start;
}
.chunk-card {
  min-width: 0;
  background: var(--gray-0);
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 16px;

  &.is-expanded { grid-column: 1 / -1; }
}
.chunk-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.chunk-order { font-size: 13px; font-weight: 600; color: var(--color-text); }
.chunk-action {
  flex: 0 0 auto;
  border: 0;
  border-radius: 4px;
  padding: 4px 0;
  background: transparent;
  color: var(--main-color);
  font: inherit;
  font-size: 13px;
  cursor: pointer;

  &:hover { text-decoration: underline; }
  &:focus-visible { outline: 2px solid var(--main-color); outline-offset: 3px; }
}
.chunk-section { margin: 0 0 8px; color: var(--color-text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.chunk-card-content {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 14px;
  line-height: 1.7;
  color: var(--color-text);

  &.is-collapsed {
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
  }
}
.chunk-source { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; margin-top: 12px; color: var(--color-text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.chunk-notice { margin: 8px 0 0; padding: 6px 8px; border-radius: 4px; background: var(--color-warning-50); color: var(--color-warning-900); font-size: 12px; }
.source-help { color: var(--color-text-secondary); font-size: 13px; }
.source-windows { max-height: 60vh; overflow-y: auto; }
.source-window { margin-bottom: 16px; border: 1px solid var(--gray-150); border-radius: 6px; overflow: hidden; }
.source-window-title { margin: 0; padding: 8px 12px; background: var(--gray-25); color: var(--color-text-secondary); font-size: 12px; font-weight: 500; }
.source-line { display: flex; gap: 12px; padding: 2px 12px; color: var(--color-text-secondary); font-family: monospace; font-size: 13px; line-height: 1.6; }
.source-line.is-highlighted { background: var(--main-10); color: var(--color-text); border-left: 3px solid var(--main-color); padding-left: 9px; }
.source-line-number { flex: 0 0 3ch; text-align: right; user-select: none; color: var(--color-text-tertiary); }
.source-line-text { min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .chunk-action { min-height: 40px; }
  .chunk-card { padding: 12px; }
}
</style>
