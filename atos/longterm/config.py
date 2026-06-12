"""
ATOS PRO v2 — Phoenix 策略配置文件
=====================================
Phoenix 长线综合策略所有可调参数集中管理。
"""

# ═══════════════════════════════════════════
# 资金配置（共享配置 → 避免长短期策略抢资金）
# ═══════════════════════════════════════════
from atos.config_shared import ALLOCATION

CAPITAL = {
    "total": ALLOCATION["long_term"],   # Fix #1: 从 config_shared 读取，不再硬编码
    "layer1_pct": 0.30,
    "layer2_pct": 0.50,
    "layer3_pct": 0.20,
    "cash_reserve_pct": 0.05,
}

# 现金储备绝对值（简化 CASH_RESERVE 访问）
CASH_RESERVE = CAPITAL["total"] * CAPITAL["cash_reserve_pct"]  # $30,000

# ═══════════════════════════════════════════
# Layer 1: 基础层配置
# ═══════════════════════════════════════════
LAYER1 = {
    "aristocrats_pct": 0.50,       # 股息贵族子组合占比
    "dca_pct": 0.50,               # 指数定投子组合占比
    
    # 股息贵族选择
    "aristocrat_min_div_yield": 0.02,        # 最低股息率 2%
    "aristocrat_min_years_growth": 10,        # 至少 10 年连续分红增长
    "aristocrat_max_payout_ratio": 0.60,      # 最高派息率 60%
    "aristocrat_min_roe": 0.12,               # 最低 ROE 12%
    "aristocrat_max_debt_equity": 80,         # 最高负债权益比
    "aristocrat_position_count": 15,          # 持仓 15 只
    "aristocrat_fallback_etf": "NOBL",        # 买不到个股时的 ETF 替代
    
    # 指数增强定投
    "dca_etf": "VOO",                         # 定投标的
    "dca_period_days": 15,                    # 每 15 天定投一次
    "dca_base_amount": 30000,                 # 基础定投金额（当 PE 正常时）
    "dca_min_pe_for_double": 17,              # PE < 17 → 双倍投
    "dca_max_pe_for_half": 28,                # PE > 28 → 减半
    "dca_max_pe_for_quarter": 35,             # PE > 35 → 四分之一
    "dca_pe_source": "sp500",                 # PE 数据来源
    
    # 再平衡
    "rebalance_frequency_days": 183,          # 每 183 天（约半年）再平衡
    "rebalance_deviation_threshold": 0.10,    # 偏离 10% 才触发再平衡
}

# ═══════════════════════════════════════════
# Layer 2: 核心层配置
# ═══════════════════════════════════════════
LAYER2 = {
    "quality_pct": 0.50,           # 质量因子子组合占比
    "multifactor_pct": 0.50,       # 多因子子组合占比
    
    # 质量因子
    "quality_min_score": 75,       # 最低质量评分
    "quality_top_n": 20,           # 选 Top 20
    "quality_min_market_cap": 2e9, # 最低市值 20 亿
    "quality_min_daily_volume": 1e6, # 最低日均交易量 100 万
    
    # 多因子排名
    "multifactor_top_n": 25,       # 选 Top 25
    "multifactor_weight_value": 0.30,
    "multifactor_weight_quality": 0.30,
    "multifactor_weight_momentum": 0.20,
    "multifactor_weight_lowvol": 0.20,
    
    # 股票池
    "universe_source": "sp500",    # 可选 sp500 / nasdaq100 / full
    "universe_custom": [],         # 自定义补充
    
    # 再平衡
    "rebalance_frequency_days": 91,  # 每季度
    "rebalance_deviation_threshold": 0.05,
    
    # 退出条件
    "sell_debt_equity_max": 200,   # 负债权益比超过此值 → 卖出
    "sell_roe_min": 0,             # ROE 低于此值 → 卖出
    "sell_revenue_decline": -0.20, # 营收下降超过此比例 → 减仓
}

# ═══════════════════════════════════════════
# Layer 3: 战术层配置
# ═══════════════════════════════════════════
LAYER3 = {
    # 因子 ETF 轮动
    "factor_rotation_pct": 0.40,
    "factor_etfs": {
        "USMV": "低波动", "QUAL": "质量", "SLYV": "小盘价值",
        "MTUM": "动量", "VLUE": "价值", "SPHQ": "质量加权",
    },
    "factor_momentum_months": 3,          # 3 个月动量
    "factor_top_n": 2,                     # 选 Top 2
    
    # 行业轮动
    "sector_rotation_pct": 0.30,
    "sector_etfs": {
        "XLK": "科技", "XLF": "金融", "XLV": "医疗",
        "XLE": "能源", "XLI": "工业", "XLP": "必选消费",
        "XLY": "可选消费", "XLU": "公用事业", "XLRE": "房地产",
        "XLB": "材料", "XLC": "通信",
    },
    "sector_momentum_months": 3,
    "sector_top_n": 1,
    
    # 内部人追踪
    "insider_pct": 0.20,
    "insider_min_purchase": 500000,       # 最低买入 $50 万
    "insider_max_hold": 180,              # 最多持有 180 天
    "insider_top_n": 3,
}

# ═══════════════════════════════════════════
# 风险控制配置
# ═══════════════════════════════════════════
RISK = {
    "max_overall_drawdown": 0.25,          # 最大整体回撤 25%
    "max_drawdown_alert": 0.20,            # 回撤 20% 时报警
    "max_single_position": 0.15,           # 单只股票上限 15%
    "max_single_sector": 0.30,             # 单行业上限 30%
    "max_single_etf": 0.25,               # 单 ETF 上限 25%
    "min_daily_volume": 500000,            # 最小日交易量
    "mandatory_reduce_on_drawdown": 0.25,  # 回撤到 25% 强制减仓 20%
    
    "sp500_sma200_sell": True,            # 跌破 200日均线时减少多头
    "cash_raise_on_downtrend": 0.10,       # 跌破 200日线加 10% 现金
    
    # 市场温度计阈值
    "market_pe_extreme_low": 10,           # PE < 10 → 极度低估
    "market_pe_extreme_high": 30,          # PE > 30 → 极度高估
    "vix_extreme_high": 40,                # VIX > 40 → 极度恐慌
    "vix_extreme_low": 12,                 # VIX < 12 → 极度贪婪
    
    # 回撤买入触发阈值（从 LAYER3 移到这里因为这是风控功能）
    "dip_buy_thresholds": {
        -0.05: 0.10,  # 跌 5% 投入 10%
        -0.10: 0.20,  # 跌 10% 投入 20%
        -0.15: 0.30,  # 跌 15% 投入 30%
        -0.20: 0.40,  # 跌 20% 投入 40%
    },
    "dip_buy_etf": "VOO",
}

# ═══════════════════════════════════════════
# 调度配置
# ═══════════════════════════════════════════
SCHEDULE = {
    "layer1_interval_minutes": 60 * 24 * 15,  # 每 15 天
    "layer2_interval_minutes": 60 * 24 * 91,  # 每 91 天
    "layer3_interval_minutes": 60 * 24 * 30,  # 每 30 天
    "risk_check_interval_minutes": 60 * 24,   # 每天
    "dip_check_interval_minutes": 60,         # 每小时检查回撤
    "report_interval_minutes": 60 * 24 * 30,  # 月报
}
