"""executor 통합 단위테스트 — 5개 운영 메모의 핵심 경로를 FakeExchange 로 검증."""

from datetime import datetime, timedelta

import pytest

from src.executor import reconcile, run_poll_once
from src.exchange import OrderResult
from src.notifier import NullNotifier
from src.strategy import (
    Fill,
    OpenLimit,
    Params,
    State,
    Status,
    start_cycle,
)

T0 = datetime(2026, 6, 1, 0, 0, 0)
P = Params()
N = NullNotifier()
FEE = 0.001


class FakeExchange:
    """스크립트 가능한 거래소 더블. 주문 체결을 테스트에서 직접 조작."""

    def __init__(
        self,
        price,
        free_usdt=0.0,
        base_balance=0.0,
        min_notional=5.0,
        lot=1e-6,
        tick=0.01,
    ):
        self.price = price
        self.free_usdt = free_usdt
        self.base_balance = base_balance
        self._min, self._lot, self._tick = min_notional, lot, tick
        self.orders: dict[str, dict] = {}
        self.seq = 0
        self.market_buys: list[float] = []
        self.market_sells: list[float] = []
        self._fill_on_cancel: dict[str, tuple[float, float]] = {}

    def min_notional(self):
        return self._min

    def lot_step(self):
        return self._lot

    def price_tick(self):
        return self._tick

    def fetch_price(self):
        return self.price

    def fetch_free_usdt(self):
        return self.free_usdt

    def fetch_base_balance(self):
        return self.base_balance

    def market_buy_quote(self, usdt):
        self.market_buys.append(usdt)
        qty = usdt / self.price * (1 - FEE)
        self.free_usdt -= usdt
        self.base_balance += qty
        return Fill(price=self.price, qty=qty, cost=usdt)

    def market_sell_all(self, qty):
        self.market_sells.append(qty)
        proceeds = qty * self.price * (1 - FEE)
        self.base_balance -= qty
        self.free_usdt += proceeds
        return proceeds

    def place_limit_buy(self, price, usdt):
        self.seq += 1
        oid = f"f-{self.seq}"
        self.orders[oid] = {
            "price": price,
            "amount": usdt / price,
            "status": "open",
            "fb": 0.0,
            "fq": 0.0,
        }
        return OpenLimit(id=oid, price=price, qty=usdt / price, usdt=usdt)

    def fetch_order(self, oid):
        o = self.orders[oid]
        return OrderResult(oid, o["status"], o["fb"], o["fq"], o["price"])

    def cancel_order(self, oid):
        if oid in self._fill_on_cancel:  # 취소-체결 race 시뮬레이션
            fb, fq = self._fill_on_cancel.pop(oid)
            o = self.orders[oid]
            o.update(fb=fb, fq=fq, status="closed")
            return OrderResult(oid, "closed", fb, fq, o["price"])
        o = self.orders[oid]
        res = self.fetch_order(oid)
        if o["status"] == "open":
            o["status"] = "canceled"
            return OrderResult(oid, "canceled", o["fb"], o["fq"], o["price"])
        return res

    # --- 테스트 헬퍼 ---
    def set_order_filled(self, oid, fb, fq, status="closed"):
        self.orders[oid] = {
            "price": self.orders.get(oid, {}).get("price", 0.0),
            "amount": 0.0,
            "status": status,
            "fb": fb,
            "fq": fq,
        }

    def program_fill_on_cancel(self, oid, fb, fq):
        self._fill_on_cancel[oid] = (fb, fq)


def mid_state(**over) -> State:
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


# --- 메모 4: 시장가 완결 + 부트스트랩 ------------------------------------------
def test_bootstrap_drives_market_buy_and_places_limit():
    ex = FakeExchange(price=50000.0, free_usdt=4000.0)
    state = start_cycle(4000.0, P, cycle_id=1)
    state = run_poll_once(state, ex, P, N, T0, atr14=500.0)
    assert ex.market_buys == [100.0]
    assert state.tranches_used == 1
    assert state.ref == 50000.0
    assert state.open_limit is not None
    assert state.open_limit.price == pytest.approx(48500.0)  # 50000×(1−0.03)


# --- 메모 1: 익절 후 새 사이클 현금 = 순매도대금 + 미투입현금 --------------------
def test_tp_restart_cash_includes_undeployed():
    # 미투입현금 3900 + 매도대금. free_usdt 가 거래소 진실(메모 5).
    ex = FakeExchange(price=56000.0, free_usdt=3900.0, base_balance=0.002)
    state = mid_state()
    state = run_poll_once(state, ex, P, N, T0, atr14=500.0)
    assert ex.market_sells == [0.002]
    proceeds = 0.002 * 56000.0 * (1 - FEE)
    expected_cash = 3900.0 + proceeds
    assert state.cycle_id == 2
    assert state.tranche_usdt == pytest.approx(
        expected_cash / 40
    )  # ≈100.3, 매도대금만이면 ≈2.8
    assert state.tranche_usdt > 100  # 미투입현금 누락 안 됨


# --- 메모 2: 취소-체결 race 시 중복매수 금지 -----------------------------------
def test_oco_race_does_not_double_buy():
    ex = FakeExchange(
        price=49000.0, free_usdt=3900.0, base_balance=0.002
    )  # 49000 → TP 아님
    ol = ex.place_limit_buy(48500.0, 100.0)
    state = mid_state(open_limit=ol, step_target_usdt=100.0, step_filled_usdt=0.0)
    # 지정가가 취소와 동시에 '전량 체결'(100 USDT) — 24h 폴백 직전 race
    ex.program_fill_on_cancel(ol.id, fb=100.0 / 48500.0 * (1 - FEE), fq=100.0)
    now = T0 + timedelta(hours=24)
    state = run_poll_once(state, ex, P, N, now, atr14=500.0)
    assert ex.market_buys == []  # 잔여 0 → 시장가 매수 건너뜀(중복매수 방지)
    assert state.tranches_used == 2  # race 체결로 스텝 1개만 완료


# --- 부분체결 폴딩(refresh) → 완료 시 ref/카운트 갱신 ---------------------------
def test_refresh_folds_partial_then_completion():
    ex = FakeExchange(price=48400.0, free_usdt=3900.0, base_balance=0.002)
    ol = ex.place_limit_buy(48500.0, 100.0)
    state = mid_state(open_limit=ol, step_target_usdt=100.0, step_filled_usdt=0.0)
    # 1) 부분체결 40 USDT 관측
    ex.orders[ol.id].update(fb=40.0 / 48500.0 * (1 - FEE), fq=40.0, status="open")
    state = run_poll_once(state, ex, P, N, T0 + timedelta(hours=1), atr14=500.0)
    assert state.open_limit is not None  # 미완료 → 유지
    assert state.tranches_used == 1
    assert state.step_filled_usdt == pytest.approx(40.0)
    assert state.ref == 50000.0  # 미완료 → ref 불변
    # 2) 잔여 60 USDT 추가 체결 → 완료
    ex.orders[ol.id].update(fb=100.0 / 48500.0 * (1 - FEE), fq=100.0, status="closed")
    state = run_poll_once(state, ex, P, N, T0 + timedelta(hours=2), atr14=500.0)
    assert state.tranches_used == 2
    assert state.open_limit is not None  # 완료 후 새 지정가 설치됨
    assert state.ref == pytest.approx(48500.0, rel=1e-3)  # 직전(증분) 체결가


# --- 메모 5 / Q1: reconcile -----------------------------------------------------
def test_reconcile_fresh_start():
    ex = FakeExchange(price=50000.0, free_usdt=4000.0)
    state = reconcile(None, ex, P, N, T0)
    assert state.status == Status.RUNNING
    assert state.tranche_usdt == pytest.approx(100.0)


def test_reconcile_insufficient_funds_halts():
    ex = FakeExchange(price=50000.0, free_usdt=3.0)
    state = reconcile(None, ex, P, N, T0)
    assert state.status == Status.HALTED


def test_reconcile_balance_mismatch_halts():
    ex = FakeExchange(
        price=50000.0, free_usdt=3900.0, base_balance=0.0009
    )  # 저장 0.002 와 큰 괴리
    state = reconcile(mid_state(), ex, P, N, T0)
    assert state.status == Status.HALTED


def test_reconcile_auto_resumes_offline_fill():
    ex = FakeExchange(price=49000.0, free_usdt=3800.0, base_balance=0.004)
    ol = OpenLimit(id="f-1", price=48500.0, qty=0.00206, usdt=100.0)
    ex.orders["f-1"] = {
        "price": 48500.0,
        "amount": 0.00206,
        "status": "closed",
        "fb": 0.002,
        "fq": 100.0,
    }
    stored = mid_state(open_limit=ol, step_target_usdt=100.0, step_filled_usdt=0.0)
    state = reconcile(stored, ex, P, N, T0)
    assert state.status == Status.RUNNING  # 오프라인 체결 자동 반영, 잔고 일치 → 재개
    assert state.tranches_used == 2
    assert state.open_limit is None
