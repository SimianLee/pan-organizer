#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pan-organizer Web 后端
=================
把命令行版的 pan_organizer.py 包装成 Web 应用：
  - 浏览器配置 alist 连接（地址/账号/密码），保存到 config.json
  - 树形选择源/目标目录
  - 勾选规则触发整理（按后缀、按大类别、按日期、按大小等）
  - 实时日志（SSE 流）
  - 开始/停止按钮
  - 历史日志查看

启动：
  python web.py --port 6060
Docker：
  docker run -p 6060:6060 pan-organizer-web
"""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, Response, jsonify, render_template, request,
    send_from_directory, stream_with_context,
)

# ---------------------------------------------------------------------------
# 路径与配置：所有持久化数据集中到 DATA_DIR，便于容器重建后完整恢复
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent

# 新的统一数据目录（一个卷 = 配置 + 状态 + 日志）
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"     # alist 连接 + 默认 options
STATE_PATH = DATA_DIR / "state.json"       # 最近任务的完整快照
LOGS_DIR = DATA_DIR / "logs"               # run-*.log
PLANS_DIR = DATA_DIR / "plans"             # plan-*.json（查询导出的整理计划，供"按计划移动"）

# 计划文件名的唯一合法形态（生成与校验共用同一个正则，避免两处写法漂移）
_PLAN_NAME_RE = re.compile(r"^plan-\d{8}-\d{6}\.json$")
# 实际监听端口：main() 里按 --port 覆盖，页脚据此显示（默认 6060）
RUN_PORT = 6060

# 老路径（兼容旧版部署）：
#  - 项目根下的 config.json / logs/    （v1.0 在容器外 / NAS 项目根的旧文件）
#  - 项目根下的 legacy/config.json / legacy/logs/   （docker-compose 通过 :ro 卷挂来）
LEGACY_PATHS = [
    (APP_DIR / "config.json",       APP_DIR / "logs"),
    (APP_DIR / "legacy" / "config.json", APP_DIR / "legacy" / "logs"),
]

# 创建新目录
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
PLANS_DIR.mkdir(exist_ok=True)

# 兼容迁移：旧 config.json / logs 一次性搬过来，避免数据丢失
def _migrate_legacy():
    for old_cfg, old_logs in LEGACY_PATHS:
        try:
            if old_cfg.exists() and old_cfg.is_file() and not CONFIG_PATH.exists():
                CONFIG_PATH.write_bytes(old_cfg.read_bytes())
        except OSError:
            pass
        try:
            if old_logs.exists() and old_logs.is_dir():
                # 仅迁移新目录里没有的日志，避免覆盖新数据
                for f in old_logs.iterdir():
                    if f.is_file() and not (LOGS_DIR / f.name).exists():
                        shutil.copy2(f, LOGS_DIR / f.name)
        except OSError:
            pass

_migrate_legacy()

PAN_ORGANIZER = APP_DIR / "pan_organizer.py"

# 复用 pan-organizer 的 WebDAV 客户端，避免重复实现；版本号也从它取，保证全项目唯一来源
sys.path.insert(0, str(APP_DIR))
from pan_organizer import (  # noqa: E402
    APP_VERSION, WebDAVClient, WebDAVError, norm_path,
    BOOK_LABELS, booksort_match_text,
)
try:
    import book_online                     # 图书联网二次分类（v1.6）
except ImportError:                        # 极少数"只拷了部分文件"的部署：
    book_online = None                     # 联网功能降级，其余功能照常

PYTHON_BIN = sys.executable


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "alist": {"base_url": "http://192.168.1.100:5244/dav",
                  "username": "", "password": "", "timeout": 30},
        "options": {"on_conflict": "rename", "exclude_dirs": []},
    }


def save_config(cfg):
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def mask_secrets(cfg):
    """
    回给前端前把敏感字段遮罩：alist 密码、联网补全的 LLM api_key。
    只回 "已设置" 标记 + 空值，前端不动该字段即保持原值不变。
    """
    view = json.loads(json.dumps(cfg or {}))
    if (view.get("alist") or {}).get("password"):
        view["alist"]["password_set"] = True
        view["alist"]["password"] = ""
    llm = ((view.get("online") or {}).get("llm") or {})
    if llm.get("api_key"):
        llm["api_key_set"] = True
        llm["api_key"] = ""
    return view


def load_state():
    """读取最近一次任务的完整快照（页面重开/容器重建后据此还原）"""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st):
    """把最近任务的快照写到 STATE_PATH。atomic 模式：先写临时文件再 rename，
    防止写到一半被 kill 弄出半截 JSON（页面读到烂文件就 show 异常结束）。"""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        # 落盘失败也不能让任务起不来——降级为直接写
        try:
            STATE_PATH.write_text(
                json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            print(f"[WARN] state.json 写入失败: {e}", flush=True)


# ---------------------------------------------------------------------------
# 任务管理器：单例，全局唯一运行中的任务
# ---------------------------------------------------------------------------
class TaskManager:
    """管理一个后台 pan-organizer 进程：启动、停止、SSE 广播、日志文件"""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None          # subprocess.Popen
        self.log_path = None      # 日志文件路径
        self.status = "idle"      # idle / running / done / failed / stopped
        self.exit_code = None
        self.started_at = None
        self.finished_at = None
        self.last_size = 0        # 已读取日志字节
        self.subscribers = []     # list[queue.Queue]
        self.label = ""           # 任务标签（描述）
        self.params = {}          # 本次任务的输入参数（src/dst/rules/skip_ext...）

    # -- 生命周期 -----------------------------------------------------------
    def start(self, cmd, label="", params=None, log_tag=None):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "已有任务在运行，请先停止"
            # log_tag 允许调用方把日志与配套文件（如 plan-*.json）对齐到同一时间戳
            ts = log_tag or datetime.now().strftime("%Y%m%d-%H%M%S")
            self.log_path = LOGS_DIR / f"run-{ts}.log"
            self.label = label
            self.params = dict(params or {})
            # 任务开始就把完整快照落到 state.json（容器/进程重建后页面据此恢复）
            snapshot = {
                **self.params,
                "label": label,
                "started_at": time.time(),
                "log_file": str(self.log_path),
                "status": "running",
            }
            save_state(snapshot)
            # 子进程 stdout 是文件描述符直写，编码由子进程自己决定：
            # Windows 下 Python 默认按系统区域编码（cp936）写中文日志，
            # 而本服务按 UTF-8 读 → 整段乱码。强制子进程用 UTF-8 输出。
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            log_f = open(self.log_path, "w", encoding="utf-8", buffering=1)
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(APP_DIR),
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    env=env,
                )
            except Exception as e:
                log_f.close()
                self.proc = None
                # 同时打到容器 stdout（docker logs 能直接看到），便于服务端起不来时定位
                print(f"[web] 启动任务失败: {e}", flush=True)
                return False, f"启动失败: {e}"
            self.status = "running"
            self.exit_code = None
            self.started_at = time.time()
            self.finished_at = None
            self.last_size = 0
            # 容器日志里留一条"谁在什么时候用什么命令跑的"，与 run-*.log 互相印证
            print(f"[web] 任务启动 pid={self.proc.pid} 日志={self.log_path.name} "
                  f"标签={self.label or '-'}", flush=True)
            print(f"[web] 命令行: {' '.join(cmd)}", flush=True)
            # 守护线程：监控日志文件变化 + 进程退出
            t = threading.Thread(target=self._watcher, args=(log_f,),
                                 daemon=True)
            t.start()
            return True, f"已启动: {self.label or 'pan-organizer'}\n日志: {self.log_path}"

    def stop(self):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                return False, "没有运行中的任务"
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.status = "stopped"
            self.finished_at = time.time()
        print(f"[web] 任务已停止（用户手动）日志={self.log_path.name if self.log_path else '-'}",
              flush=True)
        # 用户手动停：也把快照写一下（避免下次只看 done=0 一头雾水）
        try:
            summary = {}
            if self.log_path and self.log_path.exists():
                summary = parse_run_log(self.log_path)
            snapshot = dict(self.params or {})
            snapshot.update({
                "label": self.label,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "log_file": str(self.log_path) if self.log_path else None,
                "status": "stopped",
                "exit_code": None,
                "summary": summary,
            })
            save_state(snapshot)
        except Exception:
            pass
        return True, "已停止"

    # -- 日志监控线程 -------------------------------------------------------
    def _watcher(self, log_f):
        """每秒检查日志文件大小变化，把新增内容广播给订阅者；进程退出后清理"""
        proc = self.proc
        path = self.log_path
        while True:
            try:
                if path.exists():
                    cur_size = path.stat().st_size
                    if cur_size > self.last_size:
                        # 用二进制读：文本模式的 seek 是按字符而非字节，
                        # 日志含中文时偏移会错位。last_size 始终是字节数。
                        with open(path, "rb") as f:
                            f.seek(self.last_size)
                            raw = f.read()
                        self.last_size = cur_size
                        chunk = decode_log(raw) if raw else ""
                        if chunk:
                            self._broadcast(chunk)
            except Exception:
                pass
            if proc.poll() is not None:
                # 进程退出，再读一次尾部
                try:
                    with open(path, "rb") as f:
                        f.seek(self.last_size)
                        raw = f.read()
                    self.last_size += len(raw)
                    tail = decode_log(raw) if raw else ""
                    if tail:
                        self._broadcast(tail)
                except Exception:
                    pass
                with self.lock:
                    self.exit_code = proc.returncode
                    if self.status == "running":
                        self.status = "done" if proc.returncode == 0 else "failed"
                    self.finished_at = time.time()
                self._broadcast(
                    f"\n[任务结束] exit={proc.returncode} "
                    f"({self.status}) {datetime.now().strftime('%H:%M:%S')}\n"
                )
                print(f"[web] 任务结束 exit={proc.returncode} 状态={self.status} "
                      f"日志={path.name}", flush=True)
                # 进程退出 → 把这次任务的最终结果写回 state.json，
                # 容器/进程重建后，页面打开就凭这份快照完整恢复
                try:
                    summary = parse_run_log(path) if path.exists() else {}
                    snapshot = dict(self.params or {})
                    snapshot.update({
                        "label": self.label,
                        "started_at": self.started_at,
                        "finished_at": self.finished_at,
                        "log_file": str(path),
                        "status": self.status,   # done / failed / stopped
                        "exit_code": proc.returncode,
                        "summary": summary,
                    })
                    save_state(snapshot)
                except Exception:
                    pass
                log_f.close()
                break
            time.sleep(0.5)

    def _broadcast(self, msg):
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.subscribers.append(q)
        # 给新订阅者一份当前日志尾部作为起点
        if self.log_path and self.log_path.exists():
            try:
                txt = read_log(self.log_path)
                # 仅给前 200 行，避免首次连接就推巨大 backlog
                lines = txt.splitlines()
                tail = "\n".join(lines[-200:])
                if tail:
                    q.put_nowait(tail + "\n")
            except Exception:
                pass
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def get_status(self):
        with self.lock:
            return {
                "status": self.status,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "log_file": str(self.log_path) if self.log_path else None,
                "label": self.label,
            }


TM = TaskManager()


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
app = Flask(__name__,
            template_folder=str(APP_DIR / "templates"),
            static_folder=str(APP_DIR / "static"))


# ---- 页面 ----
@app.route("/")
def index():
    # 用 app.js 的 mtime 作版本号：文件一更新，浏览器自动拉新，杜绝缓存旧 JS
    try:
        js_ver = int((APP_DIR / "static" / "app.js").stat().st_mtime)
    except OSError:
        js_ver = 1
    return render_template("index.html", js_ver=js_ver, app_ver=APP_VERSION,
                           app_port=RUN_PORT)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "pan-organizer-web", "version": APP_VERSION})


# ---- 配置 ----
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        # 不要把真实密码/API key 回给前端，只回显"已设置"标记
        return jsonify(mask_secrets(load_config()))

    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    if "alist" in data:
        al = cfg.setdefault("alist", {})
        for k, v in data["alist"].items():
            # 保留原密码的逻辑：前端密码字段为空时不覆盖
            if k == "password" and not v:
                continue
            al[k] = v
    if "options" in data:
        cfg.setdefault("options", {}).update(data["options"])
    if "online" in data:
        # 联网补全（bookonline）配置：provider/超时/并发/上限 + llm 子对象
        on = cfg.setdefault("online", {})
        for k, v in (data["online"] or {}).items():
            if k == "llm" and isinstance(v, dict):
                llm = on.setdefault("llm", {})
                for kk, vv in v.items():
                    # api_key 留空表示"不修改"（前端回显的是空值，不能拿它覆盖真 key）
                    if kk == "api_key" and not vv:
                        continue
                    llm[kk] = vv
            elif v not in (None, ""):
                on[k] = v
    save_config(cfg)
    return jsonify({"ok": True})


# ---- 统一快照接口：一次拿全配置 + 最近任务状态 ----
@app.route("/api/state")
def api_state():
    """页面打开/刷新/F5 时第一件事就调这个：
       - config  : alist 连接 + 默认 options（密码字段已遮罩）
       - state   : 最近一次任务的完整快照（data/state.json）
                   容器/进程刚启动时，按这份快照回填 src/dst/rules/skip_ext/on_conflict
       - status  : 当前 web 进程内存里的实时任务状态
       - last_run: 从 state + 最新日志还原的最近一次恢复信息（供 UI 进度条展示）
    """
    # 配置（密码 / API key 遮罩）
    cfg = mask_secrets(load_config())

    state = load_state()
    st = TM.get_status()
    # 只要当前没有"正在跑"的任务，就返回最近一次任务的快照（last_run）：
    #   idle                 → 容器重建 / 进程重启后，页面一打开就能还原上次跑到哪
    #   done/failed/stopped  → 刚跑完就 F5 刷新时，日志区仍能重新载入内容
    #                          （旧实现只在 idle 时返回，导致"状态=完成但日志是空的"）
    # running 时不返回，避免与实时 SSE 日志 / 进度条互相覆盖。
    last_run = build_last_run() if st["status"] != "running" else None
    return jsonify({
        "config": cfg,
        "state": state,        # 含 src/dst/rules/skip_ext/on_conflict/started_at/finished_at/...
        "status": st,
        "last_run": last_run,
    })


@app.route("/api/test", methods=["POST"])
def api_test():
    """测试连接 + 列出挂载点"""
    try:
        cfg = load_config()
        al = cfg.get("alist", {})
        client = WebDAVClient(
            al.get("base_url", ""),
            al.get("username", ""),
            al.get("password", ""),
            timeout=al.get("timeout", 30),
        )
        # 尝试列出根目录（depth=0 探测根是否存在；depth=1 拿挂载点）
        if not client.exists("/"):
            return jsonify({"ok": False, "output": "根目录不可访问"})
        entries = client.list_dir("/")
        mounts = [e.name for e in entries if e.is_dir]
        output_lines = [f"✓ 连接成功", f"挂载点 ({len(mounts)} 个):"]
        for m in mounts:
            output_lines.append(f"  /{m}")
        return jsonify({"ok": True, "output": "\n".join(output_lines),
                        "mounts": [f"/{m}" for m in mounts]})
    except WebDAVError as e:
        return jsonify({"ok": False, "output": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "output": f"未知错误: {e}"}), 500


@app.route("/api/mounts")
def api_mounts():
    """列挂载点"""
    try:
        cfg = load_config()
        al = cfg.get("alist", {})
        client = WebDAVClient(
            al.get("base_url", ""),
            al.get("username", ""),
            al.get("password", ""),
            timeout=al.get("timeout", 30),
        )
        if not client.exists("/"):
            return jsonify({"mounts": [], "ok": False,
                            "error": "根目录不可访问"})
        entries = client.list_dir("/")
        mounts = sorted([f"/{e.name}" for e in entries if e.is_dir])
        return jsonify({"mounts": mounts, "ok": True})
    except WebDAVError as e:
        return jsonify({"mounts": [], "ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"mounts": [], "ok": False, "error": str(e)}), 500


@app.route("/api/tree")
def api_tree():
    """懒加载：列某目录下第一层（子目录）"""
    path = request.args.get("path", "/")
    try:
        cfg = load_config()
        al = cfg.get("alist", {})
        client = WebDAVClient(
            al.get("base_url", ""),
            al.get("username", ""),
            al.get("password", ""),
            timeout=al.get("timeout", 30),
        )
        path = norm_path(path)
        if not client.exists(path):
            return jsonify({"path": path, "subdirs": [], "ok": False,
                            "error": "目录不存在"})
        entries = client.list_dir(path)
        subdirs = sorted([e.name for e in entries if e.is_dir])
        files_n = sum(1 for e in entries if not e.is_dir)
        return jsonify({
            "path": path,
            "subdirs": subdirs,
            "files_count": files_n,
            "ok": True,
        })
    except WebDAVError as e:
        return jsonify({"path": path, "subdirs": [], "ok": False,
                        "error": str(e)})
    except Exception as e:
        return jsonify({"path": path, "subdirs": [], "ok": False,
                        "error": str(e)}), 500


# ---- 任务控制 ----
@app.route("/api/run", methods=["POST"])
def api_run():
    """启动一次整理任务。

    mode（body 字段，默认 query_move，兼容老前端不传 mode）：
      query      ：只查询不移动 —— extsort dry-run 预览，并把计划导出到
                   data/plans/plan-<ts>.json（供后续"按计划移动"复用，不再重复扫描）
      query_move ：查询 + 直接移动（= 原"开始整理"），同样导出计划
      move       ：按已有计划执行 —— body.plan 指定 plan-*.json，跳过扫描，
                   直接用上次查询的计划结果移动文件
    """
    data = request.get_json(force=True, silent=True) or {}
    src = (data.get("src") or "").strip()
    dst = (data.get("dst") or "").strip()
    mode = data.get("mode") or "query_move"
    rules = data.get("rules") or ["extsort"]   # 勾选启用的规则 id（可组合嵌套目录）
    on_conflict = data.get("on_conflict") or "rename"
    skip_ext = data.get("skip_ext") or ""
    regex_pattern = (data.get("regex_pattern") or "").strip()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 把本次运行参数持久化到 config.options（撞名策略 / 正则筛选）
    cfg = load_config()
    cfg.setdefault("options", {})["on_conflict"] = on_conflict
    cfg.setdefault("options", {})["regex_pattern"] = regex_pattern

    if mode == "move":
        # ---- 模式 3：按计划移动（不再扫描） ----
        plan_name = (data.get("plan") or "").strip()
        if not _PLAN_NAME_RE.match(plan_name):
            return jsonify({"ok": False,
                            "error": "请选择要执行的计划（先跑一次「查询」生成）"}), 400
        plan_path = PLANS_DIR / plan_name
        if not plan_path.exists():
            return jsonify({"ok": False,
                            "error": f"计划文件不存在：{plan_name}（可能已被删除）"}), 400
        # 读计划 meta 还原 src/dst，仅用于界面展示；执行只按计划文件内容
        try:
            meta = (json.loads(plan_path.read_text(encoding="utf-8"))
                    or {}).get("meta") or {}
        except Exception:
            meta = {}
        if meta.get("src"):
            src = meta["src"]
        if meta.get("dest"):
            dst = meta["dest"]
        cmd = [PYTHON_BIN, str(PAN_ORGANIZER), "extsort",
               "--config", str(CONFIG_PATH),
               "--from-plan", str(plan_path),
               "--apply"]
        label = f"按计划移动 {plan_name}"
        params = {"mode": mode, "plan": plan_name, "src": src, "dst": dst,
                  "rules": [], "on_conflict": on_conflict,
                  "skip_ext": meta.get("skip_ext") or ""}
    else:
        # ---- 模式 1/2：查询（可选直接移动） ----
        if mode not in ("query", "query_move"):
            return jsonify({"ok": False, "error": f"未知模式：{mode}"}), 400
        if not src:
            return jsonify({"ok": False, "error": "源目录不能为空"}), 400
        plan_name = f"plan-{ts}.json"
        plan_path = PLANS_DIR / plan_name
        # 显式传 --config 绝对路径：v1.1 重构后配置文件落到 data/ 子目录，
        # 不传会按 pan-organizer 默认的 ./config.json 找，容器内 cwd=/app 会报 FileNotFoundError
        cmd = [PYTHON_BIN, str(PAN_ORGANIZER), "extsort",
               "--config", str(CONFIG_PATH),
               "--path", src,
               "--depth", "-1",
               "--plan", str(plan_path),
               "--rules", ",".join(rules)]
        if dst and dst != src:
            cmd += ["--dest", dst]
        if skip_ext:
            cmd += ["--skip-ext", skip_ext]
        if regex_pattern:
            cmd += ["--regex-pattern", regex_pattern]
        if mode == "query_move":
            cmd += ["--apply"]
        label = ("查询并移动" if mode == "query_move" else "查询(预览)") + f" {src}"
        if dst and dst != src:
            label += f" → {dst}"
        params = {"mode": mode, "plan": plan_name, "src": src, "dst": dst,
                  "rules": rules, "on_conflict": on_conflict,
                  "skip_ext": skip_ext, "regex_pattern": regex_pattern}

    # 兼容旧逻辑：last_job 字段继续保留在 config.json，老前端也不会被打破
    cfg["last_job"] = {
        "src": src,
        "dst": dst,
        "rules": params["rules"],
        "on_conflict": on_conflict,
        "skip_ext": params["skip_ext"],
        "started_at": time.time(),
    }
    save_config(cfg)

    ok, msg = TM.start(cmd, label=label, params=params, log_tag=ts)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = TM.stop()
    return jsonify({"ok": ok, "msg": msg})


# ---- 日志 → 任务状态还原（进程重启后内存状态丢失的唯一真相来源） ----
def decode_log(raw):
    """把日志字节解码成文本：优先 UTF-8，明显不是则回退 GBK。

    背景：容器（Linux）里子进程默认 UTF-8 输出；但 Windows 上直接跑
    web.py 时，老版本子进程按系统区域编码（cp936）写日志。两种编码都要
    能正确读出，否则页面日志区整段中文变乱码（显示为 U+FFFD）。
    尾部截断可能切在多字节字符中间，用 errors="replace" 容错；只有当
    替换字符占比异常（>0.5%）才判定为 GBK 文件，避免误伤 UTF-8 日志。
    """
    if raw.startswith(b"\xef\xbb\xbf"):          # 带 BOM 的 UTF-8
        return raw.decode("utf-8-sig", errors="replace")
    txt = raw.decode("utf-8", errors="replace")
    bad = txt.count("\ufffd")
    if bad and bad * 200 > len(txt):
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            pass
    return txt


def read_log(path):
    """整读日志文件为文本（兼容 UTF-8 / GBK）。读不到返回空串。"""
    try:
        return decode_log(Path(path).read_bytes())
    except OSError:
        return ""


def _read_tail(path, max_bytes=512 * 1024):
    """读文件尾部。大日志（几十 MB）避免整读撑爆内存，只取末尾 max_bytes。"""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        if size <= max_bytes:
            raw = Path(path).read_bytes()
        else:
            with open(path, "rb") as f:
                f.seek(size - max_bytes)
                raw = f.read()
    except OSError:
        return ""
    return decode_log(raw).lstrip("\n")


def parse_run_log(path):
    """从日志内容还原一次任务跑到了什么状态。

    返回 dict：
      done        是否完整跑完（日志有收尾行）
      phase       unknown / finished / exec / scan_done / scan
      pct         执行阶段最后百分比（None=不在执行期）
      success/failed/skipped   收尾行统计（仅 done 时有）
      scanned_dirs/scanned_files  扫描相关（仅扫描期有）
      summary_line  命中的关键原始行（供前端展示）
    """
    res = {"done": False, "phase": "unknown", "pct": None,
           "success": None, "failed": None, "skipped": None,
           "scanned_dirs": None, "scanned_files": None,
           "summary_line": None}
    txt = _read_tail(path)
    # 从尾向头找"最近一条关键行"：收尾 > 执行进度 > 扫描完成 > 扫描中
    for ln in reversed(txt.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        # 1) 完整跑完的收尾行
        m = re.match(r"完成[:：]\s*成功移动 (\d+) 个，失败 (\d+) 个，跳过 (\d+) 个", ln)
        if m:
            res.update(done=True, phase="finished", pct=100,
                       success=int(m.group(1)), failed=int(m.group(2)),
                       skipped=int(m.group(3)), summary_line=ln)
            break
        # 2) 执行中最后一条精确进度
        m = re.search(r"\[进度\] 已完成 (\d+)%", ln)
        if m and res["phase"] == "unknown":
            res.update(phase="exec", pct=int(m.group(1)), summary_line=ln)
            break
        # 3) 扫描刚完成（还没开始/刚开始执行就被打断）
        m = re.search(r"\[扫描完成\] 共处理 (\d+) 个目录，发现 (\d+) 个文件", ln)
        if m and res["phase"] == "unknown":
            res.update(phase="scan_done",
                       scanned_dirs=int(m.group(1)),
                       scanned_files=int(m.group(2)),
                       summary_line=ln)
            break
        # 4) 扫描中途被打断
        m = re.search(r"\[扫描中\] 已处理 (\d+) 个目录，发现 (\d+) 个文件", ln)
        if m and res["phase"] == "unknown":
            res.update(phase="scan",
                       scanned_dirs=int(m.group(1)),
                       scanned_files=int(m.group(2)),
                       summary_line=ln)
            break
    return res


_last_run_cache = {"sig": None, "t": 0.0, "data": None}


def build_last_run():
    """内存空闲（web 进程刚重启）时，从 data/state.json + logs 目录最近一次
    run-*.log 还原"上次任务运行状态"，供前端重开页面时恢复界面。
    优先读 state.json（含任务参数与最终结果），日志二次补全 summary。"""
    state = load_state()
    if not state:
        return None
    log_file = state.get("log_file") or ""
    # 用 Path.name 取文件名（兼容 Windows 盘符与 \ 分隔符）
    log_name = Path(log_file).name if log_file else None
    summary = state.get("summary") or {}
    if not summary and log_name and (LOGS_DIR / log_name).exists():
        try:
            summary = parse_run_log(LOGS_DIR / log_name)
        except Exception:
            summary = {}
    try:
        mtime = STATE_PATH.stat().st_mtime
    except OSError:
        mtime = None
    sig = (log_name, mtime, state.get("started_at"), state.get("finished_at"))
    now = time.time()
    if _last_run_cache["sig"] == sig and now - _last_run_cache["t"] < 5:
        return _last_run_cache["data"]
    data = {
        "log": log_name,
        "mtime": mtime,
        "label": state.get("label"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "status": state.get("status"),
        "summary": summary,
    }
    _last_run_cache.update(sig=sig, t=now, data=data)
    return data


@app.route("/api/status")
def api_status():
    st = TM.get_status()
    # 进程重启后内存全清 → idle；但日志还在挂载卷里，附加最近任务供前端还原
    if st["status"] == "idle":
        st["last_run"] = build_last_run()
    return jsonify(st)


# ---- 日志 ----
@app.route("/api/logs/stream")
def api_logs_stream():
    """SSE 实时日志流"""
    q = TM.subscribe()

    def gen():
        try:
            yield "data: [已连接实时日志流]\n\n"
            idle_ticks = 0
            while True:
                try:
                    msg = q.get(timeout=1)
                    yield "data: " + msg.replace("\n", "\ndata: ") + "\n\n"
                    idle_ticks = 0
                except queue.Empty:
                    idle_ticks += 1
                    yield ": keepalive\n\n"
                    # 任务结束后再空 2 次就退出（让客户端能拿到结束标记）
                    if TM.status in ("done", "failed", "stopped") \
                            and idle_ticks > 2:
                        yield "data: [SSE 流结束]\n\n"
                        break
        finally:
            TM.unsubscribe(q)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # 关 nginx 缓冲
        "Connection": "keep-alive",
    }
    # 显式声明 charset=utf-8：日志含中文，避免中间代理按其它编码猜
    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream; charset=utf-8",
                    headers=headers)


@app.route("/api/logs")
def api_logs_list():
    """历史日志列表"""
    files = sorted(LOGS_DIR.glob("run-*.log"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return jsonify({
        "logs": [
            {"name": f.name,
             "mtime": f.stat().st_mtime,
             "size": f.stat().st_size}
            for f in files[:50]
        ]
    })


@app.route("/api/logs/<name>")
def api_logs_read(name):
    """读取某条历史日志（限 run-*.log，防路径穿越）。
    ?tail=N 时只返回最后 N 行（用于页面恢复，避免大日志整读拖垮浏览器）。"""
    if ".." in name or not name.startswith("run-") or not name.endswith(".log"):
        return jsonify({"error": "invalid name"}), 400
    p = LOGS_DIR / name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        n = int(request.args.get("tail", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        if n > 0:
            # 尾部最多取 2MB（足够覆盖数百行），再截最后 n 行
            lines = _read_tail(p, 2 * 1024 * 1024).splitlines()
            txt = "\n".join(lines[-n:])
        else:
            txt = read_log(p)
    except OSError as e:
        return jsonify({"error": f"读取日志失败: {e}"}), 500
    return Response(txt, mimetype="text/plain; charset=utf-8")


@app.route("/api/logs/<name>", methods=["DELETE"])
def api_logs_delete(name):
    """删除一条历史日志"""
    if ".." in name or not name.startswith("run-") or not name.endswith(".log"):
        return jsonify({"error": "invalid name"}), 400
    p = LOGS_DIR / name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        p.unlink()
    except OSError as e:
        return jsonify({"error": f"删除失败: {e}"}), 500
    return jsonify({"ok": True})


# ---- 整理计划（查询导出的 plan-*.json，供"按计划移动"复用）----
@app.route("/api/plans")
def api_plans():
    """历史计划列表（按生成时间倒序）。每次"查询/查询并移动"都会导出一份。"""
    files = sorted(PLANS_DIR.glob("plan-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    plans = []
    for f in files[:50]:
        item = {"name": f.name, "mtime": f.stat().st_mtime,
                "size": f.stat().st_size, "meta": {}}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                item["meta"] = data.get("meta") or {}
        except Exception:
            pass  # 解析失败的计划仍列出来（只展示文件名），不挡列表
        plans.append(item)
    return jsonify({"plans": plans})


@app.route("/api/plans/<name>")
def api_plan_read(name):
    """读取一条计划全文（前端"查看"用）"""
    if not _PLAN_NAME_RE.match(name) or ".." in name:
        return jsonify({"error": "invalid name"}), 400
    p = PLANS_DIR / name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        return jsonify({"error": f"计划文件解析失败: {e}"}), 500


@app.route("/api/plans/<name>", methods=["DELETE"])
def api_plan_delete(name):
    """删除一条历史计划"""
    if not _PLAN_NAME_RE.match(name) or ".." in name:
        return jsonify({"error": "invalid name"}), 400
    p = PLANS_DIR / name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"ok": True})


# ---- 预定义规则（前端展示用） ----
@app.route("/api/rules")
def api_rules():
    """预定义规则清单（前端勾选用）"""
    return jsonify({
        "rules": [
            {
                "id": "extsort",
                "name": "按后缀归档",
                "description": "把每个文件按 .后缀 分到对应文件夹（pdf/ → pdf 文件夹）",
                "default": True,
                "category": "core",
            },
            {
                "id": "category",
                "name": "按扩展名大类别归档",
                "description": "图片/视频/音频/文档/压缩包/代码 六大类（同类多后缀合到一目录）",
                "default": False,
                "category": "core",
            },
            {
                "id": "booksort",
                "name": "图书归类（按书名识别类型）",
                "description": "图书/漫画文件按书名关键词归入 漫画/教材教辅/计算机IT/医学养生/…/其它图书 目录；"
                              "非图书文件原地不动",
                "default": False,
                "category": "core",
            },
            {
                "id": "bookonline",
                "name": "图书联网补全（需与「图书归类」同用）",
                "description": "本地关键词判不出类型的书（如《活着》）联网查类型："
                               "默认用当当图书分类（免费、无需 key），也可配 LLM 接口（更准）。"
                               "只查「其它图书」那部分，结果本地缓存，查不到就保持原样",
                "default": False,
                "category": "core",
            },
            {
                "id": "by_date",
                "name": "按修改日期归档",
                "description": "按文件修改时间建 YYYY-MM 月份目录",
                "default": False,
                "category": "core",
            },
            {
                "id": "by_size",
                "name": "按文件大小归档",
                "description": "<10MB / 10-100MB / 100MB-1GB / >1GB 四档",
                "default": False,
                "category": "core",
            },
            {
                "id": "skip_incomplete",
                "name": "跳过未完成文件",
                "description": ".part .crdownload .!qb 等下载中文件",
                "default": True,
                "category": "safety",
            },
            {
                "id": "regex_match",
                "name": "正则匹配（文件名包含关键字）",
                "description": "用正则筛出要整理的文件（如只整理带 ISBN 的）",
                "default": False,
                "category": "advanced",
            },
            {
                "id": "dedupe",
                "name": "重复文件检测（按 hash）",
                "description": "扫到 MD5/SHA1 相同的文件，列清单供手动处理（规划中）",
                "default": False,
                "category": "advanced",
                "planned": "规划中",
            },
            {
                "id": "cleanup_empty",
                "name": "清理空目录",
                "description": "整理完后，删掉源目录下的空文件夹",
                "default": False,
                "category": "advanced",
            },
            {
                "id": "cron",
                "name": "定时任务（cron 表达式）",
                "description": "按时间自动触发整理（如每天凌晨 3 点，规划中）",
                "default": False,
                "category": "advanced",
                "planned": "规划中",
            },
        ]
    })


# ---- 图书联网补全（bookonline 规则，v1.6）----
@app.route("/api/online/defaults")
def api_online_defaults():
    """联网补全的默认配置 + 可用类型清单（前端表单预填用，不落盘）"""
    if book_online is None:
        return jsonify({"ok": False, "error": "联网补全模块（book_online.py）未安装",
                        "defaults": {}, "providers": [], "labels": BOOK_LABELS})
    return jsonify({"defaults": book_online.DEFAULT_CONFIG,
                    "providers": [
                        {"id": "auto", "name": "自动（先 LLM，再当当）"},
                        {"id": "dangdang", "name": "当当图书分类（免费，无需 key）"},
                        {"id": "llm", "name": "LLM 接口（需自配 key，最准）"},
                    ],
                    "labels": BOOK_LABELS})


@app.route("/api/online/test", methods=["POST"])
def api_online_test():
    """
    试查一个书名，回显命中的类型与数据源，方便正式跑之前验证配置。
    不写缓存（测试不应污染正式结果）；网络失败也只回错误信息，不影响任务。
    """
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "请填写要试查的书名"}), 400
    if book_online is None:
        return jsonify({"ok": True, "input": title, "label": None, "source": "",
                        "raw": "", "error": "联网补全模块（book_online.py）未安装",
                        "elapsed": 0, "hint": "当前部署缺少 book_online.py"})
    ocfg = dict(load_config().get("online") or {})
    if data.get("provider"):
        ocfg["provider"] = data["provider"]
    logs = []
    t0 = time.time()
    try:
        clf = book_online.OnlineClassifier(ocfg, booksort_match_text,
                                           cache_path=None, log=logs.append)
        clf.labels = BOOK_LABELS
        res = clf.probe(title)
    except Exception as e:                        # 网络/配置问题都只回错误
        return jsonify({"ok": True, "input": title, "label": None, "source": "",
                        "raw": "", "error": f"{type(e).__name__}: {e}",
                        "elapsed": round(time.time() - t0, 2),
                        "hint": "查询失败（检查网络 / 代理 / api_key 配置）"})
    res.update({
        "ok": True, "input": title,
        "elapsed": round(time.time() - t0, 2),
        "hint": (f"→ 会归入「{res['label']}」目录" if res.get("label")
                 else "未识别（保持「其它图书」）"),
    })
    # 有错误原因就如实说明（否则用户分不清"网络不通"和"站点判不出类型"）
    if res.get("error"):
        res["hint"] = f"查询失败：{res['error']}（检查网络 / 代理 / api_key 配置）"
    return jsonify(res)


# ---- 静态文件 ----
@app.route("/static/<path:filename>")
def static_file(filename):
    return send_from_directory(str(APP_DIR / "static"), filename)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    global RUN_PORT
    p = argparse.ArgumentParser(description="pan-organizer Web 后端")
    p.add_argument("--port", type=int, default=6060)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    RUN_PORT = args.port  # 页脚显示真实端口（默认 6060，可被 --port 覆盖）
    print("=" * 60, flush=True)
    print(f" pan-organizer-web 启动", flush=True)
    print(f"   访问地址: http://{args.host}:{args.port}", flush=True)
    print(f"   应用目录: {APP_DIR}", flush=True)
    print(f"   配置文件: {CONFIG_PATH}", flush=True)
    print(f"   日志目录: {LOGS_DIR}", flush=True)
    print("=" * 60, flush=True)
    # threaded=True 让 SSE 与 /api/run 等并发
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()