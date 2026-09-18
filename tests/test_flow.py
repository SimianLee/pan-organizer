# -*- coding: utf-8 -*-
"""
pan-organizer 集成测试：本地起一个模拟 alist WebDAV 的内存假服务，
用真实 CLI 跑 check / scan / run --apply，断言移动、冲突改名、目录创建、
递归深度、目标目录排除等行为全部正确。

运行：python tests/test_flow.py   （零依赖）
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.sax.saxutils as sax
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAN_ORGANIZER = os.path.join(ROOT, "app", "pan_organizer.py")  # v1.2 起主程序在 app/ 下
LM = "Thu, 01 Jan 2026 00:00:00 GMT"


# ---------------------------------------------------------------------------
# 内存文件树
# ---------------------------------------------------------------------------
class MockTree:
    def __init__(self):
        self.nodes = {"/": {"is_dir": True, "size": 0}}  # path -> node

    def _ensure_dirs(self, path):
        """确保 path 的所有父目录存在"""
        segs = [s for s in path.split("/") if s]
        cur = ""
        for s in segs[:-1]:
            cur += "/" + s
            if cur not in self.nodes:
                self.nodes[cur] = {"is_dir": True, "size": 0}

    def add_file(self, path, size=1024, mtime=None):
        self._ensure_dirs(path)
        self.nodes[path] = {"is_dir": False, "size": size, "mtime": mtime}

    def add_dir(self, path, mtime=None):
        self._ensure_dirs(path)
        self.nodes[path] = {"is_dir": True, "size": 0, "mtime": mtime}

    def children(self, path):
        prefix = path if path.endswith("/") else path + "/"
        out = []
        for p, node in sorted(self.nodes.items()):
            if p == path or not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" in rest:
                continue
            out.append((p, node))
        return out

    def move(self, src, dst):
        self.nodes[dst] = self.nodes.pop(src)

    def delete(self, path):
        """删除节点及其全部子孙（目录递归删除）"""
        prefix = path if path.endswith("/") else path + "/"
        gone = [p for p in list(self.nodes) if p == path or p.startswith(prefix)]
        for p in gone:
            del self.nodes[p]
        return len(gone) > 0


# ---------------------------------------------------------------------------
# 假 alist WebDAV Server
# ---------------------------------------------------------------------------
def strip_dav(path):
    """URL 路径 → 网盘路径（去掉 /dav 前缀）"""
    if path.startswith("/dav"):
        path = path[len("/dav"):]
    path = urllib.parse.unquote(path)
    if not path:
        path = "/"
    return path


def xml_escape(s):
    return sax.escape(str(s), {'"': "&quot;"})


class MockHandler(BaseHTTPRequestHandler):
    tree = None
    conflict_status = 412   # 目标已存在同名时的返回码（默认 412；置 500 模拟 alist/百度行为）
    move_flaky = False      # True 时所有 MOVE 一律返回 500（模拟服务端瞬时故障）
    overwrite_unsupported = False  # True 时模拟"后端不支持覆盖式移动"（alist + 百度网盘）：
                                   # 目标存在时即使 Overwrite: T 也返回 500（errno=12）
    fail_move_from = ()            # src 命中其中任一子串时 MOVE 返回 500（模拟特定移动失败）

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/xml; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- PROPFIND -----------------------------------------------------------
    def do_PROPFIND(self):
        p = strip_dav(urllib.parse.urlparse(self.path).path)
        if p not in self.tree.nodes:
            self._send(404, "")
            return
        depth = self.headers.get("Depth", "0")
        items = [(p, self.tree.nodes[p])]
        if depth == "1":
            items += self.tree.children(p)

        body = ['<?xml version="1.0" encoding="utf-8"?>', '<d:multistatus xmlns:d="DAV:">']
        for item_path, node in items:
            # 真实 alist 的 href 带 /dav 前缀（如 /dav/我的网盘/xxx），
            # 且 Depth:1 响应包含目录自身那条（items[0]）——按真实行为模拟，
            # 否则"跳过自身/剥前缀"的解析逻辑测不到真场景
            # （2026-09-18 曾因 href 无前缀漏测：幻影同名子目录 bug 上线）。
            href = "/dav" + urllib.parse.quote(item_path, safe="/")
            coll = "<d:collection/>" if node["is_dir"] else ""
            length = "" if node["is_dir"] else str(node["size"])
            name = item_path.rstrip("/").rsplit("/", 1)[-1]
            # 节点带 mtime 时按节点返回真实日期，否则用固定 LM 兜底
            lm = node.get("mtime")
            lm_str = (time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(lm))
                      if lm else LM)
            body.append(
                f'<d:response><d:href>{xml_escape(href)}</d:href>'
                f'<d:propstat><d:status>HTTP/1.1 200 OK</d:status>'
                f'<d:prop><d:resourcetype>{coll}</d:resourcetype>'
                f'<d:getcontentlength>{length}</d:getcontentlength>'
                f'<d:getlastmodified>{lm_str}</d:getlastmodified>'
                f'<d:displayname>{xml_escape(name)}</d:displayname>'
                f'</d:prop></d:propstat></d:response>'
            )
        body.append("</d:multistatus>")
        self._send(207, "".join(body))

    # -- MOVE ---------------------------------------------------------------
    def do_MOVE(self):
        src = strip_dav(urllib.parse.urlparse(self.path).path)
        dest_raw = self.headers.get("Destination", "")
        dst = strip_dav(urllib.parse.urlparse(dest_raw).path)
        overwrite = self.headers.get("Overwrite", "T").upper() == "T"
        if src not in self.tree.nodes:
            self._send(404, "")
            return
        if dst in self.tree.nodes and not overwrite:
            self._send(MockHandler.conflict_status, "")
            return
        if dst in self.tree.nodes and MockHandler.overwrite_unsupported:
            # 模拟 alist + 百度网盘：即使声明要覆盖，服务端也不支持（errno=12 → 500）
            self._send(500, "")
            return
        if MockHandler.move_flaky:
            self._send(500, "")
            return
        if any(s in src for s in MockHandler.fail_move_from):
            self._send(500, "")
            return
        self.tree.move(src, dst)
        self._send(201, "")

    # -- MKCOL --------------------------------------------------------------
    def do_MKCOL(self):
        p = strip_dav(urllib.parse.urlparse(self.path).path)
        if p in self.tree.nodes:
            self._send(405, "")
            return
        parent = p.rsplit("/", 1)[0] or "/"
        if parent not in self.tree.nodes:
            self._send(409, "")
            return
        self.tree.add_dir(p)
        self._send(201, "")

    # -- DELETE -------------------------------------------------------------
    def do_DELETE(self):
        p = strip_dav(urllib.parse.urlparse(self.path).path)
        if p not in self.tree.nodes:
            self._send(404, "")
            return
        if self.tree.nodes[p]["is_dir"] and self.tree.children(p):
            self._send(409, "")   # 非空目录不允许删（RFC 4918）
            return
        self.tree.delete(p)
        self._send(204, "")


def start_mock():
    tree = MockTree()
    # 挂载点 + 待整理目录 + 预置一个"视频"目标目录（制造冲突改名场景）
    tree.add_dir("/我的网盘")
    tree.add_dir("/我的网盘/下载")
    tree.add_dir("/我的网盘/下载/sub")
    tree.add_dir("/我的网盘/视频")
    # 待整理散文件
    tree.add_file("/我的网盘/下载/电影A.mp4", 524_288_000)
    tree.add_file("/我的网盘/下载/B.MKV", 1_073_741_824)
    tree.add_file("/我的网盘/下载/照片1.JPG", 3_145_728)
    tree.add_file("/我的网盘/下载/年度报告.pdf", 2_097_152)
    tree.add_file("/我的网盘/下载/notes.txt", 1024)
    tree.add_file("/我的网盘/下载/素材.zip", 83_886_080)
    tree.add_file("/我的网盘/下载/背景音乐.mp3", 10_485_760)
    tree.add_file("/我的网盘/下载/临时表.xlsx", 2_097_152)
    # 子目录文件（测递归）
    tree.add_file("/我的网盘/下载/sub/内层视频.mp4", 314_572_800)
    # 预置同名文件（测冲突 rename）
    tree.add_file("/我的网盘/视频/电影A.mp4", 1)

    MockHandler.tree = tree
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, tree, port


# ---------------------------------------------------------------------------
# 假"当当"站点：给图书联网补全（bookonline）做端到端测试用
# ---------------------------------------------------------------------------
# 真实站点结构照抄：搜索页给一个商品链接，商品页有面包屑「图书 > 小说 > 社会小说」。
# 故意用 GBK 编码返回，顺带验证解码路径（真实当当就是 GBK）。
_DD_SEARCH = ('<html><body><div class="list">'
              '<a href="http://product.dangdang.com/29311943.html">活着</a>'
              '</div></body></html>')
_DD_PRODUCT = (
    '<html><head>'
    '<title>《活着》余华 著【简介_书评_在线阅读】 - 当当图书</title>'
    '</head><body>'
    '<div class="product_wrapper">'
    '<!-- 面包屑 begin -->'
    '<div class="breadcrumb" id="breadcrumb" dd_name="顶部面包屑导航">'
    "<a href='http://book.dangdang.com/' name='__Breadcrumb_pub'><b>图书</b></a>"
    '<span class="gt">&gt;</span>'
    "<a href='http://category.dangdang.com/cp01.03.00.00.00.00.html'>小说</a>"
    '<span class="gt">&gt;</span>'
    "<a href='http://category.dangdang.com/cp01.03.45.00.00.00.html'>社会小说</a>"
    '<span class="gt">&gt;</span><span>活着（余华代表作）</span>'
    '<div class="outlets" style="display:none" id="bread-crumb-outlets">'
    '<a class="o_icon" href="http://v.dangdang.com/" title="尾品汇">尾品汇</a>'
    '</div></div></div></body></html>')


class FakeDangdangHandler(BaseHTTPRequestHandler):
    requests = []          # 记录所有请求路径，用来断言"只查了该查的"

    def log_message(self, *a):        # 静音
        pass

    def do_GET(self):
        FakeDangdangHandler.requests.append(self.path)
        if self.path.startswith("/search"):
            body = _DD_SEARCH
        elif self.path.startswith("/product/"):
            body = _DD_PRODUCT
        else:
            body = "<html><body>404</body></html>"
        raw = body.encode("gbk")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=gbk")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake_dangdang():
    FakeDangdangHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDangdangHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
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


def run_cli(args, workdir):
    """以子进程运行 pan_organizer.py，返回 (exit_code, stdout)"""
    r = subprocess.run(
        [sys.executable, PAN_ORGANIZER] + args,
        capture_output=True, text=True, encoding="utf-8",
        cwd=workdir, timeout=60,
    )
    return r.returncode, r.stdout + r.stderr


def build_rules():
    """基于 examples/rules.example.json 生成 /我的网盘 前缀的规则"""
    with open(os.path.join(ROOT, "examples", "rules.example.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    for r in data["rules"]:
        r["target"] = r["target"].replace("/百度网盘", "/我的网盘")
    return data


def main():
    server, tree, port = start_mock()
    dd_server, dd_port = start_fake_dangdang()
    base_url = f"http://127.0.0.1:{port}/dav"

    tmp = os.path.join(ROOT, "tests", "_tmp")
    os.makedirs(tmp, exist_ok=True)
    config_path = os.path.join(tmp, "config.json")
    rules_path = os.path.join(tmp, "rules.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"alist": {"base_url": base_url, "username": "u", "password": "p", "timeout": 10},
                   "options": {"on_conflict": "rename"}}, f, ensure_ascii=False, indent=2)
    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(build_rules(), f, ensure_ascii=False, indent=2)

    print("== 0) list_dir：href 带 /dav 前缀时也不出现幻影自身条目 ==")
    cfg_for_probe = json.load(open(config_path, encoding="utf-8"))
    sys.path.insert(0, os.path.join(ROOT, "app"))
    import pan_organizer as _po
    _client = _po.WebDAVClient(cfg_for_probe["alist"]["base_url"], "u", "p", timeout=10)
    _entries = _client.list_dir("/")
    check("根目录列表不含幻影 / 自身",
          "/" not in [e.path for e in _entries], str([e.path for e in _entries]))
    _entries2 = _client.list_dir("/我的网盘")
    check("子目录列表不含幻影自身",
          "/我的网盘" not in [e.path for e in _entries2], str([e.path for e in _entries2]))
    check("子目录真实子夹仍可见",
          any(e.path == "/我的网盘/下载" for e in _entries2), str([e.path for e in _entries2]))

    print("== 1) check：测试连接与挂载点列表 ==")
    code, out = run_cli(["check", "--config", config_path], ROOT)
    check("check 退出码 0", code == 0, out)
    check("列出挂载点 /我的网盘", "/我的网盘" in out, out)
    check("输出连接成功", "连接成功" in out, out)

    print("\n== 2) scan depth=0：预览应命中 8 个文件，预编号 电影A (1).mp4 ==")
    code, out = run_cli(["scan", "--config", config_path, "--rules", rules_path,
                         "--path", "/我的网盘/下载", "--depth", "0"], ROOT)
    check("scan 退出码 0", code == 0, out)
    check("命中 8 个文件", "命中规则 8 个" in out, out)
    check("同名自动编号：电影A (1).mp4（目标已有同名）",
          "同名自动编号" in out and "电影A (1).mp4" in out, out)
    check("子目录文件未被扫入", "内层视频" not in out, out)
    check("预览未执行移动", "/我的网盘/下载/电影A.mp4" in out, out)  # 计划里显示源路径，说明文件还在

    print("\n== 3) run --apply depth=0：真正执行 ==")
    code, out = run_cli(["run", "--config", config_path, "--rules", rules_path,
                         "--path", "/我的网盘/下载", "--depth", "0", "--apply"], ROOT)
    check("run 退出码 0", code == 0, out)
    check("成功移动 8 个", "成功移动 8 个" in out, out)
    check("冲突文件改名为 电影A (1).mp4", "电影A (1).mp4" in out, out)

    # 校验树状态
    check("视频目录收到 电影A (1).mp4", "/我的网盘/视频/电影A (1).mp4" in tree.nodes, str(tree.nodes.keys()))
    check("原预置 电影A.mp4 未被覆盖", "/我的网盘/视频/电影A.mp4" in tree.nodes)
    check("download 目录已清空散文件",
          not [p for p in tree.nodes if p.startswith("/我的网盘/下载/") and not p.startswith("/我的网盘/下载/sub")],
          str(tree.nodes.keys()))
    check("自动创建 图片/文档/压缩包/音频 目录",
          all(d in tree.nodes for d in ["/我的网盘/图片", "/我的网盘/文档", "/我的网盘/压缩包", "/我的网盘/音频"]))
    check("子目录文件未被移动", "/我的网盘/下载/sub/内层视频.mp4" in tree.nodes)

    print("\n== 4) 幂等：再跑一次不应有任何操作 ==")
    code, out = run_cli(["run", "--config", config_path, "--rules", rules_path,
                         "--path", "/我的网盘/下载", "--depth", "0", "--apply"], ROOT)
    check("第二次无操作", "没有需要执行的操作" in out, out)

    print("\n== 5) depth=-1 递归：整理子目录中的视频 ==")
    code, out = run_cli(["run", "--config", config_path, "--rules", rules_path,
                         "--path", "/我的网盘", "--depth", "-1", "--apply"], ROOT)
    check("run 递归退出码 0", code == 0, out)
    check("子目录视频被移动", "/我的网盘/视频/内层视频.mp4" in tree.nodes, out)
    check("目标目录内文件未被二次搬动（已在正确位置）", "/我的网盘/视频/电影A (1).mp4" in tree.nodes, out)
    check("图片目录内文件未被二次搬动", "/我的网盘/图片/照片1.JPG" in tree.nodes, out)
    check("download 目录已完全清空（无剩余文件）",
          not [p for p in tree.nodes if p.startswith("/我的网盘/下载/") and not tree.nodes[p]["is_dir"]],
          str(tree.nodes.keys()))

    print("\n== 6) run 不带 --apply 是预览，不应移动 ==")
    tree.add_file("/我的网盘/下载/新片.mkv", 999)
    code, out = run_cli(["run", "--config", config_path, "--rules", rules_path,
                         "--path", "/我的网盘/下载", "--depth", "0"], ROOT)
    check("预览退出码 0", code == 0, out)
    check("提示需加 --apply", "--apply" in out, out)
    check("文件未被移动", "/我的网盘/下载/新片.mkv" in tree.nodes, out)

    print("\n== 7) 路径不存在时给出友好错误 ==")
    code, out = run_cli(["scan", "--config", config_path, "--rules", rules_path,
                         "--path", "/不存在的目录"], ROOT)
    check("不存在目录提示", "目录不存在" in out and code == 2, out)

    # ================= extsort 自动按后缀归档 =================
    print("\n== 8) extsort 预览：dry-run 不应移动任何文件 ==")
    tree.add_dir("/我的网盘/混合区")
    tree.add_dir("/我的网盘/混合区/子夹")
    tree.add_file("/我的网盘/混合区/clip1.mp4", 1_000_000)
    tree.add_file("/我的网盘/混合区/CLIP2.MP4", 2_000_000)     # 大写后缀应归一为 mp4
    tree.add_file("/我的网盘/混合区/同.mp4", 500_000)          # 与子夹内同名 → 冲突改名
    tree.add_file("/我的网盘/混合区/doc1.pdf", 300_000)
    tree.add_file("/我的网盘/混合区/img1.jpg", 100_000)
    tree.add_file("/我的网盘/混合区/note", 50)                # 无后缀 → noext/
    tree.add_file("/我的网盘/混合区/未下完.part", 999)         # 默认跳过后缀
    tree.add_file("/我的网盘/混合区/子夹/内层.mkv", 400_000)
    tree.add_file("/我的网盘/混合区/子夹/同.mp4", 600_000)     # 与根目录同名 → rename
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/混合区", "--dest", "/我的网盘/归档"], ROOT)
    check("extsort 预览退出码 0", code == 0, out)
    check("预览提示加 --apply", "--apply" in out, out)
    check("汇总包含 .mp4 后缀行", ".mp4" in out and "→  /我的网盘/归档/mp4" in out, out)
    check("汇总提示内部同名改名", "同名" in out, out)
    check("预览未移动文件", "/我的网盘/混合区/clip1.mp4" in tree.nodes, str(tree.nodes.keys()))

    print("\n== 9) extsort --apply：真正按后缀归档 ==")
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/混合区", "--dest", "/我的网盘/归档", "--apply"], ROOT)
    check("extsort 执行退出码 0", code == 0, out)
    check("成功移动 8 个（part 被默认跳过）", "成功移动 8 个" in out, out)
    check("进度行带百分比与完成计数", "已完成 100% (8/8)" in out, out)
    check("进度行统计成功/失败/跳过", "成功 8，失败 0，跳过 0" in out, out)
    # 归档结果校验
    check("mp4 收 clip1", "/我的网盘/归档/mp4/clip1.mp4" in tree.nodes)
    check("大写后缀归一 mp4", "/我的网盘/归档/mp4/CLIP2.MP4" in tree.nodes)
    check("同.mp4 冲突改名 (1)", "/我的网盘/归档/mp4/同.mp4" in tree.nodes
          and "/我的网盘/归档/mp4/同 (1).mp4" in tree.nodes, str(tree.nodes.keys()))
    check("mkv 收子目录文件", "/我的网盘/归档/mkv/内层.mkv" in tree.nodes)
    check("pdf/jpg 归档", "/我的网盘/归档/pdf/doc1.pdf" in tree.nodes
          and "/我的网盘/归档/jpg/img1.jpg" in tree.nodes)
    check("无后缀归 noext", "/我的网盘/归档/noext/note" in tree.nodes)
    check("part 未移动（默认跳过）", "/我的网盘/混合区/未下完.part" in tree.nodes)
    check("源目录仅剩跳过项",
          not [p for p in tree.nodes
               if p.startswith("/我的网盘/混合区/") and p != "/我的网盘/混合区/未下完.part"
               and not tree.nodes[p]["is_dir"]],
          str(tree.nodes.keys()))

    print("\n== 10) extsort 幂等：再跑一次应无操作 ==")
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/混合区", "--dest", "/我的网盘/归档", "--apply"], ROOT)
    check("第二次无操作", "没有需要执行的操作" in out, out)

    print("\n== 11) extsort dest 缺省 = 扫描目录：原地按后缀归档 ==")
    tree.add_dir("/我的网盘/就地整理")
    tree.add_file("/我的网盘/就地整理/a.png", 10_000)
    tree.add_file("/我的网盘/就地整理/b.mp3", 20_000)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/就地整理", "--apply"], ROOT)
    check("原地归档退出码 0", code == 0, out)
    check("原地成功移动 2 个", "成功移动 2 个" in out, out)
    check("png/mp3 在扫描目录内的后缀子夹",
          "/我的网盘/就地整理/png/a.png" in tree.nodes
          and "/我的网盘/就地整理/mp3/b.mp3" in tree.nodes, str(tree.nodes.keys()))

    print("\n== 12) 重名预编号：目标已有同名及 (1) 占位时，从 (2) 起按序分配 ==")
    tree.add_dir("/我的网盘/重灾区")
    tree.add_dir("/我的网盘/重灾区/d1")
    tree.add_dir("/我的网盘/重灾区/d2")
    tree.add_file("/我的网盘/重灾区/d1/同名.mp4", 1_000)   # 与 d2 同名 → 撞车
    tree.add_file("/我的网盘/重灾区/d2/同名.mp4", 2_000)
    tree.add_file("/我的网盘/重灾区/d1/独家.pdf", 3_000)    # 唯一名 → 保原名
    # 目标目录已存在 同名.mp4 与 同名 (1).mp4（模拟之前已整理过）
    tree.add_dir("/我的网盘/归档x")
    tree.add_dir("/我的网盘/归档x/mp4")
    tree.add_file("/我的网盘/归档x/mp4/同名.mp4", 10)
    tree.add_file("/我的网盘/归档x/mp4/同名 (1).mp4", 20)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/重灾区", "--dest", "/我的网盘/归档x"], ROOT)
    check("预览退出码 0", code == 0, out)
    check("预览直接显示编号落点 (2)", "同名 (2).mp4" in out, out)
    check("预览汇总 2 个同名自动编号", "2 个文件因同名已自动按序编号" in out, out)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/重灾区", "--dest", "/我的网盘/归档x", "--apply"], ROOT)
    check("执行退出码 0", code == 0, out)
    check("成功移动 3 个", "成功移动 3 个" in out, out)
    check("两同名文件编为 (2)(3)，未占用 (1)",
          "/我的网盘/归档x/mp4/同名 (2).mp4" in tree.nodes
          and "/我的网盘/归档x/mp4/同名 (3).mp4" in tree.nodes, str(tree.nodes.keys()))
    check("目标目录原有文件未被覆盖",
          tree.nodes["/我的网盘/归档x/mp4/同名.mp4"]["size"] == 10
          and tree.nodes["/我的网盘/归档x/mp4/同名 (1).mp4"]["size"] == 20)
    check("唯一名文件保留原名", "/我的网盘/归档x/pdf/独家.pdf" in tree.nodes)
    check("源目录已清空",
          not [p for p in tree.nodes if p.startswith("/我的网盘/重灾区/")
               and not tree.nodes[p]["is_dir"]], str(tree.nodes.keys()))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/重灾区", "--dest", "/我的网盘/归档x", "--apply"], ROOT)
    check("重跑幂等无操作", "没有需要执行的操作" in out, out)

    # ============ 撞名兜底（方案 A）：撞名被服务端报成 500 也能自动改名 ============
    print("\n== 13) MOVE 撞名被报成 500：exists 探测命中 → 自动改名 (1) ==")
    sys.path.insert(0, os.path.join(ROOT, "app"))  # v1.2 起主程序在 app/ 下
    import pan_organizer
    _client = pan_organizer.WebDAVClient(base_url, "u", "p", timeout=10)
    tree.add_dir("/我的网盘/兜底区")
    tree.add_file("/我的网盘/兜底区/撞名.jpg", 5000)
    tree.add_dir("/我的网盘/兜底目标")
    tree.add_file("/我的网盘/兜底目标/撞名.jpg", 1)     # 目标已有同名（存量）
    MockHandler.conflict_status = 500                   # 撞名却返回 500（模拟 alist/百度）
    st, dt, rn = pan_organizer._move_one(
        _client, "/我的网盘/兜底区/撞名.jpg", "/我的网盘/兜底目标/撞名.jpg", "rename")
    MockHandler.conflict_status = 412
    check("撞名500 → 自动改名 (1) 成功", st == "ok" and rn and "撞名 (1).jpg" in dt, f"{st} | {dt}")
    check("改名文件已落位", "/我的网盘/兜底目标/撞名 (1).jpg" in tree.nodes, str(tree.nodes.keys()))
    check("原存量同名文件未被覆盖", tree.nodes["/我的网盘/兜底目标/撞名.jpg"]["size"] == 1)

    print("\n== 14) MOVE 无条件 500 且目标无同名：判瞬时故障，不误改名 ==")
    tree.add_file("/我的网盘/兜底区/瞬断.jpg", 5000)
    MockHandler.move_flaky = True                       # 所有 MOVE 一律 500
    st, dt, rn = pan_organizer._move_one(
        _client, "/我的网盘/兜底区/瞬断.jpg", "/我的网盘/兜底目标/瞬断.jpg", "rename")
    MockHandler.move_flaky = False
    check("瞬时500 → 判失败不误改名（探测确认无同名 或 探测抖动无法确认 都合法）",
          st == "fail" and "稍后重跑即可" in dt, f"{st} | {dt}")
    check("源文件原地未动", "/我的网盘/兜底区/瞬断.jpg" in tree.nodes)
    check("目标未产生 (1) 副本", "/我的网盘/兜底目标/瞬断 (1).jpg" not in tree.nodes)

    # ============ 计划导出（--plan）→ 按计划执行（--from-plan）：跳过重复扫描 ============
    print("\n== 15) 查询导出计划 --plan → --from-plan 执行 → 重跑幂等 ==")
    plan15 = os.path.join(tmp, "plan15.json")
    tree.add_dir("/我的网盘/下载2")
    tree.add_file("/我的网盘/下载2/计划甲.txt", 100)
    tree.add_file("/我的网盘/下载2/计划乙.mp4", 200)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/下载2", "--dest", "/我的网盘/归档2",
                         "--plan", plan15], ROOT)
    check("查询(预览)导出计划 退出码 0", code == 0, out)
    check("输出提示计划已导出", "计划已导出" in out, out)
    check("计划文件已落盘", os.path.exists(plan15), "")
    check("预览未移动任何文件", "/我的网盘/下载2/计划甲.txt" in tree.nodes
          and "/我的网盘/归档2/txt/计划甲.txt" not in tree.nodes, str(tree.nodes.keys()))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--from-plan", plan15], ROOT)
    check("from-plan dry-run 提示加 --apply", code == 0
          and "--from-plan" in out and "dry-run" in out, out)
    check("dry-run 未执行移动", "/我的网盘/下载2/计划甲.txt" in tree.nodes,
          str(tree.nodes.keys()))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--from-plan", plan15, "--apply"], ROOT)
    check("from-plan 执行退出码 0", code == 0, out)
    check("成功移动 2 个", "成功移动 2 个" in out, out)
    check("文件按后缀落位", "/我的网盘/归档2/txt/计划甲.txt" in tree.nodes
          and "/我的网盘/归档2/mp4/计划乙.mp4" in tree.nodes, str(tree.nodes.keys()))
    check("源目录已清空", "/我的网盘/下载2/计划甲.txt" not in tree.nodes
          and "/我的网盘/下载2/计划乙.mp4" not in tree.nodes,
          str(tree.nodes.keys()))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--from-plan", plan15, "--apply"], ROOT)
    check("重跑幂等：源已移走 → 404 计跳过 2", code == 0
          and "成功移动 0 个" in out and "跳过 2 个" in out, out)

    # ============ v2 规则：嵌套目录 / 正则筛选 / 清理空目录 ============
    print("\n== 16) v2 规则：category + by_date + by_size 嵌套目录归档 ==")
    MB2, GB2 = 1024 * 1024, 1024 ** 3
    def mt(y, mo, d):
        # 中午 12 点本地时间 → 跨 UTC 换算仍在同一天/同月，避免月末边界误判
        return int(time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1)))
    tree.add_dir("/我的网盘/v2a区")
    tree.add_file("/我的网盘/v2a区/老照片.JPG", 3 * MB2, mt(2025, 7, 15))
    tree.add_file("/我的网盘/v2a区/新视频.mkv", 2 * GB2, mt(2026, 1, 10))
    tree.add_file("/我的网盘/v2a区/文档甲.pdf", 30 * MB2, mt(2025, 11, 20))
    tree.add_file("/我的网盘/v2a区/脚本.py", 1024, mt(2026, 2, 5))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/v2a区", "--dest", "/我的网盘/v2a档",
                         "--rules", "category,by_date,by_size", "--apply"], ROOT)
    check("v2a 执行退出码 0", code == 0, out)
    check("成功移动 4 个", "成功移动 4 个" in out, out)
    check("JPG → 图片/2025-07/小于10MB/", "/我的网盘/v2a档/图片/2025-07/小于10MB/老照片.JPG" in tree.nodes,
          str(tree.nodes.keys()))
    check("MKV → 视频/2026-01/大于1GB/", "/我的网盘/v2a档/视频/2026-01/大于1GB/新视频.mkv" in tree.nodes,
          str(tree.nodes.keys()))
    check("PDF → 文档/2025-11/10-100MB/", "/我的网盘/v2a档/文档/2025-11/10-100MB/文档甲.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("PY → 代码/2026-02/小于10MB/", "/我的网盘/v2a档/代码/2026-02/小于10MB/脚本.py" in tree.nodes,
          str(tree.nodes.keys()))
    check("源目录散文件已清空",
          not [p for p in tree.nodes if p.startswith("/我的网盘/v2a区/")
               and not tree.nodes[p]["is_dir"]], str(tree.nodes.keys()))

    print("\n== 17) v2 规则：regex_match 只整理文件名匹配正则的文件 ==")
    tree.add_dir("/我的网盘/v2b区")
    tree.add_file("/我的网盘/v2b区/年度报告_2026.pdf", 2048)
    tree.add_file("/我的网盘/v2b区/随手笔记.txt", 512)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/v2b区", "--dest", "/我的网盘/v2b档",
                         "--rules", "extsort,regex_match", "--regex-pattern", "2026",
                         "--apply"], ROOT)
    check("v2b 执行退出码 0", code == 0, out)
    check("成功移动 1 个（只搬匹配 2026 的）", "成功移动 1 个" in out, out)
    check("匹配文件已落位 pdf/ 下", "/我的网盘/v2b档/pdf/年度报告_2026.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("不匹配的 随手笔记.txt 原地保留", "/我的网盘/v2b区/随手笔记.txt" in tree.nodes,
          str(tree.nodes.keys()))

    print("\n== 18) v2 规则：cleanup_empty 只删空目录，保留有未完成文件的目录 ==")
    tree.add_dir("/我的网盘/v2c区")
    tree.add_dir("/我的网盘/v2c区/空夹")
    tree.add_dir("/我的网盘/v2c区/进行中")
    tree.add_file("/我的网盘/v2c区/进行中/大电影.tmp", 888)  # 默认跳过后缀 → 不会被搬走 → 目录保持非空
    tree.add_file("/我的网盘/v2c区/已整.txt", 100)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/v2c区", "--dest", "/我的网盘/v2c档",
                         "--apply"], ROOT)
    check("v2c 第一步：移动退出码 0", code == 0 and "成功移动 1 个" in out, out)
    check("已整.txt 已落位 txt/ 下", "/我的网盘/v2c档/txt/已整.txt" in tree.nodes,
          str(tree.nodes.keys()))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/v2c区", "--dest", "/我的网盘/v2c档",
                         "--rules", "extsort,skip_incomplete,cleanup_empty", "--apply"], ROOT)
    check("v2c 第二步：清理退出码 0", code == 0, out)
    check("空夹 已被删除", "/我的网盘/v2c区/空夹" not in tree.nodes, str(tree.nodes.keys()))
    check("有 .tmp 的「进行中」目录被保留",
          "/我的网盘/v2c区/进行中" in tree.nodes
          and "/我的网盘/v2c区/进行中/大电影.tmp" in tree.nodes, str(tree.nodes.keys()))
    check("归档目标区 v2c档 不被清理", "/我的网盘/v2c档" in tree.nodes, str(tree.nodes.keys()))

    print("\n== 19) skip_incomplete 是真实开关：取消后 .tmp 也会被归档 ==")
    tree.add_dir("/我的网盘/v2d区")
    tree.add_file("/我的网盘/v2d区/半成品.tmp", 777)
    tree.add_file("/我的网盘/v2d区/成品.txt", 50)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/v2d区", "--dest", "/我的网盘/v2d档",
                         "--rules", "extsort", "--apply"], ROOT)
    check("v2d 执行退出码 0", code == 0, out)
    check("成功移动 2 个（.tmp 不再被默认跳过）", "成功移动 2 个" in out, out)
    check("半成品.tmp 被归档到 tmp/", "/我的网盘/v2d档/tmp/半成品.tmp" in tree.nodes,
          str(tree.nodes.keys()))
    check("成品.txt 被归档到 txt/", "/我的网盘/v2d档/txt/成品.txt" in tree.nodes,
          str(tree.nodes.keys()))

    # ============ overwrite 策略：后端不支持覆盖式 MOVE 时的备份式覆盖 ============
    print("\n== 20) overwrite + 后端不支持覆盖（errno=12→500）：备份式覆盖成功 ==")
    tree.add_dir("/我的网盘/ow区")
    tree.add_file("/我的网盘/ow区/封面.jpg", 5000)          # 源（新内容）
    tree.add_dir("/我的网盘/ow目标")
    tree.add_file("/我的网盘/ow目标/封面.jpg", 1)           # 目标已有同名（旧内容）
    MockHandler.overwrite_unsupported = True                # 模拟 alist+百度：Overwrite:T 也 500
    st, dt, rn = pan_organizer._move_one(
        _client, "/我的网盘/ow区/封面.jpg", "/我的网盘/ow目标/封面.jpg", "overwrite")
    MockHandler.overwrite_unsupported = False
    check("覆盖判定成功", st == "ok", f"{st} | {dt}")
    check("目标已被源覆盖（大小换成源的 5000）",
          tree.nodes.get("/我的网盘/ow目标/封面.jpg", {}).get("size") == 5000,
          str(tree.nodes.get("/我的网盘/ow目标/封面.jpg")))
    check("源文件已移走", "/我的网盘/ow区/封面.jpg" not in tree.nodes,
          str(tree.nodes.keys()))
    check("无 __bak_ 残留", not [p for p in tree.nodes if ".__bak_" in p],
          str(list(tree.nodes.keys())))

    print("\n== 21) overwrite 备份式覆盖中途失败：自动回滚，零丢失 ==")
    tree.add_dir("/我的网盘/ow2区")
    tree.add_file("/我的网盘/ow2区/正文.pdf", 8000)          # 源
    tree.add_dir("/我的网盘/ow2目标")
    tree.add_file("/我的网盘/ow2目标/正文.pdf", 2)           # 目标已有同名
    MockHandler.overwrite_unsupported = True
    # 只让"源 → 目标"这一步失败（让位成功、搬源失败），验证回滚路径
    MockHandler.fail_move_from = ("/我的网盘/ow2区/正文.pdf",)
    st, dt, rn = pan_organizer._move_one(
        _client, "/我的网盘/ow2区/正文.pdf", "/我的网盘/ow2目标/正文.pdf", "overwrite")
    MockHandler.fail_move_from = ()
    MockHandler.overwrite_unsupported = False
    check("覆盖整体判失败", st == "fail", f"{st} | {dt}")
    check("提示已回滚", "已回滚" in dt, dt)
    check("源文件退回原位", "/我的网盘/ow2区/正文.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("目标文件恢复原样（大小仍是 2）",
          tree.nodes.get("/我的网盘/ow2目标/正文.pdf", {}).get("size") == 2,
          str(tree.nodes.get("/我的网盘/ow2目标/正文.pdf")))
    check("无 __bak_ 残留", not [p for p in tree.nodes if ".__bak_" in p],
          str(list(tree.nodes.keys())))

    print("\n== 22) 端到端：config on_conflict=overwrite 贯穿到执行层（后端不支持覆盖）==")
    tree.add_dir("/我的网盘/ow3区")
    tree.add_file("/我的网盘/ow3区/文件.pdf", 4000)          # 源（新内容）
    tree.add_dir("/我的网盘/ow3档")
    tree.add_file("/我的网盘/ow3档/pdf/文件.pdf", 3)         # 目标已有同名（extsort 落到 pdf/）
    cfg_ow = os.path.join(tmp, "config_overwrite.json")
    with open(cfg_ow, "w", encoding="utf-8") as f:
        json.dump({"alist": {"base_url": base_url, "username": "u",
                             "password": "p", "timeout": 10},
                   "options": {"on_conflict": "overwrite"}},
                  f, ensure_ascii=False, indent=2)
    MockHandler.overwrite_unsupported = True                # 模拟 alist+百度：不支持覆盖式移动
    code, out = run_cli(["extsort", "--config", cfg_ow,
                         "--path", "/我的网盘/ow3区", "--dest", "/我的网盘/ow3档",
                         "--apply"], ROOT)
    MockHandler.overwrite_unsupported = False
    check("端到端覆盖退出码 0", code == 0, out)
    check("成功移动 1 个", "成功移动 1 个" in out, out)
    check("目标已被源覆盖（大小 4000）",
          tree.nodes.get("/我的网盘/ow3档/pdf/文件.pdf", {}).get("size") == 4000,
          str(tree.nodes.get("/我的网盘/ow3档/pdf/文件.pdf")))
    check("源目录已清空", "/我的网盘/ow3区/文件.pdf" not in tree.nodes,
          str(tree.nodes.keys()))
    check("无 __bak_ 残留", not [p for p in tree.nodes if ".__bak_" in p],
          str(list(tree.nodes.keys())))

    # ============ v1.5 规则：booksort 图书/漫画按书名类型归类 ============
    print("\n== 23) booksort：图书按类型归类，非图书文件不动 ==")
    tree.add_dir("/我的网盘/书库")
    tree.add_file("/我的网盘/书库/海贼王第01卷.cbz", 50_000_000)          # 漫画后缀直判
    tree.add_file("/我的网盘/书库/火影忍者漫画全集.pdf", 200_000_000)      # 书名点明漫画
    tree.add_file("/我的网盘/书库/活着.pdf", 2_000_000)                   # 无类型线索 → 其它图书
    tree.add_file("/我的网盘/书库/Python编程从入门到实践.epub", 8_000_000) # 计算机IT
    tree.add_file("/我的网盘/书库/黄帝内经养生智慧.mobi", 3_000_000)       # 医学养生
    tree.add_file("/我的网盘/书库/中国历史百科全书10.pdf", 30_000_000)     # 历史传记
    tree.add_file("/我的网盘/书库/封面设计.jpg", 500_000)                 # 非图书 → 原地不动
    tree.add_file("/我的网盘/书库/主题曲.mp3", 4_000_000)                 # 非图书 → 原地不动
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/书库", "--dest", "/我的网盘/书档",
                         "--rules", "booksort,skip_incomplete", "--apply"], ROOT)
    check("booksort 执行退出码 0", code == 0, out)
    check("成功移动 6 个（图书6个 + 非图书2个不动）", "成功移动 6 个" in out, out)
    check("汇总提示非图书跳过 2 个", "非图书/漫画文件" in out and "2 个" in out, out)
    check("cbz → 漫画/", "/我的网盘/书档/漫画/海贼王第01卷.cbz" in tree.nodes,
          str(tree.nodes.keys()))
    check("书名点明漫画的 pdf → 漫画/", "/我的网盘/书档/漫画/火影忍者漫画全集.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("无类型线索 → 其它图书/", "/我的网盘/书档/其它图书/活着.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("Python → 计算机IT/", "/我的网盘/书档/计算机IT/Python编程从入门到实践.epub" in tree.nodes,
          str(tree.nodes.keys()))
    check("养生 → 医学养生/", "/我的网盘/书档/医学养生/黄帝内经养生智慧.mobi" in tree.nodes,
          str(tree.nodes.keys()))
    check("历史 → 历史传记/", "/我的网盘/书档/历史传记/中国历史百科全书10.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("非图书 jpg/mp3 原地保留", "/我的网盘/书库/封面设计.jpg" in tree.nodes
          and "/我的网盘/书库/主题曲.mp3" in tree.nodes, str(tree.nodes.keys()))

    print("\n== 24) booksort 幂等 + 撞名改名：重跑只处理新增 ==")
    tree.add_file("/我的网盘/书库/活着.pdf", 1_500_000)   # 与已归档同名 → rename 编号
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/书库", "--dest", "/我的网盘/书档",
                         "--rules", "booksort,skip_incomplete", "--apply"], ROOT)
    check("重跑退出码 0", code == 0, out)
    check("只移动新来的同名 1 个", "成功移动 1 个" in out, out)
    check("同名书自动编号 (1)", "/我的网盘/书档/其它图书/活着 (1).pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("原归档文件未被覆盖",
          tree.nodes["/我的网盘/书档/其它图书/活着.pdf"]["size"] == 2_000_000)
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/书库", "--dest", "/我的网盘/书档",
                         "--rules", "booksort,skip_incomplete", "--apply"], ROOT)
    check("再跑无操作（非图书不算待办）", "没有需要执行的操作" in out, out)

    print("\n== 25) booksort + by_date 嵌套：类型目录下再按月份分 ==")
    tree.add_dir("/我的网盘/书库2")
    tree.add_file("/我的网盘/书库2/投资理财入门.pdf", 1_000_000, mt(2026, 3, 8))
    tree.add_file("/我的网盘/书库2/英语语法入门.pdf", 2_000_000, mt(2026, 5, 20))
    code, out = run_cli(["extsort", "--config", config_path,
                         "--path", "/我的网盘/书库2", "--dest", "/我的网盘/书档2",
                         "--rules", "booksort,by_date", "--apply"], ROOT)
    check("嵌套执行退出码 0", code == 0, out)
    check("成功移动 2 个", "成功移动 2 个" in out, out)
    check("经济管理/2026-03/ 嵌套落位",
          "/我的网盘/书档2/经济管理/2026-03/投资理财入门.pdf" in tree.nodes,
          str(tree.nodes.keys()))
    check("外语学习/2026-05/ 嵌套落位",
          "/我的网盘/书档2/外语学习/2026-05/英语语法入门.pdf" in tree.nodes,
          str(tree.nodes.keys()))

    # ============ v1.6 图书联网补全（bookonline）：只查本地判不出的书 ============
    print("\n== 26) bookonline：本地判不出的书联网归类，能判出的一律不联网 ==")
    online_config_path = os.path.join(tmp, "config_online.json")
    with open(online_config_path, "w", encoding="utf-8") as f:
        json.dump({
            "alist": {"base_url": base_url, "username": "u", "password": "p",
                      "timeout": 10},
            "options": {"on_conflict": "rename"},
            "online": {
                "provider": "dangdang",
                "timeout": 5,
                "workers": 1,
                "delay": 0,
                "search_url": f"http://127.0.0.1:{dd_port}/search?key={{q}}&act=input",
                "product_url": f"http://127.0.0.1:{dd_port}/product/{{id}}.html",
            },
        }, f, ensure_ascii=False, indent=2)
    cache_path = os.path.join(tmp, "online_cache.json")
    if os.path.exists(cache_path):
        os.remove(cache_path)
    FakeDangdangHandler.requests = []

    tree.add_dir("/我的网盘/书库3")
    tree.add_file("/我的网盘/书库3/活着.pdf", 1_000_000)                 # 本地无线索 → 联网
    tree.add_file("/我的网盘/书库3/Python编程从入门到实践.epub", 900_000)  # 本地已判计算机IT
    tree.add_file("/我的网盘/书库3/封面图.jpg", 20_000)                   # 非图书
    code, out = run_cli(["extsort", "--config", online_config_path,
                         "--path", "/我的网盘/书库3", "--dest", "/我的网盘/书档3",
                         "--rules", "booksort,bookonline", "--apply", "--verbose"], ROOT)
    check("bookonline 执行退出码 0", code == 0, out)
    check("启用提示可见", "联网补全已启用" in out, out)
    check("成功移动 2 个", "成功移动 2 个" in out, out)
    check("联网结果落位 文学小说/",
          "/我的网盘/书档3/文学小说/活着.pdf" in tree.nodes, str(tree.nodes.keys()))
    check("本地已判定的仍归 计算机IT/",
          "/我的网盘/书档3/计算机IT/Python编程从入门到实践.epub" in tree.nodes,
          str(tree.nodes.keys()))
    check("非图书原地不动", "/我的网盘/书库3/封面图.jpg" in tree.nodes, str(tree.nodes.keys()))
    check("只查了 1 本（Python 那本没联网）",
          len(FakeDangdangHandler.requests) == 2, str(FakeDangdangHandler.requests))
    check("搜索请求用 GBK 编码书名（%BB%EE%D7%C5=活着）",
          FakeDangdangHandler.requests
          and "%BB%EE%D7%C5" in FakeDangdangHandler.requests[0],
          str(FakeDangdangHandler.requests))
    check("查询结果写入本地缓存", os.path.exists(cache_path), cache_path)
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        check("缓存内容为 文学小说（来源 dangdang）",
              (cache.get("titles", {}).get("活着") or {}).get("label") == "文学小说",
              str(cache.get("titles")))
    check("汇总提示联网归类数量", "联网补全" in out and "归类成功" in out, out)

    print("\n== 27) bookonline：缓存命中不再联网 + 搜错书时宁可不分类 ==")
    FakeDangdangHandler.requests = []
    code, out = run_cli(["extsort", "--config", online_config_path,
                         "--path", "/我的网盘/书库3", "--dest", "/我的网盘/书档3",
                         "--rules", "booksort,bookonline", "--apply"], ROOT)
    check("重跑无操作（已归档的幂等跳过）", "没有需要执行的操作" in out, out)
    check("重跑零联网请求（缓存命中）",
          FakeDangdangHandler.requests == [], str(FakeDangdangHandler.requests))

    # 新书：假站点无论查什么书都返回《活着》的商品页 → 相似度守卫应判"搜到的是别的书"
    tree.add_file("/我的网盘/书库3/三体.epub", 700_000)
    FakeDangdangHandler.requests = []
    code, out = run_cli(["extsort", "--config", online_config_path,
                         "--path", "/我的网盘/书库3", "--dest", "/我的网盘/书档3",
                         "--rules", "booksort,bookonline", "--apply"], ROOT)
    check("新书执行退出码 0", code == 0, out)
    check("确实联了网（1 次搜索 + 1 次商品页）",
          len(FakeDangdangHandler.requests) == 2, str(FakeDangdangHandler.requests))
    check("书名对不上 → 宁可不分类，保留 其它图书/",
          "/我的网盘/书档3/其它图书/三体.epub" in tree.nodes, str(tree.nodes.keys()))

    server.shutdown()
    dd_server.shutdown()
    print(f"\n========== 测试结果：通过 {PASS}，失败 {FAIL} ==========")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
