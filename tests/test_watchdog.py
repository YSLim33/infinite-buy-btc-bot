"""워치독/하트비트 단위테스트 — 주입 clock/callback 으로 실제 스레드·프로세스 종료 없이 검증.

패턴: 손으로 짠 fake(FakeClock·RecorderNotifier) + 주입 on_timeout/sleep/stop_check.
on_timeout 은 os._exit 대신 리스트에 기록 → 종료 경로를 테스트가 관찰.
"""

import threading

from src.watchdog import Heartbeat, is_stale, watchdog_loop


class FakeClock:
    """리스트 대신 단일 값으로 제어하는 monotonic 대체. 호출마다 현재 t 반환."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class RecorderNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, message):
        self.messages.append(message)


def make_stop(allow: int):
    """body 를 `allow` 번 돈 뒤 멈추는 stop_check(무한 루프 방지)."""
    state = {"n": 0}

    def stop():
        state["n"] += 1
        return state["n"] > allow

    return stop


# --- is_stale (순수) ---------------------------------------------------------
def test_is_stale_boundary():
    assert is_stale(299, 300) is False
    assert is_stale(300, 300) is True
    assert is_stale(301, 300) is True


# --- Heartbeat ---------------------------------------------------------------
def test_heartbeat_age_uses_injected_clock():
    clk = FakeClock(100.0)
    hb = Heartbeat(clock=clk)  # baseline at 100
    clk.t = 142.0
    assert hb.age() == 42.0
    hb.beat()  # beat at 142
    assert hb.age() == 0.0


def test_heartbeat_writes_file(tmp_path):
    clk = FakeClock(7.0)
    p = tmp_path / ".hb"
    hb = Heartbeat(path=str(p), clock=clk)
    hb.beat()
    assert p.exists()
    assert p.read_text() == "7.0"


def test_heartbeat_file_write_failure_swallowed(tmp_path):
    clk = FakeClock(0.0)
    # 존재하지 않는 하위 디렉터리 → open 실패하지만 beat 는 비예외, ts 는 갱신.
    hb = Heartbeat(path=str(tmp_path / "no_such_subdir" / ".hb"), clock=clk)
    clk.t = 5.0
    hb.beat()  # must not raise
    assert hb.age() == 0.0


# --- watchdog_loop -----------------------------------------------------------
def test_watchdog_fires_on_timeout_when_stale():
    clk = FakeClock(0.0)
    hb = Heartbeat(clock=clk)  # baseline 0
    clk.t = 700.0  # age 700 >= 600 → stale
    notifier = RecorderNotifier()
    calls = []
    watchdog_loop(
        hb,
        600.0,
        notifier,
        on_timeout=lambda: calls.append(1),
        clock=clk,
        sleep=lambda *_: None,
    )
    assert calls == [1]
    assert any("[WATCHDOG]" in m for m in notifier.messages)


def test_watchdog_quiet_when_fresh():
    clk = FakeClock(0.0)
    hb = Heartbeat(clock=clk)
    clk.t = 100.0  # age 100 < 600 → fresh
    notifier = RecorderNotifier()
    calls = []
    watchdog_loop(
        hb,
        600.0,
        notifier,
        on_timeout=lambda: calls.append(1),
        stop_check=make_stop(1),
        clock=clk,
        sleep=lambda *_: None,
    )
    assert calls == []
    assert notifier.messages == []


def test_watchdog_exits_even_if_notify_raises():
    clk = FakeClock(0.0)
    hb = Heartbeat(clock=clk)
    clk.t = 700.0

    class Raising:
        def notify(self, m):
            raise RuntimeError("telegram down")

    calls = []
    watchdog_loop(
        hb,
        600.0,
        Raising(),
        on_timeout=lambda: calls.append(1),
        clock=clk,
        sleep=lambda *_: None,
    )
    assert calls == [1]  # 알림 예외가 종료를 막지 못함


def test_watchdog_exits_even_if_notify_hangs():
    clk = FakeClock(0.0)
    hb = Heartbeat(clock=clk)
    clk.t = 700.0
    ev = threading.Event()  # 절대 set 안 함 → notify 무한 블록

    class Hanging:
        def notify(self, m):
            ev.wait()

    calls = []
    watchdog_loop(
        hb,
        600.0,
        Hanging(),
        on_timeout=lambda: calls.append(1),
        clock=clk,
        sleep=lambda *_: None,
        notify_timeout_seconds=0.1,  # join-timeout 이 행 notify 를 우회
    )
    assert calls == [1]
    ev.set()  # 정리: 블록된 데몬 스레드 해제


def test_watchdog_stops_on_stop_check():
    clk = FakeClock(0.0)
    hb = Heartbeat(clock=clk)
    clk.t = 700.0  # stale 이지만 stop_check 가 즉시 True → 발화 없음(graceful shutdown)
    calls = []
    watchdog_loop(
        hb,
        600.0,
        RecorderNotifier(),
        on_timeout=lambda: calls.append(1),
        stop_check=lambda: True,
        clock=clk,
        sleep=lambda *_: None,
    )
    assert calls == []
