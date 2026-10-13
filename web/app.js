/* ═══════════════════════════════════════════════════════════════════════════
   Kira Wiki · 面板应用层
   状态 → 渲染；零依赖；所有网络调用带 credentials（框架会话鉴权）。

   两条硬约束（与后端一致）：
   1. **绝不使用原生 prompt / confirm / alert** —— KiraAI 插件页 iframe 的 sandbox
      没有 allow-modals，浏览器会静默吞掉模态框（按钮点了完全没反应）。
      所有确认/输入一律走页内自绘 modal。
   2. **只读接口失败不阻塞整屏** —— /library 单独取（它不依赖库存在），
      /tree /graph 失败只进「未初始化 / 不可用」态，并给出「创建 / 接管」引导。
   ═════════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const PLUGIN_ID = "KiraAI_kira_wiki_plugin";
  const PAPI = "/api/plugin/" + encodeURIComponent(PLUGIN_ID);
  const KW_KEY = "kw-active";
  const REQ_TIMEOUT = 20000;      // 单请求超时（ms）——大库不再「一直转圈」
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    kw: "", lib: null, tree: null, graph: null, origin: "wiki",
    current: null, dirty: false, mode: "read", filter: null, lastErr: ""
  };
  try { state.kw = localStorage.getItem(KW_KEY) || ""; } catch (e) { /* 隐私模式 */ }

  async function api(path, opts) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), REQ_TIMEOUT);
    let res;
    try {
      res = await fetch(PAPI + path,
        Object.assign({ credentials: "include", signal: ctl.signal }, opts || {}));
    } catch (e) {
      clearTimeout(timer);
      throw new Error(e && e.name === "AbortError"
        ? "请求超时 —— 知识库可能过大或目录不可访问"
        : "网络请求失败");
    }
    clearTimeout(timer);
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

  /* ───────────────── 页内 modal（替代原生 prompt/confirm） ───────────────── */
  let modalResolve = null;
  function closeModal(result) {
    const m = $("modal");
    m.hidden = true; m.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    const r = modalResolve; modalResolve = null;
    if (r) r(result);
  }
  function openModal(opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      modalResolve = resolve;
      $("modalTitle").textContent = opts.title || "确认";
      const txt = $("modalText");
      txt.textContent = opts.text || "";
      txt.hidden = !opts.text;
      const box = $("modalFields");
      box.innerHTML = "";
      (opts.fields || []).forEach(f => {
        const row = document.createElement("div");
        row.className = "kbs-row";
        const lab = document.createElement("label");
        lab.className = "kbs-lab"; lab.textContent = f.label || f.key;
        const inp = document.createElement("input");
        inp.className = "kbs-input" + (f.mono ? " mono" : "");
        inp.id = "mf-" + f.key;
        inp.type = "text";
        inp.value = f.value || "";
        inp.placeholder = f.placeholder || "";
        inp.spellcheck = false;
        inp.autocomplete = "off";
        row.appendChild(lab); row.appendChild(inp);
        box.appendChild(row);
      });
      $("modalOk").textContent = opts.ok || "确定";
      $("modalCancel").textContent = opts.cancel || "取消";
      const m = $("modal");
      m.hidden = false; m.setAttribute("aria-hidden", "false");
      document.body.classList.add("modal-open");
      setTimeout(() => { const f = box.querySelector("input"); if (f) f.focus(); }, 30);
      $("modalOk").onclick = () => {
        const out = {};
        (opts.fields || []).forEach(f => { out[f.key] = (($("mf-" + f.key) || {}).value || ""); });
        closeModal({ ok: true, values: out });
      };
      $("modalCancel").onclick = () => closeModal({ ok: false, values: {} });
      $("modalMask").onclick = () => closeModal({ ok: false, values: {} });
      box.querySelectorAll("input").forEach(inp => {
        inp.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); $("modalOk").click(); }
        });
      });
    });
  }
  async function askModal(title, text, okLabel) {
    const r = await openModal({ title, text, ok: okLabel || "确定" });
    return !!r.ok;
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

  /* ───────────────── 渲染：导航 / 指标 / 日志 / 库切换 ───────────────── */
  const SECVAR = { sources: "--src", entities: "--ent", concepts: "--con",
                   analyses: "--ana", imported: "--ext", root: "--fg-faint" };

  function renderNav() {
    const st = state.tree ? state.tree.stats : null;
    if (!st) return;
    const total = Math.max(1, st.section_pages || st.pages || 0);
    $("navList").innerHTML = state.tree.sections.map((s, i) => {
      const n = st.by_section[s] || 0;
      const cvar = SECVAR[s] || "--fg-faint";
      const on = state.filter === s ? " on" : "";
      return `<li class="nav-item${on}" data-sec="${s}" style="--c:var(${cvar})">
        <span class="idx">${String(i + 1).padStart(2, "0")}</span>
        <span class="nm">${esc(state.tree.labels[s] || s)}</span>
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
    const g = state.graph, st = state.tree ? state.tree.stats : null;
    roll($("mRaw"), (st && st.raw) || 0);
    roll($("mPages"), (st && (st.section_pages || st.pages)) || 0);
    roll($("mLinks"), g ? g.links.length : 0);
  }

  function renderModel() {
    const chip = $("modelChip"), lab = $("modelLabel");
    const m = (state.lib && state.lib.model) || null;
    if (!chip || !lab) return;
    if (!m) { lab.textContent = "默认 LLM"; chip.classList.remove("warn"); return; }
    lab.textContent = m.label || (m.value ? m.value : "默认 LLM");
    const bad = m.value && !m.ok;
    chip.classList.toggle("warn", !!bad);
    chip.title = bad
      ? `配置的模型解析不到：${m.error || "未知原因"}\n当前回退使用默认 LLM。请在插件设置里重新选择。`
      : `当前生成模型：${m.label}${m.canonical ? "\n引用值：" + m.canonical : "\n留空 ⇒ 使用 KiraAI 默认 LLM"}`;
  }

  function renderChrome() {
    renderModel();
    const cur = (state.lib && state.lib.current) || null;
    if (!cur) {
      $("kbPath").textContent = (state.lib && state.lib.suggested_path) || "—";
      $("kbStamp").textContent = "未初始化";
      $("tickerRun").innerHTML = "<span>尚无操作记录</span>";
      return;
    }
    $("kbPath").textContent = cur.path;
    $("kbStamp").textContent = (cur.name ? cur.name + " · " : "") +
      new Date().toLocaleString("zh-CN", { hour12: false });
    const run = $("tickerRun");
    const log = cur.log || [];
    const seq = log.length ? log : ["尚无操作记录"];
    run.innerHTML = seq.concat(seq).map(l => `<span>${esc(l.replace(/^-\s*/, ""))}</span>`).join("");
  }

  function renderKbPicker() {
    const wrap = $("kbPickWrap"), sel = $("kbPicker");
    if (!wrap || !sel) return;
    const reg = (state.lib && state.lib.registry) || {};
    const names = Object.keys(reg);
    if (!names.length && !state.kw) { wrap.hidden = true; return; }
    const opts = names.map(n => `<option value="${esc(n)}"${n === state.kw ? " selected" : ""}>${esc(n)}</option>`);
    if (state.kw && names.indexOf(state.kw) === -1) {
      opts.unshift(`<option value="${esc(state.kw)}" selected>${esc(state.kw)}</option>`);
    }
    sel.innerHTML = opts.join("");
    wrap.hidden = false;
    const cur = state.lib && state.lib.current;
    sel.title = cur ? `当前：${cur.name || ""} @ ${cur.path}\n切换知识库` : "选择知识库";
  }

  function emptyState(main, sub) {
    const e = $("empty");
    e.classList.add("show");
    $("emptyLead").textContent = main; $("emptySub").textContent = sub || "";
  }
  function hideEmpty() {
    $("empty").classList.remove("show");
    if ($("kbSetup")) $("kbSetup").hidden = true;
  }
  function showSetup(on) {
    const box = $("kbSetup");
    if (!box) return;
    box.hidden = !on;
    if (on) {
      const lib = state.lib || {};
      $("kbsPath").value = (lib.current && lib.current.path) || lib.suggested_path || "data/kw";
      const cur = lib.current;
      $("kbsName").value = cur ? (cur.name || "") : "";
      $("kbsName").placeholder = (cur && cur.name) || "默认知识库";
    }
  }
  function showTip(text, action) {
    const tip = $("tip");
    if (!tip) return;
    if (!text) { tip.hidden = true; return; }
    $("tipText").textContent = text;
    $("tipAction").hidden = !action;
    if (action) $("tipAction").onclick = action;
    tip.hidden = false;
  }

  /* ───────────────── 抽屉：阅读 / 编辑 ───────────────── */
  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    document.body.classList.remove("drawer-open");
  }

  function openDrawer(pg) {
    state.current = pg; state.mode = "read"; state.dirty = false;
    state.origin = pg.origin || "wiki";
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    $("pgTitle").textContent = pg.title || pg.name;
    const label = (state.tree && state.tree.labels && state.tree.labels[pg.section]) || pg.section;
    $("pgSection").textContent = state.origin === "vault" ? label + " · 你的笔记" : label;
    $("pgSection").classList.toggle("ext", state.origin === "vault");
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
    const extNote = (pg.origin === "vault")
      ? `<div class="ext-note">这是你自己笔记区里的文件（知识库根目录内）。保存会直接改写该 .md。</div>` : "";
    $("reader").innerHTML = extNote + fmBox + md(body);
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
    const hit = state.tree && state.tree.pages.find(p => p.name === name);
    if (!hit) { toast("页面不存在：" + name, true); return; }
    await loadPage(hit.rel, hit.origin);
  }

  async function loadPage(rel, origin) {
    try {
      const pg = await api("/page?" + qs({ rel, kw_name: state.kw, origin: origin || "" }));
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
        body: JSON.stringify({ rel: state.current.rel, content, kw: state.kw, origin: state.origin })
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
                     ["概念", st.by_section.concepts], ["分析", st.by_section.analyses],
                     ["外部笔记", st.by_section.imported || 0]];
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
        if (!r.ok) {
          if (r.error === "llm_unavailable" && r.hits && r.hits.length) {
            const hits = r.hits.map(h => `<button data-rel="${esc(h.rel)}" data-origin="${esc(h.origin || "")}">${esc(h.title)} · ${h.score}</button>`).join("");
            $("askOut").innerHTML = `<p class="meta">生成模型不可用，以下是检索到的原文片段：</p>` +
              r.hits.map(h => `<div class="answer">${esc(h.snippet)}</div>`).join("") +
              `<div class="src">${hits}</div>`;
            $("askOut").querySelectorAll(".src button").forEach(b =>
              b.onclick = () => loadPage(b.dataset.rel, b.dataset.origin));
            return;
          }
          const msg = r.error === "no_pages" ? "知识库里还没有可用页面" : ("未能回答（" + esc(r.error) + "）");
          $("askOut").innerHTML = `<p class="meta">${msg}</p>`;
          return;
        }
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
        if (r.hits.length === 1) loadPage(r.hits[0].rel, r.hits[0].origin);
        else if (r.hits.length) toast(`命中 ${r.hits.length} 页（已高亮）`);
        else toast("没有命中页面", true);
      } catch (e) { /* 搜索失败静默，避免打断输入 */ }
    }, 320);
  }

  /* ───────────────── 数据加载 ───────────────── */
  async function refresh(opts) {
    opts = opts || {};
    // ① /library 不依赖库存在 ⇒ 永远先拿它，面板至少能显示「库列表 + 建议路径」
    try {
      state.lib = await api("/library?" + qs({ kw_name: state.kw }));
      state.lastErr = "";
    } catch (e) {
      state.lib = null; state.lastErr = e.message;
    }
    renderKbPicker();
    renderModel();
    renderChrome();

    if (state.lib && state.lib.enabled === false) {
      showSetup(false); showTip("");
      emptyState("插件已停用", "在「设置 → 插件 → Kira Wiki · 知识晶格 → 常规」里打开「启用插件」。");
      return;
    }

    // ② 数据面失败只进「不可用 / 未初始化」态，绝不整屏报错
    try {
      const [tree, graph] = await Promise.all([
        api("/tree?" + qs({ kw_name: state.kw })),
        api("/graph?" + qs({ kw_name: state.kw }))
      ]);
      state.tree = tree; state.graph = graph;
      renderNav(); renderMetrics(); renderChrome();
      if (window.__lattice) {
        window.__lattice.setData(graph);
        window.__lattice.setFilter(state.filter);
      }
      const lgExt = $("lgExt");
      if (lgExt) lgExt.hidden = !(tree.stats && tree.stats.by_section && tree.stats.by_section.imported);
      const cur = state.lib && state.lib.current;
      const innit = cur && cur.initialized === false;
      const empty = graph.nodes.length === 0;
      if (innit && empty) {
        showSetup(true); showTip("");
        emptyState("这个目录还不是 Kira Wiki 知识库结构",
          `点下面的「创建 / 接管」在这一目录里建出 raw/ 与 wiki/ 生成区；` +
          `已有的笔记不会被改动，之后也会一并显示在晶格里。`);
      } else if (innit) {
        showSetup(false);
        showTip(`该目录已有 ${graph.nodes.length} 个页面（含你自己的笔记），还没有 wiki/ 生成区。`, async () => {
          await doKbInit();
        });
        hideEmpty();
      } else if (empty) {
        showSetup(true); showTip("");
        emptyState("知识库还是空的",
          "把素材丢进 raw/，或者在聊天里让我收录一篇文章；也可以直接用右上角 + 新建页面。");
      } else {
        showSetup(false); showTip("");
        if (!opts.keepDrawer) hideEmpty();
        else $("empty").classList.remove("show");
      }
    } catch (e) {
      state.tree = null; state.graph = null;
      if (window.__lattice) window.__lattice.setData({ nodes: [], links: [] });
      renderMetrics();
      showSetup(true); showTip("");
      const cur = state.lib && state.lib.current;
      if (cur) {
        emptyState("读不到知识库内容", e.message);
      } else {
        const sug = (state.lib && state.lib.suggested_path) || "data/kw";
        emptyState("还没有可用的知识库", `建议目录：${sug}。在下面填一个目录后点「创建 / 接管」，或直接用建议目录。`);
      }
    }
  }

  async function doKbInit() {
    const btn = $("kbsCreate") || $("tipAction");
    const name = ($("kbsName") && $("kbsName").value.trim()) || "";
    const path = ($("kbsPath") && $("kbsPath").value.trim()) || "";
    if (btn) btn.disabled = true;
    try {
      const r = await api("/kb/init", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, path })
      });
      state.kw = r.name || state.kw;
      try { localStorage.setItem(KW_KEY, state.kw); } catch (e) {}
      toast(`知识库「${r.name}」已就绪 · ${r.pages} 页` +
            (r.external ? ` · 载入 ${r.external} 篇已有笔记` : ""));
      await refresh();
    } catch (e) {
      toast("创建失败：" + e.message, true);
    } finally {
      if (btn) btn.disabled = false;
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
      onSelect: (n) => loadPage(n.id, n.origin),
      onOpen: (n) => loadPage(n.id, n.origin),
      onData: () => renderMetrics()
    });
    window.__lattice.setData({ nodes: [], links: [] });

    $("btnSearch").onclick = () => $("searchInput").focus();
    $("searchInput").addEventListener("input", onSearchInput);
    $("btnLint").onclick = runLint;
    $("btnAsk").onclick = runAsk;
    $("btnReindex").onclick = async () => {
      try {
        const r = await api("/index/rebuild", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kw: state.kw }) });
        toast(`索引已重建 · ${r.pages} 页`); refresh();
      } catch (e) { toast("重建失败：" + e.message, true); }
    };
    $("btnNew").onclick = async () => {
      const r = await openModal({
        title: "新建页面",
        text: "在生成区（wiki/）里建一页，建好后可直接编辑。",
        fields: [
          { key: "title", label: "标题", placeholder: "例如：知识晶格的设计" },
          { key: "section", label: "分区", value: "concepts", mono: true,
            placeholder: "sources / entities / concepts / analyses" }
        ],
        ok: "创建"
      });
      if (!r.ok) return;
      const title = (r.values.title || "").trim();
      const section = (r.values.section || "concepts").trim() || "concepts";
      if (!title) { toast("标题不能为空", true); return; }
      try {
        const res = await api("/page/new", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, section, kw: state.kw })
        });
        toast("已新建 " + res.rel);
        await refresh();
        loadPage(res.rel, res.origin || "wiki");
      } catch (e) { toast("新建失败：" + e.message, true); }
    };
    $("btnTheme").onclick = () => {
      const now = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = now;
      try { localStorage.setItem("kw-theme", now); } catch (e) {}
      $("btnTheme").querySelector("use").setAttribute("href", now === "light" ? "#i-sun" : "#i-moon");
    };
    if (document.documentElement.dataset.theme === "light") $("btnTheme").querySelector("use").setAttribute("href", "#i-sun");

    $("kbPicker").onchange = async () => {
      state.kw = $("kbPicker").value;
      try { localStorage.setItem(KW_KEY, state.kw); } catch (e) {}
      state.filter = null;
      await refresh();
    };
    $("kbsCreate").onclick = doKbInit;
    $("kbsRetry").onclick = () => refresh();
    $("kbsPath").addEventListener("keydown", (e) => { if (e.key === "Enter") doKbInit(); });

    $("btnClose").onclick = closeDrawer;
    $("btnTabRead").onclick = () => setMode("read");
    $("btnTabEdit").onclick = () => setMode("edit");
    $("btnSave").onclick = savePage;
    $("btnDelete").onclick = async () => {
      if (!state.current) return;
      const rel = state.current.rel;
      const ext = state.origin === "vault";
      const ok = await askModal("删除页面",
        `确定删除 ${rel}？此操作不可撤销。` +
        (ext ? "\n\n注意：这是你自己笔记区里的文件，删除后 Obsidian 里也会消失。" : ""),
        "删除");
      if (!ok) return;
      try {
        await api("/page/delete", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rel, kw: state.kw, origin: state.origin })
        });
        toast("已删除 " + rel);
        closeDrawer();
        refresh();
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
      if (!$("modal").hidden) { if (e.key === "Escape") closeModal({ ok: false, values: {} }); return; }
      if (e.target.matches("input,textarea,select")) { if (e.key === "Escape") e.target.blur(); return; }
      const k = e.key.toLowerCase();
      if (k === "/") { e.preventDefault(); $("searchInput").focus(); }
      else if (k === "e" && state.current) setMode(state.mode === "edit" ? "read" : "edit");
      else if (k === "l") runLint();
      else if (k === "a") runAsk();
      else if (k === "g") $("btnReindex").click();
      else if (k === "n") $("btnNew").click();
      else if (e.key === "Escape") { closeSheet(); closeDrawer(); }
    });

    refresh().then(() => {
      // 开屏退场 + 内容入场（refresh 内部已做超时，不会永久卡在开屏）
      setTimeout(() => {
        $("boot").classList.add("done");
        document.body.classList.add("loaded");
      }, 420);
    });
  }

  /* 调试钩子：面板状态只读暴露（排障用，不影响功能） */
  window.__KW = { get state() { return state; }, renderModel: renderModel };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
