import MarkdownIt from 'markdown-it'
import markdownItKatex from '@vscode/markdown-it-katex'
import katex from 'katex'
import 'katex/contrib/mhchem'
import taskLists from 'markdown-it-task-lists'
import DOMPurify from 'dompurify'
import { createHighlighter } from 'shiki'
import { load as yamlLoad } from 'js-yaml'
import { escapeHtml } from './html.js'
import { normalizeCodeLanguage } from './file_preview.js'
import { renderSvgBlocks } from './svgRenderer.js'
import { renderHtmlPreviewBlocks } from './htmlPreviewRenderer.js'
import { createMarkdownRenderCache } from './markdownRenderCache.js'

const markdownKatexPlugin = markdownItKatex.default || markdownItKatex
const FRONTMATTER_MARKER = '---'
const LEGACY_MINIO_PUBLIC_URL_RE = /https?:\/\/[^/\s)]+:9000\/public\//gi

let highlighterPromise
const getHighlighter = () => {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      themes: ['github-light', 'github-dark'],
      langs: ['plaintext']
    }).catch((error) => {
      highlighterPromise = undefined
      throw error
    })
  }
  return highlighterPromise
}

const normalizeHtmlTagQuotes = (content) => {
  const source = String(content || '')
  if (!/[“”]/.test(source)) return source
  return source.replace(/<[^>]+>/g, (tag) => tag.replaceAll('“', '"').replaceAll('”', '"'))
}

export const normalizeLegacyMinioPublicUrls = (content) =>
  String(content || '').replace(LEGACY_MINIO_PUBLIC_URL_RE, '/minio/public/')

const renderFrontmatterValue = (value) => {
  if (Array.isArray(value)) {
    return value.map((item) => `<span class="fm-tag">${escapeHtml(item)}</span>`).join('')
  }

  if (value instanceof Date) {
    return escapeHtml(value.toISOString().slice(0, 10))
  }

  if (typeof value === 'object' && value !== null) {
    return `<pre class="fm-json">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`
  }

  return escapeHtml(value)
}

const renderFrontmatterField = (key, value) => {
  if (key === 'title') {
    return `<strong class="fm-doc-title">${escapeHtml(value)}</strong>`
  }

  if (key === 'date') {
    return `<time>${renderFrontmatterValue(value)}</time>`
  }

  if (key === 'tags' && Array.isArray(value)) {
    return value.map((tag) => `<span class="fm-tag">#${escapeHtml(tag)}</span>`).join('')
  }

  return renderFrontmatterValue(value)
}

const getMarkdownLine = (state, line) => {
  const start = state.bMarks[line]
  const end = state.eMarks[line]
  return state.src.slice(start, end)
}

const renderFrontmatterCard = (data) => {
  const rows = Object.entries(data)
    .map(
      ([key, value]) => `
        <div class="fm-row">
          <div class="fm-key">${escapeHtml(key)}</div>
          <div class="fm-value">${renderFrontmatterField(key, value)}</div>
        </div>`
    )
    .join('')

  return `<section class="frontmatter-card">
    <div class="fm-body">${rows || '<div class="fm-empty">无 frontmatter 信息</div>'}</div>
  </section>`
}

const markdownItFrontmatterCard = (md) => {
  md.block.ruler.before('table', 'frontmatter_card', (state, startLine, endLine, silent) => {
    if (startLine !== 0 || getMarkdownLine(state, startLine).trim() !== FRONTMATTER_MARKER) {
      return false
    }

    let nextLine = startLine + 1
    while (nextLine < endLine && getMarkdownLine(state, nextLine).trim() !== FRONTMATTER_MARKER) {
      nextLine += 1
    }

    if (nextLine >= endLine) return false

    const rawYaml = Array.from({ length: nextLine - startLine - 1 }, (_, index) =>
      getMarkdownLine(state, startLine + index + 1)
    ).join('\n')
    let data

    try {
      data = yamlLoad(rawYaml) || {}
    } catch {
      const foldedDescription = rawYaml.replace(
        /^description:[ \t]*\n(?=[ \t]+\S)/m,
        'description: >-\n'
      )
      if (foldedDescription === rawYaml) return false

      try {
        data = yamlLoad(foldedDescription) || {}
      } catch {
        return false
      }
    }

    if (!data || typeof data !== 'object' || Array.isArray(data)) return false
    if (silent) return true

    const token = state.push('frontmatter_card', 'section', 0)
    token.block = true
    token.map = [startLine, nextLine + 1]
    token.meta = { data }
    state.line = nextLine + 1
    return true
  })

  md.renderer.rules.frontmatter_card = (tokens, idx) => renderFrontmatterCard(tokens[idx].meta.data)
}

// 附着式行内公式：原插件要求 $ 前不能是字母或数字，导致 sp$^{3}$、min$^{-1}$ 等
// 学术正文常见附着形态整段回退为原文。放宽版仅当 $ 后紧跟 ^、_ 或 \（上标、下标、
// LaTeX 命令特征）时放行前置字母数字；金额 $5 与插值 ${...} 形态不放宽。
const ATTACHED_MATH_NEXT_RE = /^[\\^_]$/
const isWhitespaceChar = (char) => /^\s$/u.test(char)
const isWordCharacterOrNumber = (char) => /^[\w\d]$/u.test(char)

const isValidInlineDelimRelaxed = (src, pos, adjacentToInlineMath = false) => {
  const prevChar = src[pos - 1]
  const char = src[pos]
  const nextChar = src[pos + 1]
  if (char !== '$') {
    return { can_open: false, can_close: false }
  }
  let canOpen = false
  let canClose = false
  if (
    prevChar !== '$' && prevChar !== '\\'
    && (prevChar === undefined || isWhitespaceChar(prevChar) || !isWordCharacterOrNumber(prevChar))
  ) {
    canOpen = true
  } else if (isWordCharacterOrNumber(prevChar) && ATTACHED_MATH_NEXT_RE.test(nextChar)) {
    canOpen = true
  } else if (prevChar === '$' && adjacentToInlineMath) {
    canOpen = true
  }
  // 闭定界仅拒绝构成 $$ 的情形（块级/行中块定界识别）；数字、字母、下一公式的
  // 开定界均允许——$\sim$529.7 eV 紧贴数值、$25^{\circ}$C 紧贴单位、
  // $^{*}$$^{\dagger}$ 相邻公式都必须正常闭合。金额形态改由配对后的内容
  // 特征校验拦截（见 inlineMathRelaxed）。
  if (nextChar !== '$' || src[pos + 2] !== '$') {
    canClose = true
  }
  return { can_open: canOpen, can_close: canClose }
}

// 金额形态判定：开定界后紧跟数字、且配对内容不含任何 LaTeX 结构特征
// （命令、上下标、花括号）时视为金额或纯文本（如 "$5 到 $10"），
// 不作为公式；数值公式以数字开头（$2.38 \times 10^{-3}$）因含特征被放行。
const MONEY_LIKE_INNER_RE = /^[^\\^_{]*$/u

const isMoneyLikePair = (src, openerPos, closerPos) => {
  const afterOpener = src[openerPos + 1]
  if (!/\d/u.test(afterOpener ?? '')) {
    return false
  }
  return MONEY_LIKE_INNER_RE.test(src.slice(openerPos + 1, closerPos))
}

// 行内数学规则的全量接管版本；查找与产出逻辑与 @vscode/markdown-it-katex 的
// inlineMath 一致，仅开定界判定换为放宽版。注册在原规则之前，原规则不再被
// 单个 $ 触发；行中 $$ 与块级 $$ 仍由插件原有规则先行处理，不受影响。
const inlineMathRelaxed = (state, silent) => {
  if (state.src[state.pos] !== '$') {
    return false
  }
  const lastToken = state.tokens.at(-1)
  if (lastToken?.type === 'html_inline' && /^<\w+.+[^/]>$/.test(lastToken.content)) {
    return false
  }
  // 相邻公式（$^{*}$$^{\dagger}$）：当前开定界紧跟上一个行内公式的闭合定界。
  // 块级/行中块 $$ 由插件原有规则先行消费，到达此处且上一 token 是行内公式时，
  // prevChar === '$' 只是相邻边界而非块级定界，放行开定界。
  const adjacentToInlineMath = state.src[state.pos - 1] === '$' && lastToken?.type === 'math_inline'
  let res = isValidInlineDelimRelaxed(state.src, state.pos, adjacentToInlineMath)
  if (!res.can_open) {
    if (!silent) {
      state.pending += '$'
    }
    state.pos += 1
    return true
  }
  const start = state.pos + 1
  let match = start
  while ((match = state.src.indexOf('$', match)) !== -1) {
    // 连续反斜杠为成对转义时，当前 $ 不是闭合定界，继续向后找
    let pos = match - 1
    while (state.src[pos] === '\\') {
      pos -= 1
    }
    if ((match - pos) % 2 === 1) {
      break
    }
    match += 1
  }
  if (match === -1) {
    if (!silent) {
      state.pending += '$'
    }
    state.pos = start
    return true
  }
  if (match - start === 0) {
    if (!silent) {
      state.pending += '$$'
    }
    state.pos = start + 1
    return true
  }
  res = isValidInlineDelimRelaxed(state.src, match)
  if (!res.can_close) {
    if (!silent) {
      state.pending += '$'
    }
    state.pos = start
    return true
  }
  // 配对内容是金额/纯文本形态（数字开头且无 LaTeX 特征）时不产出公式
  if (isMoneyLikePair(state.src, start - 1, match)) {
    if (!silent) {
      state.pending += '$'
    }
    state.pos = start
    return true
  }
  if (!silent) {
    const token = state.push('math_inline', 'math', 0)
    token.markup = '$'
    token.content = state.src.slice(start, match)
  }
  state.pos = match + 1
  return true
}

const markdownItRelaxedInlineMath = (md) => {
  md.inline.ruler.before('math_inline', 'math_inline_relaxed', inlineMathRelaxed)
}

const rendererCache = new Map()
const renderedHtmlCache = createMarkdownRenderCache()
const CODE_FENCE_RE = /(^|\n) {0,3}(```|~~~)/
const CODE_FENCE_LANGUAGE_RE = /(^|\n) {0,3}(```+|~~~+)[ \t]*([^\s:,`]*)/g

const normalizeTheme = (theme) => (theme === 'github-dark' ? 'github-dark' : 'github-light')
const hasCodeFence = (content) => CODE_FENCE_RE.test(content)

const sanitizeHtmlPreviewSrcdoc = (html) =>
  DOMPurify.sanitize(html, {
    WHOLE_DOCUMENT: true,
    ADD_TAGS: ['html', 'head', 'body', 'style', 'link'],
    FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'input', 'textarea', 'select'],
    FORBID_ATTR: ['srcdoc', 'sandbox']
  })

const collectCodeFenceLanguages = (content) => {
  const languages = new Set()
  for (const match of String(content || '').matchAll(CODE_FENCE_LANGUAGE_RE)) {
    const language = normalizeCodeLanguage(match[3])
    if (language) languages.add(language)
  }
  return [...languages]
}

const ensureLanguages = async (highlighter, languages) => {
  const loaded = new Set(highlighter.getLoadedLanguages())
  await Promise.all(
    languages
      .filter((language) => !loaded.has(language))
      .map((language) => {
        try {
          return highlighter.loadLanguage(language).catch(() => null)
        } catch {
          return null
        }
      })
  )
}

export const createMarkdownRenderer = ({ themeName, highlighter }) =>
  new MarkdownIt({
    html: true,
    breaks: true,
    linkify: true,
    typographer: true,
    highlight: highlighter
      ? (code, lang) => {
          const language = normalizeCodeLanguage(lang)
          const loadedLanguages = highlighter.getLoadedLanguages()
          const targetLanguage = loadedLanguages.includes(language) ? language : 'plaintext'
          return highlighter.codeToHtml(code, { lang: targetLanguage, theme: themeName })
        }
      : undefined
  })
    // 解析正文中的表格以 HTML <table> 存储，块级 HTML 原样输出导致内部 $...$
    // 不走行内规则；开启插件官方开关让 core 阶段拆分 HTML 块内的数学 token
    .use(markdownKatexPlugin, {
      katex,
      throwOnError: false,
      errorColor: '#cc0000',
      trust: false,
      enableMathBlockInHtml: true,
      enableMathInlineInHtml: true
    })
    .use(markdownItRelaxedInlineMath)
    .use(taskLists, { enabled: false, label: false, labelAfter: false })
    .use(markdownItFrontmatterCard)

const getRenderer = async (theme, needsHighlight) => {
  const themeName = normalizeTheme(theme)
  const cacheKey = needsHighlight ? themeName : 'plain'
  const cached = rendererCache.get(cacheKey)
  if (cached) return cached

  const rendererPromise = needsHighlight
    ? getHighlighter()
        .then((highlighter) => createMarkdownRenderer({ themeName, highlighter }))
        .catch((error) => {
          console.warn('Markdown code highlighting unavailable, rendering without Shiki:', error)
          return createMarkdownRenderer({ themeName })
        })
    : Promise.resolve(createMarkdownRenderer({ themeName }))
  rendererCache.set(cacheKey, rendererPromise)
  return rendererPromise
}

export const renderMarkdown = async (content, { theme = 'github-light' } = {}) => {
  try {
    const normalizedContent = normalizeHtmlTagQuotes(normalizeLegacyMinioPublicUrls(content))
    const htmlPreviewContent = renderHtmlPreviewBlocks(normalizedContent, {
      sanitizeHtml: sanitizeHtmlPreviewSrcdoc
    })
    const svgContent = renderSvgBlocks(htmlPreviewContent)
    const themeName = normalizeTheme(theme)
    const needsHighlight = hasCodeFence(svgContent)
    const cacheKey = `${needsHighlight ? themeName : 'plain'}\u0000${svgContent}`
    const cachedHtml = renderedHtmlCache.get(cacheKey)
    if (cachedHtml !== undefined) return cachedHtml

    if (needsHighlight) {
      try {
        const highlighter = await getHighlighter()
        await ensureLanguages(highlighter, collectCodeFenceLanguages(svgContent))
      } catch (error) {
        console.warn('Markdown languages unavailable, continuing without code highlighting:', error)
      }
    }

    const md = await getRenderer(themeName, needsHighlight)
    const html = DOMPurify.sanitize(md.render(svgContent), {
      ADD_TAGS: ['input'],
      ADD_ATTR: [
        'class',
        'style',
        'target',
        'rel',
        'type',
        'checked',
        'disabled',
        'source',
        'colspan',
        'rowspan'
      ]
    })
    renderedHtmlCache.set(cacheKey, html)
    return html
  } catch (error) {
    console.error('Failed to render markdown:', error)
    return `<pre>${escapeHtml(content)}</pre>`
  }
}
