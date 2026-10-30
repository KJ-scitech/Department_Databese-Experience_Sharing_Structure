#!/usr/bin/env bash
# 知识库自动部署脚本（在 M710q 上由 systemd timer 定时调用）
#
# 逻辑：拉取 main → 与本地比较 → 有更新就 fast-forward + 必要时装依赖 + 重启服务。
# 无更新则静默退出，不产生日志噪音。
#
# remote 默认走 gh-proxy 镜像（服务器直连 GitHub 不通，走镜像更省事）。
# 若已给机器配好代理，可用 KB_REMOTE=https://github.com/... 直连。
# 环境变量：KB_DIR / KB_BRANCH / KB_SERVICE / KB_REMOTE
set -euo pipefail

APP_DIR="${KB_DIR:-$HOME/knowledge-base}"
BRANCH="${KB_BRANCH:-main}"
SERVICE="${KB_SERVICE:-scitech-kb}"
REPO="KJ-scitech/Department_Databese-Experience_Sharing_Structure"
REMOTE="${KB_REMOTE:-https://gh-proxy.com/https://github.com/$REPO.git}"

LOG() { echo "[$(date '+%F %T')] $*"; }

# 仓库属主和跑 git 的用户不一致时，git 会判定 dubious ownership 并拒绝操作，先声明为安全目录。
# ★ 这条是写进 $HOME/.gitconfig 的——HOME 不可写的话它会静默失效，报错照旧。
#   单元里因此把 HOME / GIT_CONFIG_GLOBAL 钉到了可写目录。
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

cd "$APP_DIR" || { LOG "错误：找不到目录 $APP_DIR"; exit 1; }

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  LOG "错误：$APP_DIR 不是 git 仓库，请先跑 deploy/bootstrap.sh"
  exit 1
fi

git fetch --quiet "$REMOTE" "$BRANCH"
NEW=$(git rev-parse FETCH_HEAD)
CUR=$(git rev-parse HEAD)

# 无更新：静默退出
if [ "$NEW" = "$CUR" ]; then
  exit 0
fi

# 工作区脏就别动，避免覆盖本地改动
if ! git diff --quiet || ! git diff --cached --quiet; then
  LOG "工作区有未提交改动，跳过本次部署（请先处理）"
  exit 2
fi

LOG "发现更新：${CUR:0:8} -> ${NEW:0:8}，开始部署"
git merge --ff-only "$NEW"

# 依赖有变化才重装
if ! git diff --quiet "$CUR" "$NEW" -- requirements.txt; then
  LOG "requirements.txt 有变化，重装依赖…"
  if [ -x .venv/bin/pip ]; then
    .venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
  else
    LOG "警告：未找到 .venv，跳过装依赖"
  fi
fi

# 重启服务。★ 这一步需要 systemctl 权限：单元以服务账号跑时，
# 靠一条只放开 `systemctl restart scitech-kb` 的免密规则（见 部署说明.md 第五节）。
systemctl restart "$SERVICE"

LOG "部署完成：${NEW:0:8}"
