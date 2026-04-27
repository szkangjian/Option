"""Alert dispatch — Telegram + dedup + quiet hours.

This module is the I/O wrapper around ``domain.alert_detection``. It:
  - Reads ``AlertsConfig`` thresholds + Telegram credentials from settings.
  - Dedupes against the ``alerts`` table (alert_key within dedup window).
  - Suppresses Telegram delivery during quiet hours (still records the alert).
  - Sends via Telegram Bot API; gracefully no-ops when token / chat_id missing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select

from options_tool.db import Alert, session_scope
from options_tool.domain.alert_detection import AlertCandidate
from options_tool.settings import AlertsConfig, get_settings, load_alerts

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class DispatchResult:
    sent: int
    suppressed_quiet: int
    suppressed_dedup: int
    failed: int


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now_local: datetime, start: str, end: str) -> bool:
    """True when ``now_local`` falls inside the [start, end] window.

    Handles wraparound (e.g., 22:00 → 07:00 spans midnight).
    """
    s = _parse_hhmm(start)
    e = _parse_hhmm(end)
    t = now_local.time()
    if s <= e:
        return s <= t < e
    return t >= s or t < e


async def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = _TELEGRAM_API.format(token=token)
    try:
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT) as client:
            resp = await client.post(
                url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            )
            resp.raise_for_status()
            return True
    except httpx.HTTPError as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def _is_recent_duplicate(alert_key: str, dedup_window_hours: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=dedup_window_hours)
    with session_scope() as session:
        existing = session.scalar(
            select(Alert)
            .where(Alert.alert_key == alert_key)
            .where(Alert.triggered_at >= cutoff)
            .limit(1)
        )
    return existing is not None


def _persist(candidate: AlertCandidate, sent: bool) -> None:
    """Upsert the alert row by ``alert_key`` — update ``triggered_at`` on re-fire.

    The table has UNIQUE(alert_key); dedup is by triggered_at within the
    window. So on a legitimate re-fire (after the window has elapsed), we must
    *update* the existing row, not INSERT, or the UNIQUE constraint aborts the
    whole scan mid-dispatch — which in the wild plays out as "Telegram sent,
    then crash, next scan re-sends, forever".
    """
    payload = json.dumps(asdict(candidate), ensure_ascii=False)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        existing = session.scalar(
            select(Alert).where(Alert.alert_key == candidate.alert_key)
        )
        if existing is None:
            session.add(
                Alert(
                    alert_key=candidate.alert_key,
                    alert_type=candidate.alert_type,
                    symbol=candidate.symbol,
                    payload=payload,
                    sent_to_telegram=sent,
                    triggered_at=now,
                )
            )
        else:
            existing.alert_type = candidate.alert_type
            existing.symbol = candidate.symbol
            existing.payload = payload
            existing.sent_to_telegram = sent
            existing.triggered_at = now


async def dispatch_alerts(
    candidates: list[AlertCandidate],
    config: AlertsConfig | None = None,
) -> DispatchResult:
    """Persist + (when allowed) push every candidate to Telegram."""
    if not candidates:
        return DispatchResult(0, 0, 0, 0)

    cfg = config or load_alerts()
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    telegram_ready = bool(token and chat_id)

    quiet = in_quiet_hours(
        datetime.now().astimezone(), cfg.quiet_hours_start, cfg.quiet_hours_end
    )

    sent = suppressed_quiet = suppressed_dedup = failed = 0
    for c in candidates:
        if _is_recent_duplicate(c.alert_key, cfg.dedup_window_hours):
            suppressed_dedup += 1
            continue

        if not telegram_ready:
            _persist(c, sent=False)
            failed += 1
            continue

        if quiet and c.severity != "critical":
            _persist(c, sent=False)
            suppressed_quiet += 1
            continue

        ok = await _send_telegram(token, chat_id, c.message)
        _persist(c, sent=ok)
        if ok:
            sent += 1
        else:
            failed += 1

    return DispatchResult(sent, suppressed_quiet, suppressed_dedup, failed)
