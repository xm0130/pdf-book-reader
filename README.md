# pdf-book-reader

PDF 书籍逐页精读与知识提炼的 WorkBuddy Skill。把一本 PDF 逐页提取文字、分批送给
WorkBuddy 内置大模型逐页判断与抽取知识点，沉淀为结构化知识库，并按区间生成阶段性
摘要与全书综合总结，最终产出一份自包含、可打印的 HTML 精读报告。

**不需要任何外部大模型 API Key。** Python 只做确定性工作（取文、分批、合并、装订），
「逐页理解 + 抽知识点 + 写总结」全部由 WorkBuddy 内置模型完成。

## 特性

- 六步流水线：`inspect` → `extract` → 逐批精读 → `assemble` → 写总结 → `finalize`
- 断点续跑：批次文件与回填 JSON 全部持久化，随时用 `status` 查进度
- 空白页（目录、版权页、索引等）自动登记跳过，不消耗模型阅读
- 区间与批次大小可调，支持页码区间试读
- 默认只保留一份自包含 HTML 报告（样式内置，无外链资源）

## 安装

```bash
# 依赖：Python 3.11 + PyMuPDF
pip install -r requirements.txt

# 验证
python scripts/book_reader.py --version   # → book-reader 1.0.0
```

本 skill 安装于用户级目录 `~/.workbuddy/skills/pdf-book-reader/`，全项目可用。

## 快速开始

```bash
BR="scripts/book_reader.py"

# 1. 体检：确认 PDF 有文字层（覆盖率 < 40% 大概率是扫描版，需先 OCR）
python "$BR" inspect --pdf "book.pdf" --pages 1-30

# 2. 取文并分批
python "$BR" extract --pdf "book.pdf" --batch-size 6

# 3. 逐批 Read batches/*.md → Write knowledge/batch_*.json（模型工作）

# 4. 合并知识库与区间素材
python "$BR" assemble --out "book_analysis" --interval 20

# 5. 写 drafts/final.md（模型工作）

# 6. 装订产出（默认只保留 HTML；加 --keep-md 保留 Markdown）
python "$BR" finalize --out "book_analysis"
```

完整工作流、参数表与提示词规范见 [`SKILL.md`](SKILL.md)。

## 目录结构

```
pdf-book-reader/
├── SKILL.md                  # 使用说明与工作流（skill 核心定义）
├── README.md
├── requirements.txt          # 仅 pymupdf
├── scripts/
│   └── book_reader.py        # 主脚本（六个子命令，零 LLM 依赖、零网络）
├── references/
│   ├── PROMPTS.md            # 逐页抽取 / 区间总结 / 全书总结的提示词
│   ├── OUTPUT_SCHEMA.md      # knowledge/*.json、drafts/*.md 的格式规范
│   ├── PROJECT_ANALYSIS.md   # 原项目代码分析与移植对照
│   └── legacy/               # 原项目脚本存档（API Key 版，仅供参考不运行）
└── assets/                   # 预留（报告样式已内置在 book_reader.py 中）
```

## 限制

- 扫描版 / 图片版 PDF 无文字层，需先 OCR
- 长书成本：每批约 6~8k token 阅读 + 1~2k token 回填；350 页约 60 批
- 输出目录不要指向 skill 目录本身

## 许可与致谢

移植自 echohive 的开源项目 *AI-reads-books-page-by-page*（MIT），原脚本存档于
`references/legacy/`。本 skill 的 Python 内核为等价重写（去 API 化），原提示词
保留在 `references/PROMPTS.md`。
