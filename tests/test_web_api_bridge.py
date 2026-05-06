"""JSON API bridge behavior for cache-first market data and IB timeouts."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from options_tool.db import Account, ChainCache, StockPosition, init_db, session_scope
from options_tool.settings import load_api_accounts
from options_tool.web import api as api_mod
from options_tool.web.api import (
    _cached_option_chain_payload,
    _cached_spot_payload,
    _portfolio_payload,
    _with_ib_timeout,
)


def _fresh_db(tmp_path, monkeypatch):
    from options_tool import db as db_mod, settings as settings_mod

    db_path = tmp_path / "t.db"
    monkeypatch.setenv("OPTIONS_TOOL_DB_PATH", str(db_path))
    settings_mod.get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db()


def test_load_api_accounts_offsets_client_ids(tmp_path, monkeypatch):
    from options_tool import settings as settings_mod

    accounts_yaml = tmp_path / "accounts.yaml"
    accounts_yaml.write_text(
        """
accounts:
  - alias: main
    host: 127.0.0.1
    port: 4001
    client_id: 7878
    account_code: U1111111
    enabled: true
  - alias: secondary
    host: 127.0.0.1
    port: 4002
    client_id: 7879
    account_code: U2222222
    enabled: true
""".lstrip()
    )
    monkeypatch.setenv("OPTIONS_TOOL_API_CLIENT_ID_OFFSET", "200")
    settings_mod.get_settings.cache_clear()

    cfg = load_api_accounts(accounts_yaml)

    assert [a.client_id for a in cfg.accounts] == [8078, 8079]


def test_cached_spot_returns_fetched_at_and_age(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add(
            ChainCache(
                symbol="URA",
                expiry=date(1970, 1, 1),
                strike=0.0,
                right="C",
                underlying_price=31.25,
                fetched_at=fetched_at,
            )
        )

    payload = _cached_spot_payload("URA")

    assert payload["symbol"] == "URA"
    assert payload["spot"] == 31.25
    assert payload["source"] == "cache"
    assert payload["cache_hit"] is True
    assert payload["fetched_at"] is not None
    assert payload["age_seconds"] >= 0
    assert payload["stale"] is False


def test_stale_cached_spot_rejected_by_default(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with session_scope() as session:
        session.add(
            ChainCache(
                symbol="URA",
                expiry=date(1970, 1, 1),
                strike=0.0,
                right="C",
                underlying_price=31.25,
                fetched_at=fetched_at,
            )
        )

    with pytest.raises(HTTPException) as exc:
        _cached_spot_payload("URA", max_age_seconds=60)

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "stale_spot_cache"
    assert exc.value.detail["age_seconds"] > 60


def test_allow_stale_cached_spot_marks_payload(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with session_scope() as session:
        session.add(
            ChainCache(
                symbol="URA",
                expiry=date(1970, 1, 1),
                strike=0.0,
                right="C",
                underlying_price=31.25,
                fetched_at=fetched_at,
            )
        )

    payload = _cached_spot_payload(
        "URA",
        allow_stale=True,
        max_age_seconds=60,
    )

    assert payload["stale"] is True
    assert payload["age_seconds"] > 60


async def test_default_spot_refreshes_stale_cache(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with session_scope() as session:
        session.add(
            ChainCache(
                symbol="URA",
                expiry=date(1970, 1, 1),
                strike=0.0,
                right="C",
                underlying_price=31.25,
                fetched_at=fetched_at,
            )
        )

    async def fake_live_spot(symbol: str):
        return {
            "symbol": symbol,
            "spot": 32.0,
            "source": "ibkr",
            "cache_hit": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(api_mod, "_live_spot_payload", fake_live_spot)

    payload = await api_mod.spot_price(
        "URA",
        live=False,
        allow_stale=False,
        max_age_seconds=60,
    )

    assert payload["source"] == "ibkr"
    assert payload["spot"] == 32.0


def test_cached_option_chain_filters_and_reports_cache_age(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc)
    with session_scope() as session:
        session.add_all(
            [
                ChainCache(
                    symbol="URA",
                    expiry=date(2026, 6, 19),
                    strike=30.0,
                    right="C",
                    bid=1.0,
                    ask=1.2,
                    delta=0.2,
                    underlying_price=31.25,
                    fetched_at=fetched_at,
                ),
                ChainCache(
                    symbol="URA",
                    expiry=date(2026, 6, 19),
                    strike=25.0,
                    right="P",
                    bid=0.7,
                    ask=0.8,
                    delta=-0.25,
                    underlying_price=31.25,
                    fetched_at=fetched_at,
                ),
            ]
        )

    payload = _cached_option_chain_payload(
        "URA",
        side="CALL",
        dte_min=0,
        dte_max=90,
    )

    assert payload["source"] == "cache"
    assert payload["cache_hit"] is True
    assert payload["count"] == 1
    assert payload["quotes"][0]["right"] == "C"
    assert payload["quotes"][0]["mid"] == 1.1
    assert payload["fetched_at"] is not None
    assert payload["age_seconds"] >= 0
    assert payload["stale"] is False
    assert payload["stale_count"] == 0


def test_stale_cached_option_chain_rejected_by_default(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    with session_scope() as session:
        session.add(
            ChainCache(
                symbol="URA",
                expiry=date(2026, 6, 19),
                strike=30.0,
                right="C",
                bid=1.0,
                ask=1.2,
                delta=0.2,
                underlying_price=31.25,
                fetched_at=fetched_at,
            )
        )

    with pytest.raises(HTTPException) as exc:
        _cached_option_chain_payload(
            "URA",
            side="CALL",
            dte_min=0,
            dte_max=90,
            max_age_seconds=300,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "stale_option_chain_cache"
    assert exc.value.detail["stale_count"] == 1


async def test_default_option_chain_refreshes_stale_cache(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    with session_scope() as session:
        session.add(
            ChainCache(
                symbol="URA",
                expiry=date(2026, 6, 19),
                strike=30.0,
                right="C",
                bid=1.0,
                ask=1.2,
                delta=0.2,
                underlying_price=31.25,
                fetched_at=fetched_at,
            )
        )

    async def fake_live_chain(symbol: str, **kwargs):
        return {
            "symbol": symbol,
            "side": kwargs["side"],
            "spot": 32.0,
            "dte_window": [kwargs["dte_min"], kwargs["dte_max"]],
            "quotes": [{"symbol": symbol, "right": "C", "strike": 30.0}],
            "count": 1,
            "reason": None,
            "source": "ibkr",
            "cache_hit": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(api_mod, "_live_option_chain_request_payload", fake_live_chain)

    payload = await api_mod.option_chain(
        "URA",
        side="CALL",
        dte_min=0,
        dte_max=90,
        strike_window_pct=0.2,
        max_strikes_per_side=3,
        live=False,
        allow_stale=False,
        max_age_seconds=300,
    )

    assert payload["source"] == "ibkr"
    assert payload["count"] == 1


def test_stale_portfolio_rejected_by_default(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    synced_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with session_scope() as session:
        acct = Account(ib_account_code="U1111111", alias="main", enabled=True)
        session.add(acct)
        session.flush()
        session.add(
            StockPosition(
                account_id=acct.id,
                symbol="URA",
                qty=100,
                avg_cost=50,
                market_value=None,
                last_synced_at=synced_at,
            )
        )

    with pytest.raises(HTTPException) as exc:
        _portfolio_payload(refreshed=False, max_age_seconds=60)

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "stale_portfolio_cache"


def test_allow_stale_portfolio_marks_summary(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    synced_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with session_scope() as session:
        acct = Account(ib_account_code="U1111111", alias="main", enabled=True)
        session.add(acct)
        session.flush()
        session.add(
            StockPosition(
                account_id=acct.id,
                symbol="URA",
                qty=100,
                avg_cost=50,
                market_value=None,
                last_synced_at=synced_at,
            )
        )

    payload = _portfolio_payload(
        refreshed=False,
        allow_stale=True,
        max_age_seconds=60,
    )

    assert payload["summary"]["stale"] is True
    assert payload["summary"]["age_seconds"] > 60


async def test_default_portfolio_refreshes_stale_snapshot(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    synced_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with session_scope() as session:
        acct = Account(ib_account_code="U1111111", alias="main", enabled=True)
        session.add(acct)
        session.flush()
        session.add(
            StockPosition(
                account_id=acct.id,
                symbol="URA",
                qty=100,
                avg_cost=50,
                market_value=None,
                last_synced_at=synced_at,
            )
        )

    monkeypatch.setattr(api_mod, "_enabled_api_accounts", lambda: [])

    async def fake_sync_positions(configs=None, *, refresh_earnings=True):
        with session_scope() as session:
            row = session.query(StockPosition).filter_by(symbol="URA").one()
            row.last_synced_at = datetime.now(timezone.utc)
        return 1, 0

    monkeypatch.setattr(api_mod, "sync_positions", fake_sync_positions)

    payload = await api_mod.portfolio(
        refresh=False,
        allow_stale=False,
        max_age_seconds=60,
    )

    assert payload["summary"]["refreshed"] is True
    assert payload["summary"]["stale"] is False


async def test_ib_timeout_becomes_504():
    async def slow_call():
        await asyncio.sleep(0.05)

    with pytest.raises(HTTPException) as exc:
        await _with_ib_timeout(
            slow_call(),
            timeout_seconds=0.001,
            label="IB spot URA",
        )

    assert exc.value.status_code == 504
    assert "timed out" in exc.value.detail
