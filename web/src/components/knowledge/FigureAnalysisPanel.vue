<template>
  <section class="parse-assets-panel" aria-label="图片解析">
    <a-spin v-if="state.loading" tip="正在加载图片解析..." />
    <a-alert v-else-if="state.error" type="error" :message="state.error" show-icon />
    <a-empty v-else-if="state.legacy_without_assets" description="此文件尚无独立解析产物，请重新解析生成图片解析和校对结果。" />
    <a-empty v-else-if="!state.figures.length" description="本次解析没有提取到图片。" />
    <template v-else>
      <p class="panel-description">图片解释为模型生成的独立结果，不写入论文正文。</p>
      <a-alert v-if="['pending', 'running'].includes(state.metadata?.figure_analysis?.status)" type="info" message="正文已就绪，插图正在后台解析。可在任务中心查看进度，完成后关闭并重新打开文件详情。" show-icon />
      <a-alert v-else-if="['failed', 'cancelled', 'incomplete'].includes(state.metadata?.figure_analysis?.status)" type="warning" message="图片任务未完成，原图和已生成的正文仍可查看。" show-icon />
      <article v-for="figure in state.figures" :key="figure.id" class="asset-card">
        <header>
          <strong>{{ figure.caption || figure.id }}</strong>
          <span v-if="figure.page">第 {{ figure.page }} 页</span>
        </header>
        <MarkdownPreview v-if="imageMarkdown(figure.image_url)" :content="imageMarkdown(figure.image_url)" compact />
        <p v-else class="result-message">原图不可用</p>
        <MarkdownPreview v-if="figure.analysis_status === 'completed' && figure.analysis" :content="figure.analysis" compact />
        <p v-else class="result-message">{{ analysisMessage(figure.analysis_status) }}</p>
        <small v-if="figure.model">解析模型：{{ figure.model }}</small>
      </article>
    </template>
  </section>
</template>

<script setup>
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { knowledgeAssetImageMarkdown } from '@/utils/document_parse_state'

const props = defineProps({
  state: { type: Object, required: true },
  kbId: { type: [String, Number], required: true }
})
const imageMarkdown = (source) => knowledgeAssetImageMarkdown(source, props.kbId)
const analysisMessage = (status) => ({
  pending: '等待后台图片解析，正文已就绪。',
  running: '正在后台解析。',
  cancelled: '图片任务已取消，原图已保留。',
  failed: '图片解析失败，原图已保留。',
  not_requested: '未选择图片解析模型，未生成解释。',
  not_checked: '本图片未进行模型解析。',
  completed: '模型未返回图片解释。'
})[status] || '暂无图片解析结果。'
</script>

<style scoped>
.parse-assets-panel { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
.panel-description, .result-message, small { margin: 0; color: var(--color-text-secondary); }
.panel-description, small { font-size: 12px; }
.asset-card { border: 1px solid var(--gray-150); border-radius: 8px; padding: 16px; background: var(--gray-0); }
header { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
header span { color: var(--color-text-secondary); font-size: 12px; }
</style>
