"""
ATOS AutoPilot — 错误知识库
==========================
SQLite 数据库存储历史错误和修复方案。
新错误出现时先查知识库，命中则直接应用已知修复。
未命中则调用 AI 分析。

表结构:
  errors: 错误记录
  fixes: 修复记录
  patterns: 错误模式匹配规则
"""

import sqlite3, json, os, datetime, hashlib
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "autopilot_kb.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化知识库表结构"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_hash TEXT UNIQUE NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            module TEXT,
            stack_trace TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            occurrence_count INTEGER DEFAULT 1,
            severity TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'open',
            auto_fixed BOOLEAN DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_hash TEXT NOT NULL,
            fix_type TEXT NOT NULL,  -- 'code_patch', 'config_change', 'restart', 'cache_clear', 'manual'
            fix_description TEXT NOT NULL,
            fix_code TEXT,
            risk_level TEXT DEFAULT 'safe',  -- 'safe', 'risky', 'dangerous'
            success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_applied TIMESTAMP,
            FOREIGN KEY (error_hash) REFERENCES errors(error_hash)
        );

        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_name TEXT UNIQUE NOT NULL,
            regex_pattern TEXT NOT NULL,
            error_type TEXT,
            severity TEXT DEFAULT 'medium',
            action TEXT DEFAULT 'ai_analyze',
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_errors_hash ON errors(error_hash);
        CREATE INDEX IF NOT EXISTS idx_errors_status ON errors(status);
        CREATE INDEX IF NOT EXISTS idx_fixes_hash ON fixes(error_hash);
    """)
    conn.commit()

    # 预置常见错误模式
    _seed_patterns(conn)
    conn.close()


def _seed_patterns(conn):
    """预置 ATOS 已知错误模式"""
    patterns = [
        ("yfinance_disk_error", r"disk I/O error|SQLite.*yfinance", "data_source",
         "high", "clear_cache", "yfinance SQLite 缓存损坏，清除缓存后重试"),
        ("yfinance_timeout", r"timeout|Timed out|Too Many Requests|429", "data_source",
         "medium", "sleep_retry", "yfinance API 限流/超时，等待后重试"),
        ("futu_disconnect", r"FutuOpenD.*disconnect|connection.*refused.*11111", "broker",
         "high", "restart_futu", "FutuOpenD 连接断开，重启 Futu 进程"),
        ("deepseek_quota", r"402|Payment Required|insufficient_quota", "ai_api",
         "high", "reduce_freq", "DeepSeek API 余额不足，降频使用"),
        ("nan_propagation", r"nan|NaN.*price|float.*nan", "data_quality",
         "high", "clear_nan", "NaN 传播到交易决策，需要数据清洗"),
        ("kelly_crash", r"kelly_crash|kelly.*zero|division by zero", "risk",
         "high", "reset_kelly", "Kelly 公式崩溃（除零或极端值），重置统计"),
        ("memory_leak", r"MemoryError|memory.*exceeded|ENOSPC", "system",
         "critical", "restart_system", "内存耗尽或磁盘满，紧急重启"),
        ("import_error", r"ImportError|ModuleNotFoundError|No module named", "code",
         "critical", "install_deps", "缺少依赖包，pip install"),
        ("signal_empty", r"空信号.*跳过|0/\\d+.*数据不可用", "signal",
         "medium", "skip_cycle", "信号数据全空，跳过本周期"),
        ("circuit_breaker", r"熔断|circuit_open|日亏损.*熔断", "risk",
         "high", "stop_trading", "风控熔断触发，暂停交易"),
    ]

    for name, regex, etype, severity, action, notes in patterns:
        conn.execute("""
            INSERT OR IGNORE INTO patterns (pattern_name, regex_pattern, error_type, severity, action, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, regex, etype, severity, action, notes))
    conn.commit()


def hash_error(error_type: str, error_message: str, module: str = "") -> str:
    """生成错误哈希，用于去重和匹配"""
    content = f"{error_type}:{error_message[:200]}:{module}"
    return hashlib.md5(content.encode()).hexdigest()[:12]


def lookup_error(error_type: str, error_message: str, module: str = "") -> Optional[dict]:
    """在知识库中查找已知错误和修复方案"""
    conn = _get_conn()
    error_hash = hash_error(error_type, error_message, module)

    row = conn.execute(
        "SELECT * FROM errors WHERE error_hash = ?", (error_hash,)
    ).fetchone()

    if row:
        # 更新计数
        conn.execute("""
            UPDATE errors SET last_seen = CURRENT_TIMESTAMP,
            occurrence_count = occurrence_count + 1
            WHERE error_hash = ?
        """, (error_hash,))

        # 获取关联修复
        fixes = conn.execute(
            "SELECT * FROM fixes WHERE error_hash = ? ORDER BY success_count DESC",
            (error_hash,)
        ).fetchall()

        conn.commit()
        conn.close()

        return {
            "known": True,
            "error": dict(row),
            "fixes": [dict(f) for f in fixes],
        }

    conn.close()
    return {"known": False}


def record_error(error_type: str, error_message: str, module: str = "",
                 stack_trace: str = "", severity: str = "medium") -> str:
    """记录新错误到知识库"""
    conn = _get_conn()
    error_hash = hash_error(error_type, error_message, module)

    existing = conn.execute(
        "SELECT id, occurrence_count FROM errors WHERE error_hash = ?",
        (error_hash,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE errors SET last_seen = CURRENT_TIMESTAMP,
            occurrence_count = occurrence_count + 1,
            stack_trace = COALESCE(?, stack_trace)
            WHERE error_hash = ?
        """, (stack_trace[:5000] if stack_trace else None, error_hash))
    else:
        conn.execute("""
            INSERT INTO errors (error_hash, error_type, error_message, module, stack_trace, severity)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (error_hash, error_type, error_message[:500], module,
              stack_trace[:5000] if stack_trace else "", severity))

    conn.commit()
    conn.close()
    return error_hash


def record_fix(error_hash: str, fix_type: str, description: str,
               fix_code: str = "", risk_level: str = "safe") -> int:
    """记录修复方案"""
    conn = _get_conn()
    cursor = conn.execute("""
        INSERT INTO fixes (error_hash, fix_type, fix_description, fix_code, risk_level)
        VALUES (?, ?, ?, ?, ?)
    """, (error_hash, fix_type, description, fix_code, risk_level))
    fix_id = cursor.lastrowid

    # 如果修复类型是 'auto_applied'，标记错误为已修复
    if fix_type == "auto_applied":
        conn.execute("""
            UPDATE errors SET status = 'fixed', auto_fixed = 1
            WHERE error_hash = ?
        """, (error_hash,))

    conn.commit()
    conn.close()
    return fix_id


def update_fix_result(fix_id: int, success: bool):
    """更新修复结果（成功/失败）"""
    conn = _get_conn()
    if success:
        conn.execute("""
            UPDATE fixes SET success_count = success_count + 1,
            last_applied = CURRENT_TIMESTAMP WHERE id = ?
        """, (fix_id,))
    else:
        conn.execute("""
            UPDATE fixes SET fail_count = fail_count + 1 WHERE id = ?
        """, (fix_id,))
    conn.commit()
    conn.close()


def match_pattern(log_line: str) -> Optional[dict]:
    """用正则匹配已知错误模式"""
    conn = _get_conn()
    patterns = conn.execute("SELECT * FROM patterns").fetchall()
    conn.close()

    for p in patterns:
        import re
        if re.search(p["regex_pattern"], log_line, re.IGNORECASE):
            return dict(p)
    return None


def get_stats() -> dict:
    """获取知识库统计"""
    conn = _get_conn()
    total_errors = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
    open_errors = conn.execute("SELECT COUNT(*) FROM errors WHERE status='open'").fetchone()[0]
    auto_fixed = conn.execute("SELECT COUNT(*) FROM errors WHERE auto_fixed=1").fetchone()[0]
    total_fixes = conn.execute("SELECT COUNT(*) FROM fixes").fetchone()[0]

    recent = conn.execute("""
        SELECT error_type, error_message, last_seen, occurrence_count, severity
        FROM errors WHERE status='open'
        ORDER BY last_seen DESC LIMIT 10
    """).fetchall()

    conn.close()
    return {
        "total_errors": total_errors,
        "open_errors": open_errors,
        "auto_fixed": auto_fixed,
        "total_fixes": total_fixes,
        "auto_fix_rate": round(auto_fixed / total_errors, 2) if total_errors > 0 else 0,
        "recent_open": [dict(r) for r in recent],
    }


# 初始化
init_db()
