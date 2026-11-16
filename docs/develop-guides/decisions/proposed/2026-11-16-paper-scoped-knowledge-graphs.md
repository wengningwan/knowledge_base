# 每篇论文独立知识图谱

状态：proposed
类型：architecture
Owner：backend/package/yuxi/knowledge/graphs/milvus_graph_service.py

本提案面向知识库维护者，限定于把已有知识图谱改为按论文归属。论文使用现有 file_id 标识，沿用图谱页面、构建任务和抽取流程。代码与文档已落地，提案待部署电脑完成人工验收后收敛。

## 问题

当前图谱以整个知识库为范围。同库不同论文的同名同类型实体会共用节点，实体属性写入、关系遍历和维护无法形成明确的论文边界。用户需要每篇论文各自保存实体和关系，并能在已有图谱页面选择论文查看、构建和重置。

### 变更前确认的源码事实

| 事实 | 源码依据 |
| --- | --- |
| 实体 ID 和唯一身份只含 kb_id、normalized_name、label；三元组同样没有论文身份 | [graph_utils.py](../../../../backend/package/yuxi/knowledge/graphs/graph_utils.py)、[models_knowledge.py](../../../../backend/package/yuxi/storage/postgres/models_knowledge.py) |
| 来源 mention、Neo4j Chunk 和关系已有 file_id；Entity 本身没有 file_id | [graph_utils.py](../../../../backend/package/yuxi/knowledge/graphs/graph_utils.py)、[图谱 repository](../../../../backend/package/yuxi/repositories/knowledge_graph_repository.py) |
| 构建、重置和向量修复使用知识库 maintenance 锁，与文件解析/索引/删除互斥 | [图谱 service](../../../../backend/package/yuxi/knowledge/graphs/milvus_graph_service.py)、[文件 repository](../../../../backend/package/yuxi/repositories/knowledge_file_repository.py) |
| 删除文件图时先清理外部存储，失败回滚 PG 来源删除；已有 complete_storage_mutation 等待已发 I/O 完成后再传播取消 | [图谱 service](../../../../backend/package/yuxi/knowledge/graphs/milvus_graph_service.py)、[解析 service](../../../../backend/package/yuxi/services/knowledge_parse_service.py) |
| 现有图谱入口是知识库详情中的 KnowledgeGraphSection，没有论文专用提问入口 | [知识库详情](../../../../web/src/views/DataBaseInfoView.vue)、[图谱组件](../../../../web/src/components/KnowledgeGraphSection.vue)、[文件详情](../../../../web/src/components/FileDetailModal.vue) |
| knowledge /query、query-test 与 query_kb 返回检索片段，模型自然语言回答属于其他链路 | [知识库路由](../../../../backend/server/routers/knowledge_router.py)、[知识库 manager](../../../../backend/package/yuxi/knowledge/manager.py)、[Agent 工具](../../../../backend/package/yuxi/agents/toolkits/kbs/tools.py) |

### 目标和非目标

目标是论文 A/B 的同名实体分别保存；查看 A 不混入 B；构建、统计、失败样本、向量修复和重置按选定范围执行；整库查看继续聚合各论文的独立图。现有来源引用、权限和失败语义保留。

用户明确当前没有旧图数据。本方案以图谱首次构建为前提，不设计历史图迁移、旧实体拆分、旧 ID 清理、缓存重投影或兼容旧图格式的分支。现有知识库、论文、文本 chunk/向量及其他业务数据不属于空图前提，不执行整体数据库初始化或清空。

本次不增加提问框、独立回答流程、论文聊天、AgentRun 范围协议、领域实验语义图、图谱人工编辑或第二套图谱页面。不新增逐事实引文验证契约、Paper/Evidence 节点、多版本发布、冻结全文结果表、独立 Cleanup 表和恢复 API。

第一版保留现有知识库级串行维护，不要求不同论文并发构建；允许重建期间该范围图谱暂不可用。现有多存储崩溃恢复与未知在途副作用仍按当前失败处理和人工核对，不宣称由本次文件隔离全部解决。

## 提案

### 实现方案

使用 (kb_id, file_id) 划分图谱归属，保持现有 PostgreSQL、Neo4j、Milvus、Redis/ARQ 拓扑和抽取流程。每个 chunk 的所属文件由持久记录确定，实体和三元组的身份增加 file_id。HTTP 只解析范围、权限和响应，新增提交/维护用例进入 yuxi.services；存储查询和范围约束由对应 repository/executor 执行。

现有图谱页面增加论文选择。“全部论文”提供整库查看和构建；选择一篇后查看、构建、失败样本和维护作用于该文件。已入库文件的行菜单提供「知识图谱」，用文件 ID 打开同一图谱页并预选该文件。文件管理负责发起导航，图谱页负责选择范围、读取状态与执行操作。入口复用现有 GraphCanvas、GraphDetailPanel、图配置、Task 状态与权限边界。

「全部论文」默认开启「合看同名项」。总览在当前查询返回的画布数据中，把来自多篇论文、具有相同规范化名称和类型的实体归到一个圆点，保留每条原始关系及其文件 ID。合看圆点显示来源篇数，关系详情通过现有文档元数据接口读取文件名。总览的构建状态继续反映保存的记录，单篇视图直接绘制所选论文的图。图谱展示设置提供开关，可以逐篇看同名项。

### 数据身份与查询

| 对象 | 最小调整 |
| --- | --- |
| PG Entity / Triple | 增加非空 file_id；实体唯一身份改为 (kb_id, file_id, normalized_name, label)，三元组按同文件实体和关系类型计算 ID |
| PG 来源 mention | 保留现有 file_id/chunk_id，核对引用实体、关系和 chunk 属于同一知识库、同一文件 |
| Neo4j Entity / RELATION / Chunk | 实体 MERGE、关系匹配及全部路径节点/边包含同一文件范围；不新增论文根或证据节点 |
| Milvus 图集合 | 实体/三元组主键使用新文件级 ID，记录 file_id 供统计、索引任务和定向维护；保留每知识库 entity/triple 集合 |
| Chunk 状态与抽取缓存 | 继续使用当前 graph 状态、extraction_result 和 ent_ids；不建立第二套构建状态表 |

身份建议为 entity_id = hash(kb_id, file_id, normalized_name, label)，triple_id = hash(kb_id, file_id, source_entity_id, relation_type, target_entity_id)。实体、关系端点、文件与来源的归属由 repository 查询及必要的复合唯一/外键约束共同保证。file_id 不从模型输出或前端节点属性推断。

单篇子图、标签、统计与失败样本都接收可选 file_id。传入时先查文件确属当前知识库且不是目录，再限定种子、路径中每个节点和每条边；未传时使用整库范围。继续使用现有画布节点/边标识，不为尚不存在的外部证据 URL 引入新的稳定 ID 契约。

现有知识库级图增强检索继续消费各论文独立且不连通的图，更新 entity_id/triple_id 与 ent_ids 的读取和生成。没有精确论文问答入口作为 consumer，本次不增加 query_kb 参数、全文三种召回模式的单篇查询协议或生成回答服务。若未来提出论文问答，再单独确定真实入口、范围执行和回答依据。

### 构建、状态与维护

沿用 knowledge_graph_index Task 和现有整库去重。payload 在单篇操作时增加 file_id；未提供时仍处理整库，不新增批量枚举 Task。任务执行继续持有 lock_database_maintenance，保留已有取消、lease、投递和失败机制。

单篇构建必须把同一 file_id 传到 pending chunk 查询、抽取/结构状态、向量记录 claim、进度计数、finalize、错误样本及返回结果。仅限制抽取循环而让向量 worker 扫整库仍不满足要求。整库构建继续按每个 chunk 的真实 file_id 生成独立实体，无需为每篇创建子任务。

状态分别表达所选范围的统计/最近任务，以及当前知识库是否被其他图维护任务占用。A 的最近任务按 (kb_id, file_id=A) 查询，“全部论文”的最近任务只匹配未指定文件的整库 Task，互不冒充自身完成与进度。其他任务占用知识库时沿用冲突拒绝，可以显示“该知识库正在处理其他图任务”；Task success 与抽取覆盖仍分开查看，保持当前错误可见性。

单篇重置清理该论文的外部图记录，并在同一 PostgreSQL 事务提交图归属删除和分块状态重置。外部清理失败时事务回滚。单篇重置要求 clear_config=False，共享配置由整库操作维护。重建清理原有图后从当前分块继续构建，期间允许暂时无图。

单篇向量修复只重置/重写该文件记录；即使 all_vectors=True，也不能调用 drop_graph_collections(kb_id) 删除整个图集合。需要清理时使用该文件的 entity_id/triple_id，整库维护继续保留原有操作。

原有文件重新入库/删除流程继续先清理对应图和外部记录，再删除 chunk 或文件事实；失败保留已有可重试归属。新增文件级操作也不得先删除唯一 PG 归属再尝试外部清理。未知副作用、失联旧 worker 和服务故障仍需在部署环境回读后决定重试，不能用本次改造声称更强恢复保证。

### 接口与前端边界

复用既有 /api/graph/subgraph、labels、stats 与知识库 graph-build/status、index、failed-chunks、reset、reconcile，按 HTTP 形态增加可选 file_id；不改变现有无 file_id 调用的整库含义。config 仍是知识库级配置，不增加每篇论文配置。

所有传 file_id 的入口，在后端 READ/MANAGE 权限与文件归属查询处执行最终检查。伪造其他知识库文件 ID、目录 ID 或不合法的维护范围必须拒绝，不能因文件查不到回退整库。单篇 reset 的全库清配置选项返回明确错误，页面同时隐藏该不适用操作。

KnowledgeGraphSection 显式保存论文选择，API 封装统一带上该值。切换论文时清理旧节点选中状态，请求序号/取消和范围核对阻止旧响应覆盖新论文。现有画布关键词只用于图节点搜索，不新增自然语言提问或回答区；不改文件详情和普通聊天交互。

### 结构初始化与实施顺序

由唯一 storage-migrator 接入 PG 字段、唯一约束、外键和索引调整；API/worker 不自行修改表结构。没有旧图数据，因此直接让首次写入遵循文件级身份，不开发历史数据回填或重投影程序。

首次构建时创建 Neo4j 文件索引，Milvus 按新增 file_id 字段创建图集合。已存在不符合新结构的空图集合时，写入明确报错。部署人员可在确认集合为空后用既有整库 all_vectors 修复模式重新创建图集合。原件、Markdown、文本 chunk/向量、实验结构化和人工审核记录保留。

结构就绪后，从现有已入库论文的 chunk 沿原抽取流程首次构建。先用 A/B 两篇样例核对 PG、Neo4j、Milvus 的实体身份和文件归属，再验证单篇构建、查看、重置与修复，以及全库模式。

| 顺序 | 修改落点 | 完成结果 |
| --- | --- | --- |
| 一：身份与结构 | models_knowledge、postgres manager、storage_migration、graph_utils、graph repository | 同库同名实体按文件独立，端点和来源归属一致 |
| 二：构建与维护 | MilvusGraphService、图向量存储、chunk/graph repository、Task Handler；新增用例按需放 services | 文件范围贯穿写入、状态和修复，保留知识库锁与已有 Task |
| 三：页面和接口 | graph/knowledge router、graph_api/knowledge_api、KnowledgeGraphSection | 在原图谱页选择论文操作，没有新问答入口或第二套图页面 |
| 四：首次构建与验收 | 图结构初始化与既有知识库机制/运维/API 文档 | 首次构建归属正确，单篇操作不影响其他论文，记录真实人工结果 |

## 替代方案

| 选择 | 判断与代价 |
| --- | --- |
| 仅页面筛选、保留共享实体 | 属性与路径已合并，无法形成论文归属，不采用 |
| file_id 身份与范围＋现有任务/锁 | 满足本次目标，新增状态最少；保留整库串行与重建暂不可用，采用 |
| 总览写入共享实体 | 同名项未经过实体消歧，跨论文写入共享身份会改变来源、检索和维护边界。总览在画布中归组即可 |
| 每篇一套数据库或向量集合 | 引入资源和运维成本，当前没有物理租户隔离要求，不采用 |
| revision 发布、冻结块结果、独立 Cleanup 与恢复 API | 主要服务无缝重建、更强并发和持久恢复；在保留现有互斥与失败语义时不作为本次前置，仅在对应独立需求与证据出现后重开 |
| 论文提问框、回答生成、AgentRun 新范围 | 当前图谱没有该入口，超出本次请求，不交付 |
| 新证据模型、Paper/Evidence 节点、实验语义图 | 改变抽取和内容模型，论文隔离可复用已有来源，不交付 |
| 单篇并发任务、批量子任务、单篇配置 | 当前构建/配置已有 Owner，先复用其语义，不建立新工作流 |

## 验收标准

下表是未来实现后的人工验收预期，当前结果均为 Not run。静态源码只证明现状和候选落点，不证明部署实例的业务行为。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| A/B 同名同类型实体独立 | 只改 UI 或 ID，实际 PG/Neo4j 仍合并 | 模型约束、ID、写入 repository/Cypher | 两篇样例含同名样品但不同属性，回读 PG/Neo4j/向量 ID 与 file_id | 向 A 关系写 B 实体，或按 A 的文件范围重写已有 B 实体 ID，检查拒绝且 B 属性保持原值 | Not run |
| A 子图、标签、统计和失败样本都限定 A | 只限制起点或漏掉统计路径 | graph service、chunk/graph repository | 传入 A 的 file_id，核对全部节点、边、计数与样本 | 传入其他知识库的文件 ID、目录 ID 或空 file_id，检查拒绝响应及数据范围 | Not run |
| A 构建和向量 worker 只处理 A | 抽取已筛选，向量 claim/finalize 仍扫整库 | 图谱构建、vector store、repository | 给 A/B 留待处理记录，只构建 A，回读 B 不变 | B 的 pending/failed 记录不能被 A 构建标成已完成 | Not run |
| 单篇构建与维护使用显式范围 | 单篇参数误入请求体、clear_config 或 all_vectors 沿用整库分支 | 图谱接口、维护用例、图服务与向量存储 | 重置/修复 A，回读 B、知识库配置及集合 | A reset + clear_config=True 或将 file_id 错放在 index/reset/reconcile 请求体，检查拒绝且数据无变化。A all_vectors 保留 B 的向量 | Not run |
| 状态和重试由当前范围拥有 | B 任务进度被当成本篇进度，整库故障无法在单篇修复 | Task 状态查询、向量状态与前端范围 | 运行 B 或整库任务，再选 A，核对忙碌提示与 A 自身进度。整库任务使 A 向量失败后，A 提供向量修复入口 | B success 不得让未构建的 A 显示已完成 | Not run |
| 重置后图记录与分块构建状态一起提交 | 图记录删除已提交，旧分块状态和实体 ID 残留 | 图服务、图谱与分块 repository | 重置 A 后回读图记录、分块标志和 ent_ids，再构建 A | 让外部清理失败，回读 PG 图归属与分块状态仍可重试 | Not run |
| 权限与维护互斥沿用真实后端边界 | 前端隐藏代替权限，改细锁引入交错 | 权限依赖、repository、现有 Task/maintenance lock | READ/MANAGE 身份操作，构建期间尝试解析/重置，回读结果 | 无管理权不能构建/维护，文件操作不能绕过互斥 | Not run |
| 无历史图前提下初始化结构并首次构建 | 把无旧图误当成空业务库，或只改模型而未更新实际 schema | storage-migrator、图索引/集合初始化与首次构建 | 回读字段、约束、索引和集合 schema，构建 A/B 后核对文件归属与既有文档数据 | 在副本保留旧身份实体行时迁移拒绝并保留原数据。缺少 file_id 的空图向量集合拒绝写入，原文本向量集合保留 | Not run |
| 现有图入口完成选择，无新增问答等功能 | 方案扩成聊天/证据/领域新产品 | KnowledgeGraphSection、API、实际 diff | 浏览器选择全部/A/B、切换和维护；审阅改动范围 | A 慢响应不覆盖 B；无新提问框、回答流程、独立恢复 API 或多版本表 | Not run |
| 已入库文件的「知识图谱」入口打开对应单篇 | 自动选中同名错文件、回退到整库，或让另一个知识库沿用旧选择 | [文件表格](../../../../web/src/components/FileTable.vue)、[知识库详情](../../../../web/src/views/DataBaseInfoView.vue)、[图谱组件](../../../../web/src/components/KnowledgeGraphSection.vue) | 分别从 A/B 的行菜单打开，核对选中文件名、独有节点和索引状态，再从页签进入检查「全部论文」 | 用目录、待入库文件验证行菜单无入口。只读账号可查看，索引与重置控件不可用。可用超出论文下拉框首批结果的文件核对初始选中名称 | Not run |
| 总览合看同名同类型实体并保留每条关系的论文来源 | 同名异类被合看、关系丢失、来源串篇 | [图谱组件](../../../../web/src/components/KnowledgeGraphSection.vue)、[关系详情](../../../../web/src/components/GraphDetailPanel.vue) | A 的星辰大学连红石塔，B 的星辰大学连蓝湾桥。总览显示一个星辰大学和两条连线，分别点线核对 A、B 文件名 | 关闭开关应显示两个星辰大学。切到 A 时只显示红石塔；同名异类保持两个圆点；两篇产生相同方向和类型的关系时仍能逐条点开来源 | Not run |

最小样例使用同知识库 A/B 两篇文档，均有 Sample-1，但属性分别为 ALPHA/值1 和 BETA/值9。先核对整库构建后实体分开，再核对单篇查询/维护不影响另一篇，以及全库模式仍可查看二者。预期结果与实际结果分别记录，不能只凭 HTTP 200、Task success 或进度 100% 判通过。

当前只进行了文件读取、rg 符号检索和独立静态方案审阅；无旧图数据由用户明确提供，未连接部署存储核查。未执行真实 HTTP、模型、PG/Neo4j/Milvus、worker、浏览器或结构升级。按本机约定，工程契约脚本、git diff --check、lint、build 也由用户在部署电脑执行。本次不恢复自动化测试。

## 风险

- 必须覆盖实体 ID、数据库约束、Cypher、向量 payload、claim、finalize、状态和维护的全部真实 consumer；只改展示或只改 ID 会造成数据断链。
- 无旧图数据不代表无业务数据；结构初始化只调整图谱相关字段、约束、索引和集合，不清空整个数据库或文本向量。
- 保留整库互斥会使不同论文维护互相等待，这是第一版接受的现有约束；按论文并发需要新的明确需求。
- 本次保留当前抽取质量、属性处理和来源表达，不把论文隔离宣称为实验事实消歧或逐事实证据校验。
- 现有多存储取消、服务失联和迟到 I/O 的恢复能力仍需真实环境人工核对；没有运行证据不得承诺完整崩溃恢复或重建无缝可用。
