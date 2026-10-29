import assert from 'node:assert/strict'
import test from 'node:test'

import { inlineKnowledgeImages } from '../../src/utils/markdown_export.js'

const source = '/api/knowledge/databases/kb_example/images/kb-images/figure.png'
const image = `![figure_p1_1](${source})`

test('下载 Markdown 内嵌图片并保留公式和原图注', async () => {
  const content = `# 标题\n\n$$W_0=V_0\\rho_M$$\n\n${image}\n\nFigure 1. 原图注`
  const result = await inlineKnowledgeImages(content, async () => new Blob(['png'], { type: 'image/png' }))
  assert.ok(result.includes('![figure_p1_1](data:image/png;base64,cG5n)'))
  assert.ok(result.includes('$$W_0=V_0\\rho_M$$'))
  assert.ok(result.includes('Figure 1. 原图注'))
  assert.ok(!result.includes('/api/knowledge/'))
})

test('HTML 表格的图片离线内嵌，保留其他属性及代码中的相同地址', async () => {
  const html = `<table><tr><td><img title="${source}" src='${source}' width="100"></td></tr></table>`
  const content = `${html}\n\n\`<img src="${source}">\`\n\n\`\`\`html\n${html}\n\`\`\``
  const result = await inlineKnowledgeImages(content, async () => new Blob(['png'], { type: 'image/png' }), 'kb_example')
  assert.equal(result, content.replace(`src='${source}'`, "src='data:image/png;base64,cG5n'"))
})

test('HTML img 只读取当前 KB 的 src，不跟随外部地址或 data-src', async () => {
  const content = `<table><tr><td><img src="https://example.com/image.png"><img data-src="${source}"><img src="/api/knowledge/databases/other/images/figure.png"></td></tr></table>`
  assert.equal(await inlineKnowledgeImages(content, async () => assert.fail('不能跨 KB 或请求外链'), 'kb_example'), content)
})

test('HTML 必需图失败同样终止导出，HTML code 示例不发请求', async () => {
  await assert.rejects(inlineKnowledgeImages(`<table><tr><td><img src="${source}"></td></tr></table>`, async () => {
    throw new Error('403')
  }), /403/)
  const code = `<pre><code><img src="${source}"></code></pre>`
  assert.equal(await inlineKnowledgeImages(code, async () => assert.fail('不能读取代码图片')), code)
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


test('围栏中的单反引号不能把后面的真实图片误作代码', async () => {
  const content = ['~~~text', '`', '~~~', '', image, '', '`code`'].join('\n')
  const result = await inlineKnowledgeImages(content, async () => new Blob(['pixels'], { type: 'image/png' }))
  assert.ok(result.includes('data:image/png;base64,'))
  assert.ok(result.includes('~~~text\n`\n~~~'))
})
