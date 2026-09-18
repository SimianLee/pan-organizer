#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pan-organizer 图书联网二次分类（v1.6）
=====================================
本地关键词规则（`booksort_classify`）只能吃"书名里带类型线索"的书；
《活着》这类书名没有线索的会被兜进「其它图书」。本模块负责把这部分
交给外部数据源判定类型。

设计原则
--------
1. **默认关闭**：必须显式启用（CLI `--rules booksort,bookonline`，
   Web 勾选「图书联网补全」）。不启用时零网络请求。
2. **只查兜底项**：只有本地判为「其它图书」的文件才联网；已经能归类的一律
   不查（省流量、省配额、少打扰第三方站点）。
3. **本地缓存**：结果写入 `data/online_cache.json`，同一书名只查一次，
   重跑/续跑零请求；`--online-refresh` 可强制重查。
4. **只降级不中断**：任何网络异常、解析失败都退回「其它图书」，绝不让整理
   任务失败；遇到目标站点限流（验证码页）自动熔断，不再继续打。
5. **数据源可插拔**：见 `PROVIDERS`。默认 `auto` =
   llm（配了 key 才用，准确率最高）→ dangdang（免费、无需 key、中文书覆盖好）。

实测结论（2026-09）：Google Books / Open Library 在国内网络不可达；
豆瓣搜索被 302 重定向到 sec.douban.com 验证页。故默认只用当当，
llm 源保留给"自备 API key 追求准确率"的场景。
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__all__ = [
    "OnlineClassifier", "PROVIDERS", "PROVIDER_ORDER", "DEFAULT_CONFIG",
    "clean_title", "dangdang_provider", "llm_provider", "build_config",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 配置默认值；用户可在 config.json 的 "online" 段覆盖（Web「规则」页可视化编辑）
DEFAULT_CONFIG = {
    "provider": "auto",          # auto | dangdang | llm
    "timeout": 8,                # 单次 HTTP 超时（秒）
    "workers": 3,                # 并发查询线程数（礼貌值，别调太大）
    "limit": 0,                  # 单次任务最多查多少本，0 = 不限
    "delay": 0.4,                # 每请求前的间隔（秒），避免触发站点限流
    "search_url": "https://search.dangdang.com/?key={q}&act=input",
    "product_url": "http://product.dangdang.com/{id}.html",
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
    },
}

# 自动模式的尝试顺序：先 llm（准确率高，仅当配了 key），再 dangdang（免费兜底）
PROVIDER_ORDER = ["llm", "dangdang"]


def build_config(raw):
    """把 config.json 的 "online" 段合并到默认配置上（两层深合并）"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))          # deepcopy
    raw = raw or {}
    for k, v in raw.items():
        if k == "llm" and isinstance(v, dict):
            cfg["llm"].update({kk: vv for kk, vv in v.items() if vv is not None})
        elif v is not None:
            cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# 书名清洗：文件名 → 适合拿去检索的书名
# ---------------------------------------------------------------------------
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_BOOK_TITLE_RE = re.compile(r"《([^》]{1,80})》")
_LEAD_BRACKET_RE = re.compile(r"^\s*[\[\(（【][^\]\)）】]{1,24}[\]\)）】]\s*")
_AUTHOR_CUT_RE = re.compile(
    r"[\s_\-]*[\[\(（【]?\s*(?:作者|主编|编者|译者|编著|著|译|编|校注)\s*[:：].*$")
_TAIL_PAREN_RE = re.compile(r"\s*[\[\(（【][^\]\)）】]{0,60}[\]\)）】]\s*$")
_PLACEHOLDER_RE = re.compile(r"^(?:未命名|新建|无标题|untitled|unknown|\d+)$", re.I)


def clean_title(name):
    """
    文件名 → 检索用书名。纯函数，可单测。

    《活着》.epub                     → 活着
    中国历史百科全书10：民族与对外关系卷》主编：徐寒.pdf → 中国历史百科全书10
    活着（余华代表作，精装）.epub        → 活着
    [高清扫描版] 三体.epub             → 三体
    """
    if not name:
        return ""
    t = _EXT_RE.sub("", os.path.basename(str(name)).strip())
    # 书名号内即书名（最可靠），多个取第一个
    m = _BOOK_TITLE_RE.search(t)
    if m:
        t = m.group(1)
    else:
        t = _LEAD_BRACKET_RE.sub("", t)          # 去掉开头的 [高清版] (1) 等前缀
        t = _AUTHOR_CUT_RE.sub("", t)            # 砍掉 作者：/主编：xxx 尾巴
        # 尾部的括号补充说明（余华代表作，精装…）整段丢掉
        prev = None
        while prev != t:
            prev = t
            t = _TAIL_PAREN_RE.sub("", t)
    # 副标题只在左半边足够长时截断：人类简史：从动物到上帝 → 人类简史
    for sep in ("：", ":"):
        i = t.find(sep)
        if 2 <= i:
            t = t[:i]
            break
    t = re.sub(r"[\s\u3000]+", " ", t).strip(" \t._-·、,，")
    if _PLACEHOLDER_RE.match(t):
        return ""
    return t[:60]


# ---------------------------------------------------------------------------
# HTTP 小工具（只用标准库，与主程序零依赖风格保持一致）
# ---------------------------------------------------------------------------
class BlockedError(Exception):
    """被目标站点限流/要求验证码 —— 触发熔断，停止后续查询"""


def _http_get(url, timeout, referer=None):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_post_json(url, payload, timeout, api_key=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _decode(raw):
    """中文站点编码各异（当当是 GBK），按 常见编码 顺序试探"""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# 限流/验证码特征。注意别写裸词 robot —— 页面里的 <meta name="robots"> 会误判。
_BLOCK_RE = re.compile(
    r"请输入验证码|请输入下图|安全验证|访问过于频繁|访问受限|人机验证|滑动验证|"
    r"drag the slider|verify you are human", re.I)


def _check_blocked(html):
    if _BLOCK_RE.search(html or ""):
        raise BlockedError("目标站点要求验证码/限流")


# ---------------------------------------------------------------------------
# 源 1：当当（免费、无需 key）
# ---------------------------------------------------------------------------
# 商品页自带面包屑「图书 > 小说 > 社会小说」，正是分类所需的强信号。
_BREADCRUMB_RE = re.compile(
    r"""<a[^>]*href=["']([^"']*dangdang\.com[^"']*)["'][^>]*>(.*?)</a>""",
    re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY = {"&nbsp;": " ", "&amp;": "&", "&gt;": ">", "&lt;": "<", "&quot;": '"'}


def _strip_tags(s):
    s = _TAG_RE.sub("", s or "")
    for k, v in _ENTITY.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip()


def parse_breadcrumb(html):
    """
    当当商品页 → 分类路径段列表，如 ["图书", "小说", "社会小说"]。
    只取面包屑区域内的 book./category. 链接，避开页面其它分类导航。
    """
    i = html.find('id="breadcrumb"')
    if i < 0:
        i = html.find("breadcrumb")
    if i < 0:
        return []
    block = html[i:i + 2000]
    segs = []
    for href, inner in _BREADCRUMB_RE.findall(block):
        if "book.dangdang.com" not in href and "category.dangdang.com" not in href:
            continue
        text = _strip_tags(inner)
        if text and text not in segs:
            segs.append(text)
    # 面包屑的第一段恒为站点根（"图书"），保留也无害（本地匹配器会跳过）
    return segs[:6]


_DANGDANG_ID_RE = re.compile(r"product\.dangdang\.com/(\d{6,})\.html")
_PAGE_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_NORM_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

# 命中的商品与查询书名的相似度下限：低于它判为"搜到的是别的书"，
# 宁可不分类也不给错分类（实测「不列颠百科全书索引」会搜出无关人物传记）。
MIN_TITLE_SIMILARITY = 0.5


def product_title(html):
    """当当商品页 → 商品书名"""
    m = _PAGE_TITLE_RE.search(html or "")
    if not m:
        return ""
    t = _strip_tags(m.group(1))
    t = re.sub(r"【[^】]*】.*$", "", t)          # 去掉 【简介_书评_在线阅读】
    t = re.sub(r"\s*-\s*当当.*$", "", t)        # 去掉 "- 当当图书"
    bm = _BOOK_TITLE_RE.search(t)
    return (bm.group(1) if bm else t).strip()


def title_similarity(a, b):
    """归一化后的书名相似度 0~1（纯函数，可单测）"""
    import difflib
    na, nb = _NORM_RE.sub("", a or ""), _NORM_RE.sub("", b or "")
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def dangdang_provider(title, ocfg, timeout, labels=None):
    """
    书名 → 分类文本（面包屑用 " > " 连接）。查不到返回 None。
    两步：搜索页取首个商品 id → 商品页取面包屑。
    """
    try:
        q = urllib.parse.quote(title.encode("gbk"))
    except UnicodeEncodeError:                    # 含 GBK 之外的字符 → 退回 UTF-8
        q = urllib.parse.quote(title)
    search_url = (ocfg.get("search_url")
                  or DEFAULT_CONFIG["search_url"]).replace("{q}", q)
    html = _decode(_http_get(search_url, timeout))
    _check_blocked(html)
    m = _DANGDANG_ID_RE.search(html)
    if not m:
        return None
    pid = m.group(1)
    product_url = (ocfg.get("product_url")
                   or DEFAULT_CONFIG["product_url"]).replace("{id}", pid)
    page = _decode(_http_get(product_url, timeout, referer=search_url))
    _check_blocked(page)
    # 书名核对：搜索页首个结果不一定就是目标书，差太多就放弃（宁缺毋滥）
    pt = product_title(page)
    if pt and title_similarity(title, pt) < MIN_TITLE_SIMILARITY:
        return None
    segs = parse_breadcrumb(page)
    return " > ".join(segs) if segs else None


# ---------------------------------------------------------------------------
# 源 2：LLM（OpenAI 兼容接口，需自备 key）
# ---------------------------------------------------------------------------
LLM_PROMPT = (
    "你是图书分类员。请把下面的书名归入下列类别之一，只输出类别名本身，"
    "不要任何解释或标点；实在无法判断就输出「其它图书」。\n"
    "可选类别：{labels}\n书名：{title}"
)


def llm_provider(title, ocfg, timeout, labels=None):
    """书名 → 模型输出的类别名（可能是自由文本，交给本地匹配器归一）。"""
    llm = ocfg.get("llm") or {}
    key = (llm.get("api_key") or "").strip()
    if not key:
        return None                                # 未配置 key → 跳过（auto 落到下一源）
    base = (llm.get("base_url") or DEFAULT_CONFIG["llm"]["base_url"]).rstrip("/")
    model = llm.get("model") or DEFAULT_CONFIG["llm"]["model"]
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 24,
        "messages": [{"role": "user", "content": LLM_PROMPT.format(
            labels="、".join(labels or []), title=title)}],
    }
    data = _http_post_json(base + "/chat/completions", payload, timeout, key)
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    return text or None


### ---------------------------------------------------------------------------
# 源注册表：加数据源只需在这里加一个 fn(title, ocfg, timeout, labels) -> str|None
# ---------------------------------------------------------------------------
PROVIDERS = {
    "dangdang": dangdang_provider,
    "llm": llm_provider,
}


def _provider_ready(name, ocfg):
    """该源在当前配置下是否可用（llm 需要 key，否则视为未就绪）"""
    if name == "llm":
        return bool(((ocfg.get("llm") or {}).get("api_key") or "").strip())
    return name in PROVIDERS


# ---------------------------------------------------------------------------
# 分类器：缓存 + 并发 + 降级 + 统计
# ---------------------------------------------------------------------------
class OnlineClassifier:
    """
    用法：
        oc = OnlineClassifier(ocfg, matcher, cache_path="data/online_cache.json")
        label = oc.classify("活着.epub")          # → "文学小说" / None
        res   = oc.classify_many(list_of_names)   # {原文件名: label}
        oc.save()                                  # 落缓存（classify_many 会自动落）

    matcher：把远端返回的分类文本归一成本地类型名，签名 matcher(text) -> label|None。
             由主程序注入（用 booksort 的那套关键词规则），保证两个模块同一套口径。
    """

    def __init__(self, ocfg, matcher, cache_path=None, log=print,
                 limit=None, refresh=False):
        self.cfg = build_config(ocfg)
        self.matcher = matcher
        self.cache_path = cache_path
        self.log = log
        self.limit = int(limit if limit is not None else self.cfg.get("limit") or 0)
        self.refresh = bool(refresh)
        self.verbose = False                      # 逐条打印明细（主程序按 --verbose 打开）
        self.labels = None                        # 允许的类型全集（llm prompt 用）
        self.cache = {}
        self.cache_dirty = False
        self._lock = threading.Lock()
        self._blocked = False                     # 熔断开关
        self.last_error = None                    # 最近一次查询失败原因（试查回显用）
        # 统计
        self.stats = {"queries": 0, "cache_hit": 0, "hit": 0, "miss": 0,
                      "error": 0, "blocked": 0, "skipped_limit": 0,
                      "sources": {}}
        self._load_cache()

    # ---- 缓存 ----
    def _load_cache(self):
        if not self.cache_path or self.refresh or not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.cache = data.get("titles") or {}
        except (OSError, ValueError):
            self.cache = {}                        # 缓存损坏不影响主流程

    def save(self):
        if not self.cache_path or not self.cache_dirty:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "updated": int(time.time()),
                           "titles": self.cache}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.cache_path)
            self.cache_dirty = False
        except OSError as e:
            self.log(f"  [联网] 缓存写入失败（不影响本次结果）：{e}")

    # ---- 查询 ----
    def _lookup(self, title):
        """走 provider 链，返回 (label|None, source, raw)"""
        if self._blocked:                          # 熔断：已被限流就不再打站点
            return None, "", None
        self.last_error = None
        order = self.cfg.get("provider") or "auto"
        chain = PROVIDER_ORDER if order == "auto" else [order]
        raw = None
        for name in chain:
            fn = PROVIDERS.get(name)
            if fn is None or not _provider_ready(name, self.cfg):
                continue
            try:
                if self.cfg.get("delay"):
                    time.sleep(float(self.cfg["delay"]))
                raw = fn(title, self.cfg, float(self.cfg.get("timeout") or 8), self.labels)
            except BlockedError:
                self._blocked = True
                with self._lock:
                    self.stats["blocked"] += 1
                self.log(f"  [联网] {name} 触发站点验证码/限流 → 本次任务停止联网查询"
                         f"（已查到的结果照常使用）")
                return None, name, None
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    ValueError, KeyError) as e:
                with self._lock:
                    self.stats["error"] += 1
                    self.last_error = f"{name} 查询失败：{type(e).__name__}: {e}"
                if self.verbose:
                    self.log(f"  [联网] {self.last_error}")
                continue
            if raw:
                with self._lock:
                    self.stats["sources"][name] = self.stats["sources"].get(name, 0) + 1
                return self.matcher(raw), name, raw
        return None, (chain[0] if chain else ""), raw

    def probe(self, name):
        """
        单条试查（**不走缓存、不写缓存**）：给 Web「测试」按钮和人工排查用。
        返回 dict：{title, label, source, raw}
        """
        title = clean_title(name) or (name or "").strip()
        if not title:
            return {"title": "", "label": None, "source": "", "raw": "", "error": ""}
        label, source, raw = self._lookup(title)
        return {"title": title, "label": label, "source": source,
                "raw": raw or "", "error": self.last_error or ""}

    def classify(self, name):
        """
        单个文件名 → 类型标签（None = 没查出来，调用方保留兜底目录）。
        带缓存：同一书名第二次调用零网络请求。
        """
        res = self.classify_many([name])
        return res.get(name)

    def classify_many(self, names, progress_every=10):
        """
        批量：输入文件名列表 → {原文件名: 标签}（只含查到的）。
        内部按书名去重 → 并发查询 → 落缓存。任何异常都不会向外抛。

        职责边界：**候选筛选由调用方负责**（主程序只把"图书后缀 + 本地判不出类型"
        的文件送来），本方法不判断文件类型，收到什么书名就查什么。
        """
        out = {}
        titles = {}                                # 清洗后书名 -> [原名...]
        for n in names:
            t = clean_title(n)
            if not t:
                continue
            titles.setdefault(t, []).append(n)

        pending = []                               # 需要联网的书名
        for t, originals in titles.items():
            if self._blocked:
                break
            entry = self.cache.get(t)
            if isinstance(entry, dict) and not self.refresh:
                with self._lock:
                    self.stats["cache_hit"] += 1
                label = entry.get("label")
                if label:
                    with self._lock:
                        self.stats["hit"] += 1
                    for o in originals:
                        out[o] = label
                else:
                    with self._lock:
                        self.stats["miss"] += 1
                continue
            pending.append((t, originals))

        if self.limit and len(pending) > self.limit:
            with self._lock:
                self.stats["skipped_limit"] = len(pending) - self.limit
            self.log(f"  [联网] 本次待查 {len(pending)} 个书名，"
                     f"受 limit={self.limit} 限制，只查前 {self.limit} 个"
                     f"（其余仍归 其它图书，可再跑一次继续）")
            pending = pending[:self.limit]

        if pending:
            self.log(f"  [联网] 开始查询 {len(pending)} 个书名"
                     f"（源：{self.cfg.get('provider')}，并发 {self.cfg.get('workers')}）…")
        done = 0
        workers = max(1, int(self.cfg.get("workers") or 3))

        def work(item):
            t, originals = item
            label, source, raw = self._lookup(t)
            return t, originals, label, source, raw

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for t, originals, label, source, raw in pool.map(work, pending):
                done += 1
                with self._lock:
                    self.stats["queries"] += 1
                    self.cache[t] = {"label": label, "source": source,
                                     "raw": (raw or "")[:200], "ts": int(time.time())}
                    self.cache_dirty = True
                    if label:
                        self.stats["hit"] += 1
                    else:
                        self.stats["miss"] += 1
                if label:
                    for o in originals:
                        out[o] = label
                if self.verbose:
                    self.log(f"  [联网] {done}/{len(pending)} {t}"
                             f" → {label or '未识别（保留 其它图书）'}"
                             f"{f'（{source}）' if label else ''}")
                elif done % progress_every == 0:
                    self.log(f"  [联网] 进度 {done}/{len(pending)}，"
                             f"命中 {self.stats['hit']} 个")
        if pending:
            self.save()
        return out
