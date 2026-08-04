#!/bin/bash
# Auto-sync: check if tunnel URL changed, update Worker if needed
# 从多个来源找当前 tunnel URL

WORKER_JS="/Users/benson/ATOS_PRO/cloudflare-worker.js"

# 尝试多个日志文件 + state 文件
CURRENT_TUNNEL=""
for logfile in /tmp/atos_tunnel.log /tmp/cloudflared_tunnel.log; do
    if [ -f "$logfile" ]; then
        CURRENT_TUNNEL=$(grep -o 'https://[a-z0-9.-]*\.trycloudflare\.com' "$logfile" 2>/dev/null | tail -1)
        [ -n "$CURRENT_TUNNEL" ] && break
    fi
done

# 也从 ~/.atos_tunnel_url 读
if [ -z "$CURRENT_TUNNEL" ] && [ -f ~/.atos_tunnel_url ]; then
    CURRENT_TUNNEL=$(cat ~/.atos_tunnel_url 2>/dev/null)
fi

WORKER_TUNNEL=$(grep -o 'const TUNNEL_ORIGIN = "https://[a-z0-9.-]*\.trycloudflare\.com"' "$WORKER_JS" 2>/dev/null | grep -o 'https://[a-z0-9.-]*\.trycloudflare\.com')

if [ -n "$CURRENT_TUNNEL" ] && [ "$CURRENT_TUNNEL" != "$WORKER_TUNNEL" ]; then
    echo "[$(date)] Tunnel changed: $WORKER_TUNNEL → $CURRENT_TUNNEL"
    sed -i '' "s|const TUNNEL_ORIGIN = \"https://[a-z0-9.-]*\.trycloudflare\.com\"|const TUNNEL_ORIGIN = \"$CURRENT_TUNNEL\"|" "$WORKER_JS"
    npx wrangler deploy "$WORKER_JS" --name atos-dashboard --compatibility-date 2026-07-15 2>&1 | grep -E "Deployed|ERROR"
    echo "$CURRENT_TUNNEL" > ~/.atos_tunnel_url
    echo "✅ Worker updated to $CURRENT_TUNNEL"
fi
