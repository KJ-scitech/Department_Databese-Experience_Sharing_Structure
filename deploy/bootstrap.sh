#!/usr/bin/env bash
# 知识库首次部署脚本（在 M710q 上跑一次）
# 做：配置 git 走 gh-proxy 镜像 → 克隆 → 建 venv 装依赖 → 生成 .env → 建库造数据
set -euo pipefail

REPO="KJ-scitech/Department_Databese-Experience_Sharing_Structure"
APP_DIR="${KB_DIR:-$HOME/knowledge-base}"
BRANCH="${KB_BRANCH:-main}"

echo "==> 1/5 配置 git 走 gh-proxy 镜像（本机直连 GitHub 不通）"
git config --global url."https://gh-proxy.com/https://github.com/".insteadOf "https://github.com/"

echo "==> 2/5 克隆仓库到 $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  echo "    已存在，跳过 clone"
else
  git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> 3/5 建虚拟环境 + 装依赖（含 PyTorch，约 1-2GB，耐心等）"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

echo "==> 4/5 配置 .env"
if [ -f .env ]; then
  echo "    .env 已存在，跳过"
else
  cp .env.example .env
  chmod 600 .env
  echo "    已生成 .env（权限 600），请编辑填入 DB_PASSWORD 等：vim $APP_DIR/.env"
fi

echo "==> 5/5 建库建表 + 造演示数据（需 MySQL 已装、.env 已填好）"
.venv/bin/python -m scripts.init_db
.venv/bin/python -m scripts.seed

cat <<'EOF'

完成。下一步：
  1. 起服务验证：
       cd ~/knowledge-base && .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
  2. 配 systemd 托管：见 deploy/部署说明.md 第五节
  3. 开自动部署（CI/CD 的 CD 部分）：见 docs/CI-CD说明.md
EOF
