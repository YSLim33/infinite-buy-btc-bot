"""순수 전략 코어 단위테스트 — 거래소 모킹 없이 상태/의사결정 검증 (CLAUDE.md §2)."""

from datetime import datetime, timedelta

import pytest

from src.strategy import (
    CancelLimit,
    Fill,
    Hold,
    MarketBuy,
    Market,
    OpenLimit,
    Params,
    PlaceLimitBuy,
    SellAll,
    State,
    Status,
    apply_buy_fill,
    apply_topup,
    can_buy,
    compute_tranche,
    decide,
    meets_min_notional,
    on_limit_canceled,
    on_limit_placed,
    round_down_amount,
    round_price,
    should_take_profit,
    start_cycle,
    tp_trigger_price,
)

T0 = datetime(2026, 6, 1, 0, 0, 0)
P = (
    Params()
)  # n=40, tp=0.10, fee=0.001, x_mult=2, x_floor=0.03, fallback=24h, min_notional=5


def S(**over) -> State:
    """중간 사이클(1 tranche 완료, 추격 대기) 기본 상태 + 오버라이드."""
    base = dict(
        status=Status.RUNNING,
        cycle_id=1,
        tranche_usdt=100.0,
        tranches_used=1,
        cycle_cash_remaining=3900.0,
        ref=50000.0,
        last_fill_time=T0,
        position_qty=0.002,
        invested_usdt=100.0,
        step_target_usdt=0.0,
        step_filled_usdt=0.0,
        open_limit=None,
    )
    base.update(over)
    return State(**base)


def mkt(price=49000.0, atr14=500.0, now=T0):
    return Market(price=price, atr14=atr14, now=now)


# --- tranche / 사이클 시작·재분할 (§2.2, §2.6) ---------------------------------
def test_compute_tranche():
    assert compute_tranche(4000.0, 40) == 100.0


def test_start_cycle_divides_and_prepares_bootstrap():
    s = start_cycle(4000.0, P, cycle_id=1)
    assert s.tranche_usdt == 100.0
    assert s.status == Status.RUNNING
    assert s.ref is None and s.position_qty == 0.0
    assert s.tranches_used == 0
    assert s.step_target_usdt == 100.0  # 부트스트랩 1매수 스텝
    assert s.cycle_cash_remaining == 4000.0


def test_re_division_on_new_cycle():
    # 익절로 현금 4400 이 되어 새 사이클 시작 → tranche 재분할 (§2.6 → §2.2)
    s = start_cycle(4400.0, P, cycle_id=2)
    assert s.tranche_usdt == 110.0
    assert s.cycle_id == 2
    assert s.tranches_used == 0


# --- 부트스트랩 즉시 1매수 (§2.3) ----------------------------------------------
def test_decide_bootstrap_market_buy():
    s = start_cycle(4000.0, P, cycle_id=1)
    acts = decide(s, mkt(), P)
    assert acts == [MarketBuy(100.0)]


def test_apply_buy_fill_completes_step_sets_ref_and_timer():
    s = start_cycle(4000.0, P, cycle_id=1)
    f = Fill(
        price=50000.0, qty=0.002, cost=100.0
    )  # 수수료 BTC 차감분 반영된 순수량 가정
    t1 = T0 + timedelta(minutes=1)
    s2 = apply_buy_fill(s, f, t1, P)
    assert s2.tranches_used == 1
    assert s2.ref == 50000.0
    assert s2.last_fill_time == t1
    assert s2.position_qty == pytest.approx(0.002)
    assert s2.invested_usdt == pytest.approx(100.0)
    assert s2.cycle_cash_remaining == pytest.approx(3900.0)
    assert s2.step_target_usdt == 0.0 and s2.step_filled_usdt == 0.0
    assert s2.open_limit is None
    assert s2.status == Status.RUNNING


# --- 추격 지정가 설치 (§2.4, §2.5) ---------------------------------------------
def test_decide_places_chase_limit_floor_x():
    # atr=500, price=50000 → 2*500/50000=0.02 < 0.03 → X=0.03 → 50000*0.97=48500
    acts = decide(S(), mkt(price=50000.0, atr14=500.0), P)
    assert acts == [PlaceLimitBuy(48500.0, 100.0)]


def test_decide_places_chase_limit_above_floor():
    # atr=1000, price=50000 → 0.04 → 50000*0.96=48000
    acts = decide(S(), mkt(price=50000.0, atr14=1000.0), P)
    assert acts == [PlaceLimitBuy(48000.0, 100.0)]


def test_on_limit_placed_starts_step():
    s = on_limit_placed(S(), OpenLimit(id="L1", price=48500.0, qty=0.002, usdt=100.0))
    assert s.open_limit.id == "L1"
    assert s.step_target_usdt == 100.0
    assert s.step_filled_usdt == 0.0


def test_decide_holds_while_limit_resting():
    s = on_limit_placed(S(), OpenLimit("L1", 48500.0, 0.002, 100.0))
    assert decide(s, mkt(price=49000.0, now=T0 + timedelta(hours=1)), P) == [Hold()]


# --- 부분체결: 스텝 완료 시에만 ref/타이머/카운트 갱신 ---------------------------
def test_partial_fill_keeps_step_then_completion_updates_ref():
    s = on_limit_placed(S(), OpenLimit("L1", 48500.0, 0.002, 100.0))
    # 1) 부분체결 25 USDT
    s1 = apply_buy_fill(
        s, Fill(price=48500.0, qty=0.000515, cost=25.0), T0 + timedelta(hours=2), P
    )
    assert s1.step_filled_usdt == pytest.approx(25.0)
    assert s1.ref == 50000.0  # 미완료 → ref 불변
    assert s1.last_fill_time == T0  # 타이머 불변
    assert s1.tranches_used == 1
    assert s1.open_limit is not None  # 잔여 지정가 유지
    assert s1.invested_usdt == pytest.approx(125.0)
    # 2) 잔여 75 USDT 체결 → 스텝 완료
    t_done = T0 + timedelta(hours=3)
    s2 = apply_buy_fill(s1, Fill(price=48500.0, qty=0.001546, cost=75.0), t_done, P)
    assert s2.step_filled_usdt == 0.0
    assert s2.ref == 48500.0  # 직전 체결가로 갱신
    assert s2.last_fill_time == t_done  # 24h 타이머 리셋
    assert s2.tranches_used == 2
    assert s2.open_limit is None


# --- 24h 폴백 (§2.4) -----------------------------------------------------------
def test_24h_fallback_market_buys_remaining():
    s = on_limit_placed(S(last_fill_time=T0), OpenLimit("L1", 48500.0, 0.002, 100.0))
    now = T0 + timedelta(hours=24)
    acts = decide(s, mkt(price=49000.0, now=now), P)  # 49000 → TP 아님
    assert acts == [CancelLimit("L1"), MarketBuy(100.0)]


def test_24h_fallback_buys_only_unfilled_portion():
    s = on_limit_placed(S(last_fill_time=T0), OpenLimit("L1", 48500.0, 0.002, 100.0))
    s = apply_buy_fill(
        s, Fill(48500.0, 0.000618, 30.0), T0 + timedelta(hours=1), P
    )  # 부분 30
    now = T0 + timedelta(hours=25)
    acts = decide(s, mkt(price=49000.0, now=now), P)
    assert acts == [CancelLimit("L1"), MarketBuy(70.0)]


def test_no_fallback_before_24h():
    s = on_limit_placed(S(last_fill_time=T0), OpenLimit("L1", 48500.0, 0.002, 100.0))
    now = T0 + timedelta(hours=23, minutes=59)
    assert decide(s, mkt(price=49000.0, now=now), P) == [Hold()]


# --- 익절(TP) 순수익 기준 + 최우선순위 (§2.6) ----------------------------------
def test_should_take_profit_no_position():
    assert (
        should_take_profit(S(position_qty=0.0, invested_usdt=0.0), 99999.0, P) is False
    )


def test_tp_trigger_price_and_boundary():
    s = S()  # qty 0.002, invested 100
    trig = tp_trigger_price(s, P)  # 100*1.10/(0.002*0.999) ≈ 55055.06
    assert trig == pytest.approx(55055.0550, rel=1e-6)
    assert should_take_profit(s, trig + 0.01, P) is True
    assert should_take_profit(s, 55000.0, P) is False  # net 109.89 < 110


def test_tp_takes_priority_over_fallback_and_cancels_limit():
    s = on_limit_placed(S(last_fill_time=T0), OpenLimit("L1", 48500.0, 0.002, 100.0))
    now = T0 + timedelta(hours=30)  # 24h 도 지났지만 TP 우선
    acts = decide(s, mkt(price=56000.0, now=now), P)  # 56000 > trigger
    assert acts == [CancelLimit("L1"), SellAll()]


def test_tp_sell_all_without_open_limit():
    acts = decide(S(open_limit=None), mkt(price=56000.0), P)
    assert acts == [SellAll()]


# --- 현금 소진 (§2.7) ----------------------------------------------------------
def test_cash_exhausted_on_last_tranche():
    s = S(
        tranches_used=39,
        step_target_usdt=100.0,
        step_filled_usdt=0.0,
        cycle_cash_remaining=100.0,
    )
    s2 = apply_buy_fill(s, Fill(48000.0, 0.00208, 100.0), T0 + timedelta(hours=1), P)
    assert s2.tranches_used == 40
    assert s2.status == Status.CASH_EXHAUSTED


def test_cash_exhausted_when_remaining_below_min():
    s = S(
        tranches_used=10,
        step_target_usdt=100.0,
        step_filled_usdt=0.0,
        cycle_cash_remaining=103.0,
    )
    s2 = apply_buy_fill(s, Fill(48000.0, 0.00208, 100.0), T0 + timedelta(hours=1), P)
    assert s2.cycle_cash_remaining == pytest.approx(3.0)
    assert s2.status == Status.CASH_EXHAUSTED


def test_decide_exhausted_holds_but_still_watches_tp():
    s = S(status=Status.CASH_EXHAUSTED)
    assert decide(s, mkt(price=49000.0), P) == [Hold()]  # 신규 매수 없음
    assert decide(s, mkt(price=56000.0), P) == [SellAll()]  # TP 는 계속 감시


def test_can_buy_false_when_remaining_below_min_notional():
    assert can_buy(S(cycle_cash_remaining=4.0), P) is False
    assert can_buy(S(cycle_cash_remaining=3900.0), P) is True


def test_can_buy_false_when_all_tranches_used():
    assert can_buy(S(tranches_used=40), P) is False


# --- 안전 정지 ------------------------------------------------------------------
def test_decide_halted_holds():
    assert decide(S(status=Status.HALTED), mkt(price=56000.0), P) == [Hold()]


def test_on_limit_canceled_keeps_step_progress():
    s = on_limit_placed(S(), OpenLimit("L1", 48500.0, 0.002, 100.0))
    s = apply_buy_fill(s, Fill(48500.0, 0.000515, 25.0), T0 + timedelta(hours=1), P)
    s = on_limit_canceled(s)
    assert s.open_limit is None
    assert s.step_target_usdt == 100.0  # 스텝 진척 유지
    assert s.step_filled_usdt == pytest.approx(25.0)


# --- precision / minNotional 가드 (§4) -----------------------------------------
def test_round_down_amount():
    assert round_down_amount(0.0023456, 0.0001) == pytest.approx(0.0023)
    assert round_down_amount(1.27, 0.5) == pytest.approx(1.0)


def test_round_price():
    assert round_price(48512.3, 0.1) == pytest.approx(48512.3)
    assert round_price(48512.34, 0.5) == pytest.approx(48512.5)


def test_meets_min_notional():
    assert meets_min_notional(50000.0, 0.0001, 5.0) is True  # 5.0 >= 5
    assert meets_min_notional(50000.0, 0.00009, 5.0) is False  # 4.5 < 5


# --- 입금 재분할 apply_topup (Stage 2.5) ---------------------------------------
def test_apply_topup_resplits_and_preserves_position():
    s = S(
        tranches_used=10,
        cycle_cash_remaining=2000.0,
        position_qty=0.05,
        invested_usdt=2500.0,
        ref=48000.0,
        last_fill_time=T0,
        cycle_id=3,
    )
    out = apply_topup(s, 5000.0, P)
    # 재분할
    assert out.cycle_cash_remaining == 5000.0
    assert out.tranche_usdt == pytest.approx(5000.0 / 40)
    assert out.tranches_used == 0
    assert out.status == Status.RUNNING
    assert out.step_target_usdt == 0.0 and out.step_filled_usdt == 0.0
    # 보존
    assert out.position_qty == 0.05
    assert out.invested_usdt == 2500.0
    assert out.ref == 48000.0
    assert out.last_fill_time == T0
    assert out.cycle_id == 3


def test_apply_topup_resumes_from_cash_exhausted():
    s = S(
        status=Status.CASH_EXHAUSTED,
        tranches_used=40,
        cycle_cash_remaining=0.5,
        position_qty=0.08,
        invested_usdt=4000.0,
    )
    out = apply_topup(s, 1000.0, P)
    assert out.status == Status.RUNNING
    assert out.tranches_used == 0
    assert out.tranche_usdt == pytest.approx(25.0)
    assert out.position_qty == 0.08  # 포지션 보존
    assert out.invested_usdt == 4000.0


def test_apply_topup_cash_exhausted_when_below_min_with_position():
    # 출금으로 가용 < min_notional & 포지션 보유 → CASH_EXHAUSTED (신규매수 중단, TP 계속)
    s = S(position_qty=0.05, invested_usdt=2500.0)
    out = apply_topup(s, 2.0, P)  # 2 < min_notional(5)
    assert out.status == Status.CASH_EXHAUSTED
    assert out.tranches_used == 0
    assert out.position_qty == 0.05  # 보존
    assert out.invested_usdt == 2500.0
