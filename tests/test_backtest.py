"""백테스트 엔진 스모크 — 동일 코어로 재생되고 합리적 통계를 반환하는지."""

from datetime import datetime, timezone

from src.backtest import run_backtest
from src.strategy import Params

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
BASE = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _daily(n=20, px=50000.0, r=250.0):
    # 평탄한 종가 + 일정 레인지 → ATR ≈ r (X 는 하한 3%)
    return [[BASE + i * DAY_MS, px, px + r, px - r, px, 10.0] for i in range(n)]


def _hourly():
    # 일봉 이후 시점. 50000 부트스트랩 → 48000 으로 하락(지정가 48500 체결) → 56000 익절.
    start = BASE + 25 * DAY_MS
    prices = [50000] * 2 + [48000] * 4 + [50000] * 2 + [56000] * 4 + [55000] * 4
    bars = []
    for i, c in enumerate(prices):
        ts = start + i * HOUR_MS
        bars.append([ts, c, c + 100, c - 100, c, 5.0])
    return bars


def test_backtest_runs_and_reports():
    stats = run_backtest(_daily(), _hourly(), Params(), seed_usdt=4000.0, atr_period=14)
    assert set(stats) >= {
        "final_equity",
        "return_pct",
        "max_drawdown_pct",
        "cycles_completed",
    }
    assert stats["final_equity"] > 0
    assert stats["max_drawdown_pct"] >= 0
    assert stats["bars"] == len(_hourly())


def test_backtest_takes_profit_at_least_once():
    # 56000 까지 오르면 평단(≈49k)+10% 익절이 최소 1회 발생해야 함
    stats = run_backtest(_daily(), _hourly(), Params(), seed_usdt=4000.0, atr_period=14)
    assert stats["cycles_completed"] >= 1
