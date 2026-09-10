#!/bin/sh
# ============================================================================
# pan-organizer 在 NAS(DSM / Container Manager) 上的一键运行包装
#
# 用法：sh pan-organizer-nas.sh <pan-organizer 参数...>
# 例：
#   sh pan-organizer-nas.sh check
#   sh pan-organizer-nas.sh extsort --path /百度网盘/下载 --dest /百度网盘/归档 --apply
#
# 行为：
#   首次运行自动 docker pull python:3.12-slim 并创建常驻容器 pan-organizer
#   （挂载本脚本所在目录到 /app），之后每次只是 docker exec 进容器执行。
#   容器设为 --restart unless-stopped，NAS 重启后自动拉起。
#
# 前提：
#   1) DSM 已安装 Container Manager（docker 可用）
#   2) alist 容器正在运行，且 config.json 的 base_url 能访问到它
# ============================================================================

DIR="$(cd "$(dirname "$0")" && pwd)"

# 1) 容器不存在则创建（拉镜像 + 常驻 sleep 容器）
if ! docker ps -a --format '{{.Names}}' | grep -qx pan-organizer; then
    echo "[init] 首次运行：拉取 python:3.12-slim 并创建 pan-organizer 常驻容器…"
    docker pull python:3.12-slim
    docker run -d --name pan-organizer --restart unless-stopped \
        -v "$DIR":/app -w /app \
        python:3.12-slim sleep infinity
fi

# 2) 容器停着则拉起
if [ "$(docker inspect -f '{{.State.Running}}' pan-organizer 2>/dev/null)" != "true" ]; then
    docker start pan-organizer >/dev/null
fi

# 3) 进容器执行 pan-organizer，参数原样透传
docker exec pan-organizer python /app/pan_organizer.py "$@"
