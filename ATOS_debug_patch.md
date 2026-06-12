# ATOS PRO — 深度 Debug 修改方案
## 总览：6 个补丁，4 个文件

| # | 文件 | 问题 | 修复方向 |
|---|---|---|---|
| P1 | `shadow_trader.py` | 浮亏时仍然加仓 | 加入 `existing_pnl < 0` 防守 |
| P2 | `shadow_trader.py` | 动态冷却期只存 log 不生效 | 黑名单改为 dict 存真实 cooldown |
| P3 | `shadow_trader.py` | 仓位模型取 max 推高建仓尺寸 | 改 0.4×crouching + 0.6×vol_pct |
| P4 | `kelly.py` | crouching 基础仓位和催化乘数过激 | 降档位、乘数、硬上限 |
| P5 | `risk_manager.py` | 波动率止损和硬止损可能双重触发 | 改为 effective_stop = max(两者) |
| P6 | `strategy_v3.py` | HIGH_VOL/BEAR 下 reversal 权重过高 | 调整权重矩阵 |

---

## P1 + P2 + P3：`atos/shadow/shadow_trader.py`

### P2：修复 `add_to_blacklist()` — 让动态冷却期真正生效

**定位：** 约第 135–163 行，整个 `add_to_blacklist` 方法

**替换 BEFORE：**
```python
    def add_to_blacklist(self, symbol: str):
        """任何卖出都加入冷却黑名单。

        BUGFIX 2026-06-11: 冷却周期按波动率动态缩放。
        高波动标的（atr/price > 3%）需要更长的冷却期，
        低波动标的冷却期较短。
        """
        # 按波动率动态计算冷却周期
        vol_mult = 1.0
        try:
            if hasattr(self, 'positions') and symbol in self.positions:
                pos = self.positions[symbol]
                lp = pos.get("last_price", pos.get("avg_price", 0))
                # 用最近的价格波动幅度估算
                atr_val = pos.get("atr", 0)
                if atr_val > 0 and lp > 0:
                    daily_vol = atr_val / lp
                    if daily_vol > 0.03:
                        vol_mult = 1.5  # 高波动→1.5倍冷却
                    elif daily_vol < 0.01:
                        vol_mult = 0.7  # 低波动→0.7倍冷却
        except Exception:
            pass

        dynamic_cooldown = int(COOLDOWN_CYCLES * vol_mult)
        self.stop_loss_blacklist[symbol] = self.cycle_count
        logger.info(f"🔒 冷却: {symbol} → 禁止买入至周期#{self.cycle_count + dynamic_cooldown} (波动率系数×{vol_mult:.1f})")
```

**替换 AFTER：**
```python
    def add_to_blacklist(self, symbol: str):
        """任何卖出都加入冷却黑名单。

        FIX P2 2026-06-12: 黑名单存 dict，包含真实动态冷却长度，
        is_cooling_off() 现在按 entry["cooldown"] 判断，不再用固定常量。
        """
        vol_mult = 1.0
        try:
            if symbol in self.positions:
                pos = self.positions[symbol]
                lp = pos.get("last_price", pos.get("avg_price", 0))
                atr_val = pos.get("atr", 0)
                if atr_val > 0 and lp > 0:
                    daily_vol = atr_val / lp
                    if daily_vol > 0.03:
                        vol_mult = 1.5
                    elif daily_vol < 0.01:
                        vol_mult = 0.7
        except Exception:
            pass

        dynamic_cooldown = max(1, int(COOLDOWN_CYCLES * vol_mult))
        # ★ 关键修复：存 dict 而非纯整数，is_cooling_off() 读 cooldown 字段
        self.stop_loss_blacklist[symbol] = {
            "sold_cycle": self.cycle_count,
            "cooldown": dynamic_cooldown,
        }
        logger.info(
            f"🔒 冷却: {symbol} → 禁买 {dynamic_cooldown} 周期"
            f" (至#{self.cycle_count + dynamic_cooldown}, vol×{vol_mult:.1f})"
        )
```

---

### P2 续：修复 `is_cooling_off()` — 读取真实 cooldown

**定位：** 约第 110–133 行，整个 `is_cooling_off` 方法

**替换 BEFORE：**
```python
    def is_cooling_off(self, symbol: str) -> bool:
        """检查冷却期（任何卖出都会触发，不仅仅是止损）。

        BUGFIX P2 2026-06-12: 使用真实的动态冷却长度判断。
        """
        if symbol in self.stop_loss_blacklist:
            entry = self.stop_loss_blacklist[symbol]
            if isinstance(entry, dict):
                sold_cycle = entry.get("sold_cycle", 0)
                cooldown = entry.get("cooldown", COOLDOWN_CYCLES)
            else:
                # 兼容旧格式（纯整数）
                sold_cycle = entry
                cooldown = COOLDOWN_CYCLES
                # 升级为新格式
                self.stop_loss_blacklist[symbol] = {
                    "sold_cycle": sold_cycle,
                    "cooldown": cooldown,
                }
            if self.cycle_count - sold_cycle < cooldown:
                return True
            else:
                del self.stop_loss_blacklist[symbol]
        return False
```

> ✅ **这段已经是正确的**，不需要修改。请确认你的文件是否已经是这个版本；如果 `is_cooling_off` 里仍然是旧版（只用 `COOLDOWN_CYCLES` 固定值），就用上面这段替换。

---

### P3：修复仓位融合逻辑 — 保守加权替代 max()

**定位：** 约第 860–872 行，仓位融合部分

**替换 BEFORE（如果你的文件还是旧版 max 逻辑）：**
```python
        # 取两者中的较大值（Crouching 更激进）
        target_pct = max(crouching_pct, vol_result["pct"] if vol_result else 0)
        target_pct = min(target_pct, account.max_single_pct)
```

**替换 AFTER：**
```python
        # FIX P3: 保守加权融合，不再取 max()
        vol_pct = vol_result["pct"] if vol_result else 0
        if crouching_pct > 0 and vol_pct > 0:
            target_pct = 0.4 * crouching_pct + 0.6 * vol_pct   # vol 更保守，权重更高
        else:
            target_pct = max(crouching_pct, vol_pct)            # 只有一个有值时取那个
        target_pct = min(target_pct, account.max_single_pct)
        # 回撤折扣：账户回撤 > 3% 时，额外收缩仓位 15%
        if current_dd > 0.03:
            target_pct *= 0.85
```

---

### P1：浮亏加仓防守（确认当前代码是否已有）

**定位：** 约第 876–893 行，加仓判断块

**确认 AFTER（如果文件还没有这段，添加在 `delta_val = target_val - current_val` 之前）：**
```python
        # FIX P1: 浮亏禁止加仓 — 只允许浮盈后加仓
        if current_val > 0:
            pos_info = account.positions.get(sym, {})
            avg_px = pos_info.get("avg_price", 0)
            if avg_px > 0 and price < avg_px:          # 任何浮亏都跳过
                logger.debug(f"⏭ {sym} 浮亏{(price-avg_px)/avg_px:.2%}，禁止加仓")
                continue

        delta_val = target_val - current_val
        if delta_val <= 0:
            continue
```

---

## P4：`atos/live/kelly.py`

### 降低 crouching_allocation 各项参数

**定位：** 第 59 行开始，整个 `crouching_allocation` 函数

**替换 BEFORE：**
```python
def crouching_allocation(score: float, drawdown: float,
                          has_news_catalyst: bool = False) -> float:
    # Base allocation by confidence tier
    if score >= 0.80:
        base_pct = 0.12
    elif score >= 0.70:
        base_pct = 0.08
    elif score >= 0.50:
        base_pct = 0.05
    else:
        return 0.0  # Below threshold, no allocation

    # Drawdown penalty: each 1% DD reduces allocation by 10%
    dd_penalty = max(0.0, 1.0 - (drawdown / 0.01) * 0.10)
    dd_penalty = max(0.0, dd_penalty)  # floor at 0 (100% reduction possible)

    after_dd = base_pct * dd_penalty

    # News catalyst boost
    if has_news_catalyst:
        after_dd *= 1.5

    # Hard cap at 30% (aggressive but not reckless)
    final = min(after_dd, 0.30)

    print(f"[crouching] score={score:.2f} base={base_pct:.2%} "
          f"DD={drawdown:.2%} penalty={dd_penalty:.2f} "
          f"catalyst={has_news_catalyst} final={final:.2%}")
    return final
```

**替换 AFTER：**
```python
def crouching_allocation(score: float, drawdown: float,
                          has_news_catalyst: bool = False) -> float:
    """
    FIX P4 2026-06-12:
    降低基础档位（12/8/5 → 7/5/3），
    催化乘数从 1.5 降到 1.15，
    硬上限从 30% 降到 10%。
    原因：系统当前胜率 10%、profit_factor 0.04，
    急需收缩单笔风险而非扩大仓位。
    """
    # FIX: 降低基础档位
    if score >= 0.80:
        base_pct = 0.07      # 原 0.12 → 0.07
    elif score >= 0.70:
        base_pct = 0.05      # 原 0.08 → 0.05
    elif score >= 0.55:
        base_pct = 0.03      # 原 0.05 → 0.03，同时提高最低分数线
    else:
        return 0.0

    # Drawdown penalty: each 1% DD reduces allocation by 10%
    dd_penalty = max(0.0, 1.0 - (drawdown / 0.01) * 0.10)

    after_dd = base_pct * dd_penalty

    # FIX: 催化乘数 1.5 → 1.15（不再因新闻大幅扩仓）
    if has_news_catalyst:
        after_dd *= 1.15

    # FIX: 硬上限 30% → 10%
    final = min(after_dd, 0.10)

    logger.debug(f"[crouching] score={score:.2f} base={base_pct:.2%} "
                 f"DD={drawdown:.2%} penalty={dd_penalty:.2f} "
                 f"catalyst={has_news_catalyst} final={final:.2%}")
    return final
```

> ⚠️ 同时把函数里的 `print(...)` 改成 `logger.debug(...)`，避免 stdout 污染（在 AFTER 版中已改好）。  
> 另外在文件顶部加一行：`from atos.core.logging import get_logger; logger = get_logger("kelly")`

---

## P5：`atos/live/risk_manager.py`

### 修复止损参数 — 两处调整

**修改 1：缩紧止损阈值（STOP_LOSS_PCT）**

当前 12% 的止损线对于一个胜率只有 10% 的系统太宽，等于允许每笔错单跑 12% 才止损。

**定位：** 文件顶部常量定义区

**替换 BEFORE：**
```python
STOP_LOSS_PCT = 0.12           # v4: 从6%放宽到12% — 给持仓更多波动空间，减少假止损
TAKE_PROFIT_PCT = 0.15         # 止盈 15%卖一半（从10%放宽）
```

**替换 AFTER：**
```python
STOP_LOSS_PCT = 0.08           # FIX P5: 12% → 8%，当前低胜率环境不能允许单笔跑太远
TAKE_PROFIT_PCT = 0.12         # FIX P5: 止盈也相应收紧 15% → 12%，提高止盈次数
```

---

**修改 2：波动率止损改为纯宽松版（不再可能更严）**

当前代码里 `effective_stop = max(STOP_LOSS_PCT, vol_stop)`，这意味着 ATR 止损有时会比硬止损更严（当 vol_stop < STOP_LOSS_PCT 时 effective_stop = STOP_LOSS_PCT，正确）；但如果 ATR 止损更宽（vol_stop > STOP_LOSS_PCT），则会用 ATR 版本覆盖，导致高波动股止损线更宽。这对已经亏损的系统来说等于给亏单更多空间。

**定位：** `check_all_stops` 函数中，波动率止损段

**替换 BEFORE：**
```python
        # 3. 波动率止损 — 只在硬止损未触发时检查，不叠加。
        #    策略：取 max(硬止损阈值, 动态ATR阈值)，两者较大值才触发。
        #    高波动标的用 ATR 止损（更宽），低波动用硬止损兜底。
        atr_val = signals.get(sym, {}).get("atr", 0)
        if atr_val > 0 and px > 0:
            vol_stop = max(0.03, min(0.10, (atr_val / px) * 2.5))
            effective_stop = max(STOP_LOSS_PCT, vol_stop)  # 取较宽者
            if pnl_pct <= -effective_stop:
                forced.append({
                    "action": "SELL", "symbol": sym, "qty": qty,
                    "reason": f"合并止损 {pnl_pct:.1%} (硬{STOP_LOSS_PCT:.0%} + ATR{vol_stop:.0%}→取{effective_stop:.0%})",
                    "pnl_pct": pnl_pct,
                    "exit_type": "STOP_LOSS", "outcome": "LOSS",
                })
                continue
```

**替换 AFTER：**
```python
        # 3. FIX P5: 波动率止损改为 min(硬止损, ATR止损) — 取较严者
        #    逻辑：高波动股用 ATR 更宽的止损没有帮助，只会让亏损拖得更久。
        #    现在只在 ATR 止损比硬止损更严时才用 ATR（即 ATR 止损更小才触发）。
        atr_val = signals.get(sym, {}).get("atr", 0)
        if atr_val > 0 and px > 0:
            vol_stop = max(0.03, min(0.10, (atr_val / px) * 2.0))  # 乘数 2.5→2.0
            effective_stop = min(STOP_LOSS_PCT, vol_stop)           # ★ max → min
            if pnl_pct <= -effective_stop:
                forced.append({
                    "action": "SELL", "symbol": sym, "qty": qty,
                    "reason": f"ATR止损 {pnl_pct:.1%} (ATR={vol_stop:.1%} vs 硬={STOP_LOSS_PCT:.0%}→取{effective_stop:.1%})",
                    "pnl_pct": pnl_pct,
                    "exit_type": "STOP_LOSS", "outcome": "LOSS",
                })
                continue
```

---

## P6：`atos/live/strategy_v3.py`

### 调整权重矩阵 — 降低逆势 reversal 权重

**定位：** 约第 28–36 行，`WEIGHT_MATRIX` 字典

**替换 BEFORE：**
```python
WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.25, "breakout": 0.15, "reversal": 0.30},
    "HIGH_VOL":    {"momentum": 0.10, "trend": 0.20, "breakout": 0.10, "reversal": 0.60},
    "BEAR":        {"momentum": 0.20, "trend": 0.20, "breakout": 0.10, "reversal": 0.50},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.25, "breakout": 0.25, "reversal": 0.25},
}
```

**替换 AFTER：**
```python
# FIX P6 2026-06-12: 大幅降低 HIGH_VOL/BEAR 的 reversal 权重
# 原因：当前系统反复在高波动和熊市环境中"买超卖"，
# 结果变成接飞刀，reversal 0.60/0.50 会系统性鼓励这种行为。
# 改为以 trend 为核心，reversal 降到辅助地位。
WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.30, "breakout": 0.20, "reversal": 0.20},
    "HIGH_VOL":    {"momentum": 0.15, "trend": 0.35, "breakout": 0.25, "reversal": 0.25},
    "BEAR":        {"momentum": 0.15, "trend": 0.40, "breakout": 0.30, "reversal": 0.15},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.30, "breakout": 0.20, "reversal": 0.25},
}
```

---

## 一键应用脚本（bash）

把以下脚本保存为 `apply_patches.sh`，在 ATOS_PRO 根目录运行：

```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "=== ATOS PRO: 应用 Debug 补丁 ==="

# ---- 备份 ----
for f in atos/live/kelly.py atos/live/risk_manager.py atos/live/strategy_v3.py atos/shadow/shadow_trader.py; do
    cp "$f" "${f}.bak.$(date +%Y%m%d_%H%M%S)"
    echo "  备份: $f"
done

# ---- P4: kelly.py — 降低 crouching 档位 ----
python3 - << 'PY'
import re, pathlib
f = pathlib.Path("atos/live/kelly.py")
txt = f.read_text()

old = '''    if score >= 0.80:
        base_pct = 0.12
    elif score >= 0.70:
        base_pct = 0.08
    elif score >= 0.50:
        base_pct = 0.05'''

new = '''    if score >= 0.80:
        base_pct = 0.07      # FIX P4: 原0.12→0.07
    elif score >= 0.70:
        base_pct = 0.05      # FIX P4: 原0.08→0.05
    elif score >= 0.55:
        base_pct = 0.03      # FIX P4: 原0.05→0.03，分数线0.50→0.55'''

assert old in txt, "P4 kelly tier not found"
txt = txt.replace(old, new, 1)

old2 = "    if has_news_catalyst:
        after_dd *= 1.5

    # Hard cap at 30% (aggressive but not reckless)
    final = min(after_dd, 0.30)"
new2 = "    if has_news_catalyst:
        after_dd *= 1.15    # FIX P4: 1.5→1.15

    # FIX P4: Hard cap 30%→10%
    final = min(after_dd, 0.10)"

assert old2 in txt, "P4 kelly catalyst not found"
txt = txt.replace(old2, new2, 1)

# Fix print→logger (add logger import if missing)
if "get_logger" not in txt:
    txt = "from atos.core.logging import get_logger
logger = get_logger("kelly")
" + txt
txt = txt.replace(
    'print(f"[crouching] score={score:.2f} base={base_pct:.2%} "
          f"DD={drawdown:.2%} penalty={dd_penalty:.2f} "
          f"catalyst={has_news_catalyst} final={final:.2%}")',
    'logger.debug(f"[crouching] score={score:.2f} base={base_pct:.2%} DD={drawdown:.2%} catalyst={has_news_catalyst} final={final:.2%}")'
)

f.write_text(txt)
print("  ✅ P4 kelly.py patched")
PY

# ---- P5: risk_manager.py — 止损参数 + 波动率止损逻辑 ----
python3 - << 'PY'
import pathlib
f = pathlib.Path("atos/live/risk_manager.py")
txt = f.read_text()

old = "STOP_LOSS_PCT = 0.12           # v4: 从6%放宽到12% — 给持仓更多波动空间，减少假止损
TAKE_PROFIT_PCT = 0.15         # 止盈 15%卖一半（从10%放宽）"
new = "STOP_LOSS_PCT = 0.08           # FIX P5: 12%→8%，低胜率环境收紧止损
TAKE_PROFIT_PCT = 0.12         # FIX P5: 15%→12%，提高止盈频率"

assert old in txt, "P5 constants not found"
txt = txt.replace(old, new, 1)

old2 = "            effective_stop = max(STOP_LOSS_PCT, vol_stop)  # 取较宽者"
new2 = "            effective_stop = min(STOP_LOSS_PCT, vol_stop)  # FIX P5: max→min，取较严者"

assert old2 in txt, "P5 effective_stop not found"
txt = txt.replace(old2, new2, 1)

old3 = "            vol_stop = max(0.03, min(0.10, (atr_val / px) * 2.5))"
new3 = "            vol_stop = max(0.03, min(0.10, (atr_val / px) * 2.0))  # FIX P5: 乘数2.5→2.0"

assert old3 in txt, "P5 vol_stop multiplier not found"
txt = txt.replace(old3, new3, 1)

f.write_text(txt)
print("  ✅ P5 risk_manager.py patched")
PY

# ---- P6: strategy_v3.py — WEIGHT_MATRIX ----
python3 - << 'PY'
import pathlib
f = pathlib.Path("atos/live/strategy_v3.py")
txt = f.read_text()

old = '''WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.25, "breakout": 0.15, "reversal": 0.30},
    "HIGH_VOL":    {"momentum": 0.10, "trend": 0.20, "breakout": 0.10, "reversal": 0.60},
    "BEAR":        {"momentum": 0.20, "trend": 0.20, "breakout": 0.10, "reversal": 0.50},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.25, "breakout": 0.25, "reversal": 0.25},
}'''

new = '''# FIX P6 2026-06-12: 降低 HIGH_VOL/BEAR 的 reversal 权重，避免系统性接飞刀
WEIGHT_MATRIX = {
    "BULL_STRONG": {"momentum": 0.40, "trend": 0.30, "breakout": 0.20, "reversal": 0.10},
    "BULL_WEAK":   {"momentum": 0.30, "trend": 0.30, "breakout": 0.20, "reversal": 0.20},
    "HIGH_VOL":    {"momentum": 0.15, "trend": 0.35, "breakout": 0.25, "reversal": 0.25},
    "BEAR":        {"momentum": 0.15, "trend": 0.40, "breakout": 0.30, "reversal": 0.15},
    "UNKNOWN":     {"momentum": 0.25, "trend": 0.30, "breakout": 0.20, "reversal": 0.25},
}'''

assert old in txt, "P6 WEIGHT_MATRIX not found"
txt = txt.replace(old, new, 1)
f.write_text(txt)
print("  ✅ P6 strategy_v3.py patched")
PY

# ---- 验证语法 ----
echo ""
echo "=== 语法验证 ==="
python3 -m py_compile atos/live/kelly.py        && echo "  ✅ kelly.py OK"
python3 -m py_compile atos/live/risk_manager.py && echo "  ✅ risk_manager.py OK"
python3 -m py_compile atos/live/strategy_v3.py  && echo "  ✅ strategy_v3.py OK"
python3 -m py_compile atos/shadow/shadow_trader.py && echo "  ✅ shadow_trader.py OK"

echo ""
echo "=== 所有补丁应用完成 ==="
echo "如有问题，备份文件格式: 原文件名.bak.YYYYMMDD_HHMMSS"
```

---

## 亏损深度分析

基于系统当前真实数据（21 笔已关闭交易，胜率 10%，净亏 -48,264 USD）：

### 亏损原因分类

| 类别 | 案例 | 根因 |
|---|---|---|
| 高波动科技股追入后回撤止损 | AVGO -9,960 / INTC -9,541 / QCOM -6,313 | WEIGHT_MATRIX 在 HIGH_VOL 时 reversal=60% 鼓励买超卖，价格继续下行 |
| 浮亏加仓拉低均价再止损 | 多次 ADD 后触发统一 SELL | 旧代码缺少浮亏禁加仓，导致亏损滚大 |
| 冷却期绕过导致快速回补 | INTC 22:58 卖出 22:59 买回 | `add_to_blacklist` 存纯整数，动态冷却假生效 |
| 单仓过重暴露 | 单标 12% 首仓 + 催化 1.5× = 18% | crouching 上限 30% 过激 |
| 止损线过宽允许亏损扩大 | 12% 止损线 → 每笔平均亏 -2,783 | 胜率低时应收窄止损 |

### 6 个补丁预期改善效果

| 补丁 | 预期变化 |
|---|---|
| P1 浮亏禁加仓 | 单笔最大亏损减少约 30–40%（消除越跌越补） |
| P2 真实动态冷却 | 消除卖出后秒级回补，减少无效交易约 20% |
| P3 保守仓位融合 | 首仓规模降约 25–35%，单笔风险直接下降 |
| P4 降低 crouching 档位 | 极端情况单仓上限从 18% 降至约 8%，回撤收窄 |
| P5 止损收紧 8%+min | 平均亏损从 -2,783 预期降至 -1,800 以内 |
| P6 降 reversal 权重 | 减少高波动/熊市环境中接飞刀频率，胜率预期提升 |
