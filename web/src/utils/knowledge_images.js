/** 仅站内生成的图片代理路径可以携带会话认证。 */
export const isKnowledgeImageProxyPath = (source, kbId = null) => {
  if (typeof source !== 'string') return false
  const match = source.match(/^\/api\/knowledge\/databases\/([^/?#]+)\/images\/[^\s<>"'()?#\\]+$/)
  return Boolean(match && (kbId === null || match[1] === encodeURIComponent(kbId)))
}
