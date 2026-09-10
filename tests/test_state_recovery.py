# -*- coding: utf-8 -*-
"""
状态恢复链路单测：
  - 配置 / 任务参数 / 上次结果 三者全在 data/state.json
  - 进程重启后 GET /api/state 一并拿到 config + state + status + last_run
  - last_run 能从日志补全 summary（45% 中断场景）
  - data/config.json 与 data/logs/ 自动迁移
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_SRC = ROOT / "app"  # v1.1 起 web.py / pan_organizer.py / templates/ / static/ 都在 app/

# 临时项目目录：把 web.py 复制过去、改 APP_DIR 指这里
TMP = Path(tempfile.mkdtemp(prefix="pan-organizer_state_"))
APP_TMP = TMP / "app"
APP_TMP.mkdir(parents=True, exist_ok=True)
DATA = APP_TMP / "data"
DATA.mkdir(exist_ok=True)

# 改 web.py：把 APP_DIR 指到临时位置
src = (APP_SRC / "web.py").read_text(encoding="utf-8")
src = src.replace(
    "APP_DIR = Path(__file__).resolve().parent",
    f"APP_DIR = Path({str(APP_TMP)!r})",
    1,
)
(APP_TMP / "web.py").write_text(src, encoding="utf-8")

# web.py 还要 pan_organizer.py、templates/、static/（供 Flask）
shutil.copy(APP_SRC / "pan_organizer.py", APP_TMP / "pan_organizer.py")
shutil.copytree(APP_SRC / "templates", APP_TMP / "templates")
shutil.copytree(APP_SRC / "static", APP_TMP / "static")

sys.path.insert(0, str(APP_TMP))
import web as webmod  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def fresh_data():
    """清空 data/ 下临时文件，保留 CONFIG_PATH / STATE_PATH / LOGS_DIR 路径常量的指向"""
    if webmod.CONFIG_PATH.exists():
        webmod.CONFIG_PATH.unlink()
    if webmod.STATE_PATH.exists():
        webmod.STATE_PATH.unlink()
    if webmod.LOGS_DIR.exists():
        for f in webmod.LOGS_DIR.iterdir():
            if f.is_file():
                f.unlink()


def write_state(snap):
    webmod.save_state(snap)


def get_state(client):
    rv = client.get("/api/state")
    return json.loads(rv.data.decode("utf-8"))


def get_status(client):
    rv = client.get("/api/status")
    return json.loads(rv.data.decode("utf-8"))


def reset_tm():
    """清空 TaskManager 的运行时状态（避免上个测试遗留的 proc 干扰）"""
    with webmod.TM.lock:
        webmod.TM.proc = None
        webmod.TM.log_path = None
        webmod.TM.status = "idle"
        webmod.TM.exit_code = None
        webmod.TM.started_at = None
        webmod.TM.finished_at = None
        webmod.TM.last_size = 0
        webmod.TM.subscribers = []
        webmod.TM.label = ""
        webmod.TM.params = {}


def main():
    client = webmod.app.test_client()

    # ============ 1) 空数据：state.json 不存在 ============
    print("\n== 1) 空状态：state.json 不存在 ==")
    fresh_data()
    reset_tm()
    st = get_state(client)
    check("state 为空 dict", st["state"] == {}, st["state"])
    check("last_run 为 null", st["last_run"] is None, st["last_run"])
    check("status 是 idle", st["status"]["status"] == "idle")
    check("config 是空骨架", "alist" in st["config"], st["config"])

    # ============ 2) 完成状态：state.json 含 summary.done ============
    print("\n== 2) 完成的任务：state.json 含 summary ==")
    fresh_data()
    reset_tm()
    # 模拟 finish 日志行
    log = webmod.LOGS_DIR / "run-finished.log"
    log.write_text(
        "[扫描中] 已处理 5 个目录，发现 100 个文件\n"
        "[扫描完成] 共处理 5 个目录，发现 100 个文件\n"
        "开始执行 ...\n"
        "[进度] 已完成 50% (50/100)，成功 50，失败 0，跳过 0\n"
        "[进度] 已完成 100% (100/100)，成功 98，失败 1，跳过 1\n"
        "完成：成功移动 98 个，失败 1 个，跳过 1 个\n",
        encoding="utf-8",
    )
    write_state({
        "src": "/百度网盘-小号/亚马逊电子书",
        "dst": "/百度网盘-小号/电子书",
        "rules": ["extsort"],
        "on_conflict": "rename",
        "skip_ext": "part,tmp",
        "label": "extsort /百度网盘-小号/亚马逊电子书 → /百度网盘-小号/电子书",
        "started_at": time.time() - 600,
        "finished_at": time.time(),
        "log_file": str(log),
        "status": "done",
        "exit_code": 0,
        "summary": {
            "done": True, "phase": "finished", "pct": 100,
            "success": 98, "failed": 1, "skipped": 1,
            "summary_line": "完成：成功移动 98 个，失败 1 个，跳过 1 个",
        },
    })
    st = get_state(client)
    check("state 有 src", st["state"].get("src") == "/百度网盘-小号/亚马逊电子书")
    check("state 有 dst", st["state"].get("dst") == "/百度网盘-小号/电子书")
    check("state 有 rules", st["state"].get("rules") == ["extsort"])
    check("state 有 skip_ext", st["state"].get("skip_ext") == "part,tmp")
    check("state 有 status=done", st["state"].get("status") == "done")
    check("last_run 来自 snapshot", st["last_run"] is not None)
    if st["last_run"]:
        check("last_run.summary.done", st["last_run"]["summary"].get("done") is True)
        check("last_run.summary.success=98", st["last_run"]["summary"].get("success") == 98)
        check("last_run.summary.failed=1", st["last_run"]["summary"].get("failed") == 1)
        check("last_run.status=done", st["last_run"].get("status") == "done")

    # ============ 3) 中断状态：state 没 summary 但日志有 45% ============
    print("\n== 3) 中断状态：从日志补 summary ==")
    fresh_data()
    reset_tm()
    log = webmod.LOGS_DIR / "run-aborted.log"
    log.write_text(
        "[扫描中] 已处理 5 个目录，发现 100 个文件\n"
        "[扫描完成] 共处理 5 个目录，发现 100 个文件\n"
        "开始执行 ...\n"
        "[进度] 已完成 30% (30/100)\n"
        "[进度] 已完成 45% (45/100)，成功 45，失败 0，跳过 0\n",
        encoding="utf-8",
    )
    write_state({
        "src": "/百度网盘-小号/电影",
        "dst": "/百度网盘-小号/视频",
        "rules": ["extsort"],
        "label": "extsort /百度网盘-小号/电影 → /百度网盘-小号/视频",
        "started_at": time.time() - 120,
        "log_file": str(log),
        "status": "failed",
    })  # 没有 summary 字段 → 触发日志回填
    st = get_state(client)
    check("last_run 已还原", st["last_run"] is not None)
    if st["last_run"]:
        sumr = st["last_run"]["summary"]
        check("从日志补 summary.phase=exec", sumr.get("phase") == "exec",
              sumr)
        check("从日志补 summary.pct=45", sumr.get("pct") == 45, sumr)
        check("last_run.log=run-aborted.log",
              st["last_run"]["log"] == "run-aborted.log")

    # ============ 4) 容器重建：web 进程刚启动，TM 全空 ============
    print("\n== 4) 容器重建场景：TM 全新、state.json 仍在卷上 ==")
    fresh_data()
    reset_tm()
    log = webmod.LOGS_DIR / "run-ColdStart.log"
    log.write_text(
        "[扫描中] 已处理 2 个目录，发现 50 个文件\n"
        "[扫描完成] 共处理 2 个目录，发现 50 个文件\n"
        "开始执行 ...\n"
        "[进度] 已完成 23% (23/100)\n",
        encoding="utf-8",
    )
    write_state({
        "src": "/我的网盘/下载",
        "dst": "/我的网盘/归档",
        "rules": ["extsort"],
        "label": "extsort /我的网盘/下载 → /我的网盘/归档",
        "started_at": time.time() - 3600,
        "log_file": str(log),
        "status": "stopped",
        "summary": {"done": False, "phase": "exec", "pct": 23},
    })
    # 模拟"web 刚启动":进程内存里没东西,/api/status 返 idle + last_run
    st = get_status(client)
    check("/api/status 状态 idle", st["status"] == "idle")
    check("/api/status last_run 还原", st.get("last_run") is not None)
    if st.get("last_run"):
        check("/api/status last_run.pct=23", st["last_run"]["summary"]["pct"] == 23)

    # ============ 5) 自动迁移：旧 config.json + logs/ → data/ ============
    print("\n== 5) 老路径自动迁移到 data/ ==")
    # 把当前的 DATA 清空
    fresh_data()
    # 在原 APP_DIR 旁边（web.APP_DIR 是 APP_TMP）直接放老路径 config.json 与 logs/
    legacy_cfg = webmod.APP_DIR / "config.json"
    legacy_logs = webmod.APP_DIR / "logs"
    legacy_cfg.write_text(json.dumps({
        "alist": {"base_url": "http://old.example/dav",
                  "username": "olduser", "password": "oldpwd",
                  "timeout": 60},
        "options": {"on_conflict": "overwrite"},
        "last_job": {"src": "/legacy", "dst": "/legacy_dst",
                      "rules": ["extsort"], "on_conflict": "overwrite",
                      "skip_ext": "", "started_at": 1000.0},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    legacy_logs.mkdir(exist_ok=True)
    (legacy_logs / "run-legacy.log").write_text(
        "legacy 任务日志内容\n[进度] 已完成 60%\n",
        encoding="utf-8",
    )
    # 触发迁移函数
    webmod._migrate_legacy()
    check("data/config.json 已生成", webmod.CONFIG_PATH.exists())
    check("data/config.json 内容与老的一致",
          '"olduser"' in webmod.CONFIG_PATH.read_text(encoding="utf-8"))
    check("data/logs/run-legacy.log 已迁移",
          (webmod.LOGS_DIR / "run-legacy.log").exists())
    # 清理老路径避免污染下次测试
    legacy_cfg.unlink()
    shutil.rmtree(legacy_logs, ignore_errors=True)

    # ============ 6) 密码遮罩：password 仍存在时接口不返回 ============
    print("\n== 6) /api/state 的密码遮罩 ==")
    fresh_data()
    reset_tm()
    webmod.CONFIG_PATH.write_text(json.dumps({
        "alist": {"base_url": "http://x/dav", "username": "u",
                  "password": "realpwd", "timeout": 30},
        "options": {"on_conflict": "rename"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    st = get_state(client)
    cfg = st["config"]["alist"]
    check("password 字段空", cfg.get("password") == "", cfg)
    check("password_set=true", cfg.get("password_set") is True)
    check("其他字段保留", cfg.get("username") == "u" and cfg.get("base_url"))

    # ============ 7) state.json 原子写：临时文件 rename ============
    print("\n== 7) state.json 原子写 ==")
    fresh_data()
    reset_tm()
    webmod.save_state({"src": "/a", "dst": "/b", "status": "running"})
    tmp_f = webmod.STATE_PATH.with_suffix(".json.tmp")
    check("STATE_PATH 已落盘", webmod.STATE_PATH.exists())
    check("临时文件被替换", not tmp_f.exists())
    content = webmod.STATE_PATH.read_text(encoding="utf-8")
    check("内容含 src", '"src"' in content and '"/a"' in content)

    # 收尾
    fresh_data()
    print(f"\n========== 测试结果：通过 {PASS}，失败 {FAIL} ==========")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
