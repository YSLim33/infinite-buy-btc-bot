"""executor 통합 단위테스트 — 5개 운영 메모의 핵심 경로를 FakeExchange 로 검증."""

from datetime import datetime, timedelta

import pytest

from src.executor import check_and_apply_topup, reconcile, run_poll_once
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

    def fetch_total_usdt(self):
        return self.free_usdt  # fake 는 예약 모델 없음 → total == free

    def fetch_base_balance(self):
        return self.base_balance

    def _sync_balance(self, o):
        """관측된 체결 증분만큼 잔고 반영(실거래소 정합 — free 가 cycle_cash 와 동기)."""
        dq = o["fq"] - o.get("afq", 0.0)
        db = o["fb"] - o.get("afb", 0.0)
        if dq or db:
            self.free_usdt -= dq
            self.base_balance += db
            o["afq"], o["afb"] = o["fq"], o["fb"]

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
        self._sync_balance(o)
        return OrderResult(oid, o["status"], o["fb"], o["fq"], o["price"])

    def cancel_order(self, oid):
        if oid in self._fill_on_cancel:  # 취소-체결 race 시뮬레이션
            fb, fq = self._fill_on_cancel.pop(oid)
            o = self.orders[oid]
            o.update(fb=fb, fq=fq, status="closed")
            self._sync_balance(o)
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
    # 체결 전 잔고로 시작 → fetch_order 가 오프라인 체결을 잔고에 반영(free 3900→3800, base 0.002→0.004)
    ex = FakeExchange(price=49000.0, free_usdt=3900.0, base_balance=0.002)
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


# --- Stage 2.5: 잔고변화(입금/출금) 감지 → 40 재분할 --------------------------
P_NOBUY = Params(topup_immediate_buy_on_deposit=False)


def test_topup_resplit_no_immediate_preserves_position():
    ex = FakeExchange(price=50000.0, free_usdt=5000.0)  # 입금: 5000 vs cycle_cash 3900
    state = mid_state(cycle_cash_remaining=3900.0)
    out = check_and_apply_topup(state, ex, P_NOBUY, N, T0)
    assert out.cycle_cash_remaining == pytest.approx(5000.0)
    assert out.tranche_usdt == pytest.approx(125.0)  # 5000/40
    assert out.tranches_used == 0
    assert out.position_qty == 0.002  # 포지션 보존
    assert out.invested_usdt == 100.0
    assert out.ref == 50000.0  # 기존 ref 보존(즉시매수 없음)
    assert ex.market_buys == []


def test_topup_immediate_buy_sets_new_ref():
    ex = FakeExchange(price=50000.0, free_usdt=5000.0)
    state = mid_state(cycle_cash_remaining=3900.0, ref=48000.0)
    out = check_and_apply_topup(state, ex, P, N, T0)  # P: immediate=True
    assert len(ex.market_buys) == 1
    assert ex.market_buys[0] == pytest.approx(125.0)  # 1 tranche
    assert out.tranches_used == 1  # 즉시 1매수 완료
    assert out.ref == 50000.0  # 새 ref = 체결가
    assert out.position_qty == pytest.approx(0.002 + 125.0 / 50000.0 * (1 - FEE))


def test_topup_resumes_from_cash_exhausted():
    ex = FakeExchange(price=50000.0, free_usdt=1000.0)
    state = mid_state(
        status=Status.CASH_EXHAUSTED,
        tranches_used=40,
        cycle_cash_remaining=0.5,
        position_qty=0.08,
        invested_usdt=4000.0,
    )
    out = check_and_apply_topup(state, ex, P_NOBUY, N, T0)
    assert out.status == Status.RUNNING
    assert out.tranches_used == 0
    assert out.cycle_cash_remaining == pytest.approx(1000.0)
    assert out.position_qty == 0.08


def test_topup_below_threshold_ignored():
    ex = FakeExchange(price=50000.0, free_usdt=3905.0)  # +5 < 임계 10
    state = mid_state(cycle_cash_remaining=3900.0)
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.cycle_cash_remaining == 3900.0
    assert out.tranches_used == 1
    assert ex.market_buys == []


def test_topup_no_false_positive_with_resting_limit():
    ex = FakeExchange(price=49000.0, free_usdt=3900.0)
    ol = ex.place_limit_buy(48500.0, 100.0)  # resting (total==free==cycle_cash)
    state = mid_state(
        open_limit=ol, step_target_usdt=100.0, cycle_cash_remaining=3900.0
    )
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.open_limit is ol  # 변화 없음
    assert out.tranches_used == 1
    assert ex.market_buys == []


def test_topup_not_triggered_by_bot_own_buy():
    # 봇이 100 매수 후: free·cycle_cash 가 함께 100 감소 → delta 0
    ex = FakeExchange(price=50000.0, free_usdt=3800.0)
    state = mid_state(cycle_cash_remaining=3800.0)
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert ex.market_buys == []
    assert out.cycle_cash_remaining == 3800.0


def test_topup_idempotent():
    ex = FakeExchange(price=50000.0, free_usdt=5000.0)
    state = mid_state(cycle_cash_remaining=3900.0)
    out1 = check_and_apply_topup(state, ex, P_NOBUY, N, T0)
    assert out1.cycle_cash_remaining == pytest.approx(5000.0)
    out2 = check_and_apply_topup(out1, ex, P_NOBUY, N, T0)  # 재호출 → 재트리거 없음
    assert out2.cycle_cash_remaining == pytest.approx(5000.0)
    assert out2.tranches_used == 0
    assert out2.tranche_usdt == out1.tranche_usdt


def test_topup_on_boot_reconcile():
    # 다운 중 입금: 저장 cycle_cash 3900, 거래소 free 5000
    ex = FakeExchange(price=50000.0, free_usdt=5000.0, base_balance=0.002)
    stored = mid_state(cycle_cash_remaining=3900.0, position_qty=0.002)
    out = reconcile(stored, ex, P_NOBUY, N, T0)
    assert out.status == Status.RUNNING
    assert out.cycle_cash_remaining == pytest.approx(5000.0)
    assert out.tranches_used == 0
    assert out.position_qty == 0.002


def test_topup_small_decrease_ignored():
    ex = FakeExchange(price=50000.0, free_usdt=3895.0)  # -5 (|Δ| < 임계 10)
    state = mid_state(cycle_cash_remaining=3900.0)
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.cycle_cash_remaining == 3900.0  # 변화 없음(클램프 안 함)
    assert out.tranches_used == 1
    assert ex.market_buys == []


def test_topup_withdrawal_50pct_resplits_no_buy():
    ex = FakeExchange(price=50000.0, free_usdt=1950.0)  # ~50% 출금
    state = mid_state(
        cycle_cash_remaining=3900.0, position_qty=0.002, invested_usdt=100.0
    )
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.status == Status.RUNNING  # HALT 아님
    assert out.cycle_cash_remaining == pytest.approx(1950.0)  # 축소 재분할
    assert out.tranche_usdt == pytest.approx(48.75)  # 1950/40
    assert out.tranches_used == 0
    assert out.position_qty == 0.002  # 포지션 보존
    assert out.invested_usdt == 100.0  # 평단 보존
    assert out.ref == 50000.0  # ref 보존
    assert ex.market_buys == []  # 출금은 즉시매수 없음


def test_topup_withdrawal_below_min_cash_exhausted():
    # 거의 전액 출금 → 가용 < min_notional, 포지션 보유 → CASH_EXHAUSTED (HALT 아님)
    ex = FakeExchange(price=50000.0, free_usdt=2.0)
    state = mid_state(cycle_cash_remaining=3900.0, position_qty=0.002)
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.status == Status.CASH_EXHAUSTED
    assert out.position_qty == 0.002  # 포지션 보존
    assert ex.market_buys == []


def test_topup_skipped_when_halted():
    ex = FakeExchange(price=50000.0, free_usdt=5000.0)
    state = mid_state(status=Status.HALTED, cycle_cash_remaining=3900.0)
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.status == Status.HALTED
    assert out.cycle_cash_remaining == 3900.0
    assert ex.market_buys == []


def test_topup_skipped_at_tp():
    # 입금 + TP 동시: topup 건너뜀(TP 가 새 사이클에서 입금 흡수)
    ex = FakeExchange(price=60000.0, free_usdt=5000.0, base_balance=0.002)
    state = mid_state(
        cycle_cash_remaining=3900.0
    )  # 0.002 @ invested 100 → 60000 에서 TP
    out = check_and_apply_topup(state, ex, P, N, T0)
    assert out.cycle_cash_remaining == 3900.0  # 재분할 안 함
    assert ex.market_buys == []


def test_run_poll_once_topup_then_chase():
    ex = FakeExchange(price=50000.0, free_usdt=5000.0)
    state = mid_state(cycle_cash_remaining=3900.0, ref=50000.0)
    state = run_poll_once(state, ex, P, N, T0, atr14=500.0)
    assert len(ex.market_buys) == 1  # 즉시 1매수
    assert state.tranches_used == 1
    assert state.open_limit is not None  # 새 ref 로 추격 지정가 설치
