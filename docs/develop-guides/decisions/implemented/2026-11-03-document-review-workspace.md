# 文档校对问题工作区

状态：implemented
类型：feature
Owner：web/src/components/knowledge/DocumentReviewPanel.vue

自动入库限制与简化展示由[校对疑点阻止自动入库与可读结果](2026-11-03-review-auto-index-and-readable-results.md)部分取代。

## 问题

文件详情逐条展开校对全文、候选和诊断，用户需要从正常记录和重复文本中寻找疑点，缺少连续定位和对照入口。

## 决策

### 实现方案

校对组件消费现有 parse-assets 结果，按 parent_issue_id 或独立记录身份归并，展示待人工核对、复核未完成、系统已修改和全部记录入口，各入口分别按原问题计数，允许同一问题同时属于多个入口。出版元素不计为待人工核对和系统修改；剩余冲突继续计为待核对，协议错误继续计为未完成。未知状态作为待核对呈现，不推断成功。归并及文字对照由 web/src/utils/documentReview.js 拥有。

列表分页并只展开选中的一个问题，分片可逐项切换并显示 targets 中登记的旧文及上下文。详情并排显示鉴权裁图和文字差异，共同文字缩略但完整上下文可展开。after 表达整块正文快照，可以包含其他问题的修改，不能推断本条回执已应用；失败或未修改项优先展示未应用文字层候选。缺失 after 明确保留已识别正文，显式空字符串表达块内容移除。差异仅负责视觉对照，不改变解析结果；过大差异区域整体突出并保留全文，限制比较开销。

文件详情仅在校对视图增宽；缓存当前校对组件，切换详情视图后返回时保留选择。校对面板保留页码和裁图放大，不提供跳转 PDF 指定页的按钮；源文件预览仍由文件详情的顶部入口提供。裁图放大复用鉴权 Markdown 渲染，完整上下文按需展开。候选依据与解析诊断的展示方式由后续简化决定拥有。首版没有人工修改、确认或持久化写入。

## 替代方案

只给旧卡片增加折叠仍要求用户遍历正常记录。增加表格可以降低高度，但长段落、公式和原图仍需独立详情。直接加入编辑保存需要新的版本与持久化契约，超出首版范围。

## 后果

用户先定位需要判断的问题，再对照原文与具体差异；模型回执与系统修改保留来源标签。原问题可以同时有修改、未决和技术失败片段，各筛选计数不能相加解释为总问题数。正文快照的变化范围可能大于当前片段，界面明确说明范围，避免将相邻修改归因于失败回执。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 疑点不丢失、分片不膨胀数量 | 状态投影 | documentReview.js | node --test test/unit/document_review.test.js | 已修改仍有冲突、协议错误、未知状态与出版元素 | Passed |
| 差异保留原字节、候选不冒充正文 | 文本投影 | documentReview.js、DocumentReviewPanel.vue | node --test test/unit/document_review.test.js test/unit/document_review_runtime.test.js | 多处增删、空 after、缺失 after、长文本、成功与失败片段共享整块快照 | Passed |
| 用户可连续核对，校对面板不提供 PDF 跳转入口 | Vue 交互 | DocumentReviewPanel.vue、FileDetailModal.vue | 真实组件交互测试与浏览器代表数据验证 | 筛选为空、跨页、切换分片、解析版本更新，带有效页码时也不生成跳转按钮 | Passed |
| 页面保持主题、响应式与异常语义 | UI 装配 | DocumentReviewPanel.vue | lint、build、浏览器截图 | loading、error、empty、375/768/1024px 与暗色 | Passed |

前端新增逻辑和组件交互共 7 项测试通过。`pnpm run build` 在 web 与 docs 通过；`pnpm run lint:check` 通过。`pnpm run test:unit` 全量 431 项中 426 项通过、5 项失败，失败位于未修改的 agentRuntimeConfigVisibility、chatStartScreen 和 modelProviderSaveActions 测试，分别涉及 Windows 换行下的导入替换与既有聊天布局断言。

根目录 `python -X utf8 scripts/verify_engineering_contracts.py` 报告 13 个既有错误，涉及三份旧 PDF 决策记录及 document_parse_state.js 的路径边界；本记录无新增错误。`python -m unittest scripts.test_verify_engineering_contracts` 执行 63 项，其中 1 项因 Windows 缺少符号链接权限失败。`docker compose exec api uv run --group test pytest test/unit -m "not slow"` 未执行，环境没有 Docker 命令。

浏览器使用真实文件详情与校对组件，输入为合成代表数据；分页、切换分片与详情视图往返保持选择由组件测试覆盖。页面在 375、768、1024px 无横向溢出，浅色、暗色和加载/空/错误状态均检查。真实后端文档、鉴权服务和生产解析产物未联调。

系统检测与模型回执不能作为人工审核结果；人工确认、修订保存和解析版本冲突处理属于后续独立设计。
