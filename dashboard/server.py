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
        except: pass
    # 再查长线 state（longterm 标的不在 shadow 中）
    fp2 = os.path.join(BASE, 'data', 'longterm_state.json')
    if os.path.exists(fp2):
        try:
            with open(fp2) as f: raw = json.load(f)
            pos = raw.get('holdings', {}) or {}
            if sym in pos and isinstance(pos[sym], dict):
                lp = pos[sym].get('last_price', 0) or 0
                if lp > 0: return lp
        except: pass
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
    except: pass

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
            except: pass
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
        except: pass
    # 读取 Phoenix 长期仓位 (合并新旧两个来源)
    d['long'] = {'pv':0, 'pl':0, 'cash':0, 'init':0, 'chg':0, 'pos':[], 'cnt':0}

    def _add_long_positions(pos_dict, cash, init, source_name):
        """添加一批长期仓位到显示数据"""
        pv = d['long']['pv'] + cash
        pl_total = d['long']['pl']
        plist = list(d['long']['pos'])
        total_init = d['long']['init'] + init

        for sym, dt in pos_dict.items():
            if not isinstance(dt, dict): continue
            sh = dt.get('shares', 0) or 0
            avg = dt.get('avg_cost', 0) or 0
            if sh == 0: continue
            live = _live_price(sym)
            price = live if live > 0 else avg
            val = sh * price
            pl = (price - avg) * sh
            pl_total += pl
            pv += val
            plist.append({
                'sym': sym, 'shares': sh, 'avg': round(avg, 2), 'price': round(price, 2),
                'val': round(val, 2), 'pl': round(pl, 2),
                'pl_pct': round((price-avg)/avg*100, 2) if avg > 0 else 0,
                'buy_date': dt.get('buy_date', ''), 'source': source_name,
                'day_chg': 0, 'day_pct': 0
            })

        tv = sum(x['val'] for x in plist) or 1
        for x in plist: x['wt'] = round(x['val']/tv*100, 1)
        chg = (pv - total_init) / total_init * 100 if total_init > 0 else 0

        d['long'] = {
            'pv': round(pv, 2), 'pl': round(pl_total, 2),
            'cash': round(cash, 2), 'init': round(total_init, 2),
            'chg': round(chg, 2), 'pos': plist, 'cnt': len(plist)
        }

    # 1. 旧 Phoenix v2 仓位 (legacy_portfolio.json — 永久安全，Phoenix 不会碰)
    fpn_legacy = os.path.join(BASE, 'data', 'legacy_portfolio.json')
    if os.path.exists(fpn_legacy):
        try:
            with open(fpn_legacy) as f:
                leg = json.load(f)
            _add_long_positions(
                leg.get('positions', {}),
                leg.get('cash', 0),
                leg.get('initial_cash', 1_000_000),
                'legacy'
            )
        except: pass

    # 2. Phoenix v3 仓位 — 只添加不在 legacy 里的，不额外加资金
    fpn_new = os.path.join(BASE, 'phoenix_state.json')
    if os.path.exists(fpn_new):
        try:
            with open(fpn_new) as f:
                raw = json.load(f)
            new_pos = raw.get('positions', {}) or {}
            legacy_syms = set()
            if os.path.exists(fpn_legacy):
                try:
                    with open(fpn_legacy) as f:
                        leg = json.load(f)
                    legacy_syms = set(leg.get('positions', {}).keys())
                except: pass
            new_only = {k: v for k, v in new_pos.items() if k not in legacy_syms}
            if new_only:
                # 不加额外资金 — 只用 legacy 的 $1M 初始
                _add_long_positions(new_only, 0, 0, 'phoenix_v3')
        except: pass

    d['long_meta'] = {'rebalance': '2026-06-03 (legacy) + Phoenix v3',
                      'last_run': '', 'runs': 0}
    # 从 phoenix_state.json 获取运行次数
    if os.path.exists(fpn_new):
        try:
            with open(fpn_new) as f:
                raw = json.load(f)
            d['long_meta'] = {
                'rebalance': '2026-06-03',
                'last_run': str(raw.get('last_full_run', ''))[:19],
                'runs': raw.get('runs', 0)
            }
        except: pass
    si=d.get('short',{}); li=d.get('long',{})
    c_init=(si.get('init',0)or 0)+(li.get('init',0)or 0)
    c_pv=(si.get('pv',0)or 0)+(li.get('pv',0)or 0)
    c_pl=(si.get('pl',0)or 0)+(li.get('pl',0)or 0)
    c_chg=((c_pv-c_init)/c_init*100) if c_init>0 else 0
    d['combined']={'pv':round(c_pv,2),'pl':round(c_pl,2),'init':round(c_init,2),'chg':round(c_chg,2),'cash':round((si.get('cash',0)or 0)+(li.get('cash',0)or 0),2)}
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
    except: pass
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
    except: pass
    return d

def read_ai_insights():
    db=os.path.join(BASE,'data','ai_memory.db')
    if not os.path.exists(db):
        return {'decisions':[],'stats':{},'patterns':[],'sym_stats':[],'note':'DB not found'}
    try:
        conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
        total=conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        wins=conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='WIN'").fetchone()[0]
        losses=conn.execute("SELECT COUNT(*) FROM outcomes WHERE outcome_type='LOSS'").fetchone()[0]
        rows=conn.execute("""SELECT d.id,d.timestamp,d.symbol,d.action,d.confidence,
            d.factor_score,d.debate_summary,d.market_regime,
            o.outcome_type,o.pnl_pct,o.exit_reason,o.ai_correct
            FROM decisions d LEFT JOIN outcomes o ON d.id=o.decision_id
            ORDER BY d.timestamp DESC LIMIT 40""").fetchall()
        decisions=[{'id':r['id'],'time':r['timestamp'],'sym':r['symbol'],'action':r['action'],
            'conf':round(r['confidence'],3)if r['confidence']else 0,
            'score':round(r['factor_score'],3)if r['factor_score']else 0,
            'summary':(r['debate_summary']or'')[:120],'regime':r['market_regime']or'?',
            'outcome':r['outcome_type']or'pending',
            'pnl':round(r['pnl_pct']*100,2)if r['pnl_pct']else None,
            'exit':r['exit_reason']or'','correct':r['ai_correct']} for r in rows]
        pat=conn.execute("""SELECT id,pattern_type,description,occurrence_count,confidence_impact,last_seen
            FROM patterns ORDER BY occurrence_count DESC,last_seen DESC LIMIT 20""").fetchall()
        patterns=[{'id':p['id'],'type':p['pattern_type'],'desc':(p['description']or'')[:100],
            'count':p['occurrence_count'],
            'impact':round(p['confidence_impact'],3)if p['confidence_impact']else 0,
            'last':p['last_seen']or''} for p in pat]
        sym=conn.execute("""SELECT d.symbol,COUNT(*)as t,
            SUM(CASE WHEN o.outcome_type='WIN' THEN 1 ELSE 0 END)as w,
            SUM(CASE WHEN o.outcome_type='LOSS' THEN 1 ELSE 0 END)as l
            FROM decisions d LEFT JOIN outcomes o ON d.id=o.decision_id
            GROUP BY d.symbol ORDER BY t DESC LIMIT 20""").fetchall()
        sym_stats=[{'sym':s['symbol'],'total':s['t'],'wins':s['w']or 0,'losses':s['l']or 0,
            'win_rate':round(s['w']/s['t']*100,1)if s['t']>0 else 0} for s in sym]
        conn.close()
        return {'decisions':decisions,'stats':{'total':total,'wins':wins,'losses':losses,
            'win_rate':round(wins/(wins+losses)*100,1)if(wins+losses)>0 else 0},
            'patterns':patterns,'sym_stats':sym_stats}
    except Exception as e: return {'decisions':[],'stats':{},'patterns':[],'sym_stats':[],'error':str(e)}

def chat_with_ai(message: str) -> str:
    """Call DeepSeek API for chat responses."""
    import requests
    api_key = os.environ.get('DEEPSEEK_API_KEY','')
    if not api_key:
        # Try reading from .env
        try:
            with open(os.path.join(BASE,'.env')) as f:
                for line in f:
                    if line.startswith('DEEPSEEK_API_KEY='):
                        api_key = line.split('=',1)[1].strip().strip('"').strip("'")
                        break
        except: pass
    if not api_key:
        return '未配置 DeepSeek API Key。请在 .env 中设置 DEEPSEEK_API_KEY。'
    try:
        resp = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'},
            json={'model':'deepseek-chat','messages':[
                {'role':'system','content':'你是 ATOS PRO 交易系统的 AI 助手。用中文回答，简洁专业。可以讨论交易策略、技术分析、风险管理、持仓建议等。'},
                {'role':'user','content':message}
            ],'max_tokens':800,'temperature':0.7},
            timeout=25)
        data = resp.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        return f'AI 响应失败: {str(e)}'

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/api': self._j(read_state())
        elif p.path=='/api/ai': self._j(read_ai_insights())
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
        elif p.path=='/api/refresh':
            threading.Thread(target=refresh_all_prices,daemon=True).start()
            self._j({'ok':True})
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
                reply=chat_with_ai(body.get('message',''))
                self._j({'reply':reply})
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
