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

v1.3.0
  · 工具精简 7 → 4（write / read / ingest / query）——每轮请求注入的 schema 少 ~32%
  · bot 可自写：wiki_write 支持 replace / append / delete / init，全程零模型调用
  · wiki_query 可纯检索（ask=false）：零模型调用，返回紧凑命中列表，细节用 wiki_read 钻取
  · 只读操作绝不落盘；外部笔记（已有 Obsidian 库）与生成区一同进图谱与检索
  · 全部磁盘操作移出事件循环（asyncio.to_thread）
  · 侧边栏面板：自举引导 / 库切换 / 晶格视图 / 阅读 / 热编辑 / 体检 / 问答
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.plugin import BasePlugin, PageMenu, PluginPage, logger, register
from core.utils.path_utils import get_config_path, get_root_path
from core.provider import LLMRequest
from core.chat.message_utils import KiraMessageBatchEvent

try:                       # 插件以包形式加载（与 acc 的 `from . import` 同款）
    from . import wiki_core as wc
except ImportError:        # 兜底：以脚本形式加载
    import wiki_core as wc

REGISTRY_FILE = "kw_registry.json"
PLUGIN_ID = "KiraAI_kira_wiki_plugin"
DEFAULT_KW_DIR = "data/kw"          # 未配置 default_kw_path 时的落点（相对 KiraAI 根目录）

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


def _kira_root() -> Path:
    """KiraAI 运行根目录（解析相对路径用，不依赖进程 CWD）。"""
    try:
        return Path(get_root_path()).resolve()
    except Exception:
        return Path.cwd().resolve()


def _default_kw_path() -> str:
    """未配置时的默认落点：<KiraAI 根>/data/kw。"""
    return str(_kira_root() / DEFAULT_KW_DIR)


def _abspath(p: str) -> str:
    """把用户/模型给的路径归一成绝对路径（相对路径按 KiraAI 根解析）。"""
    raw = (p or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else (_kira_root() / path).resolve())


def _plan_pages(data: dict, slug: str, per_section: int) -> list:
    """把生成模型返回的 JSON 变成「待写入页面」清单：(kind, title, rel, body)。"""
    plan: list = []
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        plan.append(("summary", "", f"sources/{slug}.md", summary))
    for ent in _as_list(data.get("entities"), per_section):
        if not isinstance(ent, dict):
            continue
        t, body = ent.get("title") or "", ent.get("content") or ""
        if t and isinstance(body, str) and body.strip():
            plan.append(("entities", t, f"entities/{wc.slugify(t)}.md", body))
    for con in _as_list(data.get("concepts"), per_section):
        if not isinstance(con, dict):
            continue
        t, body = con.get("title") or "", con.get("content") or ""
        if t and isinstance(body, str) and body.strip():
            plan.append(("concepts", t, f"concepts/{wc.slugify(t)}.md", body))
    ana = data.get("analysis")
    if isinstance(ana, str) and ana.strip():
        plan.append(("analysis", "", f"analyses/{slug}-analysis.md", ana))
    return plan


def _trim_plan(plan: list, limit: int) -> list:
    """按 max_pages_per_ingest 截断：优先保留摘要与分析，再按顺序补实体/概念。"""
    limit = max(1, int(limit or 1))
    if len(plan) <= limit:
        return plan
    head = [p for p in plan if p[0] in ("summary", "analysis")]
    mid = [p for p in plan if p[0] in ("entities", "concepts")]
    if len(head) >= limit:
        return head[:limit]
    return head + mid[:max(0, limit - len(head))]


def _as_list(value, limit: int) -> list:
    """LLM 输出防御：只接受 list，其余（None/dict/str）一律当空。"""
    if not isinstance(value, list):
        return []
    return value[:limit]


class KiraWikiPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        sec = cfg.get("section_basic", {})
        self.enabled = bool(sec.get("enabled", True))
        # 默认知识库目录：留空 ⇒ <KiraAI 根>/data/kw；相对路径按 KiraAI 根解析（不依赖 CWD）
        self.default_kw_path = _abspath(sec.get("default_kw_path", "")) or _default_kw_path()
        llm_sec = cfg.get("section_llm", {})
        self.llm_model = llm_sec.get("model", "") or ""
        try:
            self.max_pages = max(1, int(llm_sec.get("max_pages_per_ingest", 15) or 15))
        except (TypeError, ValueError):
            self.max_pages = 15
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

    def _kw_candidates(self, name: str = "") -> list:
        """按优先级给出候选知识库目录（默认路径优先，其次注册表）。"""
        self._load_registry()
        out: list = []
        if name:
            if name in (self._registry or {}):
                out.append(Path(self._registry[name]))
            return out
        if self.default_kw_path:
            out.append(Path(self.default_kw_path))
        for path in (self._registry or {}).values():
            p = Path(path)
            if all(str(p) != str(x) for x in out):
                out.append(p)
        return out

    def _get_kw(self, name: str = "", *, create: bool = False) -> Optional[Path]:
        """取知识库根目录。

        · create=False（只读场景）：目录不存在 ⇒ None，**绝不落盘**——扫库/体检/看图
          不会在用户的目录里创建任何东西；
        · create=True（写场景）：目录不存在 ⇒ 建好 raw/ 与 wiki/ 骨架再返回，支持零配置自举；
        · name 给了但没注册 ⇒ 一律 None（绝不静默改写到别的库）。
        """
        cands = self._kw_candidates(name)
        for p in cands:
            if p.is_dir():
                return p
        if create and cands:
            kw = cands[0]
            try:
                kw.mkdir(parents=True, exist_ok=True)
                wc.ensure_structure(kw)
            except Exception as e:
                logger.error(f"[kira-wiki] 创建知识库失败 {kw}: {e}")
                return None
            logger.info(f"[kira-wiki] 已按需创建知识库 {kw}")
            return kw
        return None

    def _register_kw(self, name: str, path: Path) -> None:
        """登记知识库（路径一律归一成绝对路径，避免依赖进程 CWD）。"""
        self._load_registry()
        self._registry[name] = str(Path(path).resolve())

    def _guess_kw_name(self, path: Path) -> str:
        """给未命名的知识库取一个不冲突的名字（默认取目录名）。"""
        self._load_registry()
        base = Path(path).name or "知识库"
        name, i = base, 2
        while name in (self._registry or {}) and str(Path(self._registry[name]).resolve()) != str(Path(path).resolve()):
            name, i = f"{base}-{i}", i + 1
        return name

    def _ensure_kb_path(self, path: str = "") -> Path:
        """确定要创建/使用的知识库目录：显式路径 > 默认路径。"""
        return Path(_abspath(path) or self.default_kw_path or _default_kw_path())

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
        """调用生成模型。任何环节不可用都返回空串——调用方据此降级，
        绝不让「模型拿不到」变成工具报错（bot 仍可自己用 wiki_write 写页）。"""
        try:
            client = self.ctx.get_default_llm_client()
            if self.llm_model:
                client = self._resolve_llm_client(self.llm_model) or client
        except Exception as e:
            logger.warning(f"[kira-wiki] 获取生成模型失败：{e}")
            return ""
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

    # ------------------------------------------------------------------
    # 工具实现（4 个：write / read / ingest / query）
    # ------------------------------------------------------------------

    async def _tool_init(self, path: str = "", name: str = "") -> str:
        """建库 / 接管目录（wiki_write mode=init）。"""
        kw = self._ensure_kb_path(path)
        name = (name or "").strip() or self._guess_kw_name(kw)
        try:
            await wc.to_thread(kw.mkdir, parents=True, exist_ok=True)
            await wc.to_thread(wc.ensure_structure, kw)
            self._register_kw(name, kw)
            await wc.to_thread(self._save_registry)
            await wc.to_thread(wc.append_log, kw, f"初始化知识库 {name} @ {kw}")
            n = await wc.to_thread(wc.rebuild_index, kw)
            pages = await wc.to_thread(wc.iter_pages, kw)
        except Exception as e:
            logger.error(f"[kira-wiki] init failed: {e}")
            return f"初始化失败：{e}"
        ext = sum(1 for p in pages if p.origin == wc.ORIGIN_VAULT)
        extra = f"，另有 {ext} 篇已有笔记（可读可写）" if ext else ""
        return (f"知识库 [{name}] 已就绪 @ {kw}\n"
                f"目录：raw/（素材） + wiki/（生成区，{n} 个页面位）{extra}\n"
                f"用 Obsidian 打开 {kw} 即可浏览图谱")

    async def _tool_read(self, ref: str, check: bool, kw_name: str) -> str:
        """读页 / 列目录 / 体检（wiki_read）。"""
        kw = self._get_kw(kw_name)
        if kw is None:
            return "还没有知识库（可用 wiki_write mode=init 建一个，路径可留空）"
        if ref:
            hit = await wc.to_thread(self._find_page, kw, ref)
            if hit is None:
                return f"找不到页面：{ref}（可先用 wiki_read 不带 ref 看现有页面）"
            try:
                pg = await wc.to_thread(wc.read_page, kw, hit["rel"], origin=hit["origin"])
            except wc.WikiPathError as e:
                return f"读取失败：{e}"
            body = pg["content"]
            if len(body) > 6000:
                body = body[:6000] + "\n…（截断）"
            return f"# {pg['rel']}\n\n{body}"

        pages = await wc.to_thread(wc.iter_pages, kw)
        st = await wc.to_thread(wc.stats, kw, pages)
        by: dict = {}
        for p in pages:
            by.setdefault(p.section, []).append(p.name)
        gen = st["section_pages"] - st["by_section"].get(wc.IMPORTED, 0)
        lines = [f"知识库 @ {kw}",
                 f"素材 raw: {st['raw']} | 生成区 {gen} 页 | 外部笔记 "
                 f"{st['by_section'].get(wc.IMPORTED, 0)} 篇 | 合计 {st['pages']} 页（{st['bytes']} 字节）"]
        for s in wc.SECTIONS:
            names = by.get(s, [])
            lines.append(f"{wc.SECTION_LABEL[s]}（{len(names)}）：{'、'.join(names[:12]) or '空'}")
        ext = by.get(wc.IMPORTED, [])
        if ext:
            lines.append(f"{wc.SECTION_LABEL[wc.IMPORTED]}（{len(ext)}）：{'、'.join(ext[:12])}"
                         + ("…" if len(ext) > 12 else ""))
        if not st["root_pages"]:
            lines.append("提示：该目录还没有 wiki/ 生成区，wiki_write mode=init 即可创建")
        if not check:
            lines.append("要完整体检就带 check=true")
            return "\n".join(lines)

        rep = await wc.to_thread(wc.lint, kw)
        issues = rep["issues"]
        if issues:
            lines.append(f"体检发现 {len(issues)} 个问题：")
            lines.extend(f"- {i['detail']}（{i['page']}）" for i in issues[:20])
        else:
            lines.append("体检：没有发现问题，很健康")
        return "\n".join(lines)

    async def _tool_delete(self, ref: str, kw_name: str) -> str:
        """删除页面（wiki_write mode=delete）。"""
        kw = self._get_kw(kw_name)
        if kw is None:
            return "还没有知识库"
        hit = await wc.to_thread(self._find_page, kw, ref)
        if hit is None:
            return f"找不到页面：{ref}"
        try:
            await wc.to_thread(wc.delete_page, kw, hit["rel"], origin=hit["origin"])
            await wc.to_thread(wc.rebuild_index, kw)
            await wc.to_thread(wc.append_log, kw, f"删除 {hit['rel']}")
        except wc.WikiPathError as e:
            return f"删除失败：{e}"
        return f"已删除 {hit['rel']}"

    @register.tool(
        name="wiki_write",
        description="写知识库页面，或建库/接管目录（内容由自己写，不调模型）。",
        params={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["replace", "append", "delete", "init"],
                         "description": "replace 覆盖(默认)/append 追加/delete 删页/init 建库或接管"},
                "rel": {"type": "string", "description": "页面路径：生成区 sources|entities|concepts|analyses/名.md；自己的笔记 名.md（含子目录）同样可写；delete 可给标题"},
                "content": {"type": "string", "description": "markdown 正文（replace/append 必填），带 [[wikilink]] 更好"},
                "path": {"type": "string", "description": "仅 init：库根目录，省略=默认 data/kw（指向已有 vault 会载入其笔记）"},
                "name": {"type": "string", "description": "仅 init：库名，省略取目录名"},
                "kw_name": {"type": "string", "description": "库名，留空=默认"},
            },
            "required": ["mode"],
        },
    )
    async def wiki_write(self, event: KiraMessageBatchEvent, mode: str = "replace", rel: str = "",
                         content: str = "", path: str = "", name: str = "",
                         kw_name: str = "") -> str:
        if self._disabled():
            return self._disabled_msg()
        mode = (mode or "replace").strip().lower()
        if mode == "init":
            return await self._tool_init(path, name)
        if mode == "delete":
            if not rel:
                return "删除需要给 rel（页面相对路径或标题）"
            return await self._tool_delete(rel, kw_name)
        if mode not in ("replace", "append"):
            return f"未知 mode：{mode}（可用 replace / append / delete / init）"
        if not rel or not isinstance(content, str) or not content:
            return "写入需要同时给 rel 与 content"
        kw = self._get_kw(kw_name, create=True)
        if kw is None:
            return ("找不到知识库" if kw_name else "还没有知识库，先 wiki_write mode=init（路径可留空）")
        try:
            if mode == "append":
                p, origin = await wc.to_thread(wc.append_page, kw, rel, content)
            else:
                p, origin = await wc.to_thread(wc.write_page, kw, rel, content)
            await wc.to_thread(wc.rebuild_index, kw)
            await wc.to_thread(wc.append_log, kw, f"{'追加' if mode == 'append' else '写入'} {rel}")
        except wc.WikiPathError as e:
            return f"写入失败：{e}"
        except Exception as e:
            logger.error(f"[kira-wiki] write failed: {e}")
            return f"写入失败：{e}"
        where = "生成区" if origin == wc.ORIGIN_WIKI else "用户笔记区"
        return (f"已{'追加到' if mode == 'append' else '写入'} {rel}"
                f"（{where}，{p.stat().st_size} 字节）\n@ {p}")

    @register.tool(
        name="wiki_read",
        description="读知识库：给 ref 读全文；不给则列页面与统计，check=true 顺带体检。改页前先读。",
        params={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "页面路径或标题；留空=列目录"},
                "check": {"type": "boolean", "description": "列目录时顺带体检，默认 false"},
                "kw_name": {"type": "string", "description": "库名，留空=默认"},
            },
            "required": [],
        },
    )
    async def wiki_read(self, event: KiraMessageBatchEvent, ref: str = "", check: bool = False,
                        kw_name: str = "") -> str:
        if self._disabled():
            return self._disabled_msg()
        return await self._tool_read((ref or "").strip(), bool(check), kw_name)

    @register.tool(
        name="wiki_ingest",
        description="收录素材到 raw/，并用生成模型编译摘要/实体/概念/分析页（模型不可用时只存 raw）。",
        params={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "素材正文"},
                "title": {"type": "string", "description": "素材标题，用作文件名"},
                "kw_name": {"type": "string", "description": "库名，留空=默认"},
                "source_url": {"type": "string", "description": "来源链接，可选"},
            },
            "required": ["content", "title"],
        },
    )
    async def wiki_ingest(self, event: KiraMessageBatchEvent, content: str, title: str,
                          kw_name: str = "", source_url: str = "") -> str:
        if self._disabled():
            return self._disabled_msg()
        kw = self._get_kw(kw_name, create=True)
        if kw is None:
            return ("找不到知识库" if kw_name else "还没有知识库，先 wiki_write mode=init（路径可留空）")
        await wc.to_thread(wc.ensure_structure, kw)

        slug = wc.slugify(title)
        raw_content = f"# {title}\n\n"
        if source_url:
            raw_content += f"> 来源：{source_url}\n\n"
        raw_content += content
        raw_path = kw / "raw" / f"{slug}.md"
        existed = raw_path.exists()
        await wc.to_thread(raw_path.write_text, raw_content, "utf-8")

        limit = max(1, self.max_pages)
        per_section = min(5, max(1, limit - 1))
        prompt = (
            f"素材标题：{title}\n"
            f"素材内容：\n{content[:12000]}\n\n"
            f"请生成知识库页面。实体和概念页各最多 {per_section} 个，整次最多 {limit} 页，每个页面 100-300 字。"
        )
        llm_out = await self._llm(prompt, system=SYSTEM_COMPILER)
        if not llm_out:
            await wc.to_thread(wc.append_log, kw, f"收录 {title}（生成模型不可用，仅存 raw）")
            return (f"素材已存入 raw/{slug}.md。\n"
                    f"生成模型当前不可用 —— 请用 wiki_write 自己整理页面"
                    f"（例如 mode=replace、rel=\"sources/{slug}.md\"，或直接写 entities/concepts 页）。")

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
            return (f"素材已存入 raw/{slug}.md，但生成模型输出不是可解析的 JSON。\n"
                    f"可以用 wiki_write 自己写页面（rel=\"sources/{slug}.md\"）。")

        plan = _trim_plan(_plan_pages(data, slug, per_section), limit)

        def _write_pages() -> int:
            for _kind, _t, rel, body in plan:
                target = kw / "wiki" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, "utf-8")
            return len(plan)

        written = await wc.to_thread(_write_pages)

        def _touch_overview():
            overview = kw / "wiki" / "overview.md"
            if not overview.is_file():
                return
            with overview.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## {title}（{_now()}）\n\n")
                for kind, _t, rel, _b in plan:
                    label = {"summary": "来源", "entities": "实体",
                             "concepts": "概念", "analysis": "分析"}.get(kind, kind)
                    fh.write(f"- {label}：[[{Path(rel).stem}]]\n")

        await wc.to_thread(_touch_overview)
        count = await wc.to_thread(wc.rebuild_index, kw)
        await wc.to_thread(wc.append_log, kw, f"收录 {title}，生成 {written} 个页面")
        note = "（原同名素材已更新）" if existed else ""
        capped = "（已达单次页数上限）" if written >= limit else ""
        return (f"收录完成{note}：{title}\n"
                f"raw/{slug}.md + wiki 生成 {written} 个页面{capped}（摘要/实体/概念/分析），索引现有 {count} 页\n"
                f"面板或 Obsidian 里刷新即可看到")

    async def _search_hits(self, kw: Path, question: str) -> str:
        """纯检索（wiki_query ask=false / 面板 /search 同源）：紧凑命中列表。"""
        hits, meta = await wc.to_thread(
            wc.rank_pages, kw, question, top_k=12, per_page_chars=300)
        if not hits:
            pages = await wc.to_thread(wc.iter_pages, kw)
            n = sum(1 for p in pages if p.section != "root")
            if n == 0:
                return "知识库里还没有可检索页面（根页不参与检索），先收录素材或用 wiki_write 写页"
            return (f"没有命中「{question}」（库里共 {n} 个可检索页面）。"
                    f"换个词试试，或用 wiki_read 不带 ref 看全部页面")
        lines = [f"检索「{question}」命中 {len(hits)} 页（候选 {meta['candidates']} 页）："]
        for i, h in enumerate(hits, 1):
            where = " · 外部笔记" if h.get("origin") == wc.ORIGIN_VAULT else ""
            snippet = " ".join(h["text"][:200].split())
            lines.append(f"{i}. {h['rel']} · {h['title']}{where} [score {h['score']}]\n"
                         f"   {snippet}")
        lines.append("需要全文就用 wiki_read(ref=\"<上面的 rel>\")。")
        return "\n".join(lines)

    @register.tool(
        name="wiki_query",
        description="检索知识库：默认用生成模型写带出处的答案；ask=false 只返回片段。",
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "问题或检索词"},
                "ask": {"type": "boolean", "description": "true(默认)=让模型写答案；false=只返回片段(不调模型)"},
                "kw_name": {"type": "string", "description": "库名，留空=默认"},
            },
            "required": ["question"],
        },
    )
    async def wiki_query(self, event: KiraMessageBatchEvent, question: str,
                         ask: bool = True, kw_name: str = "") -> str:
        if self._disabled():
            return self._disabled_msg()
        kw = self._get_kw(kw_name)
        if kw is None:
            return "还没有知识库，先初始化一个"
        if not (question or "").strip():
            return "需要给 question（检索词或问题）"
        # 纯检索：零模型调用，返回紧凑命中列表（多条 × 短片段），
        # 细节交给 wiki_read 钻取 —— 既不花模型钱，也不撑爆 bot 的主上下文。
        if not ask:
            return await self._search_hits(kw, question)
        picked, meta = await wc.to_thread(wc.rank_pages, kw, question)
        if not picked:
            return "知识库里还没有可用页面，先收录点素材"
        context = "\n\n".join(f"### {p['title']}（{p['rel']}）\n{p['text']}" for p in picked)
        prompt = (f"知识库页面（按相关度排序，共 {len(picked)} 页）：\n{context}\n\n"
                  f"问题：{question}\n\n请回答并标注来源页面。")
        answer = await self._llm(prompt, system=SYSTEM_QA)
        if not answer:
            # 生成模型不可用 ⇒ 退化为「检索 + 原文片段」，至少让 bot 能自己读着回答
            head = "\n\n".join(f"### {p['title']}（{p['rel']}）\n{p['text'][:800]}" for p in picked[:3])
            return (f"生成模型不可用，返回检索到的原文片段（共 {len(picked)} 页命中）：\n\n{head}\n\n"
                    f"— 参考页面：{'、'.join(p['rel'] for p in picked[:5])}")
        src = "、".join(p["rel"] for p in picked[:5])
        return f"{answer}\n\n— 参考页面：{src}"

    # ==================================================================
    # 面板 API（供侧边栏页面使用；全部 async + auth）
    # ==================================================================

    def _disabled(self) -> bool:
        return not self.enabled

    def _disabled_msg(self) -> str:
        return ("知识库插件已停用：请在「设置 → 插件 → Kira Wiki · 知识晶格 → 常规」里"
                "打开「启用插件」。")

    def _require_enabled(self) -> None:
        from fastapi import HTTPException
        if self._disabled():
            raise HTTPException(status_code=409, detail=self._disabled_msg())

    def _kw_or_400(self, kw_name: str = "", *, create: bool = False) -> Path:
        from fastapi import HTTPException
        kw = self._get_kw(kw_name, create=create)
        if kw is None:
            raise HTTPException(status_code=404, detail="knowledge base not found")
        return kw

    def _kw_name_of(self, kw: Path) -> str:
        target = str(Path(kw).resolve())
        for n, v in (self._registry_view() or {}).items():
            try:
                if str(Path(v["path"]).resolve()) == target:
                    return n
            except Exception:
                continue
        return ""

    def _find_page(self, kw: Path, ref: str) -> Optional[dict]:
        """按相对路径或标题/文件名定位页面（供 wiki_read / wiki_write mode=delete 用）。"""
        ref = (ref or "").strip().replace("\\", "/")
        if not ref:
            return None
        pages = wc.iter_pages(kw)
        needle = ref[:-3] if ref.endswith(".md") else ref
        for pg in pages:                      # ① 相对路径精确匹配
            if pg.rel == ref or pg.rel == f"{needle}.md":
                return {"rel": pg.rel, "origin": pg.origin, "section": pg.section,
                        "title": pg.title, "name": pg.name}
        for pg in pages:                      # ② 文件名 / 标题匹配
            if pg.name == needle or pg.title == ref:
                return {"rel": pg.rel, "origin": pg.origin, "section": pg.section,
                        "title": pg.title, "name": pg.name}
        return None

    @register.api(method="GET", path="/library", auth=True)
    async def api_library(self, kw_name: str = ""):
        """面板首页数据：库列表 + 当前库统计 + 最近日志 + 自举信息。

        这是**唯一**不依赖知识库存在性的接口：库不存在时也要 200，
        面板据此渲染「一键创建」引导而不是整屏报错。
        """
        def _collect():
            data = {
                "registry": self._registry_view(),
                "default_path": self.default_kw_path or _default_kw_path(),
                "suggested_path": self.default_kw_path or _default_kw_path(),
                "can_init": True,
                "enabled": self.enabled,
                "current": None,
            }
            kw = self._get_kw(kw_name)
            if kw is not None:
                pages = wc.iter_pages(kw)
                data["current"] = {
                    "name": kw_name or self._kw_name_of(kw) or Path(kw).name,
                    "path": str(kw),
                    "initialized": wc.is_knowledge_base(kw),
                    "is_default": str(Path(kw).resolve()) == str(Path(self.default_kw_path).resolve())
                    if self.default_kw_path else False,
                    "stats": wc.stats(kw, pages),
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

    @register.api(method="POST", path="/kb/init", auth=True)
    async def api_kb_init(self, body: dict = None):
        """面板自举：按需创建/接管知识库。

        body = {name?, path?}——path 省略即用插件设置里的默认路径。
        目录不存在就创建；已存在则直接接管并补齐 raw/ 与 wiki/ 结构（不动已有笔记）。
        """
        from fastapi import HTTPException
        self._require_enabled()
        body = body or {}
        kw = self._ensure_kb_path(body.get("path") or "")
        name = (body.get("name") or "").strip() or self._guess_kw_name(kw)
        try:
            await wc.to_thread(kw.mkdir, parents=True, exist_ok=True)
            await wc.to_thread(wc.ensure_structure, kw)
            self._register_kw(name, kw)
            await wc.to_thread(self._save_registry)
            await wc.to_thread(wc.append_log, kw, f"面板创建知识库 {name} @ {kw}")
            n = await wc.to_thread(wc.rebuild_index, kw)
            pages = await wc.to_thread(wc.iter_pages, kw)
        except Exception as e:
            logger.error(f"[kira-wiki] kb init failed: {e}")
            raise HTTPException(status_code=500, detail=f"创建失败：{e}")
        ext = sum(1 for p in pages if p.origin == wc.ORIGIN_VAULT)
        return {"ok": True, "name": name, "path": str(kw), "pages": n, "external": ext}

    @register.api(method="GET", path="/tree", auth=True)
    async def api_tree(self, kw_name: str = ""):
        self._require_enabled()
        kw = self._kw_or_400(kw_name)

        def _collect():
            pages = wc.iter_pages(kw)          # 只扫一次，stats 复用
            sections = list(wc.SECTIONS)
            if any(p.section == wc.IMPORTED for p in pages):
                sections.append(wc.IMPORTED)
            return {"stats": wc.stats(kw, pages), "pages": [p.as_dict() for p in pages],
                    "sections": sections, "labels": wc.SECTION_LABEL,
                    "path": str(kw), "initialized": wc.is_knowledge_base(kw)}
        return await wc.to_thread(_collect)

    @register.api(method="GET", path="/graph", auth=True)
    async def api_graph(self, kw_name: str = ""):
        self._require_enabled()
        kw = self._kw_or_400(kw_name)
        return await wc.to_thread(wc.build_graph, kw)

    @register.api(method="GET", path="/page", auth=True)
    async def api_page(self, rel: str, kw_name: str = "", origin: str = ""):
        from fastapi import HTTPException
        self._require_enabled()
        kw = self._kw_or_400(kw_name)
        try:
            return await wc.to_thread(wc.read_page, kw, rel, origin)
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/page", auth=True)
    async def api_page_save(self, body: dict):
        """热编辑保存：body = {rel, content, kw?, origin?}"""
        from fastapi import HTTPException
        self._require_enabled()
        body = body or {}
        kw = self._kw_or_400(body.get("kw") or "")
        rel, content = body.get("rel") or "", body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        try:
            path, origin = await wc.to_thread(
                wc.write_page, kw, rel, content, origin=body.get("origin") or "")
            await wc.to_thread(wc.append_log, kw, f"面板编辑 {rel}")
            return {"ok": True, "rel": rel, "origin": origin,
                    "size": path.stat().st_size, "saved_at": _now()}
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/page/new", auth=True)
    async def api_page_new(self, body: dict):
        from fastapi import HTTPException
        self._require_enabled()
        body = body or {}
        kw = self._kw_or_400(body.get("kw") or "", create=True)
        section = (body.get("section") or "concepts").strip()
        title = (body.get("title") or "").strip()
        if section not in wc.SECTIONS or not title:
            raise HTTPException(status_code=400, detail="section/title invalid")
        rel = f"{section}/{wc.slugify(title)}.md"
        content = (f"---\ntitle: {title}\ntags: []\n---\n\n# {title}\n\n")
        try:
            await wc.to_thread(wc.write_page, kw, rel, content)
            await wc.to_thread(wc.rebuild_index, kw)
            await wc.to_thread(wc.append_log, kw, f"面板新建 {rel}")
            return {"ok": True, "rel": rel, "origin": wc.ORIGIN_WIKI}
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/page/delete", auth=True)
    async def api_page_delete(self, body: dict):
        from fastapi import HTTPException
        self._require_enabled()
        body = body or {}
        kw = self._kw_or_400(body.get("kw") or "")
        rel = body.get("rel") or ""
        try:
            origin = await wc.to_thread(wc.delete_page, kw, rel, origin=body.get("origin") or "")
            await wc.to_thread(wc.rebuild_index, kw)
            await wc.to_thread(wc.append_log, kw, f"面板删除 {rel}")
            return {"ok": True, "rel": rel, "origin": origin}
        except wc.WikiPathError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @register.api(method="POST", path="/index/rebuild", auth=True)
    async def api_rebuild(self, body: dict = None):
        self._require_enabled()
        kw = self._kw_or_400((body or {}).get("kw") or "", create=True)
        n = await wc.to_thread(wc.rebuild_index, kw)
        await wc.to_thread(wc.append_log, kw, "面板重建索引")
        return {"ok": True, "pages": n}

    @register.api(method="GET", path="/lint", auth=True)
    async def api_lint(self, kw_name: str = ""):
        self._require_enabled()
        kw = self._kw_or_400(kw_name)
        return await wc.to_thread(wc.lint, kw)

    @register.api(method="GET", path="/search", auth=True)
    async def api_search(self, q: str, kw_name: str = ""):
        self._require_enabled()
        kw = self._kw_or_400(kw_name)
        q = (q or "").strip()
        if not q:
            return {"query": "", "hits": [], "meta": {"candidates": 0, "picked": 0, "chars": 0}}
        picked, meta = await wc.to_thread(wc.rank_pages, kw, q, top_k=12, per_page_chars=400)
        return {"query": q, "hits": [{"rel": p["rel"], "title": p["title"], "section": p["section"],
                                      "origin": p.get("origin", ""), "score": p["score"],
                                      "snippet": p["text"][:300]} for p in picked],
                "meta": meta}

    @register.api(method="POST", path="/query", auth=True)
    async def api_query(self, body: dict):
        """面板问答：走与工具同一条检索 + LLM 链路。"""
        self._require_enabled()
        kw = self._kw_or_400((body or {}).get("kw") or "")
        question = ((body or {}).get("question") or "").strip()
        if not question:
            return {"ok": False, "error": "question required"}
        if (body or {}).get("ask") is False:
            hits, meta = await wc.to_thread(
                wc.rank_pages, kw, question, top_k=12, per_page_chars=400)
            return {"ok": True, "ask": False,
                    "hits": [{"rel": p["rel"], "title": p["title"], "section": p["section"],
                              "origin": p.get("origin", ""), "score": p["score"],
                              "snippet": p["text"][:400]} for p in hits],
                    "meta": meta, "sources": []}
        picked, meta = await wc.to_thread(wc.rank_pages, kw, question)
        if not picked:
            return {"ok": False, "error": "no_pages"}
        context = "\n\n".join(f"### {p['title']}（{p['rel']}）\n{p['text']}" for p in picked)
        prompt = (f"知识库页面（按相关度排序，共 {len(picked)} 页）：\n{context}\n\n"
                  f"问题：{question}\n\n请回答并标注来源页面。")
        answer = await self._llm(prompt, system=SYSTEM_QA)
        if not answer:
            return {"ok": False, "error": "llm_unavailable",
                    "hits": [{"rel": p["rel"], "title": p["title"], "section": p["section"],
                              "score": p["score"], "snippet": p["text"][:400]} for p in picked[:5]]}
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
