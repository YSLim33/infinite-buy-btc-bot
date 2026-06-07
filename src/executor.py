"""오케스트레이션 — 폴링 1회 처리, 액션 실행, 부팅 reconcile.

5개 운영 메모 반영 지점:
1. 익절 후 현금: SellAll 은 매도 후 **거래소 free USDT**(순매도대금 + 미투입현금)로 새 사이클 시작.
2. OCO race: CancelLimit 은 거래소가 돌려준 **최종 체결 상태**를 폴딩한 뒤, MarketBuy 는 실행
   시점의 **잔여분만** 매수(이미 체결된 부분 중복매수 방지).
3. ATR 봉 수: 일봉 ~150개는 main 이 조회해 atr14 로 주입.
4. 시장가 완결: exchange.market_buy_quote 가 완전체결까지 드라이브(+코어가 부분 부트스트랩 재시도).
5. 진실은 거래소: SellAll 매도수량·reconcile 정합성은 거래소 잔고를 기준으로.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from src.strategy import (
    CancelLimit,
    Fill,
    Hold,
    Market,
    MarketBuy,
    PlaceLimitBuy,
    SellAll,
    State,
    Status,
    apply_buy_fill,
    apply_topup,
    can_buy,
    decide,
    on_limit_canceled,
    on_limit_placed,
    should_take_profit,
    start_cycle,
    update_limit_seen,
)

DUST = 1e-9
MAX_STEPS_PER_POLL = 6  # 같은 폴 안에서 재결정 반복 상한(무한루프 가드)


def _fold_order_progress(state, order, now, params, notifier) -> State:
    """open_limit 주문의 누적 체결을 증분으로 상태에 폴딩(멱등). 부분/완료/race 모두 처리."""
    ol = state.open_limit
    if ol is None or order.id != ol.id:
        return state
    d_base = order.filled_base_net - ol.filled_base_seen
    d_quote = order.filled_quote_cost - ol.filled_quote_seen
    if d_quote > DUST:
        # ref(='직전 체결가')는 거래소 평균체결가를 사용. 회계용 qty/cost 는 증분으로 정확히.
        state = apply_buy_fill(
            state, Fill(price=order.avg_price, qty=d_base, cost=d_quote), now, params
        )
        notifier.notify(f"[BUY] limit fill {d_quote:.2f} USDT @ {order.avg_price:.2f}")
        if state.open_limit is not None:  # 스텝 미완료 → 관측 누적치 갱신
            state = update_limit_seen(
                state, order.filled_base_net, order.filled_quote_cost
            )
    return state


def refresh_open_limit(state, exchange, now, params, notifier) -> State:
    """폴링 시작 시 걸린 지정가의 체결(오프라인 포함)을 반영."""
    if state.open_limit is None:
        return state
    order = exchange.fetch_order(state.open_limit.id)
    state = _fold_order_progress(state, order, now, params, notifier)
    if order.status == "canceled" and state.open_limit is not None:
        notifier.notify("[WARN] limit canceled externally; clearing for re-decide")
        state = on_limit_canceled(state)
    return state


def execute_action(
    action, state, exchange, params, notifier, now
) -> tuple[State, bool]:
    """단일 액션 실행. (새 상태, 진행여부) 반환. 진행여부 False 면 재결정 루프 종료 신호."""
    if isinstance(action, Hold):
        return state, False

    if isinstance(action, MarketBuy):
        # 메모 2: 취소-체결 race 후일 수 있으니 '실행 시점' 잔여로 재계산.
        usdt = state.step_target_usdt - state.step_filled_usdt
        if usdt < params.min_notional:
            return state, False  # 살 게 없음(이미 채워짐/더스트)
        fill = exchange.market_buy_quote(usdt)  # 메모 4: 완전체결 드라이브
        state = apply_buy_fill(state, fill, now, params)
        notifier.notify(f"[BUY] market {fill.cost:.2f} USDT @ {fill.price:.2f}")
        return state, True

    if isinstance(action, PlaceLimitBuy):
        ol = exchange.place_limit_buy(action.price, action.usdt)
        state = on_limit_placed(state, ol)
        notifier.notify(f"[LIMIT] place {action.usdt:.2f} USDT @ {ol.price:.2f}")
        return state, True

    if isinstance(action, CancelLimit):
        order = exchange.cancel_order(
            action.id
        )  # 메모 2: 최종 상태(race 체결 포함) 반환
        state = _fold_order_progress(state, order, now, params, notifier)
        if state.open_limit is not None:
            state = on_limit_canceled(state)
        return state, True

    if isinstance(action, SellAll):
        qty = exchange.fetch_base_balance()  # 메모 5: 진실은 거래소 잔고
        proceeds = exchange.market_sell_all(qty)  # 메모 4: 완전체결 드라이브
        new_cash = exchange.fetch_free_usdt()  # 메모 1: 순매도대금 + 미투입현금
        notifier.notify(
            f"[TP] sell {qty:.6f} BTC → {proceeds:.2f} USDT; new cycle cash {new_cash:.2f}"
        )
        state = start_cycle(new_cash, params, cycle_id=state.cycle_id + 1)
        return state, True

    return state, False


def check_and_apply_topup(state, exchange, params, notifier, now: datetime) -> State:
    """잔고변화(입금/출금) 감지 → 40 재분할(Stage 2.5). 사이클·포지션·평단·ref·익절은 보존.

    감지: 거래소 **total USDT**(전용계정에서 free + 미체결 매수 예약분 = 봇 미투입 USDT)와
    cycle_cash_remaining 차이. |delta| 가 임계 초과면 입출금으로 처리.
    봇 자신의 매수(현금↓)·익절(start_cycle 리셋)은 기준이 state 라 자연 분리 → 오탐 없음.
    멱등(적용 후 cycle_cash_remaining=가용 → 다음 폴 재트리거 없음). HALTED/비활성이면 미적용.
    USDT 변화로는 HALT 하지 않는다(출금은 정상 축소 — HALT 은 reconcile 의 BTC 불일치 등만).
    """
    if not params.topup_enabled or state.status == Status.HALTED:
        return state

    available = exchange.fetch_total_usdt()
    delta = available - state.cycle_cash_remaining
    threshold = max(params.min_notional, params.topup_threshold)
    if abs(delta) <= threshold:  # 의미있는 변화 아님(봇 자체 활동·미세 드리프트)
        return state

    # 익절 시점이면 topup 건너뜀 — TP 가 새 사이클에서 잔여현금(입출금 포함)을 흡수.
    if should_take_profit(state, exchange.fetch_price(), params):
        return state

    # 열린 지정가는 (refresh 로 체결분 이미 폴딩됨) 취소 — 다음 decide 가 새 트랜치로 재설치.
    if state.open_limit is not None:
        order = exchange.cancel_order(state.open_limit.id)
        state = _fold_order_progress(state, order, now, params, notifier)
        if state.open_limit is not None:
            state = on_limit_canceled(state)

    avail = exchange.fetch_total_usdt()  # 취소 후 실제 가용 = 재분할 기준
    state = apply_topup(state, avail, params)
    kind = "deposit" if delta > 0 else "withdrawal"
    notifier.notify(
        f"[TOPUP] {kind} {delta:+.2f} USDT → re-split {avail:.2f} into "
        f"{params.n} (tranche {state.tranche_usdt:.2f}, {state.status.value})"
    )

    # 입금이면 즉시 1 tranche 시장가 매수(부트스트랩식) → 새 ref. 출금이면 매수 없음.
    if delta > 0 and params.topup_immediate_buy_on_deposit and can_buy(state, params):
        usdt = min(state.tranche_usdt, state.cycle_cash_remaining)
        state = replace(state, step_target_usdt=usdt, step_filled_usdt=0.0)
        state, _ = execute_action(
            MarketBuy(usdt), state, exchange, params, notifier, now
        )
    return state


def run_poll_once(
    state, exchange, params, notifier, now: datetime, atr14: float
) -> State:
    """폴링 1회: 체결 감지 → 입금 재분할 → 재결정 루프(즉시 후속행동까지) → 실행."""
    if state.status == Status.HALTED:
        return state
    state = refresh_open_limit(state, exchange, now, params, notifier)
    state = check_and_apply_topup(state, exchange, params, notifier, now)
    if state.status == Status.HALTED:
        return state
    for _ in range(MAX_STEPS_PER_POLL):
        market = Market(price=exchange.fetch_price(), atr14=atr14, now=now)
        actions = decide(state, market, params)
        progressed = False
        for act in actions:
            state, did = execute_action(act, state, exchange, params, notifier, now)
            progressed = progressed or did
            if state.status == Status.HALTED:
                return state
        if not progressed:
            break
    return state


def reconcile(stored: State | None, exchange, params, notifier, now: datetime) -> State:
    """부팅 정합화 — 거래소를 진실의 원천으로, 오프라인 체결은 자동 복원, 설명 불가 불일치만 HALT.

    (Q1 자동복구형 / 메모 5)
    """
    if stored is None:
        cash = exchange.fetch_free_usdt()
        if cash < params.min_notional:
            notifier.notify(
                f"[HALT] insufficient USDT at start: {cash:.2f} < {params.min_notional}"
            )
            return replace(start_cycle(cash, params, cycle_id=1), status=Status.HALTED)
        notifier.notify(f"[START] fresh cycle, cash {cash:.2f}")
        return start_cycle(cash, params, cycle_id=1)

    state = stored
    if state.open_limit is not None:
        try:
            order = exchange.fetch_order(state.open_limit.id)
            state = _fold_order_progress(state, order, now, params, notifier)
            if order.status == "canceled" and state.open_limit is not None:
                state = on_limit_canceled(state)
        except Exception as e:
            notifier.notify(f"[HALT] cannot fetch saved order on reconcile: {e}")
            return replace(state, status=Status.HALTED)

    bal = exchange.fetch_base_balance()
    tol = max(exchange.lot_step(), 0.005 * state.position_qty)
    if abs(bal - state.position_qty) > tol:
        notifier.notify(
            f"[HALT] balance mismatch on reconcile: exchange {bal:.6f} vs state {state.position_qty:.6f}"
        )
        return replace(state, status=Status.HALTED)
    notifier.notify(
        f"[RESUME] cycle {state.cycle_id}, qty {state.position_qty:.6f}, status {state.status.value}"
    )
    # boot 시 다운 중 입금 반영(Stage 2.5).
    return check_and_apply_topup(state, exchange, params, notifier, now)
