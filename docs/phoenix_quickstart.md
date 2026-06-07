# ATOS Phoenix Strategy — Claude Code 应用指南

> **给 Claude Code 看的指令** — 请按此顺序应用

---

## 第 1 步：理解系统架构

```
ATOS_PRO/
├── atos/longterm/            <── Phoenix 策略核心模块
│   ├── __init__.py             已更新：导出所有新模块
│   ├── config.py               新文件：所有 Phoenix 参数配置
│   ├── market_thermometer.py   新文件：Howard Marks 7 维度市场温度
│   ├── cash_manager.py         新文件：现金管理与抄底部署
│   ├── layer1_foundation.py    新文件：股息贵族 + 增强定投
│   ├── layer3_tactical.py      新文件：因子轮动 + 行业轮动 + 内部人追踪
│   ├── risk_monitor.py         新文件：统一风险监控
│   └── phoenix_runner.py       新文件：Phoenix 主协调器
│      
├── atos/live/futu_bridge.py    已存在：富途券商桥接
├── atos/longterm/engine.py     已存在：多因子引擎（Layer 2 复用它）
├── atos/longterm/value_investor.py  已存在：Burry 价值评估
│
├── docs/longterm_methods_complete.md  完整方法大全（参考读物）
└── reports/                           Phoenix 运行报告目录
```

---

## 第 2 步：安装依赖（如果还没装）

```bash
cd /Users/benson/ATOS_PRO
source venv/bin/activate   # 或 python3 直接跑

# Phoenix 没有新依赖，复用 ATOS 已有的
# yfinance, pandas, numpy — 都在 ATOS venv 里
# 如需 SEC EDGAR 解析（内部人追踪），后续安装:
#   pip install sec-edgar-api
```

## 第 3 步：验证 Phoenix 模块能加载

```bash
cd /Users/benson/ATOS_PRO
python3 -c "
from atos.longterm.config import CAPITAL, LAYER1
from atos.longterm.market_thermometer import MarketThermometer
from atos.longterm.cash_manager import get_cash_manager
from atos.longterm.layer1_foundation import get_layer1
from atos.longterm.layer3_tactical import get_layer3
from atos.longterm.risk_monitor import get_risk_monitor
from atos.longterm.phoenix_runner import get_phoenix, quick_status
print('✅ 所有 Phoenix 模块加载成功')
print(quick_status())
"
```

## 第 4 步：集成到 ATOS Cron

在现有 cron 任务中加入 Phoenix 定时调用。

**方案 A：独立 cron（推荐）**

```python
# 在 ATOS cron 列表中新增
# 每小时检查回撤和现金部署
# 每 15 天执行 Layer 1
# 每 30 天执行 Layer 3
# 每 91 天执行 Layer 2（自动）
# 每天执行风险监控

from atos.longterm.phoenix_runner import run_phoenix
run_phoenix()
```

**方案 B：直接添加 Python 调度**

```python
# 添加到 ATOS 的 iterate/daily_pipeline.py 或新建一个 cron 调度器
import schedule
from atos.longterm.phoenix_runner import get_phoenix

phoenix = get_phoenix()

# 每小时检查现金部署
schedule.every(1).hours.do(
    lambda: phoenix.cash.should_deploy_cash()
)

# 每天执行 Phoenix 主周期（自动判断哪些层需要执行）
schedule.every().day.at("09:30").do(phoenix.full_run)
```

## 第 5 步：验证运行

```bash
cd /Users/benson/ATOS_PRO
python3 -m atos.longterm.phoenix_runner --run
```

应该看到如下输出：
```
🔥🔥🔥 Phoenix 长线策略启动 🔥🔥🔥
Step 1: 评估市场温度...
Step 2: 风险检查...
Step 3: 检查现金部署...
Step 4a: Layer 1 基础层...
Step 4b: Layer 2 核心层...
Step 4c: Layer 3 战术层...
🎉 Phoenix 策略完成 (X.Xs)
```

---

## 第 6 步：查看状态

```bash
python3 -m atos.longterm.phoenix_runner --status
```

---

## 文件清单（需要确认都在）

**新增的 Phoenix 文件（8 个）：**
1. ✅ `atos/longterm/config.py`
2. ✅ `atos/longterm/market_thermometer.py`
3. ✅ `atos/longterm/cash_manager.py`
4. ✅ `atos/longterm/layer1_foundation.py`
5. ✅ `atos/longterm/layer3_tactical.py`
6. ✅ `atos/longterm/risk_monitor.py`
7. ✅ `atos/longterm/phoenix_runner.py`
8. ✅ `atos/longterm/__init__.py` (已更新)

**参考文件：**
9. ✅ `docs/longterm_methods_complete.md` — 24 种投资方法大全

---

## 策略参数调优

所有参数都在 `atos/longterm/config.py` 中集中管理：

- **资金分配：** 修改 `CAPITAL["layer1_pct"]` 等
- **股息贵族持仓数：** 修改 `LAYER1["aristocrat_position_count"]`
- **定投倍数：** 修改 `LAYER1["dca_min_pe_for_double"]` 等 PE 阈值
- **风险上限：** 修改 `RISK["max_overall_drawdown"]`
- **回撤买入阈值：** 修改 `LAYER3["dip_buy_thresholds"]`

别忘了 ATOS 用 **模拟盘（paper trading）**，资金配置是 $100万 纸交易。
