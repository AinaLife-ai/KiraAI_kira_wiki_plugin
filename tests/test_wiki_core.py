"""Kira Wiki · 服务层单测（纯标准库，不依赖框架）。

跑法（二选一）：
    python -m pytest tests/ -v
    python tests/test_wiki_core.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wiki_core as wc  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kwtest-"))
        self.kw = self.tmp / "kb"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_kb(self):
        wc.ensure_structure(self.kw)
        _write(self.kw / "wiki" / "sources" / "素材甲.md", "# 素材甲\n\n" + "内容" * 40)
        _write(self.kw / "wiki" / "concepts" / "概念乙.md",
               "---\ntitle: 概念乙\ntags: [x]\n---\n\n参见 [[素材甲]]。" + "字" * 30)

    def make_vault(self):
        """一个「用户自己的 Obsidian 库」：根目录与任意子目录都有 .md。"""
        _write(self.kw / "我的笔记.md", "# 我的笔记\n\n链接 [[计划]]，嵌入 ![[图.png]]")
        _write(self.kw / "项目" / "计划.md", "# 计划\n\n待办")
        _write(self.kw / ".obsidian" / "workspace.md", "ignore me")
        _write(self.kw / "附着" / ".hidden" / "x.md", "ignore me too")


class TestPaths(Base):
    def test_traversal_rejected(self):
        for bad in ("../evil.md", "a/../../evil.md", "/etc/passwd", "~/x.md", "C:/x.md"):
            with self.assertRaises(wc.WikiPathError):
                wc.resolve_rel(self.kw, bad)

    def test_normal_path_ok(self):
        got = wc.resolve_rel(self.kw, "a/b.md")
        self.assertTrue(str(got).startswith(str(self.kw.resolve())))

    def test_slugify(self):
        self.assertEqual(wc.slugify("Hello World!"), "hello-world")
        self.assertEqual(wc.slugify(""), "untitled")


class TestOrigin(Base):
    def test_looks_generated(self):
        self.assertTrue(wc.looks_generated("concepts/a.md"))
        self.assertTrue(wc.looks_generated("index.md"))
        self.assertFalse(wc.looks_generated("我的笔记.md"))
        self.assertFalse(wc.looks_generated("项目/计划.md"))

    def test_resolve_target_auto(self):
        _, origin = wc.resolve_target(self.kw, "entities/x.md")
        self.assertEqual(origin, wc.ORIGIN_WIKI)
        _, origin = wc.resolve_target(self.kw, "项目/x.md")
        self.assertEqual(origin, wc.ORIGIN_VAULT)

    def test_vault_cannot_escape_into_generated(self):
        with self.assertRaises(wc.WikiPathError):
            wc.resolve_target(self.kw, "wiki/index.md", wc.ORIGIN_VAULT)


class TestReadOnlyOpsDoNotCreate(Base):
    def test_iter_pages_on_missing_dir(self):
        self.assertEqual(wc.iter_pages(self.kw / "nope"), [])

    def test_stats_and_lint_do_not_create(self):
        self.assertFalse(self.kw.exists())
        wc.stats(self.kw)
        wc.lint(self.kw)
        wc.build_graph(self.kw)
        self.assertFalse(self.kw.exists(), "只读操作不得创建任何目录")


class TestExternalVault(Base):
    def test_scan_external(self):
        self.make_vault()
        pages = wc.iter_pages(self.kw)
        rels = sorted(p.rel for p in pages)
        self.assertIn("我的笔记.md", rels)
        self.assertIn("项目/计划.md", rels)
        self.assertNotIn(".obsidian/workspace.md", rels)
        self.assertNotIn("附着/.hidden/x.md", rels)
        for p in pages:
            self.assertEqual(p.origin, wc.ORIGIN_VAULT)

    def test_stats_split(self):
        self.make_kb()
        self.make_vault()
        st = wc.stats(self.kw)
        self.assertEqual(st["by_section"]["sources"], 1)
        self.assertEqual(st["by_section"]["concepts"], 1)
        self.assertEqual(st["by_section"][wc.IMPORTED], 2)
        self.assertEqual(st["root_pages"], 4)               # index/log/overview/conventions
        self.assertEqual(st["section_pages"], 4)            # 2 生成页 + 2 外部分区页
        self.assertEqual(st["pages"], 4 + 4)

    def test_stats_accepts_precomputed(self):
        self.make_kb()
        pages = wc.iter_pages(self.kw)
        self.assertEqual(wc.stats(self.kw, pages), wc.stats(self.kw))

    def test_graph_ignores_embeds_and_links_external(self):
        self.make_vault()
        _write(self.kw / "wiki" / "concepts" / "c.md", "见 [[计划]] 和 ![[图.png]]")
        g = wc.build_graph(self.kw)
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("我的笔记.md", ids)
        self.assertIn("项目/计划.md", ids)
        targets = {l["target"] for l in g["links"]}
        self.assertNotIn("图.png", targets, "嵌入 ![[...]] 不算链接")
        self.assertIn("项目/计划.md", targets)

    def test_can_edit_external(self):
        self.make_vault()
        path, origin = wc.write_page(self.kw, "项目/计划.md", "# 计划\n\n改过了")
        self.assertEqual(origin, wc.ORIGIN_VAULT)
        self.assertEqual(path.read_text("utf-8"), "# 计划\n\n改过了")
        # 删除也可以，但只限知识库根内
        wc.delete_page(self.kw, "项目/计划.md")
        self.assertFalse((self.kw / "项目" / "计划.md").exists())

    def test_cannot_write_outside_root(self):
        self.make_vault()
        for bad in ("../x.md", "/tmp/x.md"):
            with self.assertRaises(wc.WikiPathError):
                wc.write_page(self.kw, bad, "x")

    def test_rebuild_index_lists_external(self):
        self.make_kb()
        self.make_vault()
        wc.rebuild_index(self.kw)
        idx = (self.kw / "wiki" / "index.md").read_text("utf-8")
        self.assertIn(wc.SECTION_LABEL[wc.IMPORTED], idx)
        self.assertIn("我的笔记", idx)


class TestGeneratedArea(Base):
    def test_read_page_reports_section(self):
        self.make_kb()
        self.assertEqual(wc.read_page(self.kw, "concepts/概念乙.md")["section"], "concepts")
        self.assertEqual(wc.read_page(self.kw, "index.md")["section"], "root")
        (self.kw / "外部.md").write_text("# 外部", "utf-8")
        self.assertEqual(wc.read_page(self.kw, "外部.md", "vault")["section"], wc.IMPORTED)

    def test_write_read_delete(self):
        self.make_kb()
        wc.write_page(self.kw, "concepts/新页.md", "# 新页\n\n" + "字" * 60)
        pg = wc.read_page(self.kw, "concepts/新页.md")
        self.assertEqual(pg["origin"], wc.ORIGIN_WIKI)
        self.assertTrue(pg["writable"])
        wc.delete_page(self.kw, "concepts/新页.md")
        self.assertFalse((self.kw / "wiki" / "concepts" / "新页.md").exists())

    def test_append(self):
        self.make_kb()
        wc.write_page(self.kw, "concepts/a.md", "第一段")
        wc.append_page(self.kw, "concepts/a.md", "第二段")
        self.assertIn("第二段", wc.read_page(self.kw, "concepts/a.md")["content"])

    def test_core_page_protected(self):
        self.make_kb()
        with self.assertRaises(wc.WikiPathError):
            wc.delete_page(self.kw, "index.md")
        # 显式指定生成区时，wiki/ 根下只允许 4 个核心页
        with self.assertRaises(wc.WikiPathError):
            wc.write_page(self.kw, "notaroot.md", "x", origin=wc.ORIGIN_WIKI)
        # 不指定原点时，notaroot.md 属于「用户自己的笔记」，写在知识库根（合法）
        path, origin = wc.write_page(self.kw, "notaroot.md", "x")
        self.assertEqual(origin, wc.ORIGIN_VAULT)
        self.assertEqual(path, (self.kw / "notaroot.md").resolve())

    def test_only_md_and_size(self):
        self.make_kb()
        with self.assertRaises(wc.WikiPathError):
            wc.write_page(self.kw, "concepts/a.txt", "x")
        with self.assertRaises(wc.WikiPathError):
            wc.write_page(self.kw, "concepts/big.md", "x" * (wc.MAX_PAGE_BYTES + 1))


class TestRanking(Base):
    def make_many(self, n=20):
        wc.ensure_structure(self.kw)
        for i in range(n):
            _write(self.kw / "wiki" / "concepts" / f"页{i}.md", f"# 页{i}\n\n关键词 " + "正文" * 200)

    def test_top_k_honored(self):
        self.make_many(20)
        picked, meta = wc.rank_pages(self.kw, "关键词", top_k=5, per_page_chars=4000)
        self.assertEqual(len(picked), 5, "top_k 必须真正生效")
        self.assertEqual(meta["picked"], 5)

    def test_budget_honored(self):
        self.make_many(20)
        picked, meta = wc.rank_pages(self.kw, "关键词", top_k=50, per_page_chars=400, budget_chars=1200)
        self.assertLessEqual(meta["chars"] // 400, 3)
        self.assertLessEqual(len(picked), 4)

    def test_empty_query_has_meta(self):
        self.make_many(3)
        picked, meta = wc.rank_pages(self.kw, "", top_k=2)
        self.assertEqual(len(picked), 2)
        self.assertIn("top_k", meta)


if __name__ == "__main__":
    unittest.main(verbosity=2)
