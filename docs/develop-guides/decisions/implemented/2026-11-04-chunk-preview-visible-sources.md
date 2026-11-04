# 分块预览只定位可见正文的来源

状态：implemented
类型：bug-fix
Owner：web/src/utils/chunkPreview.js

## 问题

Paper 检索块在正文前生成论文名和章节上下文，并把题名、章节标题与证据正文的坐标一起写入来源元数据。预览隐藏生成前缀后，来源行号和弹窗仍包含题名，导致没有论文标题的分块也显示原文第 1 行。

## 决策

### 实现方案

[预览工具](../../../../web/src/utils/chunkPreview.js)复用 `chunkPreviewContent` 对已识别 Paper 前缀的判断。在当前原文指纹、字符范围和行号核对成功后，仅保留原文片段逐字包含于预览正文的既有来源范围，再统一派生来源行号、弹窗窗口和 PDF 页码。没有剩余来源时显示位置未知。坐标来自后端元数据，不通过正文搜索重建位置。

后端检索内容和持久化来源保持完整，已有 Paper 块直接应用展示过滤，无需重新入库。未识别前缀的块沿用现有定位行为。这是边界明确的展示修复，没有需要先裁决的持久化或模型输入变化，因此直接记录为 implemented。

## 替代方案

按行号删除第 1 行会误删从首行开始的真实正文。修改后端来源并重新入库会改变检索内容的溯源范围，且不能直接修正已有分块。只过滤行号文案会让弹窗和 PDF 跳转继续引用隐藏上下文。

## 后果

来源标签、原文弹窗高亮和 PDF 跳转使用同一组正文来源。章节仍由卡片标签展示，其独立标题范围只在片段也包含于正文时参与定位。标题接近正文时仍可能作为窗口上下文出现，但不作为独立来源或高亮行。

## 验证

- Inspected：沿 `chunk_paper` → `source_metadata` → `describeChunkSource` → `ChunkPreviewPanel` 静态核对来源生成与展示链路。
- 回归用例位于[来源逻辑测试](../../../../web/test/unit/chunk_preview.test.js)和[组件测试](../../../../web/test/unit/chunk_preview_runtime.test.js)，覆盖 v1/v2 标题过滤、首行真实正文、非连续实验条件、重复正文、页码过滤、指纹失效与无剩余来源。恢复直接使用全部来源的行为，会使正文行号、窗口及 PDF 页码断言失败。
- Not run：依本机工作约定，不启动程序，不执行测试、lint、构建及工程契约脚本。另一台电脑需执行 `docker compose exec web pnpm run test:unit`、`docker compose exec web pnpm run lint:check`、`docker compose exec web pnpm run build` 及仓库提交前 gate，并在真实页面核对来源标签和弹窗。运行结果及页面表现尚未验证。
