# 校对疑点阻止自动入库与可读结果

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/knowledge_task_service.py

## 问题

仍需人工核对的文档可以继续自动建立索引；未启用自动入库的文件也会显示跳过提示。校对记录的技术用语、红绿增删样式和诊断栏目增加用户判断负担。

部分取代[文档校对问题工作区](2026-11-03-document-review-workspace.md)的诊断展示方式，以及[PDF回执兼容与主解析结果分离](2026-10-31-pdf-review-receipts-and-warnings.md)仅按 incomplete 排除自动入库的规则。

## 决策

### 实现方案

上传处理服务在自动入库开关开启时，同时排除 needs_review 和 incomplete 文档，保留其已生成正文，独立文档继续索引。无模型但仍有疑点的 PDF 归入 needs_review，避免 not_requested 掩盖待核对事实。手动入库和解析算法、模型协议、权限不属于变更范围。

服务通过显式参数把本次自动入库开关传至解析发布边界，在同一 attempt 的 metadata 保存 auto_index_enabled。开关不进入长期 processing_params；缺少该字段的旧产物不显示自动入库操作提示。页面只在开关为真且有待核对或未完成内容时显示“暂不进行自动入库操作”。

校对规则与回执处理模块通过 pdf_review_messages 的文案映射输出通俗中文原因，读取历史 sidecar 时使用同一映射，不改写原对象。validation 诊断代码和原判断条件保持不变；模型自己的其他解释保留。页面文字对照使用“OCR识别文字”“PDF提取文字”，差异统一黄色高亮，无红绿增删线；整块修改快照继续单独标注，不能冒充 PDF 提取文字。候选依据与解析诊断从该面板移除，后端审计数据继续保存。

## 替代方案

只按 incomplete 阻止自动入库会遗漏仍需人工核对的结果。由前端猜测开关或仅隐藏入库按钮无法阻止后台索引。把全部处理参数写入产物扩大持久化表面，因此只保存本次开关事实。

## 后果

有未决疑点或未完成复核的结果保留正文并暂停该次自动索引，同批次独立完成的文档继续入库。开关未开启或历史产物缺少开关事实时不显示自动入库操作提示。文案转换立即作用于历史结果的读取，原始审计字节保持不变；只有重新解析才生成带开关事实的新产物。人工确认与修订保存仍属于独立功能。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 待核对与未完成均阻止自动入库 | 用例副作用 | knowledge_task_service.py | test_pdf_review_task_status.py，读取模拟索引状态与任务摘要 | 两种阻止状态与独立成功文档、开关关闭，恢复旧服务时在错误索引集合断言失败 | Passed |
| 无模型的疑点不伪装为可自动入库 | 状态投影 | pdf_review_service.py | test_pdf_review_fidelity.py | 恢复旧服务时 not_requested 掩盖 unresolved，预期状态断言失败 | Passed |
| 开关与解析版本一致且提示按条件显示 | 发布字节及 UI | knowledge/base.py、DocumentReviewPanel.vue | test_milvus_kb.py 的发布测试与 document_review_runtime.test.js | 开关关闭、字段缺失、状态完成、切换版本；回读 JSON 并确认历史字节不变 | Passed |
| 原因易读且诊断不冒充正文 | 规则及 UI | pdf_repair.py、pdf_review_contract.py、DocumentReviewPanel.vue | 文案/规则测试、DOM 测试、浏览器截图 | 模型其他解释不替换、仅黄色差异且无划线、隐藏栏目不渲染 | Passed |
| 真实 HTTP 与后台索引状态一致 | PostgreSQL、MinIO、worker | 任务执行与 parse-assets 入口 | test_knowledge_parse_assets.py 的真实 HTTP/对象回读及上传工作流 | 返回可读原因但原始对象不变，未决文件无新增索引 | Not run |

后端最小相关集合 87 项通过，包含 9 项任务状态测试、3 项复核语义测试、5 项 bundle 测试、4 项文件发布与读取测试、66 项文案及规则测试。另一个扩展集合 43 项通过，其中 21 项与最小集合重叠。真实存储和 SDK 边界由这些 unit 显式替换，不能据此宣称真实后台入库链路通过。

本地测试使用仓库根 .venv 的 Python，并将 uv.lock 指定的缺失 SDK 安装到被忽略的临时测试目录；原项目依赖未修改。Windows 标准库 test 包与仓库测试目录重名，通过仅限测试进程的 test.__path__ 指向仓库目录运行 pytest。最小集合实际选择为 `test/unit/services/test_pdf_review_task_status.py`、`test/unit/services/test_pdf_review_fidelity.py`、`test/unit/services/test_knowledge_parse_bundle.py`、`test/unit/plugins/test_milvus_kb.py::test_parse_publishes_one_bundle_and_retry_clears_previous_indexes`、`test/unit/knowledge/test_pdf_review_messages.py`、`test/unit/knowledge/test_pdf_repair.py`。

前端 `node --test test/unit/document_review.test.js test/unit/document_review_runtime.test.js` 7 项通过，`pnpm run lint:check` 与 `pnpm run build` 通过。浏览器代表数据验证开启/关闭开关、needs_review/incomplete/completed 三类状态、浅深色、375px 无横向溢出；实际 CSS 高亮为黄色背景且 text-decoration 为 none，诊断栏目 DOM 数量为零。

扩展的 PDF contract/execution 集合有 6 项既有失败；在独立进程加载 HEAD 原版三个 PDF 模块后复现同一 6 项失败，未扩大本次功能范围修复。Ruff 新增文件及改动行无新增问题，其余旧行存在 E501。工程契约仍有 13 项既有错误，涉及旧 PDF 决策格式和 document_parse_state.js 路径边界。

## 未验证范围

环境未提供 Docker 命令及真实 PostgreSQL 配置，未运行真实 HTTP、MinIO、worker 和自动索引 E2E。集成用例已补充开关返回、可读提示和 MinIO 原始字节不变的断言，须在 Compose 环境执行。页面移除诊断展示不删除后端问题、证据与错误记录。
