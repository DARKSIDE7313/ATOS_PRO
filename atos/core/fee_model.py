#!/usr/bin/env python3
"""
ATOS Futu Fee Model
===================
富途美股交易费用模型（2025 年标准费率）

费用构成:
1. 佣金 (Commission): $0.0049/股, 最低 $0.99/笔
2. 平台费 (Platform Fee): $0.005/股, 最低 $1.00/笔
3. SEC 费 (卖单): $0.0000278 × 成交金额
4. FINRA TAF (卖单): $0.000166/股, 最高 $8.30
5. 交收费 (Settlement): $0.003/股

来源: https://www.futu5.com/about/fees
"""

def futu_buy_fee(shares: int, price: float) -> float:
    """买入费用"""
    if shares <= 0 or price <= 0:
        return 0.0
    commission = max(shares * 0.0049, 0.99)
    platform = max(shares * 0.005, 1.00)
    settlement = shares * 0.003
    return round(commission + platform + settlement, 2)

def futu_sell_fee(shares: int, price: float) -> float:
    """卖出费用（含 SEC + FINRA）"""
    if shares <= 0 or price <= 0:
        return 0.0
    amount = shares * price
    commission = max(shares * 0.0049, 0.99)
    platform = max(shares * 0.005, 1.00)
    settlement = shares * 0.003
    sec_fee = amount * 0.0000278
    taf = min(shares * 0.000166, 8.30)
    return round(commission + platform + settlement + sec_fee + taf, 2)

def round_trip_fee(shares: int, buy_price: float, sell_price: float) -> float:
    """往返费用"""
    return futu_buy_fee(shares, buy_price) + futu_sell_fee(shares, sell_price)

def fee_pct(shares: int, price: float, side: str = "buy") -> float:
    """费用占交易额百分比"""
    amount = shares * price
    if amount <= 0:
        return 0.0
    fee = futu_buy_fee(shares, price) if side == "buy" else futu_sell_fee(shares, price)
    return fee / amount

def min_profit_to_cover_fees(shares: int, price: float) -> float:
    """最低涨幅才能覆盖往返手续费（百分比）"""
    if shares <= 0 or price <= 0:
        return 0.01
    buy_f = futu_buy_fee(shares, price)
    # 卖出时价格略高，用近似
    sell_f = futu_sell_fee(shares, price * 1.01)
    total = buy_f + sell_f
    amount = shares * price
    return total / amount

# ── 常用计算 ──
def estimate_annual_fee_drag(
    portfolio_value: float = 300_000,
    avg_position_size: float = 30_000,
    trades_per_month: int = 8,
    avg_price: float = 200.0,
) -> dict:
    """估算年度费用拖累"""
    shares_per_trade = int(avg_position_size / avg_price)
    fee_per_round_trip = round_trip_fee(shares_per_trade, avg_price, avg_price * 1.02)
    annual_trades = trades_per_month * 12
    annual_fees = fee_per_round_trip * annual_trades
    drag_pct = annual_fees / portfolio_value
    return {
        'fee_per_round_trip': fee_per_round_trip,
        'annual_trades': annual_trades,
        'annual_fees': round(annual_fees, 2),
        'annual_drag_pct': round(drag_pct * 100, 3),
        'breakeven_extra_return': round(drag_pct * 100, 3),
    }

if __name__ == '__main__':
    print("=" * 50)
    print("📊 Futu 费用模型")
    print("=" * 50)

    # 示例: 买 100 股 AAPL @ $300
    shares, price = 100, 300.0
    bf = futu_buy_fee(shares, price)
    sf = futu_sell_fee(shares, price * 1.05)
    rt = bf + sf
    amount = shares * price

    print(f"\n示例: {shares}股 @ ${price}")
    print(f"  交易额: ${amount:,.0f}")
    print(f"  买入费: ${bf:.2f} ({bf/amount*100:.3f}%)")
    print(f"  卖出费: ${sf:.2f} ({sf/(shares*price*1.05)*100:.3f}%)")
    print(f"  往返费: ${rt:.2f} ({rt/amount*100:.3f}%)")
    print(f"  最低盈利覆盖费用: {min_profit_to_cover_fees(shares, price)*100:.2f}%")

    # 年度拖累
    est = estimate_annual_fee_drag()
    print(f"\n年度费用估算 ($300K 组合, 每月8笔):")
    print(f"  每笔往返: ${est['fee_per_round_trip']:.2f}")
    print(f"  年交易: {est['annual_trades']} 笔")
    print(f"  年费用: ${est['annual_fees']:,.2f}")
    print(f"  年拖累: {est['annual_drag_pct']}%")
    print(f"  → 策略需额外赚 {est['breakeven_extra_return']}% 才能覆盖费用")
