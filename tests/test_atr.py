"""ATR14 / X 산정 단위테스트 (§2.5)."""

import pytest

from src.atr import compute_atr, compute_x, true_ranges, wilder_atr


def test_true_ranges_basic():
    highs = [10, 12, 11]
    lows = [8, 9, 7]
    closes = [9, 11, 8]
    # i=1: max(12-9, |12-9|, |9-9|) = 3 ; i=2: max(11-7, |11-11|, |7-11|) = 4
    assert true_ranges(highs, lows, closes) == [3, 4]


def test_true_ranges_length_mismatch_raises():
    with pytest.raises(ValueError):
        true_ranges([1, 2], [1], [1, 2])


def test_wilder_atr_constant_tr():
    # 매 봉 TR = 1.0 이도록 구성 → ATR = 1.0
    highs = [100.5] * 16
    lows = [99.5] * 16
    closes = [100.0] * 16
    assert wilder_atr(highs, lows, closes, period=14) == pytest.approx(1.0)


def test_wilder_atr_insufficient_candles_raises():
    with pytest.raises(ValueError):
        wilder_atr([1] * 10, [1] * 10, [1] * 10, period=14)


def test_compute_atr_from_ohlcv_rows():
    rows = [[0, 100.0, 100.5, 99.5, 100.0, 1.0] for _ in range(16)]
    assert compute_atr(rows, period=14) == pytest.approx(1.0)


def test_compute_x_applies_floor():
    # 2 × 1 / 100 = 0.02 < 0.03 → 하한 0.03
    assert compute_x(atr=1.0, price=100.0) == pytest.approx(0.03)


def test_compute_x_above_floor_no_cap():
    assert compute_x(atr=2.0, price=100.0) == pytest.approx(0.04)
    assert compute_x(atr=50.0, price=100.0) == pytest.approx(1.0)  # 상한 없음


def test_compute_x_invalid_price_raises():
    with pytest.raises(ValueError):
        compute_x(atr=1.0, price=0.0)
