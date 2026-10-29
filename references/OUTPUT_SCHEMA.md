# OUTPUT_SCHEMA.md — 回填与草稿的格式规范

## 1. knowledge/batch_NNN.json（逐页知识点回填）

路径：`<工作目录>/knowledge/batch_NNN.json`，NNN 为三位批次号（与批次文件一一对应）。

```json
{
  "batch": 1,
  "pages": [
    { "page": 3, "has_content": false, "points": [] },
    { "page": 6, "has_content": true,
      "points": ["知识点句子1", "知识点句子2"] }
  ]
}
```

硬性约束（`assemble` 会校验，违规即退出码 2）：

1. `pages` 必须是数组，且**覆盖本批每一页**（含被判为无内容的页），按页码升序。
2. `page` 为整数，等于批次文件中 `===== PAGE n =====` 的物理页码。
3. `has_content` 为布尔；`false` 时 `points` 必须为空数组。
4. `points` 为字符串数组；兼容键名 `knowledge`（原项目风格）。
5. 文件必须是纯 JSON（UTF-8，无 BOM），不要包 ```json 代码围栏。
6. 缺失批次不会阻塞 assemble，但会在结果中列为 `batches_missing`（部分结果）。

## 2. drafts/interval_XXX.md（区间摘要草稿）

- 文件名：`interval_XXX.md`，XXX 与 `digests/interval_XXX.md` 的区间号一致。
- 内容：纯 Markdown 正文（`##` 起步），无一级标题、无元信息头（装订时自动加）。
- 覆盖该区间知识点的主要脉络；区间内多数页为空（如全是目录/版权页）时，
  允许写 2~3 句说明（装订时长于 30 字符即可通过）。

## 3. drafts/final.md（全书综合总结草稿）

- 内容：纯 Markdown 正文，**不要一级标题**（`# `），不要元信息头。
- 长度需 > 200 字符，否则 `finalize` 拒绝装订（防呆）。
- 部分精读时必须包含「覆盖范围」说明。

## 4. 装订产物（脚本自动生成，无需手写）

> **保留策略**：`finalize` 默认只保留 `report/<slug>_report.html`，以下 Markdown 与知识库 JSON
> 生成后即被清理（加 `--keep-md` 才保留）。中间产物 `drafts/`、`knowledge/` 不受影响，
> 需要时可重跑 `assemble` + `finalize` 再生。

- `summaries/<slug>_interval_XXX.md`：自动加元信息头（书名、区间、时间戳）与页脚。
- `summaries/<slug>_final_NNN.md`：NNN 自动递增，不覆盖旧版。
- `knowledge_base/<slug>_knowledge.json`：

```json
{
  "book": "xxx.pdf", "slug": "xxx", "pdf": "原始路径",
  "generated_at": "2026-10-29 22:03:35",
  "pages_selected": 12, "pages_returned": 9, "pages_with_content": 6,
  "knowledge_total": 18,
  "knowledge": ["……"],
  "by_page": { "6": ["……"] }
}
```

- `report/<slug>_report.html`：自包含 HTML（样式内联，无外链），含统计卡、目录、
  区间摘要与全书总结，可直接浏览器打开或打印。
