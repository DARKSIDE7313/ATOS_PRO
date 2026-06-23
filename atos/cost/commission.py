#!/usr/bin/env python3
"""
ATOS 富途手续费计算模块
支持港股、美股，按真实富途规则计算
"""

from typing import Literal


def calculate_commission(
    side: Literal["BUY", "SELL"],
    symbol: str,
    shares: int,
    price: float,
    exchange: Literal["HK", "US"] = "HK"
) -> dict:
    """
    计算单笔交易总成本（佣金 + 其他费用）

    富途真实规则：
    - 港股：佣金 = max(成交金额*0.03%, HK$15)
             + 平台使用费 HK$15
             + 交易征费 0.0027%（仅卖出）
             + 印花税 0.13%（仅卖出）
    - 美股：佣金 = max(成交金额*0.005%, US$0.99)
             + SEC费（仅卖出，极小忽略）

    返回: {"commission": 总费用, "breakdown": 明细字典}
    """
    amount = shares * price

    if exchange == "HK":
        # 佣金
        comm = max(amount * 0.0003, 15.0)
        # 平台使用费（每单固定）
        platform = 15.0
        # 卖出额外费用
        if side == "SELL":
            levy = amount * 0.000027          # 交易征费
            stamp = amount * 0.0013           # 印花税
        else:
            levy = stamp = 0.0
        total = round(comm + platform + levy + stamp, 2)

        breakdown = {
            "佣金": round(comm, 2),
            "平台使用费": platform,
            "交易征费": round(levy, 2),
            "印花税": round(stamp, 2),
        }

    else:  # US
        comm = max(amount * 0.00005, 0.99)
        # 卖出时有极小的 SEC fee，忽略
        total = round(comm, 2)
        breakdown = {"佣金": total}

    return {"commission": total, "breakdown": breakdown}


def apply_commission_to_fill(fill: dict) -> dict:
    """
    给单笔成交记录自动加上手续费
    期望 fill 格式: {"side", "symbol", "shares", "price", "exchange"?}
    """
    result = calculate_commission(
        side=fill["side"],
        symbol=fill["symbol"],
        shares=fill["shares"],
        price=fill["price"],
        exchange=fill.get("exchange", "HK")
    )
    fill["commission"] = result["commission"]
    fill["commission_breakdown"] = result["breakdown"]
    return fill
