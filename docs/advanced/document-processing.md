# 文档处理与 OCR

Yuxi 把文档处理拆成两步：先把原文件保存到知识库，再根据文件类型和 OCR 配置生成 Markdown。知识库索引使用生成后的内容；附件解析只把结果写回当前 Workdir，不会创建知识库文件记录。

文件状态和存储归属见[知识库机制详解](../mechanisms/knowledge-base.md)，第一次上传文档见[创建并使用知识库](../intro/knowledge-base.md)。

## 支持的文件

知识库上传接口当前支持：

- 文本：`.txt`、`.md`、`.html`、`.htm`、`.json`、`.csv`；
- Office：`.docx`、`.pptx`、`.xls`、`.xlsx`；
- PDF：`.pdf`；
- 图片：`.jpg`、`.jpeg`、`.png`、`.bmp`、`.tiff`、`.tif`、`.webp`；
- ZIP：压缩包内必须包含 UTF-8 编码的 `.md` 文件。

Office 文件不走 OCR 引擎：系统用内置的 docling-slim 在本地把它们转成 Markdown（`.docx`、`.pptx`、`.xlsx`、`.xls` 都有对应解析后端，API 镜像已附带所需的 LibreOffice 组件）。选择 MinerU Official 引擎时，`.docx` 和 `.pptx` 也可以直接交给该云服务解析。其余类型中，图片文件必须使用 OCR 引擎；知识库 PDF 选择 `disable` 时使用页面选择的视觉模型逐页转录，模型前置条件与失败处理见下文。

ZIP 处理会优先使用名为 `full.md` 的 Markdown 文件，否则使用压缩包中找到的第一个 `.md` 文件，并把 `images/` 下的图片上传到知识库图片存储。压缩包内的绝对路径和 `..` 路径会被拒绝。

## 从 URL 导入网页

网页导入受白名单控制。设置 `YUXI_URL_WHITELIST` 后，系统才会抓取 URL，并把 HTML 转成 Markdown 进入同一套知识库处理流程：

```bash
YUXI_URL_WHITELIST=github.com,docs.example.com,*.wikipedia.org
```

列表以逗号分隔；配置的域名及其子域名可以通过校验，空列表表示关闭 URL 导入。抓取器的安全边界由本页拥有：DNS 解析到 loopback、私有网段或 link-local 地址时拒绝请求，并逐跳检查重定向目标，最多跟随 5 次重定向，只接受 HTML，响应体默认不超过 10 MB。DNS 解析失败目前会记录日志后继续请求，因此 URL 白名单和地址检查不能当作网络出口防火墙；生产环境还应在网络层限制出口。

## 选择 OCR 方案

| 引擎 | 运行位置 | 输出和适用场景 |
| --- | --- | --- |
| RapidOCR | 本地 CPU | 轻量图片/PDF 文字识别，默认方案 |
| MinerU | 本地 GPU 服务 | 复杂 PDF、表格和版面分析，输出 Markdown |
| MinerU Official | MinerU 云服务 | 不在本机部署 GPU，使用官方解析服务 |
| PP-Structure-V3 | 本地 GPU 服务 | 表格、票据和版面解析 |
| DeepSeek OCR | SiliconFlow API | 使用 SiliconFlow 的 DeepSeek OCR 模型 |
| PaddleOCR-VL-1.6 | 百度 AI Studio 云服务 | 文档版面解析，输出 Markdown |
| PP-OCRv6 | 百度 AI Studio 云服务 | 基础 OCR，输出纯文本 |

系统内部引擎 ID 为 `rapid_ocr`、`mineru_ocr`、`mineru_official`、`pp_structure_v3_ocr`、`deepseek_ocr`、`paddleocr_vl_1_6` 和 `paddleocr_pp_ocrv6`。页面中的健康状态只表示配置或服务探测结果，真正解析时仍会验证凭证和接口。

## 在页面配置

管理员进入“设置 → OCR 配置”可以设置默认引擎，并配置自托管服务地址或云端凭证。知识库上传和附件解析时可以单独选择引擎；没有单独选择时，使用系统默认值。

配置字段的读取规则是：数据库中保存的非空值优先，数据库没有值时读取对应环境变量。保存为空会清除数据库值并回到环境变量。API 不会回显环境变量中的密钥；数据库凭证只显示脱敏预览。

### PDF 公式与图片解释

知识库 PDF 在不启用 OCR 引擎时，需要选择支持图像输入的插图解析模型。服务端对没有显式模型的知识库任务读取“默认插图解析模型”；两处都未配置时解析失败，提示选择模型或改用版面 OCR。模型逐页读取渲染页面，恢复标题、段落、LaTeX 公式、上下标和图注；原生文本只作为候选资料。逐页调用和独立图片解释会增加模型调用次数、耗时及费用。

图片按页面视觉位置嵌入 Markdown。图片下方的“插图解析（模型生成）”是独立的模型解释；`Figure` 图注和正文属于论文原文。模型可以明确分类并排除期刊封面缩略图、重复页眉标志，正文小图和图形摘要保留。跨页续文先接回正文，再放置页首浮动图，拼接时区分英文空格、排版断词与化学名称原有的连字符。无法读取的公式、无法映射到图像资源的正文图、缺失或重复图片占位符、空解释、图片上传失败和模型调用失败都会阻止保存残缺结果。无法定位矢量图或复杂版面的文件可改选 MinerU 或其他支持版面、公式的 OCR 引擎。

格式校验只检查返回结构、图片身份和显式失败报告。模型仍可能识别错字符或漏段，科研用途需对照原 PDF 复核公式、单位、数值与图文对应关系。模型保留作者原有的数值、引用和科学表述，不把原文疑点静默改写成新的结论。既有已解析文件需要重新解析才能使用该流程。

临时聊天附件没有知识库图片归属且未指定模型时，保留只读原生文本层的行为，不调用默认视觉模型，不向未知知识库前缀上传图片；扫描件或复杂公式应选择可用 OCR 引擎。

## 配置各类引擎

### RapidOCR

RapidOCR 随 API/worker 安装，第一次实际解析时加载 ONNX 模型。默认模型目录由 `RAPIDOCR_MODEL_DIR` 指定；没有设置时使用容器缓存目录。它不需要单独启动服务，但首次解析可能需要下载模型。

### MinerU

开发 Compose 已提供 `mineru-api`，它属于 `all` profile，端口为 `30001`，解析端点是 `/file_parse`：

```bash
docker compose --profile all up -d --build mineru-api
```

API/worker 在 Compose 中默认使用 `MINERU_API_URI=http://mineru-api:30001`。如果显存不足导致 MinerU 启动失败，可以在 `docker-compose.yml` 的 `mineru-api.command` 中启用并调整：

```yaml
# --gpu-memory-utilization 0.5
```

该参数用于减小 vLLM KV cache；仍然不足时可以继续尝试 `0.4` 或更低值。模型下载和其余显存参数以 `docker/mineru.Dockerfile` 与 Compose 为准。解析超时可以用 `MINERU_TIMEOUT` 调整，或在单次处理参数中传入 `timeout_seconds`。

### MinerU Official

在[官方服务](https://mineru.net)申请 API Key，放入 API/worker 的环境：

```bash
MINERU_API_KEY=<your-mineru-api-key>
```

页面中配置也可以保存该凭证；生产环境优先使用环境变量。该服务会把文件发送到外部服务，请先确认数据合规和供应商条款。

### PP-Structure-V3

开发 Compose 的 `paddlex` 服务属于 `all` profile，默认在容器内的 `8080` 端口提供版面解析：

```bash
docker compose --profile all up -d --build paddlex
```

API/worker 默认使用 `PADDLEX_URI=http://paddlex:8080`。本地 GPU 和镜像构建要求以 Compose 为准。

### DeepSeek OCR

在“智能体 → 模型供应商”中启用 `siliconflow-cn` 并配置凭证。DeepSeek OCR 会复用该供应商的 API 地址和 API Key，不接受单独的 OCR 凭证配置。

### PaddleOCR API

在[百度 AI Studio Access Token 页面](https://aistudio.baidu.com/account/accessToken)获取 Access Token：

```bash
PADDLEOCR_API_TOKEN=<your-paddleocr-token>
PADDLEOCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
```

`PADDLEOCR_API_URL` 可省略，默认使用上面的地址。`PaddleOCR-VL-1.6` 和 `PP-OCRv6` 共用这组配置；前者输出 Markdown，后者输出纯文本。

## 分块参数和配置快照

知识库的分块配置由两部分组成：`chunk_preset_id` 表示策略，`chunk_parser_config` 保存该策略的具体参数。文件级 `processing_params` 会保存本次使用的 `ocr_engine`、分块策略和参数快照；OCR 服务地址和凭证在执行时读取，不写入文件快照。

修改分块策略不会自动重写已经索引的文件。要让旧文件使用新策略，需要重新解析或重新入库，并在最后确认文件状态和检索结果。

## 图片访问

解析器生成的知识库图片保存在私有 `kb-images` bucket，通过带知识库权限校验的后端路径访问。不要把 MinIO 对象地址直接写成公开 URL，也不要为方便预览而开放整个 MinIO 管理端口。头像等公开图片使用 `/minio/public/...` 同源只读代理，二者边界不同。

文件详情的“下载 Markdown”通过当前登录会话读取文档中的知识库图片，并内嵌为 data URI，下载后不依赖站内相对图片路径。同一图片重复引用只读取一次；图片读取失败、返回非图片或空内容时停止下载并显示错误。外部图片 URL 保持原样，导出不会携带用户认证向外部站点取图。下载文档会实际携带图片内容，分享时按文档本身的权限要求处理；离线显示需要 Markdown 阅读器支持 data URI 图片。

## 文件限制与排查

- 知识库和工作区单个上传文件最大 100 MB。
- 工作区一次最多上传 50 个文件；知识库批量处理使用任务和页面限制。
- Agent 的 `read_file` 直接读取 UTF-8 文本和图片；PDF、Office 或其他二进制文件先用 `ocr_parse_file` 生成 Markdown。
- OCR 服务不可用时，先在 OCR 配置页查看健康状态，再检查容器日志、API 地址和凭证。
- 解析完成不等于索引完成；回到知识库文件列表确认 `parsed` 或 `indexed`，再做检索验证。

详细变量和容器映射见 `.env.template`、`docker-compose.yml` 和 `docker-compose.prod.yml`。文档页面只保留影响操作的配置，避免与配置文件维护两份默认值。
