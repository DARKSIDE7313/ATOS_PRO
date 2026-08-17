"""
ATOS PRO - Daily Report Generator
Usage: python3 -m atos.reporting.daily_report
"""
import json, os, sys, threading, datetime, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587
SMTP_USER  = os.environ.get("ATOS_EMAIL_USER", "")
SMTP_PASS  = os.environ.get("ATOS_EMAIL_PASS", "")
RECIPIENTS = ["9275945.yaocp@gmail.com", "dean080207@gmail.com"]
FUTU_HOST  = "127.0.0.1"
FUTU_PORT  = 11111
ACC_ID     = 19489722


def fetch_account_data():
    try:
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm, RET_OK
    except ImportError:
        print("[错误] futu-api 未安装，请执行: pip install futu-api")
        return None, None

    def _open_trade_ctx_with_timeout(timeout=15):
        """OpenSecTradeContext 构造器内部无限重试（如需要图形验证码），主线程会被永久阻塞。
        在 daemon 线程中构造，超时即放弃（一次性脚本，daemon 线程随进程退出）。"""
        result = {}
        def _worker():
            try:
                result["ctx"] = OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                                                    host=FUTU_HOST, port=FUTU_PORT,
                                                    security_firm=SecurityFirm.FUTUINC)
            except Exception as e:
                result["err"] = e
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout)
        ctx = result.get("ctx")
        if ctx is None:
            return None, result.get("err", TimeoutError(f"Futu OpenD 连接超时 {timeout}s"))
        return ctx, None

    try:
        ctx, err = _open_trade_ctx_with_timeout()
        if ctx is None:
            raise RuntimeError(f"Futu OpenD 不可用: {err}")
        try:
            ret_acc, acc_data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
            ret_pos, pos_data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        finally:
            ctx.close()
        if ret_acc != RET_OK:
            print(f"[错误] 无法获取账户信息: {acc_data}")
            return _fallback_account_from_state()
        account = {
            "cash":       float(acc_data["cash"].iloc[0]),
            "market_val": float(acc_data["market_val"].iloc[0]),
            "total":      float(acc_data["total_assets"].iloc[0]),
        }
        positions = []
        if ret_pos == RET_OK and not pos_data.empty:
            for _, row in pos_data.iterrows():
                positions.append({
                    "code":   row["code"],
                    "qty":    int(row["qty"]),
                    "cost":   float(row["cost_price"]),
                    "last":   float(row["nominal_price"]),
                    "pl_val": float(row["pl_val"]),
                })
        print('[OK] 账户: 总资产 ' + '$' + f"{account['total']:,.2f}")
        print(f"[OK] 持仓: {len(positions)} 支")
        return account, positions
    except Exception as e:
        print(f"[错误] 连接 Futu OpenD 失败: {e}")
        print("尝试从 shadow_state.json 回退读取账户数据…")
        return _fallback_account_from_state()


def _fallback_account_from_state():
    """Futu OpenD 不可用（如需要图形验证码）时，从 shadow_state.json（系统数据源）回退。"""
    state_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "shadow_state.json")
    s = json.load(open(state_path, encoding="utf-8"))
    positions = []
    for code, p in s.get("positions", {}).items():
        qty = int(p.get("qty", 0) or p.get("shares", 0))
        avg = float(p.get("avg_price", 0))
        last = float(p.get("last_price", avg))
        positions.append({
            "code":   code,
            "qty":    qty,
            "cost":   avg,
            "last":   last,
            "pl_val": (last - avg) * qty,
        })
    equity = float(s.get("equity", 0))
    cash = float(s.get("cash", 0))
    account = {
        "cash":       cash,
        "market_val": equity - cash,
        "total":      equity,
        "initial":    float(s.get("initial_cash", 100000)),
        "_source":    "shadow_state.json",
    }
    print(f"[OK] 回退账户数据 shadow_state.json: 总资产 ${equity:,.2f} / 持仓 {len(positions)} 支")
    return account, positions


def fetch_market_regime():
    try:
        import yfinance as yf
        from atos.market.regime.regime_engine import RegimeEngine
        spy = yf.download("SPY",  period="1y", interval="1d", progress=False)
        vix = yf.download("^VIX", period="1y", interval="1d", progress=False)
        engine = RegimeEngine()
        spy_c  = spy["Close"].squeeze().tolist()
        vix_c  = vix["Close"].squeeze().tolist()
        for i in range(min(len(spy_c), len(vix_c))):
            engine.update(float(spy_c[i]), float(vix_c[i]))
        regime    = engine.get_regime()
        spy_price = float(spy_c[-1])
        vix_price = float(vix_c[-1])
        print(f"[OK] 市场状态: {regime['regime']} | SPY={spy_price:.2f} | VIX={vix_price:.1f}")
        return regime, spy_price, vix_price
    except Exception as e:
        print(f"[警告] 无法获取市场数据: {e}")
        return {"regime": "UNKNOWN", "risk_multiplier": 0.5}, 0.0, 0.0


def build_html_report(account, positions, regime, spy_price, vix_price):
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M HKT")
    colors = {"BULL_STRONG": "#16a34a", "BULL_WEAK": "#ca8a04",
              "HIGH_VOL": "#ea580c", "BEAR": "#dc2626", "UNKNOWN": "#6b7280"}
    labels = {"BULL_STRONG": "强势牛市 green", "BULL_WEAK": "弱势牛市 yellow",
              "HIGH_VOL": "高波动 orange", "BEAR": "熊市 red", "UNKNOWN": "未知"}
    r_color = colors.get(regime["regime"], "#6b7280")
    r_label = labels.get(regime["regime"], "未知")
    rows = ""
    if positions:
        for p in positions:
            c = "#16a34a" if p["pl_val"] >= 0 else "#dc2626"
            s = "+" if p["pl_val"] >= 0 else ""
            rows += ("<tr>"
                     f"<td style='padding:10px 14px;font-weight:600;'>{p['code'].replace('US.','')}</td>"
                     f"<td style='padding:10px 14px;text-align:right;'>{p['qty']}</td>"
                     f"<td style='padding:10px 14px;text-align:right;'>${p['cost']:.2f}</td>"
                     f"<td style='padding:10px 14px;text-align:right;'>${p['last']:.2f}</td>"
                     f"<td style='padding:10px 14px;text-align:right;color:{c};font-weight:600;'>{s}${p['pl_val']:,.2f}</td>"
                     "</tr>")
    else:
        rows = "<tr><td colspan='5' style='text-align:center;color:#9ca3af;padding:16px;'>目前无持仓</td></tr>"
    if account:
        tp = account["total"] - account.get("initial", 100000)
        pc = "#16a34a" if tp >= 0 else "#dc2626"
        ps = "+" if tp >= 0 else ""
        acc_html = (f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px;'>"
                    f"<div style='flex:1;min-width:148px;background:#f9fafb;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb;'><div style='font-size:12px;color:#6b7280;'>总资产</div><div style='font-size:22px;font-weight:700;'>${account['total']:,.2f}</div></div>"
                    f"<div style='flex:1;min-width:148px;background:#f9fafb;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb;'><div style='font-size:12px;color:#6b7280;'>可用现金</div><div style='font-size:22px;font-weight:700;'>${account['cash']:,.2f}</div></div>"
                    f"<div style='flex:1;min-width:148px;background:#f9fafb;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb;'><div style='font-size:12px;color:#6b7280;'>持仓市值</div><div style='font-size:22px;font-weight:700;'>${account['market_val']:,.2f}</div></div>"
                    f"<div style='flex:1;min-width:148px;background:#f9fafb;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb;'><div style='font-size:12px;color:#6b7280;'>累计盈亏</div><div style='font-size:22px;font-weight:700;color:{pc};'>{ps}${tp:,.2f}</div></div>"
                    "</div>")
        if account.get("_source"):
            acc_html += (f"<p style='font-size:11px;color:#94a3b8;margin:0 0 12px;'>"
                         f"⚠️ 数据源: {account['_source']} — Futu OpenD 需人工登录（图形验证码），本次使用本地状态文件。</p>")
    else:
        acc_html = "<p style='color:#ef4444;'>无法连接 Futu OpenD，账户数据不可用</p>"
    risk = regime["risk_multiplier"]
    html = ("<!DOCTYPE html><html lang='zh'><head><meta charset='UTF-8'><title>ATOS PRO 每日报告</title></head>"
            "<body style='margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'>"
            "<div style='max-width:680px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);'>"
            "<div style='background:#0f172a;padding:28px 32px;'>"
            "<div style='font-size:11px;letter-spacing:2px;color:#94a3b8;text-transform:uppercase;'>ATOS PRO</div>"
            "<div style='font-size:22px;font-weight:700;color:#fff;margin-top:4px;'>每日投资报告</div>"
            f"<div style='font-size:13px;color:#64748b;margin-top:6px;'>{today}</div></div>"
            "<div style='padding:28px 32px;'>"
            "<h2 style='font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 12px;'>市场状态</h2>"
            "<div style='background:#f9fafb;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb;margin-bottom:24px;display:flex;align-items:center;gap:20px;flex-wrap:wrap;'>"
            f"<div style='font-size:18px;font-weight:700;color:{r_color};'>{r_label}</div>"
            f"<div style='color:#6b7280;font-size:14px;'>SPY <strong style='color:#111827;'>{spy_price:.2f}</strong></div>"
            f"<div style='color:#6b7280;font-size:14px;'>VIX <strong style='color:#111827;'>{vix_price:.1f}</strong></div>"
            f"<div style='color:#6b7280;font-size:14px;'>风险系数 <strong style='color:#111827;'>{risk}</strong></div></div>"
            "<h2 style='font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 12px;'>账户资金（模拟盘）</h2>"
            f"{acc_html}"
            "<h2 style='font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 12px;'>持仓明细</h2>"
            "<table style='width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px;'>"
            "<thead><tr style='background:#f9fafb;'><th style='padding:10px 14px;text-align:left;font-weight:600;color:#6b7280;font-size:12px;'>股票</th><th style='padding:10px 14px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;'>数量</th><th style='padding:10px 14px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;'>成本价</th><th style='padding:10px 14px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;'>现价</th><th style='padding:10px 14px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;'>盈亏</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            "<div style='font-size:12px;color:#9ca3af;border-top:1px solid #f3f4f6;padding-top:16px;'>本报告由 ATOS PRO 自动生成，仅供参考。数据来源：富途模拟盘 / Yahoo Finance。</div>"
            "</div></div></body></html>")
    return html


def save_report(html):
    reports_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    filename = datetime.datetime.now().strftime("report_%Y%m%d_%H%M.html")
    filepath = os.path.join(reports_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] 报告已保存: {filepath}")
    return filepath


def send_email(html):
    if not SMTP_USER or not SMTP_PASS:
        print("[跳过] 未设置邮件凭据，不发送邮件")
        return
    subject = f"ATOS PRO 每日报告 {datetime.datetime.now().strftime('%Y-%m-%d')}"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ", ".join(RECIPIENTS)
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, RECIPIENTS, msg.as_string())
        print(f"[OK] 邮件已发送")
    except Exception as e:
        print(f"[错误] 邮件发送失败: {e}")


def main():
    print("\n=== ATOS PRO 每日报告 ===")
    print(f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}") 
    print("-" * 40)
    try:
        account, positions           = fetch_account_data()
    except Exception as e:
        print(f"[WARN] 账户数据获取失败: {e}")
        account, positions = {}, []
    try:
        regime, spy_price, vix_price = fetch_market_regime()
    except Exception as e:
        print(f"[WARN] 市场状态获取失败: {e}")
        regime, spy_price, vix_price = {}, 0, 0
    html = build_html_report(account, positions, regime, spy_price, vix_price)
    save_report(html)
    # 邮件发送失败不阻断报告生成
    try:
        send_email(html)
    except Exception as e:
        print(f"[WARN] 邮件发送失败: {e}")
    print("-" * 40)
    print("[完成] 每日报告生成成功")


if __name__ == "__main__":
    main()
    # futu-api 构造器失败后其内部非 daemon 网络线程会无限重试，阻止解释器正常退出。
    # 一次性脚本：main() 完成即代表报告已保存/邮件已发送，直接硬退出。
    os._exit(0)
