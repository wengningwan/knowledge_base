/** 首次继承系统默认值，保留用户显式清空的模型。 */
export function resolveDocumentParseParams(saved = {}, defaults = {}, fallbackEngine = 'disable') {
  const modelValue = (key) =>
    String(Object.hasOwn(saved, key) ? saved[key] || '' : defaults[`default_${key}`] || '')
  return {
    ocr_engine: saved.ocr_engine || defaults.default_ocr_engine || fallbackEngine,
    image_analysis_model: modelValue('image_analysis_model'),
    document_review_model: modelValue('document_review_model')
  }
}

/** 仅为 noOCR 与 OXR 发送各自支持的独立模型配置。 */
export function buildDocumentParseParams(params) {
  const result = { ocr_engine: params.ocr_engine }
  if (['disable', 'oxr_ocr'].includes(params.ocr_engine)) {
    result.image_analysis_model = params.image_analysis_model || ''
  }
  if (params.ocr_engine === 'oxr_ocr') {
    result.document_review_model = params.document_review_model || ''
  }
  return result
}
