"""Yahoo Finance fallback for option chain data.

Used by ``advisor.fetch_and_cache_chain`` when IB Gateway connection fails
(timeout / clientId collision / Gateway down). yfinance is sync and pulls a
non-trivial amount of HTML, so we run it in a worker thread to keep the
asyncio loop responsive.

Limitations vs IBKR (surfaced to the user via ``ChainFetchResult.source``):
- ~15-minute delay (Yahoo's free feed)
- No native Greeks — Δ is estimated from BS using IV from Yahoo
- No reliable open_interest cross-check; we forward what Yahoo gives
- Quotes are NOT persisted to ``chain_cache`` (caller's responsibility, and
  we currently skip it to avoid mixing IB-grade and BS-estimated data)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from options_tool.domain.black_scholes import bs_delta
from options_tool.ibkr import (
    ChainFetchResult,
    OptionQuote,
    _explain_empty_strikes,
    _select_strikes,
)

logger = logging.getLogger(__name__)


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN check (NaN != NaN)
    return f if f == f else None


def _safe_int(v) -> int | None:
    f = _safe_float(v)
    return int(f) if f is not None else None


def _fetch_chain_sync(
    symbol: str,
    *,
    side: str,
    dte_min: int,
    dte_max: int,
    today: date,
    strike_window_pct: float,
    max_strikes_per_side: int,
    strike_max: float | None,
) -> ChainFetchResult:
    """Blocking yfinance call. Run via ``asyncio.to_thread``."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)

    # 1) Spot — try fast_info first (cheapest), fall back to history close.
    spot: float | None = None
    try:
        fi = ticker.fast_info
        spot = _safe_float(getattr(fi, "last_price", None)) or _safe_float(
            getattr(fi, "previous_close", None)
        )
    except Exception:
        logger.debug("yfinance fast_info failed for %s", symbol, exc_info=True)
    if spot is None:
        try:
            hist = ticker.history(period="2d", auto_adjust=False)
            if not hist.empty:
                spot = _safe_float(hist["Close"].iloc[-1])
        except Exception:
            logger.debug("yfinance history failed for %s", symbol, exc_info=True)
    if spot is None:
        return ChainFetchResult(
            quotes=[],
            spot=None,
            reason="Yahoo 也拿不到 spot — 拼写错误？周末 / 非交易时段？",
            source="yahoo",
        )

    # 2) Expirations
    try:
        expiry_strs = list(ticker.options or ())
    except Exception as exc:
        return ChainFetchResult(
            quotes=[], spot=spot,
            reason=f"Yahoo options 接口失败：{exc}", source="yahoo",
        )

    candidate_expiries: list[date] = []
    for s in expiry_strs:
        try:
            exp = datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (exp - today).days
        if dte_min <= dte <= dte_max:
            candidate_expiries.append(exp)
    if not candidate_expiries:
        return ChainFetchResult(
            quotes=[], spot=spot,
            reason=f"Yahoo: DTE 窗口 [{dte_min},{dte_max}] 内无可用 expiry",
            source="yahoo",
        )

    # 3) For each expiry, pull the chain and filter strikes the same way IB
    #    does so the candidate set has parity. Yahoo gives us the full chain
    #    per expiry — strike pre-filter is purely cosmetic but keeps the row
    #    count down for downstream rank_chain.
    is_call = side.upper() == "CALL"
    quotes: list[OptionQuote] = []
    for exp in candidate_expiries:
        try:
            chain = ticker.option_chain(exp.strftime("%Y-%m-%d"))
        except Exception:
            logger.debug(
                "yfinance option_chain failed for %s %s", symbol, exp, exc_info=True
            )
            continue
        df = chain.calls if is_call else chain.puts
        if df is None or df.empty:
            continue

        all_strikes = sorted(_safe_float(s) for s in df["strike"].tolist())
        all_strikes = [s for s in all_strikes if s is not None]
        candidate_strikes = set(_select_strikes(
            all_strikes,
            spot=spot,
            window_pct=strike_window_pct,
            max_per_side=max_strikes_per_side,
            strike_max=strike_max,
            side=side,
        ))
        if not candidate_strikes:
            continue

        years_to_exp = max((exp - today).days, 0) / 365.0

        for row in df.itertuples(index=False):
            strike = _safe_float(getattr(row, "strike", None))
            if strike is None or strike not in candidate_strikes:
                continue
            iv = _safe_float(getattr(row, "impliedVolatility", None))
            delta = bs_delta(
                spot=spot,
                strike=strike,
                years_to_expiry=years_to_exp,
                iv=iv if iv is not None else 0.0,
                is_call=is_call,
            )
            quotes.append(OptionQuote(
                symbol=symbol,
                expiry=exp,
                strike=strike,
                right="C" if is_call else "P",
                bid=_safe_float(getattr(row, "bid", None)),
                ask=_safe_float(getattr(row, "ask", None)),
                last=_safe_float(getattr(row, "lastPrice", None)),
                delta=delta,
                gamma=None,
                theta=None,
                vega=None,
                iv=iv,
                open_interest=_safe_int(getattr(row, "openInterest", None)),
                volume=_safe_int(getattr(row, "volume", None)),
                underlying_price=spot,
            ))

    if not quotes:
        return ChainFetchResult(
            quotes=[], spot=spot,
            reason=_explain_empty_strikes(
                side=side, spot=spot, window_pct=strike_window_pct,
                strike_max=strike_max,
            ),
            source="yahoo",
        )

    return ChainFetchResult(quotes=quotes, spot=spot, reason=None, source="yahoo")


async def fetch_chain_yahoo(
    symbol: str,
    *,
    side: str,
    dte_min: int,
    dte_max: int,
    today: date | None = None,
    strike_window_pct: float = 0.25,
    max_strikes_per_side: int = 20,
    strike_max: float | None = None,
) -> ChainFetchResult:
    """Async wrapper — yfinance is blocking; offload to a worker thread."""
    today = today or datetime.now(timezone.utc).date()
    return await asyncio.to_thread(
        _fetch_chain_sync,
        symbol,
        side=side,
        dte_min=dte_min,
        dte_max=dte_max,
        today=today,
        strike_window_pct=strike_window_pct,
        max_strikes_per_side=max_strikes_per_side,
        strike_max=strike_max,
    )
