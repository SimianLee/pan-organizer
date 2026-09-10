#!/bin/sh
# ============================================================================
# 电子书整理：/百度网盘-小号/亚马逊电子书 → 按后缀归档到 /百度网盘-小号/电子书
#
# 用法（在 NAS 上，工具目录内）：
#   sh ebook-sort.sh             # 预览（只读，不动文件）
#   sh ebook-sort.sh --apply     # 真正执行移动
#   sh ebook-sort.sh check       # 列出 alist 所有可用挂载点，核对实际 WebDAV 路径
#
# 原理：用 docker run --rm 起一次性 python:3.12-slim 容器执行 pan_organizer.py，
#       跑完容器自动销毁，不常驻、不占资源（J1900 友好）。
# 前提：config.json 已填好 alist 账号密码。
# ============================================================================

SRC="/百度网盘-小号/亚马逊电子书"
DST="/百度网盘-小号/电子书"

DIR="$(cd "$(dirname "$0")" && pwd)"

# 支持子命令：check / scan / run，缺省为 extsort
CMD="extsort"
if [ -n "$1" ] && { [ "$1" = "check" ] || [ "$1" = "scan" ] || [ "$1" = "run" ]; }; then
    CMD="$1"
    shift
fi

if [ "$CMD" = "check" ]; then
    docker run --rm \
        -v "$DIR":/app -w /app \
        -e PYTHONUNBUFFERED=1 \
        --add-host host.docker.internal:192.168.1.100 \
        python:3.12-slim \
        python -u pan_organizer.py check
else
    docker run --rm \
        -v "$DIR":/app -w /app \
        -e PYTHONUNBUFFERED=1 \
        --add-host host.docker.internal:192.168.1.100 \
        python:3.12-slim \
        python -u pan_organizer.py "$CMD" --path "$SRC" --dest "$DST" "$@"
fi
