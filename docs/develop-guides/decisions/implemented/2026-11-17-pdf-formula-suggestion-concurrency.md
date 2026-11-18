# 公式簇草稿复用文档模型并发

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/pdf_review_service.py

## 问题

公式簇草稿在普通正文校对之前逐项等待模型返回。同一文档存在多个公式簇时，即使文档模型并发配置大于 1，该阶段仍只发出一个请求，长耗时调用会推迟后续草稿和正文校对。

## 决策

目标是让独立公式簇按已有文档模型并发配置生成草稿，保留每项草稿归属、失败记录和人工确认行为。范围限于公式簇模型请求调度；裁图、提示词、草稿校验、普通正文校对顺序和配置界面保持当前行为。

### 实现方案

`pdf_review_service._suggest_cluster_formulas` 选择公式簇问题，通过 `pdf_model_execution.run_pdf_workers` 有界消费，消费者数量取公式簇数量与 `executor.concurrency` 的较小值。每个消费者只更新当前问题的 `suggested_content`、`suggestion_status` 和带问题身份的诊断；草稿队列完成后继续普通正文校对。

模型请求继续经同一个 `PdfModelExecutor.request` 获取端点与模型的跨文件、跨 Worker 共享额度。单项缺图、调用失败或回复不合法时记录该项失败，继续处理队列；取消向上传播，由已有队列执行器取消并等待已启动消费者收敛。正文仍由原有人工确认入口补入。

## 替代方案

- 保持逐项等待：实现简单，但多个公式簇无法利用已配置的并发额度。
- 对全部公式簇直接创建任务：共享准入仍能限制实际请求，但无界创建等待任务，重复已有队列能力。
- 复用现有有界队列：统一消费者上限、取消清理和共享准入，采用此方案。

## 后果

公式簇草稿复用现有并发设置、共享准入和取消清理。每项结果保持原问题身份；完成顺序随模型响应而变化，附属 JSON 中的问题顺序保持原列表。普通正文校对继续等待草稿队列处理完毕。

模型服务的实际处理能力、限速和耗时仍由供应商决定。并发草稿会与相同端点模型的其他文档请求竞争已有共享额度。取消本地调用后，远端模型请求是否停止取决于供应商。

## 验证

Inspected：静态核对公式簇经有界队列调用原 `PdfModelExecutor.request`、逐项返回后继续消费，以及取消向上传播后的消费者清理。提示词、回复校验和正文人工确认入口保持当前实现。

`git diff --check -- backend/package/yuxi/services/pdf_review_service.py docs/advanced/document-processing.md`：Passed，退出码 0，无空白错误；该命令只检查已跟踪文件的差异。

以下为部署电脑上的人工验收预期，尚无实际运行结果。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 多个公式簇使用现有配置并发，跨文件仍共享额度 | 串行等待或绕过共享准入 | `backend/package/yuxi/services/pdf_review_service.py`；`backend/package/yuxi/services/pdf_model_execution.py` | 部署电脑以并发 8 解析含至少 8 个公式簇的 PDF；核对模型服务实际在途请求及最终附属 JSON；再以并发 1 和同模型多文件核对上限 | 串行实现无法出现请求重叠；绕过共享准入会使同端点模型实际在途请求超额 | Not run |
| 每项结果绑定原问题，单项失败后其余项仍有结果，正文等待人工确认 | 结果串项、失败中断队列或自动改写正文 | `backend/package/yuxi/services/pdf_review_service.py` | 在部署电脑制造单项调用失败、缺图或空回复；回读附属 JSON 中各问题草稿、诊断及 Markdown，并在校对面板核对 | 故障项应记录 failed；其他项继续；未经确认的草稿不得进入 Markdown | Not run |
| 取消停止本地消费者和后续发布 | 残留消费者继续发请求或发布结果 | `backend/package/yuxi/services/pdf_model_execution.py` | 部署电脑在多个草稿请求进行时取消 Task；核对 Task 终态、模型准入槽收敛及正文和附属 JSON 发布指针 | 取消后不得发布该次解析结果或继续启动待处理草稿 | Not run |

Not run：本机按仓库约定只编辑和审阅源码、文档，运行证据由部署电脑人工提供；尚未执行模型调用、取消验证、工程契约脚本或文档构建。
