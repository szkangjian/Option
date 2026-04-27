#!/usr/bin/env bash
# 一条命令启动 Web 面板（含后台 chain 预取 / 告警 / IV 同步等定时任务）。
# 跑之前确保 IB Gateway 已登录、API 已开启。

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f config/accounts.yaml ]]; then
  echo "✗ 找不到 config/accounts.yaml"
  echo "  请先跑：bash scripts/install.sh"
  exit 1
fi

if ! grep -qE '^\s*account_code:\s*U[0-9]+' config/accounts.yaml; then
  echo "⚠ config/accounts.yaml 里 account_code 看起来还是占位符（UXXXXXXX）"
  echo "  请编辑它，填入你真实的 IBKR 账号代码（U 开头的那串）"
  echo "  然后再跑这个脚本。"
  exit 1
fi

echo "==> 启动 options-tool Web 面板"
echo "    浏览器打开 http://localhost:8000"
echo "    按 Ctrl-C 退出"
echo

exec uv run options-tool serve
