#!/usr/bin/env bash
# ============================================================
# AgentPair 一键部署脚本(在 Linux 服务器上执行)
#
# 步骤:
#   1. git clone <repo-url> AgentPair && cd AgentPair/deploy
#   2. cp .env.production.example .env.production
#      编辑 .env.production(数据库/密钥/沙箱地址/OAuth)
#   3. bash deploy.sh
#
# 日常更新:
#   cd AgentPair && git pull && cd deploy && bash deploy.sh
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

ENV_FILE=".env.production"

if [ ! -f "$ENV_FILE" ]; then
    echo "❌ 缺少 $ENV_FILE,请先执行:"
    echo "   cp .env.production.example .env.production"
    echo "   并填写 DATABASE_URL / JWT_SECRET / GITHUB_TOKEN_SECRET / SANDBOX_SERVER_URL 等"
    exit 1
fi

# 导出配置,供 docker compose 插值构建参数(VITE_* 与 HTTP_PORT)
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

echo "==> 构建镜像(首次较慢,需拉取 base 镜像与 npm/pip 依赖)"
docker compose build

echo "==> 启动服务"
docker compose up -d

echo "==> 服务状态"
docker compose ps

echo ""
echo "✅ 部署完成"
echo "   前端: http://<服务器IP>:${HTTP_PORT:-80}"
echo "   后端文档: http://<服务器IP>:${HTTP_PORT:-80}/docs"
echo "   查看日志: docker compose logs -f backend"
echo "   停止服务: docker compose down(数据在 named volume 中保留)"
