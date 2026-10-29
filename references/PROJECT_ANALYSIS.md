# PROJECT_ANALYSIS.md — 原项目完整代码分析与移植对照

> 分析对象：`F:\BaiduSyncdisk\克尔泰\编程\agent-skills包\AI-reads-books-page-by-page-main`
> （echohive《AI reads books: Page-by-Page PDF Knowledge Extractor & Summarizer》，MIT）

## 1. 项目文件清单

| 文件 | 作用 |
| --- | --- |
| `read_books.py` | 主脚本（OpenAI 官方 API：gpt-4o-mini 逐页抽取 + o1-mini 分析） |
| `read_books_agnes.py` | Agnes API 改版（OpenAI 兼容协议 + 断点续传 + 限流退避重试） |
| `read_books_deepseek.py` | DeepSeek API 改版（同上，去进度文件） |
| `requirements.txt` | pydantic / openai / pymupdf / termcolor |
| `book_analysis/` | 运行产物：pdfs/（书副本）、knowledge_bases/（知识 JSON）、summaries/（间隔与最终 md）、progress.json（续传进度） |
| `infinite_math.pdf` | 示例书（《马克思的神学隐喻》中译，354 页） |

## 2. 核心执行流程（read_books.py）

```
main()
 ├─ print_instructions() + input()          # 交互确认（Ctrl+C 退出）
 ├─ setup_directories()                     # 建目录 + 清空旧产出 + 复制 PDF 到 pdfs/
 ├─ OpenAI()                                # 读 OPENAI_API_KEY 环境变量
 ├─ load_existing_knowledge()               # 读 knowledge_bases/<name>_knowledge.json
 ├─ fitz.open(PDF_PATH) 逐页循环：
 │    ├─ page.get_text()                    # PyMuPDF 取本页文字
 │    ├─ process_page():
 │    │    ├─ beta.chat.completions.parse(  # 结构化输出（json_schema）
 │    │    │    model="gpt-4o-mini",
 │    │    │    system=逐页抽取提示词,        # SKIP/DO 判据，见 references/PROMPTS.md
 │    │    │    response_format=PageContent) # pydantic: has_content + knowledge[]
 │    │    └─ 每页处理完立即 save_knowledge_base()   # 持久化，断电不丢
 │    ├─ 每 ANALYSIS_INTERVAL 页：
 │    │    └─ analyze_knowledge_base() → save_summary(is_final=False)
 │    └─ 最后一页：analyze_knowledge_base() → save_summary(is_final=True)
 └─ 完成
```

## 3. 输入输出

**输入**：项目根目录的 PDF 文件（文件名写死在 `PDF_NAME` 常量）。

**输出**：
- `book_analysis/pdfs/<PDF_NAME>`：PDF 副本
- `book_analysis/knowledge_bases/<name>_knowledge.json`：`{"knowledge": [知识点字符串...]}`，每页处理后即时落盘
- `book_analysis/summaries/<name>_interval_NNN.md` / `<name>_final_NNN.md`：Markdown 总结（自动编号，带时间戳头）

## 4. 配置常量（脚本顶部写死）

| 常量 | 原值 | 含义 | 本 skill 对应 |
| --- | --- | --- | --- |
| `PDF_NAME` | meditations.pdf | 目标书 | `--pdf` 参数 |
| `BASE_DIR` 等 | book_analysis/* | 输出目录结构 | 同结构保留 |
| `ANALYSIS_INTERVAL` | 20 | 每 N 页一次阶段总结（None 关闭） | `assemble --interval` |
| `MODEL` | gpt-4o-mini | 逐页抽取模型 | WorkBuddy 内置模型 |
| `ANALYSIS_MODEL` | o1-mini | 总结模型 | WorkBuddy 内置模型 |
| `TEST_PAGES` | 60 | 只处理前 N 页（None 全书） | `extract --pages`（更强，任意区间） |

## 5. 三个版本差异

| | read_books.py | agnes 版 | deepseek 版 |
| --- | --- | --- | --- |
| 端点 | OpenAI 官方 | api.agnes-ai.cn/v1 | api.deepseek.com |
| 结构化输出 | beta.parse（json_schema） | json_object + pydantic 解析容错 | 同 agnes |
| 断点续传 | 无 | progress.json + 跳过已处理页 | 无 |
| 限流处理 | 无 | 指数退避重试（最多 6 次），耗尽后存进度停止 | 无 |
| 页间延迟 | 无 | 3 秒 | 无 |
| 交互确认 | 阻塞 input() | 仅 TTY 时确认 | 同 agnes |

agnes 版的**续传 + 退避重试 + 非交互友好**这三个工程改进被本 skill 继承
（批次/回填产物天然持久化 + 脚本纯非交互）。

## 6. 外部依赖

- PyMuPDF（fitz）：PDF 文本层提取
- OpenAI SDK：模型调用（本 skill 已移除）
- pydantic：响应结构校验（本 skill 改为脚本端手写校验，不再需要）
- termcolor：终端着色（本 skill 移除，避免 Windows GBK 控制台编码问题）

## 7. 已观察到的原实现问题（本 skill 的修复）

1. **setup_directories 每次清空旧产出** → 覆盖了它自己宣称的 resume 能力；本 skill 仅 `clean --yes` 才清空。
2. **交互式 input() 阻塞** → 自动化场景挂起；本 skill 纯非交互。
3. **Windows 控制台 GBK 下 emoji/termcolor 易报 UnicodeEncodeError** → 本 skill UTF-8 重配置 + 无 emoji 输出。
4. **逐页一次 API 调用，350 页 = 350 次调用** → 本 skill 批量化（每批 ~6 页），并显式给出 token 预估。
5. **TEST_PAGES 与全书页数不一致时** `is_final_page` 判断可能提前触发最终总结 → 本 skill 的最终总结与循环解耦（finalize 阶段独立执行）。
6. **无「有内容页/知识点数」等统计** → 本 skill 的 status/assemble 提供完整进度与统计（--json 可机器读取）。
