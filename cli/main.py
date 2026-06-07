import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atos.main import run_backtest
import argparse

parser = argparse.ArgumentParser(description="ATOS PRO 量化回测系统")
parser.add_argument("--ticker", default="TSLA", help="股票代码，例如 TSLA NVDA AAPL")
args = parser.parse_args()
run_backtest(args.ticker)
