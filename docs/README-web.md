# pan-organizer-web 部署说明

## 目录结构

```
netdisk-sorter/
├── pan_organizer.py            # CLI 主程序（已被 web.py 复用）
├── web.py                 # Flask Web 后端（任务管理 + SSE）
├── templates/
│   └── index.html         # 单页前端
├── static/
│   └── app.js             # 前端逻辑
├── config.json            # alist 连接配置（首次启动自动生成）
├── requirements.txt       # Flask 依赖
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # 一键启动
└── logs/                  # 历史任务日志
```

## 快速开始（Docker）

```bash
# 1. 构建镜像
docker build -t pan-organizer-web .

# 2. 启动（端口 6060，挂载配置/日志便于持久化）
docker run -d \
    --name pan-organizer-web \
    -p 6060:6060 \
    -v $(pwd)/config.json:/app/config.json \
    -v $(pwd)/logs:/app/logs \
    pan-organizer-web

# 3. 浏览器访问 http://NAS_IP:6060
```

或者用 docker-compose：

```bash
docker compose up -d
```

## 快速开始（直接跑 Python）

```bash
# 安装依赖
pip install -r requirements.txt

# 启动
python web.py --port 6060

# 浏览器访问 http://localhost:6060
```

## 在 NAS（DSM）上部署

把整个 `netdisk-sorter/` 目录拷贝到 NAS，例如 `/volume1/docker/pan-organizer/`。

**方式一：Container Manager**
1. Container Manager → 项目 → 新增 → 选择 `docker-compose.yml`
2. 启动

**方式二：SSH**
```sh
cd /volume1/docker/pan-organizer
docker compose up -d
```

**方式三：青龙面板**
青龙容器里直接 `python /data/pan-organizer/web.py`（需要挂载项目目录）

## 使用流程

1. **打开浏览器**：访问 `http://NAS_IP:6060`
2. **【① 连接】配置**：
   - 填 alist WebDAV 地址（如 `http://192.168.1.100:5244/dav`）
   - 填账号密码
   - 点【测试连接】看输出，能列出挂载点就 OK
   - 点【保存配置】
3. **【② 路径】选择**：
   - 左侧树形选源目录（必填）
   - 右侧树形选目标目录（留空则在源目录内按后缀建子夹）
4. **【③ 规则】勾选**（可多选，目标目录按勾选顺序**嵌套拼接**）：
   - 按后缀归档 `extsort`（默认勾选）：`.pdf → pdf/` 文件夹
   - 按扩展名大类别归档 `category`：图片/视频/音频/文档/压缩包/代码（多后缀合一目录）
   - 图书归类 `booksort`：图书/漫画按**书名**归入 漫画/教材教辅/计算机IT/医学养生/心理学/
     历史传记/经济管理/法律/哲学宗教/外语学习/少儿绘本/文学小说/生活百科/其它图书；
     **非图书文件原地不动**（不是"搬错"，是设计如此），可与 by_date 嵌套
   - 图书联网补全 `bookonline`：**需与 booksort 同用**。本地书名没有类型线索的
     （《活着》《围城》）会联网查类型再归类；只查 `其它图书` 那部分，结果缓存在
     `data/online_cache.json`（同一本书只查一次）；网络异常只降级不中断任务。
     勾选后下方出现**联网设置**：数据源（auto/当当/LLM）、并发、单次上限、
     LLM 接口（base_url/model/api_key），以及**试查**框——输入书名立刻看到它会被归到哪一类
   - 按修改日期归档 `by_date`：追加 `YYYY-MM` 子目录（如 `视频/2026-01/`）
   - 按文件大小归档 `by_size`：追加 `小于10MB/10-100MB/100MB-1GB/大于1GB` 子目录
   - 跳过未完成文件 `skip_incomplete`：默认勾选（`.part/.tmp/.crdownload/.!qb`）；取消后这些也会被归档
   - 正则匹配 `regex_match`：勾选后填写下方**正则表达式**，只整理文件名匹配的文件
   - 清理空目录 `cleanup_empty`：整理完后删除源目录里的空文件夹
   - 「重复文件检测」与「定时任务」仍为**规划中**（带 amber 标签置灰，不可勾选）
   - 撞名策略：**自动改名 (1)(2)…**（默认，绝不覆盖）/ **跳过**（撞名不移动）/ **覆盖**（用源文件替换目标同名文件）
   - 选「覆盖」时采用**备份式覆盖**：目标原文件先改名为 `xxx.__bak_<时间戳>` 让位 → 移入源文件 →
     成功即删备份；**中途失败自动回滚**（目标恢复原样）。原因：百度网盘等后端**不支持覆盖式 MOVE**
     （目标同名时服务端返回 `errno=12`，alist 包装成 HTTP 500，`Overwrite: T` 请求头无效），
     只能由客户端拆成服务端支持的步骤完成，全程不丢文件
5. **【④ 执行与日志】**：三种运行方式
   - 【查询(预览)】：只扫描网盘并生成一份整理计划（`plan-*.json`），**不移动任何文件**
   - 【▶ 查询并移动】：扫描 + 预览 + 直接移动（原"开始整理"），同时也会生成一份计划
   - 【按计划移动】：先用右侧下拉选一份历史计划，再点此按钮——**跳过重复扫描**，
     直接按计划里的落点移动（适合"查询后人工确认过再执行"，与 CLI `--from-plan` 同机制）
   - 下方折叠的 **📋 计划管理**：列出所有 `plan-*.json`，每行提供
     **应用**（填到下拉）/ **查看**（弹窗显示 JSON）/ **下载**（保存为本地 JSON）/
     **删除**（彻底删，下拉也会清空）
   - 计划下拉**默认不选**，避免误点「按计划移动」用了错的计划；任务完成后自动刷新
   - 实时日志在下方滚动；需要中断点【■ 停止】
   - 任务结束后日志自动归档到"历史日志"

## API 文档

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/`                     | Web UI 主页 |
| GET  | `/api/health`           | 健康检查 |
| GET  | `/api/config`           | 读 alist 配置（密码不回显） |
| POST | `/api/config`           | 保存 alist 配置 |
| POST | `/api/test`             | 测试连接（调 pan-organizer check） |
| GET  | `/api/mounts`           | 列挂载点 |
| GET  | `/api/tree?path=X`      | 懒加载一层子目录 |
| POST | `/api/run`              | 启动任务。body 带 `mode`：`query`（只查导出计划）/ `query_move`（查询并移动）/ `move`（按 `plan` 字段指定的计划执行） |
| POST | `/api/stop`             | 停止任务 |
| GET  | `/api/status`           | 当前任务状态 |
| GET  | `/api/logs/stream`      | SSE 实时日志流 |
| GET  | `/api/logs`             | 历史日志列表 |
| GET  | `/api/logs/<name>`      | 读某条历史日志 |
| DELETE | `/api/logs/<name>`    | 删除某条历史日志 |
| GET  | `/api/plans`            | 历史计划列表（`plan-*.json`，含 meta.src/count 供下拉展示） |
| GET  | `/api/plans/<name>`     | 读某条计划全文 |
| DELETE | `/api/plans/<name>`   | 删除某条计划 |
| GET  | `/api/rules`            | 预定义规则清单 |
| GET  | `/api/online/defaults`  | 联网补全的默认配置 + 数据源选项 + 类型清单（表单预填） |
| POST | `/api/online/test`      | 试查一个书名：body `{title, provider?}` → 命中类型/数据源/远端分类/耗时；不写缓存 |

## 配置示例（config.json）

```json
{
  "alist": {
    "base_url": "http://192.168.1.100:5244/dav",
    "username": "admin",
    "password": "your-password",
    "timeout": 30
  },
  "options": {
    "on_conflict": "skip",
    "exclude_dirs": []
  }
}
```

## 故障排查

| 现象 | 排查 |
|------|------|
| 测试连接失败 | 检查 alist 地址/端口、账号密码、alist 服务是否在跑 |
| 树形目录空白 | 先回【① 连接】测试通过 |
| 任务一直 running | 看实时日志有没有 `[扫描中]` 进度输出；2TB 目录可能扫几小时 |
| 实时日志无输出 | 看 Container Manager 日志确认 pan-organizer 进程在跑；可能是 PYTHONUNBUFFERED 没生效 |
| 大量 HTTP 500 | 百度网盘对特殊字符路径不稳，已内置 5xx 自动重试 3 次；查看 alist 日志确认 |
| 页面样式乱（像裸 HTML） | 样式库已本地内置 `static/vendor/tailwind.js`，离线也正常；若手动删了它，页面会自动回落官方 CDN（需要外网） |
| 日志时间差 8 小时 | 镜像已装 tzdata（默认 Asia/Shanghai）；老镜像请重建，或 compose 里设 `TZ=Asia/Shanghai` |
| Docker 镜像大 | 用了 `python:3.12-slim`，约 150MB；不要换 alpine（已知 stdlib bug） |

## 日志怎么读（v1.4 起全行带时间戳，可精准定位问题）

一次任务按顺序会出现这些关键行，报障时把对应段落截图/复制即可：

| 日志内容 | 含义 |
|---|---|
| `启动 · …`（任务头） | 版本号、配置文件、完整命令行、挂载点、撞名策略、启用的规则——复现问题所需信息都在这 |
| `[扫描中]` / `[进度] 已完成 N%` | 扫描进度（Web 进度条就靠它驱动） |
| `[移动] 路径` | 单个文件移动成功 |
| `[失败] 路径 + 原因` | 单个文件失败，原因直接写在后面 |
| `[失败明细]` | 收尾时的失败分类表 + 明细清单（网络/5xx/4xx/目标目录创建失败…） |
| `[执行汇总] 共 N 个 ｜ 成功 X ｜ 失败 Y ｜ 跳过 Z ｜ 耗时 T ｜ 撞名策略 R` | 一行总账，方便 grep / 截图 |
| `任务尾 exit=…` | 退出码与收尾指引；dry-run 会提示「以上为预览，未做任何修改」 |

## v2 状态（已实现 / 规划中）

**已实现（引擎 + Web 全链路，规则页可勾选，目标目录按顺序嵌套拼接）：**
- 按扩展名大类别归档（图片/视频/音频/文档/压缩包/代码）
- 按修改日期 YYYY-MM 归档
- 按文件大小 4 档归档（小于10MB / 10-100MB / 100MB-1GB / 大于1GB）
- 正则匹配（只整理文件名匹配正则的文件）
- 空目录清理（整理后删除源目录空文件夹）
- 跳过未完成文件开关（取消勾选后 `.part/.tmp` 也会归档）
- 图书漫画归类 `booksort`（按书名识别 13 类 + 其它图书 兜底，非图书不动）
- 图书联网补全 `bookonline`（只补本地判不出的那部分，缓存 + 降级 + 熔断，支持 LLM）

**规划中（规则页带 amber 标签置灰，不可勾选）：**
- 重复文件检测（按 hash，需下载文件算 MD5，耗资源）
- 定时任务（cron 表达式常驻调度）
- 登录鉴权（如果需要）