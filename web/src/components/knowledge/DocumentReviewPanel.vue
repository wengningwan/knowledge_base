<template>
  <section class="parse-assets-panel" aria-label="校对结果">
    <a-spin v-if="state.loading" tip="正在加载校对结果..." />
    <a-alert v-else-if="state.error" type="error" :message="state.error" show-icon />
    <a-empty v-else-if="state.legacy_without_assets" description="此文件尚无独立解析产物，请重新解析生成图片解析和校对结果。" />
    <template v-else>
      <a-alert :type="state.metadata.review_status === 'needs_review' ? 'warning' : 'info'" :message="reviewSummary" show-icon />
      <p class="panel-description">校对记录独立展示；关键修改以醒目标记提示，诊断信息不写入论文正文。</p>
      <a-empty v-if="!state.reviews.length" description="本次解析没有校对记录。" />
      <article v-for="(review, index) in state.reviews" :key="`${review.id || review.block_id || 'review'}-${index}`" class="asset-card" :class="{ critical: review.critical }">
        <header>
          <strong>{{ statusLabel(review.status) }}</strong>
          <a-tag v-if="review.source === 'rule'">规则修复</a-tag>
          <a-tag v-else-if="review.source === 'model'">模型复核</a-tag>
          <a-tag v-if="review.classification === 'excluded'">出版元素</a-tag>
          <a-tag v-if="review.critical" color="warning">关键修改</a-tag>
          <span v-if="review.page">第 {{ review.page }} 页</span>
        </header>
        <MarkdownPreview v-if="imageMarkdown(review.source_image)" :content="imageMarkdown(review.source_image)" compact />
        <dl>
          <dt>校对前</dt><dd><pre>{{ review.before || '（空）' }}</pre></dd>
          <dt>校对后</dt><dd><pre>{{ review.after || '（空）' }}</pre></dd>
          <template v-if="review.reason"><dt>原因</dt><dd>{{ review.reason }}</dd></template>
          <template v-if="review.candidates?.ocr"><dt>OCR 候选</dt><dd><pre>{{ review.candidates.ocr }}</pre></dd></template>
          <template v-if="review.candidates?.native"><dt>文字层候选</dt><dd><pre>{{ review.candidates.native }}</pre></dd></template>
        </dl>
        <small v-if="review.model">校对模型：{{ review.model }}</small>
      </article>
      <div v-if="state.diagnostics.length" class="diagnostics">
        <strong>解析诊断</strong>
        <p v-for="(item, index) in state.diagnostics" :key="index">
          <span v-if="item.page">第 {{ item.page }} 页 · </span>{{ item.reason || item.kind }}
        </p>
      </div>
    </template>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { knowledgeAssetImageMarkdown } from '@/utils/document_parse_state'

const props = defineProps({
  state: { type: Object, required: true },
  kbId: { type: [String, Number], required: true }
})
const imageMarkdown = (source) => knowledgeAssetImageMarkdown(source, props.kbId, '原文裁图')
const reviewSummary = computed(() => ({
  not_requested: '未选择正文校对模型，未启用模型校对。',
  needs_review: '部分内容仍需人工核对，请结合原文裁图检查。',
  completed: '本次登记疑点已处理，仍可结合原PDF复核。'
})[props.state.metadata.review_status] || '暂无检查状态。')
const statusLabel = (status) => ({
  machine_corrected: '已修复', checked: '已核对', unresolved: '仍需核对', not_checked: '未核对'
})[status] || status
</script>

<style scoped>
.parse-assets-panel { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
.panel-description, small, .diagnostics { color: var(--color-text-secondary); font-size: 12px; }
.panel-description, .diagnostics p { margin: 0; }
.asset-card { border: 1px solid var(--gray-150); border-radius: 8px; padding: 16px; background: var(--gray-0); }
.asset-card.critical { border-color: var(--color-warning-700); }
header { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
header span { color: var(--color-text-secondary); font-size: 12px; }
dl { margin: 12px 0; display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: 8px; }
dt { color: var(--color-text-secondary); }
dd { margin: 0; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; margin: 0; }
.diagnostics { display: flex; flex-direction: column; gap: 8px; }
@media (max-width: 480px) { dl { grid-template-columns: 1fr; } }
</style>
