import os, json, requests

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL   = "deepseek-chat"

SYSTEM_PROMPT = """
You are an expert quantitative portfolio manager for a US equities account.
You have access to a multi-factor model (value, momentum, quality, technical).
Return ONLY a valid JSON object (no markdown, no explanation) with this structure:
{
  "short_term_actions": [
    {"action": "BUY|SELL|HOLD", "symbol": "TICKER", "target_pct": 0.10, "reason": "..."}
  ],
  "long_term_actions": [
    {"action": "BUY|SELL|HOLD", "symbol": "TICKER", "target_pct": 0.10, "reason": "..."}
  ],
  "risk_notes": "..."
}

DECISION FRAMEWORK:
1. CHECK FACTOR RANKINGS FIRST — factor_rankings are computed from value+ momentum+quality+technical. Higher score = better risk-adjusted opportunity.
2. ALIGN WITH REGIME — use factor_weights to understand which factors matter most now.
3. POSITION SIZING — use target_pct proportional to factor score. Score>0.7 → up to max_single_pct. Score 0.55-0.7 → half. Score<0.55 → skip.
4. SELL RULES — sell if: factor score dropped >0.15 since entry, or trend turned DOWN, or RSI>80, or stop_loss triggered.

Rules:
- target_pct is desired allocation as fraction of total equity (0.0-1.0)
- Respect max_single_pct and budget constraints
- BEAR/HIGH_VOL regime: avoid new longs on high-beta tech (TSLA/AMD/META/NVDA)
- AGGRESSIVE mode (<$500k): 3-5 positions max; CONSERVATIVE (>$500k): 6-10 positions
- Only use symbols from factor_rankings or universe list
- Prefer symbols with factor score > 0.60
- Diversify across sectors
"""

def get_advice(snapshot: dict) -> dict:
    if not DEEPSEEK_API_KEY:
        print("[ai_advisor] No DEEPSEEK_API_KEY - using fallback")
        return _fallback()
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": json.dumps(snapshot, ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        advice  = json.loads(content)
        print(f"[ai_advisor] DeepSeek OK. Risk: {advice.get('risk_notes', '--')}")
        return advice
    except Exception as e:
        print(f"[ai_advisor] DeepSeek error: {e} - fallback")
        return _fallback()

def _fallback():
    return {"short_term_actions": [], "long_term_actions": [], "risk_notes": "fallback: hold all"}
