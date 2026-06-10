#!/bin/bash
# ATOS Health Check — detailed metrics with state tracking
# Designed for Hermes cron job. Saves state to detect equity deltas,
# cycle count changes, and reports CPU load + news headlines.
#
# State file: /Users/benson/ATOS_PRO/data/health_check_state.json
# This tracks last-known equity and cycle count for delta reporting.

ATOS_DIR="/Users/benson/ATOS_PRO"
LOG_DIR="$ATOS_DIR/logs"
STATE_FILE="$ATOS_DIR/data/shadow_state.json"
CHECK_STATE="$ATOS_DIR/data/health_check_state.json"

echo "=== ATOS Health Check $(date '+%Y-%m-%d %H:%M:%S') ==="

# ── Helper: check if a TCP port is listening ──
check_port() {
    lsof -i ":$1" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN && echo "UP" || echo "DOWN"
}

# ── Helper: safe JSON extract with default ──
json_get() {
    python3 -c "import json,sys; d=json.load(open('$1')); print(d.get('$2', '$3'))" 2>/dev/null || echo "$3"
}

# ═══════════════════════════════════════════════════════
# 1. PORT CHECKS (ShadowTrader=19999, FutuOpenD=11111, Dashboard=9000)
# ═══════════════════════════════════════════════════════
SHADOW_PORT=$(check_port 19999)
FUTU_PORT=$(check_port 11111)
DASH_PORT=$(lsof -i :9000 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN && echo "UP" || echo "DOWN")

echo "shadow_port=$SHADOW_PORT"
echo "futu_port=$FUTU_PORT"
echo "dash_port=$DASH_PORT"

# ═══════════════════════════════════════════════════════
# 2. SHADOWTRADER STATE (cycle count, equity, positions)
# ═══════════════════════════════════════════════════════
if [ -f "$STATE_FILE" ]; then
    CYCLE=$(json_get "$STATE_FILE" "cycle_count" "?")
    EQUITY=$(json_get "$STATE_FILE" "equity" "?")
    PEAK_EQUITY=$(json_get "$STATE_FILE" "peak_equity" "?")
    CASH=$(json_get "$STATE_FILE" "cash" "?")

    # Parse positions from JSON
    POSITIONS_INFO=$(python3 -c "
import json
d=json.load(open('$STATE_FILE'))
positions = d.get('positions', {})
print(f'num_positions={len(positions)}')
for sym, p in positions.items():
    qty = p.get('qty', 0)
    avg = p.get('avg_price', 0)
    last = p.get('last_price', 0)
    pnl = round((last - avg) * qty, 2)
    pnl_pct = round(((last/avg) - 1) * 100, 2) if avg else 0
    print(f'pos_{sym}_pnl={pnl},pos_{sym}_pnl_pct={pnl_pct}')
" 2>/dev/null)

    echo "cycle_count=$CYCLE"
    echo "equity=$EQUITY"
    echo "peak_equity=$PEAK_EQUITY"
    echo "cash=$CASH"
    echo "$POSITIONS_INFO"
else
    echo "cycle_count=NO_STATE_FILE"
    echo "equity=?"
    echo "peak_equity=?"
    echo "cash=?"
    echo "num_positions=?"
fi

# ═══════════════════════════════════════════════════════
# 3. DELTA DETECTION (equity change, cycle count change since last check)
# ═══════════════════════════════════════════════════════
PREV_EQUITY=""
PREV_CYCLE=""
if [ -f "$CHECK_STATE" ]; then
    PREV_EQUITY=$(json_get "$CHECK_STATE" "last_equity" "")
    PREV_CYCLE=$(json_get "$CHECK_STATE" "last_cycle" "")
fi

if [ -n "$PREV_EQUITY" ] && [ "$EQUITY" != "?" ]; then
    EQUITY_DELTA=$(python3 -c "print(round(float('$EQUITY') - float('$PREV_EQUITY'), 2))" 2>/dev/null || echo "ERR")
    EQUITY_DELTA_PCT=$(python3 -c "print(round((float('$EQUITY')/float('$PREV_EQUITY')-1)*100, 4))" 2>/dev/null || echo "ERR")
else
    EQUITY_DELTA="N/A (first run)"
    EQUITY_DELTA_PCT="N/A"
fi

if [ -n "$PREV_CYCLE" ] && [ "$CYCLE" != "?" ] && [ "$CYCLE" != "NO_STATE_FILE" ]; then
    CYCLE_DELTA=$(python3 -c "print(int('$CYCLE') - int('$PREV_CYCLE'))" 2>/dev/null || echo "ERR")
else
    CYCLE_DELTA="N/A (first run)"
fi

echo "equity_delta=$EQUITY_DELTA"
echo "equity_delta_pct=$EQUITY_DELTA_PCT"
echo "cycle_delta=$CYCLE_DELTA"

# ═══════════════════════════════════════════════════════
# 4. CPU LOAD
# ═══════════════════════════════════════════════════════
CPU_LOAD=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2, $3, $4}' || echo "N/A")
CPU_PCT=$(ps -A -o %cpu 2>/dev/null | awk '{s+=$1} END {printf "%.1f", s}' || echo "N/A")
echo "cpu_loadavg=$CPU_LOAD"
echo "cpu_total_pct=$CPU_PCT"

# Memory usage from vm_stat
MEM_FREE=$(vm_stat 2>/dev/null | grep "Pages free" | awk '{print $3}' | sed 's/\.//')
MEM_ACTIVE=$(vm_stat 2>/dev/null | grep "Pages active" | awk '{print $3}' | sed 's/\.//')
echo "mem_free_pages=${MEM_FREE:-N/A}"
echo "mem_active_pages=${MEM_ACTIVE:-N/A}"

# ═══════════════════════════════════════════════════════
# 5. RECENT TRACEBACKS (last 500 lines of stderr)
# ═══════════════════════════════════════════════════════
if [ -f "$LOG_DIR/shadow_trader_stderr.log" ]; then
    TRACEBACKS=$(tail -500 "$LOG_DIR/shadow_trader_stderr.log" | grep -c "Traceback" 2>/dev/null || echo 0)
    echo "recent_tracebacks=$TRACEBACKS"
else
    echo "recent_tracebacks=NO_LOG"
fi

# ═══════════════════════════════════════════════════════
# 6. DASHBOARD HTTP RESPONSE
# ═══════════════════════════════════════════════════════
DASH_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:9000/api 2>/dev/null || echo "FAIL")
echo "dash_http=$DASH_HTTP"

# ═══════════════════════════════════════════════════════
# 7. FUTUOPEND STATUS
# ═══════════════════════════════════════════════════════
FUTU_PID=$(pgrep -f FutuOpenD 2>/dev/null | head -1)
FUTU_MEM=$(ps -o rss= -p "${FUTU_PID:-0}" 2>/dev/null | awk '{printf "%.0f", $1/1024}' || echo "N/A")
echo "futu_pid=${FUTU_PID:-NONE}"
echo "futu_mem_mb=${FUTU_MEM:-N/A}"

# ═══════════════════════════════════════════════════════
# 8. NEWS HEADLINES (last 3 from latest equity_history entries)
# ═══════════════════════════════════════════════════════
if [ -f "$STATE_FILE" ]; then
    python3 -c "
import json
d = json.load(open('$STATE_FILE'))
# Check if there's a news field
news = d.get('news', d.get('headlines', d.get('market_news', [])))
if news and isinstance(news, list) and len(news) > 0:
    print('news_headlines=' + ' | '.join([n.get('headline', str(n))[:100] for n in news[:3]]))
else:
    # Fallback: show latest trade reasons
    trades = d.get('trade_history', [])
    if trades:
        latest = trades[-3:]
        reasons = ' | '.join([t.get('reason', '')[:80] for t in latest])
        # Strip non-ASCII for clean output
        reasons_clean = ''.join(c if ord(c) < 128 else '?' for c in reasons)
        print(f'news_headlines=Recent trades: {reasons_clean}')
    else:
        print('news_headlines=None')
" 2>/dev/null || echo "news_headlines=PARSE_ERR"
fi

# ═══════════════════════════════════════════════════════
# 9. SAVE STATE for next delta detection
# ═══════════════════════════════════════════════════════
if [ "$EQUITY" != "?" ] && [ "$CYCLE" != "?" ] && [ "$CYCLE" != "NO_STATE_FILE" ]; then
    python3 -c "
import json
state = {
    'last_equity': float('$EQUITY'),
    'last_cycle': int('$CYCLE'),
    'last_check': '$(date '+%Y-%m-%d %H:%M:%S')'
}
with open('$CHECK_STATE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
fi

# ═══════════════════════════════════════════════════════
# 10. AUTO-FIX: if any port is DOWN, restart the service
# ═══════════════════════════════════════════════════════
FIXED_ANY=false

fix_port() {
    local port=$1
    local name=$2
    local plist=$3
    if [ "$port" = "DOWN" ]; then
        echo "fix_action=Restarting $name ($plist)..."
        launchctl unload "$plist" 2>/dev/null
        sleep 1
        launchctl load "$plist" 2>/dev/null
        echo "fix_action=$name restart issued"
        FIXED_ANY=true
    fi
}

fix_port "$SHADOW_PORT" "ShadowTrader" "$HOME/Library/LaunchAgents/com.atos.shadowtrader.plist"
fix_port "$DASH_PORT" "Dashboard" "$HOME/Library/LaunchAgents/ai.atos.dashboard.plist"

if [ "$FUTU_PORT" = "DOWN" ]; then
    echo "fix_action=Restarting FutuOpenD..."
    launchctl kickstart -k "gui/501/com.futunn.FutuOpenD" 2>/dev/null || \
        open /Applications/Futu_OpenD.app 2>/dev/null
    echo "fix_action=FutuOpenD restart issued"
    FIXED_ANY=true
fi

if [ "$FIXED_ANY" = "false" ]; then
    echo "fix_action=None (all services UP)"
fi

# ═══════════════════════════════════════════════════════
# 11. CONCISE OVERALL SUMMARY
# ═══════════════════════════════════════════════════════
ALL_UP=true
for p in "$SHADOW_PORT" "$FUTU_PORT" "$DASH_PORT"; do
    [ "$p" = "DOWN" ] && ALL_UP=false
done

if [ "$ALL_UP" = "true" ]; then
    echo "overall=OK"
else
    echo "overall=ISSUES"
fi

# Build a one-line status
if [ "$EQUITY" != "?" ] && [ "$CYCLE" != "?" ] && [ "$CYCLE" != "NO_STATE_FILE" ]; then
    echo "summary=Shadow:$SHADOW_PORT Futu:$FUTU_PORT Dash:$DASH_PORT | Cycle #$CYCLE | Equity=\$$EQUITY | Delta=\$${EQUITY_DELTA:-N/A} | CPU=$CPU_PCT%"
else
    echo "summary=Shadow:$SHADOW_PORT Futu:$FUTU_PORT Dash:$DASH_PORT | CPU=$CPU_PCT%"
fi

echo "=== END ==="
