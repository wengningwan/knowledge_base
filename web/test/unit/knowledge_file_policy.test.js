import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canReparseFile, canSelectFile, canPreviewParseAssets, canPreviewParsed, getFilePrimaryAction
} from '../../src/utils/knowledge_file_policy.js'

test('重解析仅允许有原文件的稳定终态，正常入库仍是主操作', () => {
  for (const status of ['parsed', 'indexed', 'done', 'error_indexing']) {
    assert.equal(canReparseFile({ status, has_original_file: true }), true)
    assert.equal(canReparseFile({ status, has_original_file: false, minio_url: 'old' }), false)
    assert.equal(canReparseFile({ status, has_original_file: true, is_folder: true }), false)
    assert.equal(canReparseFile({ status, has_original_file: true, file_type: 'url' }), false)
  }
  assert.equal(canReparseFile({ status: 'parsed', path: 'legacy.pdf' }), true)
  assert.equal(canReparseFile({ status: 'parsed' }), false)
  assert.deepEqual(getFilePrimaryAction({ status: 'parsed' }), { type: 'index', label: '入库' })
})

test('处理中和未解析文件不能重新解析，忙碌文件不能进入批量选择', () => {
  for (const status of ['parsing', 'indexing', 'processing', 'waiting', 'uploaded', 'error_parsing']) {
    assert.equal(canReparseFile({ status, has_original_file: true }), false)
  }
  for (const status of ['parsing', 'indexing', 'processing', 'waiting']) {
    assert.equal(canSelectFile({ status }), false)
  }
})

test('产物 metadata 与旧文件解析预览入口共存，显式正文能力不被状态猜测覆盖', () => {
  assert.equal(canPreviewParseAssets({ status: 'parsed', has_parse_assets: false }), true)
  assert.equal(canPreviewParseAssets({ status: 'uploaded', has_parse_assets: true }), true)
  assert.equal(canPreviewParseAssets({ status: 'uploaded', has_parse_assets: false }), false)
  assert.equal(canPreviewParseAssets({ status: 'parsed', is_folder: true }), false)
  assert.equal(canPreviewParsed({ status: 'parsed', has_parsed_markdown: false }), false)
})
