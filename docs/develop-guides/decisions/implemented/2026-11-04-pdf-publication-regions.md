# PDF 出版区域与正文遗漏分离

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/parser/pdf_repair.py

## 问题

PDF 原生文字中的逐页变化页码、单次版权行和页脚 DOI 被生成为疑似正文遗漏，增加人工核对负担。只按重复文字分类无法识别这些区域；DOI 的元数据保护还会优先于页脚判断。

## 决策

### 实现方案

OXR 校对入口保持不变。规则在生成遗漏问题前，结合解析块的页眉页脚类型、原生行的位置与跨页关系确定出版区域。页码需要页边短数字、相邻页编号递进与位置对应；版权行需要页边位置和明确版权格式；独立 DOI 需要明确页眉页脚、同位置跨页重复或邻近版权行的证据。

出版区域沿用 publication_element 与 classification=excluded，原始候选和区域证据继续保存在附属产物。正文渲染规则保持不变。正文数字、参考文献 DOI、联系方式、SI、明确脚注和图表内容继续保留；完整参考文献和解释型 DOI 不使用普通重复规则排除。规则版本为 yuxi-r2-3，用于隔离模型缓存；已保存的旧问题需要重新解析才能重建。

## 替代方案

- 前端按文字隐藏：后端仍计为未决，自动入库状态与页面不一致。
- 删除所有页边内容、纯数字或 DOI：会丢失正文、脚注和文章元数据。
- 修改 OCR 服务：无法覆盖独立 PDF 原生证据形成的遗漏问题。

## 后果

保守的版面条件会保留缺少佐证的单页数字或独立 DOI，避免把元数据缺失伪装为核对完成。页边采用页面高度顶部、底部各 10%，原生行高至多 4%；跨页位置容差为页面宽高的 2%，DOI 与版权行的纵向距离至多为页高的 6%。位置差异较大或奇偶页镜像排列的页码需要明确的解析页眉页脚类型才能排除。截图不能提供真实坐标，实际 PDF 是否命中需在验证机确认。

重新解析生成新的正文与附属产物；已有人工校对的文件先用副本验证，避免以重新识别的正文替换人工修订。旧记录不在读取时隐式改写。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 有证据的页码、版权、页脚 DOI 不进入待确认 | 区域分类 | pdf_repair.py | test_pdf_repair.py 与真实 PDF 重新解析 | 移除出版区域判定后对应候选恢复为 missing_region | Not run：本机禁止运行测试 |
| 正文和文章元数据保持可核对 | 误排除 | pdf_repair.py | 同文件的正文、脚注、图表和独立 DOI 配对用例 | 对所有数字、DOI 或页边文字排除会使保留断言失败 | Not run：本机禁止运行测试 |
| 排除记录保留证据且不影响待核对状态 | 装配与状态 | pdf_review_service.py | service 用例与真实校对页面、附属 JSON 回读 | 排除记录被重新加入模型队列或丢失候选时失败 | Not run：本机禁止运行测试 |

独立 Reviewer 已静态审阅完整需求、diff、测试与规范，复核了完整引用、作者信息及明确脚注的保留边界。本机按用户约定未运行程序、测试、lint 或构建；git diff --check 是仅执行的格式静态检查，运行时结果仍为 Not run。

### 验证机步骤

在同步代码后的仓库根目录、现有 Compose 环境中，先运行最小回归，再运行根目录工程检查及完整后端单测：

```bash
docker compose exec api uv run --group test pytest test/unit/knowledge/test_pdf_repair.py test/unit/services/test_pdf_review_execution.py
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
docker compose exec api uv run --group test pytest test/unit -m "not slow"
git diff --check
```

文档构建使用 `pnpm --dir docs run build`。Windows 主机若只有 `python` 命令，工程检查的 `python3` 替换为 `python`。

开发 Compose 将 backend/package 同时挂载到 API 与 worker。待现有任务完成后执行 `docker compose restart api worker`，然后在测试知识库上传原 PDF 的副本，使用 OXR 重新解析；刷新旧产物不会触发规则重算。

1. 打开校对页，确认页码、版权和满足版面证据的页脚 DOI 不再以“疑似正文遗漏”进入待确认。
2. 在浏览器 Network 中查看该文档的 parse-assets 响应，确认附属产物 metadata.pdf_repair.rule_version 为 yuxi-r2-3，excluded_publication_elements 有对应统计；对应 reviews 的 kind 为 publication_element、classification 为 excluded，candidates.native 与 evidence 仍保留原文和位置。
3. 对照原 PDF 检查真实漏段、正文数字、参考文献 DOI、作者联系方式和 SI。真实问题仍应待核对，下载正文中的这些内容应与对应解析块一致。
4. 关闭并重开详情，确认计数和内容来自新产物。其他未决或技术失败仍可使整份文档显示待核对或复核未完成，不能要求全部问题清零。

若某条仍误报，记录其候选文字、bbox、page、邻页相应行和解析块类型，区分真实坐标未满足分类条件与旧规则未更新。
