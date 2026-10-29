import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveDocumentParseParams, buildDocumentParseParams } from '../../src/utils/document_parse_params.js'

const defaults = {
  default_ocr_engine: 'oxr_ocr', default_image_analysis_model: 'vision/image',
  default_document_review_model: 'vision/review'
}

test('首次解析继承独立默认模型，两个用途可选择同一模型', () => {
  assert.deepEqual(resolveDocumentParseParams({}, defaults), {
    ocr_engine: 'oxr_ocr', image_analysis_model: 'vision/image', document_review_model: 'vision/review'
  })
  assert.deepEqual(buildDocumentParseParams({
    ocr_engine: 'oxr_ocr', image_analysis_model: 'vision/shared', document_review_model: 'vision/shared'
  }), { ocr_engine: 'oxr_ocr', image_analysis_model: 'vision/shared', document_review_model: 'vision/shared' })
})

test('保存或手动清空的模型不被系统默认值覆盖，缺失字段才继承', () => {
  const saved = { ocr_engine: 'oxr_ocr', image_analysis_model: '', document_review_model: null }
  assert.deepEqual(resolveDocumentParseParams(saved, defaults), {
    ocr_engine: 'oxr_ocr', image_analysis_model: '', document_review_model: ''
  })
  assert.equal(resolveDocumentParseParams({ image_analysis_model: '' }, defaults).document_review_model, 'vision/review')
  assert.deepEqual(buildDocumentParseParams(resolveDocumentParseParams(saved, defaults)), {
    ocr_engine: 'oxr_ocr', image_analysis_model: '', document_review_model: ''
  })
})

test('noOCR 只使用图片模型，其他 OCR 不携带额外模型或重解析标记', () => {
  const params = { image_analysis_model: 'vision/image', document_review_model: 'vision/review', reparse: true }
  assert.deepEqual(buildDocumentParseParams({ ...params, ocr_engine: 'disable' }), {
    ocr_engine: 'disable', image_analysis_model: 'vision/image'
  })
  assert.deepEqual(buildDocumentParseParams({ ...params, ocr_engine: 'mineru' }), { ocr_engine: 'mineru' })
})
