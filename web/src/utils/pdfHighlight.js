/** PDFium 归一化页面坐标（0-1，左上原点、y 向下）到高亮覆盖层的换算。 */

/** 钳制到 [0, 1] 并转为百分比数值，消除浮点尾差。 */
const toPercent = (value) => Math.round(Math.max(0, Math.min(1, value)) * 1000) / 10

/**
 * 把归一化 bbox 转为百分比定位样式：相对 canvas 显示盒，与渲染缩放无关；
 * 越界坐标钳制到页内，倒序坐标回退为零宽高（不产生负值）。
 */
export function pdfHighlightStyle(bbox) {
  const left = toPercent(bbox[0])
  const top = toPercent(bbox[1])
  return {
    left: `${left}%`,
    top: `${top}%`,
    width: `${Math.max(0, Math.round((toPercent(bbox[2]) - left) * 10) / 10)}%`,
    height: `${Math.max(0, Math.round((toPercent(bbox[3]) - top) * 10) / 10)}%`
  }
}
