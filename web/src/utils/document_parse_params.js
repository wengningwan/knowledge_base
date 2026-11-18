/** 首次继承系统默认值，保留用户显式清空的模型。 */
export function resolveDocumentParseParams(saved = {}, defaults = {}, fallbackEngine = 'disable') {
  const modelValue = (key) =>
    String(Object.hasOwn(saved, key) ? saved[key] || '' : defaults[`default_${key}`] || '')
  return {
    pdf_parse_mode: saved.pdf_parse_mode === 'model' ? 'model' : 'ocr',
    document_recognition_model: String(saved.document_recognition_model || ''),
    ocr_engine: saved.ocr_engine || defaults.default_ocr_engine || fallbackEngine,
    image_analysis_model: modelValue('image_analysis_model'),
    document_review_model: modelValue('document_review_model')
  }
}

/** 按识别方式收敛参数，模型识别必须显式选择模型。 */
export function buildDocumentParseParams(params) {
  if (params.pdf_parse_mode === 'model') {
    const model = String(params.document_recognition_model || '').trim()
    if (!model) throw new Error('请选择文档识别模型后再开始解析')
    return {
      pdf_parse_mode: 'model',
      document_recognition_model: model,
      document_review_model: params.document_review_model || '',
      image_analysis_model: params.image_analysis_model || ''
    }
  }
  const result = { pdf_parse_mode: 'ocr' }
  if (params.ocr_engine) result.ocr_engine = params.ocr_engine
  if (['disable', 'oxr_ocr'].includes(params.ocr_engine)) {
    result.image_analysis_model = params.image_analysis_model || ''
  }
  if (params.ocr_engine === 'oxr_ocr') {
    result.document_review_model = params.document_review_model || ''
  }
  return result
}
