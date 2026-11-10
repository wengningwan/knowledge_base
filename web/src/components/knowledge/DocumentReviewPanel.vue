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
        <p>{{ filter === 'attention' ? '当前没有待确认的问题。' : '当前没有已确认的问题。' }}</p>
      </div>
      <div v-else class="review-workspace">
        <aside class="review-sidebar" aria-label="校对问题列表">
          <p class="review-list-heading">问题列表</p>
          <div class="review-list">
            <button
              v-for="entry in pageEntries" :key="entry.key" type="button" class="review-item"
              :disabled="saving" :aria-current="selected?.key === entry.key ? 'true' : undefined" @click="selectGroup(entry.key)"
            >
              <span class="review-item-heading">
                <span>{{ entry.kindLabel }}<template v-if="entry.clusterTotal">（共 {{ entry.clusterTotal }} 处）</template></span>
                <span class="review-page">{{ entry.page }}</span>
              </span>
              <span class="review-excerpt" v-html="renderExcerptHtml(entry.excerpt)" />
              <span v-if="entry.critical || entry.meta" class="review-item-meta">
                <span v-if="entry.critical">关键修改</span>
                <span v-if="entry.meta">{{ entry.meta }}</span>
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
              <h3>
                {{ reviewKindLabel(clusterActive?.kind ?? active.kind) }}
                <span v-if="clusterActive" class="review-page">共 {{ clusterActive.records.length }} 处 · {{ clusterPageSummary(clusterActive.pageDistribution) }}</span>
                <span v-else class="review-page">{{ pageLabel(active.page) }}</span>
              </h3>
              <p v-if="clusterActive?.excerpt" class="review-cluster-excerpt" v-html="renderExcerptHtml(clusterActive.excerpt)" />
              <div v-if="active.classification === 'excluded' || active.critical" class="review-detail-tags">
                <a-tag v-if="active.classification === 'excluded'">出版元素</a-tag>
                <a-tag v-if="active.critical" color="warning">关键修改</a-tag>
              </div>
            </div>
          </header>
          <label v-if="selected.records.length > 1" class="review-part-selector">
            {{ clusterActive ? `共 ${selected.records.length} 处` : '检查片段' }}
            <select v-model.number="partIndex" :disabled="saving" :aria-label="clusterActive ? '选择问题位置' : '选择检查片段'">
              <option v-for="(record, index) in selected.records" :key="record.id || index" :value="index">
                <template v-if="clusterActive">{{ pageLabel(record.page) }} · {{ reviewStatusLabel(record) }}{{ record.human_review ? ' · 已确认' : '' }}</template>
                <template v-else>{{ index + 1 }} / {{ selected.records.length }} · {{ reviewStatusLabel(record) }}</template>
              </option>
            </select>
          </label>
          <p v-if="active.reason" class="review-reason">{{ active.reason }}</p>
          <section v-if="active.parent_issue_id && active.targets?.length" class="review-target" aria-label="本片段核对范围">
            <h4>本片段核对范围</h4>
            <p v-for="target in active.targets" :key="target.id" class="review-diff"><span class="review-hint">{{ target.context_before }}</span><mark>{{ target.old || '（插入位置）' }}</mark><span class="review-hint">{{ target.context_after }}</span></p>
          </section>
          <div class="review-comparison">
            <section class="review-source" aria-label="原文裁图">
              <div class="review-section-heading">
                <h4>原文裁图</h4>
                <div class="review-source-actions">
                  <button v-if="sourceImage" type="button" class="review-action" @click="imageExpanded = true"><ZoomIn :size="16" /> 放大</button>
                  <button v-if="canLocatePage" type="button" class="review-action" @click="pageExpanded = true"><FileSearch :size="16" /> 原文页定位</button>
                </div>
              </div>
              <MarkdownPreview v-if="sourceImage" :content="sourceImage" compact />
              <p v-else class="review-source-empty">此记录没有可用裁图，请查看源 PDF。<span v-if="!validPage(active.page)">此记录未提供页码。</span></p>
            </section>
            <section class="review-text" aria-label="文字对照">
              <h4>文字对照</h4>
              <p v-if="comparison.after === null" class="review-hint">未生成修改，保留已识别正文。</p>
              <p v-else-if="comparison.kind === 'snapshot'" class="review-hint">这里展示整段文字处理前后的内容，可能包含其他问题的修改，请结合当前核对范围判断。</p>
              <p v-if="comparison.kind === 'snapshot' && !hasChanges" class="review-hint">处理前后的文字没有变化。</p>
              <div class="review-text-block">
                <span class="review-text-label">{{ comparison.kind === 'snapshot' ? '处理前文字' : 'PDF识别文字' }}</span>
                <p class="review-diff"><template v-for="(part, index) in beforeParts" :key="index"><mark v-if="part.type === 'math' && part.diff !== 'equal'" class="review-difference" v-html="part.html" /><span v-else-if="part.type === 'math'" v-html="part.html" /><mark v-else-if="part.type === 'delete'" class="review-difference">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template><span v-if="!comparison.before" class="review-hint">未识别到文字</span></p>
              </div>
              <div v-if="comparison.after !== null" class="review-text-block">
                <span class="review-text-label">{{ comparison.label }}</span>
                <p class="review-diff"><template v-for="(part, index) in afterParts" :key="index"><mark v-if="part.type === 'math' && part.diff !== 'equal'" class="review-difference" v-html="part.html" /><span v-else-if="part.type === 'math'" v-html="part.html" /><mark v-else-if="part.type === 'insert'" class="review-difference">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template><span v-if="comparison.after === ''" class="review-hint">内容已移除</span></p>
              </div>
              <details class="review-disclosure">
                <summary>完整上下文</summary>
                <span class="review-text-label">处理前文字</span>
                <p class="review-diff"><template v-for="(part, index) in beforeFullParts" :key="index"><mark v-if="part.type === 'math' && part.diff !== 'equal'" class="review-difference" v-html="part.html" /><span v-else-if="part.type === 'math'" v-html="part.html" /><span v-else>{{ part.text }}</span></template><span v-if="!beforeFullParts.length" class="review-hint">未识别到文字</span></p>
                <template v-if="typeof active.after === 'string'">
                  <span class="review-text-label">处理后文字</span>
                  <p class="review-diff"><template v-for="(part, index) in afterFullParts" :key="index"><mark v-if="part.type === 'math' && part.diff !== 'equal'" class="review-difference" v-html="part.html" /><span v-else-if="part.type === 'math'" v-html="part.html" /><span v-else>{{ part.text }}</span></template><span v-if="active.after === ''" class="review-hint">内容已移除</span></p>
                </template>
              </details>
            </section>
          </div>
          <section v-if="active.kind === 'formula_fragment_cluster' && active.suggestion_status" class="review-editor" aria-label="机器修复公式草稿">
            <div class="review-section-heading"><h4>机器修复公式</h4></div>
            <p v-if="!active.suggested_content" class="review-hint">机器识别失败或未启用，请对照左侧原文裁图手动补入公式。</p>
            <MarkdownPreview v-else :content="active.suggested_content" compact />
          </section>
          <section v-if="saveReview && sourceSha256 && state.attempt_id" class="review-editor" aria-label="人工校对并同步正文">
            <div class="review-section-heading">
              <h4>{{ activeBlock ? '校对后正文（完整段落）' : '补入遗漏正文' }}</h4>
              <span>
                <button v-if="active.suggested_content" type="button" class="review-action" :disabled="saving" @click="draft = active.suggested_content">采用机器草稿</button>
                <button v-if="active.candidates?.native" type="button" class="review-action" :disabled="saving" @click="draft = active.candidates.native">采用 PDF 提取文字</button>
              </span>
            </div>
            <p class="review-hint">{{ activeBlock ? '请核对完整段落。确认后，该问题的全部检查片段均标记为已确认，原机器记录保留供参考。' : '此问题未定位到原段落。需要补充正文时，请在下方本页上下文中点选补入位置（已按版面就近预选推荐）；原文无误则确认保持现状。' }}</p>
            <div v-if="!activeBlock" class="review-anchor-picker" role="group" aria-label="补入位置">
              <div class="review-anchor-header">
                <h4>本页正文上下文</h4>
                <span class="review-anchor-pager">
                  <button type="button" class="review-action" :disabled="!canMoveContextPage(-1)" aria-label="上一页正文" @click="moveContextPage(-1)"><ChevronLeft :size="14" /></button>
                  <span>{{ pageLabel(contextPage) }}</span>
                  <button type="button" class="review-action" :disabled="!canMoveContextPage(1)" aria-label="下一页正文" @click="moveContextPage(1)"><ChevronRight :size="14" /></button>
                </span>
              </div>
              <div class="review-anchor-list">
                <button
                  v-for="block in contextBlocks" :key="block.id" type="button"
                  class="review-anchor-card" :disabled="saving" :aria-pressed="anchorBlockId === block.id" @click="anchorBlockId = block.id"
                >
                  <span class="review-anchor-type">{{ reviewBlockTypeLabel(block.type) }}</span>
                  <span class="review-anchor-excerpt" v-html="renderExcerptHtml(block.content) || '空段落'" />
                  <span v-if="recommendedAnchorId === block.id" class="review-anchor-badge">推荐</span>
                </button>
                <button type="button" class="review-anchor-card" :disabled="saving" :aria-pressed="anchorBlockId === '__start__'" @click="anchorBlockId = '__start__'">文档开头</button>
              </div>
              <p v-if="!contextBlocks.length" class="review-hint">本页暂无正文块，可切换页码定位上下文，或选择“文档开头”。</p>
            </div>
            <label class="review-editor-label" for="review-corrected-content">{{ activeBlock ? '确认后将写入 Markdown 的段落文字' : '需要补入的正文' }}</label>
            <textarea id="review-corrected-content" v-model="draft" :disabled="saving" rows="8" maxlength="200000" />
            <p v-if="draftConflict" class="review-save-error" role="alert">该段正文已更新，草稿已保留。请核对最新正文后重新编辑。<button type="button" class="review-action" @click="discardDraft">放弃草稿，载入当前段落</button></p>
            <p v-if="hasIndexes" class="review-hint">修改正文会清理旧分块和索引，保存后需重新入库。</p>
            <p v-if="saveError" class="review-save-error" role="alert">{{ saveError }}</p>
            <div class="review-editor-actions">
              <button type="button" class="review-save" :disabled="saving || draftConflict || (!activeBlock && (!draft.trim() || !anchorBlockId))" @click="confirmReview(activeBlock ? 'replace' : 'insert_after')">{{ saving ? '正在同步…' : activeBlock ? '确认并同步到 MD' : '确认并补入到 MD' }}</button>
              <button type="button" class="review-keep-danger" :disabled="saving" @click="confirmReview('keep')">确认保持现状</button>
              <span v-if="draft !== draftBase" class="review-hint">有未保存的修改</span>
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
      <a-modal v-if="pageExpanded && canLocatePage" :open="true" :title="`原文页定位 · ${pageLabel(active?.page)}`" width="900px" :footer="null" @cancel="pageExpanded = false">
        <PdfPreview :url="pdfUrl" :page="active?.page" :highlight-regions="pageHighlights" class="review-pdf-preview" />
      </a-modal>
    </template>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, FileSearch, ZoomIn } from '@lucide/vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import PdfPreview from '@/components/common/PdfPreview.vue'
import { knowledgeAssetImageMarkdown } from '@/utils/document_parse_state'
import {
  groupDocumentReviews, reviewStatusLabel, reviewKindLabel, reviewComparison,
  clusterDocumentReviewGroups, clusterPageSummary,
  diffReviewText, compactReviewSegments, alignReviewMathParts, splitMathSegments, renderMathHtml, renderExcerptHtml,
  pageContextBlocks, recommendAnchorBlock, reviewBlockTypeLabel
} from '@/utils/documentReview'

const props = defineProps({
  state: { type: Object, required: true },
  kbId: { type: [String, Number], required: true },
  sourceSha256: { type: String, default: '' },
  hasIndexes: { type: Boolean, default: false },
  saveReview: { type: Function, default: null },
  pdfUrl: { type: String, default: '' }
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
const pageExpanded = ref(false)
const contextPage = ref(1)
const pageSize = 8
const groups = computed(() => groupDocumentReviews(props.state.reviews))
const autoIndexDeferred = computed(() => props.state.metadata.auto_index_enabled === true &&
  (['needs_review', 'incomplete'].includes(props.state.metadata.review_status) ||
    groups.value.some((group) => group.attention || group.incomplete)))
const clusters = computed(() => clusterDocumentReviewGroups(groups.value))
/** 待确认页签按簇聚合重复问题一次确认，已确认页签维持原问题组列表。 */
const filters = computed(() => [
  { value: 'attention', label: '待确认', count: clusters.value.length },
  { value: 'confirmed', label: '已确认', count: groups.value.filter((group) => group.confirmed).length }
])
const filtered = computed(() => (filter.value === 'attention' ? clusters.value : groups.value.filter((group) => group.confirmed)))
const isCluster = (entry) => Array.isArray(entry?.groups)
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
const clusterActive = computed(() => (isCluster(selected.value) ? selected.value : null))
/** 列表条目统一视图：簇显示同类处数与页码分布摘要，组维持片段数文案。 */
const pageEntries = computed(() => pageGroups.value.map((entry) => {
  if (!isCluster(entry)) {
    return {
      key: entry.key, kindLabel: reviewKindLabel(entry.primary.kind), clusterTotal: 0,
      page: pageLabel(entry.primary.page),
      excerpt: entry.primary.reason || entry.primary.before || '查看原文与校对依据',
      critical: entry.critical, meta: entry.records.length > 1 ? `${entry.records.length} 个检查片段` : ''
    }
  }
  return {
    key: entry.key, kindLabel: reviewKindLabel(entry.kind), clusterTotal: entry.records.length,
    page: clusterPageSummary(entry.pageDistribution), excerpt: entry.excerpt || entry.reason,
    critical: entry.critical, meta: entry.confirmedCount ? `已确认 ${entry.confirmedCount} 处` : ''
  }
}))
/** 草稿基准：有块时是段落正文，聚簇补入行是机器修复草稿（预填），其余为空。 */
const draftBase = computed(() => activeBlock.value?.content
  || (active.value?.kind === 'formula_fragment_cluster' ? active.value.suggested_content || '' : ''))
const draft = computed({
  get: () => drafts.value[draftKey.value] ?? draftBase.value,
  set: (value) => {
    if (!Object.hasOwn(drafts.value, draftKey.value)) draftBases.value[draftKey.value] = draftBase.value
    drafts.value[draftKey.value] = value
  }
})
const draftConflict = computed(() => Object.hasOwn(drafts.value, draftKey.value) &&
  draftBases.value[draftKey.value] !== draftBase.value && draft.value !== draftBase.value)
const anchorBlockId = computed({
  // 未显式选择时回退到版面就近推荐锚点，用户只需确认或改选
  get: () => anchors.value[draftKey.value] || recommendedAnchorId.value || '',
  set: (value) => { anchors.value[draftKey.value] = value }
})
const canLocatePage = computed(() => Boolean(props.pdfUrl) && validPage(active.value?.page))
const pageHighlights = computed(() => {
  const bbox = active.value?.bbox
  return canLocatePage.value && Array.isArray(bbox) && bbox.length === 4
    ? [{ page: active.value.page, bbox, label: '缺失区域' }]
    : []
})
const contextBlocks = computed(() => pageContextBlocks(props.state.blocks, contextPage.value))
const recommendedAnchorId = computed(() =>
  !active.value || activeBlock.value ? null : recommendAnchorBlock(props.state.blocks, contextPage.value, active.value.bbox))
const contextPageBounds = computed(() => {
  const pages = new Set((props.state.blocks || []).map((block) => block.page).filter((page) => Number.isInteger(page) && page > 0))
  if (validPage(active.value?.page)) pages.add(active.value.page)
  return { min: Math.min(...pages, 1), max: Math.max(...pages, 1) }
})
const canMoveContextPage = (offset) => {
  const next = contextPage.value + offset
  return next >= contextPageBounds.value.min && next <= contextPageBounds.value.max
}
const moveContextPage = (offset) => {
  if (canMoveContextPage(offset)) contextPage.value += offset
}
const sourceImage = computed(() => knowledgeAssetImageMarkdown(active.value?.source_image, props.kbId, '原文裁图'))
const comparison = computed(() => active.value ? reviewComparison(active.value) : { before: '', after: null })
const diff = computed(() => diffReviewText(comparison.value.before, comparison.value.after ?? comparison.value.before))
const withMathHtml = (parts) => parts.map((part) => (part.type === 'math' ? { ...part, html: renderMathHtml(part) } : part))
const alignedCompactParts = (removedType) => compactReviewSegments(alignReviewMathParts(
  diff.value.parts.filter((part) => part.type !== removedType),
  removedType === 'insert' ? comparison.value.before : comparison.value.after ?? comparison.value.before
))
const beforeParts = computed(() => withMathHtml(alignedCompactParts('insert')))
const afterParts = computed(() => withMathHtml(alignedCompactParts('delete')))
const beforeFullParts = computed(() => withMathHtml(splitMathSegments(active.value ? active.value.before ?? active.value.candidates?.ocr ?? '' : '')))
const afterFullParts = computed(() => withMathHtml(splitMathSegments(typeof active.value?.after === 'string' ? active.value.after : '')))
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

/** 簇默认定位首个未确认处，组默认定位最未完成片段。 */
function selectGroup(key) {
  selectedKey.value = key
  const entry = filtered.value.find((item) => item.key === key)
  const target = entry?.primary ?? entry?.records.find((record) => !record.human_review)
  partIndex.value = entry ? Math.max(0, entry.records.indexOf(target)) : 0
  imageExpanded.value = false
  pageExpanded.value = false
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
  // 草稿清理范围与后端确认范围一致：有段落时按原问题身份覆盖分片片段，无块定位时后端只确认单条。
  const issueIdentity = active.value.parent_issue_id || active.value.id
  const savedKeys = activeBlock.value
    ? props.state.reviews.flatMap((row, rowIndex) =>
        (row.parent_issue_id || row.id) === issueIdentity ? [`${props.state.attempt_id}:${rowIndex}`] : [])
    : [`${props.state.attempt_id}:${index}`]
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
  } catch (error) {
    saveError.value = saveErrorMessage(error)
  } finally {
    saving.value = false
  }
}

/** 保存失败文案：409 提示重新打开文档，400 透出后端原因，其余统一可重试提示。 */
function saveErrorMessage(error) {
  if (error?.status === 409) return '校对结果已变化，请关闭后重新打开文档'
  if (error?.status === 400 && error.message) return error.message
  return '保存失败，请重试'
}

/** 用户明确放弃旧草稿后才重新载入当前段落。 */
function discardDraft() {
  delete drafts.value[draftKey.value]
  delete draftBases.value[draftKey.value]
}

watch([() => props.state.attempt_id, () => props.state.reviews], () => {
  const initial = groups.value.some((group) => group.attention || group.incomplete) || !groups.value.some((group) => group.confirmed)
    ? 'attention' : 'confirmed'
  changeFilter(initial)
}, { immediate: true })
watch(partIndex, () => { imageExpanded.value = false; pageExpanded.value = false })
watch(() => active.value?.page, (page) => { contextPage.value = validPage(page) ? page : 1 }, { immediate: true })
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
.review-page, .review-item-meta, .review-hint, .review-text-label, .review-pagination, .review-navigation, .review-source-empty { color: var(--color-text-secondary); font-size: 12px; font-weight: 400; }
.review-list { display: flex; flex-direction: column; }
.review-item { min-width: 0; width: 100%; padding: 12px; display: flex; flex-direction: column; gap: 6px; text-align: left; border: 0; border-top: 1px solid var(--gray-150); background: transparent; color: var(--color-text); }
.review-item:hover { background: var(--gray-25); }
.review-item[aria-current="true"] { background: var(--main-30); box-shadow: inset 3px 0 var(--main-color); }
.review-item-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 600; }
.review-page { flex-shrink: 0; }
.review-excerpt { font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.review-excerpt :deep(.katex), .review-anchor-excerpt :deep(.katex) { font-size: 1.05em; }
.review-excerpt :deep(.katex-render-fallback), .review-anchor-excerpt :deep(.katex-render-fallback) { color: #cc0000; }
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
.review-cluster-excerpt { margin: 4px 0 0; font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); overflow-wrap: anywhere; }
.review-cluster-excerpt :deep(.katex) { font-size: 1.05em; }
.review-cluster-excerpt :deep(.katex-render-fallback) { color: #cc0000; }
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
.review-diff { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; font-size: 13px; line-height: 1.8; margin: 0; }
.review-diff :deep(.katex) { font-size: 1.05em; }
.review-diff :deep(.katex-display) { margin: 0.25rem 0; overflow-x: auto; overflow-y: hidden; }
.review-diff :deep(.katex-render-fallback) { color: #cc0000; }
.review-disclosure { font-size: 13px; min-width: 0; }
.review-disclosure summary { cursor: pointer; color: var(--main-color); padding: 4px 0; }
.review-editor { display: flex; flex-direction: column; gap: 12px; padding-top: 16px; border-top: 1px solid var(--gray-150); }
.review-keep-danger { padding: 8px 12px; border: 1px solid var(--color-error-500); border-radius: 6px; background: var(--color-error-500); color: var(--gray-0); font: inherit; font-size: 13px; }
.review-keep-danger:hover:not(:disabled) { background: var(--color-error-700); border-color: var(--color-error-700); }
.review-keep-danger:disabled { background: var(--gray-100); border-color: var(--gray-150); color: var(--color-text-tertiary); }
.review-source-actions { display: flex; align-items: center; gap: 4px; }
.review-anchor-picker { display: flex; flex-direction: column; gap: 8px; }
.review-anchor-header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
.review-anchor-pager { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--color-text-secondary); }
.review-anchor-list { display: flex; flex-direction: column; gap: 6px; max-height: 220px; overflow-y: auto; padding-right: 2px; }
.review-anchor-card { min-width: 0; display: flex; align-items: baseline; gap: 8px; text-align: left; padding: 8px 10px; border: 1px solid var(--gray-150); border-radius: 6px; background: var(--gray-0); color: var(--color-text); font-size: 12px; }
.review-anchor-card:hover:not(:disabled) { border-color: var(--main-color); }
.review-anchor-card[aria-pressed="true"] { border-color: var(--main-color); background: var(--main-30); box-shadow: inset 3px 0 var(--main-color); }
.review-anchor-type { flex-shrink: 0; color: var(--color-text-secondary); }
.review-anchor-excerpt { flex: 1; min-width: 0; line-height: 1.6; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.review-anchor-badge { flex-shrink: 0; color: var(--main-color); font-weight: 600; }
.review-pdf-preview { max-height: 70vh; overflow-y: auto; }
.review-editor-label { font-size: 13px; }
.review-editor textarea { width: 100%; box-sizing: border-box; resize: vertical; min-height: 160px; padding: 12px; border: 1px solid var(--gray-200); border-radius: 6px; background: var(--gray-0); color: var(--color-text); font: inherit; font-size: 13px; line-height: 1.8; }
.review-editor textarea:focus-visible { outline: 2px solid var(--main-color); outline-offset: 2px; }
.review-editor-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.review-save { padding: 8px 12px; border: 1px solid var(--main-color); border-radius: 6px; background: var(--main-color); color: var(--gray-0); font: inherit; font-size: 13px; }
.review-save:disabled { background: var(--gray-100); border-color: var(--gray-150); color: var(--color-text-tertiary); }
.review-save-error { color: var(--color-error-700); font-size: 13px; }
.review-disclosure[open] summary { margin-bottom: 12px; }
.review-disclosure .review-diff + .review-text-label { margin-top: 12px; }
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
