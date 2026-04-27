"""_persist upserts by alert_key — regression for the 'every 5 min' spam bug.

The table has UNIQUE(alert_key) and dedup is window-based on triggered_at.
Before the fix, a legitimate re-fire after the dedup window elapsed would
try to INSERT a second row with the same key, hit UNIQUE, and abort the scan
mid-dispatch (Telegram already sent). That played out as endless re-sends.
This test pins the upsert behavior so the regression can't recur silently.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from options_tool.alerts import _is_recent_duplicate, _persist
from options_tool.db import Alert, init_db, session_scope
from options_tool.domain.alert_detection import AlertCandidate


def _cand(**overrides) -> AlertCandidate:
    base = dict(
        alert_key="STOP_LOSS:URA:C:60:2026-05-15",
        alert_type="STOP_LOSS",
        symbol="URA",
        message="🛑 URA C60 ... 考虑止损",
        severity="warn",
    )
    base.update(overrides)
    return AlertCandidate(**base)


def _fresh_db(tmp_path, monkeypatch):
    from options_tool import db as db_mod, settings as settings_mod

    db_path = tmp_path / "t.db"
    monkeypatch.setenv("OPTIONS_TOOL_DB_PATH", str(db_path))
    settings_mod.get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db()


def test_first_fire_inserts(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _persist(_cand(), sent=True)
    with session_scope() as s:
        rows = list(s.scalars(select(Alert)))
    assert len(rows) == 1
    assert rows[0].alert_key == "STOP_LOSS:URA:C:60:2026-05-15"
    assert rows[0].sent_to_telegram is True


def test_refire_updates_not_inserts(tmp_path, monkeypatch):
    """After the dedup window elapses, a second fire must UPDATE the row."""
    _fresh_db(tmp_path, monkeypatch)
    _persist(_cand(), sent=True)

    # Backdate the row to 10 hours ago — past a 6h dedup window.
    with session_scope() as s:
        row = s.scalar(select(Alert))
        assert row is not None
        row.triggered_at = datetime.now(timezone.utc) - timedelta(hours=10)

    # Re-fire: would previously crash on UNIQUE. Now upserts.
    assert _is_recent_duplicate("STOP_LOSS:URA:C:60:2026-05-15", 6) is False
    _persist(_cand(), sent=True)

    with session_scope() as s:
        rows = list(s.scalars(select(Alert)))
    # Still exactly one row, with refreshed triggered_at.
    assert len(rows) == 1
    # SQLite strips tz info on read; compare against naive UTC now.
    assert rows[0].triggered_at > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)


def test_refire_within_window_is_deduped(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _persist(_cand(), sent=True)
    # Dedup check within the same window → True; caller skips _persist.
    assert _is_recent_duplicate("STOP_LOSS:URA:C:60:2026-05-15", 6) is True


def test_refire_refreshes_sent_flag(tmp_path, monkeypatch):
    """If the first fire was suppressed (e.g., quiet hours), a re-fire that
    actually sends should flip the flag to True."""
    _fresh_db(tmp_path, monkeypatch)
    _persist(_cand(), sent=False)  # first fire during quiet hours
    with session_scope() as s:
        row = s.scalar(select(Alert))
        row.triggered_at = datetime.now(timezone.utc) - timedelta(hours=10)

    _persist(_cand(), sent=True)
    with session_scope() as s:
        row = s.scalar(select(Alert))
    assert row.sent_to_telegram is True
