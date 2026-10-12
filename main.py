"""Kira Wiki - Obsidian knowledge base manager based on Karpathy's LLM Wiki idea.

The LLM acts as the compiler: user sources material into raw/, the LLM
summarizes, cross-references, files and maintains wiki/ pages. Obsidian is
the IDE, the wiki is the codebase.

Structure:
    <kw>/
      raw/            # user-written source material (LLM read-only)
        assets/
      wiki/           # LLM-generated knowledge base
        index.md  log.md  overview.md  conventions.md
        sources/ entities/ concepts/ analyses/

v1.1.0
  · 工具精简 5 → 4（wiki_list + wiki_lint 合并为 wiki_status）
  · 全部磁盘操作移出事件循环（asyncio.to_thread）
  · 检索改为关键词排序 + 预算裁剪（不再把整库塞进 prompt）
  · 侧边栏面板：晶格视图 / 页面阅读 / 热编辑 / 体检 / 问答
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.plugin import BasePlugin, PageMenu, PluginPage, logger, register
from core.provider import LLMRequest
from core.chat.message_utils import KiraMessageBatchEvent

try:                       # 插件以包形式加载（与 acc 的 `from . import` 同款）
    from . import wiki_core as wc
except ImportError:        # 兜底：以脚本形式加载
    import wiki_core as wc

REGISTRY_FILE = "kw_registry.json"

SYSTEM_COMPILER = (
    "你是知识库编译器。根据用户提供的素材，生成 Obsidian markdown 页面。"
    "输出格式为 JSON，包含以下字段：\n"
    "- summary: 素材摘要页内容（含 YAML frontmatter，tags 含 source）\n"
    "- entities: 实体页列表，每项 {title, content}（人物/组织/工具等）\n"
    "- concepts: 概念页列表，每项 {title, content}（理论/方法/模式等）\n"
    "- analysis: 分析页内容（可选，综合论述）\n"
    "页面内容用 [[wikilink]] 互相引用，frontmatter 含 title 和 tags。"
    "只输出 JSON，不要多余文字。"
)

SYSTEM_QA = (
    "你是知识库问答助手。基于提供的 wiki 页面回答问题，"
    "必须标注信息来源（页面名），不知道就直说不知道。"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _as_list(value, limit: int) -> list:
    """LLM 输出防御：只接受 list，其余（None/dict/str）一律当空。"""
    if not isinstance(value, list):
        return []
    return value[:limit]


class KiraWikiPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        sec = cfg.get("section_basic", {})
        self.enabled = sec.get("enabled", True)
        self.default_kw_path = sec.get("default_kw_path", "") or ""
        if self.default_kw_path and not os.path.isabs(self.default_kw_path):
            self.default_kw_path = str(Path(self.default_kw_path).resolve())
        llm_sec = cfg.get("section_llm", {})
        self.llm_model = llm_sec.get("model", "") or ""
        self.max_pages = int(llm_sec.get("max_pages_per_ingest", 15) or 15)
        self._registry: dict = {}
        self._registry_path = Path(self.ctx.get_plugin_data_dir()) / REGISTRY_FILE

    async def initialize(self):
        """启动：预加载知识库注册表（磁盘读放线程池）。"""
        try:
            await wc.to_thread(self._load_registry)
            logger.info(f"[kira-wiki] initialized, {len(self._registry)} KW(s) registered")
        except Exception as e:
            logger.error(f"[kira-wiki] initialize failed: {e}")

    async def terminate(self):
        try:
            await wc.to_thread(self._save_registry)
        except Exception as e:
            logger.error(f"[kira-wiki] terminate failed: {e}")

    # ------------------------------------------------------------------
    # registry / llm
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
            json.dumps(self._registry, ensure_ascii=False, indent=2), "utf-8")

    def _registry_view(self) -> dict:
        self._load_registry()
        out = {}
        for name, path in (self._registry or {}).items():
            out[name] = {"path": path, "exists": Path(path).exists()}
        return out

    def _get_kw(self, name: str = "") -> Optional[Path]:
        """取知识库根目录。name 给了但没注册 ⇒ 返回 None（不静默改写到别的库）。"""
        self._load_registry()
        if name and name in self._registry:
            p = Path(self._registry[name])
            return p if p.exists() else None
        if name and name not in self._registry:
            return None
        if self._registry:
            # 未指定名称：优先默认路径，其次注册表里第一个可用库
            if self.default_kw_path and Path(self.default_kw_path).exists():
                return Path(self.default_kw_path)
            for path in self._registry.values():
                if Path(path).exists():
                    return Path(path)
            return None
        if self.default_kw_path and Path(self.default_kw_path).exists():
            return Path(self.default_kw_path)
        return None

    def _resolve_llm_client(self, model_str: str):
        """解析 "提供商:模型"：两段都认「人类可读名」（webui 里看到的名字）。

        · 提供商：实例 ID 或 webui 显示名
        · 模型：内部稳定 ID，或按 2.x 习惯填的可读模型名（3.0 起在 model_name）

        3.0 rc.1 的框架解析只认「实例 ID:内部 ID」；2.x 直传即成功、不进反查分支。
        """
        try:
            client = self.ctx.get_llm_client(model_str)
            if client is not None:
                return client
        except Exception:
            pass
        if ":" not in (model_str or ""):
            return None
        pid_raw, mid_raw = (s.strip() for s in model_str.split(":", 1))
        if not pid_raw or not mid_raw:
            return None
        pm = getattr(self.ctx, "provider_mgr", None)
        if pm is None:
            return None
        real_pid = pid_raw if pm.get_provider(pid_raw) is not None else None
        if real_pid is None:
            try:
                providers = pm.get_all_providers() or {}
            except Exception:
                providers = {}
            key = re.sub(r"\s+", "", pid_raw)
            cands = []
            for pid, prov in providers.items():
                name = (getattr(prov, "provider_name", "") or "").strip()
                if not name:
                    continue
                name_norm = re.sub(r"\s+", "", name)
                if name == pid_raw:
                    score = 0
                elif key and name_norm == key:
                    score = 1
                elif key and (key in name_norm or name_norm in key):
                    score = 2
                else:
                    continue
                cands.append((score, len(name), pid))
            if cands:
                cands.sort()
                real_pid = cands[0][2]
        if real_pid is None:
            logger.warning("[kira-wiki] 找不到服务商 %r（可填 webui 显示名或实例 ID），回退默认 LLM", pid_raw)
            return None
        if pm.get_model_info(real_pid, mid_raw, "llm") is not None:
            return self.ctx.get_llm_client(f"{real_pid}:{mid_raw}")
        for mi in (pm.get_model_infos(real_pid) or []):
            if getattr(mi, "model_name", None) == mid_raw and getattr(mi, "model_id", None):
                return self.ctx.get_llm_client(f"{real_pid}:{mi.model_id}")
        logger.warning("[kira-wiki] 服务商 %r 下找不到模型 %r，回退默认 LLM", real_pid, mid_raw)
        return None

    async def _llm(self, prompt: str, system: str = "") -> str:
        client = self.ctx.get_default_llm_client()
        if self.llm_model:
            client = self._resolve_llm_client(self.llm_model) or client
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

    # ==================================================================
    # 工具（4 个：init / ingest / query / status）
    # ==================================================================

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
        kw = Path(path).expanduser()
        try:
            await wc.to_thread(kw.mkdir, parents=True, exist_ok=True)
            await wc.to_thread(wc.ensure_structure, kw)
            self._registry[name] = str(kw)
            await wc.to_thread(self._save_registry)
            await wc.to_thread(wc.append_log, kw, f"初始化知识库 {name} @ {kw}")
            n = await wc.to_thread(wc.rebuild_index, kw)
        except Exception as e:
            logger.error(f"[kira-wiki] init failed: {e}")
            return f"初始化失败：{e}"
        return (f"知识库 [{name}] 已就绪 @ {kw}\n"
                f"目录：raw/（素材） + wiki/（LLM 生成，{n} 个页面位）\n"
                f"用 Obsidian 打开 {kw} 即可浏览图谱")

    @register.tool(
        name="wiki_ingest",
        description="收录素材到知识库：内容写入 raw/，LLM 自动生成摘要页/实体页/概念页并维护交叉引用。用户说'收录这篇文章/把XX存进知识库'时调用。",
        params={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "素材内容（文本/文章/笔记正文）"},
                "title": {"type": "string", "description": "素材标题，用于命名文件"},
                "kw_name": {"type": "string", "description": "知识库名称，留空用默认知识库"},
                "source_url": {"type": "string", "description": "素材来源链接（可选）"},
            },
            "required": ["content", "title"],
        },
    )
    async def wiki_ingest(self, event: KiraMessageBatchEvent, content: str, title: str,
                          kw_name: str = "", source_url: str = "") -> str:
        kw = self._get_kw(kw_name)
        if kw is None:
            return ("找不到知识库" if kw_name else "还没有知识库，先 /wiki init 或告诉我知识库路径")
        await wc.to_thread(wc.ensure_structure, kw)

        slug = wc.slugify(title)
        raw_content = f"# {title}\n\n"
        if source_url:
            raw_content += f"> 来源：{source_url}\n\n"
        raw_content += content
        raw_path = kw / "raw" / f"{slug}.md"
        existed = raw_path.exists()
        await wc.to_thread(raw_path.write_text, raw_content, "utf-8")

        prompt = (
            f"素材标题：{title}\n"
            f"素材内容：\n{content[:12000]}\n\n"
            "请生成知识库页面。实体和概念页各最多 5 个，每个页面 100-300 字。"
        )
        llm_out = await self._llm(prompt, system=SYSTEM_COMPILER)
        if not llm_out:
            await wc.to_thread(wc.append_log, kw, f"收录 {title}（LLM 生成失败，仅存 raw）")
            return f"素材已存入 raw/{slug}.md，但 LLM 生成失败，稍后再试"

        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", llm_out.strip())
        data = None
        try:
            data = json.loads(cleaned)
        except Exception:
            m = re.search(r"\{.*\}", cleaned, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
        if not isinstance(data, dict):
            await wc.to_thread(wc.append_log, kw, f"收录 {title}（JSON 解析失败）")
            return f"素材已存入 raw/{slug}.md，但 LLM 输出解析失败（可重试一次）"

        wiki = kw / "wiki"
        written = 0

        def _write_pages() -> int:
            n = 0
            summary = data.get("summary") or ""
            if isinstance(summary, str) and summary.strip():
                (wiki / "sources" / f"{slug}.md").write_text(summary, "utf-8")
                n += 1
            for ent in _as_list(data.get("entities"), 5):
                if not isinstance(ent, dict):
                    continue
                t = ent.get("title") or ""
                body = ent.get("content") or ""
                if t and isinstance(body, str) and body.strip():
                    (wiki / "entities" / f"{wc.slugify(t)}.md").write_text(body, "utf-8")
                    n += 1
            for con in _as_list(data.get("concepts"), 5):
                if not isinstance(con, dict):
                    continue
                t = con.get("title") or ""
                body = con.get("content") or ""
                if t and isinstance(body, str) and body.strip():
                    (wiki / "concepts" / f"{wc.slugify(t)}.md").write_text(body, "utf-8")
                    n += 1
            ana = data.get("analysis") or ""
            if isinstance(ana, str) and ana.strip():
                (wiki / "analyses" / f"{slug}-analysis.md").write_text(ana, "utf-8")
                n += 1
            return n

        written = await wc.to_thread(_write_pages)

        def _touch_overview():
            overview = wiki / "overview.md"
            with overview.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## {title}（{_now()}）\n\n- 来源：[[sources/{slug}]]\n")
                for ent in _as_list(data.get("entities"), 5):
                    t = ent.get("title") if isinstance(ent, dict) else None
                    if t:
                        fh.write(f"- 实体：[[entities/{wc.slugify(t)}]]\n")
                for con in _as_list(data.get("concepts"), 5):
                    t = con.get("title") if isinstance(con, dict) else None
                    if t:
                        fh.write(f"- 概念：[[concepts/{wc.slugify(t)}]]\n")

        await wc.to_thread(_touch_overview)
        count = await wc.to_thread(wc.rebuild_index, kw)
        await wc.to_thread(wc.append_log, kw, f"收录 {title}，生成 {written} 个页面")
        note = "（原同名素材已更新）" if existed else ""
        return (f"收录完成{note}：{title}\n"
                f"raw/{slug}.md + wiki 生成 {written} 个页面（摘要/实体/概念/分析），索引现有 {count} 页\n"
                f"面板或 Obsidian 里刷新即可看到")

    @register.tool(
        name="wiki_query",
        description="查询知识库：按相关度检索 wiki 页面并回答，标注来源。用户问'知识库里XX是什么/查一下wiki'时调用。",
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要查询的问题"},
                "kw_name": {"type": "string", "description": "知识库名称，留空用默认"},
            },
            "required": ["question"],
        },
    )
    async def wiki_query(self, event: KiraMessageBatchEvent, question: str,
                         kw_name: str = "") -> str:
        kw = self._get_kw(kw_name)
        if kw is None:
            return "还没有知识库，先初始化一个"
        picked, meta = await wc.to_thread(wc.rank_pages, kw, question)
        if not picked:
            return "知识库里还没有可用页面，先收录点素材"
        context = "\n\n".join(f"### {p['title']}（{p['rel']}）\n{p['text']}" for p in picked)
        prompt = (f"知识库页面（按相关度排序，共 {len(picked)} 页）：\n{context}\n\n"
                  f"问题：{question}\n\n请回答并标注来源页面。")
        answer = await self._llm(prompt, system=SYSTEM_QA)
        if not answer:
            return "查询失败，LLM 没响应，稍后再试"
        src = "、".join(p["rel"] for p in picked[:5])
        return f"{answer}\n\n— 参考页面：{src}"

    @register.tool(
        name="wiki_status",
        description="知识库状态与体检：列出已有页面、素材数量；check=true 时额外做健康检查（空页/断链/孤立页）。用户问'知识库里有什么/检查知识库/体检'时调用。",
        params={
            "type": "object",
            "properties": {
                "kw_name": {"type": "string", "description": "知识库名称，留空用默认"},
                "check": {"type": "boolean", "description": "是否做完整体检（默认 false，只列概览）"},
            },
        },
    )
    async def wiki_status(self, event: KiraMessageBatchEvent, kw_name: str = "",
                          check: bool = False) -> str:
        kw = self._get_kw(kw_name)
        if kw is None:
            return "还没有知识库"
        await wc.to_thread(wc.ensure_structure, kw)
        if not check:
            pages = await wc.to_thread(wc.iter_pages, kw)
            st = await wc.to_thread(wc.stats, kw)
            by = {s: [p.name for p in pages if p.section == s] for s in wc.SECTIONS}
            lines = [f"知识库 @ {kw}",
                     f"素材 raw: {st['raw']} | 共 {st['pages']} 页（{st['bytes']} 字节）"]
            for s in wc.SECTIONS:
                names = by[s]
                lines.append(f"{wc.SECTION_LABEL[s]}（{len(names)}）：{'、'.join(names[:12]) or '空'}")
            lines.append("要健康检查就说「体检查一下」，或用面板的体检页")
            return "\n".join(lines)
        rep = await wc.to_thread(wc.lint, kw)
        st = rep["stats"]
        lines = [f"知识库体检 @ {kw}",
                 f"素材 raw: {st['raw']} | 摘要: {st['by_section']['sources']} | 实体: {st['by_section']['entities']}"
                 f" | 概念: {st['by_section']['concepts']} | 分析: {st['by_section']['analyses']}"]
        issues = rep["issues"]
        if issues:
            lines.append(f"发现 {len(issues)} 个问题：")
            lines.extend(f"- {i['detail']}（{i['page']}）" for i in issues[:20])
        else:
            lines.append("没有发现问题，很健康")
        return "\n".join(lines)

    # ==================================================================
    # 面板 API（供侧边栏页面使用；全部 async + auth）
    # ==================================================================

    def _kw_or_400(self, kw_name: str = "") -> Path:
        from fastapi import HTTPException
        kw = self._get_kw(kw_name)
        if kw is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        return kw

    @register.api(method="GET", path="/library", auth=True)
    async def api_library(self, kw_name: str = ""):
        """面板首页数据：库列表 + 当前库统计 + 最近日志。"""
        def _collect():
            reg = self._registry_view()
            kw = self._get_kw(kw_name)
            data = {"registry": reg, "current": None}
            if kw is not None:
                data["current"] = {
                    "name": kw_name or next((n for n, v in reg.items() if v["path"] == str(kw)), ""),
                    "path": str(kw),
                    "stats": wc.stats(kw),
                    "log": wc.recent_log(kw, 30),
                }
            return data
        return await wc.to_thread(_collect)

    @register.api(method="GET", path="/tree", auth=True)
    async def api_tree(self, kw_name: str = ""):
        kw = self._kw_or_400(kw_name)
        def _collect():
            pages = [p.as_dict() for p in wc.iter_pages(kw)]
            return {"stats": wc.stats(kw), "pages": pages, "sections": list(wc.SECTIONS),
                    "labels": wc.SECTION_LABEL}
        return await wc.to_thread(_collect)

    @register.api(method="GET", path="/graph", auth=True)
    async def api_graph(self, kw_name: str = ""):
        kw = self._kw_or_400(kw_name)
        return await wc.to_thread(wc.build_graph, kw)

    @register.api(method="GET", path="/page", auth=True)
    async def api_page(self, rel: str, kw_name: str = ""):
        from fastapi import HTTPException
        kw = self._kw_or_400(kw_name)
        try:
            return await wc.to_thread(wc.read_page, kw, rel)
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/page", auth=True)
    async def api_page_save(self, body: dict):
        """热编辑保存：body = {rel, content, kw?}"""
        from fastapi import HTTPException
        body = body or {}
        kw = self._kw_or_400(body.get("kw") or "")
        rel, content = body.get("rel") or "", body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        try:
            path = await wc.to_thread(wc.write_page, kw, rel, content)
            await wc.to_thread(wc.append_log, kw, f"面板编辑 {rel}")
            return {"ok": True, "rel": rel, "size": path.stat().st_size, "saved_at": _now()}
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/page/new", auth=True)
    async def api_page_new(self, body: dict):
        from fastapi import HTTPException
        body = body or {}
        kw = self._kw_or_400(body.get("kw") or "")
        section = (body.get("section") or "concepts").strip()
        title = (body.get("title") or "").strip()
        if section not in wc.SECTIONS or not title:
            raise HTTPException(status_code=400, detail="section/title invalid")
        rel = f"{section}/{wc.slugify(title)}.md"
        body_text = wc.parse_frontmatter("")[1]
        content = (f"---\ntitle: {title}\ntags: []\n---\n\n# {title}\n\n")
        try:
            await wc.to_thread(wc.write_page, kw, rel, content)
            await wc.to_thread(wc.rebuild_index, kw)
            await wc.to_thread(wc.append_log, kw, f"面板新建 {rel}")
            return {"ok": True, "rel": rel}
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/page/delete", auth=True)
    async def api_page_delete(self, body: dict):
        from fastapi import HTTPException
        body = body or {}
        kw = self._kw_or_400(body.get("kw") or "")
        rel = body.get("rel") or ""
        try:
            await wc.to_thread(wc.delete_page, kw, rel)
            await wc.to_thread(wc.rebuild_index, kw)
            await wc.to_thread(wc.append_log, kw, f"面板删除 {rel}")
            return {"ok": True, "rel": rel}
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/index/rebuild", auth=True)
    async def api_rebuild(self, body: dict = None):
        kw = self._kw_or_400((body or {}).get("kw") or "")
        n = await wc.to_thread(wc.rebuild_index, kw)
        await wc.to_thread(wc.append_log, kw, "面板重建索引")
        return {"ok": True, "pages": n}

    @register.api(method="GET", path="/lint", auth=True)
    async def api_lint(self, kw_name: str = ""):
        kw = self._kw_or_400(kw_name)
        return await wc.to_thread(wc.lint, kw)

    @register.api(method="GET", path="/search", auth=True)
    async def api_search(self, q: str, kw_name: str = ""):
        kw = self._kw_or_400(kw_name)
        picked, meta = await wc.to_thread(wc.rank_pages, kw, q or "", top_k=12, per_page_chars=400)
        return {"query": q, "hits": [{"rel": p["rel"], "title": p["title"], "section": p["section"],
                                      "score": p["score"], "snippet": p["text"][:300]} for p in picked],
                "meta": meta}

    @register.api(method="POST", path="/query", auth=True)
    async def api_query(self, body: dict):
        """面板问答：走与工具同一条检索 + LLM 链路。"""
        kw = self._kw_or_400((body or {}).get("kw") or "")
        question = ((body or {}).get("question") or "").strip()
        if not question:
            return {"ok": False, "error": "question required"}
        picked, meta = await wc.to_thread(wc.rank_pages, kw, question)
        if not picked:
            return {"ok": False, "error": "no_pages"}
        context = "\n\n".join(f"### {p['title']}（{p['rel']}）\n{p['text']}" for p in picked)
        prompt = (f"知识库页面（按相关度排序，共 {len(picked)} 页）：\n{context}\n\n"
                  f"问题：{question}\n\n请回答并标注来源页面。")
        answer = await self._llm(prompt, system=SYSTEM_QA)
        if not answer:
            return {"ok": False, "error": "llm_unavailable"}
        return {"ok": True, "answer": answer,
                "sources": [{"rel": p["rel"], "title": p["title"], "score": p["score"]} for p in picked],
                "meta": meta}

    # ==================================================================
    # 侧边栏页面
    # ==================================================================

    @register.page(
        "/index",
        menu=PageMenu(
            label={"zh": "知识库", "en": "Kira Wiki"},
            icon="assets/menu.svg",
            order=96,
        ),
    )
    def page(self):
        return PluginPage.from_folder("./web")
