"""
ATOS PRO v17 — 配置加载器 (Config Loader)
==========================================
统一加载 YAML 配置，支持多场景切换。

参考: Qlib qrun YAML workflow, Freqtrade config.json
"""

import os
import yaml
from typing import Optional

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
_DEFAULT_SCENARIO = "default"
_config_cache: dict = {}
_loaded_scenario: Optional[str] = None


def load_config(scenario: str = None, force_reload: bool = False) -> dict:
    """加载策略配置。

    Args:
        scenario: 场景名 ("default", "conservative", "aggressive")
        force_reload: 强制重新加载

    Returns:
        嵌套的配置字典

    Example:
        cfg = load_config("conservative")
        min_score = cfg["factors"]["min_score"]
        trail = cfg["risk"]["trail_pct_bull"]
    """
    global _config_cache, _loaded_scenario

    scenario = scenario or _DEFAULT_SCENARIO
    cache_key = f"strategy_{scenario}"

    if cache_key in _config_cache and not force_reload:
        return _config_cache[cache_key]

    # 尝试从 strategy.yaml 加载
    yaml_path = os.path.join(_CONFIG_DIR, "strategy.yaml")
    if os.path.exists(yaml_path):
        with open(yaml_path) as f:
            all_configs = yaml.safe_load(f) or {}
        scenario_config = all_configs.get(scenario, all_configs.get(_DEFAULT_SCENARIO, {}))
    else:
        scenario_config = {}

    # 合并默认值（场景配置覆盖默认配置）
    default_config = {}
    if yaml_path and os.path.exists(yaml_path):
        with open(yaml_path) as f:
            all_configs = yaml.safe_load(f) or {}
        default_config = all_configs.get(_DEFAULT_SCENARIO, {})

    merged = _deep_merge(default_config, scenario_config)
    _config_cache[cache_key] = merged
    _loaded_scenario = scenario
    return merged


def current_scenario() -> str:
    return _loaded_scenario or _DEFAULT_SCENARIO


def get_capital() -> dict:
    """获取资金配置"""
    cfg = load_config()
    return cfg.get("capital", {"short_term": 300000, "long_term": 1000000, "total": 1300000})


def get_factor_config() -> dict:
    """获取因子引擎配置"""
    return load_config().get("factors", {})


def get_risk_config() -> dict:
    """获取风控配置"""
    return load_config().get("risk", {})


def get_position_config() -> dict:
    """获取持仓管理配置"""
    return load_config().get("positions", {})


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
