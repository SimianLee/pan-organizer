# pan-organizer 在 NAS 上的完整使用流程

> 适配环境：你的 NAS —— 研域工控 ITX-M56-D6L（J1900），RR 引导 DS918+ / DSM 7.2.1，
> IP `192.168.1.100`，容器由 Container Manager 管理，已运行 **alist**（WebDAV 端口 5244）。
>
> 全流程分两段：**① 一次性准备（约 10 分钟）** → **② 日常整理（每批 3 步命令）**。
> 文中的 `/百度网盘` 是 alist 里百度网盘挂载点名的**示例**，请换成你 alist 里实际的名字
> （不确定就在 alist 界面 →「存储」里看挂载路径；下面 `check` 命令也会列出来）。

---

## 0. 这套东西是怎么跑起来的

工具**直接跑在 NAS 上**，不经你的电脑，不占电脑带宽；移动文件是 alist 调百度网盘官方 API
在服务端完成的（改元数据，秒级生效，也不消耗网盘流量）。

```
┌─────────────── 你的电脑（浏览器）────────────────┐
│  File Station 传文件 / SSH 敲命令               │
└───────────────────────┬────────────────────────┘
                        ▼
┌───────────── NAS 192.168.1.100（DSM 7.2.1）─────────────────┐
│  Container Manager                                        │
│   ┌────────────────┐   http://192.168.1.100:5244/dav        │
│   │ pan-organizer 容器   │ ───── WebDAV ──────▶ ┌────────────┐ │
│   │ python:3.12-slim │  (PROPFIND/MOVE)     │  alist 容器 │ │
│   │  /app = 工具目录 │                      │  (已在跑)   │ │
│   └────────────────┘                      └──────┬─────┘ │
│      /volume1/docker/pan-organizer                     │ 百度 API│
└───────────────────────────────────────────────────┼────────┘
                                                     ▼
                                             百度网盘（全盘权限）
```

---

## ① 一次性准备

### 1.1 确认 alist 三项前提（缺一不可）

| 检查项 | 怎么查 | 达标标准 |
|---|---|---|
| alist 在运行 | Container Manager → 容器 → alist | 状态"运行中" |
| 百度网盘已挂载 | 浏览器开 `http://192.168.1.100:5244` → alist 界面左侧 | 能看到百度网盘目录且能浏览文件 |
| 记住挂载路径名 | alist 界面 → 存储 → 百度网盘存储的"挂载路径" | 记下形如 `/百度网盘` 的名字（后面所有 `--path` 前缀都用它） |

> 若百度网盘还没挂载：alist 界面 → 存储 → 添加 → 驱动选「百度网盘」，
> 按 alist 官方文档（驱动 → 百度网盘）完成 OAuth 授权即可，本篇不展开。

### 1.2 把工具拷到 NAS

1. File Station 打开 `volume1`（若你的 docker 目录在别的卷就用那个卷）→ 新建文件夹 `docker/pan-organizer`
2. 把本地 `netdisk-sorter/` 里的这些文件拖进去：

   | 文件 | 作用 |
   |---|---|
   | `pan_organizer.py` | 主程序（纯 Python 标准库，零依赖） |
   | `config.example.json` | 配置模板 |
   | `rules.example.json` | 规则模板（`scan`/`run` 才用，`extsort` 不需要） |
   | `pan-organizer-nas.sh` | **NAS 一键运行包装**（自动建容器、自动执行） |

   最终目录结构：`/volume1/docker/pan-organizer/{pan_organizer.py, pan-organizer-nas.sh, ...}`

### 1.3 准备运行环境（用 Docker 跑 Python，不污染 DSM 系统）

DSM 不自带 Python，最干净的做法是让 Container Manager 跑一个 `python:3.12-slim` 常驻容器。
**二选一：**

**方式 A：SSH（推荐，一条命令自动化）**

控制面板 → 终端机和 SNMP → 勾选「启用 SSH 功能」→ 应用。然后电脑上 SSH 连 NAS：

```bash
ssh admin@192.168.1.100        # 或 root
sudo -i                       # 切 root
cd /volume1/docker/pan-organizer
sh pan-organizer-nas.sh check      # ← 首次运行自动拉镜像、建容器、执行 check
```

`pan-organizer-nas.sh` 首次会自动完成：`docker pull python:3.12-slim` → 创建常驻容器
`pan-organizer`（挂载本目录到 `/app`，`--restart unless-stopped` 开机自启）。以后每次
`sh pan-organizer-nas.sh <参数>` 都只是 `docker exec` 进容器执行，开销极小。

**方式 B：纯 GUI（不碰 SSH）**

1. Container Manager → 注册表 → 搜索 `python` → 选 `3-alpine` → 下载
2. 映像 → `python:3.12-slim` → 运行：
   - 容器名称：`pan-organizer`
   - 高级设置 → 卷 → 添加文件夹 `/volume1/docker/pan-organizer` → 挂载路径 `/app`
   - 高级设置 → 命令：填 `sleep infinity`（让容器常驻）
3. 以后执行：Container Manager → 容器 → `pan-organizer` → **终端机** → 进入 shell 后：

   ```bash
   cd /app
   python pan_organizer.py check
   ```

### 1.4 写 config.json（连 alist 的凭据）

File Station 里把 `config.example.json` 复制一份改名为 `config.json`，编辑为：

```json
{
  "alist": {
    "base_url": "http://192.168.1.100:5244/dav",
    "username": "你的alist登录用户名",
    "password": "你的alist登录密码",
    "timeout": 30
  },
  "options": {
    "on_conflict": "skip",
    "exclude_dirs": []
  }
}
```

要点：

- `base_url` 结尾的 **`/dav` 不能漏**，这是 alist 的 WebDAV 端点
- 账号密码就是 alist 网页登录那套（WebDAV 默认复用 alist 账号）
- `on_conflict` 三选一（页面上自己选）：
  - `rename`（默认，推荐）：撞名自动编号 `(1)(2)…`，**坚决不覆盖**
  - `skip`：撞名就跳过不搬
  - `overwrite`：用源文件替换目标同名文件。走"备份式覆盖"（先备份让位 → 移入 → 成功删备份 / 失败自动回滚），
    过程不丢文件；但**目标文件内容确实会被替换**，确认两批文件谁该留谁再用
- `exclude_dirs` 可填黑名单目录（绝对路径），这些目录整体不扫描不移动

---

## ② 日常整理流程

### 2.1 先做连通性自检（每批开始前跑一次最稳妥）

```bash
cd /volume1/docker/pan-organizer
sh pan-organizer-nas.sh check
```

预期输出：列出 WebDAV 根下的所有挂载点，其中能看到 `/百度网盘` →
说明工具已连上 alist 且具备全盘可见权限。**若这里失败，后面都不用跑**（见第 5 节排查表）。

### 2.2 整理原则（2TB 量级尤其重要）

1. **只移动、不删除** —— 工具只有 MOVE 操作，改错了把 `--path`/`--dest` 对调再跑一遍就能移回去，无破坏风险
2. **按顶层目录分批**，一次一个 `--path`，不要拿整个网盘根目录一把梭
3. **每批三步走**：预览 → 核对 → `--apply`，每批都先看统计

### 2.3 每批三步：预览 → 核对 → 执行

以「把 `/百度网盘/下载` 按后缀归档到 `/百度网盘/归档`」为例：

**第 1 步 · 预览（只读，不移动任何文件）**

```bash
sh pan-organizer-nas.sh extsort --path /百度网盘/下载 --dest /百度网盘/归档
```

输出会告诉你：扫到多少个文件、按后缀分了哪几类、每类数量和总大小、
预计自动改名的数量（同名自动编号，如 `abc.mp4` → `abc (1).mp4`）。

**第 2 步 · 核对**

- `--dest` 写对没有（没写 `--dest` 等于原地按后缀建子夹，小心）
- 统计是否合理（若某个后缀数量异常巨大，见下方变体拆批）
- 注意：**默认会递归整个 `--path` 的所有子目录**（`--depth -1`）；只想整理当前一层加 `--depth 0`

**第 3 步 · 执行**

```bash
sh pan-organizer-nas.sh extsort --path /百度网盘/下载 --dest /百度网盘/归档 --apply
```

- 中途断网 / SSH 断开 / 超时？**原命令重跑一遍即可**——已归位的文件自动识别为
  "已在目标目录"跳过，天然断点续传，不需要任何状态文件
- 每 200 个文件打一行进度；想看每个文件明细加 `--verbose`

### 2.4 常用变体（都支持 `--apply`）

| 想做什么 | 追加参数 |
|---|---|
| 只先整理视频类 | `--only-ext mp4,mkv,avi,wmv` |
| 只整理大文件（≥100MB） | `--min-mb 100` |
| 只扫当前层、不碰子目录 | `--depth 0` |
| 不要 noext 文件夹 | `--skip-noext` |
| 额外跳过某后缀 | `--skip-ext iso,wmv` |
| 导出审计清单 | `--plan plan.json`（生成在 /volume1/docker/pan-organizer/plan.json） |

> 例：分后缀批次跑，一次只处理一种，最适合慢慢消化超大目录：
> `sh pan-organizer-nas.sh extsort --path /百度网盘/下载 --dest /百度网盘/归档 --only-ext mp4,mkv,avi,wmv --apply`

### 2.5 定时自动整理（可选，进阶）

让 NAS 每天凌晨自动把「下载目录」的新文件归入归档目录（工具幂等，重复执行安全）：

1. 控制面板 → **任务计划** → 新增 → **计划任务** → **用户自定义脚本**
2. 常规：任务名称 `pan-organizer-daily`，用户选 `root`
3. 计划：频率「每天」→ 时间 `04:00`
4. 任务设置 → 运行命令：

   ```bash
   sh /volume1/docker/pan-organizer/pan-organizer-nas.sh extsort \
     --path /百度网盘/下载 --dest /百度网盘/归档 --apply \
     >> /volume1/docker/pan-organizer/daily.log 2>&1
   ```

5. 确定后右键任务 → 运行 试一次，去 `/volume1/docker/pan-organizer/daily.log` 看结果

> 想定时却不想碰任务计划？你 NAS 上已在跑 **qinglong（青龙）**：
> 青龙容器里自带 python3，可直接跑 pan_organizer.py，见下方 2.6 方式二。

### 2.6 SSH 会断？三种不守着 SSH 的跑法

SSH 前台跑任务，连接一断任务就被杀。下面三种方式任务都在 **NAS 本机**执行，
跟你电脑、浏览器、SSH 会话是否断开完全无关：

#### 方式一：DSM 任务计划手动触发（纯 GUI，推荐首选）

全程浏览器操作，一个 SSH 命令都不用敲：

1. 控制面板 → **任务计划** → 新增 → 计划的任务 → **用户定义的脚本**
2. **常规**：任务名称 `ebook-sort`，用户身份选 `root`
3. **计划**：把「运行日期」里的周一~周日**全部取消勾选**
   （= 永不自动跑，只在我们手动点「运行」时触发，防止误定时）
4. **任务设置** → 用户定义的脚本，先填**预览版**（不带 --apply）：

   ```bash
   sh /volume1/docker/pan-organizer/ebook-sort.sh >> /volume1/docker/pan-organizer/run.log 2>&1
   ```

5. 保存 → 选中任务 → 点上方「**运行**」→ 确定
6. File Station 打开 `/volume1/docker/pan-organizer/run.log` 看预览统计
   （txt/pdf/epub 各多少本、多少个会自动改名编号）
7. 确认无误 → 编辑该任务，命令末尾**加上 `--apply`** → 保存 → 再点「运行」
8. 想看最新一次输出：把 `>>` 改成 `>`（覆盖写），或每次跑之前删掉 run.log

> 电子书场景专用脚本 `ebook-sort.sh` 已写死源/目标路径
> （`/百度网盘-小号/亚马逊电子书` → `/百度网盘-小号/电子书`），参数透传。
> 整理其他目录就照抄一份脚本改 SRC/DST 两行。

#### 方式二：青龙面板手动触发（你已在跑 qinglong，自带日志页）

qinglong 容器里有现成的 python3，直接跑 pan_organizer.py，不碰宿主机 docker：

1. Container Manager → 容器 → `qinglong` → 编辑 → 看**卷映射**：
   找到容器内路径形如 `/ql/data/scripts`（旧版为 `/ql/scripts`）对应的主机路径
   （例如 `/volume1/docker/qinglong/scripts`）
2. File Station 把 **`pan_organizer.py`** 和 **`config.json`** 拷进这个主机目录
3. 青龙面板（`http://192.168.1.100:5700` 之类）→ **定时任务** → 新建：
   - 任务名称：`电子书整理`
   - 命令：

     ```bash
     python3 /ql/data/scripts/pan_organizer.py extsort \
       --path "/百度网盘-小号/亚马逊电子书" \
       --dest "/百度网盘-小号/电子书"
     ```

     （容器内路径以第 1 步看到的为准；**先不加 `--apply` 跑预览**）
   - 定时规则：随便填一个（如 `30 4 * * *`），反正我们手动点运行
4. 保存后点任务行右侧的「**运行**」→ 到「**日志**」页看本次运行的完整输出
5. 预览没问题 → 编辑任务，命令末尾加 `--apply` → 再点「运行」

> 优点：每次运行的日志在青龙里按任务留存，翻历史方便；
> 想改成全自动，直接改定时规则即可，命令一个字不用动。

#### 方式三：SSH 只用来点火（nohup 后台，断线不杀任务）

如果还是习惯 SSH，让它"点完火就走"：

```bash
ssh admin@192.168.1.100
nohup sh /volume1/docker/pan-organizer/ebook-sort.sh --apply \
    > /volume1/docker/pan-organizer/run.log 2>&1 &
exit    # 直接断开 SSH，任务继续在 NAS 后台跑
```

之后随时 `tail -f /volume1/docker/pan-organizer/run.log` 或用 File Station 看进度。

---

## ③ 常见问题速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `check` 报 401 | alist 账号/密码错，或该用户无 WebDAV 权限 | 核对 config.json 的 username/password |
| `check` 超时连不上 | base_url 的 IP/端口错，或 alist 没把 5244 映射出来 | 确认 `http://192.168.1.100:5244` 浏览器能开 |
| 提示目录不存在 | `--path` 前缀写错 | 先 `check` 看挂载点实际名字（可能叫 `/百度`、`/我的云盘` 等） |
| 大量"已在目标目录跳过" | 上次跑到一半中断过，是正常续传行为 | 无需处理 |
| 把文件移错位置了 | 目标目录选错 | 把 `--path` 与 `--dest` 对调再执行一次，原样移回 |
| `noext/` 里文件太多 | 这些文件本身没有后缀 | 确认无碍后加 `--skip-noext` 跳过 |
| 某后缀目录文件近万 | 百度单目录文件数有上限 | 用 `--only-ext` 或 `--depth` 拆批消化 |
| `pan-organizer-nas.sh: not found` | Windows 换行符(CRLF)问题 | File Station 编辑该文件另存为 LF；或执行 `sed -i 's/\r$//' pan-organizer-nas.sh` |
| SSH 断开任务就停了 | 前台进程随会话被杀 | 改用 2.6 的三种方式（任务计划 / 青龙 / nohup），均在 NAS 本机执行 |

---

## ④ 安全纪律（贴墙上）

1. **永远先预览、核对，再加 `--apply`** —— 预览一行输出都不花你流量
2. **一批一个顶层目录**，别用整个网盘根做 `--path`
3. `--dest` 放在 `--path` 之外（如统一归档到 `/百度网盘/归档`），结构清爽也避免自扫
4. `on_conflict` 用默认 `rename`（撞名自动编号，绝不覆盖）；`overwrite` 仅在明确知道"源文件才是要留的那份"时使用
5. 起步阶段只整理「下载 / 临时」类目录，观察一两轮没问题再扩大范围
