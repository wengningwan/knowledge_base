<template>
  <div class="ocr-settings-section">
    <div class="section-title">默认 OCR 方法</div>
    <div class="settings-panel">
      <div class="setting-label">{{ items?.default_ocr_engine?.des || '默认 OCR 方法' }}</div>
      <OCRSelector
        :model-value="configStore.config?.default_ocr_engine"
        include-disable
        @update:model-value="configStore.setConfigValue('default_ocr_engine', $event)"
      />
    </div>

    <div class="settings-panel default-image-model-panel">
      <div class="setting-label">{{ items?.default_image_analysis_model?.des || '默认插图解析模型' }}</div>
      <ModelSelectorComponent
        :model_spec="configStore.config?.default_image_analysis_model"
        clearable
        placeholder="请选择支持图像输入的模型"
        @select-model="configStore.setConfigValue('default_image_analysis_model', $event)"
      />
      <p class="section-description">
        noOCR / OXR 解析 PDF 时可单独分析插图；留空保留原图，不生成图片说明。
      </p>
    </div>

    <div class="settings-panel">
      <div class="setting-label">{{ items?.default_document_review_model?.des || '默认正文校对模型' }}</div>
      <ModelSelectorComponent
        :model_spec="configStore.config?.default_document_review_model"
        clearable
        placeholder="请选择支持图像输入的模型"
        @select-model="configStore.setConfigValue('default_document_review_model', $event)"
      />
      <p class="section-description">OXR PDF 正文校对使用此模型，可与插图模型相同；留空不启用模型校对。</p>
    </div>

    <div class="settings-panel">
      <div class="settings-switch-row">
        <div class="setting-label">{{ items?.default_document_review_vision_final_authority?.des || '正文校对视觉终裁与自动处置' }}</div>
        <a-switch
          :checked="Boolean(configStore.config?.default_document_review_vision_final_authority)"
          checked-children="自动应用"
          un-checked-children="人工确认"
          @change="configStore.setConfigValue('default_document_review_vision_final_authority', $event)"
        />
      </div>
      <p class="section-description">
        开启后：排版级差异（标点/空白，内容不变）自动放行；替换型修正在 PDF 直读字符层证真后自动应用；
        无证据的模型修改自动保留 OCR 现状不占人工；引用标记误插自动清理；结构类差异按影响分级保留
        （公式碎片簇转人工）；模型确认无差异但文字层证出唯一误插内容时自动删除；技术失败自动重试一轮。
        关闭则以上全部转人工确认。默认关闭，保持人工确认闭环。
      </p>
    </div>

    <div class="settings-panel">
      <div class="setting-label">文档模型请求并发数（每端点与模型共享）</div>
      <a-input-number v-model:value="modelConcurrency" :min="1" :precision="0" :disabled="savingConcurrency" />
      <a-button :loading="savingConcurrency" @click="saveConcurrency">保存</a-button>
      <p class="section-description">正文校对与图片解析、多文件及 worker 共用此额度；剩余请求继续排队。默认 2，实际并行能力取决于模型服务。</p>
    </div>

    <div class="section-title">OCR 服务配置</div>
    <p class="section-description">
      仅展示需要配置的服务。保存空值会清除数据库配置，并读取对应环境变量。
    </p>
    <div class="option-list">
      <section v-for="option in configOptions" :key="option.key" class="option-card">
        <header class="option-header">
          <div>
            <h4>{{ option.name }}</h4>
            <p>{{ option.description }}</p>
            <p v-if="option.key === 'oxr_api_opts'">请填写并保存 API Base URL；未配置地址时不会自动使用免费服务。免费试用可填 https://free-oxr.somark.cn/api/v1，API Key 留空。</p>
          </div>
          <div class="option-actions" :class="{ editing: editingKey === option.key }">
            <template v-if="editingKey === option.key">
              <a-button size="small" :disabled="savingOption === option.key" @click="cancelEditing">
                取消
              </a-button>
              <a-button
                type="primary"
                size="small"
                class="save-button"
                :loading="savingOption === option.key"
                @click="saveOption(option)"
              >
                保存
              </a-button>
            </template>
            <a-button
              v-else
              size="small"
              :disabled="Boolean(editingKey)"
              @click="startEditing(option)"
            >
              编辑
            </a-button>
          </div>
        </header>
        <div v-if="editingKey === option.key" class="option-fields">
          <label v-for="field in option.params.fields" :key="field.key" class="option-field">
            <span class="setting-label">{{ field.label }}</span>
            <a-input-password
              v-if="field.sensitive"
              v-model:value="draftValue[field.key]"
              :placeholder="field.environment"
              autocomplete="new-password"
            />
            <a-input
              v-else
              v-model:value="draftValue[field.key]"
              :placeholder="field.placeholder || field.environment"
              allow-clear
            />
            <small>
              {{ field.sensitive ? '留空并保存会清除数据库中的值。' : field.help }}
            </small>
          </label>
        </div>
        <div v-else class="option-fields option-values">
          <div v-for="field in option.params.fields" :key="field.key" class="option-field">
            <span class="setting-label">{{ field.label }}</span>
            <a-input
              :value="getFieldDisplay(option, field)"
              :class="{ 'masked-value': field.sensitive }"
              disabled
            />
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { configApi, configOptionsApi } from '@/apis/system_api'
import { useConfigStore } from '@/stores/config'
import OCRSelector from '@/components/OCRSelector.vue'
import ModelSelectorComponent from '@/components/ModelSelectorComponent.vue'

const configStore = useConfigStore()
const modelConcurrency = ref(Number(configStore.config?.document_model_concurrency ?? 2))
const savingConcurrency = ref(false)
watch(() => configStore.config?.document_model_concurrency, (value) => {
  modelConcurrency.value = Number(value ?? 2)
})
const saveConcurrency = async () => {
  if (!Number.isSafeInteger(modelConcurrency.value) || modelConcurrency.value < 1) {
    message.error('并发数必须是正整数')
    return
  }
  savingConcurrency.value = true
  try {
    await configApi.updateConfig('document_model_concurrency', modelConcurrency.value)
    await configStore.refreshConfig()
    message.success('并发配置已保存')
  } catch (error) {
    message.error(error.message || '保存并发配置失败')
  } finally {
    savingConcurrency.value = false
  }
}
const OCR_OPTION_KEYS = new Set([
  'mineru_ocr_host_opts',
  'mineru_official_api_opts',
  'pp_structure_v3_ocr_host_opts',
  'paddleocr_api_opts',
  'oxr_api_opts'
])
const items = computed(() => configStore.config?._config_items || {})
const configOptions = ref([])
const editingKey = ref('')
const draftValue = ref({})
const savingOption = ref('')

const loadConfigOptions = async () => {
  try {
    const data = await configOptionsApi.getOptions()
    configOptions.value = (data.options || [])
      .filter((option) => OCR_OPTION_KEYS.has(option.key))
      .map((option) => ({
        ...option,
        value: { ...(option.value || {}) },
        sensitive_state: { ...(option.sensitive_state || {}) }
      }))
  } catch (error) {
    message.error(error.message || '加载 OCR 服务配置失败')
  }
}

const startEditing = (option) => {
  editingKey.value = option.key
  draftValue.value = { ...(option.value || {}) }
}

const cancelEditing = () => {
  editingKey.value = ''
  draftValue.value = {}
}

const getFieldDisplay = (option, field) => {
  if (!field.sensitive) {
    return option.value?.[field.key] || `读取 ${field.environment}`
  }
  const state = option.sensitive_state?.[field.key]
  if (state?.source === 'database') return state.preview
  if (state?.source === 'environment') return `已通过 ${field.environment} 配置`
  return '未配置'
}

const saveOption = async (option) => {
  savingOption.value = option.key
  try {
    const data = await configOptionsApi.updateOption(option.key, draftValue.value)
    Object.assign(option, data.option, {
      value: { ...(data.option.value || {}) },
      sensitive_state: { ...(data.option.sensitive_state || {}) }
    })
    cancelEditing()
    message.success('配置已保存')
  } catch (error) {
    message.error(error.message || '保存配置失败')
  } finally {
    savingOption.value = ''
  }
}

onMounted(loadConfigOptions)
</script>

<style lang="less" scoped>
.settings-panel,
.option-card {
  padding: 16px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
}

.settings-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;

  & + .settings-panel {
    margin-top: 12px;
  }
}

.section-title {
  margin: 0 0 10px;
  color: var(--gray-900);
  font-size: 15px;
  font-weight: 600;

  &:not(:first-child) {
    margin-top: 24px;
  }
}

.section-description {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.settings-switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.option-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}

.option-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;

  h4,
  p {
    margin: 0;
  }

  h4 {
    color: var(--gray-900);
    font-size: 14px;
    font-weight: 500;
  }

  p {
    margin-top: 3px;
    color: var(--color-text-secondary);
    font-size: 12px;
  }
}

.option-actions {
  display: flex;
  flex: none;
  gap: 8px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
}

.option-card:hover .option-actions,
.option-card:focus-within .option-actions,
.option-actions.editing {
  opacity: 1;
  pointer-events: auto;
}

.save-button {
  border-color: var(--gray-900);
  background: var(--gray-900);

  &:hover,
  &:focus {
    border-color: var(--gray-700);
    background: var(--gray-700);
  }
}

.option-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 16px;
}

.option-field {
  display: flex;
  flex-direction: column;
  gap: 6px;

  &:only-child {
    grid-column: 1 / -1;
  }

  small {
    color: var(--color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }
}

.setting-label {
  color: var(--gray-700);
  font-size: 13px;
  font-weight: 500;
}

.masked-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  letter-spacing: 0.02em;
}

@media (max-width: 680px) {
  .option-fields {
    grid-template-columns: 1fr;
  }

  .option-field:only-child {
    grid-column: auto;
  }
}
</style>
