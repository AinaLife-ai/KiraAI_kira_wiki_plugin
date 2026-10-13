/* ═══════════════════════════════════════════════════════════════════════════
   LATTICE — 知识晶格画布
   自研力导向图：斥力 + 弹簧 + 向心 + 光标引力；支持拖拽 / 缩放 / 平移 /
   悬停点亮邻域 / 双击聚焦 / 分区过滤。零依赖，DPR 感知。
   ═════════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  /* 大图降级阈值：超过后改用「网格近似斥力 + 限量标签 + 关光晕」，
     把每帧代价从 O(n²) 压到 O(n)，手机端 500+ 页也不至于卡死。 */
  const BIG_NODES = 300;
  const LABEL_LIMIT = 220;
  const GLOW_LIMIT = 400;
  /* 静止判定：每帧最大位移 < 1/4 像素、且持续 CALM_MS —— 视觉上已经不动了，停帧。 */
  const CALM_PX = 0.25;
  const CALM_MS = 700;

  const SECTION_COLOR = {
    sources: "--src", entities: "--ent", concepts: "--con", analyses: "--ana",
    imported: "--ext", root: "--fg-faint"
  };
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  function Lattice(canvas, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts || {};
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.nodes = []; this.links = [];
    this.byId = new Map();
    this.mouse = { x: 0, y: 0, inside: false };
    this.drag = null; this.pan = null;
    this.zoom = 1; this.tx = 0; this.ty = 0;
    this.filter = null;
    this.hover = null; this.selected = null;
    this.reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this._bind(); this.resize();
    this._tick = this._tick.bind(this);
    this._running = true; this._idle = 0;
    this._loop = requestAnimationFrame(this._tick);
  }

  /* 停帧后由任何交互唤醒（省电、省 CPU；不再无条件空转 rAF） */
  Lattice.prototype._wake = function () {
    this._calmSince = 0;
    if (this._running) return;
    this._running = true;
    this._loop = requestAnimationFrame(this._tick);
  };

  Lattice.prototype.resize = function () {
    const r = this.cv.getBoundingClientRect();
    this.w = r.width; this.h = r.height;
    this.cv.width = Math.floor(r.width * this.dpr);
    this.cv.height = Math.floor(r.height * this.dpr);
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this._wake();
  };

  /* 舞台矩形：晶格只在这个框里生长（默认整屏） */
  Lattice.prototype.bounds = function () {
    if (this.opts.bounds) { const b = this.opts.bounds(); if (b && b.w > 40 && b.h > 40) return b; }
    return { x: 0, y: 0, w: this.w, h: this.h };
  };

  Lattice.prototype.center = function () {
    const b = this.bounds();
    return { x: b.x + b.w / 2, y: b.y + b.h / 2 };
  };

  Lattice.prototype.setData = function (graph) {
    const prev = this.byId;
    const c0 = this.center();
    const cx = c0.x, cy = c0.y;
    this.nodes = (graph.nodes || []).map((n, i) => {
      const old = prev.get(n.id);
      const b0 = this.bounds();
      /* 黄金角螺旋：均匀铺满舞台矩形，避免中心团簇、四角留白 */
      const GA = 2.399963229728653;
      const t = (i + 0.5) / Math.max(1, graph.nodes.length);
      const rr = Math.sqrt(t);
      const a = i * GA;
      const px = Math.cos(a) * rr * (b0.w * 0.42);
      const py = Math.sin(a) * rr * (b0.h * 0.42);
      return {
        ...n,
        x: old ? old.x : cx + px,
        y: old ? old.y : cy + py,
        vx: 0, vy: 0,
        r: 2.2 + Math.min(6, Math.log2(2 + (n.size || 0) / 400)),
        appear: this.reduced ? 1 : 0
      };
    });
    this.byId = new Map(this.nodes.map(n => [n.id, n]));
    this.links = (graph.links || [])
      .map(l => ({ a: this.byId.get(l.source), b: this.byId.get(l.target) }))
      .filter(l => l.a && l.b);
    const deg = new Map();
    this.links.forEach(l => {
      deg.set(l.a.id, (deg.get(l.a.id) || 0) + 1);
      deg.set(l.b.id, (deg.get(l.b.id) || 0) + 1);
    });
    this.nodes.forEach(n => { n.deg = deg.get(n.id) || 0; });
    this.zoomToFit();
    this._wake();
    if (this.opts.onData) this.opts.onData({ nodes: this.nodes.length, links: this.links.length });
  };

  Lattice.prototype.setFilter = function (section) {
    this.filter = section || null;
    this.nodes.forEach(n => {
      const on = !this.filter || n.section === this.filter;
      n.targetAlpha = on ? 1 : 0.07;
      if (n.alpha === undefined) n.alpha = on ? 1 : 0.07;
    });
    this.links.forEach(l => {
      l.on = !this.filter || (l.a.section === this.filter || l.b.section === this.filter);
    });
  };

  Lattice.prototype.highlight = function (rels) {
    this.hits = rels && rels.length ? new Set(rels) : null;
  };

  Lattice.prototype.focus = function (id) {
    this.selected = id;
    const n = this.byId.get(id);
    if (!n) return;
    const c = this.center();
    this.zoom = Math.max(this.zoom, 1.25);
    this.tx = c.x - n.x * this.zoom;
    this.ty = c.y - n.y * this.zoom;
    n.pulse = 1;
  };

  Lattice.prototype.zoomToFit = function () {
    if (!this.nodes.length) return;
    const c0 = this.center();
    let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
    this.nodes.forEach(n => {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    });
    const b = this.bounds();
    const pad = 74;
    const sx = (b.w - pad) / Math.max(1, (maxX - minX));
    const sy = (b.h - pad) / Math.max(1, (maxY - minY));
    this.zoom = Math.max(0.35, Math.min(1.25, Math.min(sx, sy)));
    this.tx = (b.x + b.w / 2) - ((minX + maxX) / 2) * this.zoom;
    this.ty = (b.y + b.h / 2) - ((minY + maxY) / 2) * this.zoom;
  };

  Lattice.prototype._bind = function () {
    const cv = this.cv;
    cv.addEventListener("mousemove", (e) => {
      const r = cv.getBoundingClientRect();
      this.mouse.x = e.clientX - r.left; this.mouse.y = e.clientY - r.top;
      this.mouse.inside = true;
      const n = this.pick(this.mouse.x, this.mouse.y);
      this.hover = n ? n.id : null;
      if (this.drag && this.drag.node) {
        const p = this.toWorld(this.mouse.x, this.mouse.y);
        this.drag.node.x = p.x; this.drag.node.y = p.y;
        this.drag.node.vx = this.drag.node.vy = 0;
        this.drag.moved = true;
      } else if (this.pan) {
        this.tx += e.movementX || 0; this.ty += e.movementY || 0;
      }
      cv.style.cursor = this.drag ? "grabbing" : (n ? "pointer" : "grab");
      this._wake();
    });
    cv.addEventListener("mouseleave", () => { this.mouse.inside = false; this.hover = null; });
    cv.addEventListener("mousedown", (e) => {
      this._touched = true;
      const n = this.pick(this.mouse.x, this.mouse.y);
      if (n) this.drag = { node: n, moved: false };
      else this.pan = true;
      cv.classList.add("grabbing");
      this._wake();
    });
    window.addEventListener("mouseup", () => {
      if (this.drag) {
        const { node, moved } = this.drag;
        if (!moved) { this.selected = node.id; if (this.opts.onSelect) this.opts.onSelect(node); }
        else if (this.opts.onMove) this.opts.onMove(node);
      }
      this.drag = null; this.pan = null; cv.classList.remove("grabbing");
    });
    cv.addEventListener("dblclick", () => {
      const n = this.pick(this.mouse.x, this.mouse.y);
      if (n) { this.focus(n.id); if (this.opts.onOpen) this.opts.onOpen(n); }
      else this.zoomToFit();
    });
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const before = this.toWorld(mx, my);
      const k = Math.exp(-e.deltaY * 0.0012);
      this.zoom = Math.max(0.22, Math.min(4, this.zoom * k));
      const after = this.toWorld(mx, my);
      this.tx += (after.x - before.x) * this.zoom;
      this.ty += (after.y - before.y) * this.zoom;
      this._wake();
    }, { passive: false });
    window.addEventListener("resize", () => this.resize());
  };

  Lattice.prototype.toWorld = function (sx, sy) {
    return { x: (sx - this.tx) / this.zoom, y: (sy - this.ty) / this.zoom };
  };

  Lattice.prototype.screen = function (n) {
    return { x: n.x * this.zoom + this.tx, y: n.y * this.zoom + this.ty };
  };

  Lattice.prototype.pick = function (sx, sy) {
    let best = null, bestD = 16;
    for (const n of this.nodes) {
      if ((n.alpha || 1) < 0.3) continue;
      const s = this.screen(n);
      const d = Math.hypot(s.x - sx, s.y - sy);
      if (d < Math.max(10, n.r * this.zoom + 7) && d < bestD) { best = n; bestD = d; }
    }
    return best;
  };

  Lattice.prototype._physics = function () {
    const N = this.nodes, L = this.links;
    const stage = this.bounds();                      //  不要叫 b：内层循环曾同名遮蔽导致 NaN
    const cx = stage.x + stage.w / 2, cy = stage.y + stage.h / 2;
    const rest = Math.max(78, Math.min(stage.w, stage.h) * 0.135);   // 弹簧自然长度随舞台放大
    const repel = 2600 + Math.min(stage.w, stage.h) * 3.2;
    // 斥力：小图精确 O(n²)；大图走网格质心近似（O(n·k)），代价可控
    if (N.length > BIG_NODES) {
      const cell = Math.max(48, rest);
      const grid = new Map();
      for (const n of N) {
        const k = Math.floor(n.x / cell) + ":" + Math.floor(n.y / cell);
        let b = grid.get(k);
        if (!b) { b = { x: 0, y: 0, n: 0 }; grid.set(k, b); }
        b.x += n.x; b.y += n.y; b.n += 1;
      }
      for (const b of grid.values()) { b.x /= b.n; b.y /= b.n; }
      for (const n of N) {
        const gx = Math.floor(n.x / cell), gy = Math.floor(n.y / cell);
        const own = gx + ":" + gy;
        for (let ox = -1; ox <= 1; ox++) {
          for (let oy = -1; oy <= 1; oy++) {
            const key = (gx + ox) + ":" + (gy + oy);
            const b = grid.get(key);
            if (!b) continue;
            if (key === own && b.n === 1) continue;      // 自己那一格只有自己 ⇒ 跳过
            let dx = b.x - n.x, dy = b.y - n.y;
            let d2 = dx * dx + dy * dy;
            if (!(d2 >= 1)) { dx = (Math.random() - .5) * 2; dy = (Math.random() - .5) * 2; d2 = 4; }
            const d = Math.sqrt(d2);
            const f = repel * b.n / d2;
            n.vx -= (dx / d) * f; n.vy -= (dy / d) * f;
          }
        }
      }
    } else {
      for (let i = 0; i < N.length; i++) {
        const a = N[i];
        for (let j = i + 1; j < N.length; j++) {
          const q = N[j];
          let dx = q.x - a.x, dy = q.y - a.y;
          let d2 = dx * dx + dy * dy;
          if (!(d2 >= 1)) { dx = (Math.random() - .5) * 2; dy = (Math.random() - .5) * 2; d2 = 4; }
          const d = Math.sqrt(d2);
          const f = repel / d2;
          const ux = dx / d, uy = dy / d;
          a.vx -= ux * f; a.vy -= uy * f;
          q.vx += ux * f; q.vy += uy * f;
        }
      }
    }
    // 弹簧（连线）
    for (const l of L) {
      const dx = l.b.x - l.a.x, dy = l.b.y - l.a.y;
      const d = Math.max(1, Math.hypot(dx, dy));
      const f = (d - rest) * 0.0055;
      const ux = dx / d, uy = dy / d;
      l.a.vx += ux * f; l.a.vy += uy * f;
      l.b.vx -= ux * f; l.b.vy -= uy * f;
    }
    // 向心 + 光标引力 + 阻尼
    const m = this.mouse.inside ? this.toWorld(this.mouse.x, this.mouse.y) : null;
    for (const n of N) {
      n.vx += (cx - n.x) * 0.0016;
      n.vy += (cy - n.y) * 0.0016;
      /* 舞台软边界：越界越多，回拉力越强（避免节点漂出可视区） */
      const mX = stage.w * 0.46, mY = stage.h * 0.46;
      const dX = n.x - cx, dY = n.y - cy;
      if (Math.abs(dX) > mX) n.vx -= (Math.abs(dX) - mX) * 0.06 * Math.sign(dX);
      if (Math.abs(dY) > mY) n.vy -= (Math.abs(dY) - mY) * 0.06 * Math.sign(dY);
      if (m && !this.reduced) {
        const dx = m.x - n.x, dy = m.y - n.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < 40000 && d2 > 1) {
          const d = Math.sqrt(d2);
          n.vx += (dx / d) * 0.10; n.vy += (dy / d) * 0.10;
        }
      }
      if (!isFinite(n.x) || !isFinite(n.y) || !isFinite(n.vx) || !isFinite(n.vy)) {
        n.x = cx + (Math.random() - .5) * stage.w * 0.5;
        n.y = cy + (Math.random() - .5) * stage.h * 0.5;
        n.vx = n.vy = 0;
      }
      if (this.drag && this.drag.node === n) continue;
      n.vx *= 0.86; n.vy *= 0.86;
      n.x += Math.max(-14, Math.min(14, n.vx));
      n.y += Math.max(-14, Math.min(14, n.vy));
      if (n.alpha === undefined) n.alpha = 0;
      const ta = n.targetAlpha === undefined ? 1 : n.targetAlpha;
      n.alpha += (ta - n.alpha) * 0.12;
      if (n.appear < 1) n.appear = Math.min(1, n.appear + 0.035);
      if (n.pulse) n.pulse = Math.max(0, n.pulse - 0.02);
    }
  };

  Lattice.prototype._labelFits = function (n, s) {
    const txt = (n.title || n.name).slice(0, 22);
    this.ctx.font = `${n.id === this.selected ? 500 : 400} 11px ui-monospace,Menlo,monospace`;
    const w = this.ctx.measureText(txt).width + 12;
    const h = 14;
    const x0 = s.x + 6, y0 = s.y - h / 2;
    for (const b of (this._boxes || [])) {
      if (x0 < b.x + b.w && x0 + w > b.x && y0 < b.y + b.h && y0 + h > b.y) return false;
    }
    (this._boxes = this._boxes || []).push({ x: x0, y: y0, w, h });
    return true;
  };

  Lattice.prototype._draw = function () {
    const c = this.ctx;
    c.clearRect(0, 0, this.w, this.h);
    this._boxes = [];
    const focusId = this.hover || this.selected;
    const neighbors = this._nb || (this._nb = new Map());
    if (focusId && !neighbors.has(focusId)) {
      const s = new Set([focusId]);
      this.links.forEach(l => {
        if (l.a.id === focusId) s.add(l.b.id);
        if (l.b.id === focusId) s.add(l.a.id);
      });
      neighbors.set(focusId, s);
    }
    const active = (n) => !focusId || (neighbors.get(focusId) || new Set([focusId])).has(n.id);

    // 连线
    for (const l of this.links) {
      const on = this.filter ? l.on : true;
      const near = this.hover && (l.a.id === this.hover || l.b.id === this.hover);
      const sel = this.selected && (l.a.id === this.selected || l.b.id === this.selected);
      const a1 = Math.min(l.a.alpha, l.b.alpha) * (on ? 1 : 0.05);
      c.strokeStyle = near ? cssVar("--acid") : (sel ? cssVar("--ice") : cssVar("--hair-hi"));
      c.globalAlpha = near ? 0.85 : (sel ? 0.5 : 0.32 * a1 + 0.05);
      c.lineWidth = near ? 1.2 : 0.6;
      const sa = this.screen(l.a), sb = this.screen(l.b);
      c.beginPath(); c.moveTo(sa.x, sa.y); c.lineTo(sb.x, sb.y); c.stroke();
      if (near) {
        const t = (performance.now() % 2600) / 2600;
        const px = sa.x + (sb.x - sa.x) * t, py = sa.y + (sb.y - sa.y) * t;
        c.globalAlpha = 0.9; c.fillStyle = cssVar("--acid");
        c.beginPath(); c.arc(px, py, 1.8, 0, 6.2832); c.fill();
      }
    }
    // 节点（先画普通节点，悬停/选中最后画，保证标签优先级）
    const order = this.nodes.slice().sort((a, b) => {
      const pa = (a.id === this.hover || a.id === this.selected) ? 1 : 0;
      const pb = (b.id === this.hover || b.id === this.selected) ? 1 : 0;
      return pa - pb;
    });
    for (const n of order) {
      const s = this.screen(n);
      const alpha = (n.alpha === undefined ? 1 : n.alpha) * n.appear;
      if (alpha <= 0.02) continue;
      const col = cssVar(SECTION_COLOR[n.section] || "--fg");
      const isHot = this.hits ? this.hits.has(n.id) : false;
      const dim = (focusId && !active(n)) ? 0.18 : 1;
      const r = n.r * this.zoom * (0.6 + 0.4 * n.appear) * (isHot ? 1.5 : 1);
      c.globalAlpha = alpha * dim;
      // 光晕（节点过多时只给热点画，省掉每帧的渐变对象）
      const hot = n.id === this.hover || n.id === this.selected || isHot;
      if (hot || this.nodes.length <= GLOW_LIMIT) {
        const g = c.createRadialGradient(s.x, s.y, 0, s.x, s.y, Math.max(6, r * 6));
        g.addColorStop(0, col); g.addColorStop(1, "transparent");
        c.fillStyle = g; c.globalAlpha = alpha * dim * (n.id === this.hover ? 0.34 : 0.14);
        c.beginPath(); c.arc(s.x, s.y, Math.max(6, r * 6), 0, 6.2832); c.fill();
      }
      // 实体
      c.globalAlpha = alpha * dim;
      c.fillStyle = col;
      c.beginPath(); c.arc(s.x, s.y, Math.max(1.4, r), 0, 6.2832); c.fill();
      if (n.id === this.selected || n.pulse) {
        c.strokeStyle = col; c.globalAlpha = alpha * (n.pulse || 0.6);
        c.lineWidth = 1;
        c.beginPath(); c.arc(s.x, s.y, Math.max(6, r + 10 + (1 - (n.pulse || 0)) * 16), 0, 6.2832); c.stroke();
      }
      // 标签：悬停 / 选中 / 搜索命中永远画；大图只画这些，避免几千次 measureText 拖死帧
      const showLabel = hot || (this.nodes.length <= LABEL_LIMIT && n.deg >= 2 && this.zoom > 0.55);
      if (showLabel && this._labelFits(n, s)) {
        c.globalAlpha = alpha * (n.id === this.hover || n.id === this.selected ? 0.98 : 0.5);
        c.fillStyle = cssVar("--fg");
        c.font = `${n.id === this.selected ? 500 : 400} ${11}px ui-monospace,Menlo,monospace`;
        c.textAlign = "left"; c.textBaseline = "middle";
        const label = (n.title || n.name).slice(0, 22);
        c.fillText(label, s.x + Math.max(3, r) + 5, s.y);
      }
    }
    c.globalAlpha = 1;
  };

  /* 每帧最大位移（像素）—— 比速度更贴近「看起来有没有在动」 */
  Lattice.prototype._drift = function () {
    let m = 0;
    for (const n of this.nodes) {
      if (n._px !== undefined) {
        const dx = Math.abs(n.x - n._px), dy = Math.abs(n.y - n._py);
        if (dx > m) m = dx;
        if (dy > m) m = dy;
      }
      n._px = n.x; n._py = n.y;
    }
    return m;
  };

  Lattice.prototype._tick = function () {
    this._frames = (this._frames || 0) + 1;
    if (this._frames === 150 && !this._touched && this.nodes.length > 1) this.zoomToFit();
    if (!this.reduced) this._physics();
    else if (!this._laid) { for (let i = 0; i < 260; i++) this._physics(); this._laid = true; }
    this._draw();

    // 布局静止 ⇒ 停帧（不空转 CPU/电）；任何交互或数据变化都会 _wake() 重新起跳
    const busy = this.drag || this.pan || this.mouse.inside ||
      this.nodes.some(n => n.appear < 1 || n.pulse || (n.alpha !== undefined && n.alpha < 0.99));
    const drift = this._drift();
    this._lastDrift = drift;
    this._lastBusy = busy;
    const now = performance.now();
    if (!busy && this._frames > 12 && drift < CALM_PX) {
      if (!this._calmSince) this._calmSince = now;
      if (now - this._calmSince > CALM_MS) { this._running = false; return; }
    } else {
      this._calmSince = 0;
    }
    this._loop = requestAnimationFrame(this._tick);
  };

  Lattice.prototype.destroy = function () {
    this._running = false;
    cancelAnimationFrame(this._loop);
  };

  global.Lattice = Lattice;
})(window);
