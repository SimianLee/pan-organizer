#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网盘自动整理工具 pan-organizer
=========================
通过 Alist 的 WebDAV 接口，按你定义的规则把网盘上的文件移动到指定目录
（适用于 alist 挂载的百度网盘等存储，服务端直移、秒级完成、不占本地流量）。

四个子命令：
  check   测试连接：列出 WebDAV 根目录（所有挂载点）
  extsort 按文件后缀自动归档（无需规则文件）：--path 下文件 → <归档根目录>/<后缀>/
          （归档根目录用 --dest 指定，缺省为 --path 本身，即在原地建后缀子夹）
          --plan <file> 可把扫描结果导出为计划文件；之后用 --from-plan <file> --apply
          直接按该计划移动，跳过重复扫描（Web 端"查询→按计划移动"模式依赖此能力）
  scan    按规则扫描，打印"将要执行的操作"清单（只读，不移动任何文件）
  run     执行整理（默认也是 dry-run 预览；加 --apply 才真正移动）

安全机制（默认 on_conflict=rename）：
  同名文件要进同一目标目录时，先到先得保留原名，后来的自动按序编号为
  "名字 (1).后缀"、"名字 (2).后缀"…；目标目录已存在的同名文件和已占用的
  编号自动跳过，绝不覆盖任何文件。编号在计划阶段就完成，预览直接显示
  每个文件的最终落点（含改名结果），执行时零冲突直移。

规则文件(rules.json)示例见 rules.example.json，配置文件见 config.example.json。
所有网盘内路径均为 WebDAV 完整路径（以 / 开头，例如 /百度网盘/下载），
即 base_url 之后的路径，与 alist 界面里看到的目录结构一致。

纯 Python 标准库实现，零第三方依赖。Python 3.8+。
"""

import argparse
import base64
import datetime
import fnmatch
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from types import SimpleNamespace

# Python 3.13/3.14 的 email.feedparser 在解析 alist 返回的某些 WebDAV 响应头时会
# 触发 RecursionError（参见 cpython email/feedparser.py）。这里用一行兼容性补丁
# 替换 http.client.parse_headers，改用手工 key:value 解析，彻底避开 feedparser。
import email.message
import http.client


def _safe_parse_headers(fp, _class=http.client.HTTPMessage):
    """绕开 email.feedparser RecursionError 的安全 header 解析器。"""
    headers = []
    while True:
        line = fp.readline(http.client._MAXLINE + 1)
        if len(line) > http.client._MAXLINE:
            raise http.client.LineTooLong("header line")
        if not line or line in (b'\r\n', b'\n'):
            break
        headers.append(line.rstrip(b'\r\n'))
    msg = _class()
    for raw in headers:
        try:
            s = raw.decode('iso-8859-1')
            if ':' in s:
                key, val = s.split(':', 1)
                msg[key.strip()] = val.strip()
        except Exception:
            pass
    return msg


http.client.parse_headers = _safe_parse_headers

# 图书联网二次分类（v1.6）：bookonline 规则用。放在 app/ 下的独立模块里，
# 主程序 import 失败时只降级（联网功能不可用），不影响其它功能。
try:
    import book_online
except ImportError:                                    # pragma: no cover
    book_online = None

# Windows 控制台可能默认 GBK，统一转 UTF-8 输出，避免中文报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DAV = "{DAV:}"
# 版本号唯一来源：README 徽标 / Dockerfile label / Web /api/health / 页面页脚都引用它
APP_VERSION = "1.6"
APP_NAME = f"pan-organizer/{APP_VERSION}"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
# 默认按下列顺序查找配置文件（找到第一个就用）：
#   1) 命令行 --config 指定的路径（最高优先级）
#   2) ./config.json（v1.0 老路径，容器内 cwd=/app 时落到 /app/config.json）
#   3) ./data/config.json（v1.1 重构后的统一数据目录）
#   4) /app/data/config.json（Docker 镜像硬编码兜底）
#   5) 以上全不存在 → 报错并提示
# 让 web UI（容器内 cwd=/app）与 host CLI 两种用法都不需要手填路径。
DEFAULT_CONFIG_PATHS = ["config.json", "data/config.json", "/app/data/config.json"]


def resolve_config_path(arg):
    """--config 值解析：显式路径直接用；None/空或显式找不到时按候选自动探测"""
    if arg:  # 用户显式给了路径：就用它（不存在由 load_json 自然报错）
        return arg
    cwd = os.getcwd()
    for cand in DEFAULT_CONFIG_PATHS:
        # 相对路径以当前 cwd 为基准，绝对路径直接用
        probe = cand if os.path.isabs(cand) else os.path.join(cwd, cand)
        if os.path.isfile(probe):
            return probe
    # 走到这里一个都没找到 → 给出包含全部候选的清晰错误
    raise FileNotFoundError(
        "找不到配置文件。请确认以下任一位置存在 config.json：\n"
        + "\n".join(f"  - {os.path.join(cwd, c) if not os.path.isabs(c) else c}"
                    for c in DEFAULT_CONFIG_PATHS)
        + "\n或在命令行用 --config <路径> 显式指定。"
    )


def load_json(path):
    """读取 JSON 配置文件（容忍 BOM，Windows 记事本保存常见）"""
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def fmt_size(n):
    """字节数 → 人类可读"""
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def log(msg=""):
    """统一日志输出：加本地时间前缀，便于事后按时间与 alist/容器日志对齐定位。

    注意：只用于"新增的"说明性日志行（任务头/尾、失败明细等）。
    既有会被测试断言的关键行（如「完成：成功移动 N 个」）保持原样不加前缀。
    """
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          flush=True)


class WebDAVError(Exception):
    """WebDAV 操作失败"""


# ---------------------------------------------------------------------------
# WebDAV 客户端
# ---------------------------------------------------------------------------
class Entry:
    """远端目录中的一个条目"""

    __slots__ = ("path", "name", "is_dir", "size", "mtime")

    def __init__(self, path, is_dir, size=None, mtime=None):
        self.path = path          # 网盘内绝对路径，如 /百度网盘/下载/a.mp4
        self.name = path.rstrip("/").rsplit("/", 1)[-1]
        self.is_dir = is_dir
        self.size = size
        self.mtime = mtime

    def __repr__(self):
        kind = "D" if self.is_dir else "F"
        return f"<Entry {kind} {self.path} {fmt_size(self.size)}>"


class WebDAVClient:
    """极简 WebDAV 客户端：只需要 PROPFIND / MOVE / MKCOL"""

    def __init__(self, base_url, username, password, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.auth_header = f"Basic {token}"
        # /dav 前缀（base_url 的路径部分），用于剥掉响应里的 href 前缀
        self.base_path = urllib.parse.urlparse(self.base_url).path.rstrip("/")

    # -- 底层请求 -----------------------------------------------------------
    def _url(self, path):
        """网盘路径 → 完整 URL（自动做 URL 编码）"""
        return self.base_url + urllib.parse.quote(path, safe="/@:!$&'()*+,;=-._~")

    def _request(self, method, path, headers=None, body=None,
                 retries=3, backoff=1.0):
        """
        底层请求。对 5xx（服务器临时错误，如 500/502/503）自动重试，
        降低大目录树中偶发失败率；只对 5xx 重试，401/403/404/412 等不重试。

        retries < 1 时按 1 次处理（循环至少执行一次，函数必定 return 或 raise）。
        重试日志会带上 MOVE 的目标路径，便于从日志判断"是移动哪一步在重试"。
        """
        # MOVE 的失败原因一半在目标端，重试日志里把 Destination 一并打出来
        dest_hint = ""
        if headers and headers.get("Destination"):
            dest_hint = " → " + urllib.parse.unquote(
                urllib.parse.urlparse(headers["Destination"]).path)
        for attempt in range(max(1, retries)):
            # 每次尝试重建 Request 对象，避免 urllib 复用状态
            req = urllib.request.Request(self._url(path), data=body, method=method)
            req.add_header("Authorization", self.auth_header)
            req.add_header("User-Agent", APP_NAME)
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                last_code = e.code
                last_msg = e.read()
                if 500 <= last_code < 600 and attempt < retries - 1:
                    wait = backoff * (attempt + 1)
                    print(f"    [重试] {method} {path}{dest_hint} → HTTP {last_code}，"
                          f"{wait:.0f}s 后第 {attempt + 2} 次尝试", flush=True)
                    time.sleep(wait)
                    continue
                return last_code, last_msg
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    wait = backoff * (attempt + 1)
                    print(f"    [重试] {method} {path}{dest_hint} → 网络异常，"
                          f"{wait:.0f}s 后第 {attempt + 2} 次尝试", flush=True)
                    time.sleep(wait)
                    continue
                reason = getattr(e, "reason", e)
                raise WebDAVError(
                    f"无法连接 {self.base_url}（{reason}）\n"
                    f"  请检查：① alist 地址/端口是否正确  ② 本机与 NAS 是否同一网络\n"
                    f"  ③ alist 是否在运行  ④ 是否启用了 WebDAV(默认 5244 端口 /dav)"
                )
            except (TimeoutError, ConnectionError) as e:
                # 请求超时/连接中断：与网络异常一样纳入重试；重试耗尽时抛 WebDAVError，
                # 而不是让 TimeoutError 击穿上层，避免整个整理任务中途崩溃。
                if attempt < retries - 1:
                    wait = backoff * (attempt + 1)
                    print(f"    [重试] {method} {path}{dest_hint} → 请求超时/连接异常，"
                          f"{wait:.0f}s 后第 {attempt + 2} 次尝试", flush=True)
                    time.sleep(wait)
                    continue
                raise WebDAVError(
                    f"请求超时/连接异常：{method} {path}\n"
                    f"  目标 {self.base_url} 在 {self.timeout}s 内未正常响应"
                )

    # -- 列目录 -------------------------------------------------------------
    def list_dir(self, path):
        """PROPFIND Depth:1 列出 path 目录下所有条目（不含目录自身）"""
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:">'
            b"<d:prop>"
            b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:displayname/>"
            b"</d:prop></d:propfind>"
        )
        code, data = self._request("PROPFIND", path, headers={"Depth": "1"}, body=body)
        if code == 404:
            raise WebDAVError(f"路径不存在: {path}（HTTP 404）")
        if code != 207:
            raise WebDAVError(f"列目录失败: {path}（HTTP {code}）\n{data[:300].decode('utf-8', 'replace')}")
        return self._parse_propfind(data, path)

    def exists(self, path):
        """检查远端路径是否存在（PROPFIND depth 0）"""
        code, _ = self._request(
            "PROPFIND", path,
            headers={"Depth": "0"},
            body=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
        )
        return code == 207  # 404 不存在；403 等按“无法确认”处理成不存在会误判，按存在更安全

    def _parse_propfind(self, data, req_path):
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise WebDAVError(f"解析 PROPFIND 响应失败: {e}")
        req_norm = self._norm_path(req_path)
        entries = []
        for resp in root.findall(f"{DAV}response"):
            href_el = resp.find(f"{DAV}href")
            if href_el is None or not href_el.text:
                continue
            raw = href_el.text.strip()
            p = urllib.parse.urlparse(raw)  # alist 可能返回完整 URL 或纯路径
            href_path = urllib.parse.unquote(p.path or raw)
            norm = self._norm_path(href_path)

            # 跳过“目录自身”那一条记录（PROPFIND Depth:1 会包含自身）
            if norm == req_norm:
                continue
            # 剥掉 base_url 的路径前缀（如 /dav）
            if self.base_path and norm.startswith(self.base_path):
                norm = norm[len(self.base_path):] or "/"

            is_dir = False
            size = None
            mtime = None
            for propstat in resp.findall(f"{DAV}propstat"):
                if propstat.find(f"{DAV}status") is not None:
                    if "200" not in (propstat.findtext(f"{DAV}status") or ""):
                        continue
                prop = propstat.find(f"{DAV}prop")
                if prop is None:
                    continue
                rt = prop.find(f"{DAV}resourcetype")
                if rt is not None and rt.find(f"{DAV}collection") is not None:
                    is_dir = True
                cl = prop.findtext(f"{DAV}getcontentlength")
                if cl is not None and cl.strip().isdigit():
                    size = int(cl)
                lm = prop.findtext(f"{DAV}getlastmodified")
                if lm:
                    mtime = self._parse_http_date(lm)
            entries.append(Entry(norm, is_dir, size, mtime))
        return entries

    @staticmethod
    def _norm_path(p):
        """把 href 路径规整为网盘内绝对路径形态 /a/b"""
        p = p.replace("\\", "/")
        while p and p != "/" and p.endswith("/"):
            p = p[:-1]
        if not p.startswith("/"):
            p = "/" + p
        # 折叠多余斜杠与 . 段
        parts = [seg for seg in p.split("/") if seg and seg != "."]
        return "/" + "/".join(parts)

    @staticmethod
    def _parse_http_date(s):
        """解析 RFC1123/RFC850 时间，失败返回 None"""
        try:
            dt = datetime.datetime.strptime(s.strip(), "%a, %d %b %Y %H:%M:%S GMT")
            return dt.timestamp()
        except Exception:
            try:
                dt = datetime.datetime.strptime(s.strip(), "%A, %d-%b-%y %H:%M:%S GMT")
                return dt.timestamp()
            except Exception:
                return None

    # -- 移动 ---------------------------------------------------------------
    def move(self, src, dst, overwrite=False, retries=3):
        """
        WebDAV MOVE。overwrite=False 时若目标已存在返回 (412, ...)。
        返回 (http_code, 响应文本)。

        retries：底层 5xx 重试次数。默认 3（对付偶发故障）；对"目标同名"
        这类确定性错误应传 1 跳过重试——重试只会白等 3 秒后原样失败，
        且海量撞名时这些无谓重试会成倍拖慢整体进度、刷屏失败假象。
        """
        dest_url = self._url(dst)
        headers = {
            "Destination": dest_url,
            "Overwrite": "T" if overwrite else "F",
        }
        code, data = self._request("MOVE", src, headers=headers, retries=retries)
        return code, data.decode("utf-8", "replace")

    # -- 创建目录 -----------------------------------------------------------
    def mkdirs(self, path):
        """逐级创建目录（已存在的层级自动跳过）"""
        path = self._norm_path(path)
        segs = [s for s in path.split("/") if s]
        if not segs:
            return
        cur = ""
        for seg in segs:
            cur += "/" + seg
            if self.exists(cur):
                continue
            code, data = self._request("MKCOL", cur)
            if code not in (201, 405, 301):  # 201 新建成功；405/301 已存在
                raise WebDAVError(f"创建目录失败: {cur}（HTTP {code}）\n{data[:300].decode('utf-8', 'replace')}")

    def rmdir(self, path):
        """WebDAV DELETE：删除目录（仅用于清理空目录，调用方须确保为空）。"""
        code, data = self._request("DELETE", path)
        if code not in (200, 204, 404):  # 404 视为已不存在（幂等）
            raise WebDAVError(f"删除目录失败: {path}（HTTP {code}）\n{data[:300].decode('utf-8', 'replace')}")

    def delete(self, path):
        """WebDAV DELETE：删除文件或目录（覆盖流程中的目标清理 / 备份清理用）。
        与 rmdir 的区别：不抛异常、返回 bool，便于调用方在"覆盖"这种危险流程里容错。
        404（已不存在）按幂等视为成功。"""
        try:
            code, _ = self._request("DELETE", path)
        except (WebDAVError, OSError, TimeoutError):
            return False
        return code in (200, 204, 404)


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------
def _compile_clause(clause):
    """把规则里的一条 match 子句编译成 (entry) -> bool 的谓词"""
    ctype = clause.get("type", "")
    if ctype == "ext":
        exts = {str(x).lstrip(".").lower() for x in clause.get("values", [])}
        if not exts:
            raise ValueError("ext 子句缺少 values")
        return lambda e, exts=exts: (not e.is_dir) and e.name.rsplit(".", 1)[-1].lower() in exts

    if ctype == "name_contains":
        keys = [str(x) for x in clause.get("values", [])]
        return lambda e, keys=keys: any(k in e.name for k in keys)

    if ctype == "name_glob":
        pats = clause.get("pattern", "")
        pats = [pats] if isinstance(pats, str) else list(pats)
        return lambda e, pats=pats: any(fnmatch.fnmatch(e.name, p) for p in pats)

    if ctype == "name_regex":
        pat = re.compile(clause.get("pattern", ""))
        return lambda e, pat=pat: bool(pat.search(e.name))

    if ctype == "size_gt":
        limit = float(clause.get("mb", 0)) * 1024 * 1024
        return lambda e, limit=limit: (not e.is_dir) and (e.size or 0) > limit

    if ctype == "size_lt":
        limit = float(clause.get("mb", 0)) * 1024 * 1024
        return lambda e, limit=limit: (not e.is_dir) and (e.size or 0) < limit

    if ctype == "isdir":
        want = bool(clause.get("value", False))
        return lambda e, want=want: e.is_dir is want

    raise ValueError(f"未知的匹配子句类型: {ctype}")


class Rule:
    """一条整理规则：match 全部命中则执行 action"""

    def __init__(self, raw):
        self.name = str(raw.get("name", "未命名规则"))
        self.action = str(raw.get("action", "move")).lower()
        clauses = raw.get("match") or []
        self.predicates = [_compile_clause(c) for c in clauses]
        self.target = str(raw.get("target", "")).rstrip("/")
        if self.action in ("move", "copy") and not self.target:
            raise ValueError(f"规则「{self.name}」缺少 target（动作 move/copy 需要目标目录）")
        if not self.predicates:
            raise ValueError(f"规则「{self.name}」没有 match 条件")

    def match(self, entry):
        return all(p(entry) for p in self.predicates)

    def __repr__(self):
        return f"<Rule {self.name} → {self.action} → {self.target}>"


def load_rules(path):
    """读取 rules 文件：{rules:[...], fallback:{action, target}}"""
    data = load_json(path)
    rules = [Rule(r) for r in data.get("rules", [])]
    if not rules:
        raise ValueError("规则文件里没有任何 rules")
    fallback = data.get("fallback") or {"action": "skip"}
    if not isinstance(fallback, dict):
        fallback = {"action": "skip"}
    fallback.setdefault("action", "skip")
    return rules, fallback


# ---------------------------------------------------------------------------
# 扫描与整理计划
# ---------------------------------------------------------------------------
class Operation:
    """一个待执行的整理动作"""

    def __init__(self, src, dst, rule_name):
        self.src = src
        self.dst = dst
        self.rule_name = rule_name

    def __repr__(self):
        return f"move {self.src} -> {self.dst}"


def norm_path(p):
    return WebDAVClient._norm_path(p)


def collect_files(client, root, depth, exclude):
    """
    遍历 root 目录收集文件。使用显式栈，避免深层目录触发 Python 递归限制。
    depth: 0 只扫本目录下的文件；1 额外深入一层子目录；-1 全部递归。
    exclude: 路径黑名单集合（这些目录整体跳过，不会进入也不会移动其内容）。
    返回 (files, skipped_dirs, skipped_excluded)
    """
    files = []
    skipped_dirs = 0
    skipped_excluded = 0
    dirs_done = 0          # 已扫描目录计数（用于进度输出）
    files_found = 0        # 已发现文件计数
    skipped_dup_dirs = 0   # 因 alist 返回重复目录项而跳过的次数

    # 显式栈元素：(当前路径, 剩余可深入层数)
    # depth=-1 用 None 标记，表示无限递归
    root_norm = norm_path(root)
    stack = [(root_norm, None if depth == -1 else depth)]
    visited = {root_norm}  # 防止重复入栈/重复处理
    while stack:
        path, remaining = stack.pop()
        try:
            entries = client.list_dir(path)
        except WebDAVError as e:
            print(f"  [警告] 无法读取目录 {path}：{e}", flush=True)
            continue
        dirs_done += 1
        if dirs_done % 10 == 0:
            # 每 10 个目录报一次进度，大目录树时更跟手
            print(f"  [扫描中] 已处理 {dirs_done} 个目录，"
                  f"发现 {files_found} 个文件，栈中待处理 {len(stack)} 个目录"
                  f"{'' if skipped_dup_dirs == 0 else f'，跳过重复目录项 {skipped_dup_dirs} 次'}",
                  flush=True)
        for ent in entries:
            if ent.is_dir:
                ent_norm = norm_path(ent.path)
                if ent_norm in exclude or ent.name.startswith("."):
                    skipped_excluded += 1
                    continue
                if remaining == 0:
                    skipped_dirs += 1
                    continue
                if ent_norm in visited:
                    skipped_dup_dirs += 1
                    continue
                visited.add(ent_norm)
                next_remaining = None if remaining is None else remaining - 1
                stack.append((ent.path, next_remaining))
            else:
                files.append(ent)
                files_found += 1

    # 收尾：报最终统计
    print(f"  [扫描完成] 共处理 {dirs_done} 个目录，发现 {files_found} 个文件",
          flush=True)
    return files, skipped_dirs, skipped_excluded


def assign_dup_numbers(ops, existing_map):
    """
    计划阶段重名预编号（安全机制，on_conflict=rename 时使用）。
    对每个目标目录，结合"该目录已存在的条目"与"本次计划内的同名文件"，
    按计划顺序分配落点：先到先得保留原名，后来的自动编号为 "名字 (1).后缀"、
    "名字 (2).后缀"… 目标目录已占用的编号自动跳过，绝不覆盖任何文件。
    就地更新 op['dst']；改名的 op 置 renamed=True、orig_name=原名。
    返回自动改名的文件数。
    """
    buckets = {}
    for op in ops:
        d = op["dst"].rsplit("/", 1)[0]
        buckets.setdefault(d, []).append(op)

    renamed_n = 0
    for d, group in buckets.items():
        occupied = set(existing_map.get(d) or ())
        for op in group:
            fn = op["entry"].name
            if fn not in occupied:
                occupied.add(fn)  # 原名可用，保持不动
                continue
            base, dot, ext = fn.rpartition(".")
            stem = base if dot else fn
            suffix = ("." + ext) if dot else ""
            chosen = None
            for i in range(1, 10000):
                cand = f"{stem} ({i}){suffix}"
                if cand not in occupied:
                    chosen = cand
                    break
            if chosen is None:
                continue  # 编号耗尽（>9999 同名），保留原目标名，交给执行层兜底
            op["dst"] = norm_path(f"{d}/{chosen}")
            op["renamed"] = True
            op["orig_name"] = fn
            occupied.add(chosen)
            renamed_n += 1
    return renamed_n


def probe_dir_names(client, dst_dir):
    """
    探测目标目录内已存在的条目名集合（文件与子目录都算，用于重名安全判断）。
    目录不存在 → 空集；网络/读取异常 → None（表示"无法确认"，调用方不得当作空集）。
    """
    try:
        if not client.exists(dst_dir):
            return set()
        return {e.name for e in client.list_dir(dst_dir)}
    except WebDAVError:
        return None


def build_plan(client, config, rules, fallback, root, depth):
    """
    扫描并生成整理计划。
    返回 (ops, stats) —— stats: {"files":n,"moved":n,"skip_nomatch":n,"skip_same":n,
    "skip_dir":n,"excluded":n,"unreadable":[paths]}
    ops 元素为 dict: {rule, src_entry, dst_path, action, conflict:bool}
    """
    root = norm_path(root)
    # 自动排除：所有规则的 target 目录（防止把已整理目录里的文件再扫进来）
    exclude = set(norm_path(r.target) for r in rules if r.action in ("move", "copy"))
    exclude |= set(norm_path(d) for d in (config.get("options", {}).get("exclude_dirs") or []))

    # 如果扫描根目录本身就是某个规则的 target，去掉自身这条（否则整目录被跳过，无法继续整理）
    if root in exclude:
        exclude.discard(root)
        print(f"  [提示] 扫描目录 {root} 同时是某规则的目标目录，已解除该目录的自排除")

    files, skipped_dirs, skipped_excluded = collect_files(client, root, depth, exclude)

    ops = []
    stats = {
        "files": len(files),
        "hit": 0, "skip_nomatch": 0, "skip_same": 0, "renamed": 0,
        "excluded": skipped_excluded,
    }
    for ent in files:
        if ent.is_dir:
            continue
        rule = next((r for r in rules if r.match(ent)), None)
        if rule is None:
            fb_action = fallback.get("action", "skip")
            if fb_action in ("move", "copy") and fallback.get("target"):
                rule = Rule({
                    "name": "fallback",
                    "action": fb_action,
                    "target": fallback["target"],
                    "match": [{"type": "name_glob", "pattern": "*"}],
                })
            else:
                stats["skip_nomatch"] += 1
                continue

        dst = norm_path(rule.target + "/" + ent.name)
        if dst == norm_path(ent.path):
            stats["skip_same"] += 1
            continue

        if rule.action in ("move", "copy"):
            ops.append({
                "rule": rule.name, "entry": ent, "dst": dst,
                "action": rule.action, "conflict": False,
            })
            stats["hit"] += 1

    # ---- 重名预编号（安全机制）----
    # rename 策略：每个规则目标目录做一次探测（每目录 1 个 PROPFIND 请求），
    # 结合目标目录现存条目 + 计划内同名文件，在计划阶段就确定每个文件的最终落点，
    # 预览直接可见最终文件名；执行时就是干净直移，不再有"先撞 412 再改名"的重试。
    on_conflict = config.get("options", {}).get("on_conflict", "rename")
    if ops and on_conflict == "rename":
        existing = {}
        for tdir in {op["dst"].rsplit("/", 1)[0] for op in ops}:
            existing[tdir] = probe_dir_names(client, tdir)
        stats["renamed"] = assign_dup_numbers(
            ops, {d: s for d, s in existing.items() if s is not None})
        # 探测失败的目录（网络异常）：无法预编号，标记为冲突交由执行层 412 兜底
        for op in ops:
            if existing.get(op["dst"].rsplit("/", 1)[0]) is None:
                op["conflict"] = True
    return ops, stats


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def _conflict_candidate(dst, i):
    """dst(/dir/name.ext) 冲突时尝试的目标名：/dir/name (i).ext"""
    d, _, fn = dst.rpartition("/")
    base, dot, ext = fn.rpartition(".")
    stem = base if dot else fn
    suffix = ("." + ext) if dot else ""
    return norm_path(f"{d}/{stem} ({i}){suffix}")


def _dst_exists(client, dst):
    """安全探测目标路径是否已存在；探测本身失败（服务端/网络异常）返回 None（无法确认）。"""
    try:
        return client.exists(dst)
    except (WebDAVError, OSError, TimeoutError):
        return None


def _move_renamed(client, src, dst):
    """
    撞名后顺延尝试 name (1).ext、(2)… 直至成功。撞名来源可以是标准 412，
    也可以是后端把"目标同名"报成 409/5xx（经 exists 探测确认）。
    返回 (status, detail, renamed)，语义与 _move_one 一致。
    """
    for i in range(1, 10000):
        cand = _conflict_candidate(dst, i)
        c2, m2 = client.move(src, cand, overwrite=False)
        if c2 in (201, 204):
            return "ok", cand, True
        if c2 == 412:
            continue  # 该编号也被同名占用 → 试下一个
        if c2 in (409, 500, 502, 503):
            probe = _dst_exists(client, cand)
            if probe is True:
                continue  # 该编号同样撞名（被服务端报成 4xx/5xx）→ 试下一个
            if 500 <= c2 < 600:
                hint = "已确认该编号无同名占用" if probe is False else "该编号状态无法确认"
                return "fail", (f"改名重试遇服务端故障（HTTP {c2}）{m2.strip()[:200]}"
                                f"（{hint}，稍后重跑即可）"), False
        if 500 <= c2 < 600:
            return "fail", f"改名重试遇服务端故障（HTTP {c2}）{m2.strip()[:200]}", False
        return "fail", f"改名后仍失败（HTTP {c2}）{m2.strip()[:200]}", False
    return "fail", "重名过多（>9999 个同名文件），放弃", False


def _overwrite_with_backup(client, src, dst):
    """
    备份式覆盖（用于"后端不支持覆盖式 MOVE"的场景）。

    背景：alist + 百度网盘这类后端，目标位置存在同名文件时服务端直接返回
    errno=12（文件已存在），alist 把它包装成 HTTP 500 —— 此时 WebDAV 的
    `Overwrite: T` 请求头**完全无效**（协议层语义传不进网盘 API，服务端没有
    覆盖选项）。所以"覆盖"只能由客户端拆成服务端支持的原子步骤来完成：

      1) 目标改名让位：dst → dst.__bak_<ts>（同目录重命名，各后端普遍支持）
      2) 移动源到目标：src → dst（此刻目标位置已空，不会触发"同名"错误）
      3) 成功 → 删掉备份（覆盖正式完成）
         失败 → 把备份改回原名（回滚，目标文件零丢失，源文件留在原处）

    任一步失败都不会让用户丢文件：最坏情况是目标文件暂存在 __bak_ 名下并
    在日志中明示路径，可手动改回。

    返回 (status, detail, renamed)，语义与 _move_one 一致。
    """
    # 纳秒级时间戳：同一秒内对同名目标连续覆盖时，秒级时间戳会让备份名撞车
    # （前一个备份还没删，下一个让位就 412），纳秒级实际不可能重复
    bak = f"{dst}{BAK_MARK}{time.time_ns()}_{os.getpid() % 1000}"
    # 1) 目标让位（改名，不是删除——保证可回滚）
    c1, m1 = client.move(dst, bak, overwrite=False)
    if c1 not in (201, 204):
        return "fail", (f"覆盖前无法让位目标（HTTP {c1}）{m1.strip()[:160]}"
                        f"（源文件与目标文件均未变动）"), False
    # 2) 源 → 目标（目标位置已空，正常应成功）
    c2, m2 = client.move(src, dst, overwrite=False)
    if c2 in (201, 204):
        if client.delete(bak):
            return "ok", dst, True
        # 覆盖本身成功，只是备份没清掉——不影响结果，不判失败
        return "ok", f"{dst}（已覆盖；备份清理失败，残留 {bak}，可手动删除）", True
    # 3) 移动失败 → 回滚备份，把目标文件改回原名
    c3, m3 = client.move(bak, dst, overwrite=False)
    if c3 in (201, 204):
        return "fail", (f"覆盖失败（HTTP {c2}）{m2.strip()[:160]}"
                        f"（已回滚：目标文件恢复原样，源文件保留在源目录）"), False
    return "fail", (f"覆盖失败（HTTP {c2}）且回滚失败（HTTP {c3}）："
                    f"原目标文件现位于 {bak}，请手动改名恢复"), False


def _move_one(client, src, dst, on_conflict):
    """
    移动单个文件并处理冲突，避免为每个文件做"先探测再移动"（海量文件时省一半请求）。
    返回 (status, detail, renamed)：
      status: "ok" / "skip" / "fail"
      detail: 成功时的最终路径（改名时是新名）；失败时是错误信息；skip 是原因

    撞名兜底：除标准 HTTP 412 外，某些 WebDAV 后端（如 alist 接百度盘）会把
    "目标已有同名"直接报成 409/500/502/503。此时对目标补一次 exists() 探测来分辨：
      目标确有同名 → 按撞名处理，自动顺延改名 name (1).ext…；
      目标无同名   → 判为服务端瞬时故障，如实记失败、不误改名（稍后重跑即可）。
    探测只发生在失败的文件上，正常成功路径零额外请求。
    """
    # rename 策略：直接 MOVE(Overwrite:F)
    if on_conflict == "rename":
        code, msg = client.move(src, dst, overwrite=False)
        if code in (201, 204):
            return "ok", dst, False
        if code == 412:
            return _move_renamed(client, src, dst)
        if code in (409, 500, 502, 503):
            probe = _dst_exists(client, dst)
            if probe is True:
                # 撞名被服务端报成 409/5xx → 走改名顺延
                return _move_renamed(client, src, dst)
            if 500 <= code < 600:
                hint = ("已确认目标无同名，判为服务端瞬时故障" if probe is False
                        else "目标状态无法确认（探测请求也失败），暂按服务端故障处理")
                return "fail", (f"HTTP {code} {msg.strip()[:200]}"
                                f"（{hint}，稍后重跑即可）"), False
        return "fail", f"HTTP {code} {msg.strip()[:200]}", False

    if on_conflict == "skip":
        if client.exists(dst):
            return "skip", "目标已存在（on_conflict=skip）", False
        code, msg = client.move(src, dst, overwrite=False)
        if code in (201, 204):
            return "ok", dst, False
        return "fail", f"HTTP {code} {msg.strip()[:200]}", False

    # overwrite：先让服务端直接用源覆盖目标（本地盘 / 部分网盘支持）。
    # 但 alist + 百度网盘等后端**不支持覆盖式 MOVE**——目标同名时服务端返回
    # errno=12（文件已存在），alist 包装成 HTTP 500，`Overwrite: T` 头形同虚设。
    # 注意这是"确定性错误"而非瞬时故障：原版先按 5xx 重试 3 次（白等 1s+2s），
    # 撞名时 3 次必然原样失败，还让日志刷屏"[重试] HTTP 500"的失败假象。
    # 现改为第一次就按不重试拿到结果，立刻用 exists 探测分辨真假：
    #   目标确有同名 → 改用 _overwrite_with_backup()（备份 → 移动 → 删备份 /
    #                  失败回滚），把"覆盖"拆成服务端支持的原子步骤；
    #   目标无同名   → 说明是服务端瞬时故障，再按默认重试补一轮 MOVE；
    #   探测本身失败 → 无法分辨，同样补一轮 MOVE 后再探测一次，仍撞名则
    #                  走备份式覆盖，否则如实报失败。
    code, msg = client.move(src, dst, overwrite=True, retries=1)
    if code in (201, 204):
        return "ok", dst, False
    if code in (409, 412, 500, 502, 503):
        probe = _dst_exists(client, dst)
        if probe is True:
            # 目标确实存在，而后端拒绝覆盖（不支持 Overwrite）→ 客户端拆步覆盖
            return _overwrite_with_backup(client, src, dst)
        # probe 为 False（确认无同名）/ None（探测也失败）：按瞬时故障补一轮
        code2, msg2 = client.move(src, dst, overwrite=True)
        if code2 in (201, 204):
            return "ok", dst, False
        probe2 = _dst_exists(client, dst)
        if probe2 is True:
            # 补一轮仍失败但目标确实存在 → 还是撞名，走备份式覆盖
            return _overwrite_with_backup(client, src, dst)
        hint = ("已确认目标无同名，判为服务端瞬时故障" if probe2 is False
                else "目标状态无法确认（探测请求也失败），暂按服务端故障处理")
        return "fail", (f"HTTP {code2} {msg2.strip()[:200]}"
                        f"（{hint}，稍后重跑即可）"), False
    return "fail", f"HTTP {code} {msg.strip()[:200]}", False


def _fail_kind(detail):
    """把失败信息归类成便于筛选的短标签（日志里靠它快速区分故障类型）。

    这样即使日志有上千行，也能一眼看出"是服务端 5xx、还是网络断、还是目标目录建不了"。
    """
    d = detail or ""
    if "无法创建目标目录" in d:
        return "目标目录创建失败"
    if "无法连接" in d or "请求超时" in d or "网络" in d:
        return "网络/连接异常"
    if re.search(r"HTTP 5\d\d", d):
        return "服务端故障(5xx)"
    if re.search(r"HTTP 40\d", d):
        return "请求被拒绝(4xx)"
    return "其它"


# 失败明细最多列这么多条（完整逐条信息上方 [失败] 行里都有，这里只是汇总便于复制）
FAIL_DETAIL_LIMIT = 50


def _print_failure_details(failures, failed):
    """集中打印失败清单：把散落在几千行日志里的失败项收拢到一处，方便定位与重跑。"""
    if not failures:
        return
    print(f"\n  [失败明细] 共 {failed} 个（源文件均保留在原处，问题排除后重跑即可自动续跑）：",
          flush=True)
    # 按故障类型归类计数，先给一张"体检表"
    kinds = {}
    for _, _, reason in failures:
        k = _fail_kind(reason)
        kinds[k] = kinds.get(k, 0) + 1
    print("    故障分类：" + "，".join(f"{k} {v} 个" for k, v in
                                    sorted(kinds.items(), key=lambda kv: -kv[1])),
          flush=True)
    for src, dst, reason in failures[:FAIL_DETAIL_LIMIT]:
        print(f"    - [{_fail_kind(reason)}] {src}", flush=True)
        print(f"        → {dst}", flush=True)
        print(f"        {reason}", flush=True)
    if len(failures) > FAIL_DETAIL_LIMIT:
        print(f"    … 另有 {len(failures) - FAIL_DETAIL_LIMIT} 条未在此列出"
              f"（可搜索上方「[失败]」行查看全部）", flush=True)


def execute_ops(client, config, ops, detail=True):
    """
    真正执行移动。返回 (成功, 失败, 跳过)。
    detail=False 时成功项只按进度间隔输出（适用于海量文件整理，避免刷屏）。
    无论 detail 与否，每处理完约 5% 的操作都会输出一行带百分比的整体进度
    （格式固定为 "已完成 N% (n/total)"，Web 端据此渲染进度条）。

    结束时会额外打印 [失败明细]（分类 + 清单）与 [执行汇总]（一行汇总，便于 grep）。
    """
    options = config.get("options", {})
    on_conflict = options.get("on_conflict", "rename")
    moved_ok, failed, skipped = 0, 0, 0
    created_dirs = set()
    failures = []          # [(src, dst, reason)] 供日志末尾集中汇总
    total = len(ops)
    t0 = time.time()
    # 进度节奏：大任务（几千上万文件）下让进度条也跟手——
    # 约每 1% 或每 50 个文件报一次（取更小者），总数上限约百级。
    # 原实现每 5% 一行，大任务下首条 [进度] 行来得太晚，进度条长时间卡 0%。
    prog_interval = max(1, min(total // 100, 50))

    for idx, op in enumerate(ops, 1):
        ent = op["entry"]
        dst_dir = op["dst"].rsplit("/", 1)[0]
        mkdir_ok = True
        # 1) 确保目标目录存在（已建过的目录不再探测，海量文件时省大量请求）
        if dst_dir not in created_dirs:
            try:
                client.mkdirs(dst_dir)
                created_dirs.add(dst_dir)
            except WebDAVError as e:
                print(f"  [失败] {ent.path}\n        无法创建目标目录 {dst_dir}：{e}",
                      flush=True)
                failed += 1
                failures.append((ent.path, dst_dir, f"无法创建目标目录：{e}"))
                mkdir_ok = False
        # 2) 移动 / 冲突处理
        if mkdir_ok and op["action"] == "move":
            try:
                status, detail, renamed = _move_one(client, ent.path, op["dst"], on_conflict)
            except (WebDAVError, OSError, TimeoutError) as e:
                # 兜底：单个文件移动抛出的任何网络/服务端异常都不应中断整个整理任务
                failed += 1
                print(f"  [失败] {ent.path}  {e}", flush=True)
                failures.append((ent.path, op["dst"], str(e)))
            else:
                if status == "ok":
                    moved_ok += 1
                    if detail:
                        flag = " →" if renamed else "  "
                        print(f"  [移动] {ent.path}\n        {flag} {detail}",
                              flush=True)
                elif status == "skip":
                    skipped += 1
                    print(f"  [跳过] {ent.path}  {detail}", flush=True)
                elif detail.startswith("HTTP 404"):
                    # 源已不存在（多半是同一计划重复执行/已被手动移动）→ 不算失败
                    skipped += 1
                    print(f"  [跳过] {ent.path}  源已不存在（可能已移动过）", flush=True)
                else:
                    failed += 1
                    print(f"  [失败] {ent.path}  {detail}", flush=True)
                    failures.append((ent.path, op["dst"], detail))
        elif mkdir_ok and op["action"] == "copy":
            print(f"  [跳过] {ent.path}  copy 动作暂不支持，请改用 move", flush=True)
            skipped += 1
        elif mkdir_ok:
            print(f"  [跳过] {ent.path}  未知动作 {op['action']}", flush=True)
            skipped += 1
        # 3) 周期性输出整体进度（成功/失败/跳过都计入"已完成"，最后一条必为 100%）
        if idx % prog_interval == 0 or idx == total:
            pct = int(idx * 100 / total + 0.5)
            print(f"  [进度] 已完成 {pct}% ({idx}/{total})，"
                  f"成功 {moved_ok}，失败 {failed}，跳过 {skipped}", flush=True)

    # 4) 收尾：失败明细（分类+清单）+ 一行汇总（便于 grep / 复制重跑）
    _print_failure_details(failures, failed)
    print(f"  [执行汇总] 共 {total} 个 ｜ 成功 {moved_ok} ｜ 失败 {failed} "
          f"｜ 跳过 {skipped} ｜ 耗时 {time.time() - t0:.1f}s ｜ "
          f"撞名策略 {on_conflict}", flush=True)
    return moved_ok, failed, skipped


# ---------------------------------------------------------------------------
# extsort：自动按文件后缀归档（不需要规则文件）
# ---------------------------------------------------------------------------
# 默认跳过这些"未完成/临时"后缀的文件（大小写不敏感），它们不该被归档
DEFAULT_SKIP_EXTS = {"!qb", "part", "crdownload", "downloading", "tmp", "temp"}
NOEXT_DIR = "noext"  # 无后缀文件归入的文件夹名
# 覆盖式整理的"目标备份"标记（见 _overwrite_with_backup）：带此标记的文件是
# 覆盖过程中临时让位的原目标文件，仅在"回滚也失败"的极端情况下才会残留。
# 扫描时无条件跳过它们，绝不把用户的原文件当普通文件搬走。
BAK_MARK = ".__bak_"


def ext_of(name):
    """取文件小写后缀（不含点）；无后缀/隐藏文件(如 .bashrc)返回 ''"""
    if name.startswith(".") and "." not in name[1:]:
        return ""
    if "." in name:
        ext = name.rsplit(".", 1)[1].lower()
        # 只认常规扩展名：1~10 位纯字母/数字。
        # 反例：'xxx.wmv----[万G中医网（www.taozhengping.com)]' 最后点后是 'com)]'，
        # 含 ')' 等非法字符 → 视为无后缀（归入"其它"目录），而不是建一个 'com)]' 目录。
        if ext and len(ext) <= 10 and all(c.isalnum() for c in ext):
            return ext
        return ""
    return ""


# ---- v2 规则：按扩展名大类别归档（图片/视频/音频/文档/压缩包/代码/其他）----
CATEGORY_MAP = {
    # 图片
    "jpg": "图片", "jpeg": "图片", "png": "图片", "gif": "图片",
    "bmp": "图片", "webp": "图片", "svg": "图片", "ico": "图片",
    "heic": "图片", "tif": "图片", "tiff": "图片", "raw": "图片",
    "cr2": "图片", "nef": "图片", "psd": "图片", "ai": "图片",
    # 视频
    "mp4": "视频", "mkv": "视频", "avi": "视频", "mov": "视频",
    "wmv": "视频", "flv": "视频", "m4v": "视频", "mpg": "视频",
    "mpeg": "视频", "ts": "视频", "rmvb": "视频", "webm": "视频",
    "3gp": "视频", "vob": "视频",
    # 音频
    "mp3": "音频", "wav": "音频", "flac": "音频", "aac": "音频",
    "ogg": "音频", "m4a": "音频", "wma": "音频", "opus": "音频",
    # 文档
    "pdf": "文档", "doc": "文档", "docx": "文档", "xls": "文档",
    "xlsx": "文档", "ppt": "文档", "pptx": "文档", "txt": "文档",
    "md": "文档", "csv": "文档", "epub": "文档", "mobi": "文档",
    "azw3": "文档", "rtf": "文档", "odt": "文档", "tex": "文档",
    # 压缩包
    "zip": "压缩包", "rar": "压缩包", "7z": "压缩包", "tar": "压缩包",
    "gz": "压缩包", "bz2": "压缩包", "xz": "压缩包", "iso": "压缩包",
    # 代码
    "c": "代码", "h": "代码", "cpp": "代码", "hpp": "代码", "cc": "代码",
    "py": "代码", "js": "代码", "ts": "代码", "java": "代码", "go": "代码",
    "rs": "代码", "php": "代码", "rb": "代码", "sh": "代码", "bat": "代码",
    "html": "代码", "css": "代码", "json": "代码", "xml": "代码",
    "yaml": "代码", "yml": "代码", "sql": "代码", "vue": "代码",
    "swift": "代码", "kt": "代码",
}


def category_of(ext):
    """扩展名 → 大类目录名；未收录的归入「其他」"""
    return CATEGORY_MAP.get(ext.lower(), "其他")


# ---------------------------------------------------------------------------
# booksort：图书/漫画按书名类型归类（v1.5）
# ---------------------------------------------------------------------------
# 思路：正则分不出简繁、书名和类型也没有字面必然联系，所以采用
# "漫画后缀直判 + 书名关键词顺位匹配 + 兜底目录" 三层策略：
#   1) cbz/cbr 等漫画后缀 → 漫画/（最可靠，直接判型）
#   2) 书名命中哪条关键词规则 → 对应类型目录（规则按优先级排序，先具体后宽泛）
#   3) 全不命中 → 其它图书/（如《活着》这类书名无类型线索的经典）
# 关键词覆盖不了的（预计 2~3 成）留给人工或后续接 LLM 二次分类。
# 注意 BOOK_RULES 顺序即优先级：越靠前越先匹配，调整顺序即可调分类效果。

# 图书常见格式（纯文本/排版书/扫描书）
BOOK_EXTS = {
    "txt", "pdf", "epub", "mobi", "azw", "azw3", "prc", "doc", "docx",
    "rtf", "html", "htm", "chm", "djvu", "pdb", "lrf", "fb2", "lit",
}
# 漫画专属格式（后缀即铁证，直接判漫画）
COMIC_EXTS = {"cbz", "cbr", "cbt", "cb7", "cba"}
# 漫画目录名 / 图书兜底目录名
COMIC_DIR = "漫画"
BOOK_UNCLASSIFIED = "其它图书"

# 书名关键词规则（顺序 = 优先级，先具体后宽泛）。
# 单字关键词（医/药/史）误伤率高，统一用词组；用 re.IGNORECASE 兼容英文书名。
BOOK_RULES = [
    ("教材教辅", "教材|教辅|教程|考点|真题|试卷|习题|辅导|考试|高考|中考|考研|"
                "四六级|六级|四级|雅思|托福|公务员|建造师|注册会计师|指南|手册|题库|"
                "词典|字典|辞海|工具书"),
    ("计算机IT", "编程|程序|代码|算法|数据结构|人工智能|机器学习|深度学习|神经网络|"
                "Linux|Windows|Python|Java|Javas?Script|C\\+\\+|C#|SQL|数据库|前端|后端|"
                "程序员|软件工程|计算机网络|操作系统|正则表达式|Excel|Photoshop|CAD"),
    ("医学养生", "医学|医药|中医|西医|临床|护理|护士|解剖|针灸|推拿|按摩|艾灸|经络|"
                "养生|保健|健康|药膳|营养|黄帝内经|本草|伤寒论|丹溪|中医入门|康复"),
    ("心理学",   "心理学|心理|情绪|焦虑|抑郁|自卑|自控|意志|微表情|催眠|梦的解析|"
                "人格|性格|认知|幸福感|亲密关系"),
    ("历史传记", "历史|通史|史记|朝代|春秋|战国|秦汉|唐宋|明清|民国|考古|文物|文明史|"
                "帝国|王朝|皇帝|传记|自传|回忆录|人物传|大传|评传|年谱"),
    ("经济管理", "经济|金融|投资|理财|股票|基金|证券|期货|管理|营销|创业|会计|财务|"
                "经济学|资本论|国富论|货币|商业|贸易|公司|领导力|复盘"),
    ("法律",     "法律|法学|法规|刑法|民法|合同法|宪法|诉讼|律师|司法|办案|法条"),
    ("哲学宗教", "哲学|佛教|道教|禅修|禅|佛学|圣经|古兰经|塔木德|论语|道德经|老子|"
                "庄子|孟子|易经|周易|王阳明|心学|苏格拉底|柏拉图|亚里士多德|康德|黑格尔|"
                "尼采|叔本华|罗素|存在主义|形而上学"),
    ("外语学习", "英语|日语|韩语|法语|德语|俄语|西班牙语|新概念|语法|单词|词汇|口语|"
                "听力|阅读理解|English|Japanese"),
    ("少儿绘本", "儿童|幼儿|亲子|育儿|童话|绘本|睡前故事|儿童文学|漫画书|少儿|小朋友|"
                "识字|拼音|启蒙"),
    ("文学小说", "小说|文学|散文|随笔|杂文|诗集|诗歌|词选|名著|文集|全集|选集|长篇|"
                "短篇|科幻|推理|悬疑|武侠|言情|余华|莫言|路遥|贾平凹|金庸|古龙|"
                "村上春树|东野圭吾|马尔克斯|海明威|卡夫卡"),
    ("生活百科", "食谱|菜谱|家常菜|烹饪|烘焙|茶道|咖啡|旅游|旅行|攻略|手工|编织|"
                "家居|装修|收纳|育儿百科|百科全书|生活|健身|瑜伽|跑步|钓鱼|花艺|园艺"),
]
# 预编译（模块加载时一次，扫描海量文件时零重复开销）
BOOK_RULES_COMPILED = [(label, re.compile(pat, re.IGNORECASE))
                        for label, pat in BOOK_RULES]
# 漫画关键词（PDF 等通用格式但书名点明是漫画时用）
COMIC_NAME_RE = re.compile(
    r"漫画|连环画|画集|画册|漫画版|全彩漫画|番外|单行本", re.IGNORECASE)


def booksort_classify(name, ext):
    """
    文件名 + 后缀 → 图书类型目录名。
    返回 None 表示"不是图书/漫画文件"（booksort 规则不碰它）；
    返回 "其它图书" 表示是图书但书名没有类型线索。
    """
    ext = (ext or "").lower()
    # 漫画专属后缀 → 铁证直判
    if ext in COMIC_EXTS:
        return COMIC_DIR
    # 非图书后缀 → 不归 booksort 管
    if ext not in BOOK_EXTS:
        return None
    # 图书格式但书名点明是漫画（大量漫画用 pdf 发布）
    if COMIC_NAME_RE.search(name):
        return COMIC_DIR
    # 关键词顺位匹配（先具体后宽泛，首个命中即归类）
    for label, pattern in BOOK_RULES_COMPILED:
        if pattern.search(name):
            return label
    # 书名无类型线索（如《活着》）→ 兜底目录，留人工/联网二次分类
    return BOOK_UNCLASSIFIED


# ---- 联网二次分类（bookonline 规则，v1.6）----
# 本地关键词命中不了的（其它图书）才联网查；查询源与缓存见 app/book_online.py。
# 两个模块分工：book_online 负责"取回远端分类文本"，这里负责"文本 → 本地类型目录"，
# 用的是同一套 BOOK_RULES，保证两种来源口径一致。
BOOK_LABELS = [label for label, _ in BOOK_RULES] + [COMIC_DIR, BOOK_UNCLASSIFIED]
_BOOK_LABEL_SET = set(BOOK_LABELS)
_COMIC_TEXT_RE = re.compile(r"动漫|漫画|连环画|画集|画册")


def booksort_match_text(text):
    """
    远端返回的分类文本 → 本地类型目录名（None = 判不出来）。

    兼容两种形态：
      1) 面包屑路径 "图书 > 小说 > 社会小说"：**按段从左到右**，取第一个能匹配的段。
         左段是站点大类，比右段更权威 —— 否则「小说 > 历史小说」会被"历史"规则
         抢走，归进历史传记。
      2) LLM 自由文本 "文学小说" / "这本书属于小说"：先精确匹配类型名，再走关键词。
    """
    if not text:
        return None
    t = str(text).strip()
    if t in _BOOK_LABEL_SET:
        return t
    segs = [s.strip() for s in re.split(r"[>\n\r\t|,，、；;]+", t) if s.strip()]
    # 先看有没有直接点明类型名 / 漫画的段
    for s in segs:
        if s in _BOOK_LABEL_SET:
            return s
    for s in segs:
        if _COMIC_TEXT_RE.search(s):
            return COMIC_DIR
    # 再按段顺序跑关键词规则（段优先，不是整串匹配）
    for s in segs:
        for label, pattern in BOOK_RULES_COMPILED:
            if pattern.search(s):
                return label
    return None


def build_online_classifier(cfg, provider=None, limit=None, refresh=False,
                            cache_path=None, verbose=False):
    """
    按 config.json 的 "online" 段构造联网分类器；模块缺失/配置非法时返回 None
    （调用方据此走纯本地逻辑，绝不因为联网功能抛错中断整理）。
    """
    if book_online is None:
        print("  [联网] 未找到 book_online 模块，联网补全不可用（按本地规则继续）")
        return None
    ocfg = (cfg or {}).get("online") or {}
    if provider:
        ocfg = dict(ocfg, provider=provider)
    try:
        clf = book_online.OnlineClassifier(
            ocfg, booksort_match_text, cache_path=cache_path, limit=limit,
            refresh=refresh, log=print)
    except Exception as e:                             # 配置写错也不该炸任务
        print(f"  [联网] 初始化失败，按本地规则继续：{e}")
        return None
    clf.labels = BOOK_LABELS
    clf.verbose = verbose
    return clf


# ---- v2 规则：按文件大小归档（四档）----
SIZE_BUCKETS = [
    (10 * 1024 * 1024, "小于10MB"),
    (100 * 1024 * 1024, "10-100MB"),
    (1024 * 1024 * 1024, "100MB-1GB"),
]
SIZE_GT_1GB = "大于1GB"
SIZE_UNKNOWN = "未知大小"


def size_bucket(size):
    """字节数 → 大小档目录名"""
    if size is None:
        return SIZE_UNKNOWN
    if size < SIZE_BUCKETS[0][0]:
        return SIZE_BUCKETS[0][1]
    if size < SIZE_BUCKETS[1][0]:
        return SIZE_BUCKETS[1][1]
    if size < SIZE_BUCKETS[2][0]:
        return SIZE_BUCKETS[2][1]
    return SIZE_GT_1GB


def date_folder(mtime):
    """mtime 时间戳（秒）→ 'YYYY-MM' 月份目录名；异常/空返回 None"""
    if not mtime:
        return None
    try:
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m")
    except Exception:
        return None


def extsort_plan(client, cfg, root, dest, depth,
                 skip_exts=None, skip_noext=False, only_exts=None,
                 min_mb=None, max_mb=None,
                 enabled_rules=None, regex_pattern=None, online=None):
    """
    自动按规则归档：扫描 root 下所有文件，按勾选的规则（按后缀/按大类/按日期/按大小
    可嵌套拼接目录）归入 dest/<目录>/。支持正则筛选。
    返回 (ops, ext_stats, stats)：
      ops: 与 build_plan 相同的操作字典列表（action 均为 move）
      ext_stats: {ext: {"count","size","dst_dir","already"}}  用于汇总展示
      stats: {"files","hit","skip_same","skip_filter","excluded","unreadable"}
    enabled_rules: 启用的规则 id 集合（extsort/category/by_date/by_size/regex_match）
    online: 联网二次分类器（bookonline 规则用，见 build_online_classifier）；
            None = 纯本地分类（默认，零网络请求）
    """
    root = norm_path(root)
    dest = norm_path(dest)
    enabled_rules = set(enabled_rules or ["extsort"])
    regex = None
    if regex_pattern:
        try:
            regex = re.compile(regex_pattern)
        except re.error:
            regex = None

    # 归档目标区：如果 dest 在扫描范围内，整棵跳过（已归档区不重复扫描）
    exclude = set()
    if dest != root and dest.startswith(root.rstrip("/") + "/"):
        exclude.add(dest)
    if root in exclude:
        exclude.discard(root)
    for d in (cfg.get("options", {}).get("exclude_dirs") or []):
        exclude.add(norm_path(d))

    skip_exts = set(skip_exts or ())
    only_exts = set(only_exts or ())
    min_b = min_mb * 1024 * 1024 if min_mb else None
    max_b = max_mb * 1024 * 1024 if max_mb else None

    files, skipped_dirs, skipped_excluded = collect_files(client, root, depth, exclude)
    ops = []
    ext_stats = {}
    stats = {
        "files": len(files), "hit": 0, "skip_same": 0, "renamed": 0,
        "skip_filter": 0, "excluded": skipped_excluded, "skip_size": 0,
        "skip_nonbook": 0, "online_cand": 0, "online_hit": 0,
    }

    # ---- 联网二次分类预扫：只查"本地判为其它图书"的书，结果按文件名建表 ----
    # 提前批量查（并发）比在主循环里逐本查快得多；查询结果同时落本地缓存，
    # 重跑同目录零请求。pre-scan 复用主循环的过滤条件，避免查了却不搬。
    online_map = {}
    if online is not None:
        cand = []
        for ent in files:
            if BAK_MARK in ent.name:
                continue
            e = ext_of(ent.name)
            if booksort_classify(ent.name, e) != BOOK_UNCLASSIFIED:
                continue                       # 非图书 或 本地已能归类 → 不联网
            if e in skip_exts or (not e and skip_noext):
                continue
            if only_exts and e not in only_exts:
                continue
            if min_b is not None and (ent.size or 0) < min_b:
                continue
            if max_b is not None and (ent.size or 0) > max_b:
                continue
            if "regex_match" in enabled_rules and regex is not None \
                    and not regex.search(ent.name):
                continue
            cand.append(ent.name)
        stats["online_cand"] = len(cand)
        if cand:
            online_map = online.classify_many(cand)
            if not online.verbose:
                s = online.stats
                err = f"，出错 {s['error']} 次" if s["error"] else ""
                print(f"  [联网] 查询完成：{s['queries']} 次新查询，命中 {s['hit']} 个，"
                      f"未识别 {s['miss']} 个，缓存直接命中 {s['cache_hit']} 个{err}",
                      flush=True)
        else:
            print("  [联网] 没有需要联网判定的图书（本地关键词已全部命中）", flush=True)

    for ent in files:
        ext = ext_of(ent.name)
        # 跳过覆盖备份残留（程序自己产生的 __bak_ 文件，见 _overwrite_with_backup）：
        # 无条件跳过，避免把用户的原目标文件当普通文件搬走。
        if BAK_MARK in ent.name:
            stats["skip_filter"] += 1
            continue
        # 正则筛选（v2）：只整理文件名匹配正则的文件
        if "regex_match" in enabled_rules and regex is not None:
            if not regex.search(ent.name):
                stats["skip_filter"] += 1
                continue
        # 过滤：默认跳过后缀 / 无后缀 / 只整理指定后缀
        if ext in skip_exts or (not ext and skip_noext):
            stats["skip_filter"] += 1
            continue
        if only_exts and ext not in only_exts:
            stats["skip_filter"] += 1
            continue
        # 大小过滤
        if min_b is not None and (ent.size or 0) < min_b:
            stats["skip_size"] += 1
            continue
        if max_b is not None and (ent.size or 0) > max_b:
            stats["skip_size"] += 1
            continue

        # ---- 目标目录：按勾选规则嵌套拼接 ----
        # 首段：booksort（图书按类型归类）优先级最高；其次大类（category）、
        # 再次按后缀（extsort）；三者互斥取一。之后按日期（YYYY-MM）、
        # 按大小（四档）逐级追加子目录。
        segs = []
        if "booksort" in enabled_rules:
            bcat = booksort_classify(ent.name, ext)
            if bcat is None:
                # 非图书/漫画文件：booksort 规则不碰，原地保留
                stats["skip_nonbook"] += 1
                continue
            if bcat == BOOK_UNCLASSIFIED and online_map.get(ent.name):
                # 本地没线索 → 用预扫到的联网结果（bookonline 规则）
                bcat = online_map[ent.name]
                stats["online_hit"] += 1
            segs.append(bcat)
        elif "category" in enabled_rules:
            segs.append(category_of(ext))
        elif "extsort" in enabled_rules:
            segs.append(ext if ext else NOEXT_DIR)
        if "by_date" in enabled_rules:
            df = date_folder(ent.mtime)
            if df:
                segs.append(df)
        if "by_size" in enabled_rules:
            segs.append(size_bucket(ent.size or 0))
        if "booksort" in enabled_rules:
            folder = "/".join(segs) if segs else bcat
        else:
            folder = "/".join(segs) if segs else (ext if ext else NOEXT_DIR)

        dst = norm_path(f"{dest}/{folder}/{ent.name}")
        bucket = ext_stats.setdefault(
            ext, {"count": 0, "size": 0, "dst_dir": f"{dest}/{folder}", "already": 0})
        bucket["count"] += 1
        bucket["size"] += ent.size or 0

        if dst == norm_path(ent.path):
            # 文件已经在自己的后缀目录里（幂等）
            bucket["already"] += 1
            stats["skip_same"] += 1
            continue

        # 计划预览行的规则标签：booksort 显示图书类型，其余沿用后缀
        if "booksort" in enabled_rules:
            rule_label = f"[{bcat}]"
        else:
            rule_label = f".{ext}" if ext else f"({NOEXT_DIR})"
        ops.append({
            "rule": rule_label,
            "entry": ent, "dst": dst, "action": "move", "conflict": False,
        })
        stats["hit"] += 1

    # ---- 重名预编号（安全机制）----
    # rename 策略：每个后缀目录最多一次 PROPFIND，结合"现存条目 + 计划内同名文件"
    # 在计划阶段确定最终落点并编号；其余策略只标记冲突，交给执行层按策略处理。
    on_conflict = cfg.get("options", {}).get("on_conflict", "rename")
    stats["renamed"] = 0
    if ops:
        exist_cache = {}
        for op in ops:
            tdir = op["dst"].rsplit("/", 1)[0]
            if tdir not in exist_cache:
                exist_cache[tdir] = probe_dir_names(client, tdir)
        if on_conflict == "rename":
            stats["renamed"] = assign_dup_numbers(
                ops, {d: s for d, s in exist_cache.items() if s is not None})
            for op in ops:
                if exist_cache.get(op["dst"].rsplit("/", 1)[0]) is None:
                    op["conflict"] = True
        else:
            for op in ops:
                cache = exist_cache.get(op["dst"].rsplit("/", 1)[0])
                if cache is not None:
                    op["conflict"] = op["entry"].name in cache

    # ---- 目标目录原本文件统计（防覆盖可见性）----
    # exist_cache 来自 probe_dir_names，目标目录原本条目数（含文件/子目录）
    # 给汇总打印用：让用户看到"原本有 X 条，本次新增 Y 条，撞名自动编号 Z 条"
    target_pre = {}
    for op in ops:
        tdir = op["dst"].rsplit("/", 1)[0]
        rec = target_pre.setdefault(
            tdir,
            {"existing": len(exist_cache.get(tdir) or ()),
             "new": 0, "renamed_here": 0, "conflict_unknown": 0})
        rec["new"] += 1
        if op.get("renamed"):
            rec["renamed_here"] += 1
        elif op.get("conflict"):
            rec["conflict_unknown"] += 1
    stats["target_pre"] = target_pre

    return ops, ext_stats, stats


def cleanup_empty_dirs(client, root, excludes=None):
    """
    v2 规则：整理完后删除 root 下所有"空目录"（无文件且无子目录）。
    自底向上递归，只删真正空的目录；不删 root 本身、不删 excludes 里的目录
    （如归档目标区 dest 或外部黑名单）。返回删除的目录数。
    """
    root = norm_path(root)
    excludes = {norm_path(d) for d in (excludes or ())}
    deleted = 0

    # 先自顶向下收集整棵目录树（显式栈，避免递归限制），得到"子目录先于父目录"的处理顺序
    order = []
    visited = {root}
    stack = [(root, False)]
    while stack:
        path, processed = stack.pop()
        if path in excludes:
            continue
        if processed:
            order.append(path)
            continue
        stack.append((path, True))
        try:
            entries = client.list_dir(path)
        except WebDAVError:
            continue
        for e in entries:
            if e.is_dir:
                ep = norm_path(e.path)
                if ep in visited or ep in excludes:
                    continue
                visited.add(ep)
                stack.append((ep, False))

    # 自底向上删除当前确为空的目录
    for d in reversed(order):
        if d == root or d in excludes:
            continue
        try:
            cur = client.list_dir(d)
        except WebDAVError:
            continue
        # 空 = 无任何条目（子目录已在上一轮自底向上删掉，这里拿到的是最新状态）
        if cur:
            continue
        try:
            client.rmdir(d)
            deleted += 1
            print(f"  [清理] 删除空目录 {d}", flush=True)
        except WebDAVError as e:
            print(f"  [警告] 删除空目录失败 {d}：{e}", flush=True)
    return deleted


def _fmt_ext_stats(ext_stats):
    """后缀统计 → 打印用行列表（按文件数降序）"""
    rows = []
    for ext, s in sorted(ext_stats.items(), key=lambda kv: -kv[1]["count"]):
        label = f".{ext}" if ext else f"({NOEXT_DIR})"
        note = f"（{s['already']} 个已在目标目录）" if s["already"] else ""
        rows.append((label, s["count"], s["size"], s["dst_dir"], note))
    return rows


def _load_plan_file(path):
    """
    读取 extsort --plan 导出的计划文件，重建可直接交给 execute_ops 的 ops 列表。
    兼容两种文件格式：
      {"meta": {...}, "ops": [{"src","dst","size",...}]}   （新格式，v1.2+）
      [{"src","dst","size",...}]                           （旧格式，纯数组）
    返回 (meta, ops)。meta 缺失字段补默认，便于下游统一访问。
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "ops" in data:
        meta = dict(data.get("meta") or {})
        raw_ops = data["ops"]
    elif isinstance(data, list):
        meta, raw_ops = {}, data
    else:
        raise ValueError(f"计划文件结构无法识别：{path}")
    ops = []
    for it in raw_ops:
        if "entry" in it:          # 已是 op dict（防御：直接复用）
            ops.append(it)
            continue
        if "src" not in it or "dst" not in it:
            raise ValueError(f"计划条目缺少 src/dst 字段：{it}")
        src = it["src"]
        ops.append({
            "rule": it.get("rule", ""),
            "entry": SimpleNamespace(
                path=src, name=src.rsplit("/", 1)[-1],
                size=it.get("size", 0),
            ),
            "dst": it["dst"],
            "action": it.get("action", "move"),
            "conflict": bool(it.get("renamed") or it.get("conflict")),
            "renamed": bool(it.get("renamed")),
            "orig_name": it.get("orig_name"),
        })
    return meta, ops


def _print_plan_meta(meta, ops):
    """打印 from-plan 加载后的计划概要（供 dry-run 与执行前确认）"""
    def _fmt_ts(v):
        try:
            return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, OSError, ValueError):
            return "—"
    print(f"计划内移动操作：{len(ops)} 个")
    if meta.get("src"):
        print(f"  扫描源：{meta.get('src')}")
    if meta.get("dest"):
        print(f"  归档目录：{meta.get('dest')}")
    if meta.get("created_at"):
        print(f"  计划生成：{_fmt_ts(meta.get('created_at'))}")
    print(f"  说明：跳过扫描直接按此计划移动；已移动过的文件（源不存在）会自动跳过，可安全重复执行")


def _argv_echo():
    """回显本次真实命令行（只取参数部分，可直接复制重跑）"""
    return " ".join(sys.argv[1:]) or "(无参数)"


def print_run_header(args, cfg, alist, task_label, enabled_rules=None):
    """任务启动头：把"这次跑的究竟是什么"一次性写进日志。

    排查线上问题时最常见的三个问题——用的是哪个配置、命令行是什么、
    撞名策略/规则是哪套——这里全部有答案；行首带时间戳，可与 alist、
    Docker 容器的日志按时间对齐。
    """
    log("=" * 62)
    log(f"启动 · {task_label}")
    log(f"  版本     : {APP_NAME}")
    log(f"  配置文件 : {args.config}")
    log(f"  命令行   : python pan_organizer.py {_argv_echo()}")
    log(f"  挂载点   : {alist.get('base_url', '')}")
    log(f"  撞名策略 : on_conflict="
        f"{cfg.get('options', {}).get('on_conflict', 'rename')}")
    if enabled_rules is not None:
        log(f"  启用规则 : {', '.join(sorted(enabled_rules)) or '(无)'}")
    log("=" * 62)


def print_run_footer(t0, counts=None):
    """任务收尾：耗时 + 成功/失败/跳过 + 失败时怎么办。

    有了这一行，日志无论中间刷多少屏，只看最后几行就能判断任务结论。
    """
    dur = time.time() - t0
    log("-" * 62)
    if counts is None:
        log(f"结束 · 耗时 {dur:.1f}s")
    else:
        ok, bad, skip = counts
        log(f"结束 · 成功 {ok} ｜ 失败 {bad} ｜ 跳过 {skip} ｜ 耗时 {dur:.1f}s")
        if bad:
            log("  失败项说明：源文件都还在原处、目标未被破坏；"
                "排除原因后重跑同一条命令即可续跑（已成功的会自动跳过）")
    log("-" * 62)


def cmd_extsort(args):
    t_start = time.time()
    args.config = resolve_config_path(args.config)
    cfg = load_json(args.config)
    alist = cfg["alist"]
    client = WebDAVClient(
        alist["base_url"], alist.get("username", ""), alist.get("password", ""),
        timeout=alist.get("timeout", 30),
    )
    # ---- 按计划执行（--from-plan）：跳过扫描，直接移动上次查询导出的计划 ----
    # 注意：此分支不需要 --path，必须放在 path 必填检查之前
    if args.from_plan:
        plan_path = os.path.expanduser(args.from_plan)
        if not os.path.exists(plan_path):
            print(f"[错误] 计划文件不存在：{plan_path}\n"
                  f"请先运行一次带 --plan 的查询生成计划，例如：\n"
                  f"  python pan_organizer.py extsort --path /目录 --plan plan.json")
            sys.exit(2)
        try:
            meta, ops = _load_plan_file(plan_path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"[错误] 计划文件读取失败：{plan_path}\n  {e}")
            sys.exit(2)
        print_run_header(args, cfg, alist, "按计划执行 extsort")
        print(f"按计划执行：{plan_path}", flush=True)
        _print_plan_meta(meta, ops)
        if not args.apply:
            print("\n以上为计划概要（dry-run），未执行任何操作。确认无误后加 --apply 执行：")
            print(f"  python pan_organizer.py extsort --from-plan {args.from_plan} --apply")
            return
        if not ops:
            print("\n计划内没有需要移动的文件。")
            print_run_footer(t_start, (0, 0, 0))
            return
        print("\n开始按计划执行 …\n", flush=True)
        moved_ok, failed, skipped = execute_ops(client, cfg, ops, detail=args.verbose)
        print(f"\n完成：成功移动 {moved_ok} 个，失败 {failed} 个，跳过 {skipped} 个。")
        if failed:
            print("失败项通常为目标目录创建/网络瞬时问题，重新执行同一命令即可续跑"
                  "（已移动成功的会自动跳过）。")
        print_run_footer(t_start, (moved_ok, failed, skipped))
        return

    if not args.path:
        print("请用 --path 指定要整理的网盘目录，例如：\n"
              "  python pan_organizer.py extsort --path /百度网盘/下载 --apply")
        sys.exit(2)

    root = norm_path(args.path)
    dest = norm_path(args.dest) if args.dest else root
    # 启用规则在打任务头之前解析出来，让日志第一屏就写清楚"这次启用了哪些规则"
    enabled_rules = {x.strip() for x in (args.rules or "extsort").split(",") if x.strip()}

    print_run_header(args, cfg, alist, "extsort 整理", enabled_rules)
    print(f"挂载点：{alist['base_url']}", flush=True)
    print(f"扫描目录：{root}（深度 {'全部递归' if args.depth == -1 else args.depth}）", flush=True)
    if args.dest:
        print(f"归档目录：{dest}（--dest 指定；文件按后缀进 {dest}/mp4/、{dest}/pdf/ …）", flush=True)
    else:
        print(f"归档目录：{dest}（未指定 --dest，缺省 = 扫描目录，在源目录内按后缀建子夹；"
              f"如需整理到别处请加 --dest /目标路径）", flush=True)
    print("正在连接 alist 并读取目录结构…", flush=True)
    if not client.exists(root):
        print(f"[错误] 目录不存在：{root}\n请先运行 check 查看可用挂载点。")
        sys.exit(2)

    # skip_incomplete 规则 = 是否启用默认的"未完成下载文件"后缀黑名单（.part/.tmp/.crdownload…）
    skip_exts = {x.strip().lower().lstrip(".") for x in (args.skip_ext or "").split(",") if x.strip()}
    if "skip_incomplete" in enabled_rules:
        skip_exts |= DEFAULT_SKIP_EXTS
    only_exts = {x.strip().lower().lstrip(".") for x in (args.only_ext or "").split(",") if x.strip()}
    if skip_exts:
        print(f"默认跳过未完成后缀：{', '.join(sorted(skip_exts))}")
    if only_exts:
        print(f"只整理后缀：{', '.join(sorted(only_exts))}")

    # ---- 联网二次分类器（bookonline 规则，v1.6）----
    # 只有在 booksort 同时启用时才有意义：bookonline 只负责把"其它图书"进一步细化。
    online = None
    if "bookonline" in enabled_rules:
        if "booksort" not in enabled_rules:
            print("[提示] 联网补全（bookonline）只在同时启用 booksort 时生效，本次忽略。")
        elif book_online is None:
            print("[提示] 未找到 book_online 模块，联网补全不可用，按本地规则继续。")
        else:
            online = build_online_classifier(
                cfg, provider=args.online_provider, limit=args.online_limit,
                refresh=args.online_refresh, verbose=args.verbose,
                cache_path=os.path.join(
                    os.path.dirname(os.path.abspath(args.config)), "online_cache.json"))
            if online is not None:
                ocfg = online.cfg
                print(f"联网补全已启用：数据源 {ocfg['provider']}，并发 {ocfg['workers']}，"
                      f"超时 {ocfg['timeout']}s"
                      f"{'，本次最多查 %d 本' % online.limit if online.limit else ''}"
                      f"；只对本地判为「{BOOK_UNCLASSIFIED}」的图书联网")

    ops, ext_stats, stats = extsort_plan(
        client, cfg, root, dest, args.depth,
        skip_exts=skip_exts, skip_noext=args.skip_noext, only_exts=only_exts,
        min_mb=args.min_mb, max_mb=args.max_mb,
        enabled_rules=enabled_rules, regex_pattern=args.regex_pattern,
        online=online,
    )
    if enabled_rules != {"extsort"} and enabled_rules != {"extsort", "skip_incomplete"}:
        print(f"启用的规则：{', '.join(sorted(enabled_rules))}"
              f"{'（正则: ' + args.regex_pattern + '）' if args.regex_pattern else ''}")

    # ---- 汇总展示 ----
    rows = _fmt_ext_stats(ext_stats)
    total_size = sum(s["size"] for s in ext_stats.values())
    print(f"\n共扫描 {stats['files']} 个文件，命中 {len(ops)} 个待归档"
          f"（合计 {fmt_size(total_size)}）：")
    if rows:
        w = max(len(r[0]) for r in rows)
        for label, count, size, dst_dir, note in rows:
            print(f"  {label:<{w}}  {count:>6} 个  {fmt_size(size):>9}  →  {dst_dir}  {note}")
    if stats["skip_same"]:
        print(f"其中 {stats['skip_same']} 个已在对应后缀目录中（幂等跳过）")
    if stats["skip_filter"] or stats["skip_size"] or stats.get("skip_nonbook"):
        skip_reason = []
        if stats["skip_filter"]:
            skip_reason.append(f"跳过未完成/指定外后缀 {stats['skip_filter']} 个")
        if stats["skip_size"]:
            skip_reason.append(f"大小不在范围内 {stats['skip_size']} 个")
        if stats.get("skip_nonbook"):
            skip_reason.append(f"非图书/漫画文件（booksort 规则不搬动）{stats['skip_nonbook']} 个")
        print(f"未列入计划：{'，'.join(skip_reason)}")
    if stats.get("online_cand"):
        print(f"联网补全：{stats['online_cand']} 个书名无本地线索 → 联网判定，"
              f"其中 {stats['online_hit']} 个已归类成功"
              f"（其余仍归 {BOOK_UNCLASSIFIED}，可稍后重跑或改用手工归类）")

    # ---- 目标目录原本条目检测（防覆盖可见性）----
    # 把"目标目录原本有什么"明示给用户，并结合本次策略给出结论：
    #   rename    → 原本文件不受影响，撞名的源文件自动编号 (1)(2)…
    #   skip      → 撞名的源文件不搬
    #   overwrite → 原本的同名文件会被源文件替换（备份式覆盖，失败自动回滚）
    target_pre = stats.get("target_pre") or {}
    if target_pre:
        print(f"\n[目标目录原本条目检测]  原条目 = 移动前就在目标目录里的条目（文件+子目录）：")
        # 按"原本条目数"降序，让有原本内容的目录优先显示
        rows_pre = sorted(target_pre.items(),
                          key=lambda kv: (kv[1]["existing"], kv[0]),
                          reverse=True)
        for d, rec in rows_pre:
            extra = ""
            if rec["renamed_here"]:
                extra = f"  其中 {rec['renamed_here']} 个撞名已自动改名 (1)(2)…（原本同名文件不受影响）"
            elif rec["conflict_unknown"]:
                extra = f"  其中 {rec['conflict_unknown']} 个目标目录探测失败（无法判断撞名）"
            print(f"  {d:<50}  原本 {rec['existing']:>5} 条  本次新增 {rec['new']:>6} 个{extra}")
        # 结尾结论按实际策略给（旧版这里写死 overwrite=False，与 overwrite 策略自相矛盾）
        _oc = cfg.get("options", {}).get("on_conflict", "rename")
        if _oc == "overwrite":
            print("  ★ 移动策略 on_conflict=overwrite：目标处的同名文件会被源文件替换"
                  "（先备份让位再移入，失败自动回滚，不会丢文件）")
        elif _oc == "skip":
            print("  ★ 移动策略 on_conflict=skip：目标已有同名 → 跳过不搬，原本文件不受影响")
        else:
            print("  ★ 移动策略 on_conflict=rename：原本文件 100% 不被覆盖"
                  "（撞名 → 新文件改名 (1)(2)…）")

    # ---- 同名处理提示：rename 策略下已在计划阶段预编号，预览即可看到最终落点 ----
    on_conflict = cfg.get("options", {}).get("on_conflict", "rename")
    conflict_n = sum(1 for op in ops if op.get("conflict"))
    renamed_n = stats.get("renamed", 0)
    if renamed_n:
        first = next((op for op in ops if op.get("renamed")), None)
        ex = (f"例如 {first['orig_name']} → {first['dst'].rsplit('/', 1)[-1]}"
              if first is not None else "")
        print(f"⚠ {renamed_n} 个文件因同名已自动按序编号，绝不覆盖任何文件{ex}")
    elif conflict_n:
        print(f"⚠ {conflict_n} 个文件目标位置存在同名/无法探测，"
              f"冲突策略 on_conflict={on_conflict}")

    if args.verbose:
        for op in ops:
            ent = op["entry"]
            mark = "  ⚠ 同名自动编号" if op.get("renamed") else ""
            print(f"  · {ent.path}  [{fmt_size(ent.size)}]"
                  f"\n      ⇒ move → {op['dst']}{mark}")

    if args.plan:
        meta = {
            "type": "extsort",
            "created_at": time.time(),
            "src": args.path,
            "dest": args.dest or args.path,
            "depth": args.depth,
            "skip_ext": args.skip_ext or "",
            "only_ext": args.only_ext or "",
            "skip_noext": bool(args.skip_noext),
            "min_mb": args.min_mb,
            "max_mb": args.max_mb,
            "on_conflict": cfg.get("options", {}).get("on_conflict", "rename"),
            "count": len(ops),
            "total_size": total_size,
        }
        with open(args.plan, "w", encoding="utf-8") as f:
            json.dump({
                "meta": meta,
                "ops": [{"src": op["entry"].path, "dst": op["dst"],
                         "size": op["entry"].size, "rule": op["rule"],
                         "renamed": bool(op.get("renamed")),
                         "orig_name": op.get("orig_name")} for op in ops],
            }, f, ensure_ascii=False, indent=2)
        print(f"计划已导出：{args.plan}（{len(ops)} 个操作；可用 --from-plan 按此计划执行，跳过重复扫描）")

    if not args.apply:
        print("\n以上为预览（dry-run），未做任何修改。确认无误后加 --apply 执行：")
        tip = f"  python pan_organizer.py extsort --path {args.path}"
        if args.dest:
            tip += f" --dest {args.dest}"
        print(tip + " --apply")
        return

    # ---- 执行文件移动（无操作时跳过，清理空目录仍会执行）----
    moved_ok = failed = skipped = 0
    if ops:
        # 大数据量提醒
        if len(ops) > 5000:
            print(f"\n本次将移动 {len(ops)} 个文件（约 {fmt_size(total_size)}），"
                  f"请确保网络稳定。执行中可随时 Ctrl+C 中断，已完成的不会重复移动。")

        print("\n开始执行 …\n")
        moved_ok, failed, skipped = execute_ops(client, cfg, ops, detail=args.verbose)
        print(f"\n完成：成功移动 {moved_ok} 个，失败 {failed} 个，跳过 {skipped} 个。")
        if failed:
            print("有失败项，重新运行同一条命令即可继续（已成功的会自动跳过）。")
    else:
        print("\n没有需要执行的操作，目录已经整理好了。")

    # ---- v2 规则：清理空目录 ----
    if "cleanup_empty" in enabled_rules:
        # 归档目标区（dest）不清理，避免删掉刚归档出来的目录
        excludes = [dest]
        for d in (cfg.get("options", {}).get("exclude_dirs") or []):
            excludes.append(norm_path(d))
        removed = cleanup_empty_dirs(client, root, excludes=excludes)
        if removed:
            print(f"\n已清理 {removed} 个空目录。")
        else:
            print("\n未发现可清理的空目录。")

    print_run_footer(t_start, (moved_ok, failed, skipped))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_check(args):
    args.config = resolve_config_path(args.config)
    cfg = load_json(args.config)
    alist = cfg["alist"]
    client = WebDAVClient(
        alist["base_url"], alist.get("username", ""), alist.get("password", ""),
        timeout=alist.get("timeout", 30),
    )
    print(f"连接 {alist['base_url']} …")
    entries = client.list_dir("/")
    print(f"✓ 连接成功。WebDAV 根目录共 {len(entries)} 个挂载点：")
    for e in sorted(entries, key=lambda x: x.path):
        kind = "📁" if e.is_dir else "📄"
        print(f"  {kind} {e.path}")
    print("\n接下来：在 rules.json 里把规则 target 的 /网盘挂载点 前缀改成你的挂载点，")
    print("然后运行  scan 预览、run --apply 执行。")


def _common_subparser(sub, name, help_text):
    p = sub.add_parser(name, help=help_text, description=help_text)
    p.add_argument("--config", default=None,
                   help="配置文件路径（默认自动探测：./config.json → ./data/config.json → /app/data/config.json）")
    p.add_argument("--rules", default="rules.json", help="规则文件路径（默认 rules.json）")
    p.add_argument("--path", default=None, help="待整理的网盘目录（WebDAV 完整路径，如 /百度网盘/下载）")
    p.add_argument("--depth", type=int, default=0,
                   help="扫描深度：0=仅当前目录(默认)，1=含一层子目录，N=N层，-1=全部递归")
    return p


def _make_plan(args, apply_mode):
    args.config = resolve_config_path(args.config)
    cfg = load_json(args.config)
    if not args.path:
        print("请用 --path 指定要整理的网盘目录，例如：\n"
              "  python pan_organizer.py scan --path /百度网盘/下载")
        sys.exit(2)
    alist = cfg["alist"]
    client = WebDAVClient(
        alist["base_url"], alist.get("username", ""), alist.get("password", ""),
        timeout=alist.get("timeout", 30),
    )
    rules, fallback = load_rules(args.rules)
    print_run_header(args, cfg, alist,
                     f"{'执行' if apply_mode else '扫描预览'}（规则文件模式）")
    print(f"挂载点前缀：{alist['base_url']}")
    print(f"扫描目录：{args.path}（深度 {'全部递归' if args.depth == -1 else args.depth}）")
    print(f"规则数：{len(rules)}，未命中行为：{fallback.get('action')}\n")
    root = norm_path(args.path)
    if not client.exists(root):
        print(f"[错误] 目录不存在：{root}\n请先运行 check 查看可用挂载点。")
        sys.exit(2)
    ops, stats = build_plan(client, cfg, rules, fallback, root, args.depth)

    # 打印规则名列表帮助理解
    for i, r in enumerate(rules, 1):
        print(f"  规则{i}「{r.name}」: {len(r.predicates)} 个匹配条件 → {r.action} → {r.target}")
    if fallback.get("action") == "move" and fallback.get("target"):
        print(f"  fallback「未匹配兜底」: → move → {fallback.get('target')}")
    print("")

    print(f"共扫描 {stats['files']} 个文件（跳过已排除目录 {stats['excluded']} 个），"
          f"命中规则 {len(ops)} 个：")
    conflict_n = sum(1 for op in ops if op.get("conflict"))
    for op in ops:
        ent = op["entry"]
        if op.get("renamed"):
            mark = "  ⚠ 同名自动编号"
        elif op.get("conflict"):
            mark = "  ⚠ 目标已存在(按冲突策略处理)"
        else:
            mark = ""
        print(f"  · {ent.path}  [{fmt_size(ent.size)}]"
              f"\n      ⇒ {op['action']} → {op['dst']}   [规则: {op['rule']}]{mark}")
    print(f"\n未命中规则跳过 {stats['skip_nomatch']} 个，已在目标目录 {stats['skip_same']} 个。")
    renamed_n = stats.get("renamed", 0)
    if renamed_n:
        first = next((op for op in ops if op.get("renamed")), None)
        ex = (f"例如 {first['orig_name']} → {first['dst'].rsplit('/', 1)[-1]}"
              if first is not None else "")
        print(f"⚠ {renamed_n} 个文件因同名已自动按序编号，绝不覆盖任何文件{ex}")
    elif conflict_n:
        cfg_opt = cfg.get("options", {}).get("on_conflict", "rename")
        print(f"注意：{conflict_n} 个文件目标位置存在同名/无法探测，冲突策略 on_conflict={cfg_opt}")
    if not apply_mode:
        print("\n以上为预览（dry-run），未做任何修改。确认无误后运行：")
        print("  python pan_organizer.py run --path <目录> --apply")
    return client, cfg, ops, stats


def cmd_scan(args):
    t0 = time.time()
    _make_plan(args, apply_mode=False)
    print_run_footer(t0)


def cmd_run(args):
    t0 = time.time()
    client, cfg, ops, stats = _make_plan(args, apply_mode=True)
    if not args.apply:
        print("\n[预览模式] 未执行任何操作。确认无误后加 --apply 才会真正移动文件。")
        print_run_footer(t0)
        return
    if not ops:
        print("\n没有需要执行的操作。")
        print_run_footer(t0, (0, 0, 0))
        return
    print("\n开始执行 …\n")
    moved_ok, failed, skipped = execute_ops(client, cfg, ops)
    print(f"\n完成：成功移动 {moved_ok} 个，失败 {failed} 个，跳过 {skipped} 个。")
    print_run_footer(t0, (moved_ok, failed, skipped))


def main():
    parser = argparse.ArgumentParser(
        prog="pan_organizer.py",
        description="通过 Alist WebDAV 按规则整理网盘文件（默认只预览，安全优先）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="测试连接并列出所有挂载点")
    p_check.add_argument("--config", default=None,
                         help="配置文件路径（默认自动探测：./config.json → ./data/config.json → /app/data/config.json）")

    _common_subparser(sub, "scan", "按规则扫描并预览整理计划（只读）")
    _common_subparser(sub, "run", "执行整理（默认 dry-run 预览，--apply 才真正移动）")
    sub.choices["run"].add_argument("--apply", action="store_true",
                                    help="真正执行移动（不加此参数仅预览）")

    p_ext = sub.add_parser(
        "extsort",
        help="按文件后缀自动归档：把 --path 下的文件按扩展名移入 <归档根目录>/<后缀>/",
        description="按文件后缀自动归档，无需规则文件。"
                    "例：xxx.mp4 → <dest>/mp4/xxx.mp4，无后缀文件 → <dest>/noext/。"
                    "归档根目录由 --dest 指定（缺省 = --path，即原地按后缀建子夹）。"
                    "同名文件自动按序编号（名字 (1).后缀…），绝不覆盖。"
                    "默认只预览，加 --apply 才真正移动。",
    )
    p_ext.add_argument("--config", default=None,
                       help="配置文件路径（默认自动探测：./config.json → ./data/config.json → /app/data/config.json）")
    p_ext.add_argument("--path", default=None,
                       help="要整理的网盘目录（WebDAV 完整路径，如 /百度网盘/下载）")
    p_ext.add_argument("--dest", default=None,
                       help="归档根目录（默认 = --path，即在原目录内按后缀建子文件夹）")
    p_ext.add_argument("--depth", type=int, default=-1,
                       help="扫描深度：0=仅当前目录，N=N层，-1=全部递归（默认 -1）")
    p_ext.add_argument("--skip-ext", default="",
                       help="额外跳过后缀（逗号分隔，如 wmv,iso）。"
                            ".part/.tmp/.crdownload/.downloading/.!qB/.temp 默认已跳过")
    p_ext.add_argument("--only-ext", default="",
                       help="只整理这些后缀（逗号分隔，如 mp4,mkv,avi），其余全部跳过")
    p_ext.add_argument("--skip-noext", action="store_true",
                       help="跳过没有后缀的文件（默认归入 noext/ 文件夹）")
    p_ext.add_argument("--min-mb", type=float, default=None,
                       help="只移动大于等于该大小(MB)的文件")
    p_ext.add_argument("--max-mb", type=float, default=None,
                       help="只移动小于等于该大小(MB)的文件")
    p_ext.add_argument("--plan", default=None,
                       help="把整理计划导出为 JSON 文件（供审计 / 后续 --from-plan 复用）")
    p_ext.add_argument("--from-plan", default=None,
                       help="按指定计划文件执行，跳过扫描：读取上次 --plan 导出的 JSON 后直接移动"
                            "（加 --apply 才真正移动；源已不存在的文件自动跳过，可安全重跑）")
    p_ext.add_argument("--apply", action="store_true",
                       help="真正执行移动（不加此参数仅预览）")
    p_ext.add_argument("--verbose", action="store_true",
                       help="逐条打印每个文件的移动明细（默认只打汇总与进度）")
    p_ext.add_argument("--rules", default="extsort,skip_incomplete",
                       help="启用的规则 id（逗号分隔，可组合嵌套目录）："
                            "extsort按后缀 / skip_incomplete跳过未完成文件 / category按大类 / "
                            "booksort图书漫画按书名类型归类（漫画/小说/历史/医学…，非图书不动）/ "
                            "bookonline图书联网补全（需与 booksort 同用，只补「其它图书」那部分）"
                            " / by_date按日期 / by_size按大小 / regex_match正则筛选 / "
                            "cleanup_empty清理空目录。默认 extsort,skip_incomplete")
    p_ext.add_argument("--regex-pattern", default=None,
                       help="启用 regex_match 时的正则表达式：只整理文件名匹配的文件")
    p_ext.add_argument("--online-provider", default=None, choices=["auto", "dangdang", "llm"],
                       help="联网补全（bookonline）的数据源，覆盖 config.json 的 online.provider："
                            "auto=先 llm（配了 key 才用）再 dangdang / dangdang=当当分类（免费，默认）/ "
                            "llm=OpenAI 兼容接口（准确率最高，需在 config 里配 api_key）")
    p_ext.add_argument("--online-limit", type=int, default=None,
                       help="联网补全单次最多查询多少个书名（默认取 config 的 online.limit，0=不限）")
    p_ext.add_argument("--online-refresh", action="store_true",
                       help="忽略本地缓存 data/online_cache.json，强制重新联网查询")

    args = parser.parse_args()
    # 统一兜底：任何未预期异常都在日志里留下醒目标记 + 完整堆栈，
    # 避免 Web 端看到的只是"日志突然断了"而无从下手。
    try:
        if args.command == "check":
            cmd_check(args)
        elif args.command == "scan":
            cmd_scan(args)
        elif args.command == "run":
            cmd_run(args)
        elif args.command == "extsort":
            cmd_extsort(args)
    except KeyboardInterrupt:
        print("\n[中断] 已收到 Ctrl+C，任务中止。已完成的移动不会重复执行，"
              "重跑同一条命令即可从断点续跑。", flush=True)
        sys.exit(130)
    except Exception:
        log("[异常] 任务因未预期错误中止，完整堆栈如下（可整段复制反馈）：")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
