# ATOS PRO — Production Trading System

## Core Rules (Stricter Than Thales)

This is a live trading system with ~$960K AUM.
Code changes here carry financial risk. Disciplined engineering is not optional.

### 0. Safety First
- Never modify live trading parameters without triple-checking and user confirmation
- Never change strategy parameters during market hours (HKT 09:30-16:00)
- Always validate changes against shadow_state.json before deploying
- Every change should be revertible within 30 seconds

### 1. Grill Before Building
Before writing ANY code, answer:
- Which module(s) are affected?
- What's the current behaviour?
- What's the desired behaviour?
- What could go wrong? (anti-goals)
- How will we verify it worked?

### 2. Smallest Possible Change
- One change per commit. One commit per PR.
- If you're touching more than 3 files, stop and ask if it can be split.
- Prefer config changes over code changes.
- Prefer additive changes (new function) over modifications (changing existing).

### 3. Test Before Deploy
- Every logic change needs a dry-run or backtest first
- "It compiles" is NOT "it works" — verify against real state
- Check edge cases: empty positions, NaN prices, zero cycle count

### 4. Fix Root Causes
ATOS has known failure modes (documented in atos-troubleshooting skill):
- Kelly崩溃 → check position sizing divisor
- IC永久为0 → check factor engine baseline and window
- NaN传播 → check signal_engine NaN defenses
- TrailingStop崩溃 → check stop-loss merging
- 波动率止损死代码 → check volatility stop logic

If you encounter one of these, fix the class, not just the instance.

### 5. Don't Fight the Infrastructure
- LaunchAgents manage service lifecycle — don't manually restart
- Watchdog.py auto-heals crashes — don't disable it
- evolve-daemon restarts main.py — don't kill it permanently
- shadow_state.json is the truth — read from it, don't write to it

## Architecture Quick Reference

```
ATOS_PRO/
├── atos/
│   ├── live/          # ShadowTrader, signal_engine, regime_gate
│   ├── factors/       # Factor engine, IC calculation
│   ├── risk/          # Risk management, institutional_risk_engine
│   └── ai/            # AI/ML engine_v2
├── scripts/           # Watchdog, monitor, auto-heal
├── reporting/         # Performance reports
├── tools/             # Utility scripts
├── data/              # shadow_state.json, trade logs
├── logs/              # Runtime logs
└── dashboard/         # Web dashboard server.py
```

## Port Map
| Service | Port |
|---------|------|
| ShadowTrader | 19999 |
| Dashboard | 9000 |
| FutuOpenD | 11111 |
| Vibe Server | 8899 |

## Strict Prohibitions

🚫 Never change strategy params during market hours (09:30-16:00 HKT)
🚫 Never push code that hasn't been verified against real state data
🚫 Never disable watchdog.py or modify its restart logic
🚫 Never delete shadow_state.json or .shadow_trader.lock
🚫 Never commit API keys, tokens, or credentials
🚫 Never git push --force
🚫 Never make changes that can't be rolled back in <1 minute
