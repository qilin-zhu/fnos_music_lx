#!/usr/bin/env bash
# lxsource 订阅音源一键部署脚本。
#
# 自动完成：
#   1. 校验前置条件（订阅配置、node/docker 可用性）
#   2. 备份并应用 lxmusic-service 补丁（幂等：已应用则跳过）
#   3. 按模式启动订阅服务（docker compose 或 宿主机常驻）
#   4. 端到端验收（订阅服务解析 + lxmusic 能力上报）
#
# 用法：
#   ./deploy.sh --mode host              # 宿主机模式（无需 docker 权限，推荐本机）
#   ./deploy.sh --mode docker            # docker compose 模式
#   ./deploy.sh --mode docker --hotpatch # 用挂载覆盖 app.py，避免重建镜像
#   ./deploy.sh --check                  # 仅做验收检查，不修改任何东西
#
# 订阅源管理：
#   ./deploy.sh --list-subscriptions             # 查看当前订阅（密钥已脱敏）
#   ./deploy.sh --add-subscription 'name|URL'    # 追加订阅并重建服务
#   ./deploy.sh --remove-subscription NAME       # 删除订阅并重建服务
#   ./deploy.sh --set-subscriptions 'a|URL,b|URL'# 整组替换并重建服务
#   ./deploy.sh --refresh-subscriptions          # 让运行中的服务立即重新拉取脚本
#   （加 --no-redeploy 可只改配置文件、不重建容器）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"
PATCH="$ROOT/patches/lxmusic-subscription.patch"
APP="$ROOT/lxmusic-service/app.py"
LOCAL_ENV="$ROOT/.env.lxsource.local"
SUBENV_TOOL="$ROOT/lxsource-service/subscriptions_env.py"
LXMUSIC_HEALTH="${LXMUSIC_HEALTH:-http://127.0.0.1:8772/healthz}"
# 订阅服务的验收地址：优先显式 LXMUSIC/…，否则跟随宿主机绑定端口
LXSOURCE_HEALTH="${LXSOURCE_HEALTH:-}"

MODE=""
HOTPATCH=0
CHECK_ONLY=0
SUB_ACTION=""
SUB_ARG=""
NO_REDEPLOY=0
REDEPLOY_MODE=""

c_red()  { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

# 取下一个参数作为选项值；缺失时报错退出（避免 shift 失败导致的死循环）
need_value() {
    if [ "$#" -lt 2 ]; then
        c_red "选项 $1 缺少参数"
        usage
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) need_value "$@"; MODE="$2"; shift 2 ;;
        --hotpatch) HOTPATCH=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        --list-subscriptions) SUB_ACTION="list"; shift ;;
        --add-subscription) need_value "$@"; SUB_ACTION="add"; SUB_ARG="$2"; shift 2 ;;
        --remove-subscription) need_value "$@"; SUB_ACTION="remove"; SUB_ARG="$2"; shift 2 ;;
        --set-subscriptions) need_value "$@"; SUB_ACTION="set"; SUB_ARG="$2"; shift 2 ;;
        --refresh-subscriptions) SUB_ACTION="refresh"; shift ;;
        --no-redeploy) NO_REDEPLOY=1; shift ;;
        -h|--help) usage ;;
        *) c_red "未知参数: $1"; usage ;;
    esac
done

[ -z "$MODE" ] && MODE="host"

# ---------------------------------------------------------------- 工具函数
load_local_env() {
    if [ -f "$LOCAL_ENV" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$LOCAL_ENV"
        set +a
    fi
}

# 解析订阅服务的验收地址。
# 宿主机模式用 LXSOURCE_PORT；docker 模式用宿主侧映射端口 LXSOURCE_BIND_PORT。
resolve_lxsource_health() {
    if [ -n "${LXSOURCE_HEALTH:-}" ]; then
        return 0
    fi
    local host_port
    if [ "$MODE" = "docker" ]; then
        host_port="${LXSOURCE_BIND_PORT:-${LXSOURCE_PORT:-8774}}"
    else
        host_port="${LXSOURCE_PORT:-8774}"
    fi
    LXSOURCE_HEALTH="http://127.0.0.1:${host_port}/healthz"
}

# 输出订阅摘要，但绝不打印密钥（key=/token= 等参数一律脱敏）
redact() {
    printf '%s' "${1:-}" | sed -E 's#((key|token|apikey|api_key|password|secret)=)[^&, ]+#\1***#Ig'
}

subscription_summary() {
    if [ -z "${LX_SUBSCRIPTIONS:-}" ]; then
        printf '未设置'
        return
    fi
    local n
    n="$(printf '%s' "$LX_SUBSCRIPTIONS" | tr ',' '\n' | grep -c . || true)"
    printf '%s 项（%s）' "${n:-0}" "$(redact "$LX_SUBSCRIPTIONS")"
}

patch_applied() {
    grep -q '_chain_subscription' "$APP" 2>/dev/null
}

apply_patch() {
    step "应用 lxmusic-service 补丁"
    if patch_applied; then
        c_grn "  已应用，跳过"
        return 0
    fi
    if [ ! -f "$PATCH" ]; then
        c_red "  找不到补丁：$PATCH"; return 1
    fi
    local ts backup
    ts="$(date +%Y%m%d%H%M%S)"
    backup="$APP.pre-lxsource.$ts"
    if ! cp "$APP" "$backup" 2>/dev/null; then
        c_red "  无法备份 $APP（权限不足？需 root 或对目录有写权限）"; return 1
    fi
    c_grn "  已备份 -> $(basename "$backup")"

    if ! patch -p1 --dry-run --directory="$ROOT" < "$PATCH" >/dev/null 2>&1; then
        c_red "  补丁无法干净应用（app.py 可能已被其它修改）"; return 1
    fi
    if ! patch -p1 --directory="$ROOT" < "$PATCH"; then
        c_red "  补丁应用失败，正在回滚"
        cp "$backup" "$APP"
        return 1
    fi
    if ! python3 -m py_compile "$APP" 2>/dev/null; then
        c_red "  补丁后语法检查失败，正在回滚"
        cp "$backup" "$APP"
        return 1
    fi
    c_grn "  补丁已应用且语法校验通过"
}

start_host() {
    step "启动 lxsource-service（宿主机模式）"
    if ! command -v node >/dev/null 2>&1; then
        c_red "  未找到 node，无法使用宿主机模式"; return 1
    fi
    local port="${LXSOURCE_PORT:-8774}"
    # 宿主机模式与 docker 模式抢同一个 8774：先停掉容器，避免端口冲突。
    stop_docker_lxsource
    if [ "${LXSOURCE_HOST:-127.0.0.1}" = "127.0.0.1" ]; then
        c_ylw "  LXSOURCE_HOST=127.0.0.1：只有宿主机上的 lxmusic 能访问。"
        c_ylw "  若 lxmusic 是 Docker 容器，请改用 --mode docker，或设 LXSOURCE_HOST=0.0.0.0 并配置 LX_TOKEN。"
    fi
    "$ROOT/lxsource-service/run-local.sh" --restart 2>&1 | sed 's/^/  /'
    return "${PIPESTATUS[0]}"
}

# 检查端口是否被「非本方案」的进程占用，并给出可执行的处置建议。
port_owner_hint() {
    local port="$1"
    local line
    line="$(ss -tlnp 2>/dev/null | grep ":${port} " | head -1)"
    if [ -z "$line" ]; then
        return 1   # 端口空闲
    fi
    local pid
    pid="$(printf '%s' "$line" | grep -oP 'pid=\K[0-9]+' | head -1)"
    if [ -n "$pid" ]; then
        printf '%s' "pid=${pid} $(ps -o comm= -p "$pid" 2>/dev/null)"
    else
        printf '%s' "被其他用户/容器占用（当前用户无权限查看 PID）"
    fi
    return 0
}

# 停掉 docker 模式的 lxsource 容器（宿主机模式启动前调用，避免 8774 冲突）
stop_docker_lxsource() {
    command -v docker >/dev/null 2>&1 || return 0
    docker info >/dev/null 2>&1 || return 0   # 无权限则跳过，交给端口检查提示
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'fnmusic-lxsource'; then
        c_ylw "  检测到 docker 模式的 fnmusic-lxsource 正在运行，先停止以释放端口"
        docker stop fnmusic-lxsource >/dev/null 2>&1 && c_grn "  已停止 fnmusic-lxsource 容器"
    fi
}

start_docker() {
    step "启动 lxsource + lxmusic（docker compose）"
    if ! command -v docker >/dev/null 2>&1; then
        c_red "  未找到 docker"; return 1
    fi
    if ! docker info >/dev/null 2>&1; then
        c_red "  无法访问 Docker daemon（当前用户不在 docker 组，且无 sudo 免密）"
        c_red "  请用有权限的账号执行，或改用 --mode host"
        return 1
    fi

    # docker 模式与宿主机模式抢同一个 8774：先停掉宿主机实例，避免端口冲突。
    if [ -f /tmp/lxsource-service.pid ] || ss -tln 2>/dev/null | grep -q ":${LXSOURCE_PORT:-8774} "; then
        if [ -f /tmp/lxsource-service.pid ]; then
            c_ylw "  检测到宿主机模式的 lxsource 正在运行，先停止以释放端口"
            "$ROOT/lxsource-service/run-local.sh" --stop 2>&1 | sed 's/^/  /'
        fi
    fi

    # 宿主机侧映射端口冲突时自动让路（容器间仍走服务名 lxsource:8774，不受影响）
    local bind_port="${LXSOURCE_BIND_PORT:-8774}"
    local owner
    if owner="$(port_owner_hint "$bind_port")"; then
        local alt=""
        for cand in 18774 28774 38774 48774; do
            if ! port_owner_hint "$cand" >/dev/null; then alt="$cand"; break; fi
        done
        if [ -n "$alt" ]; then
            c_ylw "  宿主机端口 ${bind_port} 已被占用（${owner}）"
            c_ylw "  自动改用 ${alt}（容器内仍是 8774，lxmusic 走服务名不受影响）"
            export LXSOURCE_BIND_PORT="$alt"
            bind_port="$alt"
        else
            c_red "  宿主机端口 ${bind_port} 被占用（${owner}），且未找到可用替代端口"
            c_red "  请释放该端口，或设 LXSOURCE_BIND_PORT 指定其它端口"
            return 1
        fi
    fi
    c_ylw "  宿主机验收端口：${bind_port}（容器内 8774）"

    local compose_files=(-f "$ROOT/docker-compose.yml")
    if [ "$HOTPATCH" = "1" ]; then
        compose_files+=(-f "$ROOT/docker-compose.hotpatch.yml")
        c_ylw "  使用热补丁挂载（跳过镜像重建）"
    else
        c_ylw "  将重建 lxmusic 镜像（需要能拉取基础镜像与 pip 依赖）"
    fi

    # compose 默认只读 .env（可能不可读或缺少订阅项），显式追加订阅配置。
    COMPOSE_ARGS=()
    compose_env_args

    if [ "$HOTPATCH" = "0" ]; then
        ( cd "$ROOT" && docker compose "${COMPOSE_ARGS[@]}" "${compose_files[@]}" build lxmusic lxsource ) || {
            c_red "  镜像构建失败"; return 1; }
    fi
    ( cd "$ROOT" && docker compose "${COMPOSE_ARGS[@]}" "${compose_files[@]}" up -d lxmusic lxsource ) || {
        c_red "  compose 启动失败"; return 1; }
    c_grn "  已启动"
}

# 把 .env.lxsource.local 的订阅变量注入 compose 环境。
# compose 默认只读 .env（root 600，安装脚本生成），不会包含订阅配置，
# 因此这里显式把本文件导出，供 compose 变量替换使用。
compose_env_args() {
    if [ -f "$LOCAL_ENV" ]; then
        COMPOSE_ARGS+=(--env-file "$LOCAL_ENV")
    fi
    if [ -f "$ROOT/.env" ] && [ -r "$ROOT/.env" ]; then
        COMPOSE_ARGS+=(--env-file "$ROOT/.env")
    fi
}

# ------------------------------------------------------------ 订阅管理
# 读写 .env.lxsource.local 里的 LX_SUBSCRIPTIONS。
# 交给独立 Python 工具处理：保留注释/其它键、原子写入、密钥脱敏、shell 安全转义。
subenv() {
    if [ ! -f "$SUBENV_TOOL" ]; then
        c_red "  找不到订阅配置工具：$SUBENV_TOOL"; return 1
    fi
    python3 "$SUBENV_TOOL" "$@" --file "$LOCAL_ENV"
}

# lxsource 容器当前是跑在 docker 还是宿主机？据此决定重建方式。
detect_redeploy_mode() {
    if [ -n "$REDEPLOY_MODE" ]; then
        return 0
    fi
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 \
       && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'fnmusic-lxsource'; then
        REDEPLOY_MODE="docker"
    elif [ -f /tmp/lxsource-service.pid ]; then
        REDEPLOY_MODE="host"
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 \
         && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'fnmusic-lxsource'; then
        REDEPLOY_MODE="docker"   # 容器存在但已停止
    else
        REDEPLOY_MODE="docker"   # 默认按 docker（部署文档主推形态）
    fi
}

# 重建订阅服务，让新的 LX_SUBSCRIPTIONS 生效。
# 注意：订阅列表是「启动时注入的环境变量」，改配置文件后必须重建，热刷新无法新增/删除订阅。
redeploy_lxsource() {
    if [ "$NO_REDEPLOY" = "1" ]; then
        c_ylw "  --no-redeploy：仅写入配置，未重建服务"
        c_ylw "  新订阅要生效需重建：sudo ./deploy.sh --mode docker --hotpatch"
        return 0
    fi
    detect_redeploy_mode
    step "重建订阅服务以应用新订阅（$REDEPLOY_MODE 模式）"
    if [ "$REDEPLOY_MODE" = "host" ]; then
        "$ROOT/lxsource-service/run-local.sh" --restart 2>&1 | sed 's/^/  /'
        return "${PIPESTATUS[0]}"
    fi
    # docker：只重建/重启 lxsource，不动 lxmusic
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        c_red "  无 Docker 权限，无法重建容器"
        c_red "  请用 sudo 重新执行，或先 export LX_SUBSCRIPTIONS 后手动 compose up"
        return 1
    fi
    local args=()
    compose_env_args
    args=("${COMPOSE_ARGS[@]}")
    # 宿主机模式实例若占着端口，先停掉，避免绑定冲突
    if [ -f /tmp/lxsource-service.pid ]; then
        c_ylw "  检测到宿主机模式实例，先停止以释放端口"
        "$ROOT/lxsource-service/run-local.sh" --stop 2>&1 | sed 's/^/  /'
    fi
    ( cd "$ROOT" && docker compose "${args[@]}" -f docker-compose.yml up -d --force-recreate lxsource ) || {
        c_red "  lxsource 重建失败"; return 1; }
    c_grn "  已重建 fnmusic-lxsource"
}

# 让运行中的服务立刻重新拉取订阅脚本（脚本内容变更场景，无需重建）
refresh_subscriptions() {
    step "触发订阅服务重新拉取脚本"
    local base="${LXSOURCE_HEALTH%/healthz}"
    local resp
    if ! resp="$(curl -s --max-time 30 -X POST "${base}/api/v1/subscriptions/refresh" 2>/dev/null)"; then
        c_red "  无法访问 ${base}（服务未运行？）"; return 1
    fi
    printf '%s' "$resp" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("  响应无法解析"); raise SystemExit(1)
if not d.get("ok"):
    print("  刷新失败:", str(d.get("error"))[:120]); raise SystemExit(1)
for s in (d.get("data") or {}).get("subscriptions", []):
    name = s.get("name") or "?"
    status = s.get("status") or "?"
    h = str(s.get("hash") or "")[:8]
    print("  %-16s %-6s hash=%s" % (name, status, h))
' || return 1
}

manage_subscriptions() {
    case "$SUB_ACTION" in
        list)
            step "当前订阅"
            subenv list || return 1
            echo "  配置文件: $LOCAL_ENV"
            echo "  说明: 改订阅后需重建服务才生效；仅脚本内容更新可直接 --refresh-subscriptions"
            return 0
            ;;
        add)
            if [ -z "$SUB_ARG" ]; then c_red "  --add-subscription 需要参数 'name|URL'"; return 2; fi
            step "添加订阅"
            subenv add --entry "$SUB_ARG" || return 1
            subenv list
            redeploy_lxsource || return 1
            ;;
        remove)
            if [ -z "$SUB_ARG" ]; then c_red "  --remove-subscription 需要订阅名"; return 2; fi
            step "删除订阅"
            subenv remove --name "$SUB_ARG" || return 1
            subenv list
            redeploy_lxsource || return 1
            ;;
        set)
            if [ -z "$SUB_ARG" ]; then c_red "  --set-subscriptions 需要参数 'a|URL,b|URL'"; return 2; fi
            step "替换全部订阅"
            subenv set --value "$SUB_ARG" || return 1
            subenv list
            redeploy_lxsource || return 1
            ;;
        refresh)
            refresh_subscriptions || return 1
            return 0
            ;;
        *)
            c_red "  未知订阅操作: $SUB_ACTION"; return 2
            ;;
    esac

    sleep 3
    # 验收地址要跟随「实际在跑」的形态，而不是默认值
    detect_redeploy_mode
    [ "$REDEPLOY_MODE" = "docker" ] && MODE="docker"
    LXSOURCE_HEALTH=""      # 清掉按旧 MODE 算出的地址，重新推导
    resolve_lxsource_health
    verify
}

verify() {
    step "验收检查"
    local rc=0

    # 1) 订阅服务
    printf '  [1/4] 订阅服务 '
    local sub
    if sub="$(curl -sf --max-time 6 "$LXSOURCE_HEALTH" 2>/dev/null)"; then
        local ready total
        ready="$(printf '%s' "$sub" | python3 -c 'import json,sys;print(json.load(sys.stdin)["subscriptions"]["ready"])' 2>/dev/null)"
        total="$(printf '%s' "$sub" | python3 -c 'import json,sys;print(json.load(sys.stdin)["subscriptions"]["total"])' 2>/dev/null)"
        if [ "${ready:-0}" -gt 0 ] 2>/dev/null; then
            c_grn "OK（${ready}/${total} 订阅就绪）"
        else
            c_red "订阅均未就绪（${ready:-?}/${total:-?}）"; rc=1
        fi
    else
        c_red "无法访问 $LXSOURCE_HEALTH"; rc=1
    fi

    # 2) 订阅能力
    printf '  [2/4] 脚本平台 '
    local caps
    if caps="$(curl -sf --max-time 6 "${LXSOURCE_HEALTH%/healthz}/api/v1/capabilities" 2>/dev/null)"; then
        local plats
        plats="$(printf '%s' "$caps" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(",".join(d.get("platforms") or []) or "(无)")' 2>/dev/null)"
        c_grn "$plats"
    else
        c_red "无法获取"; rc=1
    fi

    # 3) lxmusic 能力上报
    printf '  [3/4] lxmusic 链路 '
    local h
    if h="$(curl -sf --max-time 6 "$LXMUSIC_HEALTH" 2>/dev/null)"; then
        printf '%s' "$h" | python3 -c '
import json,sys
d=json.load(sys.stdin)
chains=list((d.get("chains") or {}).keys())
caps=d.get("capabilities") or {}
tx=(caps.get("tx") or {}).get("playback_available")
print("chains=%s | tx可播放=%s" % (",".join(chains) or "(无)", tx))
if "subscription" not in chains:
    print("  \033[33m注意：运行时 chains 未包含 subscription —— 补丁可能尚未生效（docker 需重建镜像或使用 --hotpatch）\033[0m")
' || rc=1
    else
        c_red "无法访问 $LXMUSIC_HEALTH"; rc=1
    fi

    # 4) 端到端连通性：链路"已注册"不等于"可达"。
    #    常见于 lxsource 容器没起来 → 容器内解析不到 lxsource 主机名。
    printf '  [4/4] 订阅链路连通 '
    local probe
    if probe="$(curl -s --max-time 20 "${LXMUSIC_HEALTH%/healthz}/api/v1/track/url?id=lx:tx:0039MnYb0qxYhV&quality=320k" 2>/dev/null)"; then
        if printf '%s' "$probe" | grep -q '"ok":true'; then
            c_grn "OK（已解析出直链）"
        else
            c_red "链路不可达"
            printf '%s' "$probe" | python3 -c 'import json,sys
try:
    e=json.load(sys.stdin).get("error","")
    print("        %s" % e[:140])
    if "Name or service not known" in e:
        print("        → lxsource 容器未运行或不在同一网络；执行 docker compose up -d lxsource")
except Exception: pass' 2>/dev/null
            rc=1
        fi
    else
        c_red "探测失败"; rc=1
    fi

    return $rc
}

# -------------------------------------------------------------------- 主流程
load_local_env
resolve_lxsource_health

# 订阅管理子命令：不走完整部署流程
if [ -n "$SUB_ACTION" ]; then
    if [ "$SUB_ACTION" = "refresh" ]; then
        refresh_subscriptions; exit $?
    fi
    manage_subscriptions; exit $?
fi

step "前置检查"
echo "  模式            : $MODE"
echo "  订阅配置        : $(subscription_summary)"
echo "  补丁状态        : $(patch_applied && echo 已应用 || echo 未应用)"
echo "  app.py          : $APP"
echo "  本地配置        : $([ -f "$LOCAL_ENV" ] && echo "$LOCAL_ENV" || echo '（无）')"

if [ -z "${LX_SUBSCRIPTIONS:-}" ]; then
    c_ylw "  LX_SUBSCRIPTIONS 为空：服务可启动但所有解析将返回 404。"
    c_ylw "  请编辑 $LOCAL_ENV 填写订阅后再执行。"
fi

if [ "$CHECK_ONLY" = "1" ]; then
    verify; exit $?
fi

if [ "$MODE" = "host" ]; then
    apply_patch || exit 1
    start_host || exit 1
elif [ "$MODE" = "docker" ]; then
    apply_patch || exit 1
    start_docker || exit 1
    c_ylw "  Docker 模式下，lxmusic 容器需要能访问 lxsource。"
    c_ylw "  已在 docker-compose.yml 中默认使用 LX_SUBSCRIPTION_URL=http://lxsource:8774（同网络服务名）。"
else
    c_red "未知模式：$MODE（可选 host / docker）"; exit 2
fi

# 等待服务就绪：lxsource 需要先把订阅脚本加载完才监听端口，
# 慢速订阅源可能耗时十几秒，固定 sleep 会误判为失败。
wait_ready() {
    local url="$1" label="$2" tries="${3:-30}" delay="${4:-2}"
    local i
    printf '  等待 %s 就绪' "$label"
    for i in $(seq 1 "$tries"); do
        if curl -sf --max-time 4 "$url" >/dev/null 2>&1; then
            printf ' ✅\n'
            return 0
        fi
        printf '.'
        sleep "$delay"
    done
    printf ' ⚠️ 超时\n'
    return 1
}

# 先等到至少端口开放再验收，避免把启动中的服务判为故障
wait_ready "${LXSOURCE_HEALTH}" "lxsource" 30 2 || true
wait_ready "${LXMUSIC_HEALTH}" "lxmusic" 20 2 || true

if verify; then
    c_grn "\n部署完成。"
else
    c_ylw "\n部署已执行，但部分验收项未通过，请查看上面的提示。"
fi
