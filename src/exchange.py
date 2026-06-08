"""거래소 래퍼 — CCXT 현물(기본 Kraken) + 드라이런 페이퍼 시뮬레이터.

핵심 책임(메모 반영):
- 시장가 주문은 **완전 체결까지 드라이브**한 뒤 반환(메모 4).
- 취소는 **최종 체결 상태를 재조회**해 돌려줌 → executor 가 취소-체결 race 를 안전 처리(메모 2).
- 수량/가격 precision·최소주문 가드(§4). 수수료는 체결 레코드에서 읽어 원가에 반영(§3).
- 출금(withdraw) 관련 API 는 호출하지도, 구현하지도 않는다.

Kraken 이식 메모:
- 현물 sandbox/testnet 없음 → testnet 모드 제거(`set_sandbox_mode` 호출 시 TypeError).
- 시장가 매수는 cost(quote) 기반(`viqc`); 거부되면 base 수량 기반으로 폴백.
- 메타(최소주문/스텝/틱)는 CCXT 통합 `limits`/`precision` 에서 읽는다(Binance `info.filters` 는 Kraken 에 없음).
- 수수료는 Kraken 기본 `fciq`(quote=USDT) → `_normalize` 의 quote 측 합산이 실지출 USDT 와 일치.
- 레이트리밋: 읽기 호출만 NetworkError 백오프 재시도. 주문 호출은 중복주문 위험으로 재시도 금지.
  InvalidNonce 는 다중 인스턴스 신호 → 재시도 없이 전파(키당 단일 인스턴스 운영).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.strategy import Fill, OpenLimit, round_down_amount, round_price

_RETRIES = 5


@dataclass
class OrderResult:
    """거래소 주문의 정규화 스냅샷. 수수료를 반영한 '순' 값으로 채운다."""

    id: str
    status: str  # 'open' | 'closed' | 'canceled'
    filled_base_net: float  # 누적 수령 base (수수료가 base 면 차감됨)
    filled_quote_cost: float  # 누적 지출 quote (수수료가 quote 면 포함)
    avg_price: float


def _tick_size(prec, default: float) -> float:
    """CCXT precision 값을 실제 step/tick 으로.

    Kraken 은 precisionMode=TICK_SIZE → 값이 이미 tick(예 1e-8, 0.1) 이라 그대로 사용.
    DECIMAL_PLACES(예 8) 모드면 10**-8 로 변환(모드 무관 안전).
    """
    if prec is None:
        return default
    p = float(prec)
    if p <= 0:
        return default
    return p if p < 1 else 10.0 ** (-int(p))


def market_meta(market: dict, ref_price: float) -> tuple[float, float, float, float]:
    """통합 limits/precision 에서 (min_notional, amount_min, lot_step, price_tick) 추출.

    min_notional 은 cost.min 과 amount.min×현재가 중 큰 값(둘 중 강한 제약) + 안전 하한.
    """
    limits = market.get("limits", {}) or {}
    precision = market.get("precision", {}) or {}
    cost_min = (limits.get("cost") or {}).get("min")
    amount_min = float((limits.get("amount") or {}).get("min") or 0.0)
    lot = _tick_size(precision.get("amount"), 1e-8)
    tick = _tick_size(precision.get("price"), 0.01)
    min_notional = max(float(cost_min or 0.0), amount_min * float(ref_price), 1.0)
    return min_notional, amount_min, lot, tick


class CcxtExchange:
    """live 전용 CCXT 현물 래퍼(기본 Kraken). 출금 API 는 호출/구현하지 않는다."""

    def __init__(
        self,
        exchange_id: str,
        api_key: str,
        secret: str,
        symbol: str = "BTC/USDT",
        *,
        client=None,
        timeout_ms: int = 20000,
    ):
        import ccxt  # 지연 import — 페이퍼/백테스트 경로에선 불필요

        self._ccxt = ccxt
        self.symbol = symbol
        if client is None:
            client = getattr(ccxt, exchange_id)(
                {
                    "apiKey": api_key,
                    "secret": secret,
                    "enableRateLimit": True,
                    "timeout": timeout_ms,  # 네트워크 호출 행 방지(워치독의 1차 방어선)
                }
            )
        else:
            try:  # 주입된 client 에도 적용(ccxt 런타임 knob; 테스트가 검증)
                client.timeout = timeout_ms
            except Exception:
                pass
        self.client = client
        self.client.load_markets()
        self._market = self.client.market(symbol)
        self._base = self._market["base"]
        self._quote = self._market["quote"]
        ref = self.fetch_price()  # 최소주문 산정 기준가(1회)
        self._min_notional, self._amount_min, self._lot, self._tick = market_meta(
            self._market, ref
        )

    # --- 재시도 -------------------------------------------------------------
    def _retry(self, fn):
        """읽기 전용 호출용 백오프. NetworkError(레이트리밋·타임아웃·DDoS 등)만 재시도.

        InvalidNonce(NetworkError 하위지만 다중 인스턴스 신호) 는 재시도하지 않고 전파.
        주문성 오류(InvalidOrder/InsufficientFunds 등 ExchangeError)는 NetworkError 가 아니라 통과.
        """
        delay = 1.0
        for attempt in range(_RETRIES):
            try:
                return fn()
            except self._ccxt.InvalidNonce:
                raise  # 치명: 같은 API 키를 여러 인스턴스가 사용 중일 수 있음
            except self._ccxt.NetworkError:
                if attempt == _RETRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    # --- 시장 메타 -----------------------------------------------------------
    def min_notional(self) -> float:
        return self._min_notional

    def lot_step(self) -> float:
        return self._lot

    def price_tick(self) -> float:
        return self._tick

    # --- 조회 ----------------------------------------------------------------
    def fetch_price(self) -> float:
        return float(self._retry(lambda: self.client.fetch_ticker(self.symbol))["last"])

    def fetch_daily_ohlcv(self, limit: int = 150) -> list[list[float]]:
        # ATR 평활화를 위해 넉넉히(기본 150개) 일봉 조회(메모 3).
        # Kraken 공개 OHLC 는 timeframe 당 ~720개 상한(limit 비강제) — 150 요청 시 그 이상 올 수 있음.
        return self._retry(
            lambda: self.client.fetch_ohlcv(self.symbol, timeframe="1d", limit=limit)
        )

    def fetch_free_usdt(self) -> float:
        bal = self._retry(lambda: self.client.fetch_balance())
        return float(bal["free"].get(self._quote, 0.0))

    def fetch_total_usdt(self) -> float:
        # 미투입 USDT 전체(free + 미체결 매수에 예약된 quote). 입금 감지용(Stage 2.5).
        bal = self._retry(lambda: self.client.fetch_balance())
        return float(bal["total"].get(self._quote, 0.0))

    def fetch_base_balance(self) -> float:
        bal = self._retry(lambda: self.client.fetch_balance())
        return float(bal["free"].get(self._base, 0.0))

    # --- 주문 ----------------------------------------------------------------
    def _normalize(self, order: dict) -> OrderResult:
        filled_base = float(order.get("filled") or 0.0)
        filled_quote = float(order.get("cost") or 0.0)
        fee_base = 0.0
        fee_quote = 0.0
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            cur, cost = fee.get("currency"), float(fee.get("cost") or 0.0)
            if cur == self._base:
                fee_base += cost
            elif cur == self._quote:
                fee_quote += cost
        base_net = filled_base - fee_base
        quote_cost = filled_quote + fee_quote
        avg = float(
            order.get("average") or (quote_cost / base_net if base_net > 0 else 0.0)
        )
        status = order.get("status", "open")
        # Kraken: 만료 = 미체결 종료 → executor 의 canceled 재결정 경로로
        if status == "expired":
            status = "canceled"
        return OrderResult(str(order["id"]), status, base_net, quote_cost, avg)

    def _drive_to_close(
        self, order_id: str, *, tries: int = 20, delay: float = 0.5
    ) -> OrderResult:
        """주문이 'closed' 될 때까지 폴링(메모 4). 시간 내 미완결이면 예외."""
        last = self.fetch_order(order_id)
        for _ in range(tries):
            if last.status == "closed":
                return last
            time.sleep(delay)
            last = self.fetch_order(order_id)
        raise RuntimeError(
            f"order {order_id} did not fully fill (status={last.status})"
        )

    def market_buy_quote(self, usdt: float) -> Fill:
        # 주문 호출은 재시도하지 않는다(중복주문 위험). cost(quote) 기반이 우선.
        try:
            order = self.client.create_market_buy_order_with_cost(self.symbol, usdt)
        except (
            self._ccxt.NotSupported,
            self._ccxt.InvalidOrder,
            self._ccxt.BadRequest,
        ):
            # viqc(quote 단위 시장가) 거부 시 base 수량 기반 폴백.
            amount = float(
                self.client.amount_to_precision(self.symbol, usdt / self.fetch_price())
            )
            order = self.client.create_market_buy_order(self.symbol, amount)
        res = self._drive_to_close(order["id"])
        return Fill(
            price=res.avg_price, qty=res.filled_base_net, cost=res.filled_quote_cost
        )

    def market_sell_all(self, qty: float) -> float:
        amount = float(self.client.amount_to_precision(self.symbol, qty))
        if amount <= 0:
            return 0.0
        order = self.client.create_market_sell_order(self.symbol, amount)
        res = self._drive_to_close(order["id"])
        return res.filled_quote_cost  # 수수료 차감된 순수령 USDT 근사

    def place_limit_buy(self, price: float, usdt: float) -> OpenLimit:
        price = float(self.client.price_to_precision(self.symbol, price))
        amount = float(self.client.amount_to_precision(self.symbol, usdt / price))
        if amount < self._amount_min or price * amount < self._min_notional:
            raise ValueError(
                f"limit order below min (amount {amount} < {self._amount_min} "
                f"or cost {price * amount:.4f} < {self._min_notional:.4f})"
            )
        order = self.client.create_limit_buy_order(self.symbol, amount, price)
        return OpenLimit(id=str(order["id"]), price=price, qty=amount, usdt=usdt)

    def fetch_order(self, order_id: str) -> OrderResult:
        return self._normalize(
            self._retry(lambda: self.client.fetch_order(order_id, self.symbol))
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        # 취소-체결 race: 취소가 실패(이미 체결/소멸)해도 무시하고 최종 상태를 재조회(메모 2).
        try:
            self.client.cancel_order(order_id, self.symbol)
        except Exception:
            pass
        return self.fetch_order(order_id)


class PaperExchange:
    """드라이런 — 실시간 시세(공개 CCXT, 기본 Kraken)로 시뮬레이션, 주문/잔고는 로컬 가상.

    시장가: 현재가×(1±slippage) 즉시 완전체결. 지정가: 현재가 ≤ 지정가가 되면 지정가에 체결.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        *,
        exchange_id: str = "kraken",
        seed_usdt: float,
        taker_fee: float = 0.004,
        slippage: float = 0.0005,
        timeout_ms: int = 20000,
    ):
        import ccxt

        self.symbol = symbol
        self._public = getattr(ccxt, exchange_id)(
            {"enableRateLimit": True, "timeout": timeout_ms}
        )
        self._public.load_markets()
        m = self._public.market(symbol)
        self._base = m["base"]
        ref = float(self._public.fetch_ticker(symbol)["last"])
        self._min_notional, self._amount_min, self._lot, self._tick = market_meta(
            m, ref
        )
        self.free_usdt = seed_usdt
        self.base_balance = 0.0
        self.taker_fee = taker_fee
        self.slippage = slippage
        self._orders: dict[str, dict] = {}
        self._seq = 0

    def min_notional(self) -> float:
        return self._min_notional

    def lot_step(self) -> float:
        return self._lot

    def price_tick(self) -> float:
        return self._tick

    def resume_from(self, state) -> None:
        """드라이런 재시작 시 가상 잔고·미체결을 저장상태로 복원(드라이런은 거래소가 곧 시뮬레이터)."""
        self.free_usdt = state.cycle_cash_remaining
        self.base_balance = state.position_qty
        ol = state.open_limit
        if ol is not None:
            gross = (
                ol.filled_base_seen / (1 - self.taker_fee)
                if ol.filled_base_seen > 0
                else 0.0
            )
            self._orders[ol.id] = {
                "price": ol.price,
                "amount": ol.qty,
                "filled": gross,
                "status": "open",
            }
            tail = ol.id.rsplit("-", 1)[-1]
            if tail.isdigit():
                self._seq = max(self._seq, int(tail))

    def fetch_price(self) -> float:
        return float(self._public.fetch_ticker(self.symbol)["last"])

    def fetch_daily_ohlcv(self, limit: int = 150) -> list[list[float]]:
        return self._public.fetch_ohlcv(self.symbol, timeframe="1d", limit=limit)

    def fetch_free_usdt(self) -> float:
        return self.free_usdt

    def fetch_total_usdt(self) -> float:
        # 페이퍼는 미체결 지정가에 예약을 두지 않으므로 total == free (입금 감지용, Stage 2.5).
        return self.free_usdt

    def fetch_base_balance(self) -> float:
        return self.base_balance

    def _next_id(self) -> str:
        self._seq += 1
        return f"paper-{self._seq}"

    def market_buy_quote(self, usdt: float) -> Fill:
        price = self.fetch_price() * (1 + self.slippage)
        qty_gross = usdt / price
        fee_base = qty_gross * self.taker_fee
        qty_net = qty_gross - fee_base
        self.free_usdt -= usdt
        self.base_balance += qty_net
        return Fill(price=price, qty=qty_net, cost=usdt)

    def market_sell_all(self, qty: float) -> float:
        amount = round_down_amount(min(qty, self.base_balance), self._lot)
        if amount <= 0:
            return 0.0
        price = self.fetch_price() * (1 - self.slippage)
        proceeds = amount * price * (1 - self.taker_fee)
        self.base_balance -= amount
        self.free_usdt += proceeds
        return proceeds

    def place_limit_buy(self, price: float, usdt: float) -> OpenLimit:
        price = round_price(price, self._tick)
        amount = round_down_amount(usdt / price, self._lot)
        if amount < self._amount_min or price * amount < self._min_notional:
            raise ValueError("limit order below min notional")
        oid = self._next_id()
        self._orders[oid] = {
            "price": price,
            "amount": amount,
            "filled": 0.0,
            "status": "open",
        }
        return OpenLimit(id=oid, price=price, qty=amount, usdt=usdt)

    def fetch_order(self, order_id: str) -> OrderResult:
        o = self._orders[order_id]
        # 현재가가 지정가 이하로 내려오면 전량 체결로 시뮬레이션
        if o["status"] == "open" and self.fetch_price() <= o["price"]:
            filled = o["amount"]
            fee_base = filled * self.taker_fee
            self.base_balance += filled - fee_base
            self.free_usdt -= filled * o["price"]
            o["filled"] = filled
            o["status"] = "closed"
        base_net = o["filled"] * (1 - self.taker_fee)
        quote_cost = o["filled"] * o["price"]
        return OrderResult(order_id, o["status"], base_net, quote_cost, o["price"])

    def cancel_order(self, order_id: str) -> OrderResult:
        res = self.fetch_order(
            order_id
        )  # race: 막 체결됐을 수 있으니 먼저 확정(메모 2)
        o = self._orders[order_id]
        if o["status"] == "open":
            o["status"] = "canceled"
            res = OrderResult(
                order_id,
                "canceled",
                res.filled_base_net,
                res.filled_quote_cost,
                res.avg_price,
            )
        return res
