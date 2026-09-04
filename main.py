import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.plugin import BasePlugin, logger, register
from core.provider import LLMRequest
from core.chat.message_utils import KiraMessageBatchEvent

# ---------------------------------------------------------------------------
# KB registry persistence
# ---------------------------------------------------------------------------

REGISTRY_FILE = "kb_registry.json"


def _slugify(name: str) -> str:
    """Turn a title into a safe file slug."""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name.strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "untitled"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _frontmatter(title: str, tags: list, extra: dict | None = None) -> str:
    lines = ["---", f"title: {title}", f"tags: [{', '.join(tags)}]"]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class KiraWikiPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        sec = cfg.get("section_basic", {})
        self.enabled = sec.get("enabled", True)
        self.default_kb_path = sec.get("default_kb_path", "") or ""
        llm_sec = cfg.get("section_llm", {})
        self.llm_model = llm_sec.get("model", "") or ""
        self.max_pages = int(llm_sec.get("max_pages_per_ingest", 15) or 15)
        self._registry: dict = {}
        self._registry_path = Path(self.ctx.get_plugin_data_dir()) / REGISTRY_FILE

    async def initialize(self):
        """插件启动钩子：预加载知识库注册表。"""
        try:
            self._load_registry()
            logger.info(f"[kira-wiki] initialized, {len(self._registry)} KB(s) registered")
        except Exception as e:
            logger.error(f"[kira-wiki] initialize failed: {e}")

    async def terminate(self):
        """插件停止钩子：保存注册表。"""
        try:
            self._save_registry()
        except Exception as e:
            logger.error(f"[kira-wiki] terminate failed: {e}")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _load_registry(self):
        if self._registry:
            return
        if self._registry_path.exists():
            try:
                self._registry = json.loads(self._registry_path.read_text("utf-8"))
            except Exception as e:
                logger.error(f"[kira-wiki] registry load failed: {e}")
                self._registry = {}

    def _save_registry(self):
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(
            json.dumps(self._registry, ensure_ascii=False, indent=2), "utf-8"
        )

    def _get_kb(self, name: str) -> Optional[Path]:
        self._load_registry()
        path = self._registry.get(name)
        if path and Path(path).exists():
            return Path(path)
        if self.default_kb_path and Path(self.default_kb_path).exists():
            return Path(self.default_kb_path)
        return None

    def _register_kb(self, name: str, path: Path):
        self._load_registry()
        self._registry[name] = str(path)
        self._save_registry()

    async def _llm(self, prompt: str, system: str = "") -> str:
        client = self.ctx.get_default_llm_client()
        if self.llm_model:
            try:
                client = self.ctx.get_llm_client(self.llm_model)
            except Exception:
                pass
        if client is None:
            return ""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = await client.chat(LLMRequest(messages=messages))
        except Exception as e:
            logger.error(f"[kira-wiki] llm call failed: {e}")
            return ""
        return (getattr(resp, "text_response", "") or "").strip()

    def _ensure_structure(self, kb: Path):
        for d in ["raw", "raw/assets", "wiki/sources", "wiki/entities",
                  "wiki/concepts", "wiki/analyses"]:
            (kb / d).mkdir(parents=True, exist_ok=True)
        for f, content in [
            ("wiki/index.md", "# 知识库索引\n\n> 由 Kira Wiki 自动维护\n\n## 总览\n\n- [[overview]]\n\n## 来源\n\n## 实体\n\n## 概念\n\n## 分析\n"),
            ("wiki/log.md", "# 操作日志\n\n"),
            ("wiki/overview.md", "# 总览\n\n"),
            ("wiki/conventions.md", "# 使用约定\n\n- 素材放入 raw/，LLM 负责整理\n- 查询用 /wiki query\n"),
        ]:
            p = kb / f
            if not p.exists():
                p.write_text(content, "utf-8")

    def _append_log(self, kb: Path, entry: str):
        log = kb / "wiki/log.md"
        self._ensure_structure(kb)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_now()} {entry}\n")

    def _update_index(self, kb: Path):
        """Rebuild wiki/index.md from actual pages."""
        self._ensure_structure(kb)
        wiki = kb / "wiki"
        lines = ["# 知识库索引\n", "> 由 Kira Wiki 自动维护\n", "## 总览\n", "- [[overview]]\n"]
        for section, label in [
            ("sources", "来源"), ("entities", "实体"),
            ("concepts", "概念"), ("analyses", "分析"),
        ]:
            lines.append(f"\n## {label}\n")
            d = wiki / section
            if d.exists():
                for p in sorted(d.glob("*.md")):
                    lines.append(f"- [[{p.stem}]]")
            lines.append("")
        (wiki / "index.md").write_text("\n".join(lines), "utf-8")

    def _read_raw_sources(self, kb: Path) -> list[Path]:
        raw = kb / "raw"
        if not raw.exists():
            return []
        exts = {".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}
        return [p for p in raw.rglob("*") if p.is_file() and p.suffix.lower() in exts]

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------

    @register.tool(
        name="wiki_init",
        description="初始化一个 Obsidian 知识库：创建 raw/ 与 wiki/ 目录结构并注册。用户说'建知识库/初始化wiki'时调用。",
        params={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "知识库名称（用于注册和后续引用）"},
                "path": {"type": "string", "description": "知识库根目录绝对路径（Obsidian vault 目录）"},
            },
            "required": ["name", "path"],
        },
    )
    async def wiki_init(self, event: KiraMessageBatchEvent, name: str, path: str) -> str:
        kb = Path(path).expanduser()
        try:
            kb.mkdir(parents=True, exist_ok=True)
            self._ensure_structure(kb)
            self._register_kb(name, kb)
            self._append_log(kb, f"初始化知识库 {name} @ {kb}")
            self._update_index(kb)
        except Exception as e:
            logger.error(f"[kira-wiki] init failed: {e}")
            return f"初始化失败：{e}"
        return f"知识库 [{name}] 已就绪 @ {kb}\n目录：raw/（素材） + wiki/（LLM 生成）\n用 Obsidian 打开 {kb} 即可浏览图谱"

    @register.tool(
        name="wiki_ingest",
        description="收录素材到知识库：把内容写入 raw/，LLM 自动生成摘要页、实体页、概念页并维护交叉引用。用户说'收录这篇文章/把XX存进知识库'时调用。",
        params={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "素材内容（文本/文章/笔记正文）"},
                "title": {"type": "string", "description": "素材标题，用于命名文件"},
                "kb_name": {"type": "string", "description": "知识库名称，留空用默认知识库"},
                "source_url": {"type": "string", "description": "素材来源链接（可选）"},
            },
            "required": ["content", "title"],
        },
    )
    async def wiki_ingest(self, event: KiraMessageBatchEvent, content: str, title: str,
                          kb_name: str = "", source_url: str = "") -> str:
        kb = self._get_kb(kb_name) if kb_name else self._get_kb("")
        if kb is None:
            return "还没有知识库，先 /wiki init 或告诉我知识库路径"
        self._ensure_structure(kb)

        # 1. write raw source
        slug = _slugify(title)
        raw_path = kb / "raw" / f"{slug}.md"
        raw_content = f"# {title}\n\n"
        if source_url:
            raw_content += f"> 来源：{source_url}\n\n"
        raw_content += content
        raw_path.write_text(raw_content, "utf-8")

        # 2. LLM generates wiki pages
        system = (
            "你是知识库编译器。根据用户提供的素材，生成 Obsidian markdown 页面。"
            "输出格式为 JSON，包含以下字段：\n"
            "- summary: 素材摘要页内容（含 YAML frontmatter，tags 含 source）\n"
            "- entities: 实体页列表，每项 {title, content}（人物/组织/工具等）\n"
            "- concepts: 概念页列表，每项 {title, content}（理论/方法/模式等）\n"
            "- analysis: 分析页内容（可选，综合论述）\n"
            "页面内容用 [[wikilink]] 互相引用，frontmatter 含 title 和 tags。"
            "只输出 JSON，不要多余文字。"
        )
        prompt = (
            f"素材标题：{title}\n"
            f"素材内容：\n{content[:12000]}\n\n"
            "请生成知识库页面。实体和概念页各最多 5 个，每个页面 100-300 字。"
        )
        llm_out = await self._llm(prompt, system=system)
        if not llm_out:
            self._append_log(kb, f"收录 {title}（LLM 生成失败，仅存 raw）")
            return f"素材已存入 raw/{slug}.md，但 LLM 生成失败，稍后再试"

        # 3. parse JSON (tolerate code fences)
        cleaned = llm_out.strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except Exception:
            # try to find the JSON object
            m = re.search(r"\{.*\}", cleaned, re.S)
            if not m:
                self._append_log(kb, f"收录 {title}（JSON 解析失败）")
                return f"素材已存入 raw/{slug}.md，但 LLM 输出解析失败"
            try:
                data = json.loads(m.group(0))
            except Exception as e:
                self._append_log(kb, f"收录 {title}（JSON 解析失败）")
                return f"素材已存入 raw/{slug}.md，但 LLM 输出解析失败：{e}"

        # 4. write pages
        wiki = kb / "wiki"
        written = 0
        summary = data.get("summary", "")
        if summary:
            (wiki / "sources" / f"{slug}.md").write_text(summary, "utf-8")
            written += 1
        for ent in data.get("entities", [])[:5]:
            t = ent.get("title", "")
            if not t:
                continue
            (wiki / "entities" / f"{_slugify(t)}.md").write_text(ent.get("content", ""), "utf-8")
            written += 1
        for con in data.get("concepts", [])[:5]:
            t = con.get("title", "")
            if not t:
                continue
            (wiki / "concepts" / f"{_slugify(t)}.md").write_text(con.get("content", ""), "utf-8")
            written += 1
        analysis = data.get("analysis", "")
        if analysis:
            (wiki / "analyses" / f"{slug}-analysis.md").write_text(analysis, "utf-8")
            written += 1

        # 5. update overview + index + log
        overview = wiki / "overview.md"
        with overview.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {title}（{_now()}）\n\n- 来源：[[sources/{slug}]]\n")
            for ent in data.get("entities", [])[:5]:
                t = ent.get("title", "")
                if t:
                    fh.write(f"- 实体：[[entities/{_slugify(t)}]]\n")
            for con in data.get("concepts", [])[:5]:
                t = con.get("title", "")
                if t:
                    fh.write(f"- 概念：[[concepts/{_slugify(t)}]]\n")
        self._update_index(kb)
        self._append_log(kb, f"收录 {title}，生成 {written} 个页面")

        return f"收录完成：{title}\nraw/{slug}.md + wiki 生成 {written} 个页面（摘要/实体/概念/分析）\nObsidian 里刷新就能看到图谱了"

    @register.tool(
        name="wiki_query",
        description="查询知识库：基于 wiki/ 页面回答问题并标注来源。用户问'知识库里XX是什么/查一下wiki'时调用。",
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要查询的问题"},
                "kb_name": {"type": "string", "description": "知识库名称，留空用默认"},
            },
            "required": ["question"],
        },
    )
    async def wiki_query(self, event: KiraMessageBatchEvent, question: str, kb_name: str = "") -> str:
        kb = self._get_kb(kb_name) if kb_name else self._get_kb("")
        if kb is None:
            return "还没有知识库，先初始化一个"
        wiki = kb / "wiki"
        if not wiki.exists():
            return "知识库是空的"

        # gather all wiki pages as context
        pages = []
        for d in ["sources", "entities", "concepts", "analyses"]:
            dd = wiki / d
            if dd.exists():
                for p in sorted(dd.glob("*.md")):
                    try:
                        pages.append(f"### {p.stem}\n{p.read_text('utf-8')[:3000]}")
                    except Exception:
                        pass
        if not pages:
            return "知识库里还没有生成页面，先收录点素材"

        context = "\n\n".join(pages)
        system = (
            "你是知识库问答助手。基于提供的 wiki 页面回答问题，"
            "必须标注信息来源（页面名），不知道就直说不知道。"
        )
        prompt = f"知识库内容：\n{context[:30000]}\n\n问题：{question}\n\n请回答并标注来源页面。"
        answer = await self._llm(prompt, system=system)
        if not answer:
            return "查询失败，LLM 没响应，稍后再试"
        return answer

    @register.tool(
        name="wiki_lint",
        description="知识库健康检查：检查目录结构、缺失引用、空页面。用户说'检查知识库/体检'时调用。",
        params={
            "type": "object",
            "properties": {
                "kb_name": {"type": "string", "description": "知识库名称，留空用默认"},
            },
        },
    )
    async def wiki_lint(self, event: KiraMessageBatchEvent, kb_name: str = "") -> str:
        kb = self._get_kb(kb_name) if kb_name else self._get_kb("")
        if kb is None:
            return "还没有知识库"
        self._ensure_structure(kb)
        issues = []
        stats = {"raw": 0, "sources": 0, "entities": 0, "concepts": 0, "analyses": 0}
        for p in self._read_raw_sources(kb):
            stats["raw"] += 1
        for d in ["sources", "entities", "concepts", "analyses"]:
            dd = kb / "wiki" / d
            if dd.exists():
                for p in dd.glob("*.md"):
                    stats[d] += 1
                    if p.stat().st_size < 50:
                        issues.append(f"空页面：{d}/{p.stem}")
        # check broken wikilinks
        link_re = re.compile(r"\[\[([^\]|#]+)")
        for d in ["sources", "entities", "concepts", "analyses"]:
            dd = kb / "wiki" / d
            if not dd.exists():
                continue
            for p in dd.glob("*.md"):
                text = p.read_text("utf-8", errors="ignore")
                for m in link_re.finditer(text):
                    target = m.group(1).strip()
                    if target in ("overview", "index", "log", "conventions"):
                        continue
                    if not list((kb / "wiki").rglob(f"{target}.md")):
                        issues.append(f"断链 {p.stem} -> [[{target}]]")
        lines = [
            f"知识库体检 @ {kb}",
            f"素材 raw: {stats['raw']} | 摘要: {stats['sources']} | 实体: {stats['entities']} | 概念: {stats['concepts']} | 分析: {stats['analyses']}",
        ]
        if issues:
            lines.append(f"发现 {len(issues)} 个问题：")
            lines.extend(f"- {i}" for i in issues[:20])
        else:
            lines.append("没有发现问题，很健康")
        return "\n".join(lines)

    @register.tool(
        name="wiki_list",
        description="列出知识库内容概览。用户问'知识库里有什么'时调用。",
        params={
            "type": "object",
            "properties": {
                "kb_name": {"type": "string", "description": "知识库名称，留空用默认"},
            },
        },
    )
    async def wiki_list(self, event: KiraMessageBatchEvent, kb_name: str = "") -> str:
        kb = self._get_kb(kb_name) if kb_name else self._get_kb("")
        if kb is None:
            return "还没有知识库"
        self._ensure_structure(kb)
        lines = [f"知识库 @ {kb}"]
        for d, label in [("sources", "摘要"), ("entities", "实体"), ("concepts", "概念"), ("analyses", "分析")]:
            dd = kb / "wiki" / d
            names = [p.stem for p in sorted(dd.glob("*.md"))] if dd.exists() else []
            lines.append(f"{label}（{len(names)}）：{'、'.join(names[:15]) or '空'}")
        return "\n".join(lines)
