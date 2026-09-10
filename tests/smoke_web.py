# -*- coding: utf-8 -*-
"""pan-organizer-web 全接口冒烟：真起 mock WebDAV，走完整 query -> plan -> move 链路。
临时数据目录，不碰项目里的 data/。"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 项目根 = tests/ 的上一级（脚本随仓库走，不依赖机器路径）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import web            # noqa: E402
import test_flow      # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  !!  {name}   {extra}")


def wait_done(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = web.TM.get_status()
        if st["status"] != "running":
            return st
        time.sleep(0.3)
    return web.TM.get_status()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pan-organizer_smoke_"))
    # 把 web 的所有持久化路径改到临时目录，绝不碰真实 data/
    web.DATA_DIR = tmp
    web.CONFIG_PATH = tmp / "config.json"
    web.STATE_PATH = tmp / "state.json"
    web.LOGS_DIR = tmp / "logs"
    web.PLANS_DIR = tmp / "plans"
    web.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    web.PLANS_DIR.mkdir(parents=True, exist_ok=True)

    server, tree, port = test_flow.start_mock()
    base_url = f"http://127.0.0.1:{port}/dav"
    web.save_config({
        "alist": {"base_url": base_url, "username": "u", "password": "p",
                  "timeout": 10},
        "options": {"on_conflict": "rename", "exclude_dirs": []},
    })

    c = web.app.test_client()
    web.app.testing = False   # 保留真实错误处理，才能暴露 500

    def jget(url):
        r = c.get(url)
        try:
            return r.status_code, r.get_json()
        except Exception:
            return r.status_code, None

    def jpost(url, body):
        r = c.post(url, data=json.dumps(body),
                   content_type="application/json")
        try:
            return r.status_code, r.get_json()
        except Exception:
            return r.status_code, None

    print("== A. 页面与静态资源 ==")
    r = c.get("/")
    check("GET / 返回 200", r.status_code == 200, r.status_code)
    check("首页含标题 pan-organizer", "pan-organizer" in r.get_data(as_text=True))
    check("app.js 带版本号参数", "app.js?v=" in r.get_data(as_text=True))
    r = c.get("/static/app.js")
    check("GET /static/app.js 200", r.status_code == 200, r.status_code)

    print("\n== B. 只读接口 ==")
    code, j = jget("/api/health")
    check("/api/health ok", code == 200 and j and j.get("ok"), f"{code} {j}")
    check("/api/health 版本与页面一致",
          (j or {}).get("version") == "1.4", str(j))

    r = c.get("/static/vendor/tailwind.js")
    check("内置 Tailwind 可访问（离线可用）",
          r.status_code == 200 and r.data.startswith(b"("), r.status_code)
    check("首页引用本地 Tailwind",
          "vendor/tailwind.js" in c.get("/").get_data(as_text=True))
    check("首页显示版本号",
          "v1.4" in c.get("/").get_data(as_text=True))

    code, j = jget("/api/state")
    check("/api/state 200", code == 200, code)
    check("/api/state 含 4 个键",
          j and set(j.keys()) >= {"config", "state", "status", "last_run"}, str(j)[:200])
    check("/api/state 不回传明文密码",
          not ((j or {}).get("config", {}).get("alist", {}) or {}).get("password"),
          str(j)[:200])

    code, j = jget("/api/config")
    check("/api/config 200", code == 200 and j and "alist" in j, f"{code}")

    code, j = jget("/api/rules")
    rules = (j or {}).get("rules") or []
    ids = [r["id"] for r in rules]
    check("/api/rules 返回规则", code == 200 and len(ids) >= 7, f"{code} {ids}")
    check("/api/rules 无重复 id", len(ids) == len(set(ids)), str(ids))
    check("每条规则有 name/description/default/category",
          all(all(k in r for k in ("id", "name", "description", "default", "category"))
              for r in rules))
    check("planned 只出现在未实现的规则上",
          all(r.get("planned") for r in rules if r.get("planned"))
          and sorted(r["id"] for r in rules if r.get("planned")) == ["cron", "dedupe"],
          str([(r["id"], r.get("planned")) for r in rules]))

    code, j = jget("/api/status")
    check("/api/status 200 & idle", code == 200 and j.get("status") == "idle", f"{code} {j}")

    code, j = jget("/api/logs")
    check("/api/logs 200 & logs 为数组",
          code == 200 and isinstance(j.get("logs"), list), f"{code} {j}")

    code, j = jget("/api/plans")
    check("/api/plans 200 & plans 为数组",
          code == 200 and isinstance(j.get("plans"), list), f"{code} {j}")

    print("\n== C. 连接/目录接口（真连 mock）==")
    code, j = jpost("/api/test", {})
    check("/api/test 连接成功", code == 200 and j.get("ok"), f"{code} {j}")
    check("/api/test 列出挂载点", "/我的网盘" in (j.get("output") or ""), str(j)[:300])

    code, j = jget("/api/mounts")
    check("/api/mounts ok", code == 200 and j.get("ok"), f"{code} {j}")
    check("/api/mounts 含 /我的网盘", "/我的网盘" in (j.get("mounts") or []), str(j)[:200])

    code, j = jget("/api/tree?path=%2F%E6%88%91%E7%9A%84%E7%BD%91%E7%9B%98")
    check("/api/tree 列出子目录",
          code == 200 and j.get("ok") and "下载" in (j.get("subdirs") or []),
          f"{code} {j}")

    code, j = jget("/api/tree?path=%2F%E4%B8%8D%E5%AD%98%E5%9C%A8")
    check("/api/tree 不存在目录 → ok=False（不是 500）",
          code == 200 and j.get("ok") is False, f"{code} {j}")

    print("\n== D. 参数校验（必须 400 且带可读 error）==")
    code, j = jpost("/api/run", {"mode": "不存在的模式"})
    check("未知 mode → 400", code == 400 and (j or {}).get("error"), f"{code} {j}")

    code, j = jpost("/api/run", {"mode": "query"})
    check("query 无 src → 400", code == 400 and (j or {}).get("error"), f"{code} {j}")

    code, j = jpost("/api/run", {"mode": "move"})
    check("move 无 plan → 400", code == 400 and (j or {}).get("error"), f"{code} {j}")

    code, j = jpost("/api/run", {"mode": "move", "plan": "plan-20200101-000000.json"})
    check("move 计划不存在 → 400",
          code == 400 and "不存在" in ((j or {}).get("error") or ""), f"{code} {j}")

    code, j = jpost("/api/run", {"mode": "move", "plan": "../../etc/passwd"})
    check("move 路径穿越 → 400", code == 400, f"{code} {j}")

    code, j = jget("/api/logs/..%2Fconfig.json")
    check("/api/logs 路径穿越被拒（400/404 均可，关键是没读到文件）",
          code in (400, 404), code)
    code, j = jget("/api/plans/..%2Fconfig.json")
    check("/api/plans 路径穿越被拒", code in (400, 404), code)
    code, j = jget("/api/logs/run-不存在.log")
    check("/api/logs 不存在 → 404", code == 404, code)
    code, j = jget("/api/logs/run-20200101-000000.log")
    check("/api/logs 无此文件 → 404", code == 404, code)
    r = c.delete("/api/logs/run-20200101-000000.log")
    check("DELETE 不存在的日志 → 404 JSON",
          r.status_code == 404 and r.is_json, r.status_code)

    code, j = jpost("/api/stop", {})
    check("空任务时 /api/stop 优雅返回", code == 200 and j.get("ok") is False,
          f"{code} {j}")

    print("\n== E. 端到端：query(预览) 生成计划 ==")
    code, j = jpost("/api/run", {
        "mode": "query", "src": "/我的网盘/下载", "dst": "/我的网盘/归档",
        "rules": ["extsort", "skip_incomplete"], "on_conflict": "rename",
        "skip_ext": "", "regex_pattern": "",
    })
    check("query 启动成功", code == 200 and j.get("ok"), f"{code} {j}")
    st = wait_done()
    check("query 正常结束", st["status"] == "done", str(st))

    code, j = jget("/api/plans")
    plans = (j or {}).get("plans") or []
    check("计划已生成", len(plans) == 1, str(plans)[:300])
    plan_name = plans[0]["name"] if plans else ""
    check("计划名规范 plan-*.json",
          bool(plan_name.startswith("plan-") and plan_name.endswith(".json")), plan_name)
    check("计划 meta 含 src/count/dest",
          all(k in (plans[0].get("meta") or {}) for k in ("src", "count", "dest"))
          if plans else False, str(plans[:1])[:300])

    code, j = jget(f"/api/plans/{plan_name}")
    check("计划详情可读且含 ops",
          code == 200 and isinstance(j.get("ops"), list) and len(j["ops"]) > 0,
          f"{code} {str(j)[:200]}")
    check("预览阶段源文件未移动", "/我的网盘/下载/电影A.mp4" in tree.nodes)

    print("\n== F. 端到端：按计划移动 ==")
    code, j = jpost("/api/run", {
        "mode": "move", "plan": plan_name, "on_conflict": "rename",
    })
    check("move 启动成功", code == 200 and j.get("ok"), f"{code} {j}")
    st = wait_done()
    check("move 正常结束", st["status"] == "done", str(st))

    code, j = jget("/api/logs")
    logs = (j or {}).get("logs") or []
    check("产生日志文件", len(logs) >= 1, str(logs)[:200])
    log_name = logs[0]["name"] if logs else ""
    r = c.get(f"/api/logs/{log_name}")
    txt = r.get_data(as_text=True)
    check("/api/logs/<name> 可读", r.status_code == 200 and len(txt) > 0, r.status_code)
    check("日志含计划概要", "计划内移动操作" in txt)
    check("日志含执行进度", "已完成 100%" in txt, txt[-500:])
    check("日志含收尾统计", "完成：成功移动" in txt, txt[-400:])
    # —— 本轮新增的日志增强 ——
    check("日志含任务头（时间戳+版本+命令行）",
          "启动 · " in txt and "pan-organizer/1.4" in txt and "命令行" in txt,
          txt[:600])
    check("日志含撞名策略", "撞名策略" in txt, txt[:600])
    check("日志含失败明细区块或跳过说明",
          "[失败明细]" in txt or "失败明细" in txt or "失败 0" in txt, txt[-900:])
    check("日志含 [执行汇总] 一行",
          "[执行汇总]" in txt, txt[-400:])
    check("汇总含耗时", "耗时" in txt, txt[-300:])

    r = c.get(f"/api/logs/{log_name}?tail=5")
    check("tail 参数生效（≤5 行）",
          r.status_code == 200 and len([x for x in r.get_data(as_text=True).splitlines() if x.strip()]) <= 5,
          r.get_data(as_text=True)[:200])

    code, j = jget("/api/state")
    check("/api/state 记录最近任务",
          (j or {}).get("last_run") and j["last_run"].get("status") == "done",
          str((j or {}).get("last_run"))[:300])
    check("state 里有 summary 统计",
          ((j or {}).get("state") or {}).get("summary", {}).get("success") is not None,
          str((j or {}).get("state"))[:300])

    print("\n== G. 删除接口 ==")
    code, j = jget(f"/api/plans/{plan_name}")
    check("删除前计划可读", code == 200, code)
    r = c.delete(f"/api/plans/{plan_name}")
    check("DELETE 计划 ok", r.status_code == 200 and r.get_json().get("ok"), r.status_code)
    check("删除后计划不存在", not (web.PLANS_DIR / plan_name).exists())

    r = c.delete(f"/api/logs/{log_name}")
    check("DELETE 日志 ok", r.status_code == 200 and r.get_json().get("ok"), r.status_code)
    check("删除后日志不存在", not (web.LOGS_DIR / log_name).exists())

    print("\n== H. SSE 流 ==")
    r = c.get("/api/logs/stream", buffered=False)
    check("SSE 返回事件流 content-type",
          r.status_code == 200 and "text/event-stream" in r.headers.get("Content-Type", ""),
          r.headers.get("Content-Type"))

    server.shutdown()
    print(f"\n========== 冒烟结果：通过 {PASS}，失败 {FAIL} ==========")
    print(f"临时目录：{tmp}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
