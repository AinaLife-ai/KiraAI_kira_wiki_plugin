# 📚 KiraAI_kira_wiki_plugin

> 让 AI 帮你把零碎素材编译成一本会自己长出来的知识库

KiraAI_kira_wiki_plugin 是 [KiraAI](https://github.com/xxynet/KiraAI) 框架的插件，灵感来自 Karpathy 的 [LLM Wiki 思路](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：**LLM 就是编译器，Obsidian 就是 IDE**。

你只管把素材（文章、笔记、聊天记录、随手抄的东西）丢进去，AI 会自动帮你整理成结构化的 wiki 页面，还带互相引用的双链，用 Obsidian 打开就是一张知识图谱。

---

## 兼容性

支持 KiraAI 2.33.1+ 与 3.0 全系列（含 v3.0.0-rc.1；特征识别，不绑版本号）。
`model` 配置项两种写法都认，**推荐直接照抄 webui 里看到的名字**：

- **v1.0.1**：模型那一半——「可读模型名」自动解析为内部稳定 ID（rc.1 框架解析只认内部 ID/迁移旧名）；
- **v1.0.2**：服务商那一半——「服务商显示名」（如 `Agnes（CN）`）自动解析为实例 ID，与模型解析配套；
  解析不到时**告警 + 回退默认 LLM**（此前会静默返回空串，表现为「wiki 生成没反应」）。

| 写法 | 2.x | 3.0 rc.1+ |
| --- | --- | --- |
| `提供商显示名:可读模型名`（推荐） | ✓ | ✓（v1.0.2+） |
| `实例ID:可读模型名` | ✓ | ✓（v1.0.1+） |
| `实例ID:内部ID` | ✓ | ✓ |
| 留空 | 用框架默认 LLM | 同左 |

**v1.1.0**
- 工具精简 **5 → 4**（`wiki_list` + `wiki_lint` 合并为 `wiki_status`，`check=true` 出完整体检）；
  工具 schema 每轮请求都会注入，少一个工具就是少一份常驻 token。
- 全部磁盘操作移出事件循环（`asyncio.to_thread`，共 37 处）：大库扫描不再阻塞聊天。
- 检索改为**关键词排序 + 字符预算**（默认 8 页 / 14k 字符），不再把整库塞进 prompt。
- 新增**侧边栏面板**（见下）。
- 本插件**不注入任何系统提示词**（无 `on.llm_request` 钩子），因此不会破坏 prompt 前缀缓存。

## 🖥️ 侧边栏面板（v1.1.0）

在 KiraAI 主 WebUI 的侧边栏点「知识库」进入。设计语言：墨/纸双主题、Lucide 图标（内联 sprite，
无 CDN、无 emoji）、力导向晶格画布。

| 区域 | 能做什么 |
| --- | --- |
| 知识晶格 | 全库 wikilink 图：拖拽节点、滚轮缩放、双击聚焦、悬停点亮邻域；左栏分区可过滤 |
| 阅读抽屉 | 点节点打开页面：frontmatter 单独成块，`[[wikilink]]` 可点击跳转（断链为虚线下划线） |
| 热编辑 | 抽屉右上「铅笔」进入编辑（或按 `E`），`⌘/Ctrl+S` 保存，未保存有状态提示；保存即写回 `wiki/*.md` |
| 搜索 | 顶栏搜索框（或按 `/`）：关键词排序命中，命中页自动高亮 |
| 问答 | 右上「sparkle」（或按 `A`）：走与工具同一条检索链，回答标注参考页面，可点来源跳转 |
| 体检 | 右上「pulse」（或按 `L`）：空页 / 断链 / 孤立页清单 + 分区统计 |
| 索引 | 右上「refresh」（或按 `G`）：按实际页面重建 `index.md` |
| 新建 | 右上「plus」（或按 `N`）：在指定分区建页并立即打开编辑 |

安全边界：面板所有读写都经 `resolve_rel()` 校验，只允许知识库内相对路径的 `.md`；
`../`、绝对路径、盘符一律 400 拒绝，核心页（`index/overview/log/conventions`）不可删。

## ✨ 它能干什么

AI 通过 4 个工具操作知识库（工具 schema 每轮请求都会注入，所以保持精简）：

| 工具 | 作用 |
| --- | --- |
| `wiki_init` | 初始化一个知识库（建好 raw/ 与 wiki/ 目录结构并注册） |
| `wiki_ingest` | 收录素材：写入 raw/，LLM 自动生成摘要页、实体页、概念页、分析页 |
| `wiki_query` | 查询问答：**按相关度排序**挑页（默认 8 页 / 14k 字符预算），回答标注来源页面 |
| `wiki_status` | 状态与体检：默认列概览；`check=true` 出完整体检（空页 / 断链 / 孤立页） |

> v1.1.0 起 `wiki_list` 与 `wiki_lint` 合并为 `wiki_status`（语义重叠，且少一个工具就少一份常驻 token）。

## 🗂️ 目录结构

一个知识库（Obsidian vault）长这样：

```
data/kw/                  # 默认知识库（可在配置里改路径）
├── raw/                  # 素材区（你丢东西进来，LLM 只读）
│   └── assets/
└── wiki/                 # AI 生成区（LLM 自动维护）
    ├── index.md          # 自动重建的索引
    ├── overview.md       # 总览
    ├── log.md            # 操作日志
    ├── conventions.md    # 使用约定
    ├── sources/          # 每篇素材的摘要页
    ├── entities/         # 实体页（人物/组织/工具）
    ├── concepts/         # 概念页（理论/方法/模式）
    └── analyses/         # 分析页（综合论述）
```

## 🚀 快速开始

1. 把插件目录放进 KiraAI 的 `data/plugins/` 下，重启或热重载
2. 对 bot 说：**「建个知识库，路径 C:\xxx\my-kw」**（会调用 `wiki_init`）
3. 丢素材：**「把这段话收进知识库：……」**（会调用 `wiki_ingest`）
4. 用 Obsidian 打开知识库目录，看图谱、看双链，完事
5. 或者直接在 KiraAI 主 WebUI 侧边栏点 **「知识库」**：看晶格图、读页面、**直接改页面**（见下）

> 没装 Obsidian？纯文本编辑器也能看，只是没有图谱和双链跳转；面板里也能读能改。

## ⚙️ 配置

`schema.json` 里两个配置段：

- **section_basic**
  - `enabled`：开关，默认开
  - `default_kw_path`：默认知识库路径，默认 `data/kw`（相对 KiraAI 根目录），不填的话每次要指定 kw_name
- **section_llm**
  - `model`：生成 wiki 页用的模型，留空用 KiraAI 默认 LLM
  - `max_pages_per_ingest`：单次收录最多生成的页面数，默认 15

## 🧠 工作原理（一句话版）

你丢素材 → 存进 `raw/` → LLM 读素材，输出 JSON（摘要/实体/概念/分析）→ 插件把 JSON 写成带 `[[wikilink]]` 的 md 页面 → 更新索引和总览 → 完事

LLM 生成失败也不慌，素材已经躺在 `raw/` 里了，不会丢。

## 🛠️ 开发

模块划分：

| 文件 | 职责 |
| --- | --- |
| `main.py` | 插件入口：4 个工具 + 11 个面板 API + 页面注册 |
| `wiki_core.py` | 服务层：路径安全、索引、图、检索排序、体检（纯标准库、可单测） |
| `web/` | 面板前端：`index.html` / `style.css` / `app.js` / `graph.js`（零依赖、Lucide sprite 内联） |
| `assets/menu.svg` | 侧边栏菜单图标（Lucide 风格） |

- 工具走 KiraAI 的 `@register.tool`；面板接口走 `@register.api`（全部 `auth=True`）；页面走 `@register.page`
- 磁盘操作一律 `asyncio.to_thread`，不在事件循环里做同步 I/O
- 模型解析：`ctx.get_llm_client()` 优先，显示名走 `provider_mgr` 反查（2.x / 3.0 双世代）
- 无第三方依赖（`wiki_core.py` 纯标准库）

## 📝 更新日志

<details>
<summary>展开查看更新日志（当前版本：v1.1.0）</summary>

### v1.1.0
- 🖥️ **侧边栏面板**：知识晶格（力导向 wikilink 图）、阅读抽屉、**热编辑**（`⌘/Ctrl+S`）、搜索、体检、问答、
  重建索引、新建页；墨/纸双主题，Lucide 图标内联（无 CDN、无 emoji），`prefers-reduced-motion` 降级。
- 🧩 **工具精简 5 → 4**：`wiki_list` + `wiki_lint` 合并为 `wiki_status(check=false|true)`。
- ⚡ **非阻塞**：37 处磁盘操作移出事件循环（`asyncio.to_thread`）。
- 🎯 **检索优化**：关键词排序 + 字符预算（默认 8 页 / 14k 字符），不再把整库塞进 prompt。
- 🐛 **修复**：LLM 返回的 `entities`/`concepts` 非数组时切片崩溃；指定不存在的库名会静默改写到默认库；
  同名素材覆盖无提示。
- 🖼️ **图标整形**：裁掉纯色外边、512×512 圆角透明、调色板压缩（1263 KB → 71.5 KB）。
- 🔒 **面板安全边界**：读写经 `resolve_rel()` 校验，只允许库内相对路径 `.md`，穿越/绝对路径一律 400。

### v1.0.2
- 🐛 **模型配置解析补全**：`提供商:可读模型名` 中的**服务商显示名**（如 `Agnes（CN）`）现在会解析成实例 ID；
  解析失败告警并回退默认 LLM（此前会静默返回空串，表现为「wiki 生成没反应」）。

### v1.0.1
- 🐛 **3.0 兼容**：`model` 配置沿用 2.x 习惯的「可读模型名」，插件自动反查内部稳定 ID
  （rc.1 的框架解析只认内部 ID / 迁移旧名）。

</details>

## 📜 License

AGPL-3.0，随便玩，记得给个 star 就行（不是）

---

*Made with ❤️ by 爱奈丽 & znq19 —— 一个很会聊天的代码*
