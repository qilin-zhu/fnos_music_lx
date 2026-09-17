#!/usr/bin/env bash
# 宿主机模式启动 lxsource-service（不需要 Docker）。
#
# 用法：
#   ./run-local.sh                 # 后台常驻（nohup）
#   ./run-local.sh --foreground    # 前台运行，便于观察日志
#
# 配置来源（优先级从高到低）：
#   1. 已导出的环境变量
#   2. 仓库根目录的 .env.lxsource.local（已 gitignore，建议 chmod 600）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
LOG_FILE="${LXSOURCE_LOG:-/tmp/lxsource-service.log}"
PID_FILE="${LXSOURCE_PID:-/tmp/lxsource-service.pid}"

# 载入本地配置（含订阅密钥，不入库）
LOCAL_ENV="$ROOT/.env.lxsource.local"
if [ -f "$LOCAL_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$LOCAL_ENV"
    set +a
fi

export LXSOURCE_PORT="${LXSOURCE_PORT:-8774}"
export LXSOURCE_HOST="${LXSOURCE_HOST:-127.0.0.1}"
export LXSOURCE_REFRESH_S="${LXSOURCE_REFRESH_S:-3600}"

if [ -z "${LX_SUBSCRIPTIONS:-}" ]; then
    echo "[run-local] 警告：LX_SUBSCRIPTIONS 为空，服务会启动但所有解析返回 404" >&2
fi

command -v node >/dev/null 2>&1 || { echo "[run-local] 未找到 node" >&2; exit 1; }

start_foreground() {
    exec node "$HERE/server.js"
}

start_background() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[run-local] 已在运行 (pid $(cat "$PID_FILE"))；先停止再启动" >&2
        exit 1
    fi
    # setsid + nohup：脱离当前会话，SSH 断开后仍存活
    setsid nohup node "$HERE/server.js" >"$LOG_FILE" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
    sleep 2

    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[run-local] 启动失败，日志尾部：" >&2
        tail -n 20 "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE"
        exit 1
    fi

    echo "[run-local] 已启动 pid=$(cat "$PID_FILE") 端口=${LXSOURCE_PORT}"
    echo "[run-local] 日志：${LOG_FILE}"

    # 就绪探测
    for _ in $(seq 1 20); do
        if curl -sf --max-time 2 "http://${LXSOURCE_HOST}:${LXSOURCE_PORT}/healthz" >/dev/null 2>&1; then
            curl -s "http://${LXSOURCE_HOST}:${LXSOURCE_PORT}/healthz" || true
            echo
            return 0
        fi
        sleep 1
    done
    echo "[run-local] 警告：健康检查未在超时内通过" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    return 1
}

stop_service() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
            echo "[run-local] 已停止 pid=$pid"
        fi
        rm -f "$PID_FILE"
    else
        echo "[run-local] 未找到 pid 文件"
    fi
}

case "${1:-}" in
    --foreground|-f) start_foreground ;;
    --stop)          stop_service ;;
    --restart)       stop_service; start_background ;;
    ""|--start)      start_background ;;
    *) echo "用法: $0 [--start|--stop|--restart|--foreground]" >&2; exit 2 ;;
esac
