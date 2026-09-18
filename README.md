# pan-organizer — 网盘自动整理工具

通过 **Alist 的 WebDAV 接口**，按规则把网盘（百度网盘等）上的文件**移动到指定目录**。
移动在网盘服务端直接完成（alist 调用网盘 API 执行），不经过本地中转，不占用本地带宽。

> **当前版本：v1.6.2**——v1.6（图书漫画归类 + 联网补全）新增 `booksort` 规则：
> 图书/漫画按**书名**归入类型目录（漫画 / 教材教辅 / 计算机IT / 医学养生 / 心理学 / 历史传记 /
> 经济管理 / 法律 / 哲学宗教 / 外语学习 / 少儿绘本 / 文学小说 / 生活百科 / 其它图书），
> 非图书文件原地不动，可与 by_date 等规则嵌套。新增 `bookonline` 规则：本地书名没有类型线索的
> （《活着》《围城》）**联网查类型**再归类——默认用当当图书分类（免费、无需 key），
> 也可配 LLM 接口；只查兜底项、结果落本地缓存、网络异常只降级不中断任务。
> Web 规则页可直接勾选、改数据源/并发/上限，并支持**试查**单个书名。
>
> **v1.5 及更早**：v1.5 图书漫画归类雏形；v1.4（Web 计划管理 + v2 规则引擎落地）——三按钮（查询预览 / 查询并移动 / 按计划移动）+ 📋 计划管理折叠区（查看/下载/删除/应用），计划下拉**默认不选**避免误触发；规则页新增**按大类 / 按日期 / 按大小 / 正则筛选 / 清理空目录 / skip_incomplete 开关**并支持嵌套目录组合，引擎与 Web 全链路接通。**v1.4 还带来：日志全面增强**（任务头含版本/命令行/撞名策略、[失败明细] 分类清单、[执行汇总] 一行统计、任务尾含耗时与失败指引，全行带时间戳，出问题可凭日志精准定位）；**样式库本地内置**（`static/vendor/tailwind.js`，NAS 断外网页面不再退化成裸 HTML，缺失时自动回落 CDN）；**镜像装 tzdata**（日志时间戳与本地一致）；页面响应式适配窄屏，页脚显示真实端口。

```
┌─────────────┐   WebDAV (PROPFIND/MOVE/MKCOL)   ┌──────────────┐
│ pan_organizer.py │ ───────────────────────────────▶ │  alist       │
│ 规则引擎+CLI │                                  │ (NAS, 5244) │
└─────────────┘                                  └──────┬───────┘
                                                        │ 网盘官方 API
                                                 ┌──────▼───────┐
                                                 │  百度网盘等    │
                                                 └──────────────┘
```

## 项目目录结构

```
netdisk-sorter/
├── app/                  ← Docker 镜像构建上下文（纯净运行时）
│   ├── pan_organizer.py  ← 核心 CLI 引擎
│   ├── book_online.py    ← 图书联网二次分类（bookonline 规则的数据源/缓存）
│   ├── web.py            ← Flask Web 后端
│   ├── templates/        ← 页面模板
│   ├── static/           ← 前端 JS / CSS
│   ├── requirements.txt  ← Python 依赖
│   ├── Dockerfile
│   └── docker-compose.yml
├── data/                 ← 持久化目录（Docker 唯一挂载点，已 gitignore）
│   ├── config.json       ← alist 连接 + 默认 options + online（联网补全设置）
│   ├── state.json        ← 最近任务完整快照（容器重建后页面恢复用）
│   ├── plans/            ← plan-*.json（"查询"导出的计划，供"按计划移动"）
│   ├── online_cache.json ← 联网补全的书名→类型缓存（重跑零请求）
│   └── logs/             ← run-YYYYMMDD-HHMMSS.log 历史日志
├── docs/                 ← 详细文档
│   ├── README-web.md
│   ├── deploy-web-gui.md ← Docker 部署完整流程
│   └── NAS使用指南.md
├── scripts/              ← CLI 时代脚本（仍可用）
│   ├── pan-organizer-nas.sh
│   └── ebook-sort.sh
├── examples/             ← 示例配置
│   ├── config.example.json
│   └── rules.example.json
└── tests/                ← 单测
    ├── test_flow.py
    └── test_state_recovery.py
```

**原则**：Docker 镜像只装 `app/`，运行时数据只写到 `data/`（挂载卷）。
`docs/`、`scripts/`、`tests/` 都在镜像外，更新代码不会污染镜像。

## 快速开始

### 方式 A：Docker（推荐，常驻 Web UI）

```bash
cd app/
docker compose up -d --build
# 浏览器访问 http://NAS-IP:6060
```

数据存在宿主机 `../data/`，容器重建不丢。

### 方式 B：CLI（脚本整理 / 单次大任务）

```bash
cd app/
pip install -r requirements.txt
python pan_organizer.py extsort --path /百度网盘/下载
```

或用 `scripts/ebook-sort.sh` 一键（沿用老的便捷入口）。

---

## 为什么走 alist？

百度网盘开放平台 **2026-06-03 之后新建的应用**，`filemanager` / `move` 等接口**只能操作 `/apps/应用名/` 沙盒目录**，做不了全盘整理。
而 alist 挂载百度网盘用的是老授权，**具备全盘权限**，且你 NAS 上已经跑着 alist —— 直接复用，不用申请任何开发者账号。

> 前置条件：你的 alist 里已经添加并正常挂载了百度网盘存储。

## 最常用：extsort 一键按后缀归档（适合 2TB 这类大批量整理）

不用写规则文件，扫一遍自动按文件后缀分文件夹：

```bash
# 预览（只读，先看统计）
python pan_organizer.py extsort --path /百度网盘/下载

# 确认后执行（把 /下载 下所有文件按后缀归入 /下载/mp4、/下载/pdf …）
python pan_organizer.py extsort --path /百度网盘/下载 --apply
```

效果：`电影A.mp4` → `/百度网盘/下载/mp4/电影A.mp4`；`无后缀文件` → `noext/`；
`.part`/`.tmp`/`.crdownload` 等**未完成下载默认跳过**，不会误搬。

**目标路径由 `--dest` 指定**（推荐始终带上）：文件按后缀归入 `--dest` 下自动建的后缀文件夹，
源目录保持原样。不写 `--dest` 时归档根目录 = `--path` 本身（在源目录内按后缀建子夹，即"就地整理"）。

**同名自动编号（安全机制）**：不同子目录的两个 `同.mp4` 要进同一个 `mp4/` 目录时，
先到先得保留原名，后来的自动编号为 `同 (1).mp4`、`同 (2).mp4`、`同 (3).mp4`…
目标目录里已有的同名文件与已占用的编号会自动跳过，**绝不覆盖任何文件**。
编号在**预览阶段就确定**，预览里 `⇒ move → …` 显示的就是每个文件的真实落点。

想分批整理就加 `--only-ext`；只整理大文件加 `--min-mb`：

```bash
# 全部文件按后缀归入 /百度网盘/归档/ 下
python pan_organizer.py extsort --path /百度网盘/下载 --dest /百度网盘/归档 --apply

# 只把视频类归档，其余以后再处理
python pan_organizer.py extsort --path /百度网盘/下载 --only-ext mp4,mkv,avi,wmv --apply

# 只整理 100MB 以上的文件
python pan_organizer.py extsort --path /百度网盘/下载 --min-mb 100 --apply
```

**组合规则（v2）**：`--rules` 可同时勾选多条，目标目录按勾选顺序嵌套拼接：

```bash
# 按大类归档（图片/视频/音频/文档/压缩包/代码 六类，多后缀合一目录）
python pan_organizer.py extsort --path /下载 --dest /归档 \
    --rules extsort,category,skip_incomplete --apply   # extsort 与大类二选一即可

# 大类 + 修改日期 + 大小三档嵌套：电影.mp4 → /归档/视频/2026-01/大于1GB/电影.mp4
python pan_organizer.py extsort --path /下载 --dest /归档 \
    --rules category,by_date,by_size,skip_incomplete --apply

# 正则筛选：只整理文件名带「2026」的文件（其他原地不动）
python pan_organizer.py extsort --path /下载 --dest /归档 \
    --rules extsort,regex_match --regex-pattern 2026 --apply

# 整理完后清理源目录里的空文件夹（对下载站遗留的骨架目录有效）
python pan_organizer.py extsort --path /下载 --dest /归档 \
    --rules extsort,cleanup_empty,skip_incomplete --apply
```

> 目录拼接规则：首段 = 大类（category）或后缀（extsort，二选一，前者优先），
> 之后按 by_date（`YYYY-MM`）、by_size（`小于10MB/10-100MB/100MB-1GB/大于1GB`）逐级追加。
> `skip_incomplete` 是"跳过未完成下载文件"的开关（默认开）；关掉后 `.part/.tmp` 也会被归档。
> `dedupe`（重复文件 hash 检测）与 `cron`（定时调度）为规划中，尚未实现。

### 图书 / 漫画按类型归类（v1.5+）

`booksort` 规则把图书、漫画按**书名**归入类型目录（非图书文件原地不动）：

```bash
# 图书按类型归类：活着.pdf → /书库/文学小说/活着.pdf
python pan_organizer.py extsort --path /网盘/杂书 --dest /网盘/书库 \
    --rules booksort,skip_incomplete --apply

# 类型目录下再按月份分：xxx.epub → /书库/计算机IT/2026-03/xxx.epub
python pan_organizer.py extsort --path /网盘/杂书 --dest /网盘/书库 \
    --rules booksort,by_date,skip_incomplete --apply
```

三层判定：**漫画后缀直判**（cbz/cbr/cbt/cb7/cba + 书名含"漫画/连环画/画集"）→
**书名关键词顺位匹配**（教材教辅 / 计算机IT / 医学养生 / 心理学 / 历史传记 / 经济管理 /
法律 / 哲学宗教 / 外语学习 / 少儿绘本 / 文学小说 / 生活百科）→ 都不中归 `其它图书/`。

图书格式：`txt pdf epub mobi azw azw3 prc doc docx rtf html htm chm djvu pdb lrf fb2 lit`；漫画：`cbz cbr cbt cb7 cba`。

### 图书联网补全（v1.6）：给「其它图书」再判一次

书名不带类型线索的经典（《活着》《围城》《人类简史》）本地规则判不出，会被兜进
`其它图书/`。加上 `bookonline` 规则后，这部分**联网查类型**再归类：

```bash
# 本地判不出的书联网查（默认用当当图书分类，免费、无需 key）
python pan_organizer.py extsort --path /网盘/杂书 --dest /网盘/书库 \
    --rules booksort,bookonline,skip_incomplete --apply

# 先小批量试跑 200 本，确认效果和站点限流情况
python pan_organizer.py extsort --path /网盘/杂书 --dest /网盘/书库 \
    --rules booksort,bookonline --online-limit 200 --apply
```

行为与边界：

- **只查兜底项**：已能用关键词判出类型的一律不联网；非图书文件完全不参与。
- **结果落缓存**：`data/online_cache.json`（含命中的书名/类型/来源，可人工核对），
  同一本书只查一次，重跑 / 续跑零请求；`--online-refresh` 强制重查。
- **只降级不中断**：网络不通、页面改版、站点限流都只是"这本书没识别"，
  仍归 `其它图书/`，任务照常完成；遇到验证码/限流会自动熔断，不再继续打站点。
- **宁缺毋滥**：查到的商品书名与你要查的书名相似度过低（搜到的是别的书）时不采纳。
- 数据源 `dangdang`（默认，免费）取商品页面包屑「图书 > 小说 > 社会小说」；
  也可把 `provider` 设为 `llm`，用 OpenAI 兼容接口（DeepSeek / 通义 / 智谱 / Kimi /
  本地 Ollama）判定，准确率更高，需在 `config.json` 配 `api_key`。
- Web 端：规则页勾选「图书联网补全」，下方可改数据源 / 并发 / 上限 / LLM 设置，
  并有**试查**框（输入书名立刻看它会被归到哪一类）。

大数据量建议（后面有详细说明）：

1. **先跑一次不带 `--apply` 的预览**，看统计行——确认能扫到文件、归档目录符合预期。
2. 大批量文件**按顶层目录分次执行**，每次一个 `--path`，中途断了重跑同一条命令即可（已完成的自动跳过）。
3. 移动是同存储内服务端操作，速度取决于 alist → 百度网盘 API 的吞吐，几十万文件需要一些时间，属正常现象。

## 快速开始（规则模式）

```bash
# 1. 准备配置文件（改成你自己的 alist 地址和账号密码）
cp config.example.json config.json

# 2. 准备规则文件（把 target 里的 /百度网盘 改成你 alist 里的实际挂载点前缀）
cp rules.example.json rules.json

# 3. 测试连接，看看 alist 有哪些挂载点
python pan_organizer.py check

# 4. 先扫描预览（只读，不会移动任何文件）
python pan_organizer.py scan --path /百度网盘/下载

# 5. 确认计划无误后真正执行
python pan_organizer.py run --path /百度网盘/下载 --apply
```

> 所有网盘内路径都是 **WebDAV 完整路径**（`base_url` 之后的部分，以 `/` 开头），
> 与你在 alist 界面 / 百度网盘 App 里看到的目录结构一致。例如 `config.json` 里
> `base_url` 是 `http://192.168.1.100:5244/dav`，路径就写成 `/百度网盘/下载`。

## 命令一览

| 命令 | 作用 | 安全性 |
|---|---|---|
| `check` | 测试连接、列出所有挂载点 | 只读 |
| `extsort --path <目录> [--dest <目录>]` | **按后缀自动归档**，无需规则文件 | 预览只读，`--apply` 写 |
| `scan --path <目录> [--depth N]` | 扫描并按规则生成整理计划 | **只读** |
| `run --path <目录> [--depth N]` | 预览（不加 `--apply`） | 只读 |
| `run --path <目录> --apply` | **真正执行移动** | ⚠ 写操作 |

通用参数：

- `--config config.json` 配置文件（默认同目录下 `config.json`）
- `--rules rules.json` 规则文件（默认同目录下 `rules.json`）
- `--path /xxx` 待整理的网盘目录，必填
- `--depth N` 扫描深度：`0` 只扫该目录本身；`1` 额外深入一层子目录；`-1` 全部递归
  （规则模式默认 `0` 最安全；`extsort` 默认 `-1` 全递归，符合大批量整理预期）

### extsort 专属参数

| 参数 | 说明 |
|---|---|
| `--dest /目录` | 归档根目录，默认 = `--path`（在原目录内按后缀建子文件夹） |
| `--only-ext a,b,c` | 只整理这些后缀，其余跳过（配合分批整理） |
| `--skip-ext x,y` | 额外跳过后缀；`.part/.tmp/.crdownload/.downloading/.!qB/.temp` 默认已跳过 |
| `--skip-noext` | 无后缀文件不归 `noext/` 而是直接跳过 |
| `--min-mb / --max-mb` | 只整理大小在区间内的文件 |
| `--rules a,b,c` | 启用的规则 id（默认 `extsort,skip_incomplete`）。可选：`extsort`按后缀 / `category`按大类 / `booksort`图书漫画按书名归类 / `bookonline`图书联网补全（需与 `booksort` 同用） / `by_date`按修改月份 / `by_size`按大小档 / `skip_incomplete`跳过未完成文件 / `regex_match`正则筛选 / `cleanup_empty`清理空目录 |
| `--regex-pattern 正则` | 配合 `regex_match`：只整理文件名匹配正则的文件（如 `2026`、`\.(pdf\|epub)$`） |
| `--online-provider 源` | 配合 `bookonline`：`auto`（默认，先 LLM 再当当）/ `dangdang` / `llm` |
| `--online-limit N` | 配合 `bookonline`：单次最多查 N 个书名（默认取配置，0 = 不限），适合先小批量试跑 |
| `--online-refresh` | 配合 `bookonline`：忽略缓存 `data/online_cache.json`，强制重新联网查询 |
| `--plan out.json` | 把整理计划导出为 JSON（含 meta + ops；审计 / 后续 `--from-plan` 复用） |
| `--from-plan out.json` | 跳过扫描，按 `--plan` 导出的计划直接执行：先 dry-run 预览，加 `--apply` 真正移动（Web「按计划移动」同机制） |
| `--verbose` | 逐条打印每个文件的移动（默认只打汇总 + 进度） |

> 典型两段式：`extsort --path /下载 --dest /归档 --plan plan.json`（只查不搬）
> → 人工核对 `plan.json` → `extsort --from-plan plan.json --apply`（不重复扫描直接搬）。
> 已搬走/源消失的文件在计划里自动计"跳过"，重复执行天然幂等。

## 规则文件语法（rules.json）

```jsonc
{
  "rules": [
    {
      "name": "视频",                    // 规则名（仅用于日志显示）
      "match": [                        // 多个条件 = 同时满足（AND）
        { "type": "ext", "values": ["mp4", "mkv"] }
      ],
      "action": "move",                 // 目前支持 move
      "target": "/百度网盘/视频"          // 目标目录（不存在会自动创建）
    }
  ],
  "fallback": { "action": "skip" }      // 未命中任何规则的兜底：skip 或 move 到某目录
}
```

匹配条件按顺序逐条判断，**第一条全部条件命中的规则生效**（排序靠前的优先）。

### match 子句类型

| type | 参数 | 说明 | 示例 |
|---|---|---|---|
| `ext` | `values: [扩展名]` | 文件扩展名匹配（大小写不敏感，不写点） | `["mp4","mkv"]` |
| `name_contains` | `values: [关键词]` | 文件名包含任意关键词 | `["临时","tmp"]` |
| `name_glob` | `pattern: "通配符"` | 文件名通配符（可多个） | `"*.part"` |
| `name_regex` | `pattern: "正则"` | 文件名正则匹配 | `"^[Ss]\d+E\d+"` |
| `size_gt` | `mb: 数字` | 文件大于 N MB | `mb: 2048` |
| `size_lt` | `mb: 数字` | 文件小于 N MB | `mb: 100` |
| `isdir` | `value: bool` | 是否目录（默认工具只搬文件） | `value: true` |

## 配置文件说明（config.json）

```jsonc
{
  "alist": {
    "base_url": "http://192.168.1.100:5244/dav",  // alist WebDAV 地址（末尾 /dav 保留）
    "username": "...",                            // alist 登录用户名
    "password": "...",                            // alist 登录密码
    "timeout": 30                                 // 单请求超时秒数
  },
  "options": {
    "on_conflict": "rename",                      // 目标同名冲突处理（页面三选一）：
                                                  //   rename    → 撞名自动编号 (1)(2)…（默认，绝不覆盖）
                                                  //   skip      → 撞名不搬，跳过
                                                  //   overwrite → 用源文件替换目标同名文件（内容以源为准）
    "exclude_dirs": ["/百度网盘/已整理"]            // 黑名单目录（绝对路径），整体跳过
  },
  "online": {                                     // 图书联网补全（bookonline 规则）用，不用可整段删掉
    "provider": "auto",                           // auto（先 LLM 再当当）/ dangdang / llm
    "timeout": 8,                                 // 单次 HTTP 超时（秒）
    "workers": 3,                                 // 并发查询数（别调太大，站点会限流）
    "limit": 0,                                   // 单次最多查多少本，0 = 不限
    "delay": 0.4,                                 // 每请求间隔（秒）
    "llm": {                                      // provider=llm 时用（OpenAI 兼容接口）
      "base_url": "https://api.deepseek.com/v1",
      "model": "deepseek-chat",
      "api_key": ""                               // 只存本机，不进 git（data/ 已忽略）
    }
  }
}
```

## 内置安全设计

1. **默认只读**：`run` / `extsort` 不带 `--apply` 时只打印计划，绝不移动任何文件。
2. **已在目标目录的文件自动跳过**（源 == 目标直接忽略），重复执行不会乱搬，**天然幂等**。
3. **规则目标目录自动排除**：扫描时不会钻进 `/视频`、`/图片` 等目标目录把已整理好的文件再搬一遍。
   `extsort` 同理：归档区（`--dest`）在扫描范围内时整体跳过。
4. **同名自动编号**（默认 `rename`，绝不覆盖）：同名文件要进同一目标目录时，
   **计划阶段就完成编号**——先到先得保留原名，后来的自动变成 `名字 (1).扩展名`、
   `名字 (2).扩展名`…；目标目录已有的同名文件、已占用的编号自动跳过
   （目标已有 `a.mp4` 和 `a (1).mp4` 时，新来的从 `a (2).mp4` 起编号）。
   预览里 `⇒ move → …` 显示的就是含改名结果的**最终文件名**，可执行前逐条核对。
   执行时除标准 `412` 撞名外，还兜底处理"撞名被服务端报成 `409/500/502/503`"：
   失败后补一次目标探测，确认目标确有同名就自动改名重试；确认无同名则判为
   服务端瞬时故障并如实记失败（稍后重跑即可），不会误改名，也不会产生多余副本。
5. **`overwrite`（覆盖）采用"备份式覆盖"，替换目标但不丢文件**：百度网盘等后端
   **不支持覆盖式 MOVE**——目标同名时服务端返回 `errno=12`，alist 把它包装成
   HTTP 500，WebDAV 的 `Overwrite: T` 请求头形同虚设。所以选「覆盖」时引擎不直接删目标，
   而是拆成服务端支持的原子步骤：① 目标原文件改名 `xxx.__bak_<时间戳>` 让位 →
   ② 移动源到目标 → ③ 成功则删掉备份；**失败则把备份改回原名回滚**（目标恢复原样、
   源仍在原处）。最坏情况（回滚也失败）原文件仍在 `.__bak_` 名下，日志会明示路径可
   手动改回；扫描时无条件跳过 `.__bak_` 残留，不会把它们当普通文件搬走。
6. **默认只搬文件不搬目录**：目录结构保持原样，深度用 `--depth` 精确控制。
7. 全部日志打印源路径与目标路径，移动失败逐条提示，不影响其余文件。
8. **海量文件优化**：每个目标目录只做一次存量探测，重名编号一次算完；
   执行时直接 MOVE，不再有"撞 412 再改名"的反复重试（极端并发下仍保留兜底）；
   已建目录不重复请求；`extsort` 默认只打汇总与进度，不逐条刷屏（逐条看加 `--verbose`）。

## 大批量（TB 级 / 几十万文件）操作建议

1. **先预览**：`python pan_organizer.py extsort --path /xxx`，确认统计行里的文件数、后缀分布、目标目录符合预期，再 `--apply`。
2. **按顶层目录分批**：一次整理一个 `--path`，不要直接拿整个网盘根挂载点当 `--path`（几十万文件会扫很久）。每批完事后目录已幂等，重跑不会动已归档文件。
3. **中途断了直接重跑**：成功的已经移动到位，重跑自动跳过；失败项会重新尝试。断点续跑是天然能力，不需要任何状态文件。
4. **未完成下载别着急整理**：`extsort` 默认跳过 `.part/.tmp/.crdownload/.downloading/.!qB/.temp`；如果你用规则模式，给这些后缀单独写一条 `action: skip` 的兜底前规则。
5. **先删后整**：百度网盘对单个目录内文件数有上限（几万），如果某目录已经堆了大量文件，建议先在里面按类型/年份建子目录手动分几批，或用 `--only-ext` 分后缀批次执行。
6. **看进度**：执行中约每 1%（或每 50 个文件，取更小者）打一行带百分比的进度，如 `[进度] 已完成 38% (2914/7630)，成功 2911，失败 2，跳过 1`，最后一行必为 100%；失败会立即红字提示，不会卡住整个任务。Web 版日志页会在上方据此渲染实时进度条。

> `extsort` 归档根目录建议用独立的 `--dest`（如 `/百度网盘/归档`），并保持在各个 `--path`
> 的扫描范围之外（或加入 `exclude_dirs`），避免归档区被扫进来造成重复判断。

## 常见问题

**Q: 提示「无法连接 ...」？**
A: ① 确认 `base_url` 里的 IP/端口正确；② 确认运行本工具的电脑和 NAS 在**同一局域网**；
③ alist 需开启 WebDAV（默认监听 `5244`，路径 `/dav`）。

**Q: 提示「路径不存在」？**
A: 先 `python pan_organizer.py check` 看挂载点真实名字，把 `--path` 与规则 `target` 里的前缀改成一致。

**Q: 403 没有权限 / 移动失败？**
A: 确认 alist 账号对目标目录有写权限（用 alist 界面手动试一次移动）；百度网盘对目录内文件数
超过上限（约几万）的目录操作会报错，先人工清理。

**Q: 移动非常慢？**
A: alist 里百度网盘存储 → 目标也在同一存储内时是服务端秒移；如果跨存储（如从百度网盘移到
阿里云盘）则是下载再上传，会慢——请把规则 target 保持在同一个挂载点内。

**Q: 想整理的不是整个网盘而是某个子目录？**
A: `--path` 指定任意子目录即可，规则不会越界扫描。

**Q: 只想按后缀归档，不想写规则文件？**
A: 直接用 `extsort` 子命令，无需 rules.json（详见文首"最常用"一节）。

**Q: extsort 之后还想把某后缀再细分（比如按年份）？**
A: 两阶段：先用 `extsort --path /下载 --apply` 把散文件按后缀收拢，
再对某个后缀目录用规则模式写细规则二次整理（如 `name_regex` 提取年份），互不冲突。

**Q: 两个同名文件会不会互相覆盖？**
A: 不会。默认 `rename` 策略下，同名文件进同一目标目录时**在计划阶段就自动编号**：
先到先得保留原名，后来的变成 `名字 (1).后缀`、`名字 (2).后缀`…；
目标目录已有的同名文件和已占用的编号自动跳过。预览里 `⇒ move → …` 就是含编号的最终落点，
执行前即可核对，执行时绝不覆盖。

**Q: extsort 想归到别的目录，怎么指定？**
A: 加 `--dest /目标路径`，文件会按后缀归入 `目标路径/mp4/`、`目标路径/pdf/` 等，
源目录保持原样；不写 `--dest` 时是在 `--path` 目录内就地按后缀建子夹。

**Q: 定时自动整理？**
A: Windows 用任务计划程序、Linux/NAS 用 cron 定时跑 `extsort --apply` / `run --apply` 即可，天然幂等，可重复执行。

## 测试

自带本地模拟 alist 的测试（无需真实网盘），覆盖规则模式移动、冲突改名、递归、幂等、
目录排除、extsort 按后缀归档、撞名 500 兜底、覆盖式整理的"备份式覆盖 + 失败回滚"，
`--plan` 导出 → `--from-plan` 执行全链路，
v2 规则（大类/日期/大小嵌套目录、正则筛选、清理空目录、skip_incomplete 开关），
v1.5 图书漫画归类，v1.6 图书联网补全（含假"当当"站点，全程不联网）：

```bash
python tests/test_flow.py            # 144 项断言（含 mock WebDAV 服务端到端）
python tests/test_plan_ops.py        #  16 项（计划文件解析 / 旧格式兼容 / 404 跳过）
python tests/test_state_recovery.py  #  30 项（状态持久化与页面恢复）
python tests/smoke_web.py            #  80 项（Web 全接口冒烟 + query→plan→move 全链路，
                                     #       临时数据目录，不碰真实 data/）
python tests/test_online.py          #  48 项（书名清洗 / 面包屑解析 / 类型匹配 /
                                     #       缓存 / 降级 / 熔断；数据源用注入的假 provider）
```

## 文件清单

完整目录结构见文首「项目目录结构」：主程序在 `app/`（Docker 镜像上下文），
运行时数据统一在 `data/`（唯一挂载点），示例配置在 `examples/`，测试在 `tests/`。
