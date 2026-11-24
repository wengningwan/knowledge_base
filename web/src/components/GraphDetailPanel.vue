<template>
  <transition name="slide-fade-left">
    <div class="detail-panel" v-if="visible" @click="handleLinkClick">
      <div class="panel-header">
        <span class="panel-title">{{ title }}</span>
        <X :size="14" class="close-icon" @click="$emit('close')" />
      </div>
      <div class="panel-body">
        <template v-if="item">
          <template v-if="type === 'node'">
            <div :class="rowClass(item.data?.label)">
              <span class="detail-label">名称</span>
              <span class="detail-value">
                <DetailValue
                  :value="item.data?.label"
                  field-key="__label__"
                  :expanded-keys="expandedKeys"
                />
              </span>
            </div>
            <div class="detail-row">
              <span class="detail-label">来源知识库</span>
              <span class="detail-value">{{ kbName || kbId }}</span>
            </div>
            <div v-if="sourceFileIds.length > 1" class="detail-row detail-row--stack">
              <span class="detail-label">合看来源（{{ sourceFileIds.length }} 篇论文）</span>
              <span class="detail-value">
                <a
                  v-for="fileId in visibleSourceFileIds"
                  :key="fileId"
                  class="source-file-link"
                  title="在线打开原文"
                  @click="openSourceFile(fileId)"
                  >{{ fileDisplayName(fileId) }}</a
                >
                <a
                  v-if="sourceFileIds.length > SOURCE_LIST_PREVIEW_COUNT"
                  class="expand-link"
                  @click="sourceListExpanded = !sourceListExpanded"
                  >{{ sourceListExpanded ? '收起' : `展开全部 ${sourceFileIds.length} 篇` }}</a
                >
              </span>
            </div>
            <div
              v-else-if="sourceFileIds.length === 1"
              :class="rowClass(fileDisplayName(sourceFileIds[0]))"
            >
              <span class="detail-label">来源文件</span>
              <span class="detail-value">
                <a
                  class="source-file-link"
                  title="在线打开原文"
                  @click="openSourceFile(sourceFileIds[0])"
                  >{{ fileDisplayName(sourceFileIds[0]) }}</a
                >
              </span>
            </div>
            <div v-else class="detail-row">
              <span class="detail-label">ID</span>
              <span class="detail-value detail-id">{{ item.id }}</span>
            </div>
            <template v-if="item.data?.original?.labels">
              <div class="detail-row">
                <span class="detail-label">标签</span>
                <span class="detail-value">
                  <a-tag v-for="tag in item.data.original.labels" :key="tag" size="small">{{
                    tag
                  }}</a-tag>
                </span>
              </div>
            </template>
            <template v-if="item.data?.original?.properties">
              <div
                v-for="(value, key) in item.data.original.properties"
                :key="key"
                :class="rowClass(value)"
              >
                <span class="detail-label">{{ key }}</span>
                <span class="detail-value">
                  <DetailValue :value="value" :field-key="key" :expanded-keys="expandedKeys" />
                </span>
              </div>
            </template>
          </template>
          <template v-else-if="type === 'edge'">
            <div :class="rowClass(item.data?.label)">
              <span class="detail-label">类型</span>
              <span class="detail-value">
                <DetailValue
                  :value="item.data?.label"
                  field-key="__type__"
                  :expanded-keys="expandedKeys"
                />
              </span>
            </div>
            <div v-if="sourceFileIds.length" :class="rowClass(fileDisplayName(sourceFileIds[0]))">
              <span class="detail-label">来源文件</span>
              <span class="detail-value">
                <a
                  class="source-file-link"
                  title="在线打开原文"
                  @click="openSourceFile(sourceFileIds[0])"
                  >{{ fileDisplayName(sourceFileIds[0]) }}</a
                >
              </span>
            </div>
            <template v-if="item.data?.original?.properties">
              <div
                v-for="(value, key) in filteredEdgeProperties"
                :key="key"
                :class="rowClass(value)"
              >
                <span class="detail-label">{{ key }}</span>
                <span class="detail-value">
                  <DetailValue :value="value" :field-key="key" :expanded-keys="expandedKeys" />
                </span>
              </div>
            </template>
          </template>
        </template>
      </div>
    </div>
  </transition>
</template>

<script setup>
import { computed, reactive, ref, watch, defineComponent, h } from 'vue'
import { X } from '@lucide/vue'
import { documentApi } from '@/apis/knowledge_api'
import { useDatabaseStore } from '@/stores/database'

const STACK_THRESHOLD = 50
const TRUNCATE_LIMIT = 100
const SOURCE_LIST_PREVIEW_COUNT = 3

const DetailValue = defineComponent({
  props: {
    value: [String, Number, Boolean, Object, Array],
    fieldKey: { type: String, required: true },
    expandedKeys: { type: Set, required: true }
  },
  setup(props) {
    return () => {
      const v = props.value
      if (typeof v !== 'string') return String(v ?? '')
      if (v.length <= TRUNCATE_LIMIT) return v
      if (props.expandedKeys.has(props.fieldKey)) {
        return [
          v,
          h(
            'a',
            {
              class: 'expand-link',
              onClick: (e) => {
                e.preventDefault()
                props.expandedKeys.delete(props.fieldKey)
              }
            },
            ' 收起'
          )
        ]
      }
      return [
        v.slice(0, TRUNCATE_LIMIT) + '...',
        h(
          'a',
          {
            class: 'expand-link',
            onClick: (e) => {
              e.preventDefault()
              props.expandedKeys.add(props.fieldKey)
            }
          },
          '展开'
        )
      ]
    }
  }
})

const props = defineProps({
  visible: Boolean,
  item: Object,
  type: String,
  kbId: String
})

defineEmits(['close'])

const expandedKeys = reactive(new Set())
const dbStore = useDatabaseStore()
const kbName = computed(() => dbStore.database?.name || '')

/** 当前详情项关联的知识文件 ID 列表：边取自身属性，节点取属性或聚合来源集合。 */
const sourceFileIds = computed(() => {
  const original = props.item?.data?.original
  if (!original) return []
  if (props.type === 'edge') {
    const fileId = original.properties?.file_id
    return fileId ? [fileId] : []
  }
  if (Array.isArray(original.sourceFileIds)) return original.sourceFileIds
  const fileId = original.properties?.file_id
  return fileId ? [String(fileId)] : []
})

const fileNames = reactive({})
const sourceListExpanded = ref(false)

/** 聚合来源默认只展示前几篇，避免面板过长。 */
const visibleSourceFileIds = computed(() =>
  sourceListExpanded.value ? sourceFileIds.value : sourceFileIds.value.slice(0, SOURCE_LIST_PREVIEW_COUNT)
)

const fileDisplayName = (fileId) => {
  const name = fileNames[fileId]
  if (name === undefined) return '文件名加载中…'
  return name || '文件名暂不可用'
}

watch(sourceFileIds, async (fileIds, _, onCleanup) => {
  sourceListExpanded.value = false
  if (!props.kbId || !fileIds.length) return
  let current = true
  onCleanup(() => {
    current = false
  })
  await Promise.all(
    fileIds.map(async (fileId) => {
      if (fileNames[fileId] !== undefined) return
      try {
        const data = await documentApi.getDocumentBasicInfo(props.kbId, fileId)
        if (current) fileNames[fileId] = data?.meta?.filename || data?.meta?.original_filename || ''
      } catch {
        if (current) fileNames[fileId] = ''
      }
    })
  )
})

/** 来源文件点击后复用文件详情弹窗在线打开原文。 */
const openSourceFile = (fileId) => dbStore.openFileDetail(fileId)

watch(
  () => props.item,
  () => {
    expandedKeys.clear()
    sourceListExpanded.value = false
  }
)

const isOverThreshold = (value) => typeof value === 'string' && value.length > STACK_THRESHOLD

const rowClass = (value) => {
  return isOverThreshold(value) ? 'detail-row detail-row--stack' : 'detail-row'
}

const title = computed(() => {
  return props.type === 'node' ? '节点详情' : '关系详情'
})

const filteredEdgeProperties = computed(() => {
  if (!props.item?.data?.original?.properties) return {}
  const properties = props.item.data.original.properties
  const filtered = {}
  const hiddenFields = ['source_id', 'target_id', '_id', 'truncate']
  Object.keys(properties).forEach((key) => {
    if (!hiddenFields.includes(key)) filtered[key] = properties[key]
  })
  return filtered
})
</script>

<style scoped lang="less">
.detail-panel {
  position: absolute;
  top: 60px;
  left: 10px;
  width: 280px;
  max-height: calc(100% - 60px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;
  user-select: auto;

  .panel-header {
    display: flex;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);

    .panel-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-1000);
    }

    .close-icon {
      margin-left: auto;
      cursor: pointer;
      color: var(--gray-500);
      transition: color 0.2s;

      &:hover {
        color: var(--gray-800);
      }
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.detail-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 6px 0;
  border-bottom: 1px solid var(--gray-50);

  &:last-child {
    border-bottom: none;
  }

  &--stack {
    flex-direction: column;
  }

  .detail-label {
    flex-shrink: 0;
    color: var(--gray-500);
    font-size: 12px;
    margin-right: 8px;
    margin-bottom: 2px;
  }

  .detail-value {
    text-align: right;
    color: var(--gray-800);
    font-size: 13px;
    word-break: break-all;

    &.detail-id {
      font-size: 11px;
      color: var(--gray-500);
    }

    :deep(.expand-link) {
      cursor: pointer;
      color: var(--main-700);
      font-size: 12px;
      white-space: nowrap;
      margin-left: 2px;

      &:hover {
        color: var(--main-500);
      }
    }

    .source-file-link {
      display: block;
      cursor: pointer;
      color: var(--main-700);

      &:hover {
        color: var(--main-500);
        text-decoration: underline;
      }
    }
  }
}

.detail-row--stack {
  .detail-value {
    text-align: left;
  }
}

.slide-fade-left-enter-active {
  transition: all 0.25s ease-out;
}

.slide-fade-left-leave-active {
  transition: all 0.2s cubic-bezier(1, 0.5, 0.8, 1);
}

.slide-fade-left-enter-from,
.slide-fade-left-leave-to {
  transform: translateX(-20px);
  opacity: 0;
}
</style>
