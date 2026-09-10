# pan-organizer Web 版 · Docker 图形界面部署完整流程

> 适用环境：DS918+ / DSM 7.2.1（Container Manager）· NAS IP `192.168.1.100`
> 目标：把带网页界面的整理工具作为**常驻容器**跑起来，浏览器访问 `http://192.168.1.100:6060`
>
> 前提：NAS 上已有跑 CLI 的工具目录（含填好 alist 账号密码的 `config.json`），
> 假设目录为 `/volume1/docker/pan-organizer`。如果之前 CLI 目录不是这个名，全程换成你实际的路径即可。

---

## 0. 这次和 CLI 的区别

| | 之前 CLI（ebook-sort.sh） | 这次 Web 版 |
|---|---|---|
| 容器形态 | `docker run --rm` 一次性，跑完即毁 | 常驻容器，`restart: unless-stopped` |
| 入口 | SSH 敲 `sh ebook-sort.sh --apply` | 浏览器页面点按钮 |
| 界面 | 无 | 4 个标签页（连接/路径/规则/执行+日志） |
| 配置 | 改 config.json | 页面里填，保存到 `data/config.json` |
| 日志 | apply.log | 页面实时滚动 + 自动存历史日志到 `data/logs/` |
| 任务状态 | 仅看日志 | `data/state.json` 落盘：容器重建后页面打开即恢复 |
| 更新代码 | 重传文件后重跑 | 重传文件后 **重新构建镜像** |

两者共用同一个 `data/` 目录、同一个 `config.json`，可以并存，互不干扰。

> **数据目录结构**（新版统一到 `data/` 一个卷，Docker 挂这个：
> ```
> /volume1/docker/pan-organizer/data/
> ├── config.json     # alist 连接 + 默认 options
> ├── state.json      # 最近一次任务的完整快照（src/dst/rules/started_at/log_file/summary）
> └── logs/
>       └── run-YYYYMMDD-HHMMSS.log
> ```
> 旧版 `./config.json` 与 `./logs/` 会在容器首次启动时**自动迁移**到 `data/`，
> 无需手动操作。

### 项目目录结构（v1.2）

```
/volume1/docker/pan-organizer/
├── app/                  ← Docker 镜像构建上下文（项目根）
│   ├── pan_organizer.py
│   ├── web.py
│   ├── templates/
│   ├── static/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── docker-compose.yml   ← Container Manager 选这个文件作为项目入口
├── data/                 ← 唯一挂载点（持久化卷）
│   ├── config.json       ← alist 连接 + 默认 options
│   ├── state.json        ← 最近任务完整快照（页面状态恢复用）
│   ├── plans/            ← plan-*.json（"查询"导出的整理计划，供"按计划移动"）
│   └── logs/             ← run-YYYYMMDD-HHMMSS.log
├── docs/                 ← 文档（不进镜像）
├── scripts/              ← CLI 脚本（不进镜像）
├── examples/             ← 示例配置
└── tests/                ← 单测（不进镜像）
```

**关键变化**：docker-compose.yml 现在在 `app/` 子目录里，volumes 路径写的是
`../data:/app/data`（相对 docker-compose.yml 文件）。Container Manager 选项目
入口时要选 `docker/pan-organizer/app/docker-compose.yml`。

---

## 1. 部署前确认三件事

| 检查项 | 怎么查 | 达标 |
|---|---|---|
| alist 在运行 | Container Manager → 容器 → 找 alist | 状态"运行中" |
| NAS 上工具目录存在 | File Station → volume1 → docker → pan-organizer | 能看到 pan_organizer.py、config.json |
| **6060 端口没被占** | 浏览器开 `http://192.168.1.100:6060` | 打不开/无页面才安全 |

> ⚠️ **端口是最大雷区**：qBittorrent 的 Web 管理界面**默认占 8080**，本部署已特意改到 **6060** 避开。
> 如果 6060 也被别的服务占用：把 `docker-compose.yml` 里 `- "6060:6060"` 的**左边**换成空闲端口
> （如 `- "8081:6060"`），之后访问对应端口即可。

---

## 2. 把 Web 版文件传到 NAS（File Station，全程图形界面）

1. 电脑浏览器打开 `http://192.168.1.100:5000` 登录 DSM → 打开 **File Station**
2. 进入 `volume1/docker/pan-organizer`
3. 把本地 `netdisk-sorter/` 整个目录（**含 app/、data/、docs/、scripts/、examples/、tests/、README.md**）
   拖进 NAS 项目目录（同名覆盖）；如果之前没建过 `data/` 子目录，要带上。

### 必须上传/保留的文件清单

| 路径 | 状态 | 作用 |
|---|---|---|
| `app/web.py` | 新增/覆盖 | Flask Web 后端（任务管理 + SSE 日志流 + 状态持久化） |
| `app/Dockerfile` | 新增/覆盖 | 镜像构建定义 |
| `app/docker-compose.yml` | 新增/覆盖 | 容器编排（端口/挂载/重启策略/健康检查） |
| `app/requirements.txt` | 新增/覆盖 | 依赖（只有 Flask） |
| `app/templates/`（整个文件夹） | 新增/覆盖 | 网页界面模板 |
| `app/static/`（整个文件夹） | 新增/覆盖 | 前端 JS |
| `app/pan_organizer.py` | **覆盖** | 主程序，务必换最新版（5xx 探测兜底 + 执行进度%） |
| `data/config.json` | **保留** | 已填好 alist 账号密码的配置文件，新版直接复用 |
| `data/logs/` | **保留** | 历史日志 |
| `docs/` / `scripts/` / `examples/` / `tests/` | 可选 | 文档 / CLI 脚本 / 示例 / 测试，**不进 Docker 镜像**，本地维护用 |

### 自动迁移（容器内一次性）

新版 Web 启动时会自动把容器内 `/app/config.json` 与 `/app/logs/`
（即项目根目录下的同名路径，可能由旧版 build 直接 COPY 进镜像导致）迁到
`/app/data/` 下。但**用户**的旧 data 不在容器里——所以还需要做一步 host 端迁移：

### 升级到 v1.1（host 端一次性迁移，老用户必看）

如果你之前用的是 v1.0（直接挂 `./config.json` 与 `./logs/`），升级到 v1.1 之前
在 NAS 上跑下面这一条命令（File Station 不方便做，先开 SSH 或 Container Manager
的"终端"功能）：

```sh
cd /volume1/docker/pan-organizer
mkdir -p data
# 老 config.json / 老 logs/ 一次性搬到 data/ 下
[ -f config.json ] && mv config.json data/config.json
[ -d logs ] && mv logs data/logs
# 完成后老路径不再需要，可以删
ls -la data/   # 应看到 config.json + logs/
```

之后走第 5 步标准流程重建即可，新启动会自动把 data/ 内容直接用上，不会再发生
状态丢失。

### 不用动的东西

| 文件 | 原因 |
|---|---|
| `config.example.json` / `rules.example.json` / `ebook-sort.sh` 等 | 保留即可 |

> ⚠️ **上传时务必带目录结构**：`templates/` 和 `static/` 要整个文件夹传上去
> （File Station 拖文件夹，或右键 → 上传 → 上传文件夹）。只传里面的文件、不建文件夹，
> Docker 构建会报 `COPY templates/ ... no such file or directory`。

---

## 3. Container Manager 里新建项目（核心步骤）

1. NAS 桌面点开 **Container Manager**
2. 左侧导航点 **「项目」**
3. 右上角点 **「新增」**
4. 填写/选择：
   - **名称**：`pan-organizer-web`
   - **路径/来源**：选"使用现有的 docker-compose.yml"，文件夹浏览选
     `docker/pan-organizer/app`（即 docker-compose.yml 所在目录）
5. 点**下一步**：系统自动解析 `app/docker-compose.yml`，应能看到：
   - 服务 `pan-organizer-web`
   - 端口映射 `6060 → 6060`
   - 卷挂载 `../data → /app/data`（配置 / 状态 / 日志三合一，相对 app/）
   - 健康检查（healthcheck）
6. 点**下一步/应用**：开始构建镜像并启动容器
   - 首次构建：拉取 `python:3.12-slim`（约 50MB）+ 安装 Flask，视网速 **3～10 分钟**
   - 页面会滚动显示构建日志
7. 项目状态变成 **「运行中」** 即成功

> 不同 DSM 小版本按钮文案略有差异（"路径"可能叫"来源"、"下一步"可能叫"应用"），
> 认准要点即可：**名称 pan-organizer-web → 选 docker/pan-organizer 目录 → 解析 compose → 应用**。

**构建失败自查**：点进项目看构建日志，最常见原因就是第 2 步没带目录结构
（缺 `templates/` 或 `static/`），补传后重新构建即可。

---

## 4. 首次访问与页面配置

浏览器打开 `http://192.168.1.100:6060`：

1. **【① 连接】**
   - 如果 NAS 上 `config.json` 里 CLI 已经填好 alist → 页面会直接读到，点**测试连接**确认能列出挂载点即可
   - 没填过 → 填 `http://192.168.1.100:5244/dav` + 账号密码 → 测试连接 → **保存配置**
2. **【② 路径】**：左侧树选源目录（如 `/百度网盘-小号/亚马逊电子书`），右侧选目标目录
3. **【③ 规则】**：可多选并嵌套组合——
   - 按后缀归档 `extsort`（默认勾选）
   - 按大类 `category` / 按修改日期 `by_date` / 按大小 `by_size` / 正则筛选 `regex_match`（勾选后填下方正则）/ 清理空目录 `cleanup_empty`
   - 跳过未完成文件 `skip_incomplete`（默认勾选，取消后 `.part/.tmp` 也会归档）
   - 「重复文件检测」「定时任务」为规划中（置灰）
   - 撞名策略：自动改名 (1)(2)…（默认，绝不覆盖）/ 跳过 / 覆盖（用源文件替换目标同名文件，走"备份式覆盖"，失败自动回滚）
4. **【④ 执行与日志】**：三种方式任选
   - **查询(预览)**：只扫描生成计划（`plan-*.json`），不移动
   - **▶ 查询并移动**：扫描 + 移动一步到位（= 原"开始整理"）
   - **按计划移动**：在下拉里选中刚查询出的计划 → 跳过重复扫描直接移动
   - **📋 计划管理**（折叠区）：列出所有 `plan-*.json`，可查看 / 下载 / 删除 / 应用
   - 计划下拉**默认不选**（避免误触发），任务完成自动刷新
   （适合"先查询、人工核对计划后再执行"）
   日志框上方能看到实时进度条；需要中断点 **■ 停止**；任务结束日志自动归档到"历史日志"

---

## 5. 日常更新代码后如何重建（重要）

Web 版代码是**构建进镜像**的，改完代码只重传文件不会生效，必须重建：

1. 本地改完 → 用 File Station 把对应文件传回 NAS **覆盖**
   - 改了 `pan_organizer.py` → 传 `pan_organizer.py`
   - 改了页面 → 传 `templates/`、`static/`
2. **GUI 重建**：Container Manager → 项目 → 选中 `pan-organizer-web` → 详情页找
   「操作」里的 **重新构建 / 重新创建**（不同版本入口叫法不同，认"重新构建"字样）
3. **SSH 兜底（一行命令，任何时候都好使）**：
   ```sh
   cd /volume1/docker/pan-organizer/app && docker compose up -d --build
   ```
4. 浏览器 **Ctrl+F5** 强刷页面

---

## 6. 常见问题速查

| 现象 | 原因 | 解决 |
|---|---|---|
| 创建项目报 YAML 解析错（Missing closing "quote" / Implicit keys need to be on a single line / Flow sequence…end with a] 红字一大串） | `healthcheck` 的 test 用了**多行数组 + 反斜杠续行**，Container Manager 的解析器不认 | 改用单行写法（见下方"正确 healthcheck"），或直接删掉 `healthcheck:` 整段（不影响功能） |
| 容器起不来 / 页面打不开 | 6060 被其他服务占用 | compose 里把 `- "6060:6060"` 左边改成空闲端口（如 `8081:6060`）后重建，访问对应端口 |
| 页面打开，点保存配置报错 | NAS 目录里没有 `config.json` **文件**，单文件挂载被 Docker 弄成了空目录 | 确认该路径下 config.json 存在；没有就从 `config.example.json` 复制改名 |
| 连接测试失败 | alist 地址/账号密码不对，或 alist 没在跑 | 回【① 连接】核对 `http://192.168.1.100:5244/dav` |
| 页面目录树空白 | 配置没保存成功 | 重新走 连接→测试→保存 |
| 任务大量 HTTP 500 | **目标位置已有同名文件**（不是瞬时故障！百度网盘 API 不支持覆盖式移动，目标同名时返回 `errno=12`，alist 包装成 500，`Overwrite: T` 无效） | ①「自动改名」策略：引擎会自动编号 `(1)(2)…` 绕开，不会报错；②「覆盖」策略：引擎走备份式覆盖（先备份让位再移入，失败自动回滚）；③ 若要保留两份，用「自动改名」重跑即可 |
| 任务跑了几小时没完 | 2.32TB / 21,603 目录扫描本身就要很久 | 属正常，日志框进度条/流动条在动就没问题 |
| 页面样式乱（像裸 HTML） | 之前版本样式从 CDN 拉，NAS 出不了外网就加载不到 | v1.4 起样式已内置 `static/vendor/tailwind.js`（离线可用）；若删了它页面会自动回落 CDN。确认上传文件清单里包含 `static/vendor/` 整个目录 |
| 页脚端口和实际访问端口不一样 | 页脚显示的是容器内真实监听端口 | 属正常。compose 左右端口映射不同时（如 `8081:6060`），页脚显示 6060，浏览器仍访问 8081 |
| 日志时间差 8 小时 | 老镜像没装时区数据（UTC） | v1.4 镜像已装 tzdata（默认 Asia/Shanghai），重新构建镜像即可 |
| 改完代码页面没变化 | 只传了文件没重建镜像 | 见第 5 节重新构建 |

---

## 7. CLI 与 Web 并行注意

- CLI（`sh ebook-sort.sh`）和 Web 共用同一 `config.json`，配置互通，不会互相覆盖
- **别同时开两个任务搬同一批文件**：两边同时对同一路径高频 MOVE 容易互相触发 500，
  错开跑（一个跑完再跑另一个）
- 不想让 Web 常驻时：Container Manager → 项目 → 停止即可，配置和日志都在 NAS 目录里，
  下次启动直接恢复
