"""Kira Wiki · 服务层

把「知识库读写、索引、图、检索、体检」从 main.py 里抽出来，好处有三：
  1. **非阻塞**：所有磁盘操作都在 asyncio.to_thread 里跑，不在事件循环上做同步 I/O；
  2. **路径安全**：页面读写一律经过 resolve_rel()，杜绝 `../` 穿越到知识库之外；
  3. **可测**：纯函数式接口，不依赖插件实例，方便单测。

目录约定（Obsidian vault）::

    <kw>/
      raw/            素材（用户放入，LLM 只读）
        assets/
      wiki/           LLM 生成的知识库（可写区）
        index.md  log.md  overview.md  conventions.md
        sources/ entities/ concepts/ analyses/
      其它任意目录/*.md  ← 用户自己的笔记：计入 imported 分区（可读可写）

「已有 vault 直接用」是刻意支持的：vault 根目录与任意子目录下的 .md 都会被扫到，
标成 imported（外部笔记），和 LLM 生成区在数据与 UI 上分开；两边**都能读写**，
但两条硬边界不变：
  · 写操作只在**知识库根之内**（`../`、绝对路径、盘符一律拒绝）；
  · **只读操作绝不落盘** —— 扫库/体检/看图不会在用户的目录里创建任何东西
    （建目录只发生在显式的 init / ingest / write / rebuild）。

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
IMPORTED = "imported"
SECTION_LABEL = {"sources": "来源", "entities": "实体", "concepts": "概念",
                 "analyses": "分析", IMPORTED: "外部笔记"}
RAW_EXTS = {".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}
ROOT_FILES = ("index", "log", "overview", "conventions")
WIKILINK_RE = re.compile(r"(?<!\!)\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
MAX_PAGE_BYTES = 512 * 1024          # 单页写入上限（512 KiB）
MAX_EXTERNAL_PAGES = 5000            # 外部笔记扫描上限（防御巨型 vault）
EXTERNAL_SKIP_DIRS = {"wiki", "raw", "node_modules", "assets", "templates"}

ORIGIN_WIKI = "wiki"                 # 插件可写区（LLM 生成 / 面板热编辑）
ORIGIN_VAULT = "vault"               # 用户自己的笔记：只读

async def to_thread(fn, *args, **kwargs):
    """统一的线程池入口（保持调用点简洁）。"""
    return await asyncio.to_thread(fn, *args, **kwargs)

# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

class WikiPathError(ValueError):
    """非法路径（穿越/绝对路径/越界/只读越权）。"""

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
    rel: str             # wiki 区：相对 wiki/；外部笔记：相对知识库根
    size: int
    mtime: float
    title: str
    tags: list[str] = field(default_factory=list)
    origin: str = ORIGIN_WIKI     # wiki（可写） / vault（用户笔记，只读）

    def as_dict(self) -> dict:
        return {"section": self.section, "name": self.name, "rel": self.rel,
                "size": self.size, "mtime": self.mtime, "title": self.title,
                "tags": self.tags, "origin": self.origin, "writable": True}

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

def is_knowledge_base(kw: Path) -> bool:
    """是否已经具备插件的目录结构（缺它也能用——外部笔记仍可读）。"""
    return (Path(kw) / "wiki").is_dir()

def ensure_structure(kw: Path) -> None:
    """**写操作专用**：创建 raw/ 与 wiki/ 骨架。只读路径不得调用它。"""
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

def _page_meta(p: Path, section: str, rel: str, origin: str = ORIGIN_WIKI) -> PageMeta:
    st = p.stat()
    head = _read_text(p, 2000)
    meta, _ = parse_frontmatter(head)
    return PageMeta(section=section, name=p.stem, rel=rel, size=st.st_size,
                    mtime=st.st_mtime, title=meta.get("title") or p.stem,
                    tags=meta.get("tags") or [], origin=origin)

def iter_external(kw: Path, limit: int = MAX_EXTERNAL_PAGES) -> list[PageMeta]:
    """扫描「用户自己的笔记」：知识库根 + 任意子目录下的 .md（跳过 wiki/ 与 raw/）。

    用于「把已有 Obsidian 库直接指过来」的场景：这些笔记与生成区一同进图谱、进检索，
    也能被 bot / 面板直接编辑（写操作仍受 resolve_target 的边界约束）。
    """
    kw = Path(kw)
    if not kw.is_dir():
        return []
    out: list[PageMeta] = []
    for p in sorted(kw.rglob("*.md")):
        if len(out) >= limit:
            break
        try:
            rel_parts = p.relative_to(kw).parts
        except ValueError:
            continue
        if not rel_parts:
            continue
        if rel_parts[0] in EXTERNAL_SKIP_DIRS or rel_parts[0].startswith("."):
            continue
        if any(part.startswith(".") for part in rel_parts[:-1]):
            continue
        try:
            out.append(_page_meta(p, IMPORTED, "/".join(rel_parts), ORIGIN_VAULT))
        except Exception:
            continue
    return out

def iter_pages(kw: Path, *, include_external: bool = True) -> list[PageMeta]:
    """扫描全部页面：wiki 区（分区页 + 根页）＋ 可选的外部笔记。"""
    kw = Path(kw)
    wiki = kw / "wiki"
    out: list[PageMeta] = []
    if wiki.exists():
        for sec in SECTIONS:
            d = wiki / sec
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                try:
                    out.append(_page_meta(p, sec, f"{sec}/{p.name}"))
                except Exception:
                    continue
        for name in ROOT_FILES:
            p = wiki / f"{name}.md"
            if p.is_file():
                try:
                    out.append(_page_meta(p, "root", p.name))
                except Exception:
                    continue
    if include_external:
        out.extend(iter_external(kw))
    return out

def stats(kw: Path, pages: Optional[list[PageMeta]] = None) -> dict:
    """统计。`pages` 可传入已扫描结果，避免同一次请求重复扫库。"""
    pages = iter_pages(kw) if pages is None else pages
    per = {s: 0 for s in SECTIONS}
    per[IMPORTED] = 0
    root_n = 0
    words = 0
    for pg in pages:
        if pg.section in per:
            per[pg.section] += 1
        elif pg.section == "root":
            root_n += 1
        if pg.section != "root":
            words += pg.size
    raw_dir = Path(kw) / "raw"
    raw = 0
    if raw_dir.exists():
        raw = sum(1 for p in raw_dir.rglob("*") if p.is_file() and p.suffix.lower() in RAW_EXTS)
    return {"pages": len(pages),                      # 全部（含根页与外部笔记）
            "section_pages": sum(per.values()),       # 生成区 + 外部笔记的分区页
            "root_pages": root_n,                     # index/log/overview/conventions
            "by_section": per, "raw": raw, "bytes": words,
            "log_lines": _log_lines(kw)}

def _log_lines(kw: Path) -> int:
    p = Path(kw) / "wiki" / "log.md"
    if not p.is_file():
        return 0
    try:
        return max(0, len(p.read_text("utf-8", errors="ignore").splitlines()) - 1)
    except Exception:
        return 0

def recent_log(kw: Path, limit: int = 40) -> list[str]:
    p = Path(kw) / "wiki" / "log.md"
    if not p.is_file():
        return []
    lines = [l for l in p.read_text("utf-8", errors="ignore").splitlines() if l.startswith("- ")]
    return list(reversed(lines[-limit:]))

def build_graph(kw: Path) -> dict:
    """wikilink 图：节点 = 页面，边 = [[link]]。用于面板的晶格视图。"""
    pages = iter_pages(kw)
    by_stem = {pg.name: pg for pg in pages}
    nodes = [{"id": pg.rel, "name": pg.name, "title": pg.title, "section": pg.section,
              "origin": pg.origin, "writable": pg.origin == ORIGIN_WIKI,
              "size": pg.size, "mtime": pg.mtime} for pg in pages]
    links = []
    for pg in pages:
        root = (kw / "wiki" / pg.rel) if pg.origin == ORIGIN_WIKI else (kw / pg.rel)
        text = _read_text(root, 200_000)
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
    """按实际页面重建 index.md，返回收录页数（写操作）。"""
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
    external = iter_external(kw)
    if external:
        lines.append(f"\n## {SECTION_LABEL[IMPORTED]}（{len(external)}）\n")
        for pg in external:
            lines.append(f"- [[{pg.name}]]")
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

    双重上限：**页数 top_k** 与**字符预算 budget_chars**，先到者为准。
    返回 (选中页, 统计)。排序依据：标题命中 > 正文命中频次 > 新鲜度微调。
    """
    q = set(_terms(question))
    scored = []
    pages = iter_pages(kw)
    for pg in pages:
        if pg.section == "root":
            continue
        root = (Path(kw) / "wiki" / pg.rel) if pg.origin == ORIGIN_WIKI else (Path(kw) / pg.rel)
        text = _read_text(root, 200_000)
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
        if len(picked) >= max(1, int(top_k or 1)) or used >= budget_chars:
            break
        snippet = body[:per_page_chars]
        used += len(snippet)
        picked.append({"rel": pg.rel, "title": pg.title, "section": pg.section,
                       "origin": pg.origin, "score": round(score, 2), "text": snippet})
    return picked, {"candidates": len(scored), "picked": len(picked), "chars": used,
                    "top_k": int(top_k or 0), "budget_chars": budget_chars}

def lint(kw: Path) -> dict:
    """体检：空页 / 断链 / 孤立页 / 结构缺失。**只读**——不会创建任何目录。"""
    pages = iter_pages(kw)
    names = {pg.name for pg in pages}
    issues: list[dict] = []
    incoming: dict[str, int] = {pg.name: 0 for pg in pages}
    for pg in pages:
        if pg.section == "root":
            continue
        root = (Path(kw) / "wiki" / pg.rel) if pg.origin == ORIGIN_WIKI else (Path(kw) / pg.rel)
        text = _read_text(root, 400_000)
        if pg.origin == ORIGIN_WIKI and len(text.strip()) < 50:
            issues.append({"level": "warn", "kind": "empty_page", "page": pg.rel,
                           "detail": "页面内容过短（<50 字）"})
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip().split("/")[-1]
            if target in ROOT_FILES:
                continue
            if target not in names:
                # 外部笔记常引用未纳入扫描的资源/附件，只对生成区报断链，避免噪音
                if pg.origin == ORIGIN_WIKI:
                    issues.append({"level": "warn", "kind": "broken_link", "page": pg.rel,
                                   "detail": f"断链 → [[{target}]]"})
            else:
                incoming[target] = incoming.get(target, 0) + 1
    for pg in pages:
        if pg.origin == ORIGIN_WIKI and pg.section in ("entities", "concepts") \
                and incoming.get(pg.name, 0) == 0:
            issues.append({"level": "info", "kind": "orphan", "page": pg.rel,
                           "detail": "没有被任何页面引用（孤立页）"})
    st = stats(kw, pages)
    if not is_knowledge_base(kw):
        issues.append({"level": "warn", "kind": "not_initialized", "page": "-",
                       "detail": "该目录还没有 wiki/ 生成区，点「初始化知识库」即可开始（不会动你的原有笔记）"})
    if st["raw"] == 0:
        issues.append({"level": "info", "kind": "no_raw", "page": "-",
                       "detail": "raw/ 里还没有素材"})
    return {"stats": st, "issues": issues}

# ---------------------------------------------------------------------------
# 页面读写
# ---------------------------------------------------------------------------

def _norm_rel(rel: str) -> str:
    return str(rel or "").strip().replace("\\", "/")

def looks_generated(rel: str) -> bool:
    """按路径判断是否属于「生成区」：分区目录或 4 个根页。"""
    rel = _norm_rel(rel)
    if rel in tuple(f"{n}.md" for n in ROOT_FILES):
        return True
    head = rel.split("/")[0]
    return head in SECTIONS and rel.endswith(".md")

def resolve_target(kw: Path, rel: str, origin: str = "", *,
                   must_exist: bool = False) -> tuple[Path, str]:
    """把（相对路径, 原点）解析成绝对路径。

    origin 为空时按路径自动判定：`sources|entities|concepts|analyses/…` 与 4 个根页
    归生成区（wiki/），其余归用户自己的笔记（知识库根，含子目录）。
    两条硬边界：
      1. 结果必须落在知识库根内（resolve_rel 保证，`../`/绝对路径一律拒绝）；
      2. 原点为 vault 时，绝不允许落到 wiki/ 或 raw/ 里。
    """
    kw = Path(kw).resolve()
    rel = _norm_rel(rel)
    if origin not in (ORIGIN_WIKI, ORIGIN_VAULT):
        origin = ORIGIN_WIKI if looks_generated(rel) else ORIGIN_VAULT
    root = kw / "wiki" if origin == ORIGIN_WIKI else kw
    target = resolve_rel(root, rel, must_exist=must_exist)
    if origin == ORIGIN_VAULT:
        try:
            parts = target.relative_to(kw).parts
        except ValueError:
            raise WikiPathError("path escapes knowledge base")
        if parts and parts[0] in ("wiki", "raw"):
            raise WikiPathError("path inside generated area must use origin=wiki")
        if any(p.startswith(".") for p in parts):
            raise WikiPathError("hidden path not allowed")
    return target, origin

def write_page(kw: Path, rel: str, content: str, *, origin: str = "") -> tuple[Path, str]:
    """写入（或新建）页面。生成区与「用户自己的笔记」都可写，但都在知识库根之内。"""
    if not _norm_rel(rel).endswith(".md"):
        raise WikiPathError("only .md pages can be written")
    if len((content or "").encode("utf-8")) > MAX_PAGE_BYTES:
        raise WikiPathError("page too large")
    target, origin = resolve_target(kw, rel, origin)
    if origin == ORIGIN_WIKI:
        wiki_root = (Path(kw) / "wiki").resolve()
        if target.parent == wiki_root and target.stem not in ROOT_FILES:
            raise WikiPathError("unknown root page")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, "utf-8")
    return target, origin

def append_page(kw: Path, rel: str, content: str, *, origin: str = "") -> tuple[Path, str]:
    """在页面末尾追加内容（不存在则创建）。"""
    target, origin = resolve_target(kw, rel, origin)
    if origin == ORIGIN_WIKI:
        wiki_root = (Path(kw) / "wiki").resolve()
        if target.parent == wiki_root and target.stem not in ROOT_FILES:
            raise WikiPathError("unknown root page")
    old = _read_text(target) if target.is_file() else ""
    body = (old.rstrip("\n") + "\n\n" + (content or "").strip() + "\n") if old.strip() else (content or "")
    if len(body.encode("utf-8")) > MAX_PAGE_BYTES:
        raise WikiPathError("page too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, "utf-8")
    return target, origin

def delete_page(kw: Path, rel: str, *, origin: str = "") -> str:
    target, origin = resolve_target(kw, rel, origin, must_exist=True)
    if origin == ORIGIN_WIKI:
        wiki_root = (Path(kw) / "wiki").resolve()
        if target.parent == wiki_root and target.stem in ROOT_FILES:
            raise WikiPathError("core page cannot be deleted")
    target.unlink()
    return origin

def section_of(rel: str, origin: str) -> str:
    """页面归属分区：外部笔记 = imported；生成区按首段目录，其余为 root。"""
    if origin == ORIGIN_VAULT:
        return IMPORTED
    head = _norm_rel(rel).split("/")[0]
    return head if head in SECTIONS else "root"

def read_page(kw: Path, rel: str, origin: str = "") -> dict:
    target, origin = resolve_target(kw, rel, origin, must_exist=True)
    st = target.stat()
    text = _read_text(target, MAX_PAGE_BYTES)
    meta, body = parse_frontmatter(text)
    return {"rel": rel, "name": target.stem, "title": meta.get("title") or target.stem,
            "tags": meta.get("tags") or [], "content": text, "body": body,
            "size": st.st_size, "mtime": st.st_mtime, "origin": origin,
            "section": section_of(rel, origin), "writable": True}
