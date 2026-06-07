"""
ATOS PRO v2 — Phoenix 统一风险监控
=====================================
跨所有层级的风险检查：
  - 整体回撤监控
  - 单只持仓上限
  - 行业集中度
  - 市场趋势（200日均线）
  - 流动性检查

每天运行一次，在交易时段开始前检查。
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import os, json
from atos.core.logging import get_logger
from atos.longterm.config import RISK, CAPITAL
from atos.longterm.market_thermometer import MarketThermometer
from atos.longterm.cash_manager import get_cash_manager

logger = get_logger("phoenix.risk")

# yfinance Ticker.info 缓存 — 5 分钟内不重复查询
_TICKER_INFO_CACHE = {}
_TICKER_INFO_CACHE_TTL = datetime.timedelta(minutes=5)

def _get_cached_ticker_info(symbol: str) -> dict:
    """带缓存的 yfinance Ticker.info 查询"""
    now = datetime.datetime.now()
    if symbol in _TICKER_INFO_CACHE:
        ts, info = _TICKER_INFO_CACHE[symbol]
        if now - ts < _TICKER_INFO_CACHE_TTL:
            return info
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        info = {}
    _TICKER_INFO_CACHE[symbol] = (datetime.datetime.now(), info)
    return info


class RiskMonitor:
    """
    统一风险监控。
    
    在每个交易周期开始前自动检查以下项目：
    1. 整体回撤是否超过阈值
    2. 单只持仓是否超限
    3. 行业是否过度集中
    4. 市场趋势方向
    5. 流动性是否充足
    """

    def __init__(self, state_dir: str = None):
        self.thermometer = MarketThermometer()
        self.alert_history = []
        self.state_dir = state_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "state"
        )
        self._load_peak()
        self._sector_cache = {}
        self._sector_cache_ts = 0

    def _peak_file(self) -> str:
        os.makedirs(self.state_dir, exist_ok=True)
        return os.path.join(self.state_dir, "phoenix_risk_peak.json")

    def _load_peak(self):
        """从磁盘恢复组合高点"""
        try:
            with open(self._peak_file()) as f:
                data = json.load(f)
                self.portfolio_peak = data.get("peak", CAPITAL["total"])
        except Exception:
            self.portfolio_peak = CAPITAL["total"]

    def _save_peak(self):
        """持久化组合高点"""
        try:
            with open(self._peak_file(), "w") as f:
                json.dump({"peak": self.portfolio_peak,
                           "updated": datetime.datetime.now().isoformat()}, f)
        except Exception:
            pass

    def _get_sector(self, symbol: str) -> str:
        """Get sector from yfinance with 24h cache"""
        now = datetime.datetime.now().timestamp()
        if symbol in self._sector_cache and (now - self._sector_cache_ts) < 86400:
            return self._sector_cache[symbol]
        try:
            info = _get_cached_ticker_info(symbol)
            sector = info.get("sector", "") or info.get("industry", "") or "其他"
            self._sector_cache[symbol] = sector
            self._sector_cache_ts = now
            return sector
        except Exception:
            return "其他"

    def check_overall_drawdown(self, current_value: float) -> dict:
        """检查整体组合回撤"""
        if current_value > self.portfolio_peak:
            self.portfolio_peak = current_value
            self._save_peak()
        
        drawdown = (current_value - self.portfolio_peak) / self.portfolio_peak
        
        status = "OK"
        action = None
        
        if drawdown <= -RISK.get("max_overall_drawdown", 0.25):
            status = "CRITICAL"
            action = {
                "type": "reduce_all",
                "pct": 0.20,  # 减仓 20%
                "reason": f"整体回撤 {drawdown*100:.1f}% 超过 {RISK['max_overall_drawdown']*100:.0f}% 上限",
            }
        elif drawdown <= -RISK.get("max_drawdown_alert", 0.20):
            status = "WARNING"
            action = {
                "type": "alert",
                "reason": f"整体回撤 {drawdown*100:.1f}%，接近危险区域",
            }
        
        result = {
            "current_value": current_value,
            "peak": self.portfolio_peak,
            "drawdown": round(drawdown, 4),
            "drawdown_pct": round(drawdown * 100, 2),
            "status": status,
            "action": action,
        }
        
        if status != "OK":
            logger.warning(f"⚠️ 风险检查: {status} — 回撤 {result['drawdown_pct']}%")
        
        return result

    def check_concentration(self, positions: dict) -> list[dict]:
        """检查持仓集中度"""
        alerts = []
        
        # 按股票比例检查
        total_value = sum(p.get("value", 0) for p in positions.values())
        if total_value <= 0:
            return alerts
        
        for symbol, pos in positions.items():
            pct = pos.get("value", 0) / total_value
            if pct > RISK.get("max_single_position", 0.15):
                alerts.append({
                    "type": "single_position",
                    "severity": "WARNING",
                    "symbol": symbol,
                    "pct": round(pct * 100, 1),
                    "limit": RISK["max_single_position"] * 100,
                    "suggested_action": f"减仓 {symbol} 到 {RISK['max_single_position']*100:.0f}%",
                })
        
        # Check by sector
        sector_values = {}
        for symbol, pos in positions.items():
            sector = self._get_sector(symbol)
            sector_values[sector] = sector_values.get(sector, 0) + pos.get("value", 0)
        
        for sector, val in sector_values.items():
            pct = val / total_value
            if pct > RISK.get("max_single_sector", 0.30):
                alerts.append({
                    "type": "sector_concentration",
                    "severity": "WARNING",
                    "sector": sector,
                    "pct": round(pct * 100, 1),
                    "limit": RISK["max_single_sector"] * 100,
                    "suggested_action": f"减少 {sector} 行业持仓",
                })
        
        return alerts

    def check_market_trend(self) -> dict:
        """检查市场趋势（200日均线）"""
        sp500_pct = self.thermometer.get_sp500_sma200_pct()
        
        result = {
            "above_sma200": sp500_pct >= 0,
            "sma200_pct": round(sp500_pct * 100, 2),
        }
        
        if sp500_pct < 0:
            result["action"] = {
                "type": "reduce_risk",
                "reason": f"标普500低于200日均线 {abs(sp500_pct)*100:.1f}%",
                "suggested": "减少多头敞口 + 增加现金仓位",
                "cash_raise": RISK.get("cash_raise_on_downtrend", 0.10),
            }
        
        if sp500_pct < 0:
            logger.info(f"📉 标普500在200日均线下方 {abs(sp500_pct)*100:.1f}%")
        else:
            logger.debug(f"标普500在200日均线上方 {sp500_pct*100:.1f}%")
        
        return result

    def check_liquidity(self, positions: dict) -> list[dict]:
        """检查持仓流动性"""
        alerts = []
        min_volume = RISK.get("min_daily_volume", 500000)
        
        for symbol in positions:
            try:
                info = _get_cached_ticker_info(symbol)
                avg_volume = info.get("averageVolume", 0) or info.get("volume", 0) or 0
                
                if avg_volume < min_volume:
                    alerts.append({
                        "type": "liquidity",
                        "severity": "WARNING",
                        "symbol": symbol,
                        "avg_volume": avg_volume,
                        "min_required": min_volume,
                    })
                    logger.warning(f"⚠️ {symbol} 流动性不足: 日均量 {avg_volume} < {min_volume}")
            except Exception:
                pass
        
        return alerts

    def full_check(self, positions: dict = None, current_value: float = None) -> dict:
        """
        执行完整的风险检查。
        
        返回：
            {
                "pass": bool,                 # 是否全部通过
                "alerts": [...],              # 所有警报
                "drawdown": {...},            # 回撤详情
                "concentration": [...],       # 集中度
                "market_trend": {...},        # 市场趋势
                "liquidity": [...],           # 流动性
                "actions_required": [...],    # 需要执行的操作
                "thermometer": {...},         # 市场温度计快照
            }
        """
        logger.info("═══════ Phoenix 风险检查开始 ═══════")
        
        if current_value is None:
            current_value = CAPITAL["total"]
        if positions is None:
            positions = {}
        
        alerts = []
        actions = []
        
        # 1. 整体回撤
        drawdown = self.check_overall_drawdown(current_value)
        if drawdown["status"] != "OK":
            alerts.append({"category": "drawdown", **drawdown})
            if drawdown.get("action"):
                actions.append(drawdown["action"])
        
        # 2. 集中度
        concentration = self.check_concentration(positions)
        alerts.extend(concentration)
        for c in concentration:
            if c.get("suggested_action"):
                actions.append(c)
        
        # 3. 市场趋势
        trend = self.check_market_trend()
        if trend.get("action"):
            alerts.append({"category": "market_trend", **trend})
            actions.append(trend["action"])
        
        # 4. 流动性
        liquidity = self.check_liquidity(positions)
        alerts.extend(liquidity)
        
        # 5. 市场温度
        thermo = self.thermometer.comprehensive_score()
        
        pass_check = (len(alerts) == 0)  # 任何警报都不应被忽视
        
        result = {
            "pass": pass_check,
            "timestamp": datetime.datetime.now().isoformat(),
            "alerts": alerts,
            "alerts_count": len(alerts),
            "drawdown": drawdown,
            "concentration": concentration,
            "market_trend": trend,
            "liquidity": liquidity,
            "thermometer": thermo,
            "actions_required": actions,
        }
        
        if pass_check:
            logger.info("✅ 风险检查通过")
        else:
            logger.warning(f"⚠️ 风险检查发现 {len(alerts)} 个警报，需要 {len(actions)} 个操作")
        
        return result


_risk_monitor: RiskMonitor = None

def get_risk_monitor() -> RiskMonitor:
    global _risk_monitor
    if _risk_monitor is None:
        _risk_monitor = RiskMonitor()
    return _risk_monitor

def full_risk_check(positions=None, current_value=None) -> dict:
    return get_risk_monitor().full_check(positions, current_value)
