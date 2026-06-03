"""거래소 래퍼 — CCXT(Binance spot) + 드라이런 페이퍼 시뮬레이터.

핵심 책임(메모 반영):
- 시장가 주문은 **완전 체결까지 드라이브**한 뒤 반환(메모 4).
- 취소는 **최종 체결 상태를 재조회**해 돌려줌 → executor 가 취소-체결 race 를 안전 처리(메모 2).
- 수량/가격 precision·minNotional 가드(§4). 수수료는 체결 레코드에서 읽어 원가에 반영(§3).
- 출금(withdraw) 관련 API 는 호출하지도, 구현하지도 않는다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.strategy import Fill, OpenLimit, round_down_amount, round_price

QUOTE = "USDT"


@dataclass
class OrderResult:
    """거래소 주문의 정규화 스냅샷. 수수료를 반영한 '순' 값으로 채운다."""

    id: str
    status: str  # 'open' | 'closed' | 'canceled'
    filled_base_net: float  # 누적 수령 base (수수료가 base 면 차감됨)
    filled_quote_cost: float  # 누적 지출 quote (수수료가 quote 면 포함)
    avg_price: float


class CcxtExchange:
    """testnet / live 공용 CCXT(Binance spot) 래퍼."""

    def __init__(
        self, api_key: str, secret: str, symbol: str = "BTC/USDT", *, testnet: bool
    ):
        import ccxt  # 지연 import — 페이퍼/백테스트 경로에선 불필요

        self.symbol = symbol
        self.client = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        if testnet:
            self.client.set_sandbox_mode(True)
        self.client.load_markets()
        self._market = self.client.market(symbol)
        self._base = self._market["base"]
        self._filters = {
            f["filterType"]: f for f in self._market.get("info", {}).get("filters", [])
        }

    # --- 시장 메타 -----------------------------------------------------------
    def min_notional(self) -> float:
        for key in ("NOTIONAL", "MIN_NOTIONAL"):
            f = self._filters.get(key)
            if f:
                return float(f.get("minNotional") or f.get("notional") or 5.0)
        return 5.0

    def lot_step(self) -> float:
        f = self._filters.get("LOT_SIZE")
        return float(f["stepSize"]) if f else 1e-6

    def price_tick(self) -> float:
        f = self._filters.get("PRICE_FILTER")
        return float(f["tickSize"]) if f else 0.01

    # --- 조회 ----------------------------------------------------------------
    def fetch_price(self) -> float:
        return float(self.client.fetch_ticker(self.symbol)["last"])

    def fetch_daily_ohlcv(self, limit: int = 150) -> list[list[float]]:
        # ATR 평활화를 위해 넉넉히(기본 150개) 일봉 조회(메모 3).
        return self.client.fetch_ohlcv(self.symbol, timeframe="1d", limit=limit)

    def fetch_free_usdt(self) -> float:
        return float(self.client.fetch_balance()["free"].get(QUOTE, 0.0))

    def fetch_base_balance(self) -> float:
        return float(self.client.fetch_balance()["free"].get(self._base, 0.0))

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
            elif cur == QUOTE:
                fee_quote += cost
        base_net = filled_base - fee_base
        quote_cost = filled_quote + fee_quote
        avg = float(
            order.get("average") or (quote_cost / base_net if base_net > 0 else 0.0)
        )
        return OrderResult(
            str(order["id"]), order.get("status", "open"), base_net, quote_cost, avg
        )

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
        order = self.client.create_market_buy_order_with_cost(self.symbol, usdt)
        res = self._drive_to_close(order["id"])
        return Fill(
            price=res.avg_price, qty=res.filled_base_net, cost=res.filled_quote_cost
        )

    def market_sell_all(self, qty: float) -> float:
        amount = round_down_amount(qty, self.lot_step())
        if amount <= 0:
            return 0.0
        order = self.client.create_market_sell_order(self.symbol, amount)
        res = self._drive_to_close(order["id"])
        return res.filled_quote_cost  # 수수료 차감된 순수령 USDT 근사

    def place_limit_buy(self, price: float, usdt: float) -> OpenLimit:
        price = round_price(price, self.price_tick())
        amount = round_down_amount(usdt / price, self.lot_step())
        if price * amount < self.min_notional():
            raise ValueError("limit order below min notional")
        order = self.client.create_limit_buy_order(self.symbol, amount, price)
        return OpenLimit(id=str(order["id"]), price=price, qty=amount, usdt=usdt)

    def fetch_order(self, order_id: str) -> OrderResult:
        return self._normalize(self.client.fetch_order(order_id, self.symbol))

    def cancel_order(self, order_id: str) -> OrderResult:
        # 취소-체결 race: 취소가 실패(이미 체결/소멸)해도 무시하고 최종 상태를 재조회(메모 2).
        try:
            self.client.cancel_order(order_id, self.symbol)
        except Exception:
            pass
        return self.fetch_order(order_id)


class PaperExchange:
    """드라이런 — 실시간 시세(공개 CCXT)로 시뮬레이션, 주문/잔고는 로컬 가상.

    시장가: 현재가×(1±slippage) 즉시 완전체결. 지정가: 현재가 ≤ 지정가가 되면 지정가에 체결.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        *,
        seed_usdt: float,
        taker_fee: float = 0.001,
        slippage: float = 0.0005,
    ):
        import ccxt

        self.symbol = symbol
        self._public = ccxt.binance(
            {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        )
        self._public.load_markets()
        m = self._public.market(symbol)
        self._base = m["base"]
        filters = {f["filterType"]: f for f in m.get("info", {}).get("filters", [])}
        self._lot = float(filters.get("LOT_SIZE", {}).get("stepSize", 1e-6))
        self._tick = float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.01))
        self._min_notional = float(
            filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {})).get(
                "minNotional", 5.0
            )
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
        if price * amount < self._min_notional:
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
