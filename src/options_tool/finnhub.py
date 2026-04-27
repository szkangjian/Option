"""Thin Finnhub API client for earnings calendar.

Free-tier limits: 60 req/min. We only call ``/calendar/earnings`` per symbol,
so a sync of ~20 tracked symbols is well under the limit. We do NOT use the
no-symbol bulk endpoint because it returns the entire US universe (huge).

Graceful degrade: if ``finnhub_api_key`` is missing, every public function
returns ``[]`` and logs once. The caller (sync.py) should treat this as
"earnings data unavailable" rather than a hard failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from options_tool.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://finnhub.io/api/v1"
_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class EarningsRow:
    symbol: str
    earnings_date: date
    time_of_day: str | None  # "bmo" / "amc" / None


async def fetch_earnings(symbols: list[str], *, lookahead_days: int | None = None) -> list[EarningsRow]:
    """Pull upcoming earnings for the given symbols.

    Returns rows from today through ``lookahead_days`` ahead. Missing API key
    returns ``[]`` (no exception). Per-symbol HTTP errors are logged and
    skipped — one bad ticker doesn't kill the batch.
    """
    settings = get_settings()
    api_key = settings.finnhub_api_key
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set; skipping earnings fetch")
        return []
    if not symbols:
        return []

    lookahead = lookahead_days if lookahead_days is not None else settings.earnings_lookahead_days
    today = date.today()
    until = today + timedelta(days=lookahead)
    params_base = {
        "from": today.isoformat(),
        "to": until.isoformat(),
        "token": api_key,
    }

    out: list[EarningsRow] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for sym in symbols:
            try:
                resp = await client.get(
                    f"{_BASE_URL}/calendar/earnings",
                    params={**params_base, "symbol": sym},
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.warning("Finnhub earnings fetch failed for %s: %s", sym, e)
                continue

            payload = resp.json() or {}
            for row in payload.get("earningsCalendar") or []:
                date_str = row.get("date")
                if not date_str:
                    continue
                try:
                    edate = date.fromisoformat(date_str)
                except ValueError:
                    continue
                hour = row.get("hour") or None
                out.append(EarningsRow(symbol=sym, earnings_date=edate, time_of_day=hour))
    return out
