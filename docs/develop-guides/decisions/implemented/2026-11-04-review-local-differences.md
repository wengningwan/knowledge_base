# 校对页长文本保留局部差异

状态：implemented
类型：bug-fix
Owner：web/src/utils/documentReview.js

## 问题

校对对照的中间文本超过矩阵阈值时整段标黄，共同正文也被当作差异，用户需要重新逐字查找真正变化。

部分取代[文档校对问题工作区](2026-11-03-document-review-workspace.md)的长文本整体突出策略。

## 决策

### 实现方案

`diffReviewText` 保留 Unicode 码点比较、共同前后缀剥离及小文本 LCS；长文本通过双向 Myers 搜索拆分，再比较子区间，避免分配完整二维矩阵。无共同字符时直接输出删除和新增。`DocumentReviewPanel.vue` 仅渲染 delete/insert 为高亮，差异函数只返回 parts。共同上下文可缩略，全文可展开。后端、保存行为及样式不变。

## 替代方案

- 提高矩阵阈值：仍会在更长文本整段高亮，且增加平方级内存占用。
- 对全部文本使用完整矩阵：保留精确差异，但长文本内存不可接受。
- 引入第三方 diff 依赖：当前只有局部 consumer，采用现有实现加线性内存拆分即可。

## 后果

长段落中的共同正文保留普通文字展示，新增、删除与替换标在实际位置。双向搜索增加算法分支，变化密集时仍有 CPU 成本；搜索前沿采用线性内存，小文本矩阵保持原有上限。最短编辑路径可能存在多个同长度答案，重复文字的具体对齐位置可以不同。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 长段多处改动只标实际增删 | 旧阈值使内部共同文字标黄 | documentReview.js | `node --test web/test/unit/document_review.test.js` | 恢复 coarse 分支后高亮片段断言失败 | Not run：本机禁止测试 |
| 真实模板只高亮局部差异 | utility 正确但模板仍标整段 | DocumentReviewPanel.vue | `node --test web/test/unit/document_review_runtime.test.js` | 长段候选 mark 只能包含修改字符；共同文字留在 span | Not run：本机禁止测试 |
| 完整保留两侧文本及 Unicode | 拆分丢字或破坏代理对 | documentReview.js | 同一 unit 文件的重建与独立小矩阵 oracle | 空文本、重复字符、换行、完全不同长文 | Not run：本机禁止测试 |

独立 LCS oracle 按行计算最少增删数，覆盖奇偶长度差、重复字符、长短不等输入、仅一个共同 Unicode 字符及固定种子的密集变化。现有 Web workflow（`.github/workflows/web.yml`）通过 `pnpm run test:unit` 选择这两个测试文件。

源码装配路径与测试断言已只读检查（Inspected）。`git diff --check` 已执行且无输出（Passed）。本机按项目约定不启动程序，不执行测试、构建和验证脚本；上述 unit、lint、build、工程 gate、文档构建及真实页面验证均为 Not run，需在另一台电脑执行。未获得运行结果，不能声明产品行为已通过。
