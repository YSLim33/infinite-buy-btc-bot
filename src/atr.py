"""ATR14 (Wilder) 및 추격 폭 X 계산 — 순수 함수 (§2.5).

ATR 은 일봉 OHLCV 로 매일 갱신, X 는 주문 설치 시점의 ATR·현재가로 계산한다.
"""

from __future__ import annotations

from collections.abc import Sequence


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """각 봉의 True Range. 첫 봉은 직전 종가가 없어 제외 → 길이 = len-1.

    TR_t = max(high−low, |high−prev_close|, |low−prev_close|).
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs/lows/closes length mismatch")
    trs: list[float] = []
    for i in range(1, n):
        prev_close = closes[i - 1]
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return trs


def wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float:
    """Wilder 평활화 ATR 의 최신값.

    시드 = 첫 `period` 개 TR 의 단순평균, 이후 ATR_t = (ATR_{t-1}·(period-1) + TR_t) / period.
    최소 period+1 개의 봉이 필요(TR 가 period 개 이상).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    trs = true_ranges(highs, lows, closes)
    if len(trs) < period:
        raise ValueError(f"need >= {period + 1} candles, got {len(closes)}")
    atr = sum(trs[:period]) / period  # Wilder 시드
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_atr(ohlcv: Sequence[Sequence[float]], period: int = 14) -> float:
    """CCXT OHLCV 행([ts, open, high, low, close, volume])들로부터 ATR14."""
    highs = [row[2] for row in ohlcv]
    lows = [row[3] for row in ohlcv]
    closes = [row[4] for row in ohlcv]
    return wilder_atr(highs, lows, closes, period)


def compute_x(
    atr: float, price: float, mult: float = 2.0, floor: float = 0.03
) -> float:
    """추격 하락폭 X = max(floor, mult × ATR / price). 상한 없음 (§2.5)."""
    if price <= 0:
        raise ValueError("price must be positive")
    return max(floor, mult * atr / price)
