"""
ATOS PRO v2 — AI 决策记忆库
============================
SQLite 存储每笔 AI 决策的上下文、决策、结果。
支持：相似历史查询、错误模式识别、置信度追踪。

表结构：
  decisions — 每次 AI 决策的完整记录
  outcomes  — 决策的后续结果（用于学习）
  patterns  — 识别出的错误模式
"""

import sqlite3
import json
import os
import datetime
import threading
from threading import Lock
from atos.core.logging import get_logger

logger = get_logger("ai.memory")

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "ai_memory.db"
)

_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_regime TEXT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,          -- BUY / SELL / HOLD
                confidence REAL,                -- AI 自评置信度 0-1
                factor_score REAL,              -- 当时的因子评分
                reasons TEXT,                   -- JSON: 各理论视角的理由
                debate_summary TEXT,            -- 辩论总结
                snapshot_json TEXT,             -- 完整快照（压缩）
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                outcome_type TEXT NOT NULL,     -- WIN / LOSS / BREAKEVEN
                pnl_pct REAL,
                days_held INTEGER,
                exit_reason TEXT,
                ai_correct BOOLEAN,             -- AI 判断是否正确
                notes TEXT,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (decision_id) REFERENCES decisions(id)
            );

            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL,     -- RECURRING_MISTAKE / SUCCESS_PATTERN
                description TEXT,
                conditions TEXT,                -- JSON: 触发条件
                occurrence_count INTEGER DEFAULT 1,
                last_seen TEXT,
                confidence_impact REAL DEFAULT 0.0,  -- 对 AI 置信度的影响
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
            CREATE INDEX IF NOT EXISTS idx_decisions_regime ON decisions(market_regime);
            CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_outcomes_decision ON outcomes(decision_id);
        """)
        conn.commit()
    finally:
        conn.close()


def record_decision(symbol: str, action: str, confidence: float,
                    factor_score: float, reasons: dict,
                    debate_summary: str, market_regime: str,
                    snapshot: dict = None) -> int:
    """记录一次 AI 决策，返回 decision_id。快照自动裁剪以节省存储。"""
    init_db()
    with _db_lock:
        conn = _get_db()
        try:
            # Fix: 裁剪快照 — 只保留关键字段，节省 90% 存储
            trimmed_snapshot = None
            if snapshot:
                trimmed_snapshot = {
                    "positions": snapshot.get("positions", [])[:5],
                    "market_regime": snapshot.get("market_regime", {}),
                    "factor_rankings": [
                        {"symbol": r.get("symbol", "?"), "score": r.get("score", 0)}
                        for r in snapshot.get("factor_rankings", [])[:5]
                    ],
                    "vix": snapshot.get("vix"),
                }
            cursor = conn.execute(
                """INSERT INTO decisions (timestamp, market_regime, symbol, action,
                   confidence, factor_score, reasons, debate_summary, snapshot_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.datetime.now().isoformat(),
                    market_regime,
                    symbol,
                    action,
                    round(confidence, 4),
                    round(factor_score, 4),
                    json.dumps(reasons, ensure_ascii=False),
                    debate_summary,
                    json.dumps(trimmed_snapshot, ensure_ascii=False) if trimmed_snapshot else None,
                )
            )
            conn.commit()
            decision_id = cursor.lastrowid
        finally:
            conn.close()
    logger.info(f"决策已记录 #{decision_id}: {action} {symbol} conf={confidence:.2f}")
    return decision_id


def record_outcome(decision_id: int, outcome_type: str, pnl_pct: float = 0,
                   days_held: int = 0, exit_reason: str = "",
                   ai_correct: bool = None, notes: str = ""):
    """记录决策的结果 + 自动学习"""
    init_db()
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                """INSERT INTO outcomes (decision_id, outcome_type, pnl_pct, days_held,
                   exit_reason, ai_correct, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (decision_id, outcome_type, round(pnl_pct, 6), days_held,
                 exit_reason, ai_correct, notes)
            )
            conn.commit()
        finally:
            conn.close()
    logger.info(f"结果已记录: #{decision_id} → {outcome_type} pnl={pnl_pct:.2%}")

    # 自动学习：更新该标的+市场环境的置信度调整
    try:
        auto_learn_from_outcome(decision_id, outcome_type, pnl_pct)
    except Exception as e:
        logger.error(f"自动学习失败: {e}")

    # 自动检测错误模式
    try:
        detect_and_record_pattern()
    except Exception as e:
        logger.error(f"模式检测失败: {e}")


def auto_learn_from_outcome(decision_id: int, outcome_type: str, pnl_pct: float = 0):
    """根据交易结果自动更新置信度调整值。

    当交易结果是 LOSS 时，降低该 symbol+regime 的置信度。
    当交易结果是 WIN 时，略微提高置信度。
    """
    init_db()
    with _db_lock:
        conn = _get_db()
        try:
            # 获取该决策的 symbol 和 regime
            row = conn.execute(
                "SELECT symbol, market_regime, confidence FROM decisions WHERE id = ?",
                (decision_id,)
            ).fetchone()

            if not row:
                return

            symbol = row["symbol"]
            regime = row["market_regime"]
            ai_confidence = row["confidence"]

            # 计算该 symbol+regime 的历史胜率
            stats = conn.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN o.outcome_type='WIN' THEN 1 ELSE 0 END) as wins,
                          SUM(CASE WHEN o.outcome_type='LOSS' THEN 1 ELSE 0 END) as losses
                   FROM decisions d
                   JOIN outcomes o ON d.id = o.decision_id
                   WHERE d.symbol = ? AND d.market_regime = ?""",
                (symbol, regime)
            ).fetchone()

            total = stats["total"] or 0
            wins = stats["wins"] or 0
            losses = stats["losses"] or 0

            if total < 2:
                logger.debug(f"Auto-learn {symbol}/{regime}: only {total} outcomes, skipping")
                return

            win_rate = wins / total if total > 0 else 0.5

            # 更新 patterns 表中的 confidence_impact
            # 查找该 symbol+regime 的 pattern
            existing = conn.execute(
                "SELECT id FROM patterns WHERE conditions LIKE ?",
                (f"%{symbol}%{regime}%",)
            ).fetchone()

            if existing:
                # 根据胜率更新置信度影响
                # win_rate 低于 0.4 → 负向调整（降低置信度）
                # win_rate 高于 0.6 → 正向调整（提高置信度）
                if win_rate < 0.4:
                    impact = round((win_rate - 0.5) * 0.5, 3)  # negative
                elif win_rate > 0.6:
                    impact = round((win_rate - 0.5) * 0.3, 3)  # slightly positive
                else:
                    impact = 0.0

                conn.execute(
                    "UPDATE patterns SET confidence_impact = ?, last_seen = date('now'), "
                    "occurrence_count = occurrence_count + 1 WHERE id = ?",
                    (impact, existing["id"])
                )
                logger.info(f"Auto-learn {symbol}/{regime}: win_rate={win_rate:.2%}, "
                             f"impact={impact:+.3f} (pattern #{existing['id']})")
            else:
                # 记录为新 pattern
                if win_rate < 0.4:
                    impact = round((win_rate - 0.5) * 0.5, 3)
                    conn.execute(
                        """INSERT INTO patterns (pattern_type, description, conditions,
                           occurrence_count, last_seen, confidence_impact)
                           VALUES (?, ?, ?, ?, date('now'), ?)""",
                        (
                            "LEARNED_ADJUSTMENT",
                            f"Auto-learn: {symbol} in {regime} has {wins}W/{losses}L ({win_rate:.0%})",
                            f'{{"symbol": "{symbol}", "regime": "{regime}"}}',
                            total,
                            impact,
                        )
                    )
                    logger.info(f"Auto-learn {symbol}/{regime}: new pattern, impact={impact:+.3f}")

            conn.commit()
        finally:
            conn.close()


def get_similar_history(market_regime: str, symbol: str = None,
                         limit: int = 10) -> list[dict]:
    """查询历史中相似市场环境下的决策"""
    init_db()
    conn = _get_db()
    try:
        query = """SELECT d.*, o.outcome_type, o.pnl_pct, o.ai_correct
                   FROM decisions d
                   LEFT JOIN outcomes o ON d.id = o.decision_id
                   WHERE d.market_regime = ?"""
        params = [market_regime]
        if symbol:
            query += " AND d.symbol = ?"
            params.append(symbol)
        query += " ORDER BY d.timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_mistake_patterns(min_count: int = 2) -> list[dict]:
    """查询重复出现的错误模式"""
    init_db()
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT p.* FROM patterns p
               WHERE p.occurrence_count >= ?
               ORDER BY p.occurrence_count DESC""",
            (min_count,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def detect_and_record_pattern():
    """自动检测重复错误模式，写入 patterns 表"""
    init_db()
    with _db_lock:
        conn = _get_db()
        try:
            # 找到所有亏损的决策
            rows = conn.execute(
                """SELECT d.symbol, d.market_regime, d.action, d.reasons,
                          o.pnl_pct, o.exit_reason
                   FROM decisions d
                   JOIN outcomes o ON d.id = o.decision_id
                   WHERE o.outcome_type = 'LOSS'
                     AND o.recorded_at > datetime('now', '-7 days')
                   ORDER BY d.timestamp DESC"""
            ).fetchall()

            # 按 symbol+regime 分组
            from collections import Counter
            pattern_counter = Counter()
            for r in rows:
                key = f"{r['action']}_{r['symbol']}_{r['market_regime']}"
                pattern_counter[key] += 1

            # 记录出现 >=2 次的
            for key, count in pattern_counter.items():
                if count >= 2:
                    parts = key.split("_", 2)
                    if len(parts) < 3:
                        continue
                    action, symbol, regime = parts
                    # 检查是否已存在
                    existing = conn.execute(
                        "SELECT id FROM patterns WHERE description LIKE ?",
                        (f"%{symbol}%{regime}%",)
                    ).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE patterns SET occurrence_count = ?, last_seen = date('now') WHERE id = ?",
                            (count, existing["id"])
                        )
                    else:
                        conn.execute(
                            """INSERT INTO patterns (pattern_type, description, conditions,
                               occurrence_count, last_seen)
                               VALUES (?, ?, ?, ?, date('now'))""",
                            (
                                "RECURRING_MISTAKE",
                                f"重复亏损: {action} {symbol} 在 {regime} 市场",
                                json.dumps({"symbol": symbol, "regime": regime, "action": action}),
                                count,
                            )
                        )
            conn.commit()
        finally:
            conn.close()
    logger.info("错误模式检测完成")


def get_ai_confidence_adjustment(symbol: str, regime: str) -> float:
    """
    根据历史记忆调整 AI 置信度（Bug #14: 同时从 outcomes 和 patterns 学习）。

    如果历史上在此 symbol+regime 下多次失败，降低置信度。
    返回 -0.3 ~ +0.15 的调整值。
    """
    init_db()
    with _db_lock:
        conn = _get_db()
        try:
            # 1. 基于 outcomes 的统计调整（原始逻辑）
            row = conn.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN o.outcome_type='WIN' THEN 1 ELSE 0 END) as wins,
                          SUM(CASE WHEN o.outcome_type='LOSS' THEN 1 ELSE 0 END) as losses
                   FROM decisions d
                   JOIN outcomes o ON d.id = o.decision_id
                   WHERE d.symbol = ? AND d.market_regime = ?""",
                (symbol, regime)
            ).fetchone()

            total = row["total"] or 0
            if total >= 2:
                win_rate = row["wins"] / total if total > 0 else 0.5
                outcome_adjustment = (win_rate - 0.5) * 0.3
            else:
                outcome_adjustment = 0.0

            # 2. 基于 patterns 的学习调整（auto_learn 写入的 confidence_impact）
            pattern_row = conn.execute(
                """SELECT confidence_impact FROM patterns
                   WHERE conditions LIKE ?
                   ORDER BY last_seen DESC LIMIT 1""",
                (f"%{symbol}%{regime}%",)
            ).fetchone()
            pattern_adjustment = pattern_row["confidence_impact"] if pattern_row else 0.0
        finally:
            conn.close()

    # 综合调整
    adjustment = outcome_adjustment + pattern_adjustment
    # 限制范围
    adjustment = max(-0.3, min(0.15, adjustment))
    return round(adjustment, 3)


def get_memory_stats() -> dict:
    """获取记忆系统统计"""
    init_db()
    conn = _get_db()
    try:
        total_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        total_outcomes = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        win_count = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE outcome_type='WIN'"
        ).fetchone()[0]
        loss_count = conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE outcome_type='LOSS'"
        ).fetchone()[0]
        patterns = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        return {
            "total_decisions": total_decisions,
            "total_outcomes": total_outcomes,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_count / total_outcomes if total_outcomes > 0 else 0.0,
            "patterns_detected": patterns,
        }
    finally:
        conn.close()


def write_trade_journal(merged_result: dict, snapshot: dict, regime: str):
    """After each AI cycle, write a brief summary of decisions made and why, to the log.

    生成结构化的交易日志，记录：
    - 做了什么决策或没有做（HOLD）
    - 为什么
    - 市场状态
    - 学习到的教训
    """
    actions = merged_result.get("short_term_actions", [])
    position_reviews = merged_result.get("position_reviews", [])
    debate_results = merged_result.get("debate_results", [])
    risk_notes = merged_result.get("risk_notes", "N/A")
    market_read = merged_result.get("market_read", "N/A")
    lessons = merged_result.get("lessons_applied", [])

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构建日志条目
    if actions:
        action_lines = []
        for a in actions:
            sym = a.get("symbol", "?")
            act = a.get("action", "?")
            conf = a.get("confidence", 0)
            reason = a.get("reason", "")[:60]
            action_lines.append(f"    {act} {sym} (conf={conf:.2f}): {reason}")
        actions_text = "\n".join(action_lines)
    else:
        actions_text = "    无交易动作 (HOLD all)"

    pos_review_lines = []
    for pr in position_reviews[:5]:  # limit to top 5
        pos_review_lines.append(
            f"    {pr.get('position', '?')}: {pr.get('action', 'HOLD')} "
            f"(conf={pr.get('confidence', 0):.2f}) - {pr.get('reason', '')[:50]}"
        )
    pos_text = "\n".join(pos_review_lines) if pos_review_lines else "    No positions held"

    debate_lines = []
    for dr in debate_results[:3]:
        votes = dr.get("votes", {})
        debate_lines.append(
            f"    {dr.get('symbol', '?')}: {dr.get('final_action', '?')} "
            f"conf={dr.get('final_confidence', 0):.2f} votes={votes}"
        )
    debate_text = "\n".join(debate_lines) if debate_lines else "    No debates this cycle"

    lessons_text = ""
    if lessons:
        lessons_text = "\n  Lessons applied:\n" + "\n".join(
            f"    - {l}" for l in lessons[:3]
        )

    journal_entry = (
        f"\n{'='*70}\n"
        f"  AI Trade Journal — {timestamp} | Regime: {regime}\n"
        f"{'='*70}\n"
        f"  Market: {market_read[:80]}\n"
        f"  Risk:   {risk_notes[:80]}\n"
        f"  Actions ({len(actions)}):\n{actions_text}"
        f"\n  Position Reviews ({len(position_reviews)}):\n{pos_text}"
        f"\n  Debates ({len(debate_results)}):\n{debate_text}"
        f"{lessons_text}"
        f"\n{'='*70}"
    )

    logger.debug(journal_entry)


# 启动时自动建表
init_db()
