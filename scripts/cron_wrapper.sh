#!/bin/bash
# ATOS PRO cron 包装脚本
# 从 ATOS_PRO/.env 加载环境变量后执行命令
# 用法: cron_wrapper.sh <python模块名> [参数...]
set -euo pipefail

ATOS_DIR="/Users/benson/ATOS_PRO"
cd "$ATOS_DIR"

# 加载 .env（包含 DEEPSEEK_API_KEY + ATOS_EMAIL_*）
if [ -f "$ATOS_DIR/.env" ]; then
    set -a
    source "$ATOS_DIR/.env"
    set +a
fi

# 执行
MODULE="$1"
shift
exec "$ATOS_DIR/venv/bin/python" -m "$MODULE" "$@"
