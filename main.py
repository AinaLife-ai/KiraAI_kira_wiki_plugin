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
from core.utils.path_utils import get_config_path
from core.provider import LLMRequest
from core.chat.message_utils import KiraMessageBatchEvent

try:                       # 插件以包形式加载（与 acc 的 `from . import` 同款）
    from . import wiki_core as wc
except ImportError:        # 兜底：以脚本形式加载
    import wiki_core as wc

REGISTRY_FILE = "kw_registry.json"
PLUGIN_ID = "KiraAI_kira_wiki_plugin"

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
        """启动：预加载知识库注册表 + 把历史配置整理成当前形态。"""
        try:
            await wc.to_thread(self._load_registry)
            await self._migrate_config()
            logger.info(f"[kira-wiki] initialized, {len(self._registry)} KW(s) registered")
        except Exception as e:
            logger.error(f"[kira-wiki] initialize failed: {e}")

    # ------------------------------------------------------------------
    # 配置：迁移 + 回写（与 schema.json 的字段形态保持一致）
    # ------------------------------------------------------------------

    async def _migrate_config(self) -> None:
        """一次性、幂等：

        ① 清掉早期 schema 被框架误解析时留下的顶层 `properties` 幽灵键
           （旧 schema 是 JSON-Schema 形态，框架按顶层键建字段 ⇒ 配置里只落了 `properties`）；
        ② 手填的「服务商:模型名」→ 规范成「实例ID:内部ID」，让下拉框能正确回显；
        ③ 真改了才回写配置文件（plugin_cfg 与注册表内存里是同一份 dict）。
        """
        changed = False
        legacy = self.plugin_cfg.get("properties")
        if isinstance(legacy, dict):
            for key in ("section_basic", "section_llm"):
                if isinstance(legacy.get(key), dict) and key not in self.plugin_cfg:
                    self.plugin_cfg[key] = legacy[key]
            self.plugin_cfg.pop("properties", None)
            changed = True
        elif legacy is not None:
            self.plugin_cfg.pop("properties", None)
            changed = True

        if self.llm_model:
            canonical, label, err = await wc.to_thread(self._canonical_model_ref, self.llm_model)
            if canonical and canonical != self.llm_model:
                logger.info("[kira-wiki] 模型配置 %r 已规范化为 %r（%s）",
                            self.llm_model, canonical, label)
                self.llm_model = canonical
                self.plugin_cfg.setdefault("section_llm", {})["model"] = canonical
                changed = True
            elif err:
                logger.warning("[kira-wiki] 模型配置 %r 解析失败：%s（将回退默认 LLM）", self.llm_model, err)

        if changed:
            ok = await wc.to_thread(self._persist_config)
            if ok:
                logger.info("[kira-wiki] 插件配置已整理并回写")

    def _persist_config(self) -> bool:
        """把内存里的插件配置写回 data/config/plugins/<id>.json（与内置 agent 插件同款做法）。"""
        try:
            path = get_config_path() / "plugins" / f"{PLUGIN_ID}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.plugin_cfg, ensure_ascii=False, indent=4), "utf-8")
            return True
        except Exception as e:
            logger.error(f"[kira-wiki] 配置回写失败: {e}")
            return False

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

    def _model_label(self, ref: str) -> str:
        """把模型引用变成给人看的标签：`服务商显示名 · 可读模型名`（解析不出就退回引用本身）。"""
        try:
            if not ref or ":" not in ref:
                return ref or ""
            pid, mid = ref.split(":", 1)
            pm = getattr(self.ctx, "provider_mgr", None)
            name = pid
            if pm is not None:
                prov = pm.get_provider(pid)
                if prov is not None:
                    name = getattr(prov, "provider_name", "") or pid
                for mi in (pm.get_model_infos(pid) or []):
                    if getattr(mi, "model_id", "") == mid:
                        return f"{name} · {getattr(mi, 'model_name', '') or mid}"
            return f"{name} · {mid}"
        except Exception:
            return ref

    def _canonical_model_ref(self, model_str: str):
        """把配置里的模型值规范成「实例ID:内部ID」，并给出可读标签。

        返回 (canonical, label, err)：
          · canonical —— 能直接喂给 ctx.get_llm_client() 的引用（解析不出为 ""）
          · label     —— 给人看的 `服务商显示名 · 可读模型名`
          · err       —— 解析失败原因（成功为 None）

        两代通用：先直传（2.x 下配置值本就是可读名，一次命中），再走显示名解析——
        3.0 rc.1 的框架只认「实例ID:内部ID」，而用户在 webui 里只会看到显示名。
        """
        raw = (model_str or "").strip()
        if not raw:
            return "", "", None
        try:                                    # ① 直传：当前框架能解析就已是规范值
            if self.ctx.get_llm_client(raw) is not None:
                return raw, self._model_label(raw), None
        except Exception:
            pass
        if ":" not in raw:
            return "", "", "格式应为「服务商:模型」"
        pid_raw, mid_raw = (s.strip() for s in raw.split(":", 1))
        if not pid_raw or not mid_raw:
            return "", "", "格式应为「服务商:模型」"
        pm = getattr(self.ctx, "provider_mgr", None)
        if pm is None:
            return "", "", "无法访问模型管理器"

        # ② 服务商：实例 ID 直查 → 显示名精确 → 去空白精确 → 包含（多候选取名字最短者）
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
            return "", "", f"找不到服务商「{pid_raw}」（可填 webui 显示名或实例 ID）"

        # ③ 模型：内部 ID 直查 → 该服务商下按可读名（model_name）反查
        if pm.get_model_info(real_pid, mid_raw, "llm") is not None:
            ref = f"{real_pid}:{mid_raw}"
            return ref, self._model_label(ref), None
        for mi in (pm.get_model_infos(real_pid) or []):
            if getattr(mi, "model_name", None) == mid_raw and getattr(mi, "model_id", None):
                ref = f"{real_pid}:{mi.model_id}"
                return ref, self._model_label(ref), None
        return "", "", f"服务商「{real_pid}」下找不到模型「{mid_raw}」"

    def _resolve_llm_client(self, model_str: str):
        """解析配置 → 客户端；解析不出来返回 None（由调用方回退默认 LLM）。"""
        canonical, _label, err = self._canonical_model_ref(model_str)
        if canonical:
            try:
                client = self.ctx.get_llm_client(canonical)
                if client is not None:
                    return client
            except Exception:
                pass
        if err:
            logger.warning("[kira-wiki] 模型配置 %r 解析失败（%s），回退默认 LLM", model_str, err)
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
            # 模型：与配置同源（下拉选的就是这个值），面板据此显示「当前生成模型」
            canonical, label, err = self._canonical_model_ref(self.llm_model)
            data["model"] = {
                "value": self.llm_model or "",
                "canonical": canonical,
                "label": label or ("KiraAI 默认 LLM" if not self.llm_model else (self.llm_model or "")),
                "ok": bool(canonical) or not self.llm_model,
                "error": err,
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
            label={"zh": "知识晶格", "en": "Kira Wiki"},
            icon="assets/menu.svg",
            order=96,
        ),
    )
    def page(self):
        return PluginPage.from_folder("./web")
