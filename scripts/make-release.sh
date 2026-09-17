#!/usr/bin/env bash
# 生成可公开发布的干净目录。
#
# 用途：从开发工作树导出一份「不含本地账号信息、不含运行期数据」的副本，
# 用于推送到公开仓库或分享给他人安装。
#
# 用法:
#   ./scripts/make-release.sh [目标目录]
#   默认目标目录: ../fnos_music_lx.release
#
# 会做三件事：
#   1. 只复制源码与文档，排除运行期数据 / 虚拟环境 / 密钥 / 缓存
#   2. 剔除仅开发用的产物（开发用补丁副本、部署日志）
#   3. 扫描并报告残留的本地敏感信息（本地绝对路径、密钥、账号名）
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$(dirname "$SRC")/fnos_music_lx.release}"

log()  { printf '\033[32m[release]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[release]\033[0m %s\n' "$*"; }
err()  { printf '\033[31m[release]\033[0m %s\n' "$*" >&2; }

command -v rsync >/dev/null 2>&1 || { err "需要 rsync"; exit 1; }

EXCLUDES=(
    --exclude='.git/'
    --exclude='.venv*/'
    --exclude='.pytest_cache/'
    --exclude='__pycache__/'
    --exclude='*.pyc'
    --exclude='cache/'
    --exclude='musicbox-data/'
    --exclude='recommend_cache/'
    --exclude='play_history/'
    --exclude='online_favorites/'
    --exclude='online_favorites.json'
    --exclude='backup/'
    --exclude='musicdl_outputs/'
    --exclude='.env'
    --exclude='.env.*'
    --exclude='*.log'
    --exclude='*.part'
    --exclude='*.bak'
    --exclude='*.orig'
    --exclude='*.rej'
    --exclude='*.pre-lxsource.*'
    --exclude='.DS_Store'
)

log "源目录: $SRC"
log "目标目录: $DEST"

if [ -e "$DEST" ]; then
    warn "目标已存在，将先删除：$DEST"
    rm -rf "$DEST"
fi
mkdir -p "$DEST"

# --no-owner/--no-group：避免把源目录的 root 属主带过来
# （否则后续以 root 运行 git 会触发 dubious ownership 保护）
rsync -a --no-owner --no-group "${EXCLUDES[@]}" "$SRC/" "$DEST/"

# .env.example 需强制保留（被 .env.* 规则误排除）
[ -f "$SRC/.env.example" ] && cp "$SRC/.env.example" "$DEST/.env.example"

# 剔除仅开发用的产物
for f in \
    "patches/app.py.patched" \
    "patches/ikun-source.patch" \
    "patches/README-ikun-source.md" \
    "patches/DEPLOYMENT-STATUS.md"
do
    if [ -e "$DEST/$f" ]; then
        rm -f "$DEST/$f"
        log "剔除开发产物: $f"
    fi
done

# 必须显式删除 .git：rsync --exclude 对「目标已存在」的情况不生效
rm -rf "$DEST/.git"

find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "$DEST/.pytest_cache" 2>/dev/null || true

log "扫描本地敏感信息..."
FOUND=0
scan() {
    local label="$1" pattern="$2" hits
    hits="$(grep -rlE "$pattern" "$DEST" 2>/dev/null || true)"
    if [ -n "$hits" ]; then
        err "  ⚠️ 发现 $label:"
        printf '%s\n' "$hits" | sed "s#$DEST/##; s#^#      #"
        FOUND=1
    else
        log "  ✅ 无 $label"
    fi
}
# 检测本机绝对路径（fnOS 卷通常挂载在 /volN，可按需扩充）
scan "本机绝对路径(/volN)" '/vol[0-9]+/'
scan "部署密钥(IKM-)" 'IKM-'
scan "GitHub token" 'ghp_|github_pat'
if [ -d "$DEST/.git" ]; then
    err "  ⚠️ 发布目录存在 .git（不应带入，请检查导出流程）"
    FOUND=1
else
    log "  ✅ 无 .git（不会带入旧历史）"
fi

if [ "$FOUND" -ne 0 ]; then
    err "发布目录仍含敏感信息，请处理后重试。"
    exit 1
fi

log "语法自检..."
for s in install.sh extend.sh restore.sh deploy.sh lxsource-service/run-local.sh; do
    [ -f "$DEST/$s" ] && { bash -n "$DEST/$s" || { err "  $s 语法错误"; exit 1; }; }
done
log "  ✅ shell 脚本语法通过"

log "完成：$DEST"
log "文件数: $(find "$DEST" -type f | wc -l)"
