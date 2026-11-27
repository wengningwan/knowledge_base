# 论文结构化导出字段梳理记录

实现已写入，待部署验收。这里记录已确认的论文结构化 JSON 导出字段规则。

## 格式与整理

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `schema_id` | 从新导出的 JSON 根对象中删除。 |
| `schema_version` | 从新导出的 JSON 根对象中删除。 |
| `record_id` | 保留，直接使用系统分配的 `file_id`。凡指向整份结构化记录的引用，使用相同的值。 |
| `extraction_metadata.created_on` | 保留。由系统填写本次结构化结果生成时的 UTC 日期，格式为 `YYYY-MM-DD`。重复下载同一份结果时使用其已保存的日期。 |
| `extraction_metadata.extraction_scope` | 从新导出的 JSON 中删除。当前抽取范围为 `record_id` 对应的上传文件。论文引用的外部资料缺失并影响某项结果时，在 `missing_information` 写明缺失项和原因。 |
| `extraction_metadata.supplementary_information_available` | 从新导出的 JSON 中删除。影响具体结果的补充材料缺失由 `missing_information` 说明。 |
| `extraction_metadata.figure_curves_digitized` | 从新导出的 JSON 中删除。逐点数据的可用情况由具体曲线记录表达。 |
| `extraction_metadata.normalization_policy` | 从新导出的 JSON 中删除。抽取规范统一定义数值与单位的处理规则，数值记录保留原文及可用的统一值。 |
| `extraction_metadata.source_priority` | 从新导出的 JSON 中删除。抽取规范统一定义来源选择规则，每项结果的实际出处由证据引用记录。 |
| `extraction_metadata.controlled_vocabulary` 及其子字段 | 从新导出的 JSON 中删除。数值形态、单位等由相应结果字段表达。 |

## 论文题录

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `document.title`、`authors`、`journal`、`year`、`volume`、`pages`、`doi` | 保留。从论文题录提取，作者按原文顺序。论文采用文章编号代替期刊页码时，`pages` 使用原文的文章编号。 |
| `document.received_date`、`revised_date`、`accepted_date`、`online_date` | 保留。论文报告相应日期时按原文精度记录，缺失时省略。 |
| `document.keywords`、`article_type` | 保留。仅记录论文明确提供的关键词和文章类型，缺失时省略。 |
| `document.document_id` | 保留。由系统生成，作为 `evidence[].document_id` 的引用目标。 |
| `document.pdf_page_to_printed_page` | 保留可核对的 PDF 页码与印刷页码映射。印刷页码无法确认时使用 PDF 页码核对。 |
| `document.provenance_scope` | 从新导出的 JSON 中删除。 |
| `document.issue` | 不新增。 |

## 原文证据

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `evidence` | 保留原文证据列表及下列全部字段。 |
| `evidence[].evidence_id`、`document_id` | 保留。系统生成证据编号，论文编号指向 `document.document_id`。 |
| `evidence[].pdf_page`、`printed_page`、`section`、`location` | 保留。按 PDF 阅读器页码、可核对的印刷页码和原文页内位置定位。印刷页码未知时填 `null`。 |
| `evidence[].quote_raw`、`extraction_origin` | 保留可核对结果的原文或表格片段及实际载体。 |
| `evidence[].provenance_scope` | 保留。按证据和上下文判断是本文研究还是引用研究。混合证据能拆分时分别标记，无法拆分时标明混合。原文无法支持归属判断时标为未知。 |

## 物质名录

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `chemical_entities` | 保留物质名录及下列全部字段。 |
| `chemical_entities[].entity_id`、`reported_name`、`evidence_ids` | 保留。系统分配物质编号，按原文记录物质名称并关联证据。 |
| `chemical_entities[].canonical_name`、`entity_class`、`formula_ascii` | 保留。物质身份明确时填写统一名称、类别和化学式。无法从原文确认化学式时省略。 |
| `chemical_entities[].display_formula` | 保留，按需填写。用作可读的化学式，保留原文可确认的上下标、电荷等写法。与 `formula_ascii` 写法相同时可省略。 |
| `chemical_entities[].standard_or_grade`、`supplier`、`purity` | 保留。按原文报告的规格、供应商和纯度填写。 |
| `chemical_entities[].purity.kind`、`raw_text`、`operator`、`value`、`unit_raw`、`unit_canonical`、`basis` | 保留。记录数值形态、原文、限定符、数值、原单位和可用的统一单位。计量口径按原文填写。 |

## 材料类别、样品配方与样品状态

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `material_identities` | 保留材料类别清单及 `material_id`、`reported_family_name`、`abbreviation`、`material_class`、`evidence_ids`。 |
| `material_identities[].carbon_source_entity_id`、`activation_type` | 材料类别中移除这两个字段。碳源在制备路线中记录为投料及其角色，活化方式记录在实际制备步骤。 |
| `sample_batches` | 保留样品配方清单及 `sample_batch_id`、`material_id`、`reported_name`、`series_id`、`factor_assignments`、`batch_number`、`evidence_ids`。保留原样品标签，有依据时填写样品系列和实际批号。 |
| `sample_batches[].factor_assignments` | 按论文实际用于区分样品的变量记录名称、取值和单位。样例中的活化温度和 KOH 与沥青质量比适用于相应论文。 |
| `sample_states` | 保留样品状态清单及 `sample_state_id`、`sample_batch_id`、`reported_name`、`state`、`physical_form`、`parent_state_ids`、`preparation_route_id`、`evidence_ids`。前序状态和路线按原文有据的关系关联。 |

## 制备步骤

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `synthesis_routes` 及客户样例中的制备步骤字段 | 全部保留，按实际路线和步骤填写。路线、步骤、投料和条件关联原文证据。 |
| `synthesis_routes[].route_id`、`route_name`、`target_sample_state_ids`、`operations`、`evidence_ids` | 保留路线编号、名称、最终样品关系、步骤和证据。 |
| `synthesis_routes[].operations[]` | 保留步骤编号、顺序、类型、标签、设备、输入输出样品和中间物、`simultaneous_processes` 及证据。按原文记录顺序和同时发生的操作。 |
| `synthesis_routes[].operations[].reagent_uses[]` | 保留投料和物质编号、角色、用量、浓度及配比与子字段。按原文区分各项的数值和单位，明确配比双方与计量口径。 |
| `synthesis_routes[].operations[].conditions` | 保留原文实际报告的操作条件及子字段，包括按样品记录的条件、工艺气体、定性条件和操作终点。同一条件在步骤与配方中出现时，步骤中的按样品值取自 `sample_batches[].factor_assignments`。 |
| `synthesis_routes[].yield`、`replicate_count` | 保留，原文报告且适用范围明确时填写。单个步骤的重复次数记在该步骤的条件里。 |

## 实验与计算

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `experiment_runs` | 保留实验与计算登记。不同主题的论文共用实验编号、类型、方法、技术、数据产生方式、对象、曲线和原文证据等基本字段。 |
| `experiment_runs` 中的仪器、测前处理、分析方法、条件与计算设置 | 按原文实际采用的实验方法记录相应字段和参数。条件保留原文值与单位，有可靠换算时另存统一值。 |
| `experiment_runs[].parameter_definitions`、`model_parameters` 等模型字段 | 保留模型名称、简称、原文公式、参数定义、数值、拟合质量及适用的缺失原因。参数名称按该论文实际使用的模型变化。原文仅提供参数定义时保留定义，数值留空并记录可确认的缺失原因。 |
| 实验条件与结果 | 仪器、方法和测试条件记在实验登记里。测量或计算结果放在观测结果里，通过实验编号和证据关联。 |
| `experiment_runs[].method`、`measurement_method` | 同义的测量方法统一记录在 `method`。 |
| `experiment_runs[].input_run_ids`、`source_run_ids`、`source_model_run_ids` | 直接使用的前序实验或拟合统一由 `input_run_ids` 引用，输入类型可从被引用的实验记录回查。 |

## 图与曲线

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `series_sets` | 保留曲线组及客户样例中的全部字段，按原图和正文提供的信息填写。 |
| `series_sets[].series_set_id`、`run_id`、`series_kind`、`source`、`evidence_ids` | 保留曲线组编号、对应实验、曲线类别、图号、数据可用性和证据。 |
| `series_sets[].x`、`y`、`analysis_method`、`temperature` | 保留横纵轴的量、符号、单位和适用的计算表达式、取值定义、分析方法及共同条件。图内文字可用时按同次解析图注关联，无法确认的轴留空。 |
| `series_sets[].series_members[]` | 保留每条曲线的样品、物质、分支、明确的分组条件和 `data_points`。原文提供逐点数据时填写。只有图片时继续登记曲线，逐点数据留空并注明数据可用性。 |
| `series_sets[].x_axis_reported_in_figure`、`x_axis_described_in_text_and_model`、`axis_consistency_status` | 保留。图轴与正文有冲突时分别记录原文定义，标为待核对。 |

## 观测与结果

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `observations` | 保留观测记录、类别、实验和曲线关联、样品、物质、指标、数据产生方式及原文证据。具体指标按论文实际报告的内容填写。 |
| `observations` 中的数值、范围和文字结果 | 保留原文、数字、单位、符号、约数、不确定度、计量口径和范围随什么变化。可确认的统一值和单位另存。文字报告的现象、比较顺序、谱峰位置及作者归属按原文记录。 |
| `observations` 中的样品、条件与结果清单 | 保留结果专属的条件及其上下文。共同条件由该实验关联。样品、条件和实验相同的多项指标可以同组，每项保留自己的值。归属或条件不同的结果分开记录。 |
| `observations` 中的组成、比例、附加值和解释 | 保留论文实际报告的组成成分、比例双方、取值或单位约定及相关原文。涉及原因的进一步解释可关联 `mechanism_claims`。 |

## 机理解释

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `mechanism_claims` 及其字段 | 全部保留。登记作者的解释、类别、明确的比较顺序、因果确定程度及相关观察和证据。按原文保留「可能」「相关」等限定说明。 |
| `mechanism_claims[].provenance_scope`、`reported_background_values`、`normalization_note` | 保留。判定解释和数据的归属，按论文实际引用的项目保存原值及适用的单位核对说明。引用的背景数值作为解释依据登记。 |

## 缺失说明和校验疑点

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `missing_information` | 保留缺失问题编号、受影响记录、缺失字段和原因。记录影响理解或使用的缺失，并区分论文未报告、资料不可用及待核对的情况。 |
| `validation_issues` | 保留问题编号、严重级别、类别和具体说明。说明原文冲突的两处写法及位置。 |
| `validation_issues[].record_id`、`record_ids` | 受影响记录统一使用 `record_ids`，单条记录也写作列表。 |

## 整理情况

| JSON 字段 | 已确认的规则 |
| --- | --- |
| `quality_summary.document_identity`、`sample_registry`、`synthesis_route`、`overall_status` | 保留题录、样品、制备路线和整体整理状态，按实际结果反映覆盖范围和待复核情况。 |
| `quality_summary.experiment_count`、`quantitative_observation_count`、`experiments` | 按实际实验汇总各自的结果数量和整理状态。样例中写入固定实验类型的汇总项并入这份通用汇总，例如 IAST 预测、等量吸附热以及穿透容量和选择性。 |
| `quality_summary.raw_curve_points`、`supplementary_information` | 从整理情况中移除。逐点坐标的取得状态在具体曲线记录里查看，影响结果的补充资料缺失在缺失说明里查看。 |

## 验收

部署验证状态为 `Not run`。本机只修改代码与文档，真实入库、下载和浏览器检查在部署电脑完成。

分别下载一篇吸附论文和一篇其他主题论文的 JSON，核对标为删除的字段已从对应位置移除，根记录编号与系统文件编号一致，指向整份记录的引用与根编号一致。重复下载同一份结构化结果时，核对生成日期一致。打开新旧两种记录的「原始 JSON」，核对记录编号、保存的日期和已有的旧格式字段与各自的下载内容一致。复查旧草稿的样品卡和人工校对状态。

选取同一篇论文中同时出现的本文结果和文献对比结果，回查原文并核对两者的证据归属。归属不明的证据应标为未知。

在吸附样例中核对适用模型的参数定义和数值缺失原因，在晶体管论文中核对该文原有的测量方法、电学条件和结果。在「实验与曲线」核对仪器摘要和同组的数值及文字结果。回查曲线点所在表格的横纵列、数据行及曲线归属。只有图像的曲线应保留图号、已识别的坐标轴和图例，逐点坐标留空。图轴与正文或同一比较的原文基准有冲突时，回查两处说明和出处是否齐全。整理情况应与实际结果数量及待复核状态一致。

用同一样品在两个条件下得到不同值的表格核对负向情况。把第二行的值关联到第一行的条件，或把表头开尔文写成摄氏度，相关观测应进入待核对。给仅含一个样品成员的曲线填另一样品的数据行时，错误点应被剔除，曲线及正确点仍保留。
