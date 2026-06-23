"""
VibeBridge v1.0 — ATOS Layer 2 的 Vibe-Trading 接口層
- 完整連線重試（httpx + tenacity）
- Swarm 輪詢帶超時保護
- 所有異常只記錄 + 返回降級結果，不 raise
- 磁盤緩存，Vibe 宕機時自動降級
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

VIBE_BASE_URL = "http://localhost:8899"
VIBE_AUTH_KEY = "atos_internal_secret_2026"
VIBE_TIMEOUT_SHORT = 30
VIBE_TIMEOUT_LONG = 180
VIBE_POLL_INTERVAL = 5
VIBE_MAX_RETRIES = 3

DATA_DIR = Path(__file__).parent.parent.parent / "data"
SIGNAL_DIR = DATA_DIR / "vibe_signals"
REPORT_DIR = DATA_DIR / "reports"
CACHE_DIR = DATA_DIR / "cache"
for _d in (SIGNAL_DIR, REPORT_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("atos.vibe_bridge")


@dataclass
class AtosSignal:
    ticker: str
    direction: str  # "long", "short", "flat"
    confidence: float
    position_size: float  # Layer 3 Kelly 填充
    source: str
    reason: str
    generated_at: str
    expires_at: str

    def is_valid(self) -> bool:
        return (
            bool(self.ticker)
            and self.direction in ("long", "short", "flat")
            and 0.0 <= self.confidence <= 1.0
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _make_headers() -> dict:
    return {
        "Authorization": f"Bearer {VIBE_AUTH_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=VIBE_BASE_URL,
        headers=_make_headers(),
        timeout=httpx.Timeout(timeout),
    )


def _make_signal(item: dict, now_iso: str) -> AtosSignal | None:
    """Create an AtosSignal from a structured dict item."""
    ticker = (item.get("ticker") or item.get("symbol") or "").strip().upper()
    if not ticker:
        return None
    direction = item.get("direction", "flat").lower()
    if direction not in ("long", "short", "flat"):
        direction = "flat"
    try:
        confidence = float(item.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5
    return AtosSignal(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        position_size=0.0,
        source="vibe_quant_desk",
        reason=str(item.get("reason", "")),
        generated_at=now_iso,
        expires_at=str(item.get("expires_at", "")),
    )


def _parse_signals(raw: dict) -> list[AtosSignal]:
    """Parse signals from swarm response. Handles both 'signals' array and 'final_report' text."""
    signals = []
    now_iso = datetime.now().isoformat()

    # Format 1: structured signals array
    raw_signals = raw.get("signals", [])
    if raw_signals:
        for item in raw_signals:
            sig = _make_signal(item, now_iso)
            if sig and sig.is_valid():
                signals.append(sig)
        return signals

    # Format 2: final_report text — try to extract structured info
    report = raw.get("final_report", "")
    if isinstance(report, dict):
        report = json.dumps(report)
    if report and isinstance(report, str):
        # Look for ticker patterns like 0700.HK, 9988.HK, AAPL, etc.
        import re
        # 严格 regex: 只匹配大写字母数字组合 + 港股格式
        ticker_pattern = re.compile(r'\b([A-Z]{1,5}\b(?:\.HK)?|\d{4}\.HK)\b')
        direction_pattern = re.compile(r'(long|short|flat|buy|sell|hold)', re.IGNORECASE)
        conf_pattern = re.compile(r'confidence[\s:]+(\d+\.?\d*)', re.IGNORECASE)

        # 已知有效 ticker（防止正则误匹配普通英语单词）
        _KNOWN_TICKERS = set()
        try:
            from atos.core.universe import ALL_SYMBOLS
            _KNOWN_TICKERS = set(ALL_SYMBOLS)
        except ImportError:
            pass

        for line in report.split('\n'):
            ticker_match = ticker_pattern.search(line)
            if not ticker_match:
                continue
            ticker = ticker_match.group(1).upper()
            # 白名单过滤：只接受已知 universe 中的 ticker
            if _KNOWN_TICKERS and ticker not in _KNOWN_TICKERS:
                continue
            dir_match = direction_pattern.search(line)
            direction = "flat"
            if dir_match:
                d = dir_match.group(1).lower()
                direction = {"buy": "long", "sell": "short"}.get(d, d)
            conf = 0.5
            conf_match = conf_pattern.search(line)
            if conf_match:
                try:
                    conf = float(conf_match.group(1))
                    conf = max(0.0, min(1.0, conf))
                except ValueError:
                    pass
            sig = AtosSignal(
                ticker=ticker, direction=direction,
                confidence=conf, position_size=0.0,
                source="vibe_quant_desk",
                reason=line.strip()[:200],
                generated_at=now_iso,
                expires_at="",
            )
            if sig.is_valid():
                signals.append(sig)
        if signals:
            return signals

    logger.info("[VibeBridge] No structured signals found in response.")
    return []


def _save_signals_cache(signals: list[AtosSignal]) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = SIGNAL_DIR / f"signals_{ts}.json"
    payload = json.dumps([s.to_dict() for s in signals], ensure_ascii=False, indent=2)
    out.write_text(payload, encoding="utf-8")
    (CACHE_DIR / "latest_signals.json").write_text(payload, encoding="utf-8")
    logger.info("[VibeBridge] Saved %d signals → %s", len(signals), out)


def load_cached_signals() -> list[AtosSignal]:
    """從磁盤加載最近一次緩存的信號（Vibe 宕機時降級使用）"""
    latest = CACHE_DIR / "latest_signals.json"
    if not latest.exists():
        logger.warning("[VibeBridge] No cached signals found.")
        return []
    try:
        raw_list = json.loads(latest.read_text(encoding="utf-8"))
        signals = [AtosSignal(**{**d, "source": "cached"}) for d in raw_list]
        logger.info("[VibeBridge] Loaded %d cached signals.", len(signals))
        return signals
    except Exception as e:
        logger.error("[VibeBridge] Failed to load cached signals: %s", e)
        return []


class VibeBridge:
    """ATOS Layer 2 — Vibe-Trading 接口層"""

    def __init__(self):
        self._session_cache: dict[str, str] = {}

    async def _post_with_retry(
        self, path: str, json_body: dict, timeout: float = VIBE_TIMEOUT_SHORT
    ) -> Optional[dict]:
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(
                    (httpx.ConnectError, httpx.TimeoutException)
                ),
                stop=stop_after_attempt(VIBE_MAX_RETRIES),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                reraise=False,
            ):
                with attempt:
                    async with _make_client(timeout) as client:
                        resp = await client.post(path, json=json_body)
                        resp.raise_for_status()
                        return resp.json()
        except Exception as e:
            logger.error("[VibeBridge] POST %s failed: %s", path, e)
            return None

    async def _get_with_retry(
        self, path: str, timeout: float = VIBE_TIMEOUT_SHORT
    ) -> Optional[dict]:
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(
                    (httpx.ConnectError, httpx.TimeoutException)
                ),
                stop=stop_after_attempt(VIBE_MAX_RETRIES),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                reraise=False,
            ):
                with attempt:
                    async with _make_client(timeout) as client:
                        resp = await client.get(path)
                        resp.raise_for_status()
                        return resp.json()
        except Exception as e:
            logger.error("[VibeBridge] GET %s failed: %s", path, e)
            return None

    async def _create_session(self, title: str) -> Optional[str]:
        # 同一天同標題複用 session
        date_key = datetime.now().strftime("%Y%m%d")
        cache_key = f"{title}_{date_key}"
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]
        result = await self._post_with_retry("/sessions", {"title": title})
        if result and (result.get("session_id") or result.get("id")):
            sid = result.get("session_id") or result.get("id")
            self._session_cache[cache_key] = sid
            return sid
        return None

    async def _poll_run(
        self, run_id: str, timeout_secs: int = VIBE_TIMEOUT_LONG
    ) -> Optional[dict]:
        deadline = time.monotonic() + timeout_secs
        poll_url = f"/swarm/runs/{run_id}"
        while time.monotonic() < deadline:
            await asyncio.sleep(VIBE_POLL_INTERVAL)
            data = await self._get_with_retry(poll_url)
            if data is None:
                continue
            status = data.get("status", "")
            logger.debug("[VibeBridge] run_id=%s status=%s", run_id, status)
            if status == "completed":
                return data
            elif status == "failed":
                logger.error(
                    "[VibeBridge] Swarm failed: %s", data.get("error", "")
                )
                return data
        logger.warning("[VibeBridge] run %s timed out", run_id)
        return {"status": "timeout", "run_id": run_id, "signals": []}

    async def healthcheck(self) -> bool:
        """檢測 Vibe server 是否在線"""
        try:
            async with _make_client(timeout=5) as client:
                resp = await client.get("/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def morning_scan(
        self,
        universe: list[str],
        focus: str = "港股量價背離 + 北向資金流入 + RSI超賣反彈",
        horizon: str = "1-5 days",
    ) -> list[AtosSignal]:
        """早間掃描：調用 Vibe Swarm 分析指定 universe"""
        logger.info("[VibeBridge] morning_scan: %s", universe)
        session_id = await self._create_session(
            f"morning_scan_{datetime.now().strftime('%Y%m%d')}"
        )
        if not session_id:
            return load_cached_signals()

        run_data = await self._post_with_retry(
            "/swarm/runs",
            {
                "preset_name": "quant_strategy_desk",
                "session_id": session_id,
                "variables": {
                    "universe": " ".join(universe),
                    "horizon": horizon,
                    "focus": focus,
                },
            },
            timeout=VIBE_TIMEOUT_SHORT,
        )

        if not run_data or not (run_data.get("id") or run_data.get("run_id")):
            return load_cached_signals()

        run_id = run_data.get("id") or run_data.get("run_id")
        result = await self._poll_run(run_id)
        if not result or result.get("status") in ("failed", "timeout"):
            return load_cached_signals()

        signals = _parse_signals(result)
        if signals:
            _save_signals_cache(signals)
        logger.info("[VibeBridge] morning_scan done: %d signals", len(signals))
        return signals

    async def alpha_bench_hk(
        self,
        universe: list[str],
        zoo: str = "gtja191",
        period: str = "2023-2026",
        top: int = 20,
    ) -> list[dict]:
        """Alpha 因子基準測試（GTJA191 等因子庫）"""
        result = await self._post_with_retry(
            "/alpha/bench",
            {"zoo": zoo, "universe": universe, "period": period, "top": top},
        )
        if not result:
            return []
        job_id = result.get("job_id")
        if not job_id:
            return []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            data = await self._get_with_retry(f"/alpha/bench/{job_id}")
            if data and data.get("status") == "completed":
                alive = [
                    a for a in data.get("results", []) if a.get("status") == "alive"
                ]
                (
                    CACHE_DIR / f"alpha_bench_{datetime.now().strftime('%Y%m%d')}.json"
                ).write_text(
                    json.dumps(alive, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return alive
        return []

    async def shadow_review(self, trades_csv_path: str) -> str:
        """Shadow 交易回顧：上傳 CSV，讓 Vibe 分析行為偏差"""
        csv_path = Path(trades_csv_path)
        if not csv_path.exists():
            logger.error("[VibeBridge] CSV not found: %s", trades_csv_path)
            return ""
        try:
            async with httpx.AsyncClient(
                base_url=VIBE_BASE_URL,
                headers={"Authorization": f"Bearer {VIBE_AUTH_KEY}"},
                timeout=VIBE_TIMEOUT_SHORT,
            ) as client:
                with csv_path.open("rb") as f:
                    resp = await client.post(
                        "/upload",
                        files={"file": (csv_path.name, f, "text/csv")},
                    )
                resp.raise_for_status()
                upload_ref = resp.json().get("path", "")
        except Exception as e:
            logger.error("[VibeBridge] Upload failed: %s", e)
            return ""
        if not upload_ref:
            return ""
        session_id = await self._create_session(
            f"shadow_{datetime.now().strftime('%Y%m%d')}"
        )
        if not session_id:
            return ""
        msg_result = await self._post_with_retry(
            f"/sessions/{session_id}/messages",
            {
                "role": "user",
                "content": (
                    f"我上傳了富途的交易記錄：{upload_ref}。請：\n"
                    "1. 識別行為偏差（處置效應、追漲殺跌、過度交易）\n"
                    "2. 提取隱性交易規則並量化勝率\n"
                    "3. 對比 Shadow Strategy 找出機會成本最大的 3 個決策\n"
                    "4. 生成 HTML 報告保存到 reports 目錄，語言：繁體中文"
                ),
            },
        )
        if not msg_result:
            return ""
        run_id = msg_result.get("run_id")
        if not run_id:
            return ""
        result = await self._poll_run(run_id, timeout_secs=180)
        return result.get("report_path", "") if result else ""
