#!/usr/bin/env python3
"""
ATOS Serenity 信号雷达监控（cron 专用）
=========================================
每 N 分钟检查一次持仓标的和候选标的的信号变化。
记录做空比例变化、成交量异常、热度变化。

用法:
  python3 atos/longterm/run_signal_radar.py              # 一次运行
  python3 atos/longterm/run_signal_radar.py --watch       # 监控模式（每10分钟）
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.logging import get_logger
from atos.longterm.serenity import signal_radar

logger = get_logger("serenity.radar")

# 监控标的池
WATCH_UNIVERSE = [
    "SIVE", "LITE", "AAOI", "AXTI", "IQE", "SOI",  # 光互联/CPO
    "MRVL", "NVDA", "AMD",                          # 芯片
    "MU", "STX",                                     # 存储
    "NBIS", "IREN",                                  # AI 新贵
    "AMAT", "LRCX", "KLAC", "ENTG",                 # 设备
    "WOLF", "CCMP",                                  # 材料
]

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state")
RADAR_STATE_FILE = os.path.join(STATE_DIR, "serenity_radar_state.json")
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "reports")


def load_previous_state() -> dict:
    """加载上一次雷达扫描的状态"""
    try:
        if os.path.exists(RADAR_STATE_FILE):
            with open(RADAR_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_state(state: dict):
    """保存雷达状态"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(RADAR_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def run_radar():
    """运行一次雷达扫描"""
    previous = load_previous_state()
    previous_short_data = previous.get("short_data", {})

    # 构建 previous_state（signal_radar 需要的格式）
    prev_for_radar = {}
    for sym, data in previous_short_data.items():
        prev_for_radar[sym] = {"short_pct": data}

    result = signal_radar(WATCH_UNIVERSE, previous_state=prev_for_radar)
    signals = result["signals"]
    heat_map = result["heat_map"]

    # 提取做空比例（存到 state）
    short_data = {}
    for s in signals:
        if s["signal_type"] == "short_interest":
            short_data[s["symbol"]] = s["short_pct"]

    # 保存新状态
    save_state({
        "timestamp": datetime.now().isoformat(),
        "short_data": short_data,
        "previous_short_data": previous_short_data,
    })

    # 生成报告
    hot_count = len([k for k, v in heat_map.items() if v == "HOT"])
    high_signals = [s for s in signals if s["intensity"] == "HIGH"]

    report_lines = []
    report_lines.append(f"🔍 Serenity 雷达扫描 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    report_lines.append(f"   信号: {len(signals)} | HOT: {hot_count} | HIGH: {len(high_signals)}")
    if high_signals:
        for s in high_signals[:5]:
            change_str = f" [{s.get('change', 'N/A')}]" if s.get('change') else ""
            report_lines.append(f"   🚨 {s['symbol']}: {s['description']}{change_str}")
    if hot_count > 0:
        hot_symbols = [k for k, v in heat_map.items() if v == "HOT"]
        report_lines.append(f"   🔥 HOT: {', '.join(hot_symbols)}")

    report = "\n".join(report_lines)

    # 保存到文件
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"serenity_radar_{datetime.now().strftime('%Y%m%d_%H%M')}.md")
    with open(report_path, "w") as f:
        f.write(report)

    logger.info(f"雷达完成: {len(signals)}信号, {hot_count}HOT")
    return report


if __name__ == "__main__":
    report = run_radar()
    print(report)
