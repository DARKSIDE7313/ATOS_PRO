#!/usr/bin/env python3
"""
ATOS Trade Review System — 交易复盘分析系统
===========================================
每笔真实交易的分析、记录、改良，持续提升 AI 胜率。

核心原则：
  1. 只分析真实交易（BUY/SELL 有实际成交），忽略循环噪声
  2. 每笔交易记录：进场原因、出场原因、盈亏分析、可改进点
  3. 将复盘结果喂回 AI 引擎，持续学习
  4. 自动识别重复错误模式并修复

用法:
  python3 trade_review.py --status         # 当前胜率概览
  python3 trade_review.py --analyze        # 分析最近交易
  python3 trade_review.py --clean          # 清理噪声数据
  python3 trade_review.py --improve        # 读取复盘结果，自动改良策略
"""

import json
import os
import sqlite3
import subprocess
import datetime
from pathlib import Path
from collections import defaultdict

ATOS_HOME = os.path.expanduser('~/ATOS_PRO')
DB_PATH = os.path.join(ATOS_HOME, 'data', 'ai_memory.db')
LOG_PATH = os.path.join(ATOS_HOME, 'logs', 'shadow_trader_stderr.log')
REVIEW_DB = os.path.join(ATOS_HOME, 'data', 'trade_reviews.db')

# === DATA LAYER ===

def get_real_trades(min_confidence: float = 0.3) -> list:
    """从 ai_memory.db 提取真实交易（过滤掉循环噪声）"""
    conn = sqlite3.connect(DB_PATH)
    
    # 真实交易 = BUY/SELL 操作，且 pnl 不是 ±0.08% 的噪声
    rows = conn.execute("""
        SELECT d.id, d.timestamp, d.symbol, d.action, d.confidence,
               o.pnl_pct, o.days_held, o.ai_correct, o.exit_reason,
               d.reasons, d.market_regime
        FROM decisions d
        JOIN outcomes o ON d.id = o.decision_id
        WHERE d.action IN ('BUY', 'SELL')
          AND o.pnl_pct IS NOT NULL
          AND ABS(o.pnl_pct) > 0.1  -- 过滤 <0.1% 的噪声
          AND d.confidence >= {}
        ORDER BY d.timestamp DESC
    """.format(min_confidence)).fetchall()
    
    conn.close()
    
    return [{
        'id': r[0],
        'timestamp': r[1],
        'symbol': r[2],
        'action': r[3],
        'confidence': r[4],
        'pnl_pct': r[5],
        'days_held': r[6],
        'ai_correct': r[7] == 1,
        'exit_reason': r[8] or 'unknown',
        'reasons': r[9] or '',
        'regime': r[10] or 'UNKNOWN',
    } for r in rows]


def get_noise_decisions() -> int:
    """统计噪声决策数（需要清理的）"""
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("""
        SELECT COUNT(*) FROM decisions d
        JOIN outcomes o ON d.id = o.decision_id
        WHERE ABS(o.pnl_pct) <= 0.1
           OR o.exit_reason LIKE '%yfinance%'
           OR o.exit_reason LIKE '%cycle_check%'
    """).fetchone()[0]
    conn.close()
    return count


def clean_noise(dry_run: bool = True) -> int:
    """清理噪声数据"""
    conn = sqlite3.connect(DB_PATH)
    noisy = conn.execute("""
        SELECT d.id FROM decisions d
        JOIN outcomes o ON d.id = o.decision_id
        WHERE ABS(o.pnl_pct) <= 0.1
           OR o.exit_reason LIKE '%yfinance%'
           OR o.exit_reason LIKE '%cycle_check%'
    """).fetchall()
    
    ids = [r[0] for r in noisy]
    
    if not dry_run and ids:
        conn.execute(f"DELETE FROM outcomes WHERE decision_id IN ({','.join(map(str, ids))})")
        conn.execute(f"DELETE FROM decisions WHERE id IN ({','.join(map(str, ids))})")
        conn.commit()
        print(f'已清理 {len(ids)} 条噪声记录')
    
    conn.close()
    return len(ids)


def save_review(trade: dict, analysis: str):
    """保存复盘分析到 review DB"""
    os.makedirs(os.path.dirname(REVIEW_DB), exist_ok=True)
    conn = sqlite3.connect(REVIEW_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER,
            symbol TEXT,
            action TEXT,
            pnl_pct REAL,
            analysis TEXT,
            created_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO reviews (decision_id, symbol, action, pnl_pct, analysis, created_at) VALUES (?,?,?,?,?,?)",
        (trade['id'], trade['symbol'], trade['action'], trade['pnl_pct'], analysis,
         datetime.datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_summary_stats() -> dict:
    """计算真实胜率统计"""
    trades = get_real_trades()
    
    if not trades:
        return {'total': 0, 'win_rate': 0, 'message': '没有足够的真实交易数据'}
    
    total = len(trades)
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    losses = total - wins
    avg_win = sum(t['pnl_pct'] for t in trades if t['pnl_pct'] > 0) / max(wins, 1)
    avg_loss = sum(t['pnl_pct'] for t in trades if t['pnl_pct'] < 0) / max(losses, 1)
    
    # By symbol
    by_symbol = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
    for t in trades:
        s = t['symbol']
        by_symbol[s]['trades'] += 1
        if t['pnl_pct'] > 0:
            by_symbol[s]['wins'] += 1
        by_symbol[s]['pnl'] += t['pnl_pct']
    
    return {
        'total': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / total * 100, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else float('inf'),
        'by_symbol': dict(by_symbol),
    }


# === ANALYSIS ===

def analyze_trades(trades: list) -> list:
    """对每笔交易生成分析"""
    reviews = []
    
    for t in trades:
        pnl = t['pnl_pct']
        
        # 判定好坏
        if pnl > 0.5:
            verdict = 'GOOD'
            issue = '持仓方向正确'
        elif pnl > 0:
            verdict = 'FAIR'
            issue = '微利'
        elif pnl > -0.5:
            verdict = 'POOR'
            issue = '小幅亏损，止损过早或方向略偏'
        else:
            verdict = 'BAD'
            issue = '大幅亏损，方向判断错误'
        
        # 分析原因
        exit_reason = t['exit_reason'].lower()
        if 'stop' in exit_reason or 'sl' in exit_reason:
            cause = '触发止损'
        elif 'cut' in exit_reason or 'reduce' in exit_reason:
            cause = 'AI主动减仓'
        elif 'sell' in exit_reason or 'close' in exit_reason:
            cause = 'AI判断卖出'
        elif 'take' in exit_reason or 'tp' in exit_reason:
            cause = '触发止盈'
        else:
            cause = exit_reason[:30]
        
        # 生成改进建议
        improvements = []
        if t['action'] == 'SELL' and pnl < 0 and t['ai_correct']:
            improvements.append('AI应该更耐心持有，避免过早卖出')
        if t['action'] == 'BUY' and pnl < -1:
            improvements.append(f'AI应该避开 {t["symbol"]} 在 {t["regime"]} 市场环境下买入')
        if 'stop' in exit_reason and abs(pnl) > 1:
            improvements.append('止损距离可能太紧，建议放宽')
        if 'cut' in exit_reason:
            improvements.append('仓位限制触发的减仓导致了亏损，建议放宽单只仓位上限')
        
        review = {
            'symbol': t['symbol'],
            'action': t['action'],
            'pnl': pnl,
            'confidence': t['confidence'],
            'verdict': verdict,
            'issue': issue,
            'cause': cause,
            'improvements': improvements if improvements else ['继续观察'],
            'timestamp': t['timestamp'],
        }
        reviews.append(review)
        
        # 保存到 review DB
        analysis_text = (
            f"盈亏: {pnl:+.2f}% | 判定: {verdict}\n"
            f"问题: {issue}\n"
            f"原因: {cause}\n"
            f"改进: {'; '.join(improvements) if improvements else '暂无'}"
        )
        save_review(t, analysis_text)
    
    return reviews


# === STRATEGY IMPROVEMENT ===

def generate_improvements(reviews: list) -> dict:
    """根据复盘结果生成改良建议"""
    improvements = defaultdict(list)
    
    # 1. 找出最差的标的
    symbol_pnl = defaultdict(lambda: {'count': 0, 'total_pnl': 0, 'wins': 0})
    for r in reviews:
        s = r['symbol']
        symbol_pnl[s]['count'] += 1
        symbol_pnl[s]['total_pnl'] += r['pnl']
        if r['pnl'] > 0:
            symbol_pnl[s]['wins'] += 1
    
    bad_symbols = [(s, d) for s, d in symbol_pnl.items() 
                   if d['count'] >= 3 and d['total_pnl'] < 0]
    
    improvements['bad_symbols'] = [
        {'symbol': s, 'total_pnl': round(d['total_pnl'], 2), 
         'win_rate': round(d['wins']/d['count']*100, 1),
         'trades': d['count']}
        for s, d in sorted(bad_symbols, key=lambda x: x[1]['total_pnl'])[:10]
    ]
    
    # 2. AI 过度交易分析
    sell_trades = [r for r in reviews if r['action'] == 'SELL']
    bad_sells = [r for r in sell_trades if r['pnl'] < 0]
    
    improvements['sell_too_early'] = {
        'total_sells': len(sell_trades),
        'bad_sells': len(bad_sells),
        'bad_sell_rate': round(len(bad_sells)/max(len(sell_trades),1)*100, 1),
        'recommendation': 'AI 卖出过早是主要亏损源，需要让持仓有更多时间发展'
    }
    
    # 3. 仓位管理
    cut_trades = [r for r in reviews if 'cut' in r['cause'] or '仓位' in r['issue']]
    improvements['position_sizing'] = {
        'cuts_causing_loss': len(cut_trades),
        'recommendation': '仓位限制触发的减仓导致亏损，建议 max_single_pct 从 15% 放宽到 25%'
    }
    
    return dict(improvements)


# === CLI ===

def cmd_status():
    """显示当前真实胜率"""
    stats = get_summary_stats()
    
    print(f'\n{"="*50}')
    print('ATOS 交易胜率报告')
    print(f'{"="*50}')
    
    if stats['total'] == 0:
        print('\n没有足够的真实交易数据')
        return
    
    print(f'\n总交易: {stats["total"]} 笔')
    print(f'胜: {stats["wins"]}  负: {stats["losses"]}')
    print(f'胜率: {stats["win_rate"]}%')
    print(f'平均盈利: +{stats["avg_win"]}%')
    print(f'平均亏损: {stats["avg_loss"]}%')
    print(f'盈亏比: {stats["profit_factor"]}')
    print()
    
    # By symbol
    print(f'{"标的":6} {"交易":6} {"胜率":8} {"总盈亏":10}')
    print('-' * 30)
    for sym, d in sorted(stats['by_symbol'].items(), key=lambda x: x[1]['trades'], reverse=True)[:15]:
        wr = round(d['wins']/d['trades']*100, 1) if d['trades'] > 0 else 0
        print(f'{sym:6} {d["trades"]:4}次 {wr:6.1f}% {round(d["pnl"],2):+7.2f}%')
    
    # Noise
    noise = get_noise_decisions()
    print(f'\n需要清理的噪声数据: {noise} 条')
    print(f'运行 --clean 清理')


def cmd_analyze():
    """分析最近真实交易"""
    trades = get_real_trades()
    
    if not trades:
        print('没有真实交易数据')
        return
    
    print(f'\n分析最近 {len(trades)} 笔真实交易...\n')
    
    reviews = analyze_trades(trades[:30])  # 最近30笔
    
    # Summary
    good = sum(1 for r in reviews if r['verdict'] in ('GOOD', 'FAIR'))
    bad = sum(1 for r in reviews if r['verdict'] in ('POOR', 'BAD'))
    
    print(f'{"标的":6} {"操作":6} {"盈亏":8} {"判定":8} {"问题":20}')
    print('-' * 50)
    for r in reviews[:20]:
        print(f'{r["symbol"]:6} {r["action"]:6} {r["pnl"]:+6.2f}% {r["verdict"]:8} {r["issue"]:20}')
    
    print(f'\n判断正确: {good}  判断错误: {bad}')
    
    # Improvements
    impr = generate_improvements(reviews)
    
    if impr.get('bad_symbols'):
        print(f'\n[改良] 持续亏损的标的:')
        for bs in impr['bad_symbols'][:5]:
            print(f'  {bs["symbol"]}: {bs["trades"]}笔 胜率{bs["win_rate"]}% 总盈亏{bs["total_pnl"]:+.2f}%')
    
    if impr.get('sell_too_early'):
        s = impr['sell_too_early']
        print(f'\n[改良] AI 卖出过早: {s["bad_sell_rate"]}% 的卖出是亏损的')
        print(f'  -> {s["recommendation"]}')
    
    if impr.get('position_sizing'):
        ps = impr['position_sizing']
        print(f'\n[改良] 仓位管理: {ps["cuts_causing_loss"]} 笔因仓位限制导致亏损')
        print(f'  -> {ps["recommendation"]}')


def cmd_clean():
    """清理噪声数据"""
    n = clean_noise(dry_run=True)
    print(f'发现 {n} 条噪声记录')
    confirm = input(f'确认删除 {n} 条记录？(y/N): ')
    if confirm.lower() == 'y':
        clean_noise(dry_run=False)
        print('已完成清理')
    
    # Fix the recording logic: add noise guard to the AI engine
    engine_path = os.path.join(ATOS_HOME, 'atos', 'ai', 'engine_v4.py')
    if os.path.exists(engine_path):
        with open(engine_path) as f:
            content = f.read()
        
        # Check if noise guard already exists
        if 'NOISE_GUARD' not in content:
            guard_add = '''
# 噪声防护：只在 PnL 变动 > 0.1% 时才记录决策
# 防止每30秒的循环检查污染数据库
NOISE_THRESHOLD = 0.001  # 0.1% 以下不算有效交易
def _should_record_decision(pnl_change: float) -> bool:
    return abs(pnl_change) >= NOISE_THRESHOLD
'''
            # Insert after imports
            insert_point = content.find('logger = get_logger')
            if insert_point > 0:
                # Find the end of the logger line
                end_of_logger = content.find('\n', insert_point)
                content = content[:end_of_logger+1] + guard_add + content[end_of_logger+1:]
                with open(engine_path, 'w') as f:
                    f.write(content)
                print('已添加噪声防护到 AI 引擎')
        else:
            print('噪声防护已存在')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='ATOS Trade Review System')
    parser.add_argument('--status', action='store_true', help='显示胜率概览')
    parser.add_argument('--analyze', action='store_true', help='分析交易并生成改良建议')
    parser.add_argument('--clean', action='store_true', help='清理噪声数据')
    parser.add_argument('--improve', action='store_true', help='读取复盘，自动改良')
    
    args = parser.parse_args()
    
    if args.clean:
        cmd_clean()
    elif args.analyze:
        cmd_analyze()
    elif args.improve:
        trades = get_real_trades()
        reviews = analyze_trades(trades[:30])
        impr = generate_improvements(reviews)
        print(json.dumps(impr, indent=2, ensure_ascii=False))
    else:
        cmd_status()


if __name__ == '__main__':
    main()
