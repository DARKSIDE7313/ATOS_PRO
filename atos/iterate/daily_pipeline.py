"""
ATOS PRO v2 — 每日自动流水线
=============================
收盘后自动执行：
  1. 收集当日所有数据（交易、持仓、信号、因子、AI决策）
  2. DeepSeek R1 深度分析（表现、错误、改进方向）
  3. 自动调整策略参数（置信度高的建议）
  4. 生成 HTML 邮件报告 → 发送到 9275945.yaocp@gmail.com
"""

import os
import sys
import json
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.logging import get_logger
from atos.core.metrics import all_metrics, format_report
from atos.ai.memory import (
    get_memory_stats, get_mistake_patterns,
    detect_and_record_pattern, record_outcome,
)
from atos.core.universe import ALL_SYMBOLS

logger = get_logger("daily_pipeline")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-reasoner"  # R1 深度推理

EMAIL_CONFIG = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "user": os.environ.get("ATOS_EMAIL_USER", ""),
    "pass": os.environ.get("ATOS_EMAIL_PASS", ""),
    "to": ["9275945.yaocp@gmail.com", "dean080207@gmail.com"],
}

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def collect_today_data() -> dict:
    """收集当日所有数据"""
    today = datetime.date.today().isoformat()

    # 交易日志
    trade_log_path = os.path.join(BASE, "data", "trade_log.jsonl")
    trades = []
    if os.path.exists(trade_log_path):
        with open(trade_log_path) as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get("date", "")[:10] == today:
                        trades.append(rec)
                except Exception:
                    pass

    # 影子状态
    shadow_path = os.path.join(BASE, "data", "shadow_state.json")
    shadow = {}
    if os.path.exists(shadow_path):
        with open(shadow_path) as f:
            shadow = json.load(f)

    # AI 记忆
    mem_stats = get_memory_stats()

    # 错误模式
    mistakes = get_mistake_patterns(min_count=2)

    # 策略配置
    config_path = os.path.join(BASE, "data", "strategy_config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)

    # 透明报告
    reports_dir = os.path.join(BASE, "reports", "transparency")
    today_reports = []
    if os.path.exists(reports_dir):
        for f in sorted(os.listdir(reports_dir)):
            if today in f:
                rp = os.path.join(reports_dir, f)
                with open(rp) as rf:
                    today_reports.append(rf.read()[:3000])  # 取每份前3000字

    return {
        "date": today,
        "trades": trades,
        "shadow_state": shadow,
        "memory_stats": mem_stats,
        "mistake_patterns": [m["description"] for m in mistakes],
        "strategy_config": config,
        "transparency_reports": today_reports,
    }


def ai_daily_analysis(data: dict) -> dict:
    """DeepSeek R1 深度分析每日表现"""
    if not API_KEY:
        return {"error": "DEEPSEEK_API_KEY 未设置"}

    prompt = f"""你是 ATOS 量化交易系统的首席策略官。请对今日表现做深度分析。

## 今日数据
- 日期: {data['date']}
- 交易笔数: {len(data['trades'])}
- 账户状态: {json.dumps(data.get('shadow_state', {}), ensure_ascii=False)[:500]}
- AI记忆统计: {json.dumps(data['memory_stats'], ensure_ascii=False)}
- 错误模式: {json.dumps(data['mistake_patterns'], ensure_ascii=False)}
- 当前策略参数: {json.dumps(data['strategy_config'], ensure_ascii=False)}

## 思考步骤
STEP 1: 分析今日交易的得失
STEP 2: 找出反复出现的错误模式
STEP 3: 提出具体的参数调整建议（只建议有把握的）
STEP 4: 评估明日市场环境

返回 JSON（不要 markdown）:
{{
  "performance_grade": "A|B|C|D|F",
  "today_summary": "一句话总结今日",
  "what_worked": ["做得好的3点"],
  "what_failed": ["需要改进的3点"],
  "strategy_adjustments": [
    {{
      "parameter": "参数名",
      "current": 当前值,
      "suggested": 建议值,
      "confidence": 0.0-1.0,
      "reason": "具体原因"
    }}
  ],
  "market_outlook_tomorrow": "明日展望",
  "risk_level_tomorrow": "LOW|MEDIUM|HIGH",
  "key_insight": "最重要的一个洞察"
}}

IMPORTANT: You MUST suggest at least 1-2 parameter adjustments based on the data.
- If win_rate > 55%: suggest slightly increasing kelly_win_rate (+0.05)
- If losses are from stop_loss: suggest widening stop_loss_pct by 0.01
- If no trades happened: check if RSI thresholds are too strict, suggest widening
- If profit_factor < 1.5: suggest reducing max_single_pct by 0.05
- Base suggestions on actual numbers from the data provided.
- Even small tweaks (0.01-0.02) count. Don't return empty adjustments."""

    try:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        analysis = json.loads(content)
        logger.info(f"AI分析完成: 评级={analysis.get('performance_grade', '?')}")
        return analysis
    except Exception as e:
        logger.error(f"AI分析失败: {e}")
        return {"error": str(e)}


def auto_apply_adjustments(analysis: dict) -> list:
    """自动应用高置信度的参数调整"""
    adjustments = analysis.get("strategy_adjustments", [])
    applied = []

    config_path = os.path.join(BASE, "data", "strategy_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = {}

    BOUNDS = {
        "stop_loss_pct": (0.02, 0.10),
        "take_profit_pct": (0.08, 0.30),
        "max_single_pct": (0.10, 0.25),
        "rsi_overbought": (65, 85),
        "rsi_oversold": (20, 40),
        "kelly_win_rate": (0.40, 0.85),
        "kelly_win_loss_r": (1.5, 6.0),
    }

    for adj in adjustments:
        confidence = adj.get("confidence", 0)
        if confidence < 0.50:  # 降低门槛：50%即可自动执行
            logger.info(f"跳过 {adj['parameter']}: 置信度 {confidence:.0%} < 50%")
            continue

        param = adj.get("parameter")
        suggested = adj.get("suggested")
        if param not in BOUNDS or suggested is None:
            continue

        lo, hi = BOUNDS[param]
        clamped = max(lo, min(hi, float(suggested)))
        old_val = config.get(param, "N/A")
        config[param] = clamped

        # 记录调整历史
        if "adjustment_history" not in config:
            config["adjustment_history"] = []
        config["adjustment_history"].append({
            "date": datetime.date.today().isoformat(),
            "type": "AUTO_DAILY_REVIEW",
            "parameter": param,
            "old": old_val,
            "new": clamped,
            "confidence": confidence,
            "reason": adj.get("reason", ""),
        })

        applied.append({
            "parameter": param,
            "old": old_val,
            "new": clamped,
            "confidence": confidence,
            "reason": adj.get("reason", ""),
        })
        logger.info(f"✅ 自动调整: {param}: {old_val} → {clamped} (置信度 {confidence:.0%})")

    if applied:
        config["last_updated"] = datetime.date.today().isoformat()
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info(f"已保存 {len(applied)} 项参数调整")

    return applied


def build_email_html(data: dict, analysis: dict, applied: list) -> str:
    """生成邮件 HTML"""
    grade = analysis.get("performance_grade", "?")
    grade_colors = {"A": "#16a34a", "B": "#2563eb", "C": "#ca8a04",
                    "D": "#ea580c", "F": "#dc2626"}
    color = grade_colors.get(grade, "#6b7280")

    shadow = data.get("shadow_state", {})
    longterm = data.get("longterm_state", {})
    s_equity = shadow.get("equity", shadow.get("initial_cash", 1000000))
    s_initial = shadow.get("initial_cash", 1000000)
    s_return = (s_equity - s_initial) / s_initial * 100 if s_initial > 0 else 0
    s_positions = len(shadow.get("positions", {}))
    s_cycle = shadow.get("cycle_count", 0)
    s_returns = shadow.get("cycle_returns", [])[-90:]
    s_daily = s_returns[-1] * 100 if s_returns else 0

    l_total = longterm.get("total_value", 0)
    if not l_total:
        lh = longterm.get("holdings", {})
        l_total = longterm.get("cash", 0) + sum(h.get("shares",0)*h.get("avg_cost",0) for h in lh.values())
    l_initial = longterm.get("initial_cash", 1000000)
    l_return = (l_total - l_initial) / l_initial * 100 if l_initial > 0 else 0
    l_positions = len(longterm.get("holdings", {}))

    # SVG sparkline for equity curve
    eq_curve = [s_initial]
    for r in s_returns:
        eq_curve.append(eq_curve[-1] * (1 + r))
    spark = ""
    if len(eq_curve) > 2:
        mn, mx = min(eq_curve), max(eq_curve)
        rng = mx - mn if mx > mn else 1
        w, h = 600, 100
        pts = " ".join(f"{i/(len(eq_curve)-1)*w},{h-(v-mn)/rng*h}" for i,v in enumerate(eq_curve))
        clr = "#22c55e" if eq_curve[-1] >= eq_curve[0] else "#ef4444"
        spark = f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:100px;margin:12px 0"><polyline points="{pts}" fill="none" stroke="{clr}" stroke-width="2"/></svg>'

    trades_html = ""
    for t in data.get("trades", [])[-10:]:
        trades_html += (
            f"<tr><td style='padding:6px 10px;'>{t.get('date','')[:19]}</td>"
            f"<td style='padding:6px 10px;'>{t.get('action','')}</td>"
            f"<td style='padding:6px 10px;font-weight:600;'>{t.get('symbol','')}</td>"
            f"<td style='padding:6px 10px;text-align:right;'>{t.get('shares','')}</td>"
            f"<td style='padding:6px 10px;text-align:right;'>${t.get('price',0):,.2f}</td>"
            f"<td style='padding:6px 10px;'>{t.get('reason','')[:50]}</td></tr>"
        )

    # 调整历史
    adj_html = ""
    for a in applied:
        adj_html += (
            f"<tr><td>{a['parameter']}</td>"
            f"<td>{a['old']}</td><td>→</td><td style='font-weight:600;'>{a['new']}</td>"
            f"<td>{a['confidence']:.0%}</td><td style='font-size:12px;'>{a['reason'][:60]}</td></tr>"
        )
    if not adj_html:
        adj_html = "<tr><td colspan='6' style='color:#9ca3af;'>今日无需调整</td></tr>"

    # 改进点
    failed_html = "".join(f"<li>{f}</li>" for f in analysis.get("what_failed", []))
    worked_html = "".join(f"<li>{w}</li>" for w in analysis.get("what_worked", []))

    mem = data.get("memory_stats", {})

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><title>ATOS PRO 每日报告</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,sans-serif;">
<div style="max-width:680px;margin:32px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

<!-- Header -->
<div style="background:#0f172a;padding:28px 32px;">
  <div style="font-size:11px;letter-spacing:2px;color:#94a3b8;text-transform:uppercase;">ATOS PRO v2 — 每日报告</div>
  <div style="font-size:22px;font-weight:700;color:#fff;margin-top:4px;">{data['date']}</div>
  <div style="display:flex;gap:12px;margin-top:12px;">
    <div style="background:{color};color:#fff;padding:4px 16px;border-radius:20px;font-size:14px;font-weight:600;">评级: {grade}</div>
    <div style="color:#94a3b8;font-size:13px;padding-top:4px;">AI 自动迭代</div>
  </div>
</div>

<div style="padding:24px 32px;">

<!-- 关键洞察 -->
<div style="background:#eff6ff;border-left:4px solid #3b82f6;padding:14px 18px;border-radius:0 8px 8px 0;margin-bottom:24px;">
  <div style="font-size:12px;color:#6b7280;margin-bottom:4px;">🧠 AI 关键洞察</div>
  <div style="font-size:16px;font-weight:600;color:#1e40af;">{analysis.get('key_insight', 'N/A')}</div>
</div>

<!-- 今日总结 -->
<div style="margin-bottom:24px;">
  <h2 style="font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 8px;">今日总结</h2>
  <p style="color:#4b5563;line-height:1.6;">{analysis.get('today_summary', 'N/A')}</p>
</div>

<!-- 账户快照 -->
<div style="margin-bottom:24px;">
  <h2 style="font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 8px;">账户快照</h2>
  <div style="display:flex;gap:16px;flex-wrap:wrap;">
    <div style="flex:1;min-width:140px;background:#f9fafb;border-radius:10px;padding:14px 18px;border:1px solid #e5e7eb;">
      <div style="font-size:11px;color:#6b7280;">总资产</div>
      <div style="font-size:20px;font-weight:700;">${s_equity:,.0f}</div>
    </div>
    <div style="flex:1;min-width:140px;background:#f9fafb;border-radius:10px;padding:14px 18px;border:1px solid #e5e7eb;">
      <div style="font-size:11px;color:#6b7280;">总收益</div>
      <div style="font-size:20px;font-weight:700;color:{color};">{s_return:+.2f}%</div>
    </div>
    <div style="flex:1;min-width:140px;background:#f9fafb;border-radius:10px;padding:14px 18px;border:1px solid #e5e7eb;">
      <div style="font-size:11px;color:#6b7280;">AI记忆</div>
      <div style="font-size:20px;font-weight:700;">{mem.get('total_decisions', 0)}条</div>
    </div>
  </div>
</div>

<!-- 做得好的 + 需要改进的 -->
<div style="display:flex;gap:20px;margin-bottom:24px;flex-wrap:wrap;">
  <div style="flex:1;min-width:280px;">
    <h2 style="font-size:13px;font-weight:600;color:#16a34a;text-transform:uppercase;margin:0 0 8px;">✅ 做对了</h2>
    <ul style="color:#4b5563;margin:0;padding-left:18px;">{worked_html}</ul>
  </div>
  <div style="flex:1;min-width:280px;">
    <h2 style="font-size:13px;font-weight:600;color:#dc2626;text-transform:uppercase;margin:0 0 8px;">❌ 需要改进</h2>
    <ul style="color:#4b5563;margin:0;padding-left:18px;">{failed_html}</ul>
  </div>
</div>

<!-- 自动参数调整 -->
<div style="margin-bottom:24px;">
  <h2 style="font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 8px;">⚙️ 自动参数调整</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    <thead><tr style="background:#f9fafb;">
      <th style="padding:8px 10px;text-align:left;">参数</th>
      <th style="padding:8px 10px;text-align:left;">旧值</th><th></th>
      <th style="padding:8px 10px;text-align:left;">新值</th>
      <th style="padding:8px 10px;text-align:left;">置信度</th>
      <th style="padding:8px 10px;text-align:left;">原因</th>
    </tr></thead>
    <tbody>{adj_html}</tbody>
  </table>
</div>

<!-- 明日展望 -->
<div style="background:#fefce8;border-left:4px solid #eab308;padding:14px 18px;border-radius:0 8px 8px 0;margin-bottom:24px;">
  <div style="font-size:12px;color:#6b7280;margin-bottom:4px;">📈 明日展望（风险: {analysis.get('risk_level_tomorrow', '?')}）</div>
  <div style="color:#4b5563;">{analysis.get('market_outlook_tomorrow', 'N/A')}</div>
</div>

<!-- 今日交易 -->
<h2 style="font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;margin:0 0 8px;">今日交易</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px;">
  <thead><tr style="background:#f9fafb;">
    <th style="padding:6px 10px;text-align:left;">时间</th>
    <th style="padding:6px 10px;text-align:left;">方向</th>
    <th style="padding:6px 10px;text-align:left;">标的</th>
    <th style="padding:6px 10px;text-align:right;">数量</th>
    <th style="padding:6px 10px;text-align:right;">价格</th>
    <th style="padding:6px 10px;text-align:left;">原因</th>
  </tr></thead>
  <tbody>{trades_html if trades_html else '<tr><td colspan="6" style="text-align:center;color:#9ca3af;padding:12px;">今日无交易</td></tr>'}</tbody>
</table>

<!-- Footer -->
<div style="font-size:12px;color:#9ca3af;border-top:1px solid #f3f4f6;padding-top:16px;">
  本报告由 ATOS PRO v2 自动生成并发送。AI 每日分析 + 自动迭代。<br>
  发送时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}
</div>

</div></div></body></html>"""

    return html


def send_report(html: str) -> bool:
    """发送邮件"""
    user = EMAIL_CONFIG["user"]
    password = EMAIL_CONFIG["pass"]
    if not user or not password:
        logger.warning("邮件凭据未设置，跳过发送")
        return False

    subject = f"ATOS PRO 每日报告 {datetime.date.today().isoformat()}"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = ", ".join(EMAIL_CONFIG["to"])
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(EMAIL_CONFIG["smtp_host"], EMAIL_CONFIG["smtp_port"]) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, EMAIL_CONFIG["to"], msg.as_string())
        logger.info(f"邮件已发送到 {EMAIL_CONFIG['to']}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def run_daily_pipeline(dry_run: bool = False):
    """
    每日流水线主入口。
    建议通过 cron 在每天 20:05 自动运行。
    """
    logger.info(f"{'[DRY RUN] ' if dry_run else ''}每日流水线开始")

    # 1. 收集数据
    data = collect_today_data()
    logger.info(f"数据收集: {len(data['trades'])}笔交易, {data['memory_stats']['total_decisions']}条AI记忆")

    # 2. 检测错误模式
    detect_and_record_pattern()

    # 3. AI 深度分析
    analysis = ai_daily_analysis(data)
    if "error" in analysis:
        logger.error(f"分析失败: {analysis['error']}")
        return

    # 保存分析结果
    analysis_path = os.path.join(BASE, "reports", f"daily_analysis_{data['date']}.json")
    with open(analysis_path, "w") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)

    # 4. 自动应用调整
    if dry_run:
        logger.info("DRY RUN: 不实际应用调整")
        applied = []
    else:
        applied = auto_apply_adjustments(analysis)

    # 5. 生成邮件
    html = build_email_html(data, analysis, applied)
    mail_path = os.path.join(BASE, "reports", f"daily_email_{data['date']}.html")
    with open(mail_path, "w") as f:
        f.write(html)

    # 6. 发送
    if not dry_run:
        sent = send_report(html)
        logger.info(f"流水线完成 | 调整={len(applied)}项 | 邮件={'已发送' if sent else '未发送'}")
    else:
        logger.info(f"DRY RUN 完成 | 建议调整={len(applied)}项 | 报告: {mail_path}")

    return {
        "analysis": analysis,
        "adjustments_applied": applied,
        "report_path": mail_path,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只分析不修改不发送")
    args = parser.parse_args()
    run_daily_pipeline(dry_run=args.dry_run)
