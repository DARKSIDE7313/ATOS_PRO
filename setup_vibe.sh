#!/bin/bash
# ============================================================
# ATOS PRO — Vibe-Trading 一键安装脚本
# 队友拿到后只需跑这一条：bash setup_vibe.sh
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[VIBE]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC} $1"; }

PROJECTS_DIR="$HOME/projects"
ATOS_DIR="$HOME/ATOS_PRO"
VIBE_DIR="$PROJECTS_DIR/Vibe-Trading"

# ── Step 1: 克隆 Vibe-Trading ──
if [ -d "$VIBE_DIR" ]; then
    log "Vibe-Trading 已存在，跳过克隆"
else
    log "克隆 Vibe-Trading..."
    mkdir -p "$PROJECTS_DIR"
    git clone https://github.com/HKUDS/Vibe-Trading.git "$VIBE_DIR"
fi

# ── Step 2: 创建独立 venv ──
cd "$VIBE_DIR"
if [ -d ".venv" ]; then
    log "venv 已存在，跳过创建"
else
    log "创建 venv..."
    python3 -m venv .venv
fi

# ── Step 3: 安装依赖 ──
source .venv/bin/activate
log "安装依赖（可能需要几分钟）..."
pip install --quiet -e . 2>/dev/null || true
# 跳过有问题的包 (llvmlite 在 Python 3.14 上无法编译)
pip install --quiet \
    rich pyyaml langchain langchain-core langchain-openai \
    langgraph langgraph-checkpoint python-dotenv httpx \
    oauth-cli-kit pandas numpy scipy duckdb openpyxl \
    python-docx python-pptx pypdfium2 Pillow scikit-learn \
    joblib requests yfinance akshare ccxt fastapi uvicorn \
    pydantic python-multipart sse-starlette fastmcp ddgs \
    jinja2 matplotlib weasyprint prompt_toolkit 2>/dev/null

# 降级 langgraph 解决版本冲突
pip install --quiet 'langgraph>=1.0.10,<1.1' 2>/dev/null || true

log "依赖安装完成"

# ── Step 4: 配置 .env ──
if [ -f "$VIBE_DIR/agent/.env" ]; then
    log ".env 已存在，跳过创建"
else
    echo ""
    warn "需要 DeepSeek API Key 来运行 Vibe-Trading"
    echo -n "请输入你的 DeepSeek API Key (sk-...): "
    read -r API_KEY

    if [ -z "$API_KEY" ]; then
        err "未提供 API Key，将创建模板 .env，你需要手动编辑"
        API_KEY="YOUR_DEEPSEEK_API_KEY_HERE"
    fi

    cat > "$VIBE_DIR/agent/.env" << EOF
# Vibe-Trading — ATOS PRO 集成配置
LANGCHAIN_PROVIDER=deepseek
LANGCHAIN_MODEL_NAME=deepseek-v4-pro
DEEPSEEK_API_KEY=${API_KEY}
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
LANGCHAIN_TEMPERATURE=0.0
TIMEOUT_SECONDS=120
MAX_RETRIES=2
API_AUTH_KEY=atos_internal_secret_2026
EOF
    log ".env 已创建"
fi

# ── Step 5: 创建 LaunchAgent (macOS 自启动) ──
LAUNCH_PLIST="$HOME/Library/LaunchAgents/ai.vibetrading.server.plist"
if [ -f "$LAUNCH_PLIST" ]; then
    log "LaunchAgent 已存在，跳过创建"
else
    log "创建 LaunchAgent（开机自启动 localhost:8899）..."
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/logs"
    cat > "$LAUNCH_PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.vibetrading.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VIBE_DIR}/.venv/bin/python3</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>agent.api_server:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8899</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${VIBE_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/logs/vibe_server_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/logs/vibe_server_stderr.log</string>
</dict>
</plist>
PLISTEOF
    launchctl load "$LAUNCH_PLIST" 2>/dev/null || true
    log "LaunchAgent 已加载"
fi

# ── Step 6: 验证 ──
log "等待 Vibe server 启动..."
sleep 5
if curl -s http://localhost:8899/health | grep -q healthy; then
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  ✅ Vibe-Trading 安装成功！${NC}"
    echo -e "${GREEN}  Server:  http://localhost:8899${NC}"
    echo -e "${GREEN}  Health:  $(curl -s http://localhost:8899/health)${NC}"
    echo -e "${GREEN}  自启动:  已配置（重启后自动运行）${NC}"
    echo -e "${GREEN}============================================${NC}"
else
    warn "Server 未能自动启动。手动启动："
    echo "  cd $VIBE_DIR && source .venv/bin/activate"
    echo "  python3 -m uvicorn agent.api_server:app --host 127.0.0.1 --port 8899"
fi

echo ""
log "ATOS 依赖也需要更新："
echo "  cd $ATOS_DIR && source venv/bin/activate && pip install -r requirements.txt"
