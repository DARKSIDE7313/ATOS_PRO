#!/usr/bin/env python3
"""ATOS PRO Dashboard Server v5 — http://localhost:9000
Bug #14: 这是旧版仪表盘（http.server），新版在 web/server.py (FastAPI, port 8000)。
两个仪表盘功能重叠但互不冲突，web/server.py 是主要入口。"""
import http.server,json,os,sys,datetime,re,sqlite3,subprocess,threading,time
from urllib.parse import urlparse,parse_qs

BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,BASE)
_price_cache={};_price_ts=0

def _live_price(sym):
    """优先从 state 文件读取最新价格"""
    # 先查短线 state
    fp = os.path.join(BASE, 'data', 'shadow_state.json')
    if os.path.exists(fp):
        try:
            with open(fp) as f: raw = json.load(f)
            pos = raw.get('positions', {}) or {}
            if sym in pos and isinstance(pos[sym], dict):
                lp = pos[sym].get('last_price', 0) or 0
                if lp > 0: return lp
        except (json.JSONDecodeError, IOError, KeyError, TypeError): pass
    # 再查长线 state（longterm 标的不在 shadow 中）
    fp2 = os.path.join(BASE, 'data', 'longterm_state.json')
    if os.path.exists(fp2):
        try:
            with open(fp2) as f: raw = json.load(f)
            pos = raw.get('holdings', {}) or {}
            if sym in pos and isinstance(pos[sym], dict):
                lp = pos[sym].get('last_price', 0) or 0
                if lp > 0: return lp
        except (json.JSONDecodeError, IOError, KeyError, TypeError): pass
    return 0


# ── v5.1: 从文件读取日内涨跌（shadow_trader 每周期写入）──
_day_change_cache = {}
_day_change_ts = 0

def _load_day_changes() -> dict:
    """从 shadow_trader 写入的 day_changes.json 读取日内涨跌"""
    global _day_change_cache, _day_change_ts
    now = time.time()
    if _day_change_cache and (now - _day_change_ts) < 60:
        return _day_change_cache
    
    fp = os.path.join(BASE, 'data', 'day_changes.json')
    if os.path.exists(fp):
        try:
            with open(fp) as f:
                _day_change_cache = json.load(f)
            _day_change_ts = now
        except Exception:
            pass
    return _day_change_cache

def _fetch_price(sym):
    global _price_cache,_price_ts
    py = sys.executable  # Bug #13: 直接用当前 Python，不依赖不存在的 venv
    try:
        code="import sys,yfinance; t=yfinance.Ticker(sys.argv[1]); i=t.info or {}; p=i.get('currentPrice') or i.get('regularMarketPrice') or 0; print(p) if p else print(-1)"
        r=subprocess.run([py,'-c',code,sym],capture_output=True,timeout=10,text=True)
        p=float(r.stdout.strip())
        if p>0: _price_cache[sym]=p;_price_ts=time.time()
    except (subprocess.TimeoutExpired, ValueError, OSError): pass

def refresh_all_prices():
    """仅对 state 中没有价格的标的做一次 yfinance 查询"""
    all_syms = set()
    for fp in ['data/shadow_state.json','data/longterm_state.json']:
        fpn = os.path.join(BASE, fp)
        if os.path.exists(fpn):
            try:
                with open(fpn) as f: raw = json.load(f)
                for k in ['positions', 'holdings']:
                    all_syms.update((raw.get(k, {}) or {}).keys())
            except (json.JSONDecodeError, IOError, KeyError, TypeError): pass
    # 只获取 state 中没有 last_price 或 last_price=0 的标的
    for s in sorted(all_syms):
        if s not in _price_cache or _price_cache.get(s, 0) <= 0:
            _fetch_price(s)

def _calc_daily_pnl():
    """计算每日盈亏百分比（基于 shadow_state 中的 equity/trades）"""
    fp = os.path.join(BASE, 'data', 'shadow_state.json')
    if not os.path.exists(fp):
        return {'today_pnl': 0.0, 'today_pnl_pct': 0.0, 'total_pnl': 0.0, 'total_pnl_pct': 0.0}
    try:
        with open(fp) as f:
            raw = json.load(f)
        equity = raw.get('equity', 0) or 0
        init = raw.get('initial_cash', 1_000_000) or 1_000_000
        peak = raw.get('peak_equity', init) or init

        total_pnl = equity - init
        total_pnl_pct = round((equity - init) / init * 100, 2) if init > 0 else 0

        today = datetime.date.today().isoformat()

        # 从 trade_history 计算今日成交盈亏
        trades = raw.get('trade_history', []) or []
        today_trades = [t for t in trades if str(t.get('date', ''))[:10] == today]
        today_trade_pnl = sum(t.get('pnl', 0) or 0 for t in today_trades)

        # 从 equity_history 取今日首个和末个净值估算今日盈亏
        eq_hist = raw.get('equity_history', []) or []
        today_equities = [e for e in eq_hist if isinstance(e, dict) and str(e.get('time', ''))[:10] == today]
        if len(today_equities) >= 2:
            open_eq = today_equities[0].get('equity', equity)
            close_eq = today_equities[-1].get('equity', equity)
            today_pnl = close_eq - open_eq
        elif today_trade_pnl != 0:
            today_pnl = today_trade_pnl
        else:
            today_pnl = 0.0

        dd = round((peak - equity) / peak * 100, 2) if peak > 0 else 0

        return {
            'today_pnl': round(today_pnl, 2),
            'today_pnl_pct': round(today_pnl / equity * 100, 4) if equity > 0 else 0,
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': total_pnl_pct,
            'drawdown': dd,
            'equity': round(equity, 2),
            'peak': round(peak, 2),
            'today_trades': len(today_trades),
            'today_trade_pnl': round(today_trade_pnl, 2),
        }
    except Exception:
        return {'today_pnl': 0.0, 'today_pnl_pct': 0.0, 'total_pnl': 0.0, 'total_pnl_pct': 0.0}

def read_state():
    d={'short':{},'long':{},'activity':[],'short_stops':[],'trades':[],'combined':{}}
    fpn=os.path.join(BASE,'data','shadow_state.json')
    if os.path.exists(fpn):
        try:
            with open(fpn) as f: raw=json.load(f)
            init=raw.get('initial_cash',1_000_000); cash=raw.get('cash',0)
            pv=cash; pl_total=0; plist=[]
            # v5.1: 读取 shadow_trader 写入的日内涨跌数据
            futu_data = _load_day_changes()
            
            for sym,dt in (raw.get('positions',{}) or {}).items():
                if not isinstance(dt,dict): continue
                sh=dt.get('qty',0) or 0; avg=dt.get('avg_price',0) or 0
                if sh==0: continue
                last_close=dt.get('last_price',avg)
                live=_live_price(sym)
                price=live if live>0 else (last_close if last_close>0 else avg)
                val=sh*price; pl=(price-avg)*sh; pl_total+=pl; pv+=val
                # v5.1: 优先使用 Futu OpenD 实时日内涨跌
                fd = futu_data.get(sym, {})
                day_chg = fd.get('day_chg', price - last_close if last_close > 0 else 0)
                day_pct = fd.get('day_pct', round(day_chg/last_close*100, 2) if last_close > 0 else 0)
                prev_close = fd.get('prev_close', last_close)
                plist.append({'sym':sym,'shares':sh,'avg':round(avg,2),'price':round(price,2),
                    'val':round(val,2),'pl':round(pl,2),
                    'pl_pct':round((price-avg)/avg*100,2) if avg>0 else 0,
                    'last_close':round(prev_close,2),'day_chg':round(day_chg,2),
                    'day_pct':round(day_pct,2)})
            tv=sum(x['val'] for x in plist) or 1
            for x in plist: x['wt']=round(x['val']/tv*100,1)
            chg=(pv-init)/init*100 if init>0 else 0
            d['short']={'pv':round(pv,2),'pl':round(pl_total,2),'cash':round(cash,2),
                'init':round(init,2),'chg':round(chg,2),'pos':plist,'cnt':len(plist)}
            stops=raw.get('trailing_stops',{})
            d['short_stops']=[{'sym':s,'trail':round(v.get('trail_pct',0)*100,1),
                'high':round(v.get('highest_price',0),2),'stop':round(v.get('stop_price',0),2),
                'entry':round(v.get('entry_price',0),2),
                'risk':round((v.get('highest_price',0)-v.get('stop_price',0))/max(v.get('highest_price',1),1)*100,1)
                } for s,v in stops.items()]
            th=raw.get('trade_history',[])
            d['trades']=[{'time':t.get('date',''),'sym':t.get('symbol',''),'act':t.get('action',''),
                'qty':t.get('shares',0),'price':t.get('price',0),'pl':t.get('pnl',0),
                'reason':t.get('reason','')} for t in (th or [])][-25:]
            d['cycle_count']=raw.get('cycle_count',0)
            d['last_cycle']=raw.get('last_cycle','')
            d['equity_total']=round(raw.get('equity',0),2)
        except (json.JSONDecodeError, IOError, KeyError, TypeError): pass
    # v29: 长线组合已删除 — Shadow Trader 是唯一系统
    d['long'] = {'pv':0, 'pl':0, 'cash':0, 'init':0, 'chg':0, 'pos':[], 'cnt':0,
                 'archived': True, 'note': 'Long-term portfolios removed in v29 — unified into Shadow Trader'}
    d['long_meta'] = {'rebalance': 'removed', 'last_run': '', 'runs': 0}
    si=d.get('short',{})
    d['combined']={'pv':si.get('pv',0),'pl':si.get('pl',0),'init':si.get('init',0),
        'chg':si.get('chg',0),'cash':si.get('cash',0)}
    d['daily'] = _calc_daily_pnl()
    # 🏦 基金级业绩指标（从 Shadow Trader 每周期更新）
    try:
        pf = os.path.join(BASE, 'data', 'performance.json')
        if os.path.exists(pf):
            with open(pf) as f:
                pm = json.load(f)
            m = pm.get('metrics', {})
            d['fund_metrics'] = {
                'sharpe': m.get('sharpe', 0),
                'sortino': m.get('sortino', 0),
                'calmar': m.get('calmar', 0),
                'max_drawdown_pct': m.get('max_drawdown', 0),
                'annual_return_pct': round(m.get('annual_return', 0), 1),
                'annual_volatility_pct': round(m.get('annual_volatility', 0), 1),
                'win_rate_pct': round(m.get('win_rate', 0), 1),
                'profit_factor': round(m.get('profit_factor', 0), 2),
                'grade': m.get('grade', 'N/A'),
                'cycles': m.get('cycles', 0),
            }
            # 基准对比: SPY 同期表现
            spy_ret = round(m.get('benchmark_return', 0), 1) if 'benchmark_return' in m else None
            d['fund_metrics']['benchmark_spy_return_pct'] = spy_ret
            d['fund_metrics']['alpha_pct'] = round(m.get('annual_return', 0) - (spy_ret or 0), 1)
    except Exception:
        d['fund_metrics'] = None
    # 📊 集成 auto-monitor 健康检查报告
    try:
        hp = os.path.join(BASE, 'data', 'health_check_state.json')
        if os.path.exists(hp):
            with open(hp) as f:
                h = json.load(f)
            d['health'] = {
                'status': 'healthy' if h.get('consecutive_failures', 0) < 3 and h.get('consecutive_restarts', 0) < 2 else 'degraded',
                'last_check': h.get('last_check', ''),
                'last_cycle': h.get('last_cycle', 0),
                'restarts': h.get('consecutive_restarts', 0),
                'errors': h.get('consecutive_failures', 0),
                'trend': h.get('trend', {}),
                'perf': h.get('performance', {}),
                'exposure': h.get('exposure', {}),
            }
    except (json.JSONDecodeError, IOError, KeyError, TypeError): pass
    rd=os.path.join(BASE,'reports')
    if os.path.exists(rd):
        # 先收集所有 phoenix_report_ 文件，按修改时间排序
        all_reports = []
        for fn in os.listdir(rd):
            if fn.startswith('phoenix_report_'):
                fpath = os.path.join(rd, fn)
                if os.path.isfile(fpath):
                    all_reports.append((os.path.getmtime(fpath), fn))
        all_reports.sort(reverse=True)  # 最新的在前
        for _, fn in all_reports[:15]:
            fpath = os.path.join(rd, fn)
            try:
                with open(fpath) as f:
                    rpt = json.load(f)
                s = rpt.get('summary', {})
                d['activity'].append({'time': fn.replace('phoenix_report_', '').replace('.json', ''),
                    'msg': f"Phoenix #{s.get('run_id', '?')} | {s.get('market_phase', '?')} | {s.get('total_orders', 0)} orders"})
            except:
                pass
    # 📊 添加 auto-monitor 活动日志（最近5条事件）
    try:
        ml=os.path.join(BASE,'logs','auto_monitor.log')
        if os.path.exists(ml):
            events=[]
            with open(ml) as f:
                for line in f:
                    if 'ALERT' in line or 'CRITICAL' in line:
                        m=re.search(r'\|.*?\| (.*)', line)
                        if m: events.append(m.group(1).strip()[:120])
            d['monitor_events']=events[-5:]
    except (json.JSONDecodeError, IOError, KeyError, TypeError): pass

    # v28: 策略信息
    d['strategy'] = {
        'name': 'v28 QQQ Core + Alpha',
        'description': '60% QQQ + 40% momentum stocks (5 picks), quarterly rebalance',
        'backtest_annual_return': 26.81,
        'benchmark_spy_annual': 15.09,
        'alpha_vs_spy': 11.72,
        'sharpe_ratio': 1.13,
        'max_drawdown': 39.4,
        'fee_drag_annual': 0.13,
        'core_pct': 60,
        'alpha_count': 5,
        'rebalance_days': 63,
        'stop_loss_stock': 5.0,
        'stop_loss_qqq': 12.0,
    }
    # v28: 当前配置
    try:
        ss = json.load(open(os.path.join(BASE, 'data', 'shadow_state.json')))
        eq = ss.get('equity', 0)
        qqq_pos = ss.get('positions', {}).get('QQQ', {})
        qqq_val = qqq_pos.get('qty', 0) * qqq_pos.get('last_price', 0)
        d['strategy']['current'] = {
            'equity': round(eq, 2),
            'cash': round(ss.get('cash', 0), 2),
            'cash_pct': round(ss.get('cash', 0) / eq * 100, 1) if eq > 0 else 0,
            'qqq_value': round(qqq_val, 2),
            'qqq_pct': round(qqq_val / eq * 100, 1) if eq > 0 else 0,
            'positions': len(ss.get('positions', {})),
            'total_return': round((eq / 300000 - 1) * 100, 2),
        }
    except Exception:
        pass
    return d

def read_ai_insights():
    """🏦 v23: 可执行智能分析 — 每个持仓给出具体操作建议"""
    db = os.path.join(BASE, 'data', 'ai_memory.db')
    result = {'decisions': [], 'stats': {}, 'patterns': [], 'sym_stats': [],
              'signal_analysis': [], 'alerts': [], 'portfolio_insight': {}}

    try:
        sf = os.path.join(BASE, 'data', 'shadow_state.json')
        if not os.path.exists(sf):
            result['error'] = 'shadow_state.json not found'
            return result

        import signal as _sig
        def _alarm(signum, frame): raise TimeoutError()
        _sig.signal(_sig.SIGALRM, _alarm)
        _sig.alarm(3)
        try:
            with open(sf) as f:
                st = json.load(f)
        finally:
            _sig.alarm(0)

        positions = st.get('positions', {})
        stops = st.get('trailing_stops', {})
        eq = st.get('equity', 0)
        init = st.get('initial_cash', 300000)
        cash = st.get('cash', 0)
        trade_hist = st.get('trade_history', [])
        trailing = st.get('trailing_stops', {})

        # ── 逐持仓深度分析 ──
        for sym, pos in sorted(positions.items()):
            avg = pos.get('avg_price', 0)
            lp = pos.get('last_price', avg)
            qty = pos.get('qty', 0)
            pnl_pct = (lp / avg - 1) * 100 if avg > 0 else 0
            mkt_val = qty * lp
            weight = mkt_val / eq * 100 if eq > 0 else 0

            ts = stops.get(sym, {})
            stop_px = ts.get('stop_price', ts.get('stop', 0))  # 兼容 stop/stop_price 两种key
            risk_to_stop = (lp - stop_px) / lp * 100 if lp > 0 and stop_px > 0 else 0

            # 信号评分 (0-100)
            score = 50  # 基准
            action = '持有'
            action_reason = ''

            if pnl_pct > 8:
                score += 20
                action = '部分止盈'
                action_reason = f'盈利{pnl_pct:.1f}%，建议卖1/3锁利'
            elif pnl_pct > 5:
                score += 15
                action = '持有'
                action_reason = f'盈利{pnl_pct:.1f}%，让利润奔跑'
            elif pnl_pct > 2:
                score += 10
                action = '持有'
                action_reason = f'小幅盈利{pnl_pct:.1f}%'
            elif pnl_pct < -5:
                score -= 20
                action = '止损'
                action_reason = f'亏损{pnl_pct:.1f}%，建议立即止损'
            elif pnl_pct < -3:
                score -= 10
                action = '密切关注'
                action_reason = f'亏损{pnl_pct:.1f}%，接近止损线'
            elif pnl_pct < -1:
                score -= 5
                action = '持有'
                action_reason = f'小幅亏损{pnl_pct:.1f}%'
            else:
                score += 0
                action = '持有'
                action_reason = f'持平({pnl_pct:+.1f}%)'

            # 距止损风险评估
            if risk_to_stop > 0 and risk_to_stop < 2:
                score -= 15
                result['alerts'].append({
                    'type': 'danger', 'sym': sym,
                    'msg': f'🚨 {sym} 距止损仅{risk_to_stop:.1f}%! 立即评估'
                })
            elif risk_to_stop > 0 and risk_to_stop < 4:
                score -= 5
                result['alerts'].append({
                    'type': 'warning', 'sym': sym,
                    'msg': f'⚠️ {sym} 距止损{risk_to_stop:.1f}%，注意风险'
                })

            # 权重风险
            if weight > 15:
                result['alerts'].append({
                    'type': 'concentration', 'sym': sym,
                    'msg': f'⚠️ {sym} 权重{weight:.1f}%过大，建议减仓'
                })

            result['signal_analysis'].append({
                'sym': sym, 'qty': qty,
                'avg_price': round(avg, 2), 'last_price': round(lp, 2),
                'pnl_pct': round(pnl_pct, 2), 'pnl_dollar': round((lp - avg) * qty, 2),
                'weight_pct': round(weight, 1),
                'stop_price': round(stop_px, 2), 'risk_to_stop_pct': round(risk_to_stop, 1),
                'score': max(0, min(100, score)),
                'action': action, 'action_reason': action_reason
            })

        # ── 组合级别分析 ──
        invested = eq - cash
        total_ret = (eq / init - 1) * 100 if init > 0 else 0
        peak = st.get('peak_equity', eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0

        # 行业集中度
        sectors = {}
        sector_map = {
            'AAPL': 'Tech', 'MSFT': 'Tech', 'NVDA': 'Tech', 'GOOGL': 'Tech', 'META': 'Tech',
            'JPM': 'Finance', 'BAC': 'Finance', 'GS': 'Finance', 'MS': 'Finance', 'V': 'Finance',
            'JNJ': 'Health', 'UNH': 'Health', 'MRK': 'Health', 'PFE': 'Health', 'ABBV': 'Health',
            'XOM': 'Energy', 'CVX': 'Energy',
            'SBUX': 'Consumer', 'MCD': 'Consumer', 'KO': 'Consumer', 'PG': 'Consumer',
            'QQQ': 'ETF', 'SPY': 'ETF', 'IWM': 'ETF'
        }
        for sym, pos in positions.items():
            sec = sector_map.get(sym, 'Other')
            mkt = pos.get('qty', 0) * pos.get('last_price', pos.get('avg_price', 0))
            sectors[sec] = sectors.get(sec, 0) + mkt

        sector_pcts = {k: round(v / invested * 100, 1) for k, v in sorted(sectors.items(), key=lambda x: -x[1])}

        # 最近7天交易统计
        from datetime import datetime as _dt, timedelta as _td
        week_ago = (_dt.now() - _td(days=7)).isoformat()
        recent_trades = [t for t in trade_hist if t.get('date', '') > week_ago]
        recent_sells = [t for t in recent_trades if t.get('action') == 'SELL']
        recent_wins = [t for t in recent_sells if t.get('pnl', 0) > 0]

        result['portfolio_insight'] = {
            'equity': round(eq, 2),
            'cash': round(cash, 2),
            'cash_pct': round(cash / eq * 100, 1),
            'invested_pct': round(invested / eq * 100, 1),
            'total_return_pct': round(total_ret, 2),
            'drawdown_pct': round(dd, 2),
            'positions_count': len(positions),
            'sector_concentration': sector_pcts,
            'weekly_trades': len(recent_trades),
            'weekly_win_rate': round(len(recent_wins) / len(recent_sells) * 100, 1) if recent_sells else 0,
            'weekly_pnl': round(sum(t.get('pnl', 0) for t in recent_sells), 2),
            'top_risk': max(result['signal_analysis'], key=lambda x: -x['score'])['sym'] if result['signal_analysis'] else 'N/A',
        }

        # 组合建议
        recs = []
        if cash / eq > 0.25:
            recs.append(f'现金占比{cash/eq*100:.0f}%过高，建议找机会加仓')
        if cash / eq < 0.05:
            recs.append('现金不足5%，注意流动性风险')
        if dd > 5:
            recs.append(f'回撤{dd:.1f}%较大，考虑降低仓位')
        if len(positions) < 6:
            recs.append(f'仅{len(positions)}只持仓，考虑分散到8-12只')
        max_sector = max(sector_pcts.values()) if sector_pcts else 0
        if max_sector > 40:
            recs.append(f'最大行业占比{max_sector:.0f}%，建议分散')
        if not recs:
            recs.append('组合结构健康，继续持有')

        result['portfolio_insight']['recommendations'] = recs

    except Exception as e:
        result['signal_analysis_error'] = str(e)

    # DB 历史
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(db, timeout=2)
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            wins = conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='WIN'").fetchone()[0]
            losses = conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='LOSS'").fetchone()[0]
            rows = conn.execute("""SELECT d.id,d.timestamp,d.symbol,d.action,d.confidence,
                d.factor_score,d.debate_summary,d.market_regime,
                o.outcome_type,o.pnl_pct,o.exit_reason,o.ai_correct
                FROM decisions d LEFT JOIN outcomes o ON d.id=o.decision_id
                ORDER BY d.timestamp DESC LIMIT 40""").fetchall()
            result['decisions'] = [{'id': r['id'], 'time': r['timestamp'], 'sym': r['symbol'],
                'action': r['action'],
                'conf': round(r['confidence'], 3) if r['confidence'] else 0,
                'score': round(r['factor_score'], 3) if r['factor_score'] else 0,
                'summary': (r['debate_summary'] or '')[:120], 'regime': r['market_regime'] or '?',
                'outcome': r['outcome_type'] or 'pending',
                'pnl': round(r['pnl_pct'] * 100, 2) if r['pnl_pct'] else None,
                'exit': r['exit_reason'] or '', 'correct': r['ai_correct']} for r in rows]
            result['stats'] = {'total': total, 'wins': wins, 'losses': losses,
                'win_rate': round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0}
            conn.close()
        except Exception as e:
            result['db_error'] = str(e)

    # ── v23: 用实际交易统计替代 AI 辩论统计（AI辩论API已挂，数据不可靠）──
    try:
        ts_file = os.path.join(BASE, 'data', 'trade_stats.json')
        if os.path.exists(ts_file):
            with open(ts_file) as f:
                trade_stats = json.load(f)
            result['stats'] = {
                'total': trade_stats.get('total_trades', 0),
                'wins': trade_stats.get('wins', 0),
                'losses': trade_stats.get('losses', 0),
                'win_rate': round(trade_stats.get('win_rate', 0) * 100, 1),
                'win_loss_ratio': round(trade_stats.get('win_loss_ratio', 0), 2),
                'avg_win_pct': round(trade_stats.get('avg_win', 0) * 100, 2),
                'avg_loss_pct': round(trade_stats.get('avg_loss', 0) * 100, 2),
                'kelly_wr': trade_stats.get('win_rate', 0),
                'kelly_wlr': trade_stats.get('win_loss_ratio', 0),
                'source': 'actual_trades',
            }
    except Exception:
        pass

    return result

def _get_api_key(key_name: str) -> str:
    """从环境变量或 .env 文件读取 API key"""
    key = os.environ.get(key_name, '')
    if key:
        return key
    try:
        with open(os.path.join(BASE, '.env')) as f:
            for line in f:
                if line.startswith(f'{key_name}='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except:
        pass
    return ''

def chat_with_ai(message: str, provider: str = "kimi") -> str:
    """Call Kimi (primary) or DeepSeek API for chat responses.
    Kimi = 月之暗面 Moonshot, better at Chinese financial analysis."""
    import requests
    
    if provider == "kimi":
        api_key = _get_api_key('KIMI_API_KEY')
        if api_key:
            return _call_llm(api_key, message, "https://api.moonshot.cn/v1", "kimi-k2.6")
    
    # Fallback to DeepSeek
    api_key = _get_api_key('DEEPSEEK_API_KEY')
    if api_key:
        return _call_llm(api_key, message, "https://api.deepseek.com", "deepseek-chat")
    
    return '未配置 AI API Key。请在 .env 中设置 KIMI_API_KEY 或 DEEPSEEK_API_KEY。'

def _call_llm(api_key: str, message: str, base_url: str, model: str) -> str:
    """通用 LLM 调用"""
    import requests
    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': '你是 ATOS PRO 量化交易系统的 AI 助手。用中文回答，简洁专业。可以讨论交易策略、技术分析、风险管理、持仓建议、市场解读等。'},
                    {'role': 'user', 'content': message}
                ],
                'max_tokens': 2000,  # v22: 提高以容纳 kimi-k3 的 reasoning
                'temperature': 1.0  # Kimi only supports temperature=1
            },
            timeout=25)
        data = resp.json()
        if "choices" in data and data["choices"]:
            content = data["choices"][0].get("message", {}).get("content", "")
            # kimi-k3 可能把 tokens 都花在 reasoning 上导致 content 为空
            if not content:
                reasoning = data["choices"][0].get("message", {}).get("reasoning_content", "")
                if reasoning:
                    content = f"[思考过程]\n{reasoning[:500]}...\n\n[需要更多 tokens 来生成回复]"
                else:
                    content = "(空响应，请重试)"
            return content
        return f'AI 响应异常: {str(data)[:200]}'
    except Exception as e:
        return f'AI 响应失败: {str(e)}'

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/api': self._j(read_state())
        elif p.path=='/api/ai': self._j(read_ai_insights())
        elif p.path=='/api/news':
            try:
                nf = os.path.join(BASE, 'data', 'news_sentiment.json')
                if os.path.exists(nf):
                    with open(nf) as f:
                        raw = json.load(f)
                    # v28i: 统一 key 名 — stocks→sentiments, macro→macro_sentiment
                    stocks = raw.get('stocks', raw.get('sentiments', {}))
                    macro = raw.get('macro', raw.get('macro_sentiment', 0))
                    self._j({
                        'sentiments': stocks,
                        'macro_sentiment': macro,
                        'updated': raw.get('updated', ''),
                        'count': len(stocks),
                    })
                else: self._j({'sentiments': {}, 'macro_sentiment': 0.0, 'updated': 'never', 'count': 0})
            except Exception as e: self._j({'error': str(e)})
        elif p.path=='/api/daily':
            try:
                from atos.core.daily_returns import get_summary
                self._j(get_summary())
            except Exception as e:
                self._j({'error': str(e)})
        elif p.path=='/api/metrics':
            try:
                pf = os.path.join(BASE, 'data', 'performance.json')
                if os.path.exists(pf):
                    with open(pf) as f: self._j(json.load(f))
                else: self._j({'error': 'no metrics data'})
            except: self._j({'error': 'read failed'})
        elif p.path=='/api/health':
            try:
                hp=os.path.join(BASE,'data','health_check_state.json')
                if os.path.exists(hp):
                    with open(hp) as f: self._j(json.load(f))
                else: self._j({'error':'no health data'})
            except: self._j({'error':'read failed'})
        elif p.path=='/api/backtest':
            try:
                # v28i: 优先读 v5（最新），fallback v4
                bt5 = os.path.join(BASE, 'data', 'backtest_v5_result.json')
                bt4 = os.path.join(BASE, 'data', 'backtest_v4_result.json')
                if os.path.exists(bt5):
                    with open(bt5) as f:
                        raw = json.load(f)
                    results = raw.get('results', [])
                    best = max(results, key=lambda x: x.get('annual', 0)) if results else {}
                    self._j({
                        'best_strategy': {
                            'name': best.get('name', ''),
                            'annual_return': best.get('annual', 0),
                            'max_drawdown': best.get('max_dd', 0),
                            'sharpe': best.get('sharpe', 0),
                            'fee_pct': best.get('fee_pct', 0),
                        },
                        'spy_annual': 15.09,
                        'alpha': round(best.get('annual', 0) - 15.09, 2),
                        'all_strategies': results,
                        'timestamp': raw.get('timestamp', ''),
                    })
                elif os.path.exists(bt4):
                    with open(bt4) as f: self._j(json.load(f))
                else: self._j({'error':'no backtest data'})
            except Exception as e: self._j({'error':str(e)})
        elif p.path=='/api/refresh':
            threading.Thread(target=refresh_all_prices,daemon=True).start()
            self._j({'ok':True})
        elif p.path=='/api/chat':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                msg = body.get('message','')
                provider = body.get('provider','kimi')
                if not msg:
                    self._j({'error':'message required'})
                else:
                    reply = chat_with_ai(msg, provider)
                    self._j({'reply': reply})
            except Exception as e:
                self._j({'error': str(e)})
        else:
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-cache, no-store, must-revalidate'); self.send_header('Pragma','no-cache'); self.send_header('Expires','0'); self.end_headers()
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'index.html')) as f:
                try:
                    self.wfile.write(f.read().encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
    def _j(self,d):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-cache'); self.end_headers()
        try:
            self.wfile.write(json.dumps(d,ensure_ascii=False,default=str).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected, ignore
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.end_headers()
    def do_POST(self):
        p=urlparse(self.path)
        if p.path=='/api/chat':
            try:
                length=int(self.headers.get('Content-Length',0))
                body=json.loads(self.rfile.read(length)) if length>0 else {}
                provider = body.get('provider','kimi')
                reply=chat_with_ai(body.get('message',''), provider)
                self._j({'reply':reply, 'provider':provider})
            except Exception as e:
                self._j({'reply':f'错误: {str(e)}'})
        else:
            self.send_response(404); self.end_headers()

if __name__=='__main__':
    import socketserver
    class S(http.server.HTTPServer):
        allow_reuse_address=True; allow_reuse_port=True; daemon_threads=True
    print('ATOS Dashboard -> http://localhost:9000')
    # Load prices in background — don't block startup
    import threading
    threading.Thread(target=refresh_all_prices, daemon=True).start()
    S(('127.0.0.1', 9000), H).serve_forever()
