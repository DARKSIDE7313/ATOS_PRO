"""
ATOS AutoPilot — AI 自动诊断修复系统
=====================================
实时监控日志 → 检测异常 → AI分析根因 → 自动修复/报告

架构:
  monitor.py → 实时监控日志、系统状态
  ai_debugger.py → DeepSeek AI 分析 + 修复建议
  auto_fix.py → 安全自动修复 + 风险分级
  knowledge_base.py → 错误知识库 (SQLite)
  dashboard.py → Web 状态面板

用法:
  python3 -m atos.autopilot.monitor       # 启动监控守护进程
  python3 -m atos.autopilot.dashboard     # 启动 Web 面板 (port 8897)
"""

__version__ = "1.0.0"
__all__ = ["monitor", "ai_debugger", "auto_fix", "knowledge_base"]
