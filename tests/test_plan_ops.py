# -*- coding: utf-8 -*-
"""
pan-organizer 计划导出/复用（--plan + --from-plan）相关离线单测：
  1) _load_plan_file 读取新格式 {meta, ops} → 重建可直接执行的操作列表
  2) _load_plan_file 兼容旧格式（纯 ops 数组）
  3) execute_ops 把"源不存在(HTTP 404)"计为跳过而非失败（计划可安全重复执行）
  4) _load_plan_file 对损坏/缺失字段给出明确报错

运行：python tests/test_plan_ops.py   （零依赖）
"""

import io
import json
import os
import sys
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import pan_organizer  # noqa: E402

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


def write_plan(tmp_dir, name, data):
    p = os.path.join(tmp_dir, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def main():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="pan-organizer_plan_")

    print("== 1) 新格式计划：meta + ops → 重建 op（entry 轻对象 / action 默认 move）==")
    plan_path = write_plan(tmp, "plan-new.json", {
        "meta": {"type": "extsort", "created_at": 1789000000, "src": "/网盘/a",
                 "dest": "/网盘/归档", "count": 2, "total_size": 3000},
        "ops": [
            {"src": "/网盘/a/x.pdf", "dst": "/网盘/归档/pdf/x.pdf",
             "size": 1000, "rule": ".pdf", "renamed": False},
            {"src": "/网盘/a/y.pdf", "dst": "/网盘/归档/pdf/y (1).pdf",
             "size": 2000, "rule": ".pdf", "renamed": True,
             "orig_name": "y.pdf"},
        ],
    })
    meta, ops = pan_organizer._load_plan_file(plan_path)
    check("meta 原样解析", meta.get("src") == "/网盘/a" and meta.get("count") == 2)
    check("ops 数量 2", len(ops) == 2)
    check("op[0] entry.path 正确", ops[0]["entry"].path == "/网盘/a/x.pdf")
    check("op[0] entry.size 还原", ops[0]["entry"].size == 1000)
    check("op[0] action 默认 move", ops[0]["action"] == "move")
    check("op[0] dst 原样", ops[0]["dst"] == "/网盘/归档/pdf/x.pdf")
    check("op[1] renamed 标志还原", ops[1]["renamed"] is True)
    check("op[1] entry.name = 文件名", ops[1]["entry"].name == "y.pdf")

    print("\n== 2) 旧格式兼容：纯 ops 数组 ==")
    legacy_path = write_plan(tmp, "plan-old.json", [
        {"src": "/网盘/a/z.txt", "dst": "/网盘/归档/txt/z.txt",
         "size": 500, "rule": ".txt"},
    ])
    meta2, ops2 = pan_organizer._load_plan_file(legacy_path)
    check("旧格式 meta 为空字典", meta2 == {})
    check("旧格式 ops 数量 1", len(ops2) == 1)
    check("旧格式 entry 重建成功", ops2[0]["entry"].path == "/网盘/a/z.txt")

    print("\n== 3) execute_ops：源不存在(HTTP 404) → 跳过而非失败 ==")
    class MockClient:
        def __init__(self):
            self.dirs = set()
            self.moves = 0
        def mkdirs(self, d):
            self.dirs.add(d)
        def move(self, src, dst, overwrite=False):
            self.moves += 1
            return 404, "Not Found"

    client = MockClient()
    cfg = {"options": {"on_conflict": "rename"}}
    ops = [{
        "rule": ".pdf", "action": "move",
        "entry": pan_organizer.SimpleNamespace(
            path="/网盘/a/x.pdf", name="x.pdf", size=1),
        "dst": "/网盘/归档/pdf/x.pdf",
    }]
    buf = io.StringIO()
    with redirect_stdout(buf):
        moved, failed, skipped = pan_organizer.execute_ops(client, cfg, ops, detail=False)
    check("404 → 计为跳过(0,0,1)", (moved, failed, skipped) == (0, 0, 1),
          f"{moved}/{failed}/{skipped}")
    check("输出提示源已不存在", "源已不存在" in buf.getvalue(), buf.getvalue())

    print("\n== 4) 非 404 失败仍照常计为失败 ==")
    class MockClientFail:
        def mkdirs(self, d):
            pass
        def move(self, src, dst, overwrite=False):
            return 507, "Insufficient Storage"
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        m2, f2, s2 = pan_organizer.execute_ops(MockClientFail(), cfg, ops, detail=False)
    check("507 → 计为失败(0,1,0)", (m2, f2, s2) == (0, 1, 0), f"{m2}/{f2}/{s2}")

    print("\n== 5) 损坏计划文件报错清晰 ==")
    bad = os.path.join(tmp, "plan-bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ not json !!!")
    try:
        pan_organizer._load_plan_file(bad)
        check("坏 JSON 抛出异常", False)
    except (ValueError, json.JSONDecodeError):
        check("坏 JSON 抛出异常", True)

    struct_bad = write_plan(tmp, "plan-no-src.json", {"meta": {}, "ops": [
        {"dst": "/网盘/归档/a/b.txt"}]})
    try:
        pan_organizer._load_plan_file(struct_bad)
        check("缺 src 字段报错", False)
    except ValueError as e:
        check("缺 src 字段报错", "src/dst" in str(e), str(e))

    print(f"\n========== 测试结果：通过 {PASS}，失败 {FAIL} ==========")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
