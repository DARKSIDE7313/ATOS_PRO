import sys, os, socket, subprocess
OK   = "OK  "
FAIL = "FAIL"
WARN = "WARN"
print("\n=== ATOS PRO Environment Check ===\n")
v = sys.version_info
st = OK if v.major == 3 and v.minor >= 9 else WARN
print(f"[{st}] Python {v.major}.{v.minor}.{v.micro}")
for pkg in ["futu", "yfinance", "pandas"]:
    try:
        __import__(pkg)
        print(f"[{OK}] {pkg} installed")
    except ImportError:
        print(f"[{FAIL}] {pkg} NOT installed  ->  pip install {pkg}")
try:
    r = subprocess.run(["pgrep", "-l", "FutuOpenD"], capture_output=True, text=True)
    if r.stdout.strip():
        print(f"[{OK}] FutuOpenD running")
    else:
        print(f"[{FAIL}] FutuOpenD NOT running  ->  open -a FutuOpenD")
except Exception:
    print(f"[{WARN}] Cannot check FutuOpenD")
try:
    s = socket.create_connection(("127.0.0.1", 11111), timeout=2)
    s.close()
    print(f"[{OK}] OpenD port 11111 reachable")
except Exception:
    print(f"[{FAIL}] OpenD port 11111 NOT reachable  ->  confirm OpenD is logged in")
eu = os.environ.get("ATOS_EMAIL_USER", "")
ep = os.environ.get("ATOS_EMAIL_PASS", "")
if eu and ep:
    print(f"[{OK}] Email credentials set ({eu})")
else:
    print(f"[{WARN}] Email credentials not set  ->  export ATOS_EMAIL_USER=... ATOS_EMAIL_PASS=...")
files = [
    "~/ATOS_PRO/atos/reporting/daily_report.py",
    "~/ATOS_PRO/atos/futu_trader.py",
    "~/ATOS_PRO/atos/market/regime/regime_engine.py",
    "~/ATOS_PRO/atos/main.py",
]
print()
for f in files:
    path = os.path.expanduser(f)
    if os.path.exists(path):
        size = os.path.getsize(path)
        tag = OK if size > 200 else FAIL
        print(f"[{tag}] {f}  ({size} bytes)")
    else:
        print(f"[{FAIL}] {f}  (missing)")
print("\n=== Done ===\n")
