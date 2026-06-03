"""상태 영속화 — SQLite. 봇 재시작에도 유실 금지(§5). 진실의 최종 원천은 거래소(메모 5).

단일 상태행(JSON 블롭) + append-only 이벤트 로그. WAL + 트랜잭션으로 크래시 안전.
reconcile(부팅 정합화)는 거래소를 다뤄야 하므로 executor.reconcile 에 둔다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

from src.strategy import OpenLimit, State, Status


def state_to_json(state: State) -> str:
    d = asdict(state)
    d["status"] = state.status.value
    d["last_fill_time"] = (
        state.last_fill_time.isoformat() if state.last_fill_time else None
    )
    return json.dumps(d)


def state_from_json(text: str) -> State:
    d = json.loads(text)
    d["status"] = Status(d["status"])
    d["last_fill_time"] = (
        datetime.fromisoformat(d["last_fill_time"]) if d["last_fill_time"] else None
    )
    ol = d["open_limit"]
    d["open_limit"] = OpenLimit(**ol) if ol else None
    return State(**d)


class StateStore:
    def __init__(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL, detail TEXT)"
        )
        self.conn.commit()

    def save(self, state: State) -> None:
        self.conn.execute(
            "INSERT INTO bot_state (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (state_to_json(state),),
        )
        self.conn.commit()

    def load(self) -> State | None:
        row = self.conn.execute("SELECT data FROM bot_state WHERE id=1").fetchone()
        return state_from_json(row[0]) if row else None

    def log_event(self, type_: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), type_, detail),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
