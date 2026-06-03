"""SQLite 영속화 라운드트립 — 재시작에도 상태 유실 금지(§5)."""

from datetime import datetime

from src.state import StateStore, state_from_json, state_to_json
from src.strategy import OpenLimit, Params, Status, on_limit_placed, start_cycle


def _sample_state():
    s = start_cycle(4000.0, Params(), cycle_id=3)
    s = on_limit_placed(
        s,
        OpenLimit(
            "L1",
            48500.0,
            0.00206,
            100.0,
            filled_base_seen=0.0005,
            filled_quote_seen=25.0,
        ),
    )
    return s


def test_json_roundtrip_with_datetime_and_openlimit():
    s = _sample_state()
    import dataclasses

    s = dataclasses.replace(
        s, last_fill_time=datetime(2026, 6, 1, 12, 30, 0), ref=49000.0
    )
    back = state_from_json(state_to_json(s))
    assert back == s
    assert back.status == Status.RUNNING
    assert back.open_limit.filled_quote_seen == 25.0


def test_store_save_load(tmp_path):
    db = str(tmp_path / "t.db")
    store = StateStore(db)
    s = _sample_state()
    store.save(s)
    store.log_event("BUY", "test")
    store.close()

    store2 = StateStore(db)  # 재오픈 = 재시작 모사
    loaded = store2.load()
    store2.close()
    assert loaded == s


def test_load_empty_returns_none(tmp_path):
    store = StateStore(str(tmp_path / "empty.db"))
    assert store.load() is None
    store.close()
