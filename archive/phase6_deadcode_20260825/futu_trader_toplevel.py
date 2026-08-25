from futu import *

try:
    from atos.config_shared import FUTU
    ACC_ID = FUTU["acc_id"]
    HOST = FUTU["host"]
    PORT = FUTU["port"]
    TRD_ENV = TrdEnv.REAL if FUTU["trd_env"] == "REAL" else TrdEnv.SIMULATE
except ImportError:
    ACC_ID = 19489722
    HOST = '127.0.0.1'
    PORT = 11111
    TRD_ENV = TrdEnv.SIMULATE

def _tcp_ok(host: str, port: int) -> bool:
    """H7: TCP 预检查 — 防止 Futu Context 构造函数内部重试阻塞 (Pattern 92/94)"""
    import socket
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def place_order(ticker, side, quantity):
    """
    side: 'BUY' or 'SELL'
    """
    if not _tcp_ok(HOST, PORT):
        print(f"[错误] FutuOpenD 不可达 {HOST}:{PORT}")
        return None
    trd_side = TrdSide.BUY if side == 'BUY' else TrdSide.SELL
    symbol = f"US.{ticker}"

    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=HOST,
        port=PORT,
        security_firm=SecurityFirm.FUTUINC
    )

    # 获取当前报价
    quote_ctx = OpenQuoteContext(host=HOST, port=PORT)
    ret, data = quote_ctx.get_market_snapshot([symbol])
    quote_ctx.close()

    if ret != RET_OK:
        print(f"[错误] 无法获取 {ticker} 报价: {data}")
        ctx.close()
        return None

    price = float(data['last_price'].iloc[0])
    print(f"当前价格: ${price:.2f}")

    # 下单
    ret, data = ctx.place_order(
        qty=quantity,
        code=symbol,
        trd_side=trd_side,
        order_type=OrderType.MARKET,  # L7: 市价单不传 price
        trd_env=TRD_ENV,
        acc_id=ACC_ID
    )
    ctx.close()

    if ret != RET_OK:
        print(f"[错误] 下单失败: {data}")
        return None

    order_id = data['order_id'].iloc[0]
    print(f"[成功] {side} {quantity}股 {ticker} @ ${price:.2f} | 订单ID: {order_id}")
    return order_id

def get_positions():
    """查看当前持仓"""
    if not _tcp_ok(HOST, PORT):
        print(f"[错误] FutuOpenD 不可达 {HOST}:{PORT}")
        return None
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=HOST,
        port=PORT,
        security_firm=SecurityFirm.FUTUINC
    )
    ret, data = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    ctx.close()

    if ret != RET_OK:
        print(f"[错误] 无法获取持仓: {data}")
        return

    if data.empty:
        print("当前无持仓")
    else:
        print("=== 当前持仓 ===")
        for _, row in data.iterrows():
            print(f"  {row['code']} | 数量: {row['qty']} | 成本: ${float(row['cost_price']):.2f} | 现价: ${float(row['nominal_price']):.2f} | 盈亏: ${float(row['pl_val']):.2f}")

def get_account_info():
    """查看账户资金"""
    if not _tcp_ok(HOST, PORT):
        print(f"[错误] FutuOpenD 不可达 {HOST}:{PORT}")
        return None
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=HOST,
        port=PORT,
        security_firm=SecurityFirm.FUTUINC
    )
    ret, data = ctx.accinfo_query(trd_env=TRD_ENV, acc_id=ACC_ID)
    ctx.close()

    if ret != RET_OK:
        print(f"[错误] 无法获取账户信息: {data}")
        return

    cash = float(data['cash'].iloc[0])
    total = float(data['total_assets'].iloc[0])
    market_val = float(data['market_val'].iloc[0])
    print(f"=== 账户资金 ===")
    print(f"  现金: ${cash:,.2f}")
    print(f"  持仓市值: ${market_val:,.2f}")
    print(f"  总资产: ${total:,.2f}")
