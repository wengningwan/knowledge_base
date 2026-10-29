# OXR 论文解析与独立校对产物

状态：proposed
类型：feature
Owner：backend/package/yuxi/services/knowledge_parse_service.py

## 问题

整页视觉转录把原文识别、图片定位和严格 JSON 契约交给一次模型输出，造成随机失败。纯原生文字提取又会损失公式、字体编码和阅读次序。把诊断和图片解释写入论文 Markdown，使下载文件混入原文以外内容，并让分块与论文正文难以区分。

## 提案

### 实现方案

在现有 OCR 注册和配置中增加 OXR，保留初始 disable 默认值和其他解析器入口。OXR 结构化块是主结果；原生文字按区域作为第二候选，局部视觉模型只校对疑点。插图模型独立生成说明。正文模型显式清空表示未启用，不把模型配置完整性当作主解析成功条件。

parser 的 ParsedDocument 统一携带 Markdown、块、图片资源、修订和诊断，既有字符串消费者使用包装入口。knowledge_parse_service 负责任务控制、不可变对象和发布准备，KnowledgeFileRepository 负责文件/知识库操作边界与同事务 owner fencing。正文与附属 JSON 的两个指针归同一 attempt，失败清理只删除自己未发布的前缀。schema 相邻升级为旧文件增加可空字段。

前端详情展示正文、图片解析和校对结果，导出只读取保存的正文并内嵌知识库图片。legacy 文件通过重新解析获得新产物；重新解析成功发布前清理旧派生索引，随后由用户重新入库。失败重试、取消、外部清理失败和整库删除的边界由同一文件/知识库锁体系闭合。

当前代码实现已准备；真实服务、数据库并发和论文视觉验收按用户要求在另一台电脑进行，运行证据取得后收敛此记录。配置事实由[文档处理与 OCR](../../../advanced/document-processing.md)拥有，产物和生命周期由[知识库机制](../../../mechanisms/knowledge-base.md)拥有。

## 替代方案

只给整页模型转录重试仍让正文受随机生成影响。只用文字提取无法恢复缺失的字体和公式。让模型重写整篇论文会增加语义改写风险。商业 API 的高级开关不能假定在开源或免费部署上均有效，因此请求仅使用核实契约，本地整理保留来源依据。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| OXR 表单、状态、恢复正确 | HTTP 与业务码、取消 | parser/oxr.py | test_oxr_parser.py 协议用例 | 200+非零业务码、worker.cancel | 已编写，Not run |
| MD 保留论文图文 | 块序列与公式、浮图 | parser/pdf_structure.py | test_oxr_structure.py、人工对 PDF | 双栏跨页、未知类型、四公式编号 | 已编写，Not run |
| 模型不污染正文 | 局部 patch 与独立说明 | services/pdf_review_service.py | test_pdf_review.py、test_pdf_disable_figures.py | 改写整段、替换原图、无效JSON | 已编写，Not run |
| 发布与清理有归属 | PG、对象存储与任务取消 | services/knowledge_parse_service.py | 真实 PG durable/schema 集成用例 | 取消时记录task_id、拒绝发布 | 已编写，Not run |
| 前端与导出保持版本 | HTTP、权限与异步请求 | FileDetailModal.vue | web unit 与真实页面验收 | 旧请求、版本变化、图片读取失败 | 已编写，Not run |

## 风险

OXR 与视觉模型都可能识别错误；双候选和局部校对提高可审查性，不能保证零误差。免费服务可用性和不同部署版本需要实际验证。不可变 attempt 在文件删除前保留历史已发布资源，增加对象存储占用。真实 PostgreSQL、MinIO、Milvus、Neo4j、worker、浏览器、模型及目标论文视觉验收尚未执行，本机只进行静态源码检查。
