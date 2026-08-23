"""
ATOS PRO — 跨周期状态对象 (Phase 5 框架重塑)
==============================================
历史问题 (报告 §7.1.2): 跨周期状态用 **函数属性** 存储
(run_shadow_cycle._prev_scores / ._ic_ema / ._regime_engine 等),
不可测试、重启即丢、散落在主循环各处。

本模块把这些状态收进一个显式单例对象。内存态语义与原函数属性完全一致
(进程生命周期内生效, 重启重置) — 行为不变, 但可注入、可单测。

字段一览 (原函数属性 → 对象字段):
  _regime_engine     → regime_engine      (RegimeEngine 持久化实例)
  _prev_scores       → prev_scores        (IC 反馈: 上周期因子分数)
  _prev_breakdown    → prev_breakdown     (IC 反馈: 上周期因子明细)
  _prev_prices       → prev_prices        (IC 反馈: 上周期价格)
  _prev_rsi          → prev_rsi           (IC 反馈: 上周期 RSI)
  _ic_ema            → ic_ema             (IC 指数平滑)
  _ic_inverted       → ic_inverted        (IC 方向反转标记)
  _ic_invert_logged  → ic_invert_logged   (反转日志去重)
  _last_news_fetch   → last_news_fetch    (新闻抓取节流, epoch 秒)
  _last_vibe         → last_vibe          (Vibe Swarm 节流, epoch 秒)
  _last_intel_cycle  → last_intel_cycle   (情报简报节流, 周期号)
  _perf_inited       → perf_inited        (绩效追踪器初始化标记)
"""

import threading


class CycleState:
    """跨周期内存状态 — 线程安全单例"""

    _instance = None
    _lock = threading.RLock()

    def __init__(self):
        self.regime_engine = None     # RegimeEngine 实例 (惰性创建)
        self.prev_scores = {}
        self.prev_breakdown = {}
        self.prev_prices = {}
        self.prev_rsi = {}
        self.ic_ema = None
        self.ic_inverted = False
        self.ic_invert_logged = False
        self.last_news_fetch = 0.0
        self.last_vibe = 0.0
        self.last_intel_cycle = -999
        self.perf_inited = False

    @classmethod
    def get(cls) -> "CycleState":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def reset(self):
        """测试用: 重置全部状态"""
        with self._lock:
            self.__init__()
