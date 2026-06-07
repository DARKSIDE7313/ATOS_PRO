"""
ATOS PRO v2 — 现金管理与抄底触发器
=====================================
在熊市底部、回撤达到阈值或内部人信号强烈时，
自动将现金储备转化为仓位。
"""

import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import os, json
from atos.core.logging import get_logger
from atos.longterm.config import CASH_RESERVE, RISK
from atos.longterm.market_thermometer import MarketThermometer

logger = get_logger("phoenix.cash")

# yfinance 缓存 — 5 分钟内不重复下载/查询
_CASH_YF_CACHE = {}
_CASH_YF_CACHE_TTL = datetime.timedelta(minutes=5)

def _get_cached_cash_ticker_info(symbol: str) -> dict:
    """带缓存的 yfinance Ticker.info 查询"""
    key = f"info:{symbol}"
    now = datetime.datetime.now()
    if key in _CASH_YF_CACHE:
        ts, info = _CASH_YF_CACHE[key]
        if now - ts < _CASH_YF_CACHE_TTL:
            return info
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        info = {}
    _CASH_YF_CACHE[key] = (datetime.datetime.now(), info)
    return info

def _get_cached_cash_history(symbol: str, period="1y", interval="1d"):
    """带缓存的 yfinance history 查询"""
    key = f"hist:{symbol}:{period}:{interval}"
    now = datetime.datetime.now()
    if key in _CASH_YF_CACHE:
        ts, hist = _CASH_YF_CACHE[key]
        if now - ts < _CASH_YF_CACHE_TTL:
            return hist
    try:
        hist = yf.Ticker(symbol).history(period=period, interval=interval)
    except Exception:
        hist = None
    _CASH_YF_CACHE[key] = (datetime.datetime.now(), hist)
    return hist


class CashManager:
    """
    现金管理器：
    1. 平时持有在短债 ETF（SHV）或现金中
    2. 在触发条件时自动转换为仓位
    
    触发器：
    - 标普500 从高点回撤超过阈值
    - 市场温度计进入极度悲观区域
    - 内部人信号强烈（以后扩展）
    """

    def __init__(self, state_dir: str = None):
        self.thermometer = MarketThermometer()
        self.state_dir = state_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "state"
        )
        self._load_peak()
        self.trigger_history = []

    def _peak_file(self) -> str:
        os.makedirs(self.state_dir, exist_ok=True)
        return os.path.join(self.state_dir, "phoenix_cash_peak.json")

    def _load_peak(self):
        try:
            with open(self._peak_file()) as f:
                data = json.load(f)
                self.sp500_peak = data.get("peak", 0)
        except Exception:
            pass
        if not getattr(self, 'sp500_peak', 0) or self.sp500_peak <= 0:
            self.sp500_peak = self._get_sp500_peak()

    def _save_peak(self):
        try:
            with open(self._peak_file(), "w") as f:
                json.dump({"peak": self.sp500_peak,
                           "updated": datetime.datetime.now().isoformat()}, f)
        except Exception:
            pass

    def _get_sp500_peak(self) -> float:
        """获取标普500 过去 52 周最高价，作为'高点'基准"""
        try:
            hist = _get_cached_cash_history("SPY", period="1y", interval="1d")
            if hist is not None and not hist.empty:
                return float(hist["High"].squeeze().max())
        except Exception as e:
            logger.warning(f"获取标普高点失败: {e}")
        return self.sp500_peak if hasattr(self, 'sp500_peak') else 500.0

    def get_sp500_current(self) -> float:
        """获取标普500当前价格"""
        try:
            info = _get_cached_cash_ticker_info("SPY")
            return float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or 500)
        except Exception:
            return self.sp500_peak

    def compute_drawdown(self) -> float:
        """计算标普500当前回撤百分比（从52周高点）"""
        current = self.get_sp500_current()
        peak = self._get_sp500_peak()
        if peak <= 0:
            return 0.0
        drawdown = (current - peak) / peak
        # Update peak on new high
        if current > peak:
            self.sp500_peak = current
            self._save_peak()
            return 0.0
        return drawdown

    def check_dip_trigger(self, current_drawdown: float = None) -> dict:
        """
        检查是否触发了回撤买入条件。
        
        返回：
            triggered: bool — 是否触发
            target_pct: float — 目标买入比例
            reason: str — 触发原因
        """
        if current_drawdown is None:
            current_drawdown = self.compute_drawdown()
        
        result = {
            "triggered": False,
            "target_pct": 0.0,
            "reason": "",
            "current_drawdown": current_drawdown,
        }
        
        # 冷却期：最近 24 小时内触发过就不再触发
        if self.trigger_history:
            last = self.trigger_history[-1]
            if (datetime.datetime.now() - last["time"]) < datetime.timedelta(hours=24):
                return result
        
        thresholds = sorted(RISK.get("dip_buy_thresholds", {}).items())
        
        for threshold_str, pct in thresholds:
            threshold = float(threshold_str)
            if current_drawdown <= threshold:
                result["triggered"] = True
                result["target_pct"] = float(pct)
                result["reason"] = f"标普500回撤超过{abs(threshold)*100:.0f}%"
        
        return result

    def check_pessimism_trigger(self) -> dict:
        """检查市场温度计是否进入极度悲观"""
        thermo = self.thermometer.comprehensive_score()
        score = thermo["score"]
        
        result = {
            "triggered": False,
            "target_pct": 0.0,
            "reason": "",
            "score": score,
        }
        
        if score < -60:
            result["triggered"] = True
            result["target_pct"] = 0.50
            result["reason"] = "市场进入极度悲观区域（分数 < -60）"
        elif score < -30:
            result["triggered"] = True
            result["target_pct"] = 0.25
            result["reason"] = "市场进入悲观区域（分数 < -30）"
        
        return result

    def should_deploy_cash(self) -> dict:
        """
        核心方法：决定是否动用现金储备。
        
        返回：
            deploy: bool — 是否动用
            pct: float — 动用现金储备百分之几
            reason: str — 原因
            total_cash: float — 当前现金总额
            deploy_amount: float — 实际部署金额
        """
        drawdown = self.compute_drawdown()
        
        dip_result = self.check_dip_trigger(drawdown)
        pessimism_result = self.check_pessimism_trigger()
        
        deploy = False
        max_pct = 0.0
        reasons = []
        
        if dip_result["triggered"]:
            deploy = True
            max_pct = max(max_pct, dip_result["target_pct"])
            reasons.append(dip_result["reason"])
        
        if pessimism_result["triggered"]:
            deploy = True
            max_pct = max(max_pct, pessimism_result["target_pct"])
            reasons.append(pessimism_result["reason"])
        
        result = {
            "deploy": deploy,
            "pct": max_pct,
            "reason": " + ".join(reasons) if reasons else "无触发",
            "drawdown": drawdown,
            "temperature": pessimism_result.get("score", 0),
            "deploy_amount": max_pct * CASH_RESERVE,
        }
        
        if deploy:
            logger.info(f"💵 现金部署触发! {result['reason']} — 部署 {max_pct*100:.0f}% 现金")
            self.trigger_history.append({
                "time": datetime.datetime.now(),
                "pct": max_pct,
                "reason": result["reason"],
            })
        else:
            logger.debug(f"现金等待中。回撤: {drawdown*100:.1f}% | 温度: {pessimism_result.get('score',0)}")
        
        return result


# ─── 便捷入口 ───

_cash_manager_instance: CashManager = None

def get_cash_manager() -> CashManager:
    global _cash_manager_instance
    if _cash_manager_instance is None:
        _cash_manager_instance = CashManager()
    return _cash_manager_instance

def should_buy_the_dip() -> dict:
    return get_cash_manager().should_deploy_cash()
