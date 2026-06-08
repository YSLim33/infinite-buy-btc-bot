"""라이브니스 워치독 — 폴 루프가 조용히 멈추면 자동 복구(프로세스 종료→재시작)하고 즉시 알림.

배경: 폴 루프가 time.sleep(clock_nanosleep)에서 ~19h hang 했는데 컨테이너·status 는 정상으로 보여
restart:always 가 무력했다(프로세스 생존 / 루프 정지). 이 모듈은 메인 루프의 박동(heartbeat)을
별도 데몬 스레드로 감시해, 일정 시간 무박동이면 os._exit(1) → 컨테이너/systemd 가 복구하게 한다.

설계: 순수 헬퍼(is_stale)는 단위테스트로 검증하고, 부수효과(파일 touch·notify·_exit)는 주입으로
분리해 스레드/프로세스 종료 없이도 테스트 가능. monotonic 클럭 사용 — NTP/벽시계 스텝에 면역
(원래 hang 이 CLOCK_MONOTONIC 의 clock_nanosleep 이었음 → false-positive 방지).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("june_bot.watchdog")


def is_stale(age_seconds: float, timeout_seconds: float) -> bool:
    """마지막 박동 이후 경과(age)가 임계(timeout) 이상이면 정지로 판단."""
    return age_seconds >= timeout_seconds


class Heartbeat:
    """메인 폴 루프가 매 반복 beat() 로 갱신하는 박동. 워치독 스레드가 age() 로 읽는다.

    monotonic 타임스탬프(클럭 주입 가능)를 락으로 보호. path 가 주어지면 박동 파일에도 기록해
    외부 모니터가 파일 mtime 으로 신선도를 볼 수 있게 한다(워치독 자체는 in-memory ts 만 사용).
    """

    def __init__(
        self, path: str | None = None, clock: Callable[[], float] = time.monotonic
    ):
        self._clock = clock
        self._path = path
        self._lock = threading.Lock()
        self._ts = clock()  # 생성 시 baseline 박동

    def beat(self) -> None:
        now = self._clock()
        with self._lock:
            self._ts = now
        if self._path:  # 외부 가시성용 — 실패해도 매매에 영향 없게 swallow
            try:
                with open(self._path, "w") as f:
                    f.write(str(now))
            except Exception as e:
                log.warning("heartbeat file write failed: %s", e)

    def age(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        with self._lock:
            return now - self._ts


def _safe_notify(notifier, message: str) -> None:
    try:
        notifier.notify(message)
    except Exception as e:  # 알림 실패가 종료를 막아선 안 됨
        log.warning("watchdog notify failed: %s", e)


def _notify_bounded(notifier, message: str, timeout_seconds: float) -> None:
    """notify 를 별도 데몬 스레드로 보내고 join-timeout — 느린/행 telegram(예: DNS)이
    os._exit 을 지연시키지 못하게. timeout 안에 안 끝나도 반환(이후 on_timeout 이 처리)."""
    t = threading.Thread(target=_safe_notify, args=(notifier, message), daemon=True)
    t.start()
    t.join(timeout_seconds)


def watchdog_loop(
    heartbeat: Heartbeat,
    timeout_seconds: float,
    notifier,
    *,
    on_timeout: Callable[[], None] = lambda: os._exit(1),
    stop_check: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: float = 15.0,
    notify_timeout_seconds: float = 5.0,
) -> None:
    """interval 마다 박동 신선도 검사. timeout 이상 무박동이면 로그+알림 후 on_timeout().

    on_timeout 기본은 os._exit(1)(반환하지 않음 → 프로세스 종료, restart:always 가 복구).
    stop_check()가 True 면(graceful shutdown) 발화 없이 종료. 클럭/sleep/on_timeout 은 테스트 주입용.
    """
    while not stop_check():
        age = heartbeat.age(clock())
        if is_stale(age, timeout_seconds):
            msg = (
                f"[WATCHDOG] poll loop stalled {age:.0f}s "
                f">= {timeout_seconds:.0f}s — restarting process"
            )
            log.error(msg)
            _notify_bounded(notifier, msg, notify_timeout_seconds)
            on_timeout()
            return  # 프로덕션에선 도달 안 함(os._exit). 주입된 on_timeout 이 반환하면(테스트) 종료.
        sleep(interval_seconds)


def start_watchdog(
    heartbeat: Heartbeat, timeout_seconds: float, notifier, **kwargs
) -> threading.Thread:
    """워치독을 데몬 스레드로 시작(인터프리터 종료를 막지 않음)."""
    t = threading.Thread(
        target=watchdog_loop,
        args=(heartbeat, timeout_seconds, notifier),
        kwargs=kwargs,
        daemon=True,
        name="watchdog",
    )
    t.start()
    return t
