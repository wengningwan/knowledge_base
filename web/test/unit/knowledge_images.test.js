import assert from 'node:assert/strict'
import test from 'node:test'
import { isKnowledgeImageProxyPath } from '../../src/utils/knowledge_images.js'

test('only site generated knowledge image paths can receive authentication', () => {
  assert.equal(isKnowledgeImageProxyPath('/api/knowledge/databases/k/images/kb-images/a.png', 'k'), true)
  for (const source of [
    'https://attacker.example/api/knowledge/databases/k/images/a.png',
    '//attacker.example/api/knowledge/databases/k/images/a.png',
    '/api/knowledge/databases/k/images/\\attacker.example/a.png',
    '/api/knowledge/databases/k/images/a.png?redirect=https://attacker.example',
    'javascript:/api/knowledge/databases/k/images/a.png',
    '/api/knowledge/databases/other/images/a.png',
  ]) assert.equal(isKnowledgeImageProxyPath(source, 'k'), false, source)
})
