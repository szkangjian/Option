#!/usr/bin/env bash
# 一键安装：装依赖 + 初始化配置 + 建数据库。
# 第一次跑或者环境损坏时跑一次就够。

set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> [1/5] 检查 uv（Python 包管理器）"
if ! command -v uv >/dev/null 2>&1; then
  echo "    没装 uv，正在安装……"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # 让当前 shell 立刻能用
  export PATH="$HOME/.local/bin:$PATH"
  echo "    ✓ uv 安装完成。下次开新终端如果找不到 uv，重启终端即可。"
else
  echo "    ✓ 已装 uv（$(uv --version)）"
fi

echo "==> [2/5] 装项目依赖（首次大约 1–2 分钟）"
uv sync

echo "==> [3/5] 准备配置文件"
if [[ ! -f config/accounts.yaml ]]; then
  cp config/accounts.yaml.example config/accounts.yaml
  echo "    ✓ 创建了 config/accounts.yaml"
  echo "      ⚠ 请用文本编辑器打开它，把 UXXXXXXX 改成你的 IBKR 账号代码"
else
  echo "    · config/accounts.yaml 已存在，保留不动"
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "    ✓ 创建了 .env（Telegram / Finnhub 密钥可选；不填也能用）"
else
  echo "    · .env 已存在，保留不动"
fi

echo "==> [4/5] 初始化 SQLite 数据库"
uv run options-tool init-db

echo "==> [5/5] 完成"
cat <<'EOF'

下一步——
  1. 安装并登录 IB Gateway，开启 API（详见 docs/ib-gateway-setup.md）
  2. 编辑 config/accounts.yaml，把示例里的 UXXXXXXX 改成你的真实账号代码
  3. 启动：bash scripts/run.sh
     然后浏览器打开 http://localhost:8000

EOF
