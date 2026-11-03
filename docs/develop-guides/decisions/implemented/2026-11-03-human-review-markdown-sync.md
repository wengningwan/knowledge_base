# 人工校对确认与 Markdown 同步

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/knowledge_review_service.py

## 问题

校对页面只展示机器回执，用户无法将人工判断保存为正文。疑似遗漏没有可靠的替换块；已入库正文修改后还会留下过期检索数据。

## 决策

### 实现方案

校对页展示当前结构块的完整正文，允许修改或采用 PDF 提取文字，再确认整段；同段相关问题同时记录人工确认。没有结构块的问题允许保留正文确认，或由用户明确选择段落后的补入位置。候选不自动应用。

管理权限 HTTP 入口经知识库 manager 选择真实 executor，调用 review service；service 持有现有同文件操作锁，校验文件归属、状态、attempt、人工版本和正文哈希，重读最新 sidecar。以原结构渲染与当前 MD 一致为前提修改 blocks，生成不可变新 MD 和 sidecar，再由 repository 在同一事务 CAS 发布两条指针与状态。变更正文时复用 executor 清理旧分块、向量及图谱并回到 parsed；确认本身不触发入库。对象上传或 CAS 失败不发布新正文，未确认 commit 的对象先回读再决定是否清理。

人工确认回执与块正文进入同一 sidecar；原机器状态与依据保留。metadata 的人工版本在每次确认后更新，已确认项不再阻止复核完成。读取与下载继续消费数据库当前 MD 指针。父组件保存成功后重新加载正文和 sidecar，失败保留草稿。图片后台任务在同 attempt 发布时仅把图片字段合并到锁内读取的最新 sidecar，避免覆盖人工回执。

刷新使旧正文与下载立即失效，同时保留同 attempt 的组件身份。草稿按问题身份保留，确认只清除同段草稿；其他段草稿继续存在。草稿记录原段内容，重新读取后同段改变时禁止直接覆盖，用户可明确放弃草稿并载入当前段落。通用接口错误不展示运维日志命令，校对保存出错统一提示“保存失败”，输入继续保留。

本决定补充[校对工作区](2026-11-03-document-review-workspace.md)的人工保存能力，沿用[易读结果与自动入库](2026-11-03-review-auto-index-and-readable-results.md)的候选展示、黄色差异与自动入库开关语义。

## 替代方案

- 自动采用所有候选：无法证明 PDF 文字层正确，也无法定位缺失区域，拒绝。
- 仅修改前端或下载副本：刷新与后续入库仍使用旧正文，拒绝。
- 全文 Markdown 编辑器：可处理所有场景，但核对定位负担更大，当前采用整段确认及显式补入。

## 后果

MinIO 与索引不是 PostgreSQL 事务资源，清理失败或发布前崩溃可能使旧正文需要重新入库，但不得发布一半新正文。正文渲染与结构不一致的历史产物明确要求重新解析。人工确认只同步 MD 并收敛待核状态，不恢复上传任务的自动入库意图。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 确认后 MD 与回执同版本 | 只改页面、只改 sidecar | review service / repository | service unit 回读对象字节；真实 HTTP integration 已添加 | 上传失败、CAS 失败保留旧指针 | Passed（unit）；Not run（真实 HTTP/PG/MinIO） |
| 旧版本不能覆盖新版 | 并发保存、重解析、跨库 | repository / service / HTTP 依赖 | stale hash/revision/status/scope 单测 | 隔离进程移除 SHA guard 后，source_sha256 案例因 DID NOT RAISE 失败 | Passed（unit/负控）；Not run（真实 PG 并发） |
| 更改正文清理旧派生数据 | MD 新、检索仍旧 | executor | service unit 核对最终派生集合与 parsed；复用真实 executor 清理函数 | 清理失败不得发布新正文 | Passed（unit）；Inspected（executor）；Not run（真实外部索引） |
| 人工确认可操作且失败可恢复 | 错误采用候选、草稿丢失 | Vue panel / parent loader | DOM unit、lint/build；实际 Modal 的合成 HTTP 页面 | 保存 A 后仍保留 B 草稿，同段外部变化拒绝覆盖 | Passed（unit/组件页面） |
| 图片发布保留最新人工结果 | 旧 sidecar 覆盖回执 | figure service | 延迟发布时回读新 sidecar，含 commit ACK 失败 | 同 attempt 正文变化仍只合并图片字段 | Passed（unit） |

后端实际运行 `test_knowledge_review_sync.py`、`test_knowledge_figure_analysis.py`、`test_knowledge_parse_bundle.py` 共 60 项通过。本地以 `.venv/Scripts/python.exe -X utf8 -c` 载入 pytest；Windows stdlib 的 `test` 包遮蔽仓库 namespace，因此执行入口先创建 `test` 模块并把 `__path__` 绑定到仓库 `backend/test`，再调用 `pytest.main([...,'-q','--tb=short'])`。SDK 依赖使用被忽略的独立目标目录，未改项目依赖。

前端实际执行 `node --test --test-concurrency=1 test/unit/document_review.test.js test/unit/document_review_runtime.test.js test/unit/document_parse_state.test.js test/unit/knowledge_document_parse_api.test.js test/unit/api_boundary.test.js`，30 项通过。`pnpm run lint:check`、`pnpm run build` 验证前端，`cd docs && pnpm run build` 验证文档链接与构建。新增 Python service 与 unit 的 Ruff lint/format 通过；既有文件仍有原行诊断，新增修改行不增加诊断。`git diff --check` 通过。

实际 FileDetailModal、ReviewPanel 与 MarkdownPreview 在合成 HTTP 响应环境验证确认、保存失败保留输入、预览刷新和下载；重新读取下载字节确认数值从 10 改为 1.0、公式原样保留。浅色、深色与 375/768/1024/1280 宽度检查编辑区域，无页面横向溢出。独立 Reviewer 审查通过。

真实 HTTP/PostgreSQL/MinIO integration 未运行：本地无 Docker 命令且未配置数据库连接；现有 CI 使用显式选择器，未包含新增 integration，不假设 CI 已覆盖。integration 只创建 PostgreSQL chunk，即使运行通过也不直接证明已有 Milvus 向量与 Neo4j 图谱的消失；该范围保留未验证。工程契约检查仍有 13 个既有错误，契约单测 63 项中 1 项因 Windows 缺少符号链接权限报错，均不计通过。

提交前执行 `pnpm run test:unit`，432 项中 427 项通过、5 项失败：`agentRuntimeConfigVisibility` 的 3 项模块导入错误、`chatStartScreen` 的布局断言和 `modelProviderSaveActions` 的重复声明导入错误。失败对应文件不在功能修改范围，未把全量测试记为通过。管理权限新增路由用例使用可读但不可管理的管理员，校验拒绝写入后正文与回执不变，并显式弱化 MANAGE 为 READ 证明能进入副作用；该用例经独立审查，但本地收集被已有 `server.routers` 导入链依赖的 `os.O_DIRECTORY` 阻止，记录为 Not run，不伪造 Windows 的 POSIX 标志。相关 60 项后端测试与前端 lint 重跑通过。
