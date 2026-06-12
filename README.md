# ATOS PRO — 机构级量化交易系统

## 安装

```bash
pip install -r requirements.txt
```

## 运行 Web 仪表盘

```bash
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000
```

浏览器打开 http://localhost:8000

## 项目结构

```
atos/
├── cli/              # CLI 入口
├── strategy/         # 交易策略 (金叉+EMA+RSI+ATR)
├── market/regime/    # 市场状态识别
├── risk/             # 风控 & 凯利仓位
├── reporting/        # 绩效报告
├── portfolio/        # 投资组合
├── infrastructure/   # 事件总线
├── backtest/         # 回测引擎 (yfinance)
├── broker/futu/      # 富途网关 (mock)
├── monitoring/       # Kill Switch
└── web/              # FastAPI Web 服务
    ├── server.py
    └── templates/index.html
```

## 协作流程

1. `git checkout main && git pull origin main`
2. `git checkout -b feature/xxx`
3. 写代码 → `git commit`
4. `git push -u origin feature/xxx`
5. GitHub 上创建 Pull Request → 合并
