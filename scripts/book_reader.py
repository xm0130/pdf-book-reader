#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""book_reader.py — PDF 书籍「逐页精读」流水线的确定性内核。

设计要点
--------
本脚本**不调用任何大模型 API，也不需要 API Key**。它只承担不需要智能的部分：

  inspect   体检 PDF（页数、文字层覆盖率、是否扫描版、预估批次）
  extract   逐页取文 → 落盘为 pages/*.txt 与 batches/batch_NNN.md（供模型阅读）
  status    查看进度（哪些批次已提取、哪些批次已回填知识点、草稿是否齐全）
  assemble  把模型回填的 knowledge/batch_*.json 合并为知识库 + 分区间摘要素材
  finalize  把模型写的 drafts/*.md 装订成带元信息的正式 Markdown + HTML 报告
  clean     清空某个书籍工作目录下的生成物（需 --yes）

「读一页、判断要不要、抽知识点、写总结」这些需要理解力的步骤，由 WorkBuddy 的
内置大模型执行；脚本通过批次文件把文本递给它，再通过约定好的 JSON/Markdown 结构
把结果收回来。因此整体能力等价于原项目 read_books.py，但完全没有外部模型依赖。

用法示例
--------
  python book_reader.py inspect  --pdf book.pdf
  python book_reader.py extract  --pdf book.pdf --batch-size 6
  python book_reader.py status   --out <workdir>
  python book_reader.py assemble --out <workdir> --interval 20
  python book_reader.py finalize --out <workdir>
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TOOL_NAME = "book-reader"
TOOL_VERSION = "1.0.0"

DEFAULT_BATCH_SIZE = 6          # 每批页数（等价原项目「每次模型调用处理 1 页」的批量版）
DEFAULT_MAX_CHARS = 14000       # 单批字符上限，防止单次阅读过长
DEFAULT_MIN_CHARS = 40          # 低于此字符数视为空页/图片页，不必送模型
DEFAULT_INTERVAL = 20           # 等价原项目 ANALYSIS_INTERVAL
PAGE_SEP = "===== PAGE {n} ====="


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def setup_stdio() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 以免中文/符号报 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def need_fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit(
            "缺少 PyMuPDF。请先安装：\n"
            "  D:\\anaconda3\\python.exe -m pip install pymupdf\n"
        )
    return fitz


def slugify(name: str) -> str:
    s = re.sub(r"\.pdf$", "", name, flags=re.I)
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", s, flags=re.UNICODE).strip("_")
    return s or "book"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def sha1_of(path: Path, limit: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(limit)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def parse_pages(spec: Optional[str], total: int) -> List[int]:
    """解析 "1-40,55,60-" → [1..40, 55, 60..total]，返回 1-based 页码列表。"""
    if not spec:
        return list(range(1, total + 1))
    picked: List[int] = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?", part)
        if m and ("-" in part):
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else total
        elif part.isdigit():
            start = end = int(part)
        else:
            raise SystemExit(f"无法解析页码区间：{part!r}（示例：1-40,55,60-）")
        start = max(1, start)
        end = min(total, end)
        picked.extend(range(start, end + 1))
    return sorted(set(picked))


def workdir_of(pdf: Path, out: Optional[str]) -> Path:
    if out:
        return Path(out).expanduser().resolve()
    return pdf.parent / "book_analysis"


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------
def cmd_inspect(args: argparse.Namespace) -> int:
    fitz = need_fitz()
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.exists():
        raise SystemExit(f"找不到 PDF：{pdf}")

    doc = fitz.open(pdf)
    try:
        total = doc.page_count
        selected = parse_pages(args.pages, total)
        per_page: List[Dict[str, Any]] = []
        for p in selected:
            text = doc[p - 1].get_text() or ""
            per_page.append({"page": p, "chars": len(text.strip())})
        empty = [x["page"] for x in per_page if x["chars"] < DEFAULT_MIN_CHARS]
        chars = sum(x["chars"] for x in per_page)
        batches = plan_batches(per_page, args.batch_size, args.max_chars)
        meta = {
            "pdf": str(pdf),
            "file_mb": round(pdf.stat().st_size / 1048576, 2),
            "pages_total": total,
            "pages_selected": len(selected),
            "chars_selected": chars,
            "avg_chars_per_page": round(chars / max(1, len(per_page))),
            "empty_pages": len(empty),
            "text_layer_coverage": round(1 - len(empty) / max(1, len(per_page)), 3),
            "planned_batches": len(batches),
            "batch_size": args.batch_size,
            "max_chars": args.max_chars,
            "encrypted": bool(doc.is_encrypted),
            "title": (doc.metadata or {}).get("title") or "",
            "toc_entries": len(doc.get_toc() or []),
            "warning": "",
        }
        if meta["text_layer_coverage"] < 0.4 and len(per_page) > 5:
            meta["warning"] = (
                "文字层覆盖率过低，疑似扫描版/图片版 PDF。本工具依赖文字层，"
                "请先 OCR（如 ocrmypdf、ABBYY、Adobe）后再处理。"
            )
        if args.json:
            say(json.dumps(meta, ensure_ascii=False, indent=2))
        else:
            say(f"[inspect] {pdf.name}")
            say(f"  文件大小      : {meta['file_mb']} MB")
            say(f"  总页数        : {total}")
            say(f"  本次选取页数  : {meta['pages_selected']}")
            say(f"  有效字符数    : {chars:,}（均值 {meta['avg_chars_per_page']}/页）")
            say(f"  空白/图片页   : {len(empty)}   文字层覆盖率 {meta['text_layer_coverage']:.0%}")
            say(f"  预计批次数    : {len(batches)}（每批约 {args.batch_size} 页 / ≤{args.max_chars} 字符）")
            if meta["encrypted"]:
                say("  [warn] 该 PDF 已加密，若取文失败请先解密。")
            if meta["warning"]:
                say(f"  [warn] {meta['warning']}")
            if meta["toc_entries"]:
                say(f"  书签/目录项   : {meta['toc_entries']} 条")
    finally:
        doc.close()
    return 0


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------
def plan_batches(per_page: List[Dict[str, Any]], batch_size: int, max_chars: int) -> List[List[int]]:
    """按页数与字符数双约束切批；超长单页独占一批。"""
    batches: List[List[int]] = []
    cur: List[int] = []
    cur_chars = 0
    for item in per_page:
        page, chars = item["page"], item["chars"]
        too_long = chars > max_chars * 1.5
        if too_long:
            if cur:
                batches.append(cur)
                cur, cur_chars = [], 0
            batches.append([page])
            continue
        if cur and (len(cur) >= batch_size or cur_chars + chars > max_chars):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(page)
        cur_chars += chars
    if cur:
        batches.append(cur)
    return batches


BATCH_HEADER = """<!-- {tool}:batch
batch: {idx}/{total}
pages: {pages_human}
count: {count}
output_file: knowledge/batch_{idx:03d}.json
schema: references/OUTPUT_SCHEMA.md
-->

# 批次 {idx}/{total} · 第 {pages_human} 页

处理要求（逐页执行，不要合并判断）：
1. 逐页阅读下面的正文，判断该页是否含「可提取的教学内容」。
2. 目录页、章节列表、索引、版权页、出版信息、参考文献、致谢、全空页 → `has_content: false`，`points: []`。
3. 前言中解释重要概念的部分、正文内容、关键定义、重要论点/理论、示例与案例、重要结论、方法框架、批判性分析 → `has_content: true`，抽出可直接学习的知识点。
4. 知识点写成完整、可独立理解的句子（不要只写「介绍了 X」这种空话）；保留关键引文、术语原文与定义、例子所处的上下文。
5. 输出语言：{lang_rule}
6. 结果写入 `{out_file}`，严格符合 schema：
   `{{"batch": {idx}, "pages": [{{"page": {first_page}, "has_content": true, "points": ["知识点 1", "知识点 2"]}}]}}`
   本批共 {count} 页，`pages` 数组必须正好 {count} 项，按页码升序，页码用 PDF 物理页码（见每页分隔符）。
"""

LANG_RULE = {
    "auto": "与页面原文语言一致（原文是英文就写英文，是中文就写中文）",
    "zh": "简体中文（术语首次出现时括注原文）",
    "en": "English",
}


def cmd_extract(args: argparse.Namespace) -> int:
    fitz = need_fitz()
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.exists():
        raise SystemExit(f"找不到 PDF：{pdf}")

    wd = workdir_of(pdf, args.out)
    slug = slugify(pdf.name)
    pages_dir = wd / "pages"
    batches_dir = wd / "batches"
    for d in (pages_dir, batches_dir, wd / "knowledge", wd / "drafts", wd / "digests"):
        d.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf)
    try:
        total = doc.page_count
        selected = parse_pages(args.pages, total)

        per_page: List[Dict[str, Any]] = []
        for p in selected:
            txt = (doc[p - 1].get_text() or "").replace("\r\n", "\n").replace("\r", "\n")
            txt = re.sub(r"[ \t]+\n", "\n", txt)
            txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
            per_page.append({"page": p, "chars": len(txt), "text": txt})
            write_text(pages_dir / f"p{p:04d}.txt", txt)  # 逐页原文，供抽查与溯源

        usable = [x for x in per_page if x["chars"] >= args.min_chars]
        skipped = [x["page"] for x in per_page if x["chars"] < args.min_chars]

        if args.force:
            for old in batches_dir.glob("batch_*.md"):
                old.unlink()

        batches = plan_batches(usable, args.batch_size, args.max_chars)
        total_b = len(batches)

        index: List[Dict[str, Any]] = []
        for i, pages in enumerate(batches, 1):
            tmap = {x["page"]: x["text"] for x in usable}
            pages_human = f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0])
            body: List[str] = []
            body.append(
                BATCH_HEADER.format(
                    tool=TOOL_NAME,
                    idx=i,
                    total=total_b,
                    pages_human=pages_human,
                    count=len(pages),
                    lang_rule=LANG_RULE.get(args.lang, LANG_RULE["auto"]),
                    out_file=f"knowledge/batch_{i:03d}.json",
                    first_page=pages[0],
                )
            )
            for p in pages:
                body.append("\n" + PAGE_SEP.format(n=p) + "\n")
                body.append(tmap[p])
            batch_file = batches_dir / f"batch_{i:03d}.md"
            write_text(batch_file, "\n".join(body).rstrip() + "\n")
            index.append(
                {
                    "batch": i,
                    "file": f"batches/batch_{i:03d}.md",
                    "pages": pages,
                    "chars": sum(tmap[p] and len(tmap[p]) or 0 for p in pages),
                }
            )

        meta = {
            "tool": f"{TOOL_NAME} {TOOL_VERSION}",
            "book_file": pdf.name,
            "slug": slug,
            "pdf": str(pdf),
            "sha1": sha1_of(pdf),
            "pages_total": total,
            "pages_selected": len(selected),
            "pages_usable": len(usable),
            "pages_skipped_empty": skipped,
            "batch_size": args.batch_size,
            "max_chars": args.max_chars,
            "min_chars": args.min_chars,
            "lang": args.lang,
            "batch_count": total_b,
            "created_at": now(),
        }
        write_json(wd / "meta.json", meta)
        write_json(wd / "batches" / "index.json", {"batches": index})

        # 隔离与新股次页码不匹配的旧回填文件，防止旧结果被错配到新批次
        stale_dir = wd / "knowledge" / "_stale"
        quarantined = 0
        for old_kb in (wd / "knowledge").glob("batch_*.json"):
            data = read_json(old_kb)
            ok = any(kb_matches(data, b["pages"]) for b in index)
            if not ok:
                stale_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_kb), str(stale_dir / old_kb.name))
                quarantined += 1

        if args.json:
            say(json.dumps({**meta, "batches": index}, ensure_ascii=False, indent=2))
        else:
            say(f"[extract] 工作目录：{wd}")
            say(f"  选取 {len(selected)} 页，其中 {len(usable)} 页有文字层，{len(skipped)} 页空白/图片页已登记跳过")
            say(f"  生成 {total_b} 个批次文件 → {batches_dir}")
            if skipped:
                preview = ", ".join(str(x) for x in skipped[:15])
                more = "" if len(skipped) <= 15 else f" …共 {len(skipped)} 页"
                say(f"  跳过页：{preview}{more}")
            if quarantined:
                say(f"  [warn] 检测到 {quarantined} 个旧回填文件与新批次页码不符，已移入 knowledge/_stale/（不丢失，可人工核对）")
            say("  下一步：逐个读取批次文件，按 SKILL.md 第 3 步回填 knowledge/batch_NNN.json")
    finally:
        doc.close()
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def kb_pages_of(data: Any) -> Optional[List[int]]:
    """从回填 JSON 中取出页码列表（结构不合法返回 None）。"""
    if not isinstance(data, dict):
        return None
    pages = data.get("pages")
    if not isinstance(pages, list):
        return None
    out: List[int] = []
    for it in pages:
        try:
            out.append(int(it.get("page")))
        except Exception:
            return None
    return out


def kb_matches(data: Any, pages: List[int]) -> bool:
    return kb_pages_of(data) == list(pages)


def collect_progress(wd: Path) -> Dict[str, Any]:
    meta = read_json(wd / "meta.json", {}) or {}
    idx = (read_json(wd / "batches" / "index.json", {}) or {}).get("batches", [])
    done_batches, missing, stale = [], [], []
    points = 0
    pages_done = 0
    for b in idx:
        kb = wd / "knowledge" / f"batch_{b['batch']:03d}.json"
        if not kb.exists():
            missing.append(b["batch"])
            continue
        data = read_json(kb, {}) or {}
        if not kb_matches(data, b["pages"]):
            stale.append(b["batch"])
            missing.append(b["batch"])
            continue
        done_batches.append(b["batch"])
        for it in data.get("pages") or []:
            pts = it.get("points") or it.get("knowledge") or []
            points += len(pts) if isinstance(pts, list) else 0
        pages_done += len(data.get("pages") or [])
    drafts = sorted((wd / "drafts").glob("*.md")) if (wd / "drafts").exists() else []
    summaries = sorted((wd / "summaries").glob("*.md")) if (wd / "summaries").exists() else []
    return {
        "workdir": str(wd),
        "book": meta.get("book_file", ""),
        "slug": meta.get("slug", ""),
        "pages_selected": meta.get("pages_selected", 0),
        "pages_total": meta.get("pages_total", 0),
        "batches_total": len(idx),
        "batches_done": len(done_batches),
        "batches_missing": missing,
        "batches_stale": stale,
        "pages_returned": pages_done,
        "knowledge_points": points,
        "drafts": [d.name for d in drafts],
        "summaries": [s.name for s in summaries],
        "percent": round(100 * len(done_batches) / max(1, len(idx))),
        "complete": bool(idx) and not missing,
    }


def cmd_status(args: argparse.Namespace) -> int:
    wd = Path(args.out).expanduser().resolve()
    if not (wd / "meta.json").exists():
        raise SystemExit(f"{wd} 不是有效的工作目录（缺 meta.json），请先运行 extract。")
    st = collect_progress(wd)
    if args.json:
        say(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    say(f"[status] {st['book']}  ({wd})")
    say(f"  批次进度      : {st['batches_done']}/{st['batches_total']}（{st['percent']}%）")
    if st["batches_missing"]:
        miss = ", ".join(str(x) for x in st["batches_missing"][:20])
        more = "" if len(st["batches_missing"]) <= 20 else f" …共 {len(st['batches_missing'])} 批"
        say(f"  待回填批次    : {miss}{more}")
    say(f"  已回填页数    : {st['pages_returned']} / {st['pages_selected']}")
    say(f"  知识点总数    : {st['knowledge_points']}")
    say(f"  草稿文件      : {', '.join(st['drafts']) if st['drafts'] else '（无）'}")
    say(f"  已装订总结    : {len(st['summaries'])} 份")
    if st["batches_stale"]:
        say(f"  [warn] {len(st['batches_stale'])} 个回填文件与本批页码不匹配（已隔离到 knowledge/_stale/ 或待 assemble 处理）")
    if st["complete"]:
        say("  → 所有批次已回填，可执行 assemble")
    else:
        say("  → 还有批次未回填，继续按 SKILL.md 第 3 步处理")
    return 0


# --------------------------------------------------------------------------
# assemble
# --------------------------------------------------------------------------
def cmd_assemble(args: argparse.Namespace) -> int:
    wd = Path(args.out).expanduser().resolve()
    meta = read_json(wd / "meta.json")
    if not meta:
        raise SystemExit(f"{wd} 不是有效的工作目录（缺 meta.json），请先运行 extract。")
    idx = (read_json(wd / "batches" / "index.json", {}) or {}).get("batches", [])
    if not idx:
        raise SystemExit("batches/index.json 为空，请先运行 extract。")

    slug = meta["slug"]
    book = meta["book_file"]

    items: List[Dict[str, Any]] = []
    missing: List[int] = []
    bad: List[str] = []
    for b in idx:
        path = wd / "knowledge" / f"batch_{b['batch']:03d}.json"
        data = read_json(path)
        if data is None:
            missing.append(b["batch"])
            continue
        if not kb_matches(data, b["pages"]):
            missing.append(b["batch"])
            bad.append(
                f"batch_{b['batch']:03d}.json 的页码与本批次 {b['pages']} 不匹配"
                "（可能由页码范围变更导致），已按缺失处理；如需保留可移入 knowledge/_stale/"
            )
            continue
        pages = data.get("pages")
        if not isinstance(pages, list):
            bad.append(f"batch_{b['batch']:03d}.json 缺少 pages 数组")
            continue
        for it in pages:
            try:
                page = int(it.get("page"))
            except Exception:
                bad.append(f"batch_{b['batch']:03d}.json 存在无法识别的 page 字段")
                continue
            pts = it.get("points", it.get("knowledge", []))
            if not isinstance(pts, list):
                pts = []
            pts = [str(x).strip() for x in pts if str(x).strip()]
            has = bool(it.get("has_content", bool(pts)))
            items.append({"page": page, "has_content": has and bool(pts), "points": pts if has else []})

    items.sort(key=lambda x: x["page"])
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for it in items:
        if it["page"] in seen:
            continue
        seen.add(it["page"])
        uniq.append(it)
    items = uniq

    flat = [p for it in items for p in it["points"]]
    by_page = {str(it["page"]): it["points"] for it in items}
    content_pages = sum(1 for it in items if it["points"])

    kb = {
        "book": book,
        "slug": slug,
        "pdf": meta.get("pdf", ""),
        "generated_at": now(),
        "pages_selected": meta.get("pages_selected"),
        "pages_returned": len(items),
        "pages_with_content": content_pages,
        "knowledge_total": len(flat),
        "knowledge": flat,
        "by_page": by_page,
    }
    kb_path = wd / "knowledge_base" / f"{slug}_knowledge.json"
    write_json(kb_path, kb)

    # 分区间摘要素材（纯拼接，不含模型调用）
    interval = int(args.interval or 0)
    digests: List[Path] = []
    dig_dir = wd / "digests"
    dig_dir.mkdir(parents=True, exist_ok=True)
    for old in dig_dir.glob("*.md"):
        old.unlink()

    if interval > 0 and items:
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for it in items:
            g = (it["page"] - 1) // interval + 1
            groups.setdefault(g, []).append(it)
        for g in sorted(groups):
            rows = groups[g]
            lo, hi = rows[0]["page"], rows[-1]["page"]
            lines = [f"# 第 {g} 区间（第 {lo}-{hi} 页）", ""]
            lines.append(f"本区间共 {len(rows)} 页，知识点 {sum(len(r['points']) for r in rows)} 条。")
            lines.append("")
            for r in rows:
                if not r["points"]:
                    continue
                lines.append(f"## p{r['page']}")
                for p in r["points"]:
                    lines.append(f"- {p}")
                lines.append("")
            p = dig_dir / f"interval_{g:03d}.md"
            write_text(p, "\n".join(lines).rstrip() + "\n")
            digests.append(p)

    index_lines = [f"# {book} · 分区间素材索引", "",
                   f"- 页码范围：{meta.get('pages_selected')} 页（实际回填 {len(items)} 页）",
                   f"- 有内容页：{content_pages} 页；知识点合计 {len(flat)} 条",
                   f"- 区间长度：{'每 ' + str(interval) + ' 页' if interval else '未启用'}",
                   f"- 生成时间：{kb['generated_at']}", ""]
    if digests:
        index_lines += ["| 区间 | 文件 | 页数 | 知识点 |", "| --- | --- | --- | --- |"]
        for p in digests:
            g = int(re.search(r"(\d+)", p.stem).group(1))
            rows = [it for it in items if (it["page"] - 1) // interval + 1 == g]
            index_lines.append(f"| {g} | `{p.name}` | {len(rows)} | {sum(len(r['points']) for r in rows)} |")
    else:
        index_lines.append("未启用区间切分，请直接读 `knowledge_base/*_knowledge.json` 的 `knowledge` 数组。")
    write_text(dig_dir / "index.md", "\n".join(index_lines) + "\n")

    result = {
        "knowledge_base": str(kb_path),
        "knowledge_total": len(flat),
        "pages_returned": len(items),
        "pages_with_content": content_pages,
        "digests": [str(p) for p in digests],
        "batches_missing": missing,
        "schema_errors": bad,
    }
    if args.json:
        say(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        say(f"[assemble] 知识库 → {kb_path}")
        say(f"  回填页数 {len(items)}；有内容页 {content_pages}；知识点 {len(flat)} 条")
        if digests:
            say(f"  区间素材 {len(digests)} 份 → {dig_dir}")
            say(f"  索引：{dig_dir / 'index.md'}")
        if missing:
            say(f"  [warn] {len(missing)} 个批次缺回填：{', '.join(str(x) for x in missing[:20])}")
            say("         当前知识库仅基于已回填批次，属部分结果。")
        if bad:
            for m in bad:
                say(f"  [error] {m}")
        say("  下一步：按 SKILL.md 第 4 步读取素材，把总结写到 drafts/ 下")
    return 0 if not bad else 2


# --------------------------------------------------------------------------
# finalize：装订 Markdown + 生成 HTML 报告
# --------------------------------------------------------------------------
def md_to_html(md: str) -> str:
    """极简 Markdown → HTML（标题/列表/引用/代码块/分割线/行内样式）。"""

    def inline(s: str) -> str:
        s = html.escape(s, quote=False)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*\*([^*]+)\*\*\*", r"<strong><em>\1</em></strong>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        return s

    out: List[str] = []
    lines = md.splitlines()
    i = 0
    in_code = False
    list_tag: Optional[str] = None
    para: List[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + inline(" ".join(para).strip()) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                flush_para(); close_list()
                out.append("<pre><code>")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(html.escape(line, quote=False))
            i += 1
            continue

        if not stripped:
            flush_para(); close_list()
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(); close_list()
            lvl = len(m.group(1))
            anchor = "s" + hashlib.md5(stripped.encode("utf-8")).hexdigest()[:8]
            out.append(f'<h{lvl} id="{anchor}">{inline(m.group(2))}</h{lvl}>')
            i += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para(); close_list()
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith(">"):
            flush_para(); close_list()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + inline(" ".join(buf)) + "</blockquote>")
            continue

        # 表格：当前行以 | 开头，且下一行是 | --- | --- | 形式的分隔行
        if stripped.startswith("|") and i + 1 < len(lines) and re.fullmatch(
            r"\|[\s:|\-]+\|", lines[i + 1].strip()
        ):
            flush_para(); close_list()
            split_row = lambda row: [c.strip() for c in row.strip().strip("|").split("|")]
            header = split_row(stripped)
            i += 2
            body: List[List[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(split_row(lines[i].strip()))
                i += 1
            parts = ["<table><thead><tr>"]
            parts += [f"<th>{inline(c)}</th>" for c in header]
            parts.append("</tr></thead><tbody>")
            for row in body:
                parts.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
            parts.append("</tbody></table>")
            out.append("".join(parts))
            continue

        m = re.match(r"^([-*+]|\d+\.)\s+(.*)$", stripped)
        if m:
            flush_para()
            tag = "ul" if m.group(1) in "-*+" else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{inline(m.group(2))}</li>")
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush_para(); close_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


REPORT_CSS = """
:root{--bg:#fbfbfa;--card:#ffffff;--ink:#1f2328;--muted:#5c6672;--line:#e3e6ea;--accent:#8a5a2b;--accent2:#0f6f6c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Georgia","Songti SC","Microsoft YaHei",serif;line-height:1.78;font-size:16px}
.wrap{max-width:920px;margin:0 auto;padding:56px 28px 96px}
h1{font-size:30px;line-height:1.35;margin:0 0 6px;letter-spacing:.02em}
.sub{color:var(--muted);font-size:14px;margin-bottom:28px;font-family:system-ui,"Microsoft YaHei",sans-serif}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:26px 0 34px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat b{display:block;font-size:22px;font-family:system-ui,sans-serif;color:var(--accent)}
.stat span{font-size:12px;color:var(--muted);font-family:system-ui,sans-serif;letter-spacing:.06em}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:26px 30px;margin:0 0 22px}
h2{font-size:21px;margin:26px 0 10px;padding-bottom:8px;border-bottom:2px solid var(--line)}
section > h2:first-child{margin-top:0}
h3{font-size:17px;margin:20px 0 8px;color:var(--accent2)}
h4{font-size:15px;margin:16px 0 6px;color:var(--muted)}
p{margin:10px 0}
ul,ol{margin:10px 0 10px 22px;padding:0}
li{margin:5px 0}
blockquote{margin:14px 0;padding:10px 16px;border-left:3px solid var(--accent);background:#faf6f1;color:#4a3f34;border-radius:0 8px 8px 0}
code{background:#f1f3f5;padding:1px 5px;border-radius:4px;font-size:.9em;font-family:"Cascadia Mono",Consolas,monospace}
pre{background:#f6f7f8;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto}
pre code{background:none;padding:0}
hr{border:0;border-top:1px solid var(--line);margin:24px 0}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14.5px;font-family:system-ui,"Microsoft YaHei",sans-serif}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;line-height:1.6}
thead th{background:#f6f4f1;font-weight:600;color:#3a3a34;white-space:nowrap}
tbody tr:nth-child(even){background:#fcfcfb}
.toc{font-family:system-ui,"Microsoft YaHei",sans-serif;font-size:14px}
.toc a{display:block;color:var(--ink);text-decoration:none;padding:5px 0;border-bottom:1px dashed var(--line)}
.toc a:hover{color:var(--accent)}
.toc small{color:var(--muted);margin-left:8px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:40px;font-family:system-ui,sans-serif}
@media print{body{background:#fff}section{border:none;padding:0;page-break-inside:avoid}}
"""


def build_report(book: str, meta: Dict[str, Any], final_md: str, intervals: List[Tuple[str, str]],
                 stats: Dict[str, Any]) -> str:
    toc = []
    for title, body in intervals:
        anchor = "s" + hashlib.md5(("# " + title).encode("utf-8")).hexdigest()[:8]
        toc.append(f'<a href="#{anchor}">{html.escape(title)}</a>')
    toc.append(f'<a href="#final">全书综合总结<small>final</small></a>')
    toc_html = "".join(toc)

    secs = []
    for title, body in intervals:
        secs.append(f"<section>{md_to_html('# ' + title + chr(10) + body)}</section>")
    secs.append(f'<section id="final">{md_to_html(final_md)}</section>')

    statc = "".join(
        f'<div class="stat"><b>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>'
        for k, v in stats.items()
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(book)} · 逐页精读报告</title>
<style>{REPORT_CSS}</style></head>
<body><div class="wrap">
<h1>{html.escape(book)}</h1>
<div class="sub">逐页精读报告 · 由 WorkBuddy 内置大模型生成 · {now()}</div>
<div class="stats">{statc}</div>
<section class="toc"><h2>目录</h2>{toc_html}</section>
{''.join(secs)}
<footer>逐页精读报告 · 生成工具：{TOOL_NAME} {TOOL_VERSION} · WorkBuddy 内置大模型</footer>
</div></body></html>
"""


def cmd_finalize(args: argparse.Namespace) -> int:
    wd = Path(args.out).expanduser().resolve()
    meta = read_json(wd / "meta.json")
    if not meta:
        raise SystemExit(f"{wd} 不是有效的工作目录（缺 meta.json）。")
    slug, book = meta["slug"], meta["book_file"]
    drafts_dir = wd / "drafts"
    if not drafts_dir.exists():
        raise SystemExit(f"缺少 drafts 目录：{drafts_dir}（先写总结草稿）")

    final_draft = drafts_dir / "final.md"
    if not final_draft.exists():
        raise SystemExit(
            f"缺少 {final_draft}\n"
            "请先按 SKILL.md 第 4 步把全书综合总结写入 drafts/final.md（纯 Markdown 正文，不要加一级标题）。"
        )
    final_md = final_draft.read_text(encoding="utf-8").strip()
    if len(final_md) < 200:
        raise SystemExit(f"{final_draft} 内容过短（{len(final_md)} 字符），疑似未写完，已中止装订。")

    sum_dir = wd / "summaries"
    sum_dir.mkdir(parents=True, exist_ok=True)

    # 区间草稿 → 正式命名
    interval_drafts = sorted(
        p for p in drafts_dir.glob("interval_*.md")
        if re.fullmatch(r"interval_\d+\.md", p.name)
    )
    intervals: List[Tuple[str, str]] = []
    written: List[Path] = []
    for p in interval_drafts:
        g = int(re.search(r"(\d+)", p.stem).group(1))
        body = p.read_text(encoding="utf-8").strip()
        if len(body) < 30:
            say(f"  [skip] {p.name} 内容过短（{len(body)} 字符），视为占位文件跳过")
            continue
        title = f"区间 {g} 摘要"
        target = sum_dir / f"{slug}_interval_{g:03d}.md"
        header = (f"# Book Analysis: {book}\n"
                  f"区间：{g}　Generated on: {now()}\n\n")
        write_text(target, header + body + "\n\n---\n*Analysis generated by WorkBuddy built-in LLM · book-reader 1.0*\n")
        intervals.append((title, body))
        written.append(target)

    # 最终总结
    existing = list(sum_dir.glob(f"{slug}_final_*.md"))
    n = len(existing) + 1
    final_path = sum_dir / f"{slug}_final_{n:03d}.md"
    header = (f"# Book Analysis: {book}\n"
              f"Generated on: {now()}\n"
              f"来源：{meta.get('pages_selected')} 页逐页精读\n\n")
    write_text(final_path, header + final_md + "\n\n---\n*Analysis generated by WorkBuddy built-in LLM · book-reader 1.0*\n")

    kb = read_json(wd / "knowledge_base" / f"{slug}_knowledge.json", {}) or {}
    # 知识库 JSON 可能已被上一次 finalize 清理，故优先从 knowledge/batch_*.json 直接重算统计，
    # 保证重复装订（改草稿后重跑）时统计不丢。
    pages_done = content_pages = points_total = 0
    for f in sorted((wd / "knowledge").glob("batch_*.json")):
        data = read_json(f, {}) or {}
        items = data.get("pages") or []
        if not isinstance(items, list):
            continue
        pages_done += len(items)
        for it in items:
            pts = it.get("points") or []
            if isinstance(pts, list) and pts:
                points_total += len(pts)
                if it.get("has_content"):
                    content_pages += 1
    stats = {
        "页数": kb.get("pages_returned") or pages_done or meta.get("pages_selected", "-"),
        "有内容页": content_pages or kb.get("pages_with_content", "-"),
        "知识点": points_total or kb.get("knowledge_total", "-"),
        "区间摘要": len(intervals),
    }
    html_path = wd / "report" / f"{slug}_report.html"
    write_text(html_path, build_report(book, meta, final_md, intervals, stats))

    # 默认只保留 HTML：删除同名家族（summaries/*.md 与 knowledge_base/*.json）
    # drafts/ 与 batches/ 等中间产物不受影响，随时可重新 finalize。
    removed: List[str] = []
    if not args.keep_md:
        for p in sorted(sum_dir.glob(f"{slug}_*.md")):
            p.unlink()
            removed.append(f"summaries/{p.name}")
        kb_file = wd / "knowledge_base" / f"{slug}_knowledge.json"
        if kb_file.exists():
            kb_file.unlink()
            removed.append(f"knowledge_base/{kb_file.name}")

    result = {
        "final": str(final_path),
        "intervals": [str(p) for p in written],
        "report_html": str(html_path),
        "stats": stats,
        "removed": removed,
        "kept": "仅 HTML 报告（drafts/ 与 batches/ 保留以便重跑）" if removed else "HTML + Markdown 全套",
    }
    if args.json:
        say(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not args.keep_md:
            say(f"[finalize] 仅保留 HTML → {html_path}")
            say(f"  已按同名清理 {len(removed)} 个文件（summaries/*.md、knowledge_base/*.json）")
            say("  中间产物 drafts/ batches/ knowledge/ 仍在，可再次 finalize 重建")
        else:
            say(f"[finalize] 最终总结 → {final_path}")
            if written:
                say(f"  区间摘要 {len(written)} 份 → {sum_dir}")
            say(f"  HTML 报告 → {html_path}")
        say(f"  统计：{stats}")
    return 0


# --------------------------------------------------------------------------
# clean
# --------------------------------------------------------------------------
def cmd_clean(args: argparse.Namespace) -> int:
    wd = Path(args.out).expanduser().resolve()
    if not wd.exists():
        say(f"[clean] 目录不存在：{wd}")
        return 0
    if not args.yes:
        say(f"[clean] 将删除以下子目录中的生成物（保留 meta.json 与最后提示）：\n  {wd}")
        say("        确认请加 --yes")
        return 1
    targets = ["pages", "batches", "knowledge", "digests", "drafts", "knowledge_base", "summaries", "report"]
    removed = []
    for name in targets:
        d = wd / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(name)
    for f in ("meta.json",):
        p = wd / f
        if p.exists():
            p.unlink()
            removed.append(f)
    say(f"[clean] 已移除：{', '.join(removed) if removed else '（无内容）'}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="book_reader.py",
        description="PDF 书籍逐页精读流水线（确定性内核，不含任何大模型调用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="体检 PDF：页数、文字层、是否扫描版、预估批次")
    p.add_argument("--pdf", required=True)
    p.add_argument("--pages", default=None, help="页码范围，如 1-40,55,60-（默认全书）")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("extract", help="逐页取文并生成批次文件")
    p.add_argument("--pdf", required=True)
    p.add_argument("--out", default=None, help="工作目录（默认 <pdf 所在目录>/book_analysis）")
    p.add_argument("--pages", default=None, help="页码范围，如 1-40,55,60-（默认全书）")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS)
    p.add_argument("--lang", choices=["auto", "zh", "en"], default="auto")
    p.add_argument("--force", action="store_true", help="覆盖已有批次文件")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("status", help="查看批次回填与草稿进度")
    p.add_argument("--out", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("assemble", help="合并知识点、生成知识库与分区间素材")
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="每 N 页一个区间；0 表示不切分")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("finalize", help="装订草稿并生成 HTML 报告（默认只保留 HTML）")
    p.add_argument("--out", required=True)
    p.add_argument("--keep-md", action="store_true",
                   help="同时保留 summaries/*.md 与 knowledge_base/*.json（默认删除，只留 HTML）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("clean", help="清空工作目录下的生成物")
    p.add_argument("--out", required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_clean)
    return ap


def main(argv: Optional[Iterable[str]] = None) -> int:
    setup_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
