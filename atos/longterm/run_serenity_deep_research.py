#!/usr/bin/env python3
"""
ATOS Serenity 深度调研脚本
===========================
- 扫描 AI 供应链标的
- 跑 4问过滤 + 评分
- 二级瓶颈扫描
- 信号雷达检测
- 输出调研报告到 reports/ 目录

用法:
  python3 atos/longterm/run_serenity_deep_research.py
  python3 atos/longterm/run_serenity_deep_research.py --include-deep
  python3 atos/longterm/run_serenity_deep_research.py --symbols NVDA,AMD,MRVL,SIVE
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.logging import get_logger
from atos.longterm.serenity import (
    find_chokepoint_stocks,
    deep_chokepoint_scan,
    signal_radar,
    serenity_quality_filter,
    SUPPLIER_CHAIN_HINTS,
)

logger = get_logger("serenity.research")

# 默认扫描宇宙：AI 供应链核心标的
DEFAULT_UNIVERSE = [
    # 芯片设计
    "NVDA", "AMD", "MRVL", "AVGO", "INTC",
    # 存储
    "MU", "STX", "WDC",
    # 光互联/CPO
    "SIVE", "LITE", "AAOI", "COHR", "LUMN",
    # 设备
    "AMAT", "ASML", "LRCX", "KLAC", "TER", "ENTG",
    # 材料
    "AXTI", "IQE", "SOI", "WOLF", "CCMP",
    # 封装/测试
    "AMKR", "STAT",
    # AI 新贵
    "NBIS", "IREN", "CIFR",
]

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "reports")


def run_deep_research(symbols: list[str], include_deep: bool = True) -> dict:
    """运行完整的 Serenity 深度调研"""

    logger.info(f"🔍 Serenity 深度调研启动 — {len(symbols)} 个标的")

    # 阶段1: 基础瓶颈扫描（含 4问过滤）
    logger.info("阶段1: 瓶颈扫描...")
    primary = find_chokepoint_stocks(symbols)

    # 阶段2: 二级瓶颈扫描（瓶颈中的瓶颈）
    deep = []
    chain_map = {}
    if include_deep:
        logger.info("阶段2: 二级瓶颈扫描...")
        deep_result = deep_chokepoint_scan(symbols)
        deep = deep_result.get("deep", [])
        chain_map = deep_result.get("chain_map", {})

    # 阶段3: 信号雷达
    logger.info("阶段3: 信号雷达...")
    radar = signal_radar(symbols)

    # 阶段4: 质量过滤（供因子引擎用）
    logger.info("阶段4: 质量过滤...")
    quality = serenity_quality_filter(symbols)

    # 统计
    strong = [c for c in primary if c["decision"] == "STRONG_CHOKEPOINT"]
    watch = [c for c in primary if c["decision"] == "CHOKEPOINT_WATCH"]
    hot = [s for s in radar["heat_map"].items() if s[1] == "HOT"]
    hot_symbols = [h[0] for h in hot]

    return {
        "timestamp": datetime.now().isoformat(),
        "symbols_scanned": len(symbols),
        "primary_chokepoints": primary,
        "strong_chokepoints": strong,
        "watch_chokepoints": watch,
        "deep_chokepoints": deep,
        "supply_chain_map": chain_map,
        "signals": radar["signals"],
        "heat_map": radar["heat_map"],
        "quality_scores": quality,
        "summary": {
            "total_scanned": len(symbols),
            "strong_count": len(strong),
            "watch_count": len(watch),
            "deep_count": len(deep),
            "hot_signals": len(hot_symbols),
            "top_5_strong": [c["symbol"] for c in strong[:5]],
            "top_5_hot": hot_symbols[:5],
        },
    }


def generate_report(result: dict) -> str:
    """生成可读的调研报告文本"""
    s = result["summary"]
    lines = []
    lines.append("=" * 60)
    lines.append(f"SERENITY 深度调研报告")
    lines.append(f"时间: {result['timestamp']}")
    lines.append("=" * 60)
    lines.append("")

    # 摘要
    lines.append(f"📊 摘要")
    lines.append(f"  扫描标的: {s['total_scanned']} 只")
    lines.append(f"  强力瓶颈: {s['strong_count']} 只")
    lines.append(f"  观察列表: {s['watch_count']} 只")
    lines.append(f"  二级瓶颈: {s['deep_count']} 只")
    lines.append(f"  热度信号: {s['hot_signals']} 个")
    lines.append("")

    # 强力瓶颈
    if result["strong_chokepoints"]:
        lines.append("🔥 强力瓶颈（STRONG_CHOKEPOINT）")
        lines.append("-" * 40)
        for c in result["strong_chokepoints"][:8]:
            fq = c.get("four_question", {})
            conf = fq.get("confidence", "N/A")
            lines.append(
                f"  {c['symbol']:6s} | 评分:{c['serenity_score']:2d} | "
                f"市值:${c['market_cap_m']:.0f}M | "
                f"做空:{c.get('short_pct',0)*100 if c.get('short_pct') else 0:.1f}% | "
                f"置信度:{conf}"
            )
        lines.append("")

    # 二级瓶颈
    if result["deep_chokepoints"]:
        lines.append("🔬 二级瓶颈（瓶颈中的瓶颈）")
        lines.append("-" * 40)
        for c in result["deep_chokepoints"][:5]:
            lines.append(
                f"  {c['symbol']:6s} | 评分:{c['serenity_score']:2d} | "
                f"市值:${c['market_cap_m']:.0f}M"
            )
        lines.append("")

    # 供应链映射
    if result["supply_chain_map"]:
        lines.append("🔗 供应链上下游关系")
        lines.append("-" * 40)
        for buyer, suppliers in result["supply_chain_map"].items():
            lines.append(f"  {buyer} → {', '.join(suppliers)}")
        lines.append("")

    # 信号雷达
    high_signals = [s for s in result["signals"] if s["intensity"] == "HIGH"]
    if high_signals:
        lines.append("🚨 高优先级信号")
        lines.append("-" * 40)
        for s in high_signals[:10]:
            change_str = f" [{s.get('change', 'N/A')}]" if s.get('change') else ""
            lines.append(f"  {s['symbol']:6s} | {s['signal_type']:20s} | {s['description']}{change_str}")
        lines.append("")

    # 热度图
    hot_items = [k for k, v in result["heat_map"].items() if v == "HOT"]
    warm_items = [k for k, v in result["heat_map"].items() if v == "WARM"]
    if hot_items or warm_items:
        lines.append("🌡️  热度图")
        lines.append("-" * 40)
        if hot_items:
            lines.append(f"  🔥 HOT:  {', '.join(hot_items)}")
        if warm_items:
            lines.append(f"  ⚡ WARM: {', '.join(warm_items)}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="ATOS Serenity 深度调研")
    parser.add_argument("--symbols", help="自定义标的列表（逗号分隔）")
    parser.add_argument("--include-deep", action="store_true", default=True,
                        help="包含二级瓶颈扫描")
    parser.add_argument("--output", choices=["json", "report", "both"], default="report",
                        help="输出格式")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else DEFAULT_UNIVERSE

    result = run_deep_research(symbols, include_deep=args.include_deep)
    report = generate_report(result)

    # 保存报告
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.output in ("json", "both"):
        json_path = os.path.join(REPORT_DIR, f"serenity_research_{ts}.json")
        with open(json_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"📄 JSON 报告: {json_path}")

    if args.output in ("report", "both"):
        md_path = os.path.join(REPORT_DIR, f"serenity_research_{ts}.md")
        with open(md_path, "w") as f:
            f.write(report)
        print(f"📄 报告: {md_path}")
        print()
        print(report)

    logger.info(f"调研完成: {result['summary']['strong_count']}强/{result['summary']['watch_count']}观/{result['summary']['deep_count']}深")


if __name__ == "__main__":
    main()
