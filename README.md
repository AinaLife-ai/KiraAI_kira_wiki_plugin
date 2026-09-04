# 📚 KiraAI_kira_wiki_plugin

> 让 AI 帮你把零碎素材编译成一本会自己长出来的知识库

KiraAI_kira_wiki_plugin 是 [KiraAI](https://github.com/xxynet/KiraAI) 框架的插件，灵感来自 Karpathy 的 [LLM Wiki 思路](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：**LLM 就是编译器，Obsidian 就是 IDE**。

你只管把素材（文章、笔记、聊天记录、随手抄的东西）丢进去，AI 会自动帮你整理成结构化的 wiki 页面，还带互相引用的双链，用 Obsidian 打开就是一张知识图谱。

---

## ✨ 它能干什么

| 命令 | 作用 |
| --- | --- |
| `wiki_init` | 初始化一个知识库（建好 raw/ 和 wiki/ 目录结构） |
| `wiki_ingest` | 收录素材：写入 raw/，LLM 自动生成摘要页、实体页、概念页、分析页 |
| `wiki_query` | 基于 wiki 页面回答问题，并标注信息来源 |
| `wiki_lint` | 知识库体检：查空页面、断链、目录结构 |
| `wiki_list` | 看一眼知识库里都有啥 |

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

> 没装 Obsidian？纯文本编辑器也能看，只是没有图谱和双链跳转。

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

- 插件主体：`main.py`（单文件，无第三方依赖）
- 工具注册走 KiraAI 的 `@register.tool` 装饰器
- 页面生成靠 `ctx.get_default_llm_client()`，可指定模型

## 📜 License

AGPL-3.0，随便玩，记得给个 star 就行（不是）

---

*Made with ❤️ by 爱奈丽 —— 一个很会聊天的代码*
