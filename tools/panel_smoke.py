#!/usr/bin/env python3
"""Kira Wiki · 真框架冒烟测试（起真 FastAPI 路由 + 真插件管理器，不占端口）。

用途：把插件装进**你本地的 KiraAI 源码**跑一遍端到端断言，确认与框架的世代兼容
（2.34.x / 3.0 rc.x 都行——脚本按世代自适应模块名与 PluginContext 字段）。

    python tools/panel_smoke.py --framework /path/to/KiraAI
    python tools/panel_smoke.py --framework /path/to/KiraAI-3.0 --keep

退出码 0 = 全部通过；非 0 = 有断言失败（会打印失败项）。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import wiki_core as wc  # noqa: E402  （插件根目录下的纯标准库服务层）
PLUGIN_DIR_NAME = PLUGIN_ROOT.name
_MANIFEST = json.loads((PLUGIN_ROOT / "manifest.json").read_text("utf-8"))
PLUGIN_NAME = _MANIFEST.get("plugin_id") or PLUGIN_DIR_NAME

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra and not cond else ""))


class _ToolMgr:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, description, parameters, func):
        self.tools[name] = func


class _ProviderMgr:
    def get_default_llm(self):
        return None

    def get_default_fast_llm(self):
        return None

    def get_default_embedding(self):
        return None

    def get_model_client(self, pid, mid):
        return None

    def get_provider(self, pid):
        return None

    def get_model_infos(self, pid):
        return []

    def get_model_info(self, pid, mid, mtype="llm"):
        return None

    def get_all_providers(self):
        return {}


class _Config(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def get_config(self, key, default=None):
        return default


class _FakeLLM:
    """假生成模型：固定返回 12 页的 JSON，用来验证 max_pages 真的被裁剪。"""

    calls = 0

    def __init__(self, payload=None):
        self.payload = payload

    async def chat(self, req):
        type(self).calls += 1
        n = type(self).calls          # 每次调用给不同的页面名，避免覆盖导致计数失真
        payload = self.payload or {
            "summary": "# 摘要\n\n" + "内容" * 80,
            "entities": [{"title": f"实体{n}-{i}", "content": "# 实体\n\n" + "字" * 80} for i in range(5)],
            "concepts": [{"title": f"概念{n}-{i}", "content": "# 概念\n\n" + "字" * 80} for i in range(5)],
            "analysis": "# 分析\n\n" + "析" * 80,
        }
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

        class R:
            text_response = text

        return R()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--framework", required=True, help="KiraAI 源码根目录")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    fw = Path(args.framework).resolve()
    if not (fw / "core" / "plugin").is_dir():
        print(f"✗ 不像 KiraAI 源码根：{fw}")
        return 2

    work = Path(tempfile.mkdtemp(prefix="kw-smoke-"))
    data = work / "data"
    for d in ("logs", "config", "plugins"):
        (data / d).mkdir(parents=True, exist_ok=True)
    os.chdir(fw)
    sys.path.insert(0, str(fw))

    from core.utils.path_utils import init_paths            # noqa: E402

    init_paths(data_dir=str(data))

    from fastapi import FastAPI                             # noqa: E402
    from fastapi.testclient import TestClient               # noqa: E402

    from core.plugin.plugin_context import PluginContext    # noqa: E402
    try:
        from core.plugin.plugin_registry import PluginManager, PLUGINS_DIR, _plugin_components
    except ModuleNotFoundError:                             # 3.0：模块被拆成 manager/registry
        from core.plugin.manager import PluginManager, PLUGINS_DIR
        from core.plugin.registry import _plugin_components
    from webui.routes.auth import require_auth              # noqa: E402

    from core.config.default import VERSION                 # noqa: E402

    print(f"· 框架 {fw.name} / {VERSION}")
    print(f"· 插件 {PLUGIN_NAME} @ {PLUGIN_ROOT}")

    dest = Path(PLUGINS_DIR) / PLUGIN_DIR_NAME
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_ROOT, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    fields = {f.name for f in dataclasses.fields(PluginContext)}
    kw = {n: None for n in fields}
    if "provider_mgr" in fields:
        kw["provider_mgr"] = _ProviderMgr()
    if "tool_mgr" in fields:
        kw["tool_mgr"] = _ToolMgr()
    if "config" in fields:
        kw["config"] = _Config()
    ctx = PluginContext(**kw)

    app = FastAPI()
    mgr = PluginManager(ctx=ctx)
    mgr.ctx = ctx
    ctx.plugin_mgr = mgr
    mgr.set_web_app(app)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    pid = loop.run_until_complete(mgr.load_plugin_from_dir(dest, auto_install=False))
    inst = mgr.plugin_instances.get(PLUGIN_NAME)
    app.dependency_overrides[require_auth] = lambda: "smoke"
    client = TestClient(app)
    base = f"/api/plugin/{PLUGIN_NAME}"

    print("\n[1] 加载与注册")
    check("插件加载成功", pid == PLUGIN_NAME, str(pid))
    tools = ctx.tool_mgr.tools
    for t in ("wiki_write", "wiki_read", "wiki_ingest", "wiki_query"):
        check(f"工具 {t}", t in tools)
    # 每轮请求都会注入工具 schema ⇒ 数量与体量都要守住，防止悄悄反弹
    check("工具数恰好为 4（合并后不许反弹）", len(tools) == 4, f"实际 {len(tools)}: {sorted(tools)}")
    import json as _json
    comp = _plugin_components[PLUGIN_NAME]
    budget = 0
    for name, meta in comp.tools.items():
        budget += len(name) + len(meta.get("description", "")) + len(
            _json.dumps(meta.get("parameters") or {}, ensure_ascii=False, separators=(",", ":"))) + 12
    check("工具 schema 总量 <= 1600 字符（v1.4.0 实测 1566）", budget <= 1600, f"实际 {budget}")
    page = f"/page/plugin/{PLUGIN_NAME}/index/"
    check("页面路由已挂载", client.get(page).status_code in (200, 307, 401))
    r = client.get(f"{base}/library")
    check("/library 可达", r.status_code == 200, r.text[:120])

    print("\n[2] 无库：只读接口 404，但 /library 必须 200 且给出自举信息")
    r = client.get(f"{base}/library")
    j = r.json()
    check("/library 200", r.status_code == 200)
    check("can_init=true", j.get("can_init") is True)
    check("suggested_path 非空", bool(j.get("suggested_path")), str(j.get("suggested_path")))
    check("默认路径不存在时 current=None", j.get("current") is None)
    for ep in ("/tree", "/graph", "/lint", "/search?q=x"):
        check(f"{ep} 404", client.get(f"{base}{ep}").status_code == 404)
    check("只读探测没有创建目录", not Path(j["suggested_path"]).exists())

    print("\n[3] 面板自举：POST /kb/init")
    kb = work / "kb"
    r = client.post(f"{base}/kb/init", json={"name": "smoke", "path": str(kb)})
    check("/kb/init 200", r.status_code == 200, r.text[:200])
    check("目录已建", (kb / "wiki" / "index.md").is_file())
    for ep in ("/tree", "/graph", "/lint"):
        check(f"{ep} 200", client.get(f"{base}{ep}").status_code == 200, ep)
    check("registry 有该库", "smoke" in client.get(f"{base}/library").json()["registry"])

    print("\n[4] bot 自己写 / 读 / 删（完全不调用生成模型）")
    r = client.post(f"{base}/page", json={"rel": "entities/自己写.md", "content": "# 自己写\n\n" + "字" * 60})
    check("面板写页 200", r.status_code == 200, r.text[:150])
    out = loop.run_until_complete(tools["wiki_write"](
        event=None, mode="replace", rel="concepts/工具写.md",
        content="# 工具写\n\n[[自己写]] " + "字" * 60))
    check("wiki_write(默认 replace) 成功", "已写入" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_write"](
        event=None, mode="append", rel="concepts/工具写.md", content="追加一段"))
    check("wiki_write(mode=append) 成功", "已追加到" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_read"](event=None, ref="工具写"))
    check("wiki_read(ref) 命中", "工具写" in out and "追加一段" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_write"](
        event=None, mode="delete", rel="concepts/工具写.md"))
    check("wiki_write(mode=delete) 成功", "已删除" in out, out[:120])
    check("文件真的没了", not (kb / "wiki" / "concepts" / "工具写.md").exists())
    check("整个写/读/删过程一次都没碰生成模型", _FakeLLM.calls == 0, f"实际调用 {_FakeLLM.calls} 次")

    print("\n[5] 生成模型不可用 ⇒ ingest 只存 raw 且明确指引（不静默失败）")
    out = loop.run_until_complete(tools["wiki_ingest"](event=None, content="素材" * 60, title="无模型素材"))
    check("提示改用 wiki_write", "wiki_write" in out, out[:160])
    check("raw 已落地", (kb / "raw" / "无模型素材.md").is_file())

    print("\n[6] 生成模型可用 + max_pages 真的生效")
    inst.ctx.get_default_llm_client = lambda *a, **k: _FakeLLM()
    inst.ctx.get_llm_client = lambda *a, **k: _FakeLLM()
    def section_pages():
        n = 0
        for s in wc.SECTIONS:
            n += len(list((kb / "wiki" / s).glob("*.md")))
        return n

    inst.max_pages = 3
    before = section_pages()
    out = loop.run_until_complete(tools["wiki_ingest"](event=None, content="素材" * 60, title="裁剪素材"))
    delta = section_pages() - before
    check("提示已达上限", "上限" in out, out[:160])
    check("max_pages=3 ⇒ 恰好写 3 页", delta == 3, f"实际新增 {delta}")
    # 反向：默认 15 时不应裁掉 12 页
    inst.max_pages = 15
    before = section_pages()
    loop.run_until_complete(tools["wiki_ingest"](event=None, content="素材" * 60, title="不裁素材"))
    delta = section_pages() - before
    check("max_pages=15 ⇒ 12 页全写入", delta == 12, f"实际新增 {delta}")

    print("\n[6.5] 工具合并后的等价性：init / 列目录 / 体检 / 参数校验")
    kb3 = work / "kb3"
    out = loop.run_until_complete(tools["wiki_write"](
        event=None, mode="init", path=str(kb3), name="kb3"))
    check("wiki_write(mode=init) 建库成功", "已就绪" in out and kb3.is_dir(), out[:120])
    check("建库后 registry 有 kb3", "kb3" in client.get(f"{base}/library").json()["registry"])
    out = loop.run_until_complete(tools["wiki_read"](event=None, kw_name="kb3"))
    check("wiki_read 无 ref ⇒ 列目录+统计", "知识库 @" in out and "素材 raw" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_read"](event=None, kw_name="kb3", check=True))
    check("wiki_read(check=true) ⇒ 带体检结论", "体检" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_write"](event=None, mode="bogus", rel="a.md", content="x"))
    check("未知 mode 被拒", "未知 mode" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_write"](event=None, mode="replace", rel="concepts/x.md"))
    check("缺 content 被拒", "需要同时给" in out, out[:120])
    out = loop.run_until_complete(tools["wiki_write"](event=None, mode="delete", rel=""))
    check("缺 rel 被拒", "需要给 rel" in out, out[:120])

    print("\n[7] enabled=false 真的停用")
    inst.enabled = False
    out = loop.run_until_complete(tools["wiki_read"](event=None))
    check("工具返回已停用", "已停用" in out, out[:120])
    check("API 返回 409", client.get(f"{base}/tree").status_code == 409)
    check("/library 仍 200（面板要能解释为什么）",
          client.get(f"{base}/library").json().get("enabled") is False)
    inst.enabled = True

    print("\n[8] 接管已有 Obsidian 库（外部笔记进场，且只读操作不动它）")
    vault = work / "vault"
    (vault / "项目").mkdir(parents=True, exist_ok=True)
    (vault / "我的笔记.md").write_text("# 我的笔记\n\n链接 [[计划]]\n", "utf-8")
    (vault / "项目" / "计划.md").write_text("# 计划\n\n待办\n", "utf-8")
    before = {p: p.read_text("utf-8") for p in vault.rglob("*.md")}
    r = client.post(f"{base}/kb/init", json={"name": "vault", "path": str(vault)})
    check("接管 200", r.status_code == 200, r.text[:150])
    parsed = tool_load_tree(client, base, "vault")
    rels = {p["rel"] for p in parsed["pages"]}
    check("外部笔记出现在 tree", {"我的笔记.md", "项目/计划.md"} <= rels, str(sorted(rels)))
    check("tree.sections 含 imported", "imported" in parsed["sections"], str(parsed["sections"]))
    ext = [p for p in parsed["pages"] if p["origin"] == "vault"]
    check("origin=vault 标记正确", len(ext) == 2, str(len(ext)))
    for p, txt in before.items():
        check(f"只读操作没有改动 {p.name}", p.read_text("utf-8") == txt)
    r = client.get(f"{base}/page", params={"rel": "我的笔记.md", "kw_name": "vault", "origin": "vault"})
    check("能读外部笔记", r.status_code == 200 and "我的笔记" in r.text, r.text[:120])
    check("读页返回 section（面板分区标签要用）", r.json().get("section") == "imported", str(r.json().get("section")))
    r = client.post(f"{base}/page", json={"kw": "vault", "rel": "我的笔记.md",
                                          "content": "# 我的笔记\n\n改过了\n", "origin": "vault"})
    check("能写外部笔记", r.status_code == 200, r.text[:150])
    check("外部笔记真的改了", "改过了" in (vault / "我的笔记.md").read_text("utf-8"))
    r = client.post(f"{base}/page", json={"kw": "vault", "rel": "../../escape.md", "content": "x"})
    check("越界写入被拒", r.status_code == 400, r.text[:120])

    print("\n[9] 检索 top_k / 空查询")
    r = client.get(f"{base}/search", params={"q": "计划", "kw_name": "vault"})
    check("search 200", r.status_code == 200)
    check("空查询返回空命中", client.get(f"{base}/search", params={"q": " "}).json()["hits"] == [])
    picked, meta = wc.rank_pages(Path(vault), "计划", top_k=1)
    check("rank_pages 遵守 top_k", len(picked) <= 1, str(meta))

    print("\n[9.5] wiki_query 的 ask 参数：纯检索 vs 模型合成")
    base_calls = _FakeLLM.calls
    out = loop.run_until_complete(
        tools["wiki_query"](event=None, question="计划", ask=False, kw_name="vault"))
    check("ask=false ⇒ 零模型调用", _FakeLLM.calls == base_calls,
          f"{base_calls} -> {_FakeLLM.calls}")
    check("ask=false ⇒ 返回命中列表（含 score）", "检索「计划」" in out and "[score" in out, out[:200])
    check("ask=false ⇒ 提示用 wiki_read 钻取", 'wiki_read(ref=' in out, out[:220])
    out2 = loop.run_until_complete(
        tools["wiki_query"](event=None, question="计划", kw_name="vault"))
    check("默认 ask=true ⇒ 会调模型", _FakeLLM.calls > base_calls,
          f"{base_calls} -> {_FakeLLM.calls}")
    out3 = loop.run_until_complete(
        tools["wiki_query"](event=None, question="绝不存在的检索词zzz", ask=False, kw_name="vault"))
    check("未命中 ⇒ 说明库里有几个可检索页", "没有命中" in out3 and "可检索页面" in out3, out3[:180])
    out4 = loop.run_until_complete(
        tools["wiki_query"](event=None, question="   ", ask=False, kw_name="vault"))
    check("空 question 被拒", "需要给 question" in out4, out4[:120])
    r = client.post(f"{base}/query", json={"kw": "vault", "question": "计划", "ask": False})
    j = r.json()
    check("面板 /query 支持 ask=false（返回 hits）",
          r.status_code == 200 and j.get("ask") is False and isinstance(j.get("hits"), list),
          str(j)[:150])

    print("\n[9.8] 设置 API：面板内改 + 热生效 + 框架配置作为同步镜像")
    r = client.get(f"{base}/settings")
    st = r.json()
    check("/settings 200", r.status_code == 200, r.text[:120])
    check("返回 4 个设置项", all(k in st for k in
          ("enabled", "default_kw_path", "model", "max_pages_per_ingest")), str(list(st))[:120])
    check("返回 revision（乐观并发用）", bool(st.get("revision")), str(st.get("revision")))
    check("返回 models 列表字段", isinstance(st.get("models"), list), str(type(st.get("models"))))
    rev = st["revision"]

    # ① 热生效：改 default_kw_path / max_pages —— 不改配置文件的情况下立刻影响本进程行为
    new_path = str(work / "kb-set")
    r = client.post(f"{base}/settings", json={"revision": rev, "default_kw_path": new_path,
                                              "max_pages_per_ingest": 7})
    check("保存设置 200", r.status_code == 200, r.text[:150])
    check("default_kw_path 立即生效", inst.default_kw_path == new_path,
          f"{inst.default_kw_path}")
    check("max_pages 立即生效", inst.max_pages == 7, str(inst.max_pages))
    check("返回新 revision", r.json().get("revision") and r.json()["revision"] != rev)

    # ② 落盘 + 宿主内存同步（= 框架插件设置页看到的就是这份）
    cfg_path = Path(data) / "config" / "plugins" / f"{PLUGIN_NAME}.json"
    on_disk = json.loads(cfg_path.read_text("utf-8"))
    check("已原子落盘到配置文件", on_disk["section_basic"]["default_kw_path"] == new_path,
          str(on_disk.get("section_basic"))[:120])
    host = mgr.plugin_configs.get(PLUGIN_NAME, {})
    check("宿主 plugin_configs 同步（设置页不会看到旧值）",
          host.get("section_basic", {}).get("default_kw_path") == new_path,
          str(host.get("section_basic"))[:120])
    check("plugin_cfg 与宿主内存是同一份", host is inst.plugin_cfg or host == inst.plugin_cfg)

    # ③ enabled=false 也能读设置（否则关掉就再也打不开了）
    inst.enabled = False
    check("停用后 /settings 仍可读", client.get(f"{base}/settings").status_code == 200)
    inst.enabled = True

    # ④ 参数校验
    bad = client.post(f"{base}/settings", json={"max_pages_per_ingest": 0})
    check("页数越界被拒 400", bad.status_code == 400, bad.text[:120])
    # 注意：前面为了让 ingest/query 能跑，给 ctx.get_llm_client 打了「任何模型都返回客户端」的桩；
    # 这里先摘掉，才是在验证真实的「模型解析失败要拒绝」路径。
    _orig_client = inst.ctx.get_llm_client
    inst.ctx.get_llm_client = lambda *a, **k: None
    bad2 = client.post(f"{base}/settings", json={"model": "不存在:模型"})
    inst.ctx.get_llm_client = _orig_client
    check("模型不可用被拒 400", bad2.status_code == 400, bad2.text[:120])
    conflict = client.post(f"{base}/settings", json={"revision": "stale-rev", "enabled": True})
    check("revision 过期 ⇒ 409（防互相覆盖）", conflict.status_code == 409, conflict.text[:120])
    check("校验失败没有改坏配置", inst.max_pages == 7, str(inst.max_pages))

    # ⑤ 反向同步：在「框架插件设置页」改 ⇒ 插件被重建并读到新值（两边都能改）
    host_path = str(work / "kb-host")
    loop.run_until_complete(mgr.update_plugin_config(
        PLUGIN_NAME, {"section_basic": {"enabled": True, "default_kw_path": host_path},
                      "section_llm": {"model": "", "max_pages_per_ingest": 9}}))
    inst = mgr.plugin_instances.get(PLUGIN_NAME)
    check("框架设置页改动 ⇒ 插件重建并读到新值",
          inst is not None and inst.default_kw_path == host_path,
          getattr(inst, "default_kw_path", "无实例"))
    check("框架设置页改动 ⇒ max_pages 同步", getattr(inst, "max_pages", None) == 9,
          str(getattr(inst, "max_pages", None)))
    check("面板再读也是新值（双向一致）",
          client.get(f"{base}/settings").json().get("default_kw_path") == host_path,
          client.get(f"{base}/settings").text[:120])

    print("\n[10] 版本与清单")
    sentinel = json.loads((PLUGIN_ROOT / "manifest.json").read_text("utf-8"))
    check("manifest 版本 >= 1.2.0", tuple(int(x) for x in sentinel["version"].split(".")) >= (1, 2, 0),
          sentinel["version"])
    # 面板（体检/问答）的闭合态：只写 translateY(104%) 时，因为 .sheet 是 bottom:40px 定位，
    # 头部会残留约 27px 在屏幕底边 —— 那一截正好带 ×，用户会以为「面板关不掉、点了没反应」。
    import re as _re
    css = (PLUGIN_ROOT / "web" / "style.css").read_text("utf-8")
    _m = _re.search(r"\.sheet\{(.*?)\}", css, _re.S)
    sheet_closed = _m.group(1) if _m else ""
    check("sheet 闭合态：位移须为 calc(100% + Npx)（纯 100% 不足以移出 bottom:40px）",
          bool(_re.search(r"translate\(-50%,calc\(100% \+ \d+px\)\)", sheet_closed)),
          sheet_closed[:80])
    check("sheet 闭合态：opacity:0", "opacity:0" in sheet_closed)
    check("sheet 闭合态：visibility:hidden", "visibility:hidden" in sheet_closed)
    check("sheet 闭合态：pointer-events:none（不会截走点击）",
          "pointer-events:none" in sheet_closed)

    html = (PLUGIN_ROOT / "web" / "index.html").read_text("utf-8")
    for asset in ("style.css", "graph.js", "app.js"):
        check(f"{asset} 带版本查询串（防静态资源缓存）",
              f'{asset}?v={sentinel["version"]}' in html,
              f'需要 {asset}?v={sentinel["version"]}')

    print(f"\n===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====")
    if FAIL:
        for f in FAIL:
            print("  × " + f)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"（临时目录保留：{work}）")
    return 1 if FAIL else 0


def tool_load_tree(client, base, kw_name):
    return client.get(f"{base}/tree", params={"kw_name": kw_name}).json()


if __name__ == "__main__":
    sys.exit(main())
