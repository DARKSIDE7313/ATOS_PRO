"""导入冒烟: 关键模块可正常 import (捕获 NameError 类回归, 见 Pattern 33/53)。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atos.shadow.strategy_v28 as s
import atos.core.universe as u
import atos.shadow.risk_loop  # noqa
import atos.live.risk_manager  # noqa
assert s.V28_ALPHA_UNIVERSE
print("import OK:", len(s.V28_ALPHA_UNIVERSE), "alpha /", len(u.ALL_SYMBOLS), "universe")
