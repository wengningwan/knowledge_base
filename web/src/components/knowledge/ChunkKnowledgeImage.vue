<template>
  <img v-if="objectUrl" :src="objectUrl" :alt="alt" loading="lazy" class="chunk-knowledge-image" />
  <span v-else-if="failed" class="chunk-image-notice">图片加载失败</span>
  <span v-else class="chunk-image-notice">图片加载中…</span>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'

const props = defineProps({
  src: { type: String, required: true },
  alt: { type: String, default: '' }
})
const userStore = useUserStore()
const objectUrl = ref('')
const failed = ref(false)
let ticket = 0

/** 释放当前 object URL，来源切换与卸载共用。 */
function release() {
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = ''
  }
}

/** 知识库图片是会话认证资源：携带 Bearer 头取回 blob 再交给 img，旧请求按票据作废。 */
watch(() => props.src, (src) => {
  ticket += 1
  const current = ticket
  release()
  failed.value = false
  fetch(src, { headers: userStore.getAuthHeaders(), redirect: 'error' })
    .then(async (response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return response.blob()
    })
    .then((blob) => {
      if (ticket !== current) return
      objectUrl.value = URL.createObjectURL(blob)
    })
    .catch(() => {
      if (ticket === current) failed.value = true
    })
}, { immediate: true })

onBeforeUnmount(() => {
  ticket += 1
  release()
})
</script>

<style scoped lang="less">
.chunk-knowledge-image {
  max-width: 100%;
  height: auto;
  margin: 4px 0;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  vertical-align: middle;
}
.chunk-image-notice { color: var(--color-text-tertiary); font-size: 12px; }
</style>
