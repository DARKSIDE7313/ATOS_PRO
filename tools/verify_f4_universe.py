"""F4 落地校验: v28 alpha 宇宙必须全部可被信号引擎取数 (在 ALL_SYMBOLS 中)。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atos.core.universe import ALL_SYMBOLS
from atos.shadow.strategy_v28 import V28_ALPHA_UNIVERSE, V28_CORE_SYMBOL, _trend_derisk_scale, V28_TREND_DERISK_SCALE
from atos.core.position_schema import get_qty  # noqa

missing = [s for s in V28_ALPHA_UNIVERSE if s not in ALL_SYMBOLS]
core_missing = V28_CORE_SYMBOL not in ALL_SYMBOLS
print(f"alpha 宇宙: {len(V28_ALPHA_UNIVERSE)} 只, ALL_SYMBOLS: {len(ALL_SYMBOLS)} 只")
print(f"缺失(信号引擎不会取数): {missing}")
print(f"核心 {V28_CORE_SYMBOL} 缺失: {core_missing}")
print(f"趋势去险系数 = {V28_TREND_DERISK_SCALE} (QQQ<MA200时)")
assert not missing, f"这些 v28 标的不会被取数, 宇宙扩展静默失效: {missing}"
assert not core_missing
print("OK: v28 全宇宙均可取数 ✅")
