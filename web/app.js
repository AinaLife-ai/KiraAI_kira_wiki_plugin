/* ═══════════════════════════════════════════════════════════════════════════
   Kira Wiki · 面板应用层
   状态 → 渲染；零依赖；所有网络调用带 credentials（框架会话鉴权）。
   ═════════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const PLUGIN_ID = "KiraAI_kira_wiki_plugin";
  const PAPI = "/api/plugin/" + encodeURIComponent(PLUGIN_ID);
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    kw: "", lib: null, tree: null, graph: null,
    current: null, dirty: false, mode: "read", filter: null
  };

  async function api(path, opts) {
    const res = await fetch(PAPI + path, Object.assign({ credentials: "include" }, opts || {}));
    const txt = await res.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { raw: txt }; }
    if (!res.ok) throw new Error((data && (data.detail || data.error)) || ("HTTP " + res.status));
    return data;
  }
  const qs = (o) => Object.keys(o).filter(k => o[k] !== undefined && o[k] !== "")
    .map(k => encodeURIComponent(k) + "=" + encodeURIComponent(o[k])).join("&");

  let toastTimer = null;
  function toast(msg, isErr) {
    const t = $("toast");
    t.textContent = msg; t.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = "toast"; }, isErr ? 4200 : 2200);
  }

  /* ───────────────── 迷你 Markdown 渲染（离线、无依赖） ───────────────── */
  function slugOf(p) { return String(p || "").split("/").pop().replace(/\.md$/, ""); }
  function known(name) { return !!(state.tree && state.tree.pages.some(p => p.name === name)); }

  function md(text) {
    const lines = String(text || "").split("\n");
    let out = [], i = 0, inCode = false, listMode = null;
    const closeList = () => { if (listMode) { out.push("</" + listMode + ">"); listMode = null; } };
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]/g, (m, t, label) => {
        const name = String(t).trim().split("/").pop();
        const ok = known(name);
        return `<span class="wl${ok ? "" : " dead"}" data-open="${ok ? esc(name) : ""}">${esc(label || t)}</span>`;
      })
      .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    while (i < lines.length) {
      const ln = lines[i];
      if (/^```/.test(ln)) {
        if (inCode) { out.push("</code></pre>"); inCode = false; }
        else { closeList(); out.push("<pre><code>"); inCode = true; }
        i++; continue;
      }
      if (inCode) { out.push(esc(ln) + "\n"); i++; continue; }
      let m;
      if ((m = ln.match(/^(#{1,6})\s+(.*)$/))) { closeList(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
      if (/^\s*([-*_])\1{2,}\s*$/.test(ln)) { closeList(); out.push("<hr>"); i++; continue; }
      if ((m = ln.match(/^>\s?(.*)$/))) { closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); i++; continue; }
      if ((m = ln.match(/^\s*[-*+]\s+(.*)$/))) {
        if (listMode !== "ul") { closeList(); out.push("<ul>"); listMode = "ul"; }
        out.push(`<li>${inline(m[1])}</li>`); i++; continue;
      }
      if ((m = ln.match(/^\s*\d+[.)]\s+(.*)$/))) {
        if (listMode !== "ol") { closeList(); out.push("<ol>"); listMode = "ol"; }
        out.push(`<li>${inline(m[1])}</li>`); i++; continue;
      }
      if (!ln.trim()) { closeList(); i++; continue; }
      closeList(); out.push(`<p>${inline(ln)}</p>`); i++;
    }
    closeList();
    if (inCode) out.push("</code></pre>");
    return out.join("\n");
  }

  /* ───────────────── 渲染：导航 / 指标 / 日志 ───────────────── */
  function renderNav() {
    const st = state.tree ? state.tree.stats : null;
    if (!st) return;
    const total = Math.max(1, st.pages);
    $("navList").innerHTML = state.tree.sections.map((s, i) => {
      const n = st.by_section[s] || 0;
      const cvar = { sources: "--src", entities: "--ent", concepts: "--con", analyses: "--ana" }[s];
      const on = state.filter === s ? " on" : "";
      return `<li class="nav-item${on}" data-sec="${s}" style="--c:var(${cvar})">
        <span class="idx">${String(i + 1).padStart(2, "0")}</span>
        <span class="nm">${esc(state.tree.labels[s])}</span>
        <span class="ct">${n}</span>
        <span class="bar"><i style="--p:${(n / total).toFixed(3)}"></i></span>
      </li>`;
    }).join("");
    $("navList").querySelectorAll(".nav-item").forEach(el => {
      el.onclick = () => {
        const s = el.dataset.sec;
        state.filter = state.filter === s ? null : s;
        renderNav();
        if (window.__lattice) window.__lattice.setFilter(state.filter);
      };
    });
  }

  function roll(el, to) {
    const from = parseInt(el.textContent, 10) || 0;
    if (from === to) return;
    const t0 = performance.now(), dur = 620;
    (function step(t) {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      el.textContent = Math.round(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
    })(t0);
  }

  function renderMetrics() {
    const g = state.graph, st = state.tree ? state.tree.stats : { raw: 0, pages: 0 };
    roll($("mRaw"), st.raw || 0);
    roll($("mPages"), st.pages || 0);
    roll($("mLinks"), g ? g.links.length : 0);
  }

  function renderChrome() {
    if (!state.lib || !state.lib.current) return;
    const cur = state.lib.current;
    $("kbPath").textContent = cur.path;
    $("kbStamp").textContent = (cur.name ? cur.name + " · " : "") + new Date().toLocaleString("zh-CN", { hour12: false });
    const run = $("tickerRun");
    const log = cur.log || [];
    const seq = log.length ? log : ["尚无操作记录"];
    run.innerHTML = seq.concat(seq).map(l => `<span>${esc(l.replace(/^-\s*/, ""))}</span>`).join("");
  }

  function emptyState(main, sub) {
    const e = $("empty");
    e.classList.add("show");
    $("emptyLead").textContent = main; $("emptySub").textContent = sub || "";
  }
  function hideEmpty() { $("empty").classList.remove("show"); }

  /* ───────────────── 抽屉：阅读 / 编辑 ───────────────── */
  function openDrawer(pg) {
    state.current = pg; state.mode = "read"; state.dirty = false;
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    $("pgTitle").textContent = pg.title || pg.name;
    $("pgSection").textContent = (state.tree && state.tree.labels[pg.section]) || pg.section;
    $("pgPath").textContent = pg.rel || "";
    $("pgSize").textContent = (pg.size || 0) + " B";
    $("pgCount").textContent = (pg.content || "").length + " 字";
    $("pgUpdated").textContent = pg.mtime ? new Date(pg.mtime * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
    setDirty(false);
    renderReader(pg);
    setMode("read");
  }

  function renderReader(pg) {
    const text = pg.content || "";
    const fm = text.match(/^---\s*\n([\s\S]*?)\n---\s*\n?/);
    const body = fm ? text.slice(fm[0].length) : text;
    const fmBox = fm ? `<div class="fm">${esc(fm[1])}</div>` : "";
    $("reader").innerHTML = fmBox + md(body);
    $("reader").querySelectorAll(".wl[data-open]").forEach(el => {
      el.onclick = () => { if (el.dataset.open) loadPageByName(el.dataset.open); };
    });
  }

  function setMode(mode) {
    state.mode = mode;
    const editing = mode === "edit";
    $("reader").hidden = editing;
    $("editor").hidden = !editing;
    $("btnTabRead").classList.toggle("on", !editing);
    $("btnTabEdit").classList.toggle("on", editing);
    if (editing) {
      $("editor").value = (state.current && state.current.content) || "";
      $("editor").focus();
    }
  }

  function setDirty(on) {
    state.dirty = on;
    const el = $("pgDirty");
    el.className = "tag-mono dirty" + (on ? " on" : " ok");
    el.textContent = on ? "未保存" : "已同步";
  }

  async function loadPageByName(name) {
    const hit = state.tree.pages.find(p => p.name === name);
    if (!hit) { toast("页面不存在：" + name, true); return; }
    await loadPage(hit.rel);
  }

  async function loadPage(rel) {
    try {
      const pg = await api("/page?" + qs({ rel, kw_name: state.kw }));
      openDrawer(pg);
      if (window.__lattice) window.__lattice.focus(rel);
    } catch (e) { toast("打开失败：" + e.message, true); }
  }

  async function savePage() {
    if (!state.current) return;
    const content = state.mode === "edit" ? $("editor").value : (state.current.content || "");
    try {
      const r = await api("/page", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: state.current.rel, content, kw: state.kw })
      });
      state.current.content = content;
      setDirty(false);
      const rel = (r && r.rel) || state.current.rel;
      const size = (r && r.size) ? r.size + "B" : "";
      toast("已保存 · " + rel + (size ? " · " + size : ""));
      await refresh({ keepDrawer: true });
    } catch (e) { toast("保存失败：" + e.message, true); }
  }

  /* ───────────────── 浮层：体检 / 问答 ───────────────── */
  function openSheet(kind) {
    $("sheet").classList.add("open");
    $("sheet").setAttribute("aria-hidden", "false");
    $("sheetKind").textContent = kind === "lint" ? "体检 / LINT" : "问答 / ASK";
    $("sheetBody").innerHTML = `<p class="meta">正在${kind === "lint" ? "扫描" : "检索"}…</p>`;
    return kind;
  }
  function closeSheet() { $("sheet").classList.remove("open"); $("sheet").setAttribute("aria-hidden", "true"); }

  async function runLint() {
    openSheet("lint");
    try {
      const rep = await api("/lint?" + qs({ kw_name: state.kw }));
      const st = rep.stats;
      const cards = [["素材", st.raw], ["来源", st.by_section.sources], ["实体", st.by_section.entities],
                     ["概念", st.by_section.concepts], ["分析", st.by_section.analyses]];
      const rows = (rep.issues || []).map(it => `
        <div class="row ${it.level === "warn" ? "warn" : "info"}">
          <svg viewBox="0 0 24 24"><use href="#${it.level === "warn" ? "i-warn" : "i-info"}"/></svg>
          <div><b>${esc(it.detail)}</b><div class="meta">${esc(it.page)} · ${esc(it.kind)}</div></div>
        </div>`).join("");
      $("sheetBody").innerHTML =
        `<div class="stats">${cards.map(c => `<div><b>${c[1]}</b><span>${c[0]}</span></div>`).join("")}</div>` +
        (rows || `<div class="row info"><svg viewBox="0 0 24 24"><use href="#i-check"/></svg><div><b>结构健康，没有发现问题</b></div></div>`);
    } catch (e) { $("sheetBody").innerHTML = `<p class="meta">体检失败：${esc(e.message)}</p>`; }
  }

  function runAsk() {
    openSheet("ask");
    $("sheetBody").innerHTML = `
      <div class="askbar">
        <input id="askInput" placeholder="向知识库提问，例如：Z 插件的记忆分层是怎么设计的？" spellcheck="false">
        <button class="icobtn" id="askGo" title="提问"><svg viewBox="0 0 24 24"><use href="#i-arrow"/></svg></button>
      </div>
      <div id="askOut"><p class="meta">回车提问 · 回答会标注参考页面</p></div>`;
    const go = async () => {
      const q = $("askInput").value.trim();
      if (!q) return;
      $("askOut").innerHTML = `<p class="meta">检索并生成回答…</p>`;
      try {
        const r = await api("/query", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q, kw: state.kw })
        });
        if (!r.ok) { $("askOut").innerHTML = `<p class="meta">未能回答（${esc(r.error)}）</p>`; return; }
        const src = (r.sources || []).map(s => `<button data-rel="${esc(s.rel)}">${esc(s.title)} · ${s.score}</button>`).join("");
        $("askOut").innerHTML = `<div class="answer">${esc(r.answer)}</div>
          <div class="meta" style="margin-top:14px">参考页面 ${(r.sources || []).length} · 候选 ${r.meta.candidates} · 取用 ${r.meta.chars} 字</div>
          <div class="src">${src}</div>`;
        $("askOut").querySelectorAll(".src button").forEach(b => b.onclick = () => loadPage(b.dataset.rel));
      } catch (e) { $("askOut").innerHTML = `<p class="meta">提问失败：${esc(e.message)}</p>`; }
    };
    $("askGo").onclick = go;
    $("askInput").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
    setTimeout(() => $("askInput") && $("askInput").focus(), 260);
  }

  /* ───────────────── 搜索 ───────────────── */
  let searchTimer = null;
  function onSearchInput() {
    const q = $("searchInput").value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      if (!q) { if (window.__lattice) window.__lattice.highlight(null); return; }
      try {
        const r = await api("/search?" + qs({ q, kw_name: state.kw }));
        if (window.__lattice) window.__lattice.highlight(r.hits.map(h => h.rel));
        if (r.hits.length === 1) loadPage(r.hits[0].rel);
        else if (r.hits.length) toast(`命中 ${r.hits.length} 页（已高亮）`);
        else toast("没有命中页面", true);
      } catch (e) { /* 搜索失败静默，避免打断输入 */ }
    }, 320);
  }

  /* ───────────────── 数据加载 ───────────────── */
  async function refresh(opts) {
    opts = opts || {};
    try {
      const [lib, tree, graph] = await Promise.all([
        api("/library?" + qs({ kw_name: state.kw })),
        api("/tree?" + qs({ kw_name: state.kw })),
        api("/graph?" + qs({ kw_name: state.kw }))
      ]);
      state.lib = lib; state.tree = tree; state.graph = graph;
      renderNav(); renderMetrics(); renderChrome();
      if (window.__lattice) {
        window.__lattice.setData(graph);
        window.__lattice.setFilter(state.filter);
      }
      if (graph.nodes.length === 0) {
        emptyState("知识库还是空的", "在聊天里让我收录一篇文章，或把素材丢进 raw/ 目录，晶格会自己长出来。");
      } else if (!opts.keepDrawer) {
        hideEmpty();
      }
    } catch (e) {
      emptyState("读不到知识库", e.message + " · 先在插件设置里填写知识库路径，或让 AI 执行 wiki_init。");
    }
  }

  /* ───────────────── 启动 ───────────────── */
  function boot() {
    const cv = $("lattice");
    window.__lattice = new window.Lattice(cv, {
      /* 晶格只在「主甲板」矩形内生长：左让开导航、下让开图例与 ticker */
      bounds: () => {
        const deck = document.querySelector(".deck");
        if (!deck) return null;
        const r = deck.getBoundingClientRect();
        return { x: r.left + 8, y: r.top + 64, w: r.width - 16, h: r.height - 120 };
      },
      onSelect: (n) => loadPage(n.id),
      onOpen: (n) => loadPage(n.id),
      onData: () => renderMetrics()
    });
    window.__lattice.setData({ nodes: [], links: [] });

    $("btnSearch").onclick = () => $("searchInput").focus();
    $("searchInput").addEventListener("input", onSearchInput);
    $("btnLint").onclick = runLint;
    $("btnAsk").onclick = runAsk;
    $("btnReindex").onclick = async () => {
      try { const r = await api("/index/rebuild", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kw: state.kw }) });
        toast(`索引已重建 · ${r.pages} 页`); refresh(); } catch (e) { toast("重建失败：" + e.message, true); }
    };
    $("btnNew").onclick = async () => {
      const title = prompt("新页面标题（将创建在「概念」分区，可稍后拖动归属）：");
      if (!title) return;
      try { const r = await api("/page/new", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, section: "concepts", kw: state.kw }) });
        toast("已新建 " + r.rel); await refresh(); loadPage(r.rel);
      } catch (e) { toast("新建失败：" + e.message, true); }
    };
    $("btnTheme").onclick = () => {
      const now = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = now;
      try { localStorage.setItem("kw-theme", now); } catch (e) {}
      $("btnTheme").querySelector("use").setAttribute("href", now === "light" ? "#i-sun" : "#i-moon");
    };
    if (document.documentElement.dataset.theme === "light") $("btnTheme").querySelector("use").setAttribute("href", "#i-sun");

    $("btnClose").onclick = () => {
      $("drawer").classList.remove("open");
      $("drawer").setAttribute("aria-hidden", "true");
      document.body.classList.remove("drawer-open");
    };
    $("btnTabRead").onclick = () => setMode("read");
    $("btnTabEdit").onclick = () => setMode("edit");
    $("btnSave").onclick = savePage;
    $("btnDelete").onclick = async () => {
      if (!state.current) return;
      if (!confirm("删除页面 " + state.current.rel + " ？此操作不可撤销。")) return;
      try { await api("/page/delete", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rel: state.current.rel, kw: state.kw }) });
        toast("已删除"); $("btnClose").onclick(); refresh();
      } catch (e) { toast("删除失败：" + e.message, true); }
    };
    $("btnSheetClose").onclick = closeSheet;
    $("editor").addEventListener("input", () => {
      setDirty(true);
      $("pgCount").textContent = $("editor").value.length + " 字";
      if (state.current) state.current.content = $("editor").value;
    });
    $("editor").addEventListener("keydown", e => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); savePage(); }
      if (e.key === "Tab") { e.preventDefault();
        const el = e.target, s = el.selectionStart;
        el.setRangeText("  ", s, el.selectionEnd, "end"); setDirty(true); }
    });
    // 光标视差：背景光晕跟随
    document.addEventListener("mousemove", (e) => {
      const gx = (e.clientX / window.innerWidth - .5), gy = (e.clientY / window.innerHeight - .5);
      const g = document.querySelector(".glow");
      if (g) g.style.transform = `translate(${gx * 26}px,${gy * 22}px)`;
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input,textarea")) { if (e.key === "Escape") e.target.blur(); return; }
      const k = e.key.toLowerCase();
      if (k === "/") { e.preventDefault(); $("searchInput").focus(); }
      else if (k === "e" && state.current) setMode(state.mode === "edit" ? "read" : "edit");
      else if (k === "l") runLint();
      else if (k === "a") runAsk();
      else if (k === "g") $("btnReindex").click();
      else if (k === "n") $("btnNew").click();
      else if (e.key === "Escape") { closeSheet(); $("btnClose").onclick(); }
    });

    refresh().then(() => {
      // 开屏退场 + 内容入场
      setTimeout(() => {
        $("boot").classList.add("done");
        document.body.classList.add("loaded");
      }, 520);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
