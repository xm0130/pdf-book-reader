---
name: pdf-book-reader
description: "PDF 书籍逐页精读与知识提炼。把一本 PDF 逐页提取文字、分批送给 WorkBuddy 内置大模型逐页判断与抽取知识点，沉淀为结构化知识库，并按区间生成阶段性摘要与全书综合总结，最终产出一份自包含、可打印的 HTML 精读报告（默认只保留 HTML，不额外保留 md/json）。支持断点续跑、空白页跳过、区间与批次大小可调。不需要任何外部大模型 API Key。适用于：精读一本书/长报告/论文合集并产出学习笔记、构建书籍知识库、生成章节式摘要报告。"
---

# PDF Book Reader — PDF 书籍逐页精读（WorkBuddy 内置大模型版）

## 一句话定位

把原开源项目 *AI-reads-books-page-by-page*（echohive，MIT）的核心能力移植到 WorkBuddy：
**Python 只做确定性工作（取文、分批、合并、装订），「逐页理解 + 抽知识点 + 写总结」全部由
WorkBuddy 内置大模型完成**——因此本 skill 不需要、也不读取任何大模型 API Key。

## 适用场景与触发条件

| 触发词/场景 | 示例 |
| --- | --- |
| 逐页精读一本书 | "帮我把这本书逐页精读，整理知识点" |
| 构建书籍知识库 | "把这本 PDF 提炼成知识库 JSON" |
| 生成读书笔记/报告 | "读一遍这份白皮书，出一份分章节的总结报告" |
| 长文档分批消化 | "这本 300 页的书太长，分批读并每 30 页给我一个阶段总结" |

**不适用**：扫描版/图片版 PDF（无文字层，需先 OCR）；只需快速摘要几页的场景（直接用 Read 工具即可，不必启动本流水线）。

## 环境与依赖（Python 统一用 D:\anaconda3）

- 解释器：`D:\anaconda3\python.exe`（Python 3.11）
- pip：`D:\anaconda3\python.exe -m pip`（不要用裸 `pip`，避免装错环境）
- 唯一第三方依赖：**PyMuPDF**（本机已装 1.24.10）。缺失时安装：

```bash
"D:/anaconda3/python.exe" -m pip install pymupdf
# 或按 requirements.txt 安装：
"D:/anaconda3/python.exe" -m pip install -r "C:/Users/Tiffany/.workbuddy/skills/pdf-book-reader/requirements.txt"
```

主脚本：`C:\Users\Tiffany\.workbuddy\skills\pdf-book-reader\scripts\book_reader.py`
下文简写 `$BR`。运行示例统一用 bash 路径风格：

```bash
BR="C:/Users/Tiffany/.workbuddy/skills/pdf-book-reader/scripts/book_reader.py"
PY="D:/anaconda3/python.exe"
```

## Skill 目录结构

```
pdf-book-reader/
├── SKILL.md                      # 本文件：使用说明与工作流
├── requirements.txt              # 仅 pymupdf
├── scripts/
│   └── book_reader.py            # 主脚本（六个子命令，零 LLM 依赖、零网络）
├── references/
│   ├── PROMPTS.md                # 逐页抽取 / 区间总结 / 全书总结的提示词（沿用原项目）
│   ├── OUTPUT_SCHEMA.md          # knowledge/*.json、drafts/*.md 的格式规范
│   ├── PROJECT_ANALYSIS.md       # 原项目的完整代码分析与移植对照
│   └── legacy/                   # 原项目脚本存档（API Key 版，仅供参考不运行）
│       ├── read_books.py         #   原版（OpenAI gpt-4o-mini / o1-mini）
│       ├── read_books_agnes.py   #   Agnes API 改版（含断点续传，本 skill 的续传设计来源）
│       ├── read_books_deepseek.py#   DeepSeek API 改版
│       ├── README.md             #   原项目说明
│       ├── requirements.txt      #   原依赖（pydantic/openai/pymupdf/termcolor）
│       └── LICENCE               #   MIT
└── assets/                       # 预留（报告样式已内置在 book_reader.py 中）
```

## 核心设计：与原项目的关键差异

原项目 `read_books.py` 用 `OpenAI SDK` 逐页调用 `gpt-4o-mini` 抽知识点、用模型生成总结。
本 skill 把「模型调用」从 Python 里抽出来交给 WorkBuddy 自己，脚本只负责确定性部分：

| 环节 | 原项目 | 本 skill |
| --- | --- | --- |
| PDF 取文 | PyMuPDF（脚本内） | PyMuPDF（`extract` 子命令） |
| 逐页理解/抽知识点 | OpenAI API | **WorkBuddy 内置模型**（agent 逐批读 `batches/*.md`，回填 `knowledge/*.json`） |
| 结构化输出约束 | `beta.chat.completions.parse` / json_object | 固定 JSON schema + 脚本端校验（`assemble` 容错解析） |
| 阶段性总结 | API 每 N 页一次 | 模型读 `digests/interval_XXX.md` 后写 `drafts/interval_XXX.md` |
| 全书总结 | API | 模型写 `drafts/final.md` |
| 装订与报告 | 无（只存 md） | `finalize` 生成正式 md + HTML 报告 |
| 断点续传 | progress.json（Agnes 版） | 天然支持：批次文件与回填 JSON 均持久化，`status` 随时查进度 |
| API Key | 需要（环境变量） | **不需要** |

## 工作流（六步，严格按顺序）

设 `PDF` 为目标书籍绝对路径，`WD` 为工作目录（默认 `<PDF 所在目录>/book_analysis`，
**不要**放在 skill 目录里）。

### 第 1 步 · inspect：先体检，再动手

```bash
"$PY" "$BR" inspect --pdf "$PDF" --pages 1-60
```

- 看文字层覆盖率：**< 40% 大概率是扫描版**，告知用户需先 OCR，终止流程。
- 看 `预计批次数`：向用户预估工作量（每批一次模型阅读）。
- `--json` 可拿到机器可读结果。

### 第 2 步 · extract：取文并分批

```bash
"$PY" "$BR" extract --pdf "$PDF" --batch-size 6 --max-chars 14000
```

产出 `WD/batches/batch_NNN.md`（每批含处理指令头 + `===== PAGE n =====` 分隔的正文）。
空白页（< min-chars）自动登记跳过，不消耗模型阅读。

### 第 3 步 · 逐批精读（模型工作，本 skill 的核心）

对每个批次循环执行，直到 `status` 显示 100%：

1. **Read** `WD/batches/batch_001.md`（批次头里写明了本批页码与输出文件名）。
2. 按批次头要求逐页判断：目录/版权/索引/参考文献/空白 → `has_content: false`；
   正文/定义/论点/案例/结论/方法框架 → `has_content: true` 并抽知识点。
   完整判据沿用原项目提示词，见 `references/PROMPTS.md` 第 1 节。
3. **Write** `WD/knowledge/batch_001.json`，schema 见 `references/OUTPUT_SCHEMA.md`：

```json
{
  "batch": 1,
  "pages": [
    {"page": 3, "has_content": false, "points": []},
    {"page": 6, "has_content": true, "points": ["知识点句子1", "知识点句子2"]}
  ]
}
```

要求：`pages` 数组必须覆盖本批每一页、按页码升序；知识点写完整可独立理解的句子，
保留术语原文、关键引文与例子上下文；输出语言按批次头 `lang` 规则（默认随原文）。
4. 每完成若干批（建议 5~10 批）跑一次 `status` 检查，防止漏批。

### 第 4 步 · assemble：合并知识库与素材

```bash
"$PY" "$BR" assemble --out "$WD" --interval 20
```

- 生成 `knowledge_base/<slug>_knowledge.json`（扁平知识点 + 按页索引，原项目同名产物）。
- 生成 `digests/interval_XXX.md`（每 20 页一个区间的知识点汇编）与 `digests/index.md`。
- 有缺批/坏 JSON 会明确报出（退出码 2 = 有 schema 错误；1 类警告不影响继续）。

### 第 5 步 · 写总结草稿（模型工作）

- 逐份 **Read** `digests/interval_XXX.md` → **Write** `WD/drafts/interval_XXX.md`
  （区间编号与 digest 对应，正文用 Markdown，参考 `references/PROMPTS.md` 第 2 节的区间总结提示词）。
- 通读全部区间素材后 **Write** `WD/drafts/final.md`：全书综合总结，
  **纯 Markdown 正文，不要自己加一级标题**（装订时自动加），参考 `PROMPTS.md` 第 3 节。

### 第 6 步 · finalize：装订产出（默认只保留 HTML）

```bash
"$PY" "$BR" finalize --out "$WD"
```

**默认行为：只输出一份自包含 HTML 报告，同名家族的 `summaries/*.md` 与
`knowledge_base/*.json` 在生成后被自动清理**（用户已明确要求只保留 HTML）。
需要 Markdown 原件时加 `--keep-md`。

产出：

```
WD/
├── pages/pNNNN.txt                          # 每页原始文本（保留，供抽查溯源）
├── batches/batch_NNN.md + index.json        # 批次文件与索引（保留，可重跑）
├── knowledge/batch_NNN.json                 # 模型回填的逐页知识点（保留）
├── digests/interval_XXX.md + index.md       # 区间素材（保留，可重新总结）
├── drafts/final.md, interval_XXX.md         # 模型草稿（保留，可重新装订）
└── report/<slug>_report.html                # ★ 唯一交付物：自包含 HTML 精读报告
```

完成后用 `present_files` **只呈现 `report/<slug>_report.html`**，不要附带 md/json。

### 验证

```bash
"$PY" "$BR" status --out "$WD" --json
```

- `batches_missing: []`、`complete: true` → 全部批次已回填。
- `knowledge_points > 0` → 知识库非空。
- HTML 报告用浏览器/预览打开检查排版（样式内置于脚本，无外链资源）。
- 抽查：随机挑 2~3 页，对照 `WD/pages/pNNNN.txt` 与 `WD/knowledge/batch_NNN.json` 中该页的 `points` 是否对应。

## 参数说明

### extract

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--pdf` | 必填 | PDF 路径 |
| `--out` | `<pdf目录>/book_analysis` | 工作目录 |
| `--pages` | 全书 | 页码范围，如 `1-40,55,60-` |
| `--batch-size` | 6 | 每批页数（原项目每 API 调用 1 页；批量可减少轮次） |
| `--max-chars` | 14000 | 单批字符上限，超限自动切批；超长单页独占一批 |
| `--min-chars` | 40 | 低于此字符视为空白/图片页，登记跳过 |
| `--lang` | auto | 知识点输出语言：`auto`/`zh`/`en`，写入批次头 |
| `--force` | 关 | 覆盖已有批次文件（不动 knowledge/） |

### assemble

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--out` | 必填 | 工作目录 |
| `--interval` | 20 | 每 N 页一个区间（等价原项目 `ANALYSIS_INTERVAL`）；`0` 不切分 |

### inspect / status / finalize / clean

`inspect`：`--pdf --pages --batch-size --max-chars --json`；
`status`：`--out --json`；
`finalize`：`--out [--keep-md] [--json]`——**默认只留 HTML**，`--keep-md` 才保留 md/json；
`clean`：`--out --yes`（危险操作，必须确认）。

### 输出策略（重要）

用户偏好：**结果只保留 HTML，其余同名文件一律删除**。因此：

- `finalize` 默认清理 `summaries/*.md` 与 `knowledge_base/*.json`，只留 `report/*.html`；
- 交付时只 `present_files` 那一份 HTML；
- 中间产物（pages/batches/knowledge/digests/drafts）保留，随时可重新 `assemble`/`finalize` 重建，
  不向用户额外罗列；
- 若用户临时要 Markdown 原文，重跑 `finalize --keep-md` 即可再生。

## 调用示例

### 示例 1 · 试读 30 页

```bash
"$PY" "$BR" inspect  --pdf "D:/books/deep_work.pdf" --pages 1-30
"$PY" "$BR" extract  --pdf "D:/books/deep_work.pdf" --pages 1-30 --batch-size 6
# → 逐批 Read batches/*.md，Write knowledge/batch_*.json
"$PY" "$BR" assemble --out "D:/books/book_analysis" --interval 10
# → 写 drafts/final.md（试读可省区间摘要）
"$PY" "$BR" finalize --out "D:/books/book_analysis"
```

### 示例 2 · 整本精读（350 页 ≈ 60 批）

```bash
"$PY" "$BR" extract --pdf "D:/books/deep_work.pdf" --batch-size 6
# 循环回填 60 个批次，每 10 批跑一次 status
"$PY" "$BR" status   --out "D:/books/book_analysis"
"$PY" "$BR" assemble --out "D:/books/book_analysis" --interval 20
# → 写 18 份 drafts/interval_XXX.md + drafts/final.md
"$PY" "$BR" finalize --out "D:/books/book_analysis"
```

### 示例 3 · 中断续跑

extract/回填任意一步中断后，重跑同一命令即可：`extract` 幂等重建批次文件，
已存在的 `knowledge/batch_*.json` 不会丢失；`status` 会精确列出还缺哪些批次。

## 注意事项与限制

- **扫描版 PDF 无法直接处理**：inspect 发现文字层覆盖率 < 40% 时必须先 OCR。
- **长书成本预估**：每批约消耗 6~8k token 阅读 + 1~2k token 回填；350 页 ≈ 60 批。
  预算紧张时可 `--batch-size 8 --max-chars 16000` 减少轮次，但单批理解质量略降。
- **断点续跑**：任何时候中断都安全；`clean` 才会删除全部产物（需 `--yes`）。
- **不要并行写同一批**：每个批次文件只回填一次，避免覆盖。
- 输出路径不要指向 skill 目录本身；skill 目录是只读的。

## 安装（本机）

1. 已放置于 `C:\Users\Tiffany\.workbuddy\skills\pdf-book-reader\`（用户级，全项目可用）。
2. 依赖检查/安装：`"D:/anaconda3/python.exe" -m pip show pymupdf` 或按上文安装命令。
3. 验证安装：`"D:/anaconda3/python.exe" "$BR" --version` → `book-reader 1.0.0`；
   再对任意 PDF 跑一次 `inspect`。

## 许可与致谢

移植自 echohive 的开源项目 *AI-reads-books-page-by-page*（MIT），原脚本存档于
`references/legacy/`。本 skill 的 Python 内核为等价重写（去 API 化），原提示词原文保留在
`references/PROMPTS.md`。
