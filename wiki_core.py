"""Kira Wiki · 服务层

把「知识库读写、索引、图、检索、体检」从 main.py 里抽出来，好处有三：
  1. **非阻塞**：所有磁盘操作都在 asyncio.to_thread 里跑，不在事件循环上做同步 I/O；
  2. **路径安全**：页面读写一律经过 resolve_rel()，杜绝 `../` 穿越到知识库之外；
  3. **可测**：纯函数式接口，不依赖插件实例，方便单测。

目录约定（Obsidian vault）::

    <kw>/
      raw/            素材（用户放入，LLM 只读）
        assets/
      wiki/           LLM 生成的知识库
        index.md  log.md  overview.md  conventions.md
        sources/ entities/ concepts/ analyses/

3.0 兼容：本模块不导入任何框架内部路径，纯标准库。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

SECTIONS = ("sources", "entities", "concepts", "analyses")
SECTION_LABEL = {"sources": "来源", "entities": "实体", "concepts": "概念", "analyses": "分析"}
RAW_EXTS = {".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}
ROOT_FILES = ("index", "log", "overview", "conventions")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
MAX_PAGE_BYTES = 512 * 1024          # 单页写入上限（512 KiB）


async def to_thread(fn, *args, **kwargs):
    """统一的线程池入口（保持调用点简洁）。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

class WikiPathError(ValueError):
    """非法路径（穿越/绝对路径/越界）。"""


def resolve_rel(root: Path, rel: str, *, must_exist: bool = False) -> Path:
    """把相对路径解析到 root 之内；越界、绝对路径一律拒绝。

    这是面板热编辑的安全边界：Web 侧传来的 path 只允许是「知识库内的相对路径」。
    """
    if rel is None:
        raise WikiPathError("empty path")
    rel = str(rel).strip().replace("\\", "/")
    if not rel or rel.startswith("/") or rel.startswith("~"):
        raise WikiPathError("absolute path not allowed")
    if re.match(r"^[a-zA-Z]:", rel):          # Windows 盘符
        raise WikiPathError("drive path not allowed")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise WikiPathError("path traversal not allowed")
    if not parts:
        raise WikiPathError("empty path")
    root_r = Path(root).resolve()
    target = (root_r / Path(*parts)).resolve()
    if target != root_r and root_r not in target.parents:
        raise WikiPathError("path escapes knowledge base")
    if must_exist and not target.exists():
        raise WikiPathError("not found: " + rel)
    return target


def slugify(name: str) -> str:
    """标题 → 安全文件名。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (name or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "untitled"


def _read_text(p: Path, limit: Optional[int] = None) -> str:
    try:
        t = p.read_text("utf-8", errors="ignore")
    except Exception:
        return ""
    return t[:limit] if limit else t


# ---------------------------------------------------------------------------
# 知识库基础
# ---------------------------------------------------------------------------

@dataclass
class PageMeta:
    section: str
    name: str            # 文件 stem
    rel: str             # 相对 wiki/ 的路径
    size: int
    mtime: float
    title: str
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"section": self.section, "name": self.name, "rel": self.rel,
                "size": self.size, "mtime": self.mtime, "title": self.title, "tags": self.tags}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter（轻量实现，只认 key: value 与 tags: [a, b]）。"""
    m = FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, text or ""
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "tags":
            inner = v.strip("[]")
            meta["tags"] = [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
        else:
            meta[k] = v.strip("'\"")
    return meta, text[m.end():]


def ensure_structure(kw: Path) -> None:
    for d in ("raw", "raw/assets", *(f"wiki/{s}" for s in SECTIONS)):
        (kw / d).mkdir(parents=True, exist_ok=True)
    seeds = {
        "wiki/index.md": "# 知识库索引\n\n> 由 Kira Wiki 自动维护\n\n## 总览\n\n- [[overview]]\n",
        "wiki/log.md": "# 操作日志\n\n",
        "wiki/overview.md": "# 总览\n\n",
        "wiki/conventions.md": "# 使用约定\n\n- 素材放入 raw/，LLM 负责整理\n- 查询用 /wiki query\n",
    }
    for rel, content in seeds.items():
        p = kw / rel
        if not p.exists():
            p.write_text(content, "utf-8")


def append_log(kw: Path, entry: str) -> None:
    log = kw / "wiki/log.md"
    if not log.parent.exists():
        ensure_structure(kw)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {entry}\n")


def iter_pages(kw: Path) -> list[PageMeta]:
    """扫描 wiki/ 下的全部页面（含根级 index/overview/conventions）。"""
    wiki = kw / "wiki"
    if not wiki.exists():
        return []
    out: list[PageMeta] = []
    for sec in SECTIONS:
        d = wiki / sec
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            st = p.stat()
            head = _read_text(p, 2000)
            meta, _ = parse_frontmatter(head)
            out.append(PageMeta(section=sec, name=p.stem, rel=f"{sec}/{p.name}",
                                size=st.st_size, mtime=st.st_mtime,
                                title=meta.get("title") or p.stem,
                                tags=meta.get("tags") or []))
    for name in ROOT_FILES:
        p = wiki / f"{name}.md"
        if p.is_file():
            st = p.stat()
            out.append(PageMeta(section="root", name=name, rel=p.name,
                                size=st.st_size, mtime=st.st_mtime, title=name, tags=[]))
    return out


def stats(kw: Path) -> dict:
    pages = iter_pages(kw)
    per = {s: 0 for s in SECTIONS}
    words = 0
    for pg in pages:
        if pg.section in per:
            per[pg.section] += 1
        if pg.section != "root":
            words += pg.size
    raw_dir = kw / "raw"
    raw = 0
    if raw_dir.exists():
        raw = sum(1 for p in raw_dir.rglob("*") if p.is_file() and p.suffix.lower() in RAW_EXTS)
    return {"pages": len(pages), "by_section": per, "raw": raw, "bytes": words,
            "log_lines": _log_lines(kw)}


def _log_lines(kw: Path) -> int:
    p = kw / "wiki" / "log.md"
    if not p.is_file():
        return 0
    try:
        return max(0, len(p.read_text("utf-8", errors="ignore").splitlines()) - 1)
    except Exception:
        return 0


def recent_log(kw: Path, limit: int = 40) -> list[str]:
    p = kw / "wiki" / "log.md"
    if not p.is_file():
        return []
    lines = [l for l in p.read_text("utf-8", errors="ignore").splitlines() if l.startswith("- ")]
    return list(reversed(lines[-limit:]))


def build_graph(kw: Path) -> dict:
    """wikilink 图：节点 = 页面，边 = [[link]]。用于面板的晶格视图。"""
    pages = iter_pages(kw)
    by_stem = {pg.name: pg for pg in pages}
    nodes = [{"id": pg.rel, "name": pg.name, "title": pg.title, "section": pg.section,
              "size": pg.size, "mtime": pg.mtime} for pg in pages]
    links = []
    for pg in pages:
        text = _read_text(kw / "wiki" / pg.rel, 200_000)
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip().split("/")[-1]
            tgt = by_stem.get(target)
            if tgt and tgt.rel != pg.rel:
                links.append({"source": pg.rel, "target": tgt.rel})
    seen, uniq = set(), []
    for e in links:
        k = (e["source"], e["target"])
        if k not in seen:
            seen.add(k); uniq.append(e)
    return {"nodes": nodes, "links": uniq}


def rebuild_index(kw: Path) -> int:
    """按实际页面重建 index.md，返回收录页数。"""
    ensure_structure(kw)
    wiki = kw / "wiki"
    lines = ["# 知识库索引\n", "> 由 Kira Wiki 自动维护\n", "## 总览\n", "- [[overview]]\n"]
    total = 0
    for sec in SECTIONS:
        lines.append(f"\n## {SECTION_LABEL[sec]}\n")
        d = wiki / sec
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                lines.append(f"- [[{p.stem}]]")
                total += 1
        lines.append("")
    (wiki / "index.md").write_text("\n".join(lines), "utf-8")
    return total


# ---------------------------------------------------------------------------
# 检索（替代「全量塞进 prompt」）
# ---------------------------------------------------------------------------

def _terms(text: str) -> list[str]:
    """中英混排的轻量分词：英文/数字按词，中文按 2-gram。"""
    text = (text or "").lower()
    out = re.findall(r"[a-z0-9_]{2,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    for chunk in cjk:
        if len(chunk) == 1:
            out.append(chunk)
        else:
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return out


def rank_pages(kw: Path, question: str, *, top_k: int = 8, per_page_chars: int = 2600,
               budget_chars: int = 14000) -> tuple[list[dict], dict]:
    """按关键词重合度排序挑页（无外部依赖，替代 30k 全量拼接）。

    返回 (选中页, 统计)。排序依据：标题命中 > 正文命中频次 > 新鲜度微调。
    """
    q = set(_terms(question))
    scored = []
    pages = iter_pages(kw)
    for pg in pages:
        if pg.section == "root":
            continue
        text = _read_text(kw / "wiki" / pg.rel, 200_000)
        body = parse_frontmatter(text)[1]
        body_terms = _terms(body)
        if not q:
            score = 0.0
        else:
            overlap = sum(1 for t in body_terms if t in q)
            title_hit = sum(2 for t in _terms(pg.title) if t in q)
            score = overlap + title_hit * 3
        if score > 0 or not q:
            scored.append((score, pg, body))
    scored.sort(key=lambda x: (-x[0], -x[1].mtime))
    picked, used = [], 0
    for score, pg, body in scored:
        if used >= budget_chars:
            break
        snippet = body[:per_page_chars]
        used += len(snippet)
        picked.append({"rel": pg.rel, "title": pg.title, "section": pg.section,
                       "score": round(score, 2), "text": snippet})
    return picked, {"candidates": len(scored), "picked": len(picked), "chars": used}


def lint(kw: Path) -> dict:
    """体检：空页 / 断链 / 孤立页 / 结构缺失。"""
    ensure_structure(kw)
    pages = iter_pages(kw)
    names = {pg.name for pg in pages}
    issues: list[dict] = []
    incoming: dict[str, int] = {pg.name: 0 for pg in pages}
    for pg in pages:
        if pg.section == "root":
            continue
        text = _read_text(kw / "wiki" / pg.rel, 400_000)
        if len(text.strip()) < 50:
            issues.append({"level": "warn", "kind": "empty_page", "page": pg.rel,
                           "detail": "页面内容过短（<50 字）"})
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip().split("/")[-1]
            if target in ROOT_FILES:
                continue
            if target not in names:
                issues.append({"level": "warn", "kind": "broken_link", "page": pg.rel,
                               "detail": f"断链 → [[{target}]]"})
            else:
                incoming[target] = incoming.get(target, 0) + 1
    for pg in pages:
        if pg.section in ("entities", "concepts") and incoming.get(pg.name, 0) == 0:
            issues.append({"level": "info", "kind": "orphan", "page": pg.rel,
                           "detail": "没有被任何页面引用（孤立页）"})
    raw_len = stats(kw)["raw"]
    if raw_len == 0:
        issues.append({"level": "info", "kind": "no_raw", "page": "-",
                       "detail": "raw/ 里还没有素材"})
    return {"stats": stats(kw), "issues": issues}


def write_page(kw: Path, rel: str, content: str) -> Path:
    """写入（或新建）知识库内的页面 —— 只允许 wiki/ 下的 .md。"""
    if not rel.endswith(".md"):
        raise WikiPathError("only .md pages can be written")
    if len(content.encode("utf-8")) > MAX_PAGE_BYTES:
        raise WikiPathError("page too large")
    target = resolve_rel(kw / "wiki", rel)
    if target.parent == (kw / "wiki").resolve() and target.stem not in ROOT_FILES:
        raise WikiPathError("unknown root page")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, "utf-8")
    return target


def delete_page(kw: Path, rel: str) -> None:
    if not rel.endswith(".md"):
        raise WikiPathError("only .md pages can be deleted")
    target = resolve_rel(kw / "wiki", rel, must_exist=True)
    if target.parent == (kw / "wiki").resolve() and target.stem in ROOT_FILES:
        raise WikiPathError("core page cannot be deleted")
    target.unlink()


def read_page(kw: Path, rel: str) -> dict:
    target = resolve_rel(kw / "wiki", rel, must_exist=True)
    st = target.stat()
    text = _read_text(target, MAX_PAGE_BYTES)
    meta, body = parse_frontmatter(text)
    return {"rel": rel, "name": target.stem, "title": meta.get("title") or target.stem,
            "tags": meta.get("tags") or [], "content": text, "body": body,
            "size": st.st_size, "mtime": st.st_mtime}
