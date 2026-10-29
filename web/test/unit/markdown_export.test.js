import assert from 'node:assert/strict'
import test from 'node:test'

import { inlineKnowledgeImages } from '../../src/utils/markdown_export.js'

const source = '/api/knowledge/databases/kb_example/images/kb-images/figure.png'
const image = `![figure_p1_1](${source})`

test('下载 Markdown 内嵌图片并保留公式、原图注和独立模型解释', async () => {
  const content = `# 标题\n\n$$W_0=V_0\\rho_M$$\n\n${image}\n\n> **插图解析（模型生成）**\n> 曲线趋势\n\nFigure 1. 原图注`
  const result = await inlineKnowledgeImages(content, async () => new Blob(['png'], { type: 'image/png' }))
  assert.ok(result.includes('![figure_p1_1](data:image/png;base64,cG5n)'))
  assert.ok(result.includes('$$W_0=V_0\\rho_M$$'))
  assert.ok(result.includes('Figure 1. 原图注'))
  assert.ok(result.includes('插图解析（模型生成）'))
  assert.ok(!result.includes('/api/knowledge/'))
})

test('同一图片重复引用只读取一次', async () => {
  let count = 0
  const result = await inlineKnowledgeImages(`${image}\n\n${image}`, async () => {
    count += 1
    return new Blob(['png'], { type: 'image/png' })
  })
  assert.equal(count, 1)
  assert.equal(result.split('data:image/png;base64,').length - 1, 2)
})

test('不读取外部地址、代码围栏或行内代码中的图片示例', async () => {
  const content = `![external](https://example.com/image.png)\n\n\`\`\`md\n${image}\n\`\`\`\n\n示例 \`${image}\``
  const result = await inlineKnowledgeImages(content, async () => {
    assert.fail('不能请求示例或外部图片')
  })
  assert.equal(result, content)
})

test('图片读取失败、空图片或返回 HTML 时阻止下载', async () => {
  await assert.rejects(
    inlineKnowledgeImages(image, async () => {
      throw new Error('403')
    }),
    /403/
  )
  await assert.rejects(
    inlineKnowledgeImages(image, async () => new Blob(['login'], { type: 'text/html' })),
    /格式无效/
  )
  await assert.rejects(
    inlineKnowledgeImages(image, async () => new Blob([], { type: 'image/png' })),
    /图片为空/
  )
})
