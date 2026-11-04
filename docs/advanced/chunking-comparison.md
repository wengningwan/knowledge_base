# 分块方案同题对照

本页面向维护论文知识库的开发和验收人员，说明如何在同一批原文、问题、Embedding 和回答模型下比较切分方案。知识库上传与入库操作见[创建并使用知识库](../intro/knowledge-base.md)，分块参数见[文档处理与 OCR](document-processing.md#分块参数和配置快照)。

## 前置条件

按[升级指南](deployment.md)启动兼容的 storage-migrator、API 和 worker。论文来源字段需要 knowledge schema 4；旧索引需重新入库才能产生来源。对照命令在已配置数据库、Milvus 和模型的 API 容器内运行，使用部署的运维权限读取知识库，不提供 Web 接口，也不写入或重建知识库。

准备两个 Milvus 知识库，导入同一批已核对的 Markdown：一个显式选择 Separator、重叠比例为 0，一个选择 Paper。各库必须使用同一 Embedding 模型并完成索引；原文指纹及文档数量必须一致。命令保存实际文件的分块参数快照，对照期间保持原文、分块及模型配置不变。对照固定关闭图号与同小节关联补取，并在报告的检索配置中记录 `include_related_evidence=false`，检验切分产生的主证据；普通问答使用关联补取的效果需要另行核验。不要把题目或标准答案加入这些知识库。

对照要求每个片段都有精确原文来源。General 等重排或拼接正文的策略，以及带重叠的 Separator，可能无法建立完整来源；当前命令会拒绝这些输入，重复入库不能弥补不支持的来源映射。

命令固定所有组的检索模式、Top K、融合权重和阈值，并关闭图检索与 Reranker，避免其他变量干扰分块比较。默认使用 hybrid，权重为向量 0.7、BM25 0.3；也可用 `--search-mode vector` 或 `keyword` 分别比较。回答 prompt 与现有知识库评估相同，只使用召回前五块。对照使用只加载已有集合的检索路径，索引缺失或模型/结构不兼容时失败，不执行普通查询入口的自动创建或重建。

## 固定问题格式

JSONL 每行一题，包含以下字段：

| 字段 | 含义 |
|---|---|
| `id`、`query` | 固定题号和问题，各方案不得改写 |
| `gold_evidence` | 人工核对的正确原文范围列表，每项含 `source_sha256`、`start_char`、`end_char`、`line_start`、`line_end` |
| `required_conditions` | 必须覆盖的条件，每项为可接受表述列表，如 `["300 °C", "300°C"]` |
| `answer_points` | 人工维护的答案事实、条件及不可混淆的样品归属 |

指纹针对当前解析后的完整 Markdown UTF-8 字节；从内容响应的 `source_sha256` 核对，不能使用原始 PDF 的指纹。行号从一开始且包含两端；字符范围按 Unicode 字符从零开始、右端不包含，含表情的字符也按一个 Unicode 字符计。字符范围用于覆盖计算，避免只命中同一长行的少量文字就算整段命中。

题目及原文范围独立于 chunk 编号。改变分块策略不更新金标准；修改原文后需人工复核题目和证据，命令会拒绝未知原文指纹。问题应至少覆盖公式变量及单位、同一样品的条件与结果、图注、表格表头。模型可以辅助起草，人工审核后再冻结为验收题。

仓库的 [合成论文](https://github.com/xerrors/Yuxi/blob/main/backend/test/fixtures/paper_chunking/paper.md) 和 [四道固定题](https://github.com/xerrors/Yuxi/blob/main/backend/test/fixtures/paper_chunking/questions.jsonl) 使用虚构数据验证软件链路；它们不代表真实化学论文的质量验收。正式论文及测试结果保存在部署数据目录，不提交仓库。

## 执行对照

先将合成论文分别上传到两个测试知识库并完成对应策略的索引，用真实知识库 ID 替换下面的占位值：

```bash
docker compose exec api uv run python -m yuxi.knowledge.eval.chunking_comparison --cases test/fixtures/paper_chunking/questions.jsonl --variant separator=kb_separator --variant paper=kb_paper --answer-model provider:model-id --output /tmp/chunking-comparison.json
```

省略 `--answer-model` 只检查真实检索结果，答案状态保存为 `not_run`。真实模型报错、来源缺失或配置不一致时命令失败；不把空输出当作通过。命令只创建新报告文件，已有文件不会被覆盖。

`--milvus-db` 指定已有索引所在的 Milvus 数据库，默认 `default`，对应常规 ORM 查询连接的默认命名空间；有显式部署覆盖时填写实际名称。命令只连接指定数据库，不创建数据库或静默回退。

评估自己的论文时，把人工审核后的 JSONL 放入 API 容器可读取的文件位置，再修改 `--cases`。报告同时保存问题快照及指纹、模型与检索参数、实际片段、回答 prompt、生成答案和人工核验空位。来源缺失时先重新入库，不修改题目迎合索引结果。

## 核对报告

`retrieval_metrics.evidence_coverage` 统计前 K 块覆盖了多少完整金标准范围；`answer_context_metrics` 只统计真正进入回答 prompt 的前五块。两者必须分开查看。每个范围的 `range_coverage` 可以定位部分召回。

`condition_text_coverage` 仅检查必要条件文字是否出现，兼容明确列出的同义表述和空白；它不能证明该数值属于正确样品，也不能证明科学结论正确。数值与单位的对应、跨论文可比性和未报告条件由人工核验。

逐题核对 `answer_points`、`answer_prompt` 与原文，在 `human_review` 填写 `conditions_complete`、`answer_supported`、`unsupported_assertions` 和备注，再将状态改为已核验。存在无依据断言、样品条件混用或关键条件遗漏时，该题不能判为通过。报告初始为 `pending_human_review`，没有自动科学正确率或自动胜出方案。

## 部署后回归

在有真实底座的部署中执行相关 PostgreSQL/HTTP integration 和论文入库 E2E，再检查真实页面的长片段展开、来源窗口、重复块、未知来源、暗色和窄屏。E2E 使用独立测试资源和真实 Embedding 模型，具体环境参数由测试源码维护；命令见[测试规范](../develop-guides/testing-guidelines.md)。离线切分与 Web unit 结果不能替代这些验证。

```bash
docker compose exec api uv run --group test pytest test/integration/services/test_schema_migration_version.py
docker compose exec -e TEST_PAPER_EMBEDDING_MODEL_SPEC=provider:embedding-model api uv run --group test pytest test/e2e/test_paper_ingestion_e2e.py -m e2e
```

E2E 还需按测试规范配置测试管理员身份。缺少测试身份或真实 Embedding 时的 skip 表示未验证，不能计为产品通过。
