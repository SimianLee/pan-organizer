# pan-organizer-web Dockerfile
# 网盘自动整理工具 · 带 Web UI 的版本

FROM python:3.12-slim

LABEL maintainer="pan-organizer" \
      description="网盘自动整理工具 · Web UI 版本" \
      version="1.6.2"

# 时区：官方 python:slim 镜像里没有 tzdata，compose 里设了 TZ=Asia/Shanghai 也不会生效，
# 日志时间戳会是 UTC（比本地早 8 小时），排查问题时会误判时间。
# 装上 tzdata 并把默认时区落盘，容器日志 / run-*.log 时间戳即与本地一致。
ARG TZ=Asia/Shanghai
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# 减少镜像体积：不安装额外系统包
# pip 缓存清理
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 工作目录
WORKDIR /app

# 先装依赖（利用 Docker 缓存层）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目源码
# 注意：*.py 用通配而不是逐个列名 —— app/ 下每加一个模块（如 book_online.py）
# 若忘了补 COPY，容器会在 import 时直接崩，通配可一劳永逸避免这种漏拷贝。
COPY *.py            ./
COPY templates/      ./templates/
COPY static/         ./static/

# 日志/配置目录（可挂载）
RUN mkdir -p /app/logs

# 默认参数
ENV PAN_ORGANIZER_PORT=6060 \
    PAN_ORGANIZER_HOST=0.0.0.0

EXPOSE 6060

# 健康检查
# 每次探测都是冷启动一个 Python 解释器（slim 无 curl/wget），J1900 上启动即需 1~3s，
# 整理任务跑着时 CPU/IO 争抢会更慢 —— timeout 5s 必然周期性超时（DSM 显示
# "Health check exceeded timeout (5s)"）。三管齐下：
#   1) urlopen 自带 timeout=10，HTTP 慢时主动断，绝不超过预算；
#   2) HEALTHCHECK timeout 放宽到 15s（>= 解释器启动 + urlopen 预算）；
#   3) start-period=30s，容器刚起、Flask 还在初始化时不算失败。
HEALTHCHECK --interval=30s --timeout=15s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; \
print(urllib.request.urlopen('http://localhost:${PAN_ORGANIZER_PORT}/api/health', timeout=10).status)" \
    || exit 1

# 启动
CMD ["sh", "-c", "python web.py --host ${PAN_ORGANIZER_HOST} --port ${PAN_ORGANIZER_PORT}"]