# CLAUDE.md — ATOS PRO Trading System

## Project
ATOS PRO — Automated Trading Operating System. $1M paper trading, 24/7.
Location: /Users/benson/ATOS_PRO/

## Key Architecture
- **3-tier**: Shadow Trader (live) / Phoenix (long-term) / Dashboard (UI)
- **LaunchAgents**: com.atos.shadowtrader (port 19999), ai.atos.dashboard (port 9000), com.futunn.FutuOpenD (port 11111)
- **AI**: DeepSeek API via Hermes Agent, ULTRA mode (1 call/hr)
- **DB**: SQLite at data/ai_memory.db
- **Logs**: logs/shadow_trader_stderr.log

## Running
- Shadow Trader: `python3 -m atos.trading.shadow_trader`
- Phoenix: `python3 -m atos.longterm.phoenix_runner --run`
- Dashboard: `python3 -m atos.dashboard.app`
- Test: `python3 -m pytest tests/ -v`

## Known Bugs/Gotchas
- Never use `if df:` — use `if df is not None` to avoid DataFrame boolean ambiguity
- DeepSeek chat doesn't support `response_format={"type":"json_object"}` — use `deepseek-chat` model
- LaunchAgent: set `KeepAlive` with `SuccessfulExit: false` to avoid restart storms
- Always clear `__pycache__` after code changes: `find . -name __pycache__ -exec rm -rf {} + 2>/dev/null`

## Coding Style
- Python 3.11+, f-strings, type hints where helpful
- Config in config/ directory, .env files for secrets
- Docs in docs/ directory
