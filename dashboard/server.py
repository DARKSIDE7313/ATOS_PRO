#!/usr/bin/env python3
"""ATOS PRO Dashboard Server v5 — http://localhost:9000"""
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
    # fallback 到缓存
    if sym in _price_cache:
        cached = _price_cache[sym]
        if cached > 0: return cached
    return 0

def _fetch_price(sym):
    global _price_cache,_price_ts
    py=os.path.join(BASE,'venv','bin','python3')
    if not os.path.exists(py): py=sys.executable
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

def read_state():
    d={'short':{},'long':{},'activity':[],'short_stops':[],'trades':[],'combined':{}}
    fpn=os.path.join(BASE,'data','shadow_state.json')
    if os.path.exists(fpn):
        try:
            with open(fpn) as f: raw=json.load(f)
            init=raw.get('initial_cash',1_000_000); cash=raw.get('cash',0)
            pv=cash; pl_total=0; plist=[]
            for sym,dt in (raw.get('positions',{}) or {}).items():
                if not isinstance(dt,dict): continue
                sh=dt.get('qty',0) or 0; avg=dt.get('avg_price',0) or 0
                if sh==0: continue
                last_close=dt.get('last_price',avg)
                live=_live_price(sym)
                price=live if live>0 else (last_close if last_close>0 else avg)
                val=sh*price; pl=(price-avg)*sh; pl_total+=pl; pv+=val
                day_chg=price-last_close if last_close>0 else 0
                plist.append({'sym':sym,'shares':sh,'avg':round(avg,2),'price':round(price,2),
                    'val':round(val,2),'pl':round(pl,2),
                    'pl_pct':round((price-avg)/avg*100,2) if avg>0 else 0,
                    'last_close':round(last_close,2),'day_chg':round(day_chg,2),
                    'day_pct':round(day_chg/last_close*100,2) if last_close>0 else 0})
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
    fpn=os.path.join(BASE,'data','longterm_state.json')
    if os.path.exists(fpn):
        try:
            with open(fpn) as f: raw=json.load(f)
            pos=raw.get('holdings',{}); cash=raw.get('cash',0); init=raw.get('initial_cash',1_000_000)
            pv=cash; pl_total=0; plist=[]
            for sym,dt in pos.items():
                if not isinstance(dt,dict): continue
                sh=dt.get('shares',0) or 0; avg=dt.get('avg_cost',0) or 0
                if sh==0: continue
                live=_live_price(sym); price=live if live>0 else avg
                val=sh*price; pl=(price-avg)*sh; pl_total+=pl; pv+=val
                plist.append({'sym':sym,'shares':sh,'avg':round(avg,2),'price':round(price,2),
                    'val':round(val,2),'pl':round(pl,2),
                    'pl_pct':round((price-avg)/avg*100,2) if avg>0 else 0,
                    'score':dt.get('composite_score',''),'buy_date':dt.get('buy_date',''),
                    'day_chg':0,'day_pct':0})
            tv=sum(x['val'] for x in plist) or 1
            for x in plist: x['wt']=round(x['val']/tv*100,1)
            chg=(pv-init)/init*100 if init>0 else 0
            d['long']={'pv':round(pv,2),'pl':round(pl_total,2),'cash':round(cash,2),
                'init':round(init,2),'chg':round(chg,2),'pos':plist,'cnt':len(plist)}
            d['long_meta']={'rebalance':raw.get('last_rebalance','')}
        except: pass
    si=d.get('short',{}); li=d.get('long',{})
    c_init=(si.get('init',0)or 0)+(li.get('init',0)or 0)
    c_pv=(si.get('pv',0)or 0)+(li.get('pv',0)or 0)
    c_pl=(si.get('pl',0)or 0)+(li.get('pl',0)or 0)
    c_chg=((c_pv-c_init)/c_init*100) if c_init>0 else 0
    d['combined']={'pv':round(c_pv,2),'pl':round(c_pl,2),'init':round(c_init,2),'chg':round(c_chg,2),'cash':round((si.get('cash',0)or 0)+(li.get('cash',0)or 0),2)}
    rd=os.path.join(BASE,'reports')
    if os.path.exists(rd):
        for fn in sorted(os.listdir(rd),reverse=True)[:15]:
            if fn.startswith('phoenix_report_'):
                try:
                    with open(os.path.join(rd,fn)) as f: rpt=json.load(f)
                    s=rpt.get('summary',{})
                    d['activity'].append({'time':fn.replace('phoenix_report_','').replace('.json',''),
                        'msg':f"Phoenix #{s.get('run_id','?')} | {s.get('market_phase','?')} | {s.get('total_orders',0)} orders"})
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

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/api': self._j(read_state())
        elif p.path=='/api/ai': self._j(read_ai_insights())
        elif p.path=='/api/refresh':
            threading.Thread(target=refresh_all_prices,daemon=True).start()
            self._j({'ok':True})
        else:
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers()
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'index.html')) as f:
                self.wfile.write(f.read().encode())
    def _j(self,d):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-cache'); self.end_headers()
        self.wfile.write(json.dumps(d,ensure_ascii=False,default=str).encode())
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.end_headers()

if __name__=='__main__':
    import socketserver
    class S(http.server.HTTPServer):
        allow_reuse_address=True; allow_reuse_port=True; daemon_threads=True
    print('ATOS Dashboard -> http://localhost:9000')
    # Load prices in background — don't block startup
    import threading
    threading.Thread(target=refresh_all_prices, daemon=True).start()
    S(('0.0.0.0',9000),H).serve_forever()
