# PDF回执兼容与主解析结果分离

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/parser/pdf_review_contract.py、backend/package/yuxi/services/knowledge_task_service.py

自动入库排除范围由[校对疑点阻止自动入库与可读结果](2026-11-03-review-auto-index-and-readable-results.md)扩展为 needs_review 与 incomplete。

任务状态部分取代[PDF完整复核与共享模型并发](2026-10-30-pdf-complete-review.md)。

## 问题

真实双模型日志显示主OCR产物与图片结果已发布，正文部分回执因无实际改动、错误原文或越界修改被拒绝，任务随后把整个文件标成失败。重复无改动回执增加调用；最终再校验丢失原拒绝原因，缺少可操作的故障解释。

## 决定

先校验问题、目标、块版本、原文和证据，全部通过后的无修改回执按unchanged解释；文字候选冲突时保持未决。越界修改、错误ID或不匹配原文继续拒绝。请求明确给出响应结构和不变回执示例，重试携带各问题具体拒绝代码；最终保留真实失败原因，避免统一改写成缺失ID。未检查与失败内容不计checked，不写入成功缓存。

主解析成功与模型复核状态分别统计。主MD成功发布后，复核incomplete以结构化摘要和进度警告表达，不单独触发主解析任务失败；incomplete仍跳过自动入库。真正OCR、文件发布及索引失败仍计失败。MD只保留原文，警告与模型信息留在任务和校对结果。

并发配置、模型单次无固定超时、完整队列和worker/OXR原有机制保持现有契约。不得用删除全部证据校验或扩大模型改写权限来消除报错。

## 替代

继续要求所有模型严格输出状态标签，会对已证明没有改动的回执进行重复请求。把无效补丁当成已修复会隐藏内容错误。把整个PDF任务一直判失败会掩盖已发布主产物与局部复核问题的边界。

## 验证

| 验收主张 | 失败面 | Owner | 直接证据 | 负例 | 结果 |
|---|---|---|---|---|---|
| 无改动回执有正确语义 | 模型JSON/证据 | pdf_review_contract | 人工无改动及科学冲突用例 | 错old、错页、未知目标仍拒绝 | Not run |
| 拒绝原因保留且重试可纠正 | 公开模型循环 | pdf_review_service | 公开入口回归 | 错补丁不能变成checked或覆盖原文 | Not run |
| 复核警告不冒充主解析失败 | Durable Task摘要 | knowledge_task_service | ingest/parse/pending回归 | 真解析失败仍失败，自动入库仍跳过 | Not run |

用户要求本机不启动环境、OCR或模型，不运行应用测试、lint/build；仅静态检查。真实服务、内容准确度与效果由验证机确认。文章核对报告独立保存，不把机器字符相似度当作科学准确率。
