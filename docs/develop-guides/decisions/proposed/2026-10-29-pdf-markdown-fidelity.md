# PDF Markdown 视觉转录与图片导出

状态：proposed
类型：bug-fix
Owner：backend/package/yuxi/knowledge/parser/unified.py

## 问题

知识库禁用 OCR 引擎时直接导出 PDF 原生文字，文本层无法表达的公式、化学键、上下标和版面顺序进入持久 Markdown；插图解析失败只留下日志，下载内容引用私有站内图片路径。

## 提案

### 实现方案

知识库解析用已选视觉模型逐页读取渲染图、候选文字及带 ID 的插图，输出带续文及其连接方式、插图占位符的 Markdown；续文先接回上页正文，再放置页首浮动图。期刊封面或重复页眉标志可按受限理由明确排除，每个图片 ID 只能拥有一个归宿；独立的插图解释标识为模型生成。无法读取的文字、未定位图形、缺失占位符或模型失败阻止保存残缺结果。已有 OCR 引擎选择和私有图片授权边界保持原有 Owner。

`ocr_service` 只对携带知识库图片归属的禁用 OCR 任务补齐系统默认模型；共享临时附件无模型时继续纯文本解析，避免新增模型调用或写入 unknown 图片归属。下载入口使用鉴权图片 API，将实际引用的知识库图片内嵌到单个 Markdown 文件。

PDFium 原生调用的共同锁由 `knowledge/utils/pdf_utils.py` 拥有，页面预检、视觉转录渲染、RapidOCR 和 DeepSeek 的原生操作共享该锁。模型和 OCR 等待在锁外执行，避免并发文档同时进入非线程安全的原生库。

## 替代方案

正则替换无法恢复丢失的公式结构；新增完整的 Docling PDF 模型管线需要额外推理依赖和模型部署；强制切换 MinerU 改变已选引擎并新增服务前置条件。复用已有视觉模型只改变知识库禁用 OCR 路径，保留其他引擎供需要确定性版面解析的用户选择。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 模型返回公式、图文位置和独立解释被完整装配 | 公式扁平化、图片追加页末、解释缺失 | PDF parser | 相关 pytest 与真实科研 PDF 逐页核对 | 缺图、串页、重复图、残留乱码、解释空或失败 | Not run：用户指定在另一台电脑运行 |
| 临时附件保持纯文本能力且不写未知归属图片 | 共享入口新增模型依赖 | ocr_service / PDF parser | 相关 pytest | 无模型且无图片归属的附件 | Not run |
| 下载结果携带知识库图片，失败阻止残缺下载 | 站内路径离线失效、错误响应被当作图片 | Web 导出 | Node unit、真实页面断网打开导出 MD | 重复图片、未授权、非图片响应、外部地址 | Not run |

在具备应用依赖的环境中执行：

```bash
docker compose exec api uv run --group test pytest test/unit/knowledge/test_pdf_markdown.py test/unit/knowledge/test_pdf_disable_figures.py test/unit/knowledge/test_parser_facade.py -q
docker compose exec web node --test test/unit/markdown_export.test.js test/unit/api_boundary.test.js
docker compose exec web pnpm run lint:check
docker compose exec web pnpm run build
```

真实验收使用含独立公式、双栏正文、图注与图片的科研 PDF，逐项核对四类展示公式中的分数、负号、希腊字母及上下标，化学键、孔体积变量、标题、跨页句子与图文对应；论文原有数值疑点保留。图片下方必须能区分原文图注和模型生成解释。下载后断开网络，用支持 data URI 的 Markdown 阅读器重新打开确认图片可见。未配置模型、模型失败或正文图形无法定位时确认文件进入错误态，没有保存残缺新产物。运行检查尚未执行，静态格式契约不能代替这些验收。

## 风险

逐页视觉调用增加耗时、费用并依赖模型的视觉和 LaTeX 能力；JSON 及占位符校验不证明模型内容完全准确。嵌入位图以外的正文图形需要模型显式报告未定位，后续使用版面 OCR 解析。真实模型、浏览器和离线阅读器验收在用户提供的运行环境完成。
