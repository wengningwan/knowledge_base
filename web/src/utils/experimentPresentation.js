import { splitMathSegments, renderMathHtml } from '@/utils/documentReview'

export const experimentFields = {
  schema_id: '格式标识', schema_version: '格式版本', record_id: '记录编号',
  extraction_metadata: '提取信息', document: '论文题录', evidence: '原文证据',
  chemical_entities: '化学物质', material_identities: '材料类型', sample_batches: '样品批次',
  sample_states: '样品状态', synthesis_routes: '制备路线', experiment_runs: '实验登记',
  series_sets: '曲线', observations: '实验观测', mechanism_claims: '机制解释',
  missing_information: '缺失信息', validation_issues: '校验说明', quality_summary: '覆盖摘要'
}

const labels = {
  characterization: '材料表征', equilibrium_isotherm: '平衡吸附', isotherm_model_fit: '模型拟合',
  mixture_prediction: '混合气预测', isosteric_heat_derivation: '等量吸附热推导',
  breakthrough: '动态穿透', computational_adsorption: '计算吸附',
  gas_physisorption: '氮气吸附测试', scanning_electron_microscopy: '扫描电子显微镜',
  Raman_spectroscopy: '拉曼光谱', thermogravimetric_analysis: '热重分析',
  'X-ray_photoelectron_spectroscopy': 'X 射线光电子能谱',
  'textural.surface_area.specific': 'BET 比表面积', 'textural.pore_volume.total': '总孔容',
  'raman.I_D/I_G': '拉曼强度比 Iᴅ/Iɢ', textural_properties: '孔结构参数',
  spectral_ratio: '光谱比值', spectral_peak_assignments: '光谱峰归属',
  surface_elemental_composition: '表面元素组成', qualitative_characterization: '定性表征',
  qualitative_results: '文字观察', reported_observation: '文字观察',
  equilibrium_uptake: '平衡吸附量', qualitative_ordering: '吸附量排序',
  selectivity: '选择性', isosteric_heat: '等量吸附热', breakthrough_working_capacity: '穿透工作容量',
  dynamic_selectivity: '动态选择性', qualitative_breakthrough_behavior: '穿透行为', binding_energy: '结合能',
  C_percent: '表面碳含量', O_percent: '表面氧含量', N_percent: '表面氮含量',
  O_over_C_percent: '氧碳比', N_over_C_percent: '氮碳比',
  isotherm_shape_class_reported: '等温线类型', hysteresis_class_reported: '滞后环类型',
  hysteresis_size_qualifier: '滞后环大小', pore_structure_interpretation: '孔结构解释',
  dissolution_and_mixing: '溶解与混合', stirring: '搅拌', rotary_evaporation: '旋转蒸发',
  'rotary evaporation': '旋转蒸发', thermal_treatment: '炭化与活化',
  acid_washing: '酸洗与水洗', washing: '洗涤', vacuum_drying: '真空干燥',
  adsorption_desorption_isotherm: '吸附／脱附等温线', pore_size_distribution: '孔径分布',
  thermogravimetric_curve: '热重曲线', pure_component_equilibrium_isotherm: '纯组分吸附等温线',
  surface_area_normalized_isotherm: '按表面积归一的等温线',
  selectivity_vs_pressure: '选择性与压力', isosteric_heat_vs_loading: '吸附热与吸附量',
  breakthrough_curve: '穿透曲线', temperature: '温度', pressure: '压力',
  duration: '时长', hold_duration: '保温时长', heating_rate: '升温速率',
  atmosphere: '气氛', atmosphere_streams: '气体与流量', endpoint: '终点',
  repeat_qualifier: '重复次数', temperature_qualifier: '温度描述', pressure_qualifier: '压力描述',
  invalid_record_shape: '记录格式问题', invalid_record_reference: '编号引用问题',
  printed_page_conflict: '印刷页码有冲突', unverified_printed_pages: '印刷页码未核对',
  unverified_observation: '观测需要核对', ambiguous_peak_assignment: '峰归属有疑点',
  comparison_basis_mismatch: '比较基准不同', interpretation_scope_tension: '解释范围有差异',
  sign_convention_ambiguity: '符号定义有疑点', suspected_unit_error: '单位疑点'
}

/** 将规范字段翻译为阅读标签，原始键仍由字段树保留。 */
export function experimentLabel(key) {
  return experimentFields[key] || labels[key] || String(key ?? '').replaceAll('_', ' ')
}

/** 统一数量概览中的单位写法。 */
export function experimentUnit(unit) {
  return String(unit ?? '').replaceAll('cm^3', 'cm³').replaceAll('mm^3', 'mm³')
    .replaceAll('m^2', 'm²').replaceAll('degC', '°C').replace(/^dimensionless$/i, '无量纲')
}

/** 概览保留数量限定词、单位和复合取值。 */
export function experimentValue(value) {
  if (value == null) return '未记录'
  if (Array.isArray(value)) return value.map(experimentValue).join(' · ')
  if (typeof value !== 'object') return experimentUnit(value)
  if (value.reported_value != null) return [value.reported_value, experimentUnit(value.reported_unit)].join(' ').trim()
  if (value.raw_text) return value.raw_text
  const unit = experimentUnit(value.unit_canonical || value.unit_raw || value.unit)
  if (value.minimum != null && value.maximum != null) return (value.minimum + '–' + value.maximum + ' ' + unit).trim()
  if (value.value != null) return ((value.operator && value.operator !== '=' ? value.operator : '') + String(value.value) + ' ' + unit).trim()
  return Object.entries(value).map(([key, item]) => experimentLabel(key) + ' ' + experimentValue(item)).join(' · ')
}

/** HTML 转义仅用于受控的阅读片段。 */
function escapeText(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char])
}

/** 复用数学分段和 KaTeX，普通文本始终转义。 */
export function experimentTextHtml(value) {
  return splitMathSegments(String(value ?? '')).map(part =>
    part.type === 'math' ? renderMathHtml(part) : escapeText(part.text)
  ).join('')
}

/** 将公式定界统一交给既有 Markdown 渲染器。 */
function mathMarkdown(value) {
  return splitMathSegments(value).map(part => {
    if (part.type !== 'math') return part.text
    return part.displayMode ? '\n$$\n' + part.latex + '\n$$\n' : '$' + part.latex + '$'
  }).join('')
}

/** 单元格的 HTML 上下标保留为公式，不保留其他 HTML。 */
function cellMathText(node) {
  if (node.nodeType === 3) return node.textContent
  if (node.nodeName === 'SUB') return '$_{' + node.textContent + '}$'
  if (node.nodeName === 'SUP') return '$^{' + node.textContent + '}$'
  return Array.from(node.childNodes).map(cellMathText).join('')
}

/** 原表中的未定界上下标也使用数学排版。 */
function tableCellHtml(cell) {
  const text = splitMathSegments(cellMathText(cell)).map(part => {
    if (part.type === 'math') return part.text
    return part.text.replace(/[A-Za-z][A-Za-z0-9()-]*(?:[_^](?:\{[^{}]+\}|[+-]?\d+|[A-Za-z]+)[A-Za-z0-9()-]*)+/g,
      match => '$' + match.replace(/([_^])([A-Za-z]+|[+-]?\d+)/g, '$1{$2}') + '$')
  }).join('')
  return experimentTextHtml(text)
}

/** 原表只保留单元格文字，按样品和结果标记交点。 */
export function experimentEvidenceMarkdown(quote, record) {
  const text = String(quote ?? '')
  if (!/^\s*<table\b/i.test(text)) {
    if (/^(?:\\(?:frac|ln)\b|[A-Za-z]\s*=.*\\frac\b)/.test(text.trim())) return '$$\n' + text + '\n$$'
    return mathMarkdown(text)
  }
  const table = new DOMParser().parseFromString(text, 'text/html').querySelector('table')
  if (!table) return text
  const regular = Array.from(table.rows).every(row => Array.from(row.cells).every(cell => cell.colSpan === 1 && cell.rowSpan === 1))
  return '<table>' + Array.from(table.rows, (row, rowIndex) => '<tr>' +
    Array.from(row.cells, (cell, columnIndex) => {
      const tag = rowIndex === 0 || columnIndex === 0 ? 'th' : 'td'
      const raw = cell.textContent.trim()
      const sampleMatches = record?.sample && (
        table.rows[0]?.cells[columnIndex]?.textContent.trim() === record.sample ||
        row.cells[0]?.textContent.trim() === record.sample
      )
      const marked = regular && tag === 'td' && sampleMatches && raw === record.result?.trim()
      const content = tableCellHtml(cell)
      return '<' + tag + ' colspan="' + cell.colSpan + '" rowspan="' + cell.rowSpan + '">' +
        (marked ? '<mark>' + content + '</mark>' : content) + '</' + tag + '>'
    }).join('') + '</tr>'
  ).join('') + '</table>'
}

/** 按实验用途归类审核记录，保留未知方法的原始名称。 */
export function experimentCategory(method) {
  if (['equilibrium_isotherm', 'mixture_prediction', 'isosteric_heat_derivation', 'isotherm_model_fit'].includes(method)) return 'adsorption'
  if (['breakthrough', 'computational_adsorption'].includes(method)) return 'breakthrough'
  return 'character'
}

/** 提取已报告的测试条件供就地展开。 */
export function experimentMethodFacts(run) {
  const prep = run.preconditioning
  const program = run.temperature_program
  return [
    ['仪器', run.instrument?.model],
    ['方法', run.measurement_method || run.method],
    ['测试温度', (run.temperatures || [run.probe?.temperature || run.temperature]).filter(value => value != null).map(experimentValue).join('、')],
    ['测试压力', run.pressure == null ? '' : experimentValue(run.pressure)],
    ['测试前处理', prep && Object.entries(prep).filter(([, value]) => value != null).map(([key, value]) => experimentLabel(key) + ' ' + experimentValue(value)).join(' · ')],
    ['升温范围', program && [program.start, program.end].filter(value => value != null).map(experimentValue).join(' 至 ')],
    ['升温速率', program?.heating_rate && experimentValue(program.heating_rate)]
  ].filter(([, value]) => value)
}

/** 使用当前批次的温度呈现制备条件。 */
export function experimentStepConditions(operation, sample) {
  const { temperature_by_sample, ...conditions } = operation.conditions || {}
  const temperature = temperature_by_sample?.[sample?.sample_batch_id]
  if (temperature != null) conditions.temperature = temperature
  return Object.entries(conditions).filter(([, value]) => value != null)
    .map(([key, value]) => experimentLabel(key) + ' ' + experimentValue(value)).join(' · ')
}

/** 读取规范观测的指标和取值，供显示单位核对。 */
export function experimentObservationValues(observation) {
  const values = []
  const add = (metric, value) => {
    if (value != null) values.push({ metric, value })
  }
  if (Array.isArray(observation.results)) {
    for (const item of observation.results) {
      if (typeof item === 'string') {
        add(observation.observation_type, item)
        continue
      }
      add(item.metric_id || item.entity_id || item.peak_name || item.assignment || observation.observation_type,
        item.value ?? item.position ?? item.binding_energy_eV)
    }
  } else if (observation.results && typeof observation.results === 'object') {
    for (const [metric, value] of Object.entries(observation.results)) add(metric, value)
  }
  for (const text of observation.qualitative_results || []) add('qualitative_results', text)
  for (const key of ['value', 'selectivity', 'result', 'reported_observation', 'reported_order', 'qualitative_result', 'signed_value_from_figure', 'magnitude_reported_in_prose', 'interaction_distance_from_figure']) {
    add(observation.metric_id || (['value', 'result'].includes(key) ? observation.observation_type : key), observation[key])
  }
  if (observation.observation_type === 'equilibrium_uptake') add(observation.observation_type, observation.loading)
  return values
}
