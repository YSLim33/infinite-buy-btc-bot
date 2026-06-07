"""순수 전략 코어 — 거래소 I/O 없음 (단위테스트·백테스트가 그대로 재사용).

설계: `decide(state, market, params)` 가 현재 상태를 보고 다음 행동(Action) 목록을 돌려준다.
실제 체결은 호출자(executor)가 거래소에 보내고, 그 결과(Fill)를 순수 리듀서
(`apply_buy_fill`, `start_cycle`, `on_limit_placed`, `on_limit_canceled`)로 상태에 접는다.
이 모듈은 시간·난수·네트워크에 의존하지 않는다(now 는 인자로 주입).

규칙 출처: CLAUDE.md(SSOT) §2. 절(§) 번호는 거기에 대응.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

from src.atr import compute_x


class Status(str, Enum):
    RUNNING = "RUNNING"  # 사이클 진행 중, 추격매수 활성
    CASH_EXHAUSTED = (
        "CASH_EXHAUSTED"  # 40 tranche 소진 — 신규매수 중단, TP만 감시 (§2.7)
    )
    HALTED = "HALTED"  # 안전 정지 (reconcile 불일치/치명적 오류) — 매매 금지


# ----------------------------------------------------------------------------
# 설정 파라미터 (config.yaml 유래) — 런타임 상태(State)와 분리해 영속화 단순화
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Params:
    n: int = 40  # 분할 수 (§2.2)
    tp: float = 0.10  # 익절 목표, 평단 대비 +10% 순수익 (§2.6)
    taker_fee: float = 0.001  # taker ≈ 0.1% (§4)
    x_mult: float = 2.0  # X = x_mult × ATR/price (§2.5)
    x_floor: float = 0.03  # X 하한 3%, 상한 없음 (§2.5)
    fallback_hours: float = 24.0  # 추격 폴백 (§2.4)
    min_notional: float = (
        5.0  # 거래소 최소 주문금액 가드 (§4). exchange.py가 실값으로 갱신.
    )
    topup_enabled: bool = True  # 잔고변화(입금/출금) 재분할 on/off (Stage 2.5)
    topup_threshold: float = 10.0  # 잔고변화 판단 임계 USDT (Stage 2.5)
    topup_immediate_buy_on_deposit: bool = True  # 입금 시 즉시 시장가 1매수 (Stage 2.5)


# ----------------------------------------------------------------------------
# 관측값 / 체결 결과
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Market:
    price: float  # 현재가
    atr14: float  # 당일 ATR14
    now: datetime


@dataclass(frozen=True)
class Fill:
    """매수 체결 결과 — 거래소 trade 레코드에서 읽어 채운다 (가정값 금지, §3)."""

    price: float  # 평균 체결가
    qty: float  # 실제 수령 BTC (수수료가 BTC로 빠졌으면 차감된 순수량)
    cost: float  # 실제 지출 USDT (수수료가 USDT면 포함)


@dataclass(frozen=True)
class OpenLimit:
    id: str
    price: float
    qty: float  # 거래소에 걸린 잔여 base 수량
    usdt: float  # 이 스텝의 목표 quote (= step_target_usdt)
    # executor 가 이 주문에서 '이미 상태에 반영한' 누적 체결량(증분 폴딩 멱등성용)
    filled_base_seen: float = 0.0
    filled_quote_seen: float = 0.0


# ----------------------------------------------------------------------------
# 영속 런타임 상태
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class State:
    status: Status
    cycle_id: int
    tranche_usdt: float  # 1회 매수액 = 사이클현금/N, 사이클 내 고정 (§2.2)
    tranches_used: int  # 완료된 tranche 수 (0..N)
    cycle_cash_remaining: float  # 아직 투입 안 한 사이클 현금
    ref: float | None  # 직전 체결가 — 추격 기준 (§2.4)
    last_fill_time: datetime | None  # 직전 '스텝 완료' 체결 시각 (24h 타이머 기준)
    position_qty: float  # 보유 BTC
    invested_usdt: float  # 보유분 취득에 실제 들어간 USDT(매수수수료 포함) = 원가 (§3)
    step_target_usdt: float  # 진행 중 스텝의 목표 USDT (0 = 진행 스텝 없음)
    step_filled_usdt: float  # 진행 스텝에 체결된 USDT
    open_limit: OpenLimit | None


# ----------------------------------------------------------------------------
# Action (decide 의 출력) — executor 가 순서대로 실행
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketBuy:
    usdt: float


@dataclass(frozen=True)
class PlaceLimitBuy:
    price: float
    usdt: float


@dataclass(frozen=True)
class CancelLimit:
    id: str


@dataclass(frozen=True)
class SellAll:
    pass


@dataclass(frozen=True)
class Hold:
    pass


Action = MarketBuy | PlaceLimitBuy | CancelLimit | SellAll | Hold


# ----------------------------------------------------------------------------
# 순수 헬퍼
# ----------------------------------------------------------------------------
def compute_tranche(cash: float, n: int) -> float:
    """사이클 1회 매수액 = 가용현금 / N (§2.2)."""
    if n <= 0:
        raise ValueError("n must be positive")
    return cash / n


def elapsed_hours(since: datetime, now: datetime) -> float:
    return (now - since).total_seconds() / 3600.0


def tp_trigger_price(state: State, params: Params) -> float:
    """순수익 +tp 를 만족시키는 최소 매도가.

    qty·price·(1−fee) ≥ invested·(1+tp) 를 price 에 대해 푼 값 (§2.6).
    """
    denom = state.position_qty * (1.0 - params.taker_fee)
    if denom <= 0:
        return float("inf")
    return state.invested_usdt * (1.0 + params.tp) / denom


def should_take_profit(state: State, price: float, params: Params) -> bool:
    """매도 수수료까지 차감한 순수익이 평단 대비 +tp 이상인가 (§2.6)."""
    if state.position_qty <= 0:
        return False
    net_proceeds = state.position_qty * price * (1.0 - params.taker_fee)
    return net_proceeds >= state.invested_usdt * (1.0 + params.tp)


def can_buy(state: State, params: Params) -> bool:
    """신규 tranche 매수 여력 — 미소진 & 잔여현금이 최소주문금액 이상 (§2.7)."""
    next_usdt = min(state.tranche_usdt, state.cycle_cash_remaining)
    return state.tranches_used < params.n and next_usdt >= params.min_notional


def round_down_amount(amount: float, step: float) -> float:
    """수량을 lotSize(step) 배수로 내림 (초과주문 방지, §4)."""
    if step <= 0:
        return amount
    a, s = Decimal(str(amount)), Decimal(str(step))
    return float((a // s) * s)


def round_price(price: float, tick: float) -> float:
    """가격을 호가단위(tick)로 반올림 (§4)."""
    if tick <= 0:
        return price
    p, t = Decimal(str(price)), Decimal(str(tick))
    return float((p / t).to_integral_value(rounding=ROUND_HALF_UP) * t)


def meets_min_notional(price: float, amount: float, min_notional: float) -> bool:
    """주문 금액이 거래소 최소 명목금액 이상인지 (§4)."""
    return price * amount >= min_notional


# ----------------------------------------------------------------------------
# 의사결정 (순수)
# ----------------------------------------------------------------------------
def decide(state: State, market: Market, params: Params) -> list[Action]:
    """현재 상태에서 다음 행동들을 결정. 부분체결/오프라인 체결 감지는 executor 책임이며,
    이 함수는 항상 '정합화된' 상태를 받는다고 가정한다.

    우선순위: 부트스트랩 1매수 → TP(최우선) → 24h 폴백 → 추격 지정가 재설치 → Hold.
    """
    if state.status == Status.HALTED:
        return [Hold()]

    # 사이클 부트스트랩: 시작/익절 직후 즉시 시장가 1매수 (§2.3).
    # ref 가 None 인 동안(첫 체결 전)에는 스텝 잔여분을 끝까지 매수 → 시장가 부분체결로
    # ref·타이머가 None 으로 남아 멈추는 틈 차단(메모 4). executor 도 완전체결까지 드라이브.
    if state.ref is None and state.step_target_usdt - state.step_filled_usdt > 0:
        return [MarketBuy(state.step_target_usdt - state.step_filled_usdt)]

    # 1) 익절 — 최우선 (§2.6). 대기 지정가 있으면 먼저 취소 후 전량 매도.
    if should_take_profit(state, market.price, params):
        acts: list[Action] = []
        if state.open_limit is not None:
            acts.append(CancelLimit(state.open_limit.id))
        acts.append(SellAll())
        return acts

    # 2) 24h 폴백 — 진행 스텝이 있고 직전 체결 +24h 경과 시 잔여분 시장가 매수 (§2.4)
    if (
        state.last_fill_time is not None
        and state.step_target_usdt > 0
        and elapsed_hours(state.last_fill_time, market.now) >= params.fallback_hours
    ):
        remaining = state.step_target_usdt - state.step_filled_usdt
        if remaining >= params.min_notional:
            acts = []
            if state.open_limit is not None:
                acts.append(CancelLimit(state.open_limit.id))
            acts.append(MarketBuy(remaining))
            return acts

    # 3) 스텝 사이 idle 이면 ref×(1−X) 추격 지정가 설치 (§2.4, §2.5)
    if (
        state.status == Status.RUNNING
        and state.open_limit is None
        and state.step_target_usdt == 0
        and state.ref is not None
        and can_buy(state, params)
    ):
        usdt = min(state.tranche_usdt, state.cycle_cash_remaining)
        x = compute_x(market.atr14, market.price, params.x_mult, params.x_floor)
        return [PlaceLimitBuy(state.ref * (1.0 - x), usdt)]

    return [Hold()]


# ----------------------------------------------------------------------------
# 리듀서 (순수, 새 State 반환)
# ----------------------------------------------------------------------------
def start_cycle(cash: float, params: Params, cycle_id: int) -> State:
    """사이클 시작 — 현금 재분할 후 부트스트랩 1매수 대기 상태 (§2.2, §2.6 익절 후 재진입)."""
    tranche = compute_tranche(cash, params.n)
    return State(
        status=Status.RUNNING,
        cycle_id=cycle_id,
        tranche_usdt=tranche,
        tranches_used=0,
        cycle_cash_remaining=cash,
        ref=None,
        last_fill_time=None,
        position_qty=0.0,
        invested_usdt=0.0,
        step_target_usdt=tranche,  # 부트스트랩 시장가 1매수 스텝
        step_filled_usdt=0.0,
        open_limit=None,
    )


def apply_topup(state: State, available_usdt: float, params: Params) -> State:
    """잔고변화(입금/출금) 시 가용 USDT 전체를 N 재분할 (Stage 2.5).

    재분할만 수행 — 현금/tranche 재계산, tranches_used=0, 진행 스텝 비움. 기존 포지션·평단·
    추격기준·익절은 그대로 유지: position_qty, invested_usdt, ref, last_fill_time, cycle_id 보존.
    열린 지정가(open_limit) 정리는 executor 책임(체결분 폴딩 후 취소).
    status: 기본 RUNNING(CASH_EXHAUSTED→RUNNING 재개 포함). 단 가용현금이 최소주문 미만이고
    포지션 보유 중이면 CASH_EXHAUSTED — 신규매수 중단, +10% 익절은 계속 감시(HALT 아님).
    """
    tranche = compute_tranche(available_usdt, params.n)
    exhausted = available_usdt < params.min_notional and state.position_qty > 0
    return replace(
        state,
        status=Status.CASH_EXHAUSTED if exhausted else Status.RUNNING,
        tranche_usdt=tranche,
        tranches_used=0,
        cycle_cash_remaining=available_usdt,
        step_target_usdt=0.0,
        step_filled_usdt=0.0,
    )


def apply_buy_fill(state: State, fill: Fill, now: datetime, params: Params) -> State:
    """매수 체결을 상태에 반영.

    포지션 수량·원가·현금·스텝 진척은 매 체결마다 즉시 정확 반영하되,
    ref·24h타이머·tranche 카운트는 **스텝 완료(=tranche 1개 완성) 시에만** 갱신한다
    → 평단·40분할 정합성 보장 (설계안 OCO/부분체결 정책).
    """
    qty = state.position_qty + fill.qty
    invested = state.invested_usdt + fill.cost
    cash = state.cycle_cash_remaining - fill.cost
    step_filled = state.step_filled_usdt + fill.cost
    remaining = state.step_target_usdt - step_filled

    if remaining < params.min_notional:  # 스텝 완료 (잔여로는 추가매수 불가)
        tranches_used = state.tranches_used + 1
        exhausted = tranches_used >= params.n or cash < params.min_notional
        return replace(
            state,
            position_qty=qty,
            invested_usdt=invested,
            cycle_cash_remaining=cash,
            ref=fill.price,  # 직전 체결가 = 추격 기준 (§2.4)
            last_fill_time=now,  # 24h 타이머 리셋 (§2.4)
            tranches_used=tranches_used,
            step_target_usdt=0.0,
            step_filled_usdt=0.0,
            open_limit=None,
            status=Status.CASH_EXHAUSTED if exhausted else Status.RUNNING,
        )

    # 부분체결 — 스텝 유지(ref·타이머·카운트 불변), 회계만 갱신
    return replace(
        state,
        position_qty=qty,
        invested_usdt=invested,
        cycle_cash_remaining=cash,
        step_filled_usdt=step_filled,
    )


def on_limit_placed(state: State, open_limit: OpenLimit) -> State:
    """추격 지정가를 걸었을 때 — 새 스텝 시작(목표 USDT 설정)."""
    return replace(
        state,
        open_limit=open_limit,
        step_target_usdt=open_limit.usdt,
        step_filled_usdt=0.0,
    )


def on_limit_canceled(state: State) -> State:
    """지정가 취소 — open_limit 만 비우고 스텝 진척(목표/체결)은 유지(폴백 완성용)."""
    return replace(state, open_limit=None)


def update_limit_seen(
    state: State, filled_base_seen: float, filled_quote_seen: float
) -> State:
    """executor 가 관측한 이 주문의 누적 체결량을 기록(증분 폴딩 멱등성용, 메모 2)."""
    if state.open_limit is None:
        return state
    return replace(
        state,
        open_limit=replace(
            state.open_limit,
            filled_base_seen=filled_base_seen,
            filled_quote_seen=filled_quote_seen,
        ),
    )
