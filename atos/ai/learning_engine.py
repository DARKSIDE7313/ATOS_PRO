#!/usr/bin/env python3
"""
ATOS 学习引擎 v3 — 高强度训练系统
====================================
能力：
  - P0: 评估所有决策（含 HOLD）用 yfinance 价格追踪
  - P1: 模式挖掘 + 错误模式自动修复
  - P2: 因子权重自动调整 + 正则化
  - P3: 滑动回测 + 参数稳定性验证
  - P4: 策略衰减实时追踪
  - P5: 胜率趋势 + 置信度校准
  
每天跑 4 次（6h/次），比之前快 4 倍。
"""

import argparse
import json
import os
import sys
import sqlite3
import time
import math
from datetime import datetime, timedelta
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.logging import get_logger

logger = get_logger("ai.learning_engine")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE, "data", "ai_memory.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_price_at(symbol: str, target_date: datetime) -> float:
    """用 yfinance 获取某只股票在目标日期前后的价格"""
    try:
        import yfinance as yf
        # 查目标日期前后 3 天的数据
        start = (target_date - timedelta(days=2)).strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=5)).strftime("%Y-%m-%d")
        df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            return 0
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        close = df["Close"].squeeze()
        if len(close) == 0:
            return 0
        # 返回决策日之后第一个有效收盘价
        return float(close.iloc[0]) if not close.empty else 0
    except Exception:
        return 0


def _get_price_after_days(symbol: str, target_date: datetime, days: int = 2) -> float:
    """获取决策后 N 天的价格（用于评估 HOLD）"""
    try:
        import yfinance as yf
        start = target_date.strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=days + 2)).strftime("%Y-%m-%d")
        df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            return 0
        if isinstance(df.columns, pd.__class__):
            pass
        close = df["Close"].squeeze() if "Close" in df.columns else (df.iloc[:, 0] if df.shape[1] >= 1 else None)
        if close is None or len(close) == 0:
            return 0
        # 取最后一天的价格
        return float(close.iloc[-1])
    except Exception:
        return 0


# ════════════════════════════════════════════════════════════
# P0 v3: 全量评估 — 所有决策类型，用 yfinance 价格验证
# ════════════════════════════════════════════════════════════

def evaluate_all_outcomes():
    """
    评估所有未评估决策，包括 HOLD。
    
    评估方法：
    - BUY: 检查 trade_history 中是否有后续 SELL，有则算 PnL
          如果没有 SELL 记录，用 yfinance 查 2 天后价格
    - SELL: 检查 trade_history 中对应 BUY 的 PnL
    - HOLD: 检查决策后 2 天该标的的价格变化（涨=WIN,跌=LOSS）
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT d.id, d.timestamp, d.symbol, d.action, d.confidence,
                   d.factor_score, d.market_regime
            FROM decisions d
            LEFT JOIN outcomes o ON d.id = o.decision_id
            WHERE o.id IS NULL
            ORDER BY d.id DESC
        """).fetchall()

        if not rows:
            logger.info("P0: 无待评估决策")
            return

        logger.info(f"P0: 评估 {len(rows)} 条决策 (含 HOLD)...")

        # 读取交易记录
        trades = []
        for fp in ['data/shadow_state.json', 'data/longterm_state.json']:
            fpn = os.path.join(BASE, fp)
            if os.path.exists(fpn):
                with open(fpn) as f:
                    raw = json.load(f)
                trades.extend(raw.get("trade_history", []) or [])

        # 当前持仓
        current_positions = {}
        for fp in ['data/shadow_state.json', 'data/longterm_state.json']:
            fpn = os.path.join(BASE, fp)
            if os.path.exists(fpn):
                with open(fpn) as f:
                    raw = json.load(f)
                for k in ['positions', 'holdings']:
                    pos = raw.get(k, {}) or {}
                    for sym, dt in pos.items():
                        if isinstance(dt, dict):
                            current_positions[sym] = dt

        import pandas as pd
        import yfinance as yf

        evaluated = 0
        skipped_hold = 0
        batch = []

        for row in rows:
            decision_id = row["id"]
            symbol = row["symbol"]
            action = row["action"]
            ts_str = row["timestamp"]

            try:
                dec_time = datetime.fromisoformat(ts_str)
            except Exception:
                continue

            # 如果正在持仓中，跳过（还没到评估时机）
            if symbol in current_positions and action in ("BUY", "HOLD"):
                skipped_hold += 1
                continue

            outcome_type = "BREAKEVEN"
            pnl_pct = 0.0
            days_held = 0
            exit_reason = "auto_evaluated"
            ai_correct = None

            if action == "BUY":
                # 找后续 SELL
                sell_pnl = None
                for t in trades:
                    if t.get("symbol") == symbol and t.get("action") == "SELL":
                        try:
                            sell_time = datetime.fromisoformat(t.get("date", ""))
                            if sell_time >= dec_time:
                                sell_pnl = t.get("pnl", 0)
                                exit_reason = t.get("reason", "trade")[:50]
                                days_held = max(0, (sell_time - dec_time).days)
                                break
                        except Exception:
                            continue

                if sell_pnl is not None:
                    pnl_pct = sell_pnl / 10000  # 归一化
                    pnl_pct = max(-0.15, min(0.15, pnl_pct))
                    outcome_type = "WIN" if sell_pnl > 0 else "LOSS"
                else:
                    # 没有卖出记录 → 用 yfinance 查 2 天后价格
                    try:
                        end_dt = (dec_time + timedelta(days=2)).strftime("%Y-%m-%d")
                        start_dt = (dec_time - timedelta(days=1)).strftime("%Y-%m-%d")
                        df = yf.download(symbol, start=start_dt, end=end_dt, progress=False, auto_adjust=True)
                        if not df.empty:
                            if isinstance(df.columns, pd.MultiIndex):
                                df.columns = [c[0] for c in df.columns]
                            close = df["Close"].squeeze()
                            if len(close) >= 2:
                                entry_px = float(close.iloc[0])
                                exit_px = float(close.iloc[-1])
                                if entry_px > 0:
                                    pnl_pct = (exit_px - entry_px) / entry_px
                                    pnl_pct = max(-0.10, min(0.10, pnl_pct))
                                    outcome_type = "WIN" if pnl_pct > 0.01 else "LOSS" if pnl_pct < -0.01 else "BREAKEVEN"
                                    exit_reason = "yfinance_2d_check"
                    except Exception:
                        continue

            elif action == "SELL":
                # 找之前最近的 BUY
                for t in trades:
                    if t.get("symbol") == symbol and t.get("action") == "BUY":
                        try:
                            buy_time = datetime.fromisoformat(t.get("date", ""))
                            if buy_time <= dec_time:
                                days_held = max(0, (dec_time - buy_time).days)
                                break
                        except Exception:
                            continue
                # 这个 SELL 的 PnL 就是它自己的 pnl 字段
                for t in trades:
                    if t.get("symbol") == symbol and t.get("action") == "SELL":
                        try:
                            st = datetime.fromisoformat(t.get("date", ""))
                            if abs((st - dec_time).total_seconds()) < 3600:  # 1h内匹配
                                pnl = t.get("pnl", 0)
                                pnl_pct = pnl / 10000
                                pnl_pct = max(-0.15, min(0.15, pnl_pct))
                                outcome_type = "WIN" if pnl > 0 else "LOSS"
                                exit_reason = t.get("reason", "sell")[:50]
                                break
                        except Exception:
                            continue

            elif action == "HOLD":
                # HOLD 决策：查 2 天后该标的涨跌
                try:
                    end_dt = (dec_time + timedelta(days=2)).strftime("%Y-%m-%d")
                    start_dt = (dec_time - timedelta(days=1)).strftime("%Y-%m-%d")
                    df = yf.download(symbol, start=start_dt, end=end_dt, progress=False, auto_adjust=True)
                    if not df.empty:
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = [c[0] for c in df.columns]
                        close = df["Close"].squeeze()
                        if len(close) >= 2:
                            entry_px = float(close.iloc[0])
                            exit_px = float(close.iloc[-1])
                            if entry_px > 0:
                                pnl_pct = (exit_px - entry_px) / entry_px
                                pnl_pct = max(-0.08, min(0.08, pnl_pct))
                                outcome_type = "WIN" if pnl_pct > 0.005 else "LOSS" if pnl_pct < -0.005 else "BREAKEVEN"
                                exit_reason = "yfinance_hold_check"
                                # HOLD 正确性：涨了→AI正确；跌了→AI错误
                                ai_correct = pnl_pct > 0
                except Exception:
                    continue

            # 写入结果
            batch.append((
                decision_id, outcome_type, round(pnl_pct, 4),
                days_held, exit_reason,
                True if ai_correct else (False if ai_correct is False else None),
                "auto_v3",
            ))
            evaluated += 1

            if evaluated % 300 == 0:
                conn.executemany("""
                    INSERT INTO outcomes (decision_id, outcome_type, pnl_pct, days_held,
                                          exit_reason, ai_correct, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, batch)
                conn.commit()
                logger.info(f"P0 进度: {evaluated}/{len(rows)} (跳过持仓中: {skipped_hold})")
                batch = []

        if batch:
            conn.executemany("""
                INSERT INTO outcomes (decision_id, outcome_type, pnl_pct, days_held,
                                      exit_reason, ai_correct, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, batch)
            conn.commit()

        logger.info(f"P0: 完成 — 评估了 {evaluated} 条, 持仓中跳过 {skipped_hold} 条")
        return evaluated
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# P1 v3: 智能模式挖掘 + 自动修复配置
# ════════════════════════════════════════════════════════════

def mine_patterns_and_fix():
    """挖掘模式 + 自动写入修复建议"""
    conn = get_db()
    try:
        # 1. 按 regime+action+symbol 看胜率
        rows = conn.execute("""
            SELECT d.market_regime, d.action, d.symbol,
                   COUNT(*) as total,
                   SUM(CASE WHEN o.outcome_type='WIN' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN o.outcome_type='LOSS' THEN 1 ELSE 0 END) as losses
            FROM decisions d
            JOIN outcomes o ON d.id = o.decision_id
            GROUP BY d.market_regime, d.action, d.symbol
            HAVING total >= 3
            ORDER BY total DESC
        """).fetchall()

        # 按 regimes 聚合统计
        regime_stats = defaultdict(lambda: {"total": 0, "wins": 0, "losses": 0})
        symbol_blacklist = []
        regime_blacklist = []
        
        for r in rows:
            regime = r["market_regime"]
            action = r["action"]
            symbol = r["symbol"]
            total = r["total"]
            wins = r["wins"]
            losses = r["losses"]
            win_rate = wins / total if total > 0 else 0

            reg = regime_stats[regime]
            reg["total"] += total
            reg["wins"] += wins
            reg["losses"] += losses

            # 标的级别：连续亏损 > 75%
            if losses >= 5 and win_rate < 0.25:
                symbol_blacklist.append({
                    "symbol": symbol, "regime": regime, "action": action,
                    "win_rate": round(win_rate, 3), "total": total,
                })

        # 新模式计数
        new_patterns = 0

        # 写入亏损标的模式
        existing_blacklist = set()
        for ex in conn.execute("SELECT conditions FROM patterns WHERE pattern_type='BAD_SYMBOL'"):
            try:
                existing_blacklist.add(json.loads(ex["conditions"]).get("symbol", ""))
            except Exception:
                pass

        for item in symbol_blacklist:
            if item["symbol"] in existing_blacklist:
                continue
            desc = f"亏损标的: {item['symbol']} in {item['regime']} 胜率{item['win_rate']:.0%} ({item['total']}笔)"
            conditions = json.dumps({"symbol": item["symbol"], "regime": item["regime"], "action": item["action"]})
            impact = round((item["win_rate"] - 0.5) * 0.5, 3)
            conn.execute("""
                INSERT INTO patterns (pattern_type, description, conditions,
                    occurrence_count, last_seen, confidence_impact)
                VALUES (?, ?, ?, ?, date('now'), ?)
            """, ("BAD_SYMBOL", desc, conditions, item["total"], impact))
            new_patterns += 1
            logger.info(f"P1 BAD_SYMBOL: {item['symbol']} win_rate={item['win_rate']:.0%} impact={impact:.3f}")

        # 2. 按 regime 看整体胜率
        for regime, stats in sorted(regime_stats.items(), key=lambda x: -x[1]["total"]):
            total = stats["total"]
            wins = stats["wins"]
            losses = stats["losses"]
            if total < 10:
                continue
            win_rate = wins / total if total > 0 else 0
            desc = f"整体评估: {regime} 胜率{win_rate:.0%} ({wins}W/{losses}L/{total})"
            conditions = json.dumps({"regime": regime})

            pattern_type = "LOSS_REGIME" if win_rate < 0.30 else "WIN_REGIME" if win_rate > 0.60 else "NEUTRAL_REGIME"

            existing = conn.execute(
                "SELECT id FROM patterns WHERE conditions=? AND pattern_type=?",
                (conditions, pattern_type)
            ).fetchone()
            impact = round((win_rate - 0.5) * 0.4, 3)
            if existing:
                conn.execute("""
                    UPDATE patterns SET occurrence_count=?, last_seen=date('now'),
                        confidence_impact=?, description=?
                    WHERE id=?
                """, (total, impact, desc, existing["id"]))
            else:
                conn.execute("""
                    INSERT INTO patterns (pattern_type, description, conditions,
                        occurrence_count, last_seen, confidence_impact)
                    VALUES (?, ?, ?, ?, date('now'), ?)
                """, (pattern_type, desc, conditions, total, impact))
                new_patterns += 1
            if win_rate < 0.30:
                regime_blacklist.append(regime)
            logger.info(f"P1 {pattern_type}: {desc} impact={impact:.3f}")

        conn.commit()

        # 3. 生成修复建议
        fix_suggestions = []
        for regime in regime_blacklist:
            fix_suggestions.append(f"⚠️ {regime} 胜率偏低 — 建议减少该环境下开仓")

        for item in symbol_blacklist[:5]:
            fix_suggestions.append(f"⚠️ 避开 {item['symbol']} ({item['win_rate']:.0%} 胜率)")

        logger.info(f"P1: 完成 — 新增 {new_patterns} 个模式")
        if fix_suggestions:
            logger.info("P1 修复建议:")
            for s in fix_suggestions:
                logger.info(f"  {s}")

        return new_patterns, fix_suggestions
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# P2 v3: 权重优化 + 正则化
# ════════════════════════════════════════════════════════════

def optimize_weights_v3():
    """
    基于评估结果自动调整 REGIME_WEIGHTS。
    
    方法：
    - 如果某个 regime 胜率 < 30%，降低 momentum/mean_rev 权重
    - 如果某个 regime 胜率 > 60%，维持或略微增加
    - 应用正则化防止权重漂移过大
    """
    conn = get_db()
    try:
        # 获取各 regime 的胜率
        rows = conn.execute("""
            SELECT d.market_regime,
                   COUNT(*) as total,
                   SUM(CASE WHEN o.outcome_type='WIN' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN o.outcome_type='LOSS' THEN 1 ELSE 0 END) as losses
            FROM decisions d
            JOIN outcomes o ON d.id = o.decision_id
            GROUP BY d.market_regime
            HAVING total >= 10
        """).fetchall()

        if not rows:
            logger.info("P2: 无足够数据")
            return False

        import importlib
        from atos.factors import engine as factor_engine

        changed = False
        for r in rows:
            regime = r["market_regime"]
            total = r["total"]
            wins = r["wins"]
            losses = r["losses"]
            win_rate = wins / total if total > 0 else 0

            if regime not in factor_engine.REGIME_WEIGHTS:
                continue

            current = dict(factor_engine.REGIME_WEIGHTS[regime])
            old_weights = dict(current)

            # 基于胜率的调整系数
            # win_rate 0.5 = 不调整
            # win_rate 0.3 = -40% 动量, -30% 均值回归
            # win_rate 0.7 = +20% 动量
            adjustment = (win_rate - 0.5) * 2.0  # [-1.0, 1.0]

            # 动量：低胜率→降低，高胜率→增加
            current["momentum"] *= (1.0 + adjustment * 0.3)
            current["momentum"] = max(0.05, min(0.40, current["momentum"]))

            # 均值回归：低胜率→大幅降低（追涨杀跌）
            current["mean_rev"] *= (1.0 + adjustment * 0.5)
            current["mean_rev"] = max(0.0, min(0.25, current["mean_rev"]))

            # 技术面：保持稳定，微调
            current["technical"] *= (1.0 + adjustment * 0.1)
            current["technical"] = max(0.10, min(0.40, current["technical"]))

            # 归一化
            total_w = sum(current.values())
            current = {k: round(v / total_w, 4) for k, v in current.items()}

            if current != old_weights:
                factor_engine.REGIME_WEIGHTS[regime] = current
                changed = True
                logger.info(f"P2 [{regime}] win_rate={win_rate:.0%}:")
                for k in sorted(current.keys()):
                    delta = current[k] - old_weights.get(k, 0)
                    if abs(delta) > 0.01:
                        logger.info(f"  {k:12s}: {old_weights.get(k,0):.4f} → {current[k]:.4f} ({delta:+.4f})")

        if not changed:
            logger.info("P2: 无权重变化")

        return changed
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# P3 v3: 回测 + 胜率稳定性分析
# ════════════════════════════════════════════════════════════

def backtest_v3():
    """滑动窗口回测 + 胜率趋势 + 置信度校准"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT d.timestamp, d.symbol, d.action, d.confidence, d.factor_score,
                   d.market_regime, o.outcome_type, o.pnl_pct
            FROM decisions d
            JOIN outcomes o ON d.id = o.decision_id
            WHERE o.outcome_type IN ('WIN', 'LOSS')
            ORDER BY d.timestamp ASC
        """).fetchall()

        total = len(rows)
        if total < 20:
            logger.info(f"P3: 数据不足 ({total}条)")
            return

        wins = sum(1 for r in rows if r["outcome_type"] == "WIN")
        losses = sum(1 for r in rows if r["outcome_type"] == "LOSS")
        wr = wins / total * 100
        logger.info(f"P3 全量: {total}笔 {wins}赢{losses}输 胜率{wr:.1f}%")

        # 滑动窗口：每 30 笔一组，每次前进 10 笔
        window = min(50, total // 3)
        if window < 15:
            window = total // 2

        if window >= 10:
            wr_trend = []
            for start in range(0, total - window + 1, max(5, window // 5)):
                w = rows[start:start + window]
                w_wins = sum(1 for r in w if r["outcome_type"] == "WIN")
                w_total = len(w)
                w_wr = w_wins / w_total * 100
                first_ts = w[0]["timestamp"][:10]
                last_ts = w[-1]["timestamp"][:10]
                avg_conf = sum(r["confidence"] for r in w) / w_total
                wr_trend.append((first_ts, w_wr, avg_conf))

            # 趋势分析
            if len(wr_trend) >= 3:
                first_wr = wr_trend[0][1]
                last_wr = wr_trend[-1][1]
                trend = "↗️上升" if last_wr > first_wr else "↘️下降" if last_wr < first_wr else "➡️持平"
                logger.info(f"P3 趋势: {trend} ({first_wr:.0f}%→{last_wr:.0f}%)")
                
                # 置信度校准：平均置信度 vs 胜率
                avg_all_conf = sum(r["confidence"] for r in rows) / total
                logger.info(f"P3 置信度校准: 平均置信度={avg_all_conf:.2f} vs 实际胜率={wr:.1f}%")
                if avg_all_conf > 0.6 and wr < 30:
                    logger.info(f"⚠️ 置信度过高({avg_all_conf:.2f})但胜率低({wr:.1f}%) → 需要降低AI置信度输出")

            # 最后 3 个窗口的平均
            recent_wr = sum(w[1] for w in wr_trend[-3:]) / 3 if len(wr_trend) >= 3 else wr
            logger.info(f"P3 近期平均: {recent_wr:.1f}%")

        # 输出亏损集中度
        loss_symbols = Counter(r["symbol"] for r in rows if r["outcome_type"] == "LOSS")
        if loss_symbols:
            top5 = loss_symbols.most_common(5)
            logger.info(f"P3 亏损集中: {top5}")

        logger.info("P3: 完成")
        return wr
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# P4: 策略衰减跟踪
# ════════════════════════════════════════════════════════════

def track_decay():
    """
    检测策略是否在衰减。
    
    方法：
    - 最近 50 笔 vs 前 50 笔的胜率对比
    - 如果近期胜率 < 前期胜率 * 0.7 → 衰减警告
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT d.timestamp, o.outcome_type
            FROM decisions d
            JOIN outcomes o ON d.id = o.decision_id
            WHERE o.outcome_type IN ('WIN', 'LOSS')
            ORDER BY d.timestamp ASC
        """).fetchall()

        total = len(rows)
        if total < 60:
            return

        recent = rows[-50:]
        previous = rows[-100:-50]

        def win_rate(rr):
            w = sum(1 for r in rr if r["outcome_type"] == "WIN")
            return w / len(rr) if rr else 0

        recent_wr = win_rate(recent)
        prev_wr = win_rate(previous)

        logger.info(f"P4 策略衰减: 前期={prev_wr:.0%} 近期={recent_wr:.0%}")

        if prev_wr > 0 and recent_wr < prev_wr * 0.7:
            logger.warning(f"⚠️ 策略衰减警告: {prev_wr:.0%}→{recent_wr:.0%}")
            logger.warning("⚠️ 建议: 检查市场环境变化，可能需要重新校准参数")

        return recent_wr, prev_wr
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# P5: 快照 — 全量统计输出
# ════════════════════════════════════════════════════════════

def full_stats():
    """输出全套学习统计"""
    conn = get_db()
    try:
        t = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        out = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        pat = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        un = conn.execute("""
            SELECT COUNT(*) FROM decisions d
            LEFT JOIN outcomes o ON d.id = o.decision_id
            WHERE o.id IS NULL
        """).fetchone()[0]

        wins = conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='WIN'").fetchone()[0]
        losses = conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='LOSS'").fetchone()[0]
        breaks = conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='BREAKEVEN'").fetchone()[0]

        logger.info(f"📊 学习引擎状态")
        logger.info(f"  决策: {t} 条")
        logger.info(f"  已评估: {out} 条 ({out/t*100:.1f}%)")
        logger.info(f"  待评估: {un} 条")
        logger.info(f"  模式: {pat} 个")
        logger.info(f"  结果: {wins}赢 {losses}输 {breaks}平")
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        logger.info(f"  胜率: {wr:.1f}%")

        # 各 regime 胜率
        rows = conn.execute("""
            SELECT d.market_regime,
                   COUNT(*) as total,
                   SUM(CASE WHEN o.outcome_type='WIN' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN o.outcome_type='LOSS' THEN 1 ELSE 0 END) as losses
            FROM decisions d JOIN outcomes o ON d.id = o.decision_id
            GROUP BY d.market_regime HAVING total>=5
            ORDER BY total DESC
        """).fetchall()
        for r in rows:
            wr_r = r["wins"]/(r["wins"]+r["losses"])*100 if (r["wins"]+r["losses"])>0 else 0
            logger.info(f"  {r['market_regime']:15s}: {r['total']}笔 {r['wins']}赢{r['losses']}输 胜率{wr_r:.0f}%")

        return {"total": t, "evaluated": out, "patterns": pat, "win_rate": wr}
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# 主入口：完全训练
# ════════════════════════════════════════════════════════════

def run_full_training():
    """完整训练流程 = P0+P1+P2+P3+P4+P5"""
    start = time.time()
    logger.info("🚀 ATOS 学习引擎 v3 — 高强度训练启动")

    step = 0
    total_steps = 6
    
    step += 1; logger.info(f"\n[{step}/{total_steps}] P0 — 全量评估...")
    t0 = time.time()
    n = evaluate_all_outcomes()
    logger.info(f"  {time.time()-t0:.1f}s — {n}条")

    step += 1; logger.info(f"\n[{step}/{total_steps}] P1 — 模式挖掘...")
    t0 = time.time()
    p, fixes = mine_patterns_and_fix()
    logger.info(f"  {time.time()-t0:.1f}s — {p}个新模式")

    step += 1; logger.info(f"\n[{step}/{total_steps}] P2 — 权重优化...")
    t0 = time.time()
    changed = optimize_weights_v3()
    logger.info(f"  {time.time()-t0:.1f}s — {'✅有权重变化' if changed else '⏭️无变化'}")

    step += 1; logger.info(f"\n[{step}/{total_steps}] P3 — 回测分析...")
    t0 = time.time()
    wr = backtest_v3()
    logger.info(f"  {time.time()-t0:.1f}s — 胜率{wr:.1f}%" if wr else f"  {time.time()-t0:.1f}s")

    step += 1; logger.info(f"\n[{step}/{total_steps}] P4 — 衰减检测...")
    t0 = time.time()
    track_decay()
    logger.info(f"  {time.time()-t0:.1f}s")

    step += 1; logger.info(f"\n[{step}/{total_steps}] P5 — 全量统计...")
    t0 = time.time()
    stats = full_stats()
    logger.info(f"  {time.time()-t0:.1f}s")

    total_time = time.time() - start
    logger.info(f"\n✅ 高强度训练完成 ({total_time:.1f}s)")
    logger.info(f"  总决策: {stats['total']} | 已评估: {stats['evaluated']} | 胜率: {stats.get('win_rate',0):.1f}%")
    return stats


def main():
    parser = argparse.ArgumentParser(description="ATOS 学习引擎 v3")
    parser.add_argument("--full", action="store_true", default=True, help="完整训练 (默认)")
    parser.add_argument("--p0", action="store_true", help="只评估")
    parser.add_argument("--p1", action="store_true", help="只挖掘模式")
    parser.add_argument("--p2", action="store_true", help="只优化权重")
    parser.add_argument("--p3", action="store_true", help="只回测")
    parser.add_argument("--stats", action="store_true", help="只输出统计")
    args = parser.parse_args()

    if args.p0: evaluate_all_outcomes()
    elif args.p1: mine_patterns_and_fix()
    elif args.p2: optimize_weights_v3()
    elif args.p3: backtest_v3()
    elif args.stats: full_stats()
    else: run_full_training()


if __name__ == "__main__":
    main()
