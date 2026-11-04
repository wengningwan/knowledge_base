# 论文正文来源与检索关联证据

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/chunking/paper.py

## 问题

论文分块的来源把论文标题、章节标题和正文混为一体。正文引用的图片及同小节的公式分散在不同块，单次向量命中不能证明回答上下文完整。

## 决策

### 实现方案

Paper v3 在现有 JSON 元数据中分别保存正文来源 `source_spans` 和标题上下文来源 `context_source_spans`；最细小节身份绑定文件、原文指纹及标题位置，图号仅在同篇同版本的唯一图注与图片间建立明确链接。现有按长度组合完整段落的规则继续生效，同小节补取提供方法上下文。

Milvus 完成主命中排序与 Top K 后，在 repository 的知识库、文件及原文版本边界内补取明确关联图与最细小节上下文，独立标注原因、缺失和预算限制，主命中的分数与顺序保持独立。索引元数据绑定当前 Markdown 发布指针的哈希；repository 复用发布、索引、删除的文件操作锁，在同一短事务中核对当前状态、发布身份及主块，再读取同版关联块。查询只尝试获取锁，文件忙时返回无法补齐状态，释放连接；内部发布校验字段从模型可见来源中剥离。

`related_evidence` 模块为 Search 输出和评估回答装配去重后的补充块，原子保留正文并按原文块序排列。评估选取前五个主命中时，仍能从其关联关系取得排序靠后的相关主块。补充正文总量受 24 块、24,000 字符的上限约束，超额保留未补齐状态。Agent 工具压缩保留关联身份、状态与可容纳的正文，显式报告省略块及正文缩短；切分对照关闭补取以保持主证据指标口径。

分块预览保留原文件预览和 Markdown 来源弹窗，正文行号仅列正文来源。旧块仅在可确认标题身份时过滤上下文，无法确认时显示需重新入库。分块专用 PDF 入口及其页跳转事件链移除；普通原文件预览继续渲染完整 PDF。该决定部分取代[小节与来源页决定](2026-11-02-paper-subsections-and-source-pages.md)的页跳转行为，并补充[论文证据分块决定](2026-11-02-paper-evidence-chunking.md)的来源角色及检索装配。PDF 页码映射、解析阅读顺序与页脚处理保持现有行为。

## 替代方案

按章节名称直接匹配会串联不同论文和同名小节。把整篇论文交给回答会增加无关内容和输入成本。为 D–A 方法建立专用推导图会引入当前需求不需要的维护表面。采用同文档最细小节的有界补取，并对缺失明确说明。

## 后果

现有 Paper 块需重新入库才拥有稳定小节身份和图号关系。该变化复用已有 JSON 字段，不增加数据库 Schema 版本。无需为本功能重新解析正文；原文中的阅读顺序问题仍由解析链路负责。图号关系与同节上下文提供原图入口、图注和原文证据，不证明科学结论或公式之间的数学依赖。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 来源行号不混入标题 | 分块和预览 | paper.py、chunkPreview.js | 来源切片 unit、Vue unit 与模拟 API 浏览器 DOM | 标题文本在正文中真实重复时仍保留 | Passed |
| 预览没有 PDF 跳转入口 | 组件事件链 | ChunkPreviewPanel.vue | 真实 Vue unit、FileDetailModal 浏览器 DOM | 存在可靠页码仍无按钮，普通 PDF 仍渲染 | Passed |
| 图号识别与补取逻辑边界 | 链接、查询及装配 | paper.py、repository、related_evidence.py | 相关 Python unit | 同名图、S4/4、陈旧来源、跨文件、预算不足 | Passed |
| 补取进入 Search 输出和评估 prompt | 主检索与模型输入装配 | Milvus、base、evaluator | 实际 aquery 出口及回答 prompt 的 unit | 重排/不重排、第六块相关主命中、去重 | Passed |
| 压缩保留补充证据及不完整状态 | 工具结果压缩 | summary.py | 直接执行生产纯函数的 unit | 长状态中间缺图、极小预算、正文缩短 | Passed |
| 主命中排序和切分指标保持原口径 | 主检索、对照 | Milvus、chunking_comparison | 相关 unit | 补取内容不得计作主命中 | Passed |
| 真实持久化、共享锁与 HTTP/worker/Milvus 链路 | PostgreSQL 和部署集成 | repository、Milvus、worker | 新增 PostgreSQL integration 和论文入库 E2E | 发布竞争、旧发布残留、同名小节隔离 | Not run |

后端相关 101 项 unit 通过，覆盖来源、图号、repository 边界、预算、实际查询出口与回答 prompt；压缩单测直接执行生产纯函数，实际 ToolMessage 测试的环境限制见下文。前端 12 项相关测试通过；完整前端测试 429 通过、5 项既有失败，失败名称与基线一致。lint、Web build、文档 build 通过。浏览器使用真实 FileDetailModal 和 PDF.js、模拟 API 及合成四页 PDF，核对正文高亮、无 PDF 按钮、普通 PDF 渲染，覆盖 1280、1024、768、375 像素及明暗主题。

Not run：开发机没有 Docker，真实 PostgreSQL、worker、Milvus、Embedding 与回答质量由部署环境验证。Windows 缺少 POSIX 文件原语，正常 Agent 模块导入和实际 ToolMessage 中间件测试未执行；纯函数单测只证明压缩装配逻辑。相关 Milvus 既有单测存在 6 项缺少本地 langchain_openai 依赖的失败；整体 Python Ruff 检查有 2 处既有长行，格式检查有 4 个文件的旧区域未通过。工程契约检查存在 13 项基线错误，其 63 项单测中的 1 项受 Windows 创建符号链接权限限制。未以这些局部结果宣称完整部署验证通过。
