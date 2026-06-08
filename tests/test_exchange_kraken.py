"""Kraken 이식 검증 — CCXT 클라이언트를 fake 로 주입해 네트워크 없이 테스트.

검증 범위: 필드 매핑(_normalize, 수수료=quote, expired→canceled), 통합 limits/precision 메타,
cost 기반 시장가 매수 + viqc 거부 폴백, 지정가 precision·이중 최소주문 가드, 취소-체결 race,
읽기 호출의 NetworkError 백오프(InvalidNonce 는 재시도 안 함).
"""

import math

import ccxt
import pytest

from src.exchange import CcxtExchange, market_meta

# 실측한 Kraken BTC/USDT 마켓 형태(precisionMode=TICK_SIZE → precision 이 곧 tick).
KRAKEN_MARKET = {
    "base": "BTC",
    "quote": "USDT",
    "limits": {
        "amount": {"min": 5e-05, "max": None},
        "cost": {"min": 0.5, "max": None},
        "price": {"min": None, "max": None},
    },
    "precision": {"amount": 1e-08, "price": 0.1},
}


class FakeKraken:
    """CcxtExchange 가 호출하는 ccxt 메서드만 구현한 시뮬레이터."""

    def __init__(self, price=50000.0, balance=None):
        self.price = price
        self.balance = balance or {"USDT": 0.0, "BTC": 0.0}
        self.orders: dict[str, dict] = {}
        self.seq = 0
        self.calls: list[tuple] = []  # 생성/취소 호출 로그
        self.buy_cost_supported = True
        self.timeout = None  # CcxtExchange 가 주입 client 에 timeout 설정함(Stage 2.7)

    # --- 메타/조회 ---
    def load_markets(self):
        return {}

    def market(self, symbol):
        return KRAKEN_MARKET

    def fetch_ticker(self, symbol):
        return {"last": self.price, "ask": self.price}

    def fetch_balance(self):
        return {"free": dict(self.balance)}

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=150):
        return [[0, self.price, self.price, self.price, self.price, 1.0]]

    # --- precision (Kraken: amount 절삭, price 반올림) ---
    def amount_to_precision(self, symbol, amount):
        return f"{math.floor(float(amount) * 1e8) / 1e8:.8f}"

    def price_to_precision(self, symbol, price):
        return f"{round(float(price) * 10) / 10:.1f}"

    # --- 주문 (즉시 완전체결, 수수료는 quote=USDT 의 fciq) ---
    def _closed_buy(self, kind, cost, base):
        self.seq += 1
        oid = f"O-{self.seq}"
        fee = cost * 0.004
        o = {
            "id": oid,
            "status": "closed",
            "filled": base,
            "cost": cost,  # CCXT: filled×average (수수료 제외)
            "average": self.price,
            "fee": {"currency": "USDT", "cost": fee},
        }
        self.orders[oid] = o
        self.calls.append((kind, cost, base))
        return o

    def create_market_buy_order_with_cost(self, symbol, cost):
        if not self.buy_cost_supported:
            raise ccxt.InvalidOrder("viqc not supported on this market")
        return self._closed_buy("buy_cost", cost, cost / self.price)

    def create_market_buy_order(self, symbol, amount):
        return self._closed_buy("buy_amount", amount * self.price, amount)

    def create_market_sell_order(self, symbol, amount):
        self.seq += 1
        oid = f"O-{self.seq}"
        cost = amount * self.price
        o = {
            "id": oid,
            "status": "closed",
            "filled": amount,
            "cost": cost,
            "average": self.price,
            "fee": {"currency": "USDT", "cost": cost * 0.004},
        }
        self.orders[oid] = o
        self.calls.append(("sell", amount))
        return o

    def create_limit_buy_order(self, symbol, amount, price):
        self.seq += 1
        oid = f"O-{self.seq}"
        self.orders[oid] = {
            "id": oid,
            "status": "open",
            "filled": 0.0,
            "cost": 0.0,
            "average": None,
            "fee": None,
        }
        self.calls.append(("limit", amount, price))
        return self.orders[oid]

    def fetch_order(self, order_id, symbol):
        return self.orders[order_id]

    def cancel_order(self, order_id, symbol):
        self.calls.append(("cancel", order_id))
        o = self.orders.get(order_id)
        if o and o["status"] == "open":
            o["status"] = "canceled"
        return o


def _ex(price=50000.0, balance=None):
    fake = FakeKraken(price=price, balance=balance)
    ex = CcxtExchange("kraken", "k", "s", "BTC/USDT", client=fake)
    return ex, fake


# --- 메타 ---------------------------------------------------------------
def test_market_meta_from_unified_limits_precision():
    min_notional, amount_min, lot, tick = market_meta(KRAKEN_MARKET, 50000.0)
    assert lot == 1e-08
    assert tick == 0.1
    assert amount_min == 5e-05
    # max(cost.min 0.5, amount.min×price 2.5, 하한 1.0) = 2.5
    assert min_notional == pytest.approx(2.5)


def test_exchange_exposes_meta():
    ex, _ = _ex()
    assert ex.lot_step() == 1e-08
    assert ex.price_tick() == 0.1
    assert ex.min_notional() == pytest.approx(2.5)


# --- 필드 매핑 ----------------------------------------------------------
def test_normalize_fee_in_quote_buy():
    ex, _ = _ex()
    order = {
        "id": "X1",
        "status": "closed",
        "filled": 0.002,
        "cost": 100.0,  # 수수료 제외 gross
        "average": 50000.0,
        "fee": {"currency": "USDT", "cost": 0.4},
    }
    r = ex._normalize(order)
    assert r.filled_base_net == pytest.approx(0.002)  # quote 수수료 → base 전량 수령
    assert r.filled_quote_cost == pytest.approx(100.4)  # 실지출 = gross + fee
    assert r.avg_price == pytest.approx(50000.0)
    assert r.status == "closed"


def test_normalize_partial_fill_cumulative():
    ex, _ = _ex()
    order = {
        "id": "X2",
        "status": "open",
        "filled": 0.0008,
        "cost": 40.0,
        "average": 50000.0,
        "fee": {"currency": "USDT", "cost": 0.16},
    }
    r = ex._normalize(order)
    assert r.status == "open"
    assert r.filled_base_net == pytest.approx(0.0008)
    assert r.filled_quote_cost == pytest.approx(40.16)


def test_normalize_expired_maps_to_canceled():
    ex, _ = _ex()
    order = {"id": "X3", "status": "expired", "filled": 0.0, "cost": 0.0, "fee": None}
    r = ex._normalize(order)
    assert r.status == "canceled"  # executor 의 재결정 경로로 흘러가게


def test_normalize_avg_fallback_when_missing():
    ex, _ = _ex()
    order = {
        "id": "X4",
        "status": "closed",
        "filled": 0.002,
        "cost": 100.0,
        "average": None,
        "fee": {"currency": "USDT", "cost": 0.4},
    }
    r = ex._normalize(order)
    assert r.avg_price == pytest.approx(100.4 / 0.002)  # quote_cost/base_net


# --- 시장가 매수 --------------------------------------------------------
def test_market_buy_quote_cost_based():
    ex, fake = _ex(price=50000.0)
    fill = ex.market_buy_quote(100.0)
    assert fake.calls[0][0] == "buy_cost"
    assert fill.qty == pytest.approx(100.0 / 50000.0)
    assert fill.cost == pytest.approx(100.4)  # 100 + 0.4% quote fee
    assert fill.price == pytest.approx(50000.0)


def test_market_buy_quote_falls_back_when_viqc_rejected():
    ex, fake = _ex(price=50000.0)
    fake.buy_cost_supported = False
    fill = ex.market_buy_quote(100.0)
    assert fake.calls[0][0] == "buy_amount"  # base 수량 기반 폴백 경로
    assert fill.qty == pytest.approx(100.0 / 50000.0, rel=1e-6)


# --- 지정가 매수 --------------------------------------------------------
def test_place_limit_buy_uses_precision_and_passes_min():
    ex, fake = _ex(price=50000.0)
    ol = ex.place_limit_buy(48500.04, 25.0)
    assert ol.price == 48500.0  # 0.1 틱 반올림
    assert ol.qty == pytest.approx(25.0 / 48500.0, rel=1e-4)
    assert fake.calls[-1][0] == "limit"


def test_place_limit_buy_rejects_below_amount_min():
    ex, _ = _ex(price=50000.0)
    with pytest.raises(ValueError):
        ex.place_limit_buy(50000.0, 1.0)  # amount 2e-5 < 5e-5, cost 1.0 < 2.5


# --- 시장가 매도 --------------------------------------------------------
def test_market_sell_all_returns_proceeds():
    ex, _ = _ex(price=50000.0)
    proceeds = ex.market_sell_all(0.002)
    assert proceeds == pytest.approx(0.002 * 50000.0 + 0.002 * 50000.0 * 0.004)


# --- 취소-체결 race -----------------------------------------------------
def test_cancel_returns_final_state_open_to_canceled():
    ex, fake = _ex(price=50000.0)
    ol = ex.place_limit_buy(48500.0, 25.0)
    res = ex.cancel_order(ol.id)
    assert res.status == "canceled"


def test_cancel_returns_filled_when_race_filled():
    ex, fake = _ex(price=50000.0)
    ol = ex.place_limit_buy(48500.0, 25.0)
    # 취소 직전 완전체결됐다고 가정(거래소가 최종 상태로 closed 반환)
    fake.orders[ol.id].update(
        status="closed",
        filled=0.0005,
        cost=24.25,
        average=48500.0,
        fee={"currency": "USDT", "cost": 0.097},
    )
    res = ex.cancel_order(ol.id)
    assert res.status == "closed"
    assert res.filled_quote_cost == pytest.approx(24.25 + 0.097)


# --- 재시도(읽기 전용) --------------------------------------------------
def test_retry_recovers_from_network_error(monkeypatch):
    import src.exchange as exmod

    monkeypatch.setattr(exmod.time, "sleep", lambda *_: None)
    ex, fake = _ex(price=50000.0)

    calls = {"n": 0}
    real = fake.fetch_balance

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ccxt.RequestTimeout("transient")  # NetworkError 하위
        return real()

    fake.fetch_balance = flaky
    fake.balance = {"USDT": 123.0, "BTC": 0.0}
    assert ex.fetch_free_usdt() == pytest.approx(123.0)
    assert calls["n"] == 3  # 2회 실패 후 성공


def test_retry_does_not_retry_invalid_nonce(monkeypatch):
    import src.exchange as exmod

    monkeypatch.setattr(exmod.time, "sleep", lambda *_: None)
    ex, fake = _ex(price=50000.0)

    calls = {"n": 0}

    def nonce_fail():
        calls["n"] += 1
        raise ccxt.InvalidNonce("nonce")

    fake.fetch_balance = nonce_fail
    with pytest.raises(ccxt.InvalidNonce):
        ex.fetch_free_usdt()
    assert calls["n"] == 1  # 재시도 없이 즉시 전파


# --- ccxt 타임아웃 (Stage 2.7 — 네트워크 호출 행 방지) ----------------------
def test_ccxt_default_timeout():
    ex, fake = _ex()
    assert fake.timeout == 20000  # 기본값이 주입 client 에 적용됨


def test_ccxt_timeout_applied_to_injected_client():
    fake = FakeKraken()
    CcxtExchange("kraken", "k", "s", "BTC/USDT", client=fake, timeout_ms=12345)
    assert fake.timeout == 12345
