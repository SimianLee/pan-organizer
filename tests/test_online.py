# -*- coding: utf-8 -*-
"""
pan-organizer 图书联网补全（bookonline）单元测试
===============================================
不联网：数据源用注册表里注入的假 provider，专测清洗/解析/匹配/缓存/降级/熔断。

运行：python tests/test_online.py   （零依赖）
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import book_online as bo        # noqa: E402
import pan_organizer as po      # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


# 假数据源：按书名返回预设分类文本，同时记录调用次数
FAKE_MAP = {
    "活着": "图书 > 小说 > 社会小说",
    "人类简史": "图书 > 历史 > 世界史",
    "新华字典": "图书 > 工具书 > 汉语工具书",
    "海贼王": "图书 > 动漫/幽默 > 日本漫画",
    "晦涩之书": "图书 > 成功/励志",          # 站点分类本地认不出 → 应判未识别
}
FAKE_CALLS = []


def fake_provider(title, ocfg, timeout, labels=None):
    FAKE_CALLS.append(title)
    return FAKE_MAP.get(title)


def fake_error_provider(title, ocfg, timeout, labels=None):
    FAKE_CALLS.append(title)
    raise OSError("模拟网络故障")


def fake_blocked_provider(title, ocfg, timeout, labels=None):
    FAKE_CALLS.append(title)
    raise bo.BlockedError("模拟验证码")


def make_classifier(tmp, provider="fake", **over):
    ocfg = dict(provider=provider, timeout=1, workers=1, delay=0, limit=0)
    ocfg.update(over)
    clf = bo.OnlineClassifier(ocfg, po.booksort_match_text,
                              cache_path=os.path.join(tmp, "online_cache.json"),
                              log=lambda *a: None)
    clf.labels = po.BOOK_LABELS
    return clf


def main():
    tmp = tempfile.mkdtemp(prefix="ponline_")
    bo.PROVIDERS["fake"] = fake_provider
    bo.PROVIDERS["fake_error"] = fake_error_provider
    bo.PROVIDERS["fake_blocked"] = fake_blocked_provider
    try:
        print("== 1) 配置合并：默认值 + 用户覆盖（llm 子块深合并） ==")
        cfg = bo.build_config({"provider": "llm", "llm": {"api_key": "k"}})
        check("顶层被覆盖", cfg["provider"] == "llm", str(cfg))
        check("llm 子块深合并（key 覆盖、model 保留默认）",
              cfg["llm"]["api_key"] == "k"
              and cfg["llm"]["model"] == bo.DEFAULT_CONFIG["llm"]["model"], str(cfg["llm"]))
        check("未指定的项取默认值", cfg["workers"] == bo.DEFAULT_CONFIG["workers"])
        check("空配置也能用", bo.build_config(None)["provider"] == "auto")

        print("\n== 2) 书名清洗 clean_title ==")
        cases = [
            ("《活着》.epub", "活着"),
            ("活着（余华代表作，精装，易烊千玺推荐阅读）.epub", "活着"),
            ("《中国历史百科全书10：民族与对外关系卷》主编：徐寒.pdf",
             "中国历史百科全书10"),
            ("[高清扫描版] 三体.epub", "三体"),
            ("人类简史：从动物到上帝.mobi", "人类简史"),
            ("万历十五年（增订纪念本）.azw3", "万历十五年"),
            ("围城 (1).pdf", "围城"),
            ("5.jpg", ""),
            ("未命名.pdf", ""),
            ("", ""),
        ]
        for raw, want in cases:
            got = bo.clean_title(raw)
            check(f"{raw!r} → {want!r}", got == want, f"实际 {got!r}")

        print("\n== 3) 当当页面解析（面包屑 / 商品名 / 相似度） ==")
        html = (
            '<html><head><title>《活着》余华 著【简介_书评_在线阅读】 - 当当图书</title>'
            '</head><body><div class="breadcrumb" id="breadcrumb">'
            "<a href='http://book.dangdang.com/'><b>图书</b></a>"
            "<span class='gt'>&gt;</span>"
            "<a href='http://category.dangdang.com/cp01.03.00.00.00.00.html'>小说</a>"
            "<span class='gt'>&gt;</span>"
            "<a href='http://category.dangdang.com/cp01.03.45.00.00.00.html'>社会小说</a>"
            "<span>活着（余华代表作）</span>"
            '<div class="outlets"><a href="http://v.dangdang.com/">尾品汇</a></div>'
            "</div></body></html>")
        segs = bo.parse_breadcrumb(html)
        check("面包屑解析出 图书>小说>社会小说（含 <b> 也能剥干净、站外链接被滤掉）",
              segs == ["图书", "小说", "社会小说"], str(segs))
        check("商品名提取（去掉【】与站点后缀）",
              bo.product_title(html) == "活着", repr(bo.product_title(html)))
        check("书名一致 → 相似度 1.0", bo.title_similarity("活着", "活着（余华代表作）") == 1.0)
        check("搜到别的书 → 相似度低",
              bo.title_similarity("三体", "活着") < bo.MIN_TITLE_SIMILARITY,
              str(bo.title_similarity("三体", "活着")))
        check("空面包屑不炸", bo.parse_breadcrumb("<html></html>") == [])

        print("\n== 4) 远端文本 → 本地类型（段优先，防止「小说>历史小说」被判历史） ==")
        mcases = [
            ("图书 > 小说 > 社会小说", "文学小说"),
            ("图书 > 小说 > 历史小说", "文学小说"),
            ("图书 > 历史 > 中国史", "历史传记"),
            ("图书 > 工具书 > 汉语工具书", "教材教辅"),
            ("图书 > 动漫/幽默 > 日本漫画", "漫画"),
            ("图书 > 少儿 > 绘本", "少儿绘本"),
            ("文学小说", "文学小说"),
            ("这本书属于小说", "文学小说"),
            ("图书 > 成功/励志", None),
            ("", None),
        ]
        for text, want in mcases:
            got = po.booksort_match_text(text)
            check(f"{text!r} → {want!r}", got == want, f"实际 {got!r}")

        print("\n== 5) 批量查询：去重 / 归类 / 命中统计 ==")
        FAKE_CALLS.clear()
        clf = make_classifier(tmp)
        # 注意：候选筛选是调用方的事（extsort_plan 只把"图书后缀 + 本地判不出"
        # 的文件送进来），分类器本身不认识文件类型，见模块 docstring 的职责划分。
        out = clf.classify_many([
            "活着.epub", "活着.pdf",            # 同一本书两种格式 → 只查一次
            "人类简史.mobi",
            "新华字典.pdf",
            "晦涩之书.pdf",                      # 站点分类本地认不出
        ])
        check("同书不同后缀都拿到类型",
              out.get("活着.epub") == "文学小说" and out.get("活着.pdf") == "文学小说",
              str(out))
        check("书名去重：只查了 4 个不同书名", len(FAKE_CALLS) == 4, str(FAKE_CALLS))
        check("工具书 → 教材教辅", out.get("新华字典.pdf") == "教材教辅", str(out))
        check("认不出的书名不产出结果", "晦涩之书.pdf" not in out, str(out))
        check("统计：命中 3 未识别 1",
              clf.stats["hit"] == 3 and clf.stats["miss"] == 1, str(clf.stats))

        print("\n== 6) 缓存：第二次零请求，且落盘可复用 ==")
        FAKE_CALLS.clear()
        clf2 = make_classifier(tmp)              # 同一缓存文件的新实例
        out2 = clf2.classify_many(["活着.epub", "人类简史.mobi"])
        check("缓存命中未再联网", FAKE_CALLS == [], str(FAKE_CALLS))
        check("缓存命中数量统计正确", clf2.stats["cache_hit"] == 2, str(clf2.stats))
        check("缓存结果与首次一致",
              out2.get("活着.epub") == "文学小说", str(out2))
        with open(os.path.join(tmp, "online_cache.json"), "r", encoding="utf-8") as f:
            cache = json.load(f)
        check("缓存文件含书名/类型/来源",
              cache["titles"]["活着"]["label"] == "文学小说"
              and cache["titles"]["活着"]["source"] == "fake",
              str(cache["titles"].get("活着")))
        check("未识别也缓存（避免反复无效查询）",
              cache["titles"]["晦涩之书"]["label"] is None, str(cache["titles"].get("晦涩之书")))

        print("\n== 7) 只降级不中断：网络异常 → 返回空结果，不抛错 ==")
        FAKE_CALLS.clear()
        clf3 = make_classifier(tmp, provider="fake_error")
        out3 = clf3.classify_many(["未知书甲.pdf", "未知书乙.pdf"])
        check("异常被吞掉，返回空映射", out3 == {}, str(out3))
        check("统计里记了出错次数", clf3.stats["error"] == 2, str(clf3.stats))

        print("\n== 8) 熔断：遇到验证码/限流就不再继续打站点 ==")
        FAKE_CALLS.clear()
        clf4 = make_classifier(tmp, provider="fake_blocked")
        clf4.classify_many([f"限流书{i}.pdf" for i in range(5)])
        check("只打了一次就熔断（并发 1）", len(FAKE_CALLS) == 1, str(FAKE_CALLS))
        check("熔断计数为 1", clf4.stats["blocked"] == 1, str(clf4.stats))

        print("\n== 9) limit：单次查询上限，避免一次跑飞 ==")
        FAKE_CALLS.clear()
        clf5 = make_classifier(tmp, limit=2)
        clf5.classify_many(["限量甲.pdf", "限量乙.pdf", "限量丙.pdf", "限量丁.pdf"])
        check("只查了 2 个", len(FAKE_CALLS) == 2, str(FAKE_CALLS))
        check("剩余数量被记录", clf5.stats["skipped_limit"] == 2, str(clf5.stats))

        print("\n== 10) probe：试查不写缓存（Web「试查」按钮用） ==")
        cache_file = os.path.join(tmp, "probe_cache.json")
        clf6 = bo.OnlineClassifier({"provider": "fake", "delay": 0},
                                   po.booksort_match_text, cache_path=cache_file,
                                   log=lambda *a: None)
        clf6.labels = po.BOOK_LABELS
        r = clf6.probe("《活着》.epub")
        check("probe 返回清洗后的书名", r["title"] == "活着", str(r))
        check("probe 返回类型与来源",
              r["label"] == "文学小说" and r["source"] == "fake", str(r))
        check("probe 不落缓存", not os.path.exists(cache_file), cache_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n========== 测试结果：通过 {PASS}，失败 {FAIL} ==========")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
