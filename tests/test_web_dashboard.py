"""Dashboard recommendation surfacing rules."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from options_tool.db import Recommendation, Symbol, init_db, session_scope
from options_tool.web import app as app_mod
from options_tool.jobs import _recent_top_recommendations
from options_tool.web.app import _build_dashboard


def _fresh_db(tmp_path, monkeypatch):
    from options_tool import db as db_mod, settings as settings_mod

    db_path = tmp_path / "t.db"
    monkeypatch.setenv("OPTIONS_TOOL_DB_PATH", str(db_path))
    settings_mod.get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    app_mod._recommendation_refresh_markers.clear()
    app_mod._recommendation_refresh_inflight.clear()
    app_mod._recommendation_refresh_errors.clear()
    init_db()


def _recommendation(
    *,
    symbol: str,
    intent: str,
    generated_at: datetime,
    roc: float,
    expiry: date = date(2026, 6, 18),
) -> Recommendation:
    is_csp = intent == "WANT_TO_OWN"
    return Recommendation(
        generated_at=generated_at,
        symbol=symbol,
        intent=intent,
        right="P" if is_csp else "C",
        strike=48.0 if is_csp else 60.0,
        expiry=expiry,
        premium=1.25,
        delta=-0.22 if is_csp else 0.20,
        dte=38,
        annualized_roc=roc,
        rank=1,
    )


def test_dashboard_ignores_recommendations_from_previous_intent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(Symbol(symbol="URA", intent="INCOME"))
        session.add_all(
            [
                _recommendation(
                    symbol="URA",
                    intent="INCOME",
                    generated_at=now - timedelta(minutes=1),
                    roc=0.30,
                ),
                _recommendation(
                    symbol="URA",
                    intent="WANT_TO_OWN",
                    generated_at=now,
                    roc=0.80,
                ),
            ]
        )

    dashboard = _build_dashboard()

    assert len(dashboard["opportunities"]) == 1
    assert dashboard["opportunities"][0]["symbol"] == "URA"
    assert dashboard["opportunities"][0]["intent"] == "INCOME"
    assert dashboard["opportunities"][0]["right"] == "C"


def test_dashboard_ignores_stale_recommendations(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIONS_TOOL_RECOMMENDATION_MAX_AGE_SECONDS", "60")
    _fresh_db(tmp_path, monkeypatch)
    with session_scope() as session:
        session.add(Symbol(symbol="URA", intent="INCOME"))
        session.add(
            _recommendation(
                symbol="URA",
                intent="INCOME",
                generated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                roc=0.80,
            )
        )

    dashboard = _build_dashboard()

    assert dashboard["opportunities"] == []


def test_dashboard_ignores_weekly_recommendation_when_weekly_disabled(
    tmp_path, monkeypatch
):
    _fresh_db(tmp_path, monkeypatch)
    with session_scope() as session:
        session.add(Symbol(symbol="CCJ", intent="INCOME", weekly_ok=False))
        session.add(
            _recommendation(
                symbol="CCJ",
                intent="INCOME",
                generated_at=datetime.now(timezone.utc),
                roc=0.80,
                expiry=date(2026, 6, 5),
            )
        )

    assert _build_dashboard()["opportunities"] == []


def test_dashboard_allows_holiday_adjusted_monthly_when_weekly_disabled(
    tmp_path, monkeypatch
):
    _fresh_db(tmp_path, monkeypatch)
    with session_scope() as session:
        session.add(Symbol(symbol="CCJ", intent="INCOME", weekly_ok=False))
        session.add(
            _recommendation(
                symbol="CCJ",
                intent="INCOME",
                generated_at=datetime.now(timezone.utc),
                roc=0.80,
                expiry=date(2026, 6, 18),
            )
        )

    opportunities = _build_dashboard()["opportunities"]

    assert len(opportunities) == 1
    assert opportunities[0]["symbol"] == "CCJ"
    assert opportunities[0]["expiry"] == date(2026, 6, 18)


async def test_dashboard_schedules_stale_recommendation_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIONS_TOOL_RECOMMENDATION_MAX_AGE_SECONDS", "60")
    _fresh_db(tmp_path, monkeypatch)
    with session_scope() as session:
        session.add(Symbol(symbol="URA", intent="INCOME"))
        session.add(
            _recommendation(
                symbol="URA",
                intent="INCOME",
                generated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                roc=0.30,
            )
        )

    calls = []

    async def fake_refresh(symbols: list[str]):
        calls.extend(symbols)
        return {"refreshed": symbols, "errors": {}}

    monkeypatch.setattr(app_mod, "_refresh_recommendations_for_symbols", fake_refresh)

    refresh_status = await app_mod._refresh_stale_dashboard_recommendations()
    await asyncio.sleep(0)

    assert refresh_status["refreshing"] == ["URA"]
    assert calls == ["URA"]
    assert _build_dashboard(refresh_status)["opportunities"] == []


def test_opportunity_alerts_ignore_recommendations_from_previous_intent(
    tmp_path, monkeypatch
):
    _fresh_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(Symbol(symbol="URA", intent="INCOME"))
        session.add(
            _recommendation(
                symbol="URA",
                intent="WANT_TO_OWN",
                generated_at=now,
                roc=0.80,
            )
        )

    assert _recent_top_recommendations(within_minutes=10) == []


def test_opportunity_alerts_ignore_weekly_when_weekly_disabled(
    tmp_path, monkeypatch
):
    _fresh_db(tmp_path, monkeypatch)
    with session_scope() as session:
        session.add(Symbol(symbol="CCJ", intent="INCOME", weekly_ok=False))
        session.add(
            _recommendation(
                symbol="CCJ",
                intent="INCOME",
                generated_at=datetime.now(timezone.utc),
                roc=0.80,
                expiry=date(2026, 6, 5),
            )
        )

    assert _recent_top_recommendations(within_minutes=10) == []
