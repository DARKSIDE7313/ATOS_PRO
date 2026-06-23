#!/usr/bin/env python3
"""
ATOS Auto-Monitor v3 — Self-healing system monitor
===================================================
Runs every 5 minutes. Detects crashes, analyzes root cause,
and triggers Hermes to fix the code when restart isn't enough.

Two-tier recovery:
  Tier 1 (quick): Restart the service (port lost, process died)
  Tier 2 (deep):  If restarts keep failing or code bug detected,
                  write an incident report and let Hermes fix the code

Monitors:
  - ShadowTrader (port 19999, cycle progress, stderr errors)
  - Dashboard (port 9000, HTTP 200)
  - FutuOpenD (port 11111)
  - AI/ULTRA failures (400 errors, JSON parse errors)
  - Code bugs (import errors, logic failures)
  - Equity drawdown alerts (configurable threshold)
"""

import os, sys, json, time, socket, subprocess, traceback, re, math, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ATOS_DIR = "/Users/benson/ATOS_PRO"
LOG_DIR = os.path.join(ATOS_DIR, "logs")
STATE_FILE = os.path.join(ATOS_DIR, "data", "shadow_state.json")
LOCK_FILE = os.path.join(ATOS_DIR, "data", ".shadow_trader.lock")
INCIDENTS_DIR = os.path.join(ATOS_DIR, "data", "incidents")
HEALTH_STATE_FILE = os.path.join(ATOS_DIR, "data", "health_check_state.json")
os.makedirs(INCIDENTS_DIR, exist_ok=True)

# ── Configuration ──────────────────────────────────────────
CHECK_INTERVAL = 300        # 5 minutes
RESTART_COOLDOWN = 120       # Wait 2min after restart before deep fixing
RESTART_THRESHOLD = 3        # If restarted 3+ times in 30min → deep fix
DRAWDOWN_ALERT_PCT = -5.0   # Alert if equity drops >5% from peak
MAX_RECENT_ERRORS = 5        # Max errors in window before deep fix

# ── Phased Drawdown Circuit Breaker ────────────────────────
# Each threshold: (drawdown_pct, recommended_exposure_pct)
DRAWDOWN_LEVELS = [
    (-5.0, 75),   # -5% → 75% exposure
    (-8.0, 50),   # -8% → 50% exposure
    (-12.0, 25),  # -12% → 25% exposure
]
EMERGENCY_DRAWDOWN_PCT = -10.0  # At -10% drawdown, trigger emergency stop
EXPOSURE_SIGNAL_FILE = "/tmp/atos_EXPOSURE"

# ── Network Resilience ──────────────────────────────────────
NETWORK_CHECK_HOSTS = ["8.8.8.8", "1.1.1.1", "api.deepseek.com"]
NETWORK_CHECK_TIMEOUT = 5       # seconds per host
NETWORK_RETRY_BACKOFF = [60, 120, 300, 600]  # progressive backoff on disconnect
MAX_NETWORK_FAILURES = 3        # After this many consecutive failures, do a full reconnect sweep
EMERGENCY_STOP_FILE = "/tmp/atos_EMERGENCY_STOP"

# ── Trend Tracking ─────────────────────────────────────────
EQUITY_TREND_DAYS = 7  # Lookback period for equity trend slope

# ── Helpers ────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | {level:7s} | monitor | {msg}")
    # 同时写入 ATOS 主日志，让 tail -f atos_*.log 能看到
    try:
        log_fn = os.path.join(ATOS_DIR, "logs", f"atos_{datetime.now().strftime('%Y%m%d')}.log")
        with open(log_fn, "a") as f:
            f.write(f"{ts} | {level:7s} | monitor | {msg}\n")
    except: pass

def check_port(port):
    try:
        r = subprocess.run(["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                          capture_output=True, text=True, timeout=5)
        return "LISTEN" in r.stdout
    except:
        return False

def check_http(url):
    try:
        import urllib.request
        urllib.request.urlopen(url, timeout=5)
        return True
    except:
        return False

def read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def read_health_state():
    try:
        with open(HEALTH_STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_health_state(data):
    with open(HEALTH_STATE_FILE, "w") as f:
        json.dump(data, f)

# ── Diagnostic Engine ──────────────────────────────────────

def get_incident_id():
    return f"inc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def analyze_stderr_errors(max_lines=300):
    """Parse the last N lines of stderr, extract structured crash info."""
    log_file = os.path.join(LOG_DIR, "shadow_trader_stderr.log")
    if not os.path.exists(log_file):
        return {"has_errors": False, "errors": []}
    
    result = subprocess.run(["tail", "-n", str(max_lines), log_file],
                           capture_output=True, text=True, timeout=10)
    lines = result.stdout.strip().split("\n")
    
    errors = []
    error_types = {"traceback": 0, "import_error": 0, "ultra_fail": 0,
                   "value_error": 0, "key_error": 0, "connection": 0}
    
    for i, line in enumerate(lines):
        if "Traceback" in line or "ERROR" in line or "Exception" in line:
            # Get context: 5 lines after
            context = "\n".join(lines[i:min(i+6, len(lines))])
            
            err_type = "unknown"
            if "Traceback" in line:
                err_type = "traceback"
            elif "ImportError" in context or "ModuleNotFoundError" in context:
                err_type = "import_error"
            elif "ULTRA" in line and "400" in context:
                err_type = "ultra_fail"
            elif "ValueError" in context:
                err_type = "value_error"
            elif "KeyError" in context:
                err_type = "key_error"
            elif "Connection" in context or "Timeout" in context or "refused" in context:
                err_type = "connection"
            
            error_types[err_type] = error_types.get(err_type, 0) + 1
            
            errors.append({
                "line": line,
                "type": err_type,
                "context": context,
            })
    
    return {
        "has_errors": len(errors) > 0,
        "total_errors": len(errors),
        "error_types": error_types,
        "recent_errors": errors[-5:],  # Last 5 errors for report
    }

def check_network() -> dict:
    """Check internet connectivity. Returns status dict.
    
    During a network outage, services may report DOWN (ports unreachable)
    but we should NOT restart them — they'll reconnect when network returns.
    """
    network_ok = False
    for host in NETWORK_CHECK_HOSTS:
        try:
            if host in ("8.8.8.8", "1.1.1.1"):
                # ICMP ping for DNS servers
                r = subprocess.run(["ping", "-c", "1", "-t", "2", host],
                                  capture_output=True, timeout=NETWORK_CHECK_TIMEOUT)
                if r.returncode == 0:
                    network_ok = True
                    break
            else:
                # HTTP check for API hosts
                req = urllib.request.Request(f"https://{host}", method="HEAD")
                urllib.request.urlopen(req, timeout=NETWORK_CHECK_TIMEOUT)
                network_ok = True
                break
        except Exception:
            continue
    
    # Track consecutive failures for backoff
    _net_state = check_network.__dict__
    _net_state["consecutive_failures"] = _net_state.get("consecutive_failures", 0) + (0 if network_ok else 1)
    if network_ok:
        _net_state["consecutive_failures"] = 0
    
    return {
        "online": network_ok,
        "consecutive_failures": _net_state["consecutive_failures"],
        "backoff_seconds": NETWORK_RETRY_BACKOFF[min(_net_state["consecutive_failures"], len(NETWORK_RETRY_BACKOFF)-1)] if not network_ok else 0,
    }


def reconnect_all_services():
    """Full reconnect sweep: restart FutuOpenD + ShadowTrader to refresh connections."""
    log("Network restored — performing full reconnect sweep", "RECONNECT")
    
    # 1. Restart FutuOpenD (primary data source — needs fresh connection)
    log("Reconnecting FutuOpenD...", "RECONNECT")
    try:
        subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.futunn.FutuOpenD"],
                      capture_output=True, timeout=15)
        log("FutuOpenD reconnected", "RECONNECT")
    except:
        try:
            subprocess.Popen(["open", "/Applications/Futu_OpenD.app"])
            log("FutuOpenD launched via open", "RECONNECT")
        except:
            log("FutuOpenD reconnect failed — will retry next cycle", "RECONNECT")
    
    time.sleep(5)
    
    # 2. Restart ShadowTrader (gets new data subscriptions)
    log("Reconnecting ShadowTrader...", "RECONNECT")
    try:
        subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.atos.shadowtrader"],
                      capture_output=True, timeout=15)
        log("ShadowTrader reconnected", "RECONNECT")
    except:
        log("ShadowTrader reconnect failed", "RECONNECT")


def compute_health():
    """Full health snapshot — returns dict.
    
    Output fields used by the Hermes cron job prompt.
    """
    # Port checks
    shadow = check_port(19999)
    futu = check_port(11111)
    dash_port_check = check_port(9000)
    dash_http = check_http("http://localhost:9000/api")
    
    # State from file
    state = read_state()
    equity = state.get("equity", 0)
    peak = state.get("peak_equity", equity)
    cash = state.get("cash", 0)
    cycle = state.get("cycle_count", 0)
    positions = state.get("positions", {})
    num_positions = len(positions)
    
    # Drawdown
    dd_pct = ((equity / peak) - 1) * 100 if peak > 0 else 0
    
    # Position details
    pos_details = []
    for sym, info in positions.items():
        if isinstance(info, dict):
            qty = info.get("qty", 0)
            avg = info.get("avg_price", 0)
            last = info.get("last_price", 0)
            pnl = (last - avg) * qty
            pnl_pct = ((last / avg) - 1) * 100 if avg > 0 else 0
            pos_details.append({
                "symbol": sym, "qty": qty, "avg_price": round(avg, 2),
                "last_price": round(last, 2), "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })
    
    # Errors
    err_analysis = analyze_stderr_errors()
    
    # AI health (Ultra failures)
    ultra_fails = err_analysis["error_types"].get("ultra_fail", 0)
    
    # Cycle stalling — detect if cycle hasn't moved
    # After a restart, cycle may reset to 1 — don't flag that as stalled
    prev_health = read_health_state()
    prev_cycle = prev_health.get("last_cycle", 0)
    # If cycle reset (e.g. 516→1 after restart), don't flag as stalled
    cycle_stalled = False
    if prev_cycle > 0 and cycle > 0:
        if cycle == prev_cycle:
            # Same cycle repeated — check if it's been long enough to be a real stall
            last_check = prev_health.get("last_check", "")
            if last_check:
                try:
                    last_dt = datetime.fromisoformat(last_check)
                    stall_minutes = (datetime.now() - last_dt).total_seconds() / 60
                    if stall_minutes > 15:
                        cycle_stalled = True
                except:
                    pass
        elif cycle < prev_cycle and cycle <= 5:
            # Cycle reset after restart — this is normal, don't flag
            cycle_stalled = False
            log(f"Cycle reset detected: #{prev_cycle}→#{cycle} (normal after restart)", "INFO")
    
    return {
        "timestamp": datetime.now().isoformat(),
        "services": {
            "shadow": "running" if shadow else "stopped",
            "futu": "running" if futu else "stopped",
            "dashboard": "running" if dash_http else "stopped",
        },
        "portfolio": {
            "equity": round(equity, 2),
            "peak_equity": round(peak, 2),
            "drawdown_pct": round(dd_pct, 2),
            "cash": round(cash, 2),
            "cycle": cycle,
            "cycle_stalled": cycle_stalled,
            "num_positions": num_positions,
            "positions": pos_details,
        },
        "errors": {
            "total": err_analysis["total_errors"],
            "types": err_analysis["error_types"],
            "recent": err_analysis["recent_errors"],
        },
        "flags": {
            "any_service_down": not (shadow and futu and dash_http),
            "cycle_stalled": cycle_stalled,
            "ultra_fails": ultra_fails,
            "drawdown_alert": dd_pct < DRAWDOWN_ALERT_PCT,
            "high_error_rate": err_analysis["total_errors"] > MAX_RECENT_ERRORS,
            "import_errors": err_analysis["error_types"].get("import_error", 0) > 0,
        },
    }

def needs_deep_fix(health):
    """Determine if this needs Tier 2 (code fix) vs Tier 1 (restart)."""
    flags = health["flags"]
    
    # Code-level bugs → must escalate to Hermes
    if flags["import_errors"]:
        return True, "import_error — code has missing or broken imports"
    if flags["high_error_rate"] and flags["any_service_down"]:
        return True, f"high_error_rate ({health['errors']['total']} errors) + service down — likely code bug"
    
    # Restart-loop detection: if shadow restarted recently
    prev = read_health_state()
    prev_restarts = prev.get("consecutive_restarts", 0)
    if prev_restarts >= RESTART_THRESHOLD:
        return True, f"consecutive_restarts ({prev_restarts}) — restart loop, needs code fix"
    
    return False, ""

def fix_tier1(health):
    """Tier 1: Restart services that are down."""
    fixed = []
    s = health["services"]
    
    if s["shadow"] == "stopped":
        log("ShadowTrader is DOWN — clearing lock + restarting", "FIX")
        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
            subprocess.run(["find", os.path.join(ATOS_DIR, "atos"), "-type", "d",
                           "-name", "__pycache__", "-exec", "rm", "-rf", "{}", "+"],
                          capture_output=True, timeout=10)
            subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.atos.shadowtrader"],
                          capture_output=True, timeout=10)
            fixed.append("shadow")
        except Exception as e:
            log(f"Shadow restart failed: {e}", "ERROR")
    
    if s["futu"] == "stopped":
        log("FutuOpenD is DOWN — restarting", "FIX")
        try:
            subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.futunn.FutuOpenD"],
                          capture_output=True, timeout=10)
            fixed.append("futu")
        except:
            try:
                subprocess.Popen(["open", "/Applications/Futu_OpenD.app"])
                fixed.append("futu")
            except:
                pass
    
    if s["dashboard"] == "stopped":
        log("Dashboard is DOWN — restarting", "FIX")
        try:
            subprocess.run(["launchctl", "kickstart", "-k", "gui/501/ai.atos.dashboard"],
                          capture_output=True, timeout=10)
            fixed.append("dashboard")
        except Exception as e:
            log(f"Dashboard restart failed: {e}", "ERROR")
    
    # Update restart counter
    prev = read_health_state()
    prev_restarts = prev.get("consecutive_restarts", 0)
    if fixed:
        save_health_state({
            **prev,
            "consecutive_restarts": prev_restarts + 1,
            "last_restart_at": datetime.now().isoformat(),
            "restarted_services": fixed,
        })
    
    return fixed

def create_incident_report(health, reason):
    """Write a structured incident JSON that a Hermes cron job can read."""
    inc_id = get_incident_id()
    incident = {
        "incident_id": inc_id,
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
        "severity": "critical",
        "health_snapshot": health,
        "suggested_actions": [],
    }
    
    # Add smart suggestions based on error type
    errs = health["errors"]
    for e in errs.get("recent", []):
        if "import" in e.get("type", ""):
            incident["suggested_actions"].append({
                "type": "fix_import",
                "file": extract_file_from_traceback(e["context"]),
                "error": e["context"][:200],
            })
        elif "ultra" in e.get("type", ""):
            incident["suggested_actions"].append({
                "type": "fix_ultra_api",
                "error": e["context"][:200],
            })
    
    filepath = os.path.join(INCIDENTS_DIR, f"{inc_id}.json")
    with open(filepath, "w") as f:
        json.dump(incident, f, indent=2, default=str)
    
    log(f"Incident report written: {filepath}", "CRITICAL")
    return inc_id

def extract_file_from_traceback(tb):
    """Try to extract the source file from a traceback string."""
    m = re.search(r'File "([^"]+)"', tb)
    return m.group(1) if m else "unknown"


# ── Drawdown Circuit Breaker ───────────────────────────────

def compute_exposure_signal(dd_pct):
    """Compute recommended exposure level based on phased drawdown thresholds.
    
    Returns a dict with:
        - exposure_pct: recommended exposure level (100 = full, 0 = halt)
        - level: which threshold was crossed (or 'normal')
        - triggered: True if any threshold is active
    """
    for threshold_dd, exposure in reversed(DRAWDOWN_LEVELS):
        # DRAWDOWN_LEVELS are negative values (e.g. -5.0)
        # dd_pct is already negative (e.g. -6.2)
        if dd_pct <= threshold_dd:
            return {
                "exposure_pct": exposure,
                "level": f"{threshold_dd}%",
                "triggered": True,
            }
    return {"exposure_pct": 100, "level": "normal", "triggered": False}


def write_exposure_signal(exposure_pct, dd_pct, reason=""):
    """Write exposure signal file that the system can read for position sizing."""
    signal = {
        "timestamp": datetime.now().isoformat(),
        "exposure_pct": exposure_pct,
        "drawdown_pct": round(dd_pct, 2),
        "reason": reason,
    }
    try:
        with open(EXPOSURE_SIGNAL_FILE, "w") as f:
            json.dump(signal, f)
        log(f"Exposure signal written: {exposure_pct}% at DD={dd_pct:.1f}%", "SIGNAL")
    except Exception as e:
        log(f"Failed to write exposure signal: {e}", "ERROR")


def write_emergency_stop(dd_pct):
    """Write emergency stop file to trigger immediate halt."""
    try:
        with open(EMERGENCY_STOP_FILE, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "drawdown_pct": round(dd_pct, 2),
                "reason": f"Emergency stop triggered at {dd_pct:.1f}% drawdown",
            }, f)
        log(f"🚨 EMERGENCY STOP file written at DD={dd_pct:.1f}%", "CRITICAL")
    except Exception as e:
        log(f"Failed to write emergency stop: {e}", "ERROR")


def check_emergency_drawdown(dd_pct):
    """Check if drawdown exceeds emergency threshold and trigger stop."""
    if dd_pct <= EMERGENCY_DRAWDOWN_PCT:
        write_emergency_stop(dd_pct)
        log(f"CRITICAL: Drawdown {dd_pct:.1f}% exceeds emergency threshold {EMERGENCY_DRAWDOWN_PCT:.0f}%", "CRITICAL")
        return True
    return False


# ── Trend Tracking & Performance Metrics ───────────────────

def compute_equity_trend(state):
    """Compute 7-day equity trend slope from equity_history.
    
    Returns dict with slope, direction (up/down/flat), and R².
    """
    ehistory = state.get("equity_history", [])
    if not isinstance(ehistory, list) or len(ehistory) < 2:
        return {"slope": 0.0, "direction": "flat", "r2": 0.0, "n": 0}
    
    # Take up to EQUITY_TREND_DAYS worth of points
    # equity_history is stored chronologically, use last N entries
    # Each entry: {"time": "...", "equity": float}
    n_points = min(len(ehistory), EQUITY_TREND_DAYS * 288)  # ~288 cycles/day
    recent = ehistory[-n_points:]
    
    # Extract values
    values = []
    timestamps = []
    for entry in recent:
        if isinstance(entry, dict) and "equity" in entry:
            eq = entry.get("equity", 0)
            if isinstance(eq, (int, float)) and eq > 0:
                values.append(eq)
                timestamps.append(len(values))  # Use index as x
    
    if len(values) < 2:
        return {"slope": 0.0, "direction": "flat", "r2": 0.0, "n": len(values)}
    
    # Simple linear regression
    n = len(values)
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    
    num = 0.0
    denom = 0.0
    for i, y in enumerate(values):
        xi = i - x_mean
        yi = y - y_mean
        num += xi * yi
        denom += xi * xi
    
    slope = num / denom if denom != 0 else 0.0
    base_price = values[0] if values[0] > 0 else 1.0
    slope_pct = (slope / base_price) * 100  # % per step
    
    # R²
    if denom > 0:
        y_pred = [y_mean + slope * (i - x_mean) for i in range(n)]
        ss_res = sum((values[i] - y_pred[i]) ** 2 for i in range(n))
        ss_tot = sum((v - y_mean) ** 2 for v in values)
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    else:
        r2 = 0.0
    
    direction = "up" if slope_pct > 0.1 else ("down" if slope_pct < -0.1 else "flat")
    
    return {
        "slope": round(slope_pct, 4),
        "direction": direction,
        "r2": round(r2, 4),
        "n": n,
    }


def compute_performance_metrics(state):
    """Compute performance metrics from trade_history.
    
    Returns dict with win_rate, total_trades, avg_win, avg_loss, profit_factor.
    """
    trades = state.get("trade_history", [])
    if not isinstance(trades, list) or not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "net_pnl": 0.0,
        }
    
    # Filter to completed trades (SELL actions with pnl)
    completed = [t for t in trades if isinstance(t, dict) and t.get("action") == "SELL" and t.get("pnl", 0) != 0]
    
    if not completed:
        return {
            "total_trades": len(trades),
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "net_pnl": 0.0,
        }
    
    wins = [t["pnl"] for t in completed if t["pnl"] > 0]
    losses = [t["pnl"] for t in completed if t["pnl"] < 0]
    
    total_trades = len(completed)
    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 1.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
    net_pnl = gross_profit - gross_loss
    
    return {
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "net_pnl": round(net_pnl, 2),
    }

# ── Main Loop ──────────────────────────────────────────────

def main():
    log("ATOS Auto-Monitor v3 starting")
    log(f"Check interval: {CHECK_INTERVAL}s | Drawdown alert: {DRAWDOWN_ALERT_PCT}%")
    
    # Track last exposure level to avoid repeating signals
    _last_exposure_pct = 100
    _last_emergency_warning = False
    
    while True:
        try:
            # ── Network Check ────────────────────────────────
            # Must pass before any service checks — prevents false
            # DOWN alerts and automatic restarts during network outage.
            net = check_network()
            if not net["online"]:
                log(f"NETWORK DOWN ({net['consecutive_failures']}/{MAX_NETWORK_FAILURES}) — "
                    f"all port/health checks skipped. Backoff: {net['backoff_seconds']}s", "WARN")
                
                # If we've been offline too long, write an incident for Hermes
                if net["consecutive_failures"] >= MAX_NETWORK_FAILURES:
                    log("Network has been down for multiple cycles — creating incident", "CRITICAL")
                    dummy_health = compute_health()
                    create_incident_report(dummy_health,
                        f"network_outage — {net['consecutive_failures']} consecutive failures, no Hermes response")
                    time.sleep(net["backoff_seconds"])
                    continue
                
                # Short backoff then retry
                time.sleep(net["backoff_seconds"])
                continue
            # Network is up. If we just recovered from an outage, do a full reconnect
            # We detect this via the state file: if consecutive_failures was > 0 last time
            prev_state = read_health_state()
            was_offline = prev_state.get("consecutive_failures", 0) > 0
            if was_offline:
                log("Network restored after outage — performing reconnect sweep", "RECONNECT")
                reconnect_all_services()
            
            health = compute_health()
            flags = health["flags"]
            
            # Summary line
            s = health["services"]
            p = health["portfolio"]
            dd_pct = p['drawdown_pct']
            log(f"Shadow:{s['shadow']} Futu:{s['futu']} Dash:{s['dashboard']} "
                f"| Cycle #{p['cycle']} | Equity=${p['equity']:,.0f} "
                f"| DD={dd_pct:.1f}% | Errors:{health['errors']['total']}")
            
            # ── Enhanced Drawdown Circuit Breaker ────────────
            state = read_state()
            
            # Compute equity trend
            trend = compute_equity_trend(state)
            if trend['n'] > 0:
                log(f"Trend: {trend['direction']} (slope={trend['slope']:+.4f}%/step, R²={trend['r2']:.3f}, n={trend['n']})")
            
            # Compute performance metrics
            perf = compute_performance_metrics(state)
            if perf['total_trades'] > 0:
                log(f"Perf: {perf['total_trades']} trades | "
                    f"Win={perf['win_rate']:.1%} | "
                    f"AvgWin=${perf['avg_win']:.0f} | "
                    f"AvgLoss=${perf['avg_loss']:.0f} | "
                    f"PF={perf['profit_factor']:.2f} | "
                    f"Net=${perf['net_pnl']:.0f}")
            
            # Phased drawdown response
            exposure = compute_exposure_signal(dd_pct)
            if exposure['triggered']:
                if exposure['exposure_pct'] != _last_exposure_pct:
                    write_exposure_signal(exposure['exposure_pct'], dd_pct,
                                          reason=f"Drawdown at {dd_pct:.1f}% (level={exposure['level']})")
                    _last_exposure_pct = exposure['exposure_pct']
                log(f"DRAWDOWN PHASE {exposure['level']}: reducing to {exposure['exposure_pct']}% exposure",
                    "ALERT")
            
            # Emergency drawdown check (at -10%)
            emergency = check_emergency_drawdown(dd_pct)
            if emergency and not _last_emergency_warning:
                _last_emergency_warning = True
                log(f"🚨 EMERGENCY DRAWDOWN AT {dd_pct:.1f}% — stop file written", "CRITICAL")
                # Force-kill shadow trader via launchctl
                try:
                    subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.atos.shadowtrader"],
                                   capture_output=True, timeout=10)
                    log("ShadowTrader killed via launchctl", "FIX")
                except Exception as e:
                    log(f"Could not kill ShadowTrader: {e}", "ERROR")
            elif not emergency:
                _last_emergency_warning = False
            
            # ── Existing Service Checks ─────────────────────
            if flags["any_service_down"]:
                log(f"SERVICE DOWN: shadow={s['shadow']} futu={s['futu']} dash={s['dashboard']}", "ALERT")
                
                need_deep, reason = needs_deep_fix(health)
                if need_deep:
                    log(f"TIER 2 needed: {reason}", "CRITICAL")
                    inc_id = create_incident_report(health, reason)
                    log(f"Incident {inc_id} — waiting for Hermes to fix code", "CRITICAL")
                    time.sleep(60)  # Give Hermes time to pick up the incident
                else:
                    # Tier 1: just restart
                    fixed = fix_tier1(health)
                    if fixed:
                        log(f"Tier 1: restarted {', '.join(fixed)} — will recheck", "FIX")
                    time.sleep(RESTART_COOLDOWN)
            
            elif flags["drawdown_alert"] and not exposure['triggered']:
                log(f"DRAWDOWN ALERT: {dd_pct:.1f}% from peak={p['peak_equity']:,.0f}", "ALERT")
            
            elif flags["cycle_stalled"]:
                log(f"CYCLE STALLED at #{p['cycle']} — might be stuck", "STALL")
                # Stalled but not crashed: do a soft restart
                log("Soft restarting ShadowTrader (cycle stalled)", "FIX")
                try:
                    subprocess.run(["launchctl", "kickstart", "-k", "gui/501/com.atos.shadowtrader"],
                                  capture_output=True, timeout=10)
                except:
                    pass
            
            elif flags["ultra_fails"] > 0:
                log(f"ULTRA failures detected ({flags['ultra_fails']}) — AI analysis degraded", "WARN")
            
            # Save state for next cycle (include trend & perf for persistence)
            save_health_state({
                "last_cycle": p["cycle"],
                "last_equity": p["equity"],
                "consecutive_restarts": 0,  # Reset: no issues this cycle
                "consecutive_failures": net.get("consecutive_failures", 0),  # Network: 0=online
                "last_check": datetime.now().isoformat(),
                "trend": trend,
                "performance": perf,
                "exposure": exposure,
            })
            
        except Exception as e:
            log(f"Monitor self-error: {e}", "ERROR")
            traceback.print_exc()
        
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
