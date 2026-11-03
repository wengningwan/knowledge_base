<template>
  <section class="document-review-panel" aria-label="校对结果">
    <a-spin v-if="state.loading" tip="正在加载校对结果..." />
    <a-alert v-else-if="state.error" type="error" :message="state.error" show-icon />
    <a-empty v-else-if="state.legacy_without_assets" description="此文件尚无独立解析产物，请重新解析生成图片解析和校对结果。" />
    <template v-else>
      <a-alert :type="['needs_review', 'incomplete'].includes(state.metadata.review_status) ? 'warning' : 'info'" :message="reviewSummary" show-icon />
      <p v-if="autoIndexDeferred" class="review-notice" role="status">暂不进行自动入库操作</p>
      <div v-if="groups.length" class="review-filters" role="group" aria-label="筛选校对问题">
        <button
          v-for="option in filters" :key="option.value" type="button"
          class="review-filter" :disabled="saving" :aria-pressed="filter === option.value" @click="changeFilter(option.value)"
        >{{ option.label }} <span>{{ option.count }}</span></button>
      </div>
      <a-empty v-if="!groups.length" description="本次解析没有校对记录。" />
      <div v-else-if="!filtered.length" class="review-empty">
        <p>{{ filter === 'attention' ? '当前没有待人工核对的问题。' : '当前分类没有校对记录。' }}</p>
        <button type="button" class="review-action" @click="changeFilter('all')">查看全部记录</button>
      </div>
      <div v-else class="review-workspace">
        <aside class="review-sidebar" aria-label="校对问题列表">
          <p class="review-list-heading">问题列表 <span>按原问题计数</span></p>
          <div class="review-list">
            <button
              v-for="group in pageGroups" :key="group.key" type="button" class="review-item"
              :disabled="saving" :aria-current="selected?.key === group.key ? 'true' : undefined" @click="selectGroup(group.key)"
            >
              <span class="review-item-heading">
                <span>{{ reviewKindLabel(group.primary.kind) }}</span>
                <span class="review-page">{{ pageLabel(group.primary.page) }}</span>
              </span>
              <span class="review-item-status" :class="{ 'needs-attention': group.attention || group.incomplete }">{{ reviewStatusLabel(group.primary) }}</span>
              <span class="review-excerpt">{{ group.primary.reason || group.primary.before || '查看原文与校对依据' }}</span>
              <span v-if="group.critical || group.records.length > 1" class="review-item-meta">
                <span v-if="group.critical">关键修改</span>
                <span v-if="group.records.length > 1">{{ group.records.length }} 个检查片段</span>
              </span>
            </button>
          </div>
          <div v-if="pageCount > 1" class="review-pagination">
            <button type="button" class="review-action" :disabled="listPage === 1" aria-label="上一页问题" @click="listPage--"><ChevronLeft :size="16" /></button>
            <span>{{ listPage }} / {{ pageCount }} 页</span>
            <button type="button" class="review-action" :disabled="listPage === pageCount" aria-label="下一页问题" @click="listPage++"><ChevronRight :size="16" /></button>
          </div>
        </aside>
        <article v-if="active" :key="`${selected.key}-${partIndex}`" class="review-detail" aria-label="当前校对问题">
          <header class="review-detail-header">
            <div>
              <h3>{{ reviewKindLabel(active.kind) }} <span class="review-page">{{ pageLabel(active.page) }}</span></h3>
              <div class="review-detail-tags">
                <a-tag :color="selected.attention || selected.incomplete ? 'warning' : undefined">{{ reviewStatusLabel(active) }}</a-tag>
                <a-tag v-if="active.classification === 'excluded'">出版元素</a-tag>
                <a-tag v-if="active.critical" color="warning">关键修改</a-tag>
              </div>
            </div>
          </header>
          <label v-if="selected.records.length > 1" class="review-part-selector">
            检查片段
            <select v-model.number="partIndex" :disabled="saving" aria-label="选择检查片段">
              <option v-for="(record, index) in selected.records" :key="record.id || index" :value="index">
                {{ index + 1 }} / {{ selected.records.length }} · {{ reviewStatusLabel(record) }}
              </option>
            </select>
          </label>
          <p v-if="selected.incomplete" class="review-notice">此问题存在未完成的复核；这不代表正文一定有错，请结合原文检查。</p>
          <p v-if="active.reason" class="review-reason">{{ active.reason }}</p>
          <section v-if="active.parent_issue_id && active.targets?.length" class="review-target" aria-label="本片段核对范围">
            <h4>本片段核对范围</h4>
            <p v-for="target in active.targets" :key="target.id" class="review-diff"><span class="review-hint">{{ target.context_before }}</span><mark>{{ target.old || '（插入位置）' }}</mark><span class="review-hint">{{ target.context_after }}</span></p>
          </section>
          <div class="review-comparison">
            <section class="review-source" aria-label="原文裁图">
              <div class="review-section-heading">
                <h4>原文裁图</h4>
                <button v-if="sourceImage" type="button" class="review-action" @click="imageExpanded = true"><ZoomIn :size="16" /> 放大</button>
              </div>
              <MarkdownPreview v-if="sourceImage" :content="sourceImage" compact />
              <p v-else class="review-source-empty">此记录没有可用裁图，请查看源 PDF。<span v-if="!validPage(active.page)">此记录未提供页码。</span></p>
            </section>
            <section class="review-text" aria-label="文字对照">
              <h4>文字对照</h4>
              <p v-if="comparison.after === null" class="review-hint">未生成修改，保留已识别正文。</p>
              <p v-else-if="comparison.kind === 'snapshot'" class="review-hint">这里展示整段文字处理前后的内容，可能包含其他问题的修改，请结合当前核对范围判断。</p>
              <p v-if="comparison.kind === 'snapshot' && !hasChanges" class="review-hint">处理前后的文字没有变化。</p>
              <p v-if="diff.coarse" class="review-hint">变化范围较大，已整体标出；可展开完整上下文逐字核对。</p>
              <div class="review-text-block">
                <span class="review-text-label">{{ comparison.kind === 'snapshot' ? '处理前文字' : 'OCR识别文字' }}</span>
                <p class="review-diff"><template v-for="(part, index) in beforeParts" :key="index"><mark v-if="part.type === 'delete'" class="review-difference">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template><span v-if="!comparison.before" class="review-hint">未识别到文字</span></p>
              </div>
              <div v-if="comparison.after !== null" class="review-text-block">
                <span class="review-text-label">{{ comparison.label }}</span>
                <p class="review-diff"><template v-for="(part, index) in afterParts" :key="index"><mark v-if="part.type === 'insert'" class="review-difference">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template><span v-if="comparison.after === ''" class="review-hint">内容已移除</span></p>
              </div>
              <details class="review-disclosure">
                <summary>完整上下文</summary>
                <span class="review-text-label">处理前文字</span>
                <pre>{{ active.before ?? active.candidates?.ocr ?? '未识别到文字' }}</pre>
                <template v-if="typeof active.after === 'string'">
                  <span class="review-text-label">处理后文字</span>
                  <pre>{{ active.after === '' ? '内容已移除' : active.after }}</pre>
                </template>
              </details>
            </section>
          </div>
          <section v-if="saveReview && sourceSha256 && state.attempt_id" class="review-editor" aria-label="人工校对并同步正文">
            <div class="review-section-heading">
              <h4>{{ activeBlock ? '校对后正文（完整段落）' : '补入遗漏正文' }}</h4>
              <button v-if="active.candidates?.native" type="button" class="review-action" :disabled="saving" @click="draft = active.candidates.native">采用 PDF 提取文字</button>
            </div>
            <p class="review-hint">{{ activeBlock ? '请核对完整段落。确认后，该段相关问题均标记为人工已确认，原机器记录保留供参考。' : '此问题未定位到原段落。需要补充正文时，请明确选择补入位置；原文无误则保留正文并确认。' }}</p>
            <label v-if="!activeBlock" class="review-part-selector">
              补入位置
              <select v-model="anchorBlockId" :disabled="saving" aria-label="选择补入位置">
                <option value="">请选择补入位置</option>
                <option value="__start__">文档开头</option>
                <option v-for="block in state.blocks || []" :key="block.id" :value="block.id">在此段落之后：{{ String(block.content || '空段落').slice(0, 70) }}</option>
              </select>
            </label>
            <label class="review-editor-label" for="review-corrected-content">{{ activeBlock ? '确认后将写入 Markdown 的段落文字' : '需要补入的正文' }}</label>
            <textarea id="review-corrected-content" v-model="draft" :disabled="saving" rows="8" maxlength="200000" />
            <p v-if="draftConflict" class="review-save-error" role="alert">该段正文已更新，草稿已保留。请核对最新正文后重新编辑。<button type="button" class="review-action" @click="discardDraft">放弃草稿，载入当前段落</button></p>
            <p v-if="hasIndexes" class="review-hint">修改正文会清理旧分块和索引，保存后需重新入库。</p>
            <p v-if="saveError" class="review-save-error" role="alert">{{ saveError }}</p>
            <div class="review-editor-actions">
              <button type="button" class="review-save" :disabled="saving || draftConflict || (!activeBlock && (!draft.trim() || !anchorBlockId))" @click="confirmReview(activeBlock ? 'replace' : 'insert_after')">{{ saving ? '正在同步…' : activeBlock ? '确认整段并同步到 MD' : '确认补入并同步到 MD' }}</button>
              <button type="button" class="review-action" :disabled="saving" @click="confirmReview('keep')">保留正文并确认</button>
              <span v-if="draft !== (activeBlock?.content || '')" class="review-hint">有未保存的修改</span>
            </div>
          </section>
          <p v-else-if="saveReview && !sourceSha256" class="review-notice">正文尚未加载成功，暂时无法保存校对，请重新打开文档后重试。</p>
          <footer class="review-navigation">
            <span role="status" aria-live="polite">当前分类第 {{ selectedIndex + 1 }} / {{ filtered.length }} 项</span>
            <div>
              <button type="button" class="review-action" :disabled="saving || selectedIndex === 0" @click="move(-1)"><ChevronLeft :size="16" /> 上一项</button>
              <button type="button" class="review-action" :disabled="saving || selectedIndex === filtered.length - 1" @click="move(1)">下一项 <ChevronRight :size="16" /></button>
            </div>
          </footer>
        </article>
      </div>
      <a-modal v-if="imageExpanded" :open="true" title="原文裁图" width="900px" :footer="null" @cancel="imageExpanded = false">
        <MarkdownPreview :content="sourceImage" compact />
      </a-modal>
    </template>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, ZoomIn } from '@lucide/vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { knowledgeAssetImageMarkdown } from '@/utils/document_parse_state'
import {
  groupDocumentReviews, reviewStatusLabel, reviewKindLabel, reviewComparison,
  diffReviewText, compactReviewSegments
} from '@/utils/documentReview'

const props = defineProps({
  state: { type: Object, required: true },
  kbId: { type: [String, Number], required: true },
  sourceSha256: { type: String, default: '' },
  hasIndexes: { type: Boolean, default: false },
  saveReview: { type: Function, default: null }
})
const saving = ref(false)
const saveError = ref('')
const drafts = ref({})
const draftBases = ref({})
const anchors = ref({})
const filter = ref('attention')
const selectedKey = ref(null)
const partIndex = ref(0)
const listPage = ref(1)
const imageExpanded = ref(false)
const pageSize = 8
const groups = computed(() => groupDocumentReviews(props.state.reviews))
const autoIndexDeferred = computed(() => props.state.metadata.auto_index_enabled === true &&
  (['needs_review', 'incomplete'].includes(props.state.metadata.review_status) ||
    groups.value.some((group) => group.attention || group.incomplete)))
const filters = computed(() => [
  { value: 'attention', label: '待人工核对' }, { value: 'incomplete', label: '复核未完成' },
  { value: 'modified', label: '系统已修改' }, { value: 'confirmed', label: '人工已确认' },
  { value: 'all', label: '全部记录' }
].map((option) => ({ ...option, count: groups.value.filter((group) => option.value === 'all' || group[option.value]).length })))
const filtered = computed(() => groups.value.filter((group) => filter.value === 'all' || group[filter.value]))
const pageCount = computed(() => Math.ceil(filtered.value.length / pageSize))
const pageGroups = computed(() => filtered.value.slice((listPage.value - 1) * pageSize, listPage.value * pageSize))
const selectedIndex = computed(() => Math.max(0, filtered.value.findIndex((group) => group.key === selectedKey.value)))
const selected = computed(() => filtered.value[selectedIndex.value])
const active = computed(() => selected.value?.records[partIndex.value])
const activeBlock = computed(() => {
  const id = active.value?.human_review?.block_id || active.value?.block_id
  return id == null ? null : props.state.blocks?.find((block) => block.id === id && !['page_header', 'page_footer'].includes(block.type))
})
const draftKey = computed(() => `${props.state.attempt_id}:${props.state.reviews.indexOf(active.value)}`)
const draft = computed({
  get: () => drafts.value[draftKey.value] ?? activeBlock.value?.content ?? '',
  set: (value) => {
    if (!Object.hasOwn(drafts.value, draftKey.value)) draftBases.value[draftKey.value] = activeBlock.value?.content || ''
    drafts.value[draftKey.value] = value
  }
})
const draftConflict = computed(() => Object.hasOwn(drafts.value, draftKey.value) &&
  draftBases.value[draftKey.value] !== (activeBlock.value?.content || '') && draft.value !== activeBlock.value?.content)
const anchorBlockId = computed({
  get: () => anchors.value[draftKey.value] || '',
  set: (value) => { anchors.value[draftKey.value] = value }
})
const sourceImage = computed(() => knowledgeAssetImageMarkdown(active.value?.source_image, props.kbId, '原文裁图'))
const comparison = computed(() => active.value ? reviewComparison(active.value) : { before: '', after: null })
const diff = computed(() => diffReviewText(comparison.value.before, comparison.value.after ?? comparison.value.before))
const compactParts = computed(() => compactReviewSegments(diff.value.parts))
const beforeParts = computed(() => compactParts.value.filter((part) => part.type !== 'insert'))
const afterParts = computed(() => compactParts.value.filter((part) => part.type !== 'delete'))
const hasChanges = computed(() => diff.value.parts.some((part) => part.type !== 'equal'))
const validPage = (page) => Number.isInteger(page) && page > 0
const pageLabel = (page) => validPage(page) ? `第 ${page} 页` : '页码未知'
const reviewSummary = computed(() => ({
  not_requested: '未启用模型校对，以下为系统检测和规则处理记录。',
  needs_review: '部分内容仍需人工核对，请逐项对照原文。',
  incomplete: '正文已生成，部分自动复核尚未完成，请对照原文检查。',
  completed: '本次登记疑点已完成核对，仍可结合原 PDF 复核。'
})[props.state.metadata.review_status] || '以下为系统校对记录，可结合原文逐项复核。')

/** 切换分类时从该分类的首个问题开始核对。 */
function changeFilter(value) {
  filter.value = value
  listPage.value = 1
  selectGroup(filtered.value[0]?.key)
}

/** 原问题默认定位最未完成的片段，正常片段仍可查看。 */
function selectGroup(key) {
  selectedKey.value = key
  const group = filtered.value.find((entry) => entry.key === key)
  partIndex.value = group ? group.records.indexOf(group.primary) : 0
  imageExpanded.value = false
}

/** 连续核对跨列表分页时同步定位当前问题。 */
function move(offset) {
  const index = selectedIndex.value + offset
  if (index < 0 || index >= filtered.value.length) return
  selectGroup(filtered.value[index].key)
  listPage.value = Math.floor(index / pageSize) + 1
}

/** 只提交人工确认的完整段落；失败时原草稿可继续修改重试。 */
async function confirmReview(action) {
  if (saving.value || !props.saveReview || (action !== 'keep' && draftConflict.value)) return
  saving.value = true
  saveError.value = ''
  const index = props.state.reviews.indexOf(active.value)
  const blockId = activeBlock.value?.id
  const savedKeys = props.state.reviews.flatMap((row, rowIndex) => {
    const id = row.human_review?.block_id || row.block_id
    return rowIndex === index || (blockId && id === blockId) ? [`${props.state.attempt_id}:${rowIndex}`] : []
  })
  try {
    await props.saveReview({
      attempt_id: props.state.attempt_id,
      review_revision: props.state.metadata.human_review_revision || null,
      source_sha256: props.sourceSha256,
      review_index: index,
      action, content: action === 'keep' ? '' : draft.value,
      anchor_block_id: action === 'insert_after' ? anchorBlockId.value : null
    })
    for (const key of savedKeys) {
      delete drafts.value[key]
      delete draftBases.value[key]
      delete anchors.value[key]
    }
  } catch {
    saveError.value = '保存失败'
  } finally {
    saving.value = false
  }
}

/** 用户明确放弃旧草稿后才重新载入当前段落。 */
function discardDraft() {
  delete drafts.value[draftKey.value]
  delete draftBases.value[draftKey.value]
}

watch([() => props.state.attempt_id, () => props.state.reviews], () => {
  const initial = groups.value.some((group) => group.attention) ? 'attention'
    : groups.value.some((group) => group.incomplete) ? 'incomplete' : 'all'
  changeFilter(initial)
}, { immediate: true })
watch(partIndex, () => { imageExpanded.value = false })
watch(draftKey, () => { saveError.value = '' })
</script>

<style scoped lang="less">
.document-review-panel { padding: 16px; display: flex; flex-direction: column; gap: 16px; container-type: inline-size; }
.review-filters { display: flex; flex-wrap: wrap; gap: 8px; }
button, select { font: inherit; }
button { cursor: pointer; }
button:disabled { cursor: default; color: var(--color-text-tertiary); }
button:focus-visible, summary:focus-visible, select:focus-visible { outline: 2px solid var(--main-color); outline-offset: 2px; }
.review-filter { padding: 8px 12px; border: 1px solid var(--gray-150); border-radius: 6px; background: var(--gray-0); color: var(--color-text-secondary); display: inline-flex; gap: 8px; align-items: center; }
.review-filter:hover { border-color: var(--main-color); }
.review-filter[aria-pressed="true"] { color: var(--main-color); background: var(--main-50); border-color: var(--main-color); }
.review-filter span { font-variant-numeric: tabular-nums; }
.review-workspace { display: grid; grid-template-columns: 224px minmax(0, 1fr); border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-0); }
.review-sidebar { min-width: 0; border-right: 1px solid var(--gray-150); }
.review-list-heading { padding: 14px 12px; margin: 0; font-weight: 600; display: flex; justify-content: space-between; gap: 8px; }
.review-list-heading span, .review-page, .review-item-meta, .review-hint, .review-text-label, .review-pagination, .review-navigation, .review-source-empty { color: var(--color-text-secondary); font-size: 12px; font-weight: 400; }
.review-list { display: flex; flex-direction: column; }
.review-item { min-width: 0; width: 100%; padding: 12px; display: flex; flex-direction: column; gap: 6px; text-align: left; border: 0; border-top: 1px solid var(--gray-150); background: transparent; color: var(--color-text); }
.review-item:hover { background: var(--gray-25); }
.review-item[aria-current="true"] { background: var(--main-30); box-shadow: inset 3px 0 var(--main-color); }
.review-item-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 600; }
.review-page { flex-shrink: 0; }
.review-item-status { font-size: 12px; color: var(--color-text-secondary); }
.needs-attention { color: var(--color-warning-700); }
.review-excerpt { font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.review-item-meta { display: flex; flex-wrap: wrap; gap: 8px; }
.review-pagination { padding: 8px; display: flex; align-items: center; justify-content: space-between; border-top: 1px solid var(--gray-150); }
.review-detail { padding: 20px; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
.review-detail-header, .review-section-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
h3, h4, p { margin: 0; }
h3 { font-size: 16px; font-weight: 600; display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; }
h4 { font-size: 13px; font-weight: 600; }
.review-detail-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.review-detail-tags :deep(.ant-tag) { margin: 0; }
.review-action { display: inline-flex; gap: 4px; align-items: center; justify-content: center; border: 0; border-radius: 4px; background: transparent; color: var(--main-color); font-size: 12px; padding: 4px; }
.review-action:hover:not(:disabled) { background: var(--main-50); }
.review-part-selector { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
select { background: var(--gray-0); color: var(--color-text); border: 1px solid var(--gray-200); border-radius: 4px; padding: 6px 8px; max-width: 100%; }
.review-notice { font-size: 13px; background: var(--color-warning-50); color: var(--color-warning-700); padding: 10px 12px; border-radius: 4px; }
.review-reason { font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.review-comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; }
.review-source, .review-text { min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.review-source { align-self: start; }
.review-source :deep(img) { max-height: 360px; width: 100%; object-fit: contain; }
.review-source-empty { padding: 24px 12px; line-height: 1.8; background: var(--gray-25); border-radius: 6px; }
.review-hint { line-height: 1.7; }
.review-text-block { background: var(--gray-25); border-radius: 6px; padding: 12px; }
.review-target { padding: 12px; background: var(--gray-25); border-radius: 6px; display: flex; flex-direction: column; gap: 8px; }
.review-target mark, .review-difference { color: var(--color-text); background: var(--color-warning-100); text-decoration: none; }
.review-text-label { display: block; margin-bottom: 8px; }
.review-diff, pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; font-size: 13px; line-height: 1.8; margin: 0; }
.review-disclosure { font-size: 13px; min-width: 0; }
.review-disclosure summary { cursor: pointer; color: var(--main-color); padding: 4px 0; }
.review-editor { display: flex; flex-direction: column; gap: 12px; padding-top: 16px; border-top: 1px solid var(--gray-150); }
.review-editor-label { font-size: 13px; }
.review-editor textarea { width: 100%; box-sizing: border-box; resize: vertical; min-height: 160px; padding: 12px; border: 1px solid var(--gray-200); border-radius: 6px; background: var(--gray-0); color: var(--color-text); font: inherit; font-size: 13px; line-height: 1.8; }
.review-editor textarea:focus-visible { outline: 2px solid var(--main-color); outline-offset: 2px; }
.review-editor-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.review-save { padding: 8px 12px; border: 1px solid var(--main-color); border-radius: 6px; background: var(--main-color); color: var(--gray-0); font: inherit; font-size: 13px; }
.review-save:disabled { background: var(--gray-100); border-color: var(--gray-150); color: var(--color-text-tertiary); }
.review-save-error { color: var(--color-error-700); font-size: 13px; }
.review-disclosure[open] summary { margin-bottom: 12px; }
.review-disclosure pre + .review-text-label { margin-top: 12px; }
.review-navigation { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding-top: 12px; border-top: 1px solid var(--gray-150); }
.review-navigation > div { display: flex; gap: 12px; }
.review-empty { padding: 32px 16px; text-align: center; display: flex; flex-direction: column; gap: 12px; align-items: center; }
@container (max-width: 900px) { .review-comparison { grid-template-columns: minmax(0, 1fr); } }
@container (max-width: 600px) {
  .review-workspace { grid-template-columns: minmax(0, 1fr); }
  .review-sidebar { border-right: 0; border-bottom: 1px solid var(--gray-150); }
  .review-list { max-height: 200px; overflow-y: auto; }
  .review-detail { padding: 16px; }
  .review-filter { padding: 8px; }
}
@media (pointer: coarse) { .review-action, .review-filter, select, summary { min-height: 40px; } }
</style>
