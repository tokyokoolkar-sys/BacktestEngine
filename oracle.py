"""
oracle.py — AI timing oracle for DCA bot.

Calls OpenCode Zen API (or any OpenAI-compatible endpoint) with current market data
and previous trade context. Returns "go" / "no-go" signal with confidence score.

Usage from bot.py:
    oracle = AiOracle(api_key, endpoint, model)
    decision = oracle.decide(symbol, klines, prev_trade)
    double_checked = oracle.reconsider(symbol, decision, reason)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("oracle")

OPENCODE_DEFAULT_ENDPOINT = "http://localhost:20128/v1"
OPENCODE_DEFAULT_MODEL = "oc/deepseek-v4-flash-free"


@dataclass
class OracleDecision:
    signal: str          # "go" | "no-go"
    confidence: float    # 0.0 - 1.0
    reason: str          # brief explanation
    double_check: Optional[dict] = None  # if bot flagged a conflict


class AiOracle:
    """Calls an LLM (DeepSeek via OpenCode API) for trade timing."""

    def __init__(
        self,
        api_key: str,
        endpoint: str | None = None,
        model: str | None = None,
        timeout: int = 30,
    ):
        self.api_key = api_key
        self.endpoint = endpoint or os.getenv(
            "OPENCODE_API_ENDPOINT", OPENCODE_DEFAULT_ENDPOINT
        )
        self.model = model or os.getenv(
            "OPENCODE_MODEL", OPENCODE_DEFAULT_MODEL
        )
        self.timeout = timeout
        self._history: list[dict] = []  # conversation per-symbol kept externally

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_payload(self, symbol: str, klines: list, prev_trade: dict | None,
                       tp_pct: float, stop_pct: float, reason: str | None = None) -> dict:
        """Build the chat payload for OpenCode API."""

        # Format last 10 klines concisely
        if klines:
            # Each kline: [time, open, high, low, close, volume, ...]
            candles = []
            for k in klines[-12:]:  # last 12 for context
                candles.append({
                    "t": k[0], "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                })
        else:
            candles = []

        market_data = {
            "symbol": symbol,
            "interval": "1h",
            "last_candles": candles,
            "tp_pct": tp_pct,
            "stop_pct": stop_pct,
        }

        if prev_trade:
            market_data["last_trade"] = {
                "result": "win" if prev_trade.get("net_pnl", 0) > 0 else "loss",
                "net_pnl": round(prev_trade.get("net_pnl", 0), 4),
                "exit_reason": prev_trade.get("exit_reason", ""),
                "n_safety_filled": prev_trade.get("n_safety_filled", 0),
                "pnl_pct": round(prev_trade.get("pnl_pct", 0), 2),
            }

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a crypto futures DCA scalp timing oracle. "
                    "Your job: analyze 1h market data for a symbol and decide if now is a good "
                    "time to enter a long DCA position.\n\n"
                    "Strategy: entry on red candle close → DCA safety orders at 0.5% drops → "
                    f"TP at {tp_pct}% → stop at {stop_pct}%.\n\n"
                    "Rules:\n"
                    "1. Only say 'go' if the risk/reward looks favorable — crab/range markets, "
                    "oversold bounces, clear support levels.\n"
                    "2. Say 'no-go' in strong downtrends, low volatility chop, or when "
                    "the last trade was a big loss on this symbol.\n"
                    "3. 'no-go' is safe — waiting is always an option.\n"
                    "4. Return exactly {\"signal\": \"go|no-go\", \"confidence\": 0.0, \"reason\": \"short\"}. "
                    "No markdown, no extra text."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Market data:\n```json\n{json.dumps(market_data, indent=2)}\n```\n\n"
                    "Decide: go or no-go?"
                ),
            },
        ]

        # If this is a double-check, append the reason for re-query
        if reason:
            messages.append({
                "role": "user",
                "content": f"Double-check: my risk layer flagged: {reason}. "
                           "Re-evaluate. If still 'go', explain why the flag is acceptable.",
            })

        return {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
        }

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------

    def _call(self, payload: dict) -> dict | None:
        """POST to OpenCode API. Returns parsed JSON or None on failure."""
        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            # OpenAI-compatible: base URL may or may not include /chat/completions
            url = self.endpoint.rstrip("/")
            if not url.endswith("chat/completions"):
                url += "/chat/completions"
            r = None  # init so exception handlers can safely reference it
            r = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            # This endpoint (local deepseek) returns a valid JSON object followed by
            # trailing extra data, which strict r.json() rejects ("Extra data: ..."").
            # raw_decode parses the first complete object and ignores the rest.
            try:
                data = r.json()
            except json.JSONDecodeError:
                # Recover the leading object; if it isn't a JSON object (e.g. plain
                # "502 Bad Gateway" parses as the number 502), it's a real failure.
                obj, _ = json.JSONDecoder().raw_decode(r.text.lstrip())
                if not isinstance(obj, dict):
                    raise
                data = obj

            # Guard against error bodies that lack "choices" (rate-limit, auth, etc.)
            choices = data.get("choices")
            if not choices or not isinstance(choices, list):
                logger.error(
                    "oracle response missing 'choices'; raw body: %s",
                    str(data)[:500],
                )
                return None

            content = choices[0]["message"].get("content") or ""
            if not content.strip():
                logger.warning("empty oracle content; raw response: %s", str(data)[:500])
                return None
            parsed = json.loads(content)
            return parsed

        except ImportError:
            logger.error("requests not installed. pip install requests")
            return None
        except json.JSONDecodeError as e:
            resp_text = r.text[:500] if r is not None else "<no response>"
            status = r.status_code if r is not None else 0
            logger.error("API response not valid JSON: %s | status=%d | body=%s",
                         e, status, resp_text)
            return None
        except Exception as e:
            logger.error("API call failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def decide(
        self,
        symbol: str,
        klines: list | None,
        prev_trade: dict | None = None,
        tp_pct: float = 0.5,
        stop_pct: float = 3.0,
    ) -> OracleDecision | None:
        """Query DeepSeek: go or no-go for this symbol right now.

        Returns OracleDecision, or None if API call failed.
        IMPORTANT: callers MUST treat None as FAIL CLOSED (block entry), not fail-open.
        """
        payload = self._build_payload(symbol, klines, prev_trade, tp_pct, stop_pct)
        raw = self._call(payload)

        if raw is None:
            return None

        signal = raw.get("signal", "no-go")
        confidence = float(raw.get("confidence", 0.0))
        reason = str(raw.get("reason", ""))
        logger.info("%s: oracle=%s  confidence=%.2f  reason=%s",
                     symbol, signal, confidence, reason)
        return OracleDecision(signal=signal, confidence=confidence, reason=reason)

    def reconsider(
        self,
        symbol: str,
        klines: list | None,
        prev_trade: dict | None,
        tp_pct: float,
        stop_pct: float,
        flag: str,
    ) -> OracleDecision | None:
        """Double-check: if bot risk layer flagged DeepSeek's go, re-query with context."""
        payload = self._build_payload(symbol, klines, prev_trade, tp_pct, stop_pct,
                                      reason=flag)
        raw = self._call(payload)

        if raw is None:
            return None

        signal = raw.get("signal", "no-go")
        confidence = float(raw.get("confidence", 0.0))
        reason = str(raw.get("reason", ""))
        logger.info("%s: oracle=DOUBLE_CHECK  signal=%s  flag=%s  confidence=%.2f  reason=%s",
                     symbol, signal, flag, confidence, reason)
        return OracleDecision(
            signal=signal, confidence=confidence, reason=reason,
            double_check={"flag": flag},
        )
