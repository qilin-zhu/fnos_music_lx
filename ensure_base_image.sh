#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext Docker 基础镜像源探测 / 保障（v1.2.3+）
# 背景：fnOS 等 NAS 系统通常在 Docker daemon 全局配置镜像加速器（如 docker.fnnas.com），
#       加速器异常（401/超时）时 BuildKit 解析 python:3.13-slim 元数据会失败且不会
#       回退官方源，导致 docker compose up --build 直接失败。
# 本脚本不修改任何系统配置，仅为本应用解析一个「真实可拉取」的基础镜像引用：
#   1. 环境变量 BASE_IMAGE 手动指定 → 仅验证该引用（跳过自动探测）
#   2. .env 已缓存的镜像源引用 FNMUSIC_BASE_IMAGE → 真实拉取验证，可用即沿用
#   3. 国内镜像优先逐个尝试（环境变量 FNMUSIC_DOCKER_MIRRORS 空格分隔可覆盖）：
#      daemon 加速器（如 docker.fnnas.com）只拦截 docker.io 短引用，完整镜像源引用
#      直连对应仓库，绕开故障/限速的加速器，稳定可靠
#   4. 官方 python:3.13-slim 作为最后兜底（走 daemon 加速器链路，国内网络下常慢/不稳）
# 除 Python 外，lxsource 订阅服务基于 Node，其 node:22-alpine 同样需要国内镜像源保障，
# 因此本脚本会分别探测并写入 FNMUSIC_BASE_IMAGE（Python）与 FNMUSIC_NODE_IMAGE（Node）。
# 探测结果经 proxy/env_merge.py 安全增量写入 .env（备份+600 权限），
# docker-compose.yml 的 build.args 自动读取该值；install.sh / extend.sh / 手动重建全部生效。
# 用法: bash ensure_base_image.sh   （由 install.sh / extend.sh 在 docker 模式构建前调用）
# 可调环境变量: BASE_IMAGE / NODE_IMAGE / FNMUSIC_DOCKER_MIRRORS / PULL_TIMEOUT（单次拉取超时秒数，默认 240）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PATH="${BASE_DIR}/.env"
BASE_TAG="python:3.13-slim"
NODE_TAG="node:22-alpine"
DEFAULT_MIRRORS="docker.1ms.run docker.m.daocloud.io docker.1panel.live hub.rat.dev"
PULL_TIMEOUT="${PULL_TIMEOUT:-240}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

# 与 install.sh / extend.sh 同款 docker 访问回退（docker 或 sudo docker）
if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
else
    log_err "Docker daemon 不可用（docker / sudo docker 均无法连接），无法探测基础镜像源。"
    exit 1
fi

# 读取 .env 中已缓存的镜像引用键（只取该键，不打印 .env 其他内容）
# 用法: cached_env_key FNMUSIC_BASE_IMAGE
cached_env_key() {
    local key="$1"
    [ -f "${ENV_PATH}" ] || return 0
    grep -E "^\s*(export\s+)?${key}=" "${ENV_PATH}" 2>/dev/null \
        | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true
}

# 兼容旧调用名
cached_base_image() { cached_env_key FNMUSIC_BASE_IMAGE; }

# 用真实 docker pull 验证（与 BuildKit 构建走同一条 daemon 链路，顺带预取镜像层）
pull_ok() {
    local ref="$1"
    # shellcheck disable=SC2086
    timeout "${PULL_TIMEOUT}" ${DOCKER_CMD} pull "$ref" >/dev/null 2>&1
}

# 安全增量写入单个 env 键（沿用 install.sh 的 env_merge 备份惯例）
# 用法: persist_env_key KEY VALUE
persist_env_key() {
    local key="$1" ref="$2" desired
    desired="$(mktemp)"
    echo "${key}='$(printf "%s" "${ref}" | sed "s/'/'\\\\''/g")'" > "${desired}"
    if [ -f "${ENV_PATH}" ]; then
        cp -p "${ENV_PATH}" "${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
        python3 "${BASE_DIR}/proxy/env_merge.py" \
            --existing "${ENV_PATH}" --desired "${desired}" \
            --output "${ENV_PATH}" --explicit "${key}" --quiet
    else
        python3 "${BASE_DIR}/proxy/env_merge.py" \
            --existing /dev/null --desired "${desired}" \
            --output "${ENV_PATH}" --explicit "${key}" --quiet
    fi
    rm -f "${desired}"
    chmod 600 "${ENV_PATH}"
}

# 兼容旧调用名
persist_base_image() { persist_env_key FNMUSIC_BASE_IMAGE "$1"; }

# ------------------------------------------------------------------ 探测流程
# 为单个镜像 tag 解析「真实可拉取」的引用，并写入指定 env 键。
# 用法: resolve_image <env键> <镜像tag> <手动指定值> <展示名>
resolve_image() {
    local env_key="$1" tag="$2" manual="$3" label="$4"
    local cached mirrors
    cached="$(cached_env_key "${env_key}")"
    mirrors="${FNMUSIC_DOCKER_MIRRORS:-${DEFAULT_MIRRORS}}"

    # 组装候选列表（保序去重；手动指定时仅验证该引用）
    local candidates=() seen=" "
    add_candidate() {
        local c="$1"
        [ -z "${c}" ] && return 0
        case "${seen}" in *" ${c} "*) return 0 ;; esac
        seen="${seen}${c} "
        candidates+=("${c}")
    }

    if [ -n "${manual}" ]; then
        log_info "[${label}] 已手动指定镜像，跳过自动探测。"
        add_candidate "${manual}"
    else
        # 缓存的镜像源引用优先复用；官方短引用（docker.io 域）不提前——
        # daemon 加速器链路不稳时它最慢最不可靠，统一沉底做最后兜底
        case "${cached}" in
            ""|"${tag}"|"docker.io/"*|"registry.hub.docker.com/"*) ;;
            *) add_candidate "${cached}" ;;
        esac
        local m
        # shellcheck disable=SC2086
        for m in ${mirrors}; do
            add_candidate "${m}/library/${tag}"
        done
        add_candidate "${tag}"
    fi

    local chosen="" ref rc
    for ref in "${candidates[@]}"; do
        log_info "[${label}] 尝试拉取: ${ref} ..."
        rc=0
        pull_ok "${ref}" || rc=$?
        if [ "${rc}" -eq 0 ]; then
            chosen="${ref}"
            break
        fi
        log_warn "[${label}] 拉取 ${ref} 失败（exit=${rc}），尝试下一个候选..."
    done

    if [ -z "${chosen}" ]; then
        log_err "[${label}] 所有镜像候选均拉取失败: ${candidates[*]}"
        return 1
    fi

    if [ "${chosen}" = "${cached}" ]; then
        log_info "[${label}] 可用: ${chosen}（沿用 .env 缓存）"
    else
        persist_env_key "${env_key}" "${chosen}"
        log_info "[${label}] 已写入 .env: ${env_key}=${chosen}"
    fi
    return 0
}

# Python 基础镜像：musicdl / musicbox / lxmusic 使用
resolve_image "FNMUSIC_BASE_IMAGE" "${BASE_TAG}" "${BASE_IMAGE:-}" "Python" || {
    log_err "可尝试：1) 设置 BASE_IMAGE 环境变量手动指定可用镜像引用（如 docker.m.daocloud.io/library/python:3.13-slim）；"
    log_err "        2) 通过 FNMUSIC_DOCKER_MIRRORS 自定义国内镜像候选列表；"
    log_err "        3) 检查 fnOS Docker 镜像加速器（如 docker.fnnas.com）是否可用，或改用 ./install.sh --mode host。"
    exit 1
}

# Node 基础镜像：仅 lxsource 订阅服务使用。
# 只有在启用订阅时才探测（避免无用拉取）；失败只告警，不阻断 Python 音源。
if [ "${FNMUSIC_ENSURE_NODE:-0}" = "1" ]; then
    if ! resolve_image "FNMUSIC_NODE_IMAGE" "${NODE_TAG}" "${NODE_IMAGE:-}" "Node"; then
        log_warn "  lxsource（Node）基础镜像未解析成功；订阅音源构建可能失败。"
        log_warn "  可设置 NODE_IMAGE 手动指定，例如："
        log_warn "    NODE_IMAGE=docker.m.daocloud.io/library/node:22-alpine bash ensure_base_image.sh"
    fi
else
    log_info "未启用订阅音源，跳过 Node 基础镜像探测。"
fi
