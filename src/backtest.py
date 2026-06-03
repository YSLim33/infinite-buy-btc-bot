"""백테스트 — 과거 OHLCV 를 동일 전략 코어/executor 로 재생(§5).

- ATR=일봉, 체결판정=시간봉(1h). look-ahead 금지(해당일 ATR 은 *직전 완성 일봉*까지만 사용).
- 수수료·슬리피지 반영. 한쪽이 닿으면 지정가 체결(저가 ≤ 지정가).
산출: 최종 자산·수익률·최대낙폭·완료 사이클 수.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timezone

from src.atr import true_ranges
from src.exchange import OrderResult
from src.executor import run_poll_once
from src.notifier import NullNotifier
from src.strategy import (
    Fill,
    OpenLimit,
    Params,
    round_down_amount,
    round_price,
    start_cycle,
)


class BacktestExchange:
    def __init__(self, *, seed_usdt, taker_fee, slippage, min_notional, lot, tick):
        self.free_usdt = seed_usdt
        self.base_balance = 0.0
        self.taker_fee = taker_fee
        self.slippage = slippage
        self._min_notional, self._lot, self._tick = min_notional, lot, tick
        self.cur_price = 0.0
        self.cur_low = 0.0
        self._orders: dict[str, dict] = {}
        self._seq = 0

    def set_bar(self, price: float, low: float) -> None:
        self.cur_price, self.cur_low = price, low

    def min_notional(self):
        return self._min_notional

    def lot_step(self):
        return self._lot

    def price_tick(self):
        return self._tick

    def fetch_price(self):
        return self.cur_price

    def fetch_free_usdt(self):
        return self.free_usdt

    def fetch_base_balance(self):
        return self.base_balance

    def market_buy_quote(self, usdt: float) -> Fill:
        price = self.cur_price * (1 + self.slippage)
        qty_net = (usdt / price) * (1 - self.taker_fee)
        self.free_usdt -= usdt
        self.base_balance += qty_net
        return Fill(price=price, qty=qty_net, cost=usdt)

    def market_sell_all(self, qty: float) -> float:
        amount = round_down_amount(min(qty, self.base_balance), self._lot)
        if amount <= 0:
            return 0.0
        price = self.cur_price * (1 - self.slippage)
        proceeds = amount * price * (1 - self.taker_fee)
        self.base_balance -= amount
        self.free_usdt += proceeds
        return proceeds

    def place_limit_buy(self, price: float, usdt: float) -> OpenLimit:
        price = round_price(price, self._tick)
        amount = round_down_amount(usdt / price, self._lot)
        self._seq += 1
        oid = f"bt-{self._seq}"
        self._orders[oid] = {"price": price, "amount": amount, "status": "open"}
        return OpenLimit(id=oid, price=price, qty=amount, usdt=usdt)

    def fetch_order(self, oid: str) -> OrderResult:
        o = self._orders[oid]
        if (
            o["status"] == "open" and self.cur_low <= o["price"]
        ):  # 저가가 지정가를 터치 → 전량 체결
            self.base_balance += o["amount"] * (1 - self.taker_fee)
            self.free_usdt -= o["amount"] * o["price"]
            o["status"] = "closed"
        filled = o["amount"] if o["status"] == "closed" else 0.0
        return OrderResult(
            oid,
            o["status"],
            filled * (1 - self.taker_fee),
            filled * o["price"],
            o["price"],
        )

    def cancel_order(self, oid: str) -> OrderResult:
        res = self.fetch_order(oid)
        o = self._orders[oid]
        if o["status"] == "open":
            o["status"] = "canceled"
            return OrderResult(
                oid,
                "canceled",
                res.filled_base_net,
                res.filled_quote_cost,
                res.avg_price,
            )
        return res


def _atr_schedule(daily: list[list[float]], period: int) -> tuple[list, list]:
    """각 일봉 종가 시점에 '확정'되는 ATR 을 (date, atr) 로. 다음날부터 사용 가능(look-ahead 방지)."""
    highs = [r[2] for r in daily]
    lows = [r[3] for r in daily]
    closes = [r[4] for r in daily]
    trs = true_ranges(highs, lows, closes)  # 길이 = len-1, daily[1:] 에 대응
    dates, atrs = [], []
    atr = None
    for i, tr in enumerate(trs):
        if i + 1 < period:
            continue
        if atr is None:
            atr = sum(trs[:period]) / period
        else:
            atr = (atr * (period - 1) + tr) / period
        dates.append(
            datetime.fromtimestamp(daily[i + 1][0] / 1000, tz=timezone.utc).date()
        )
        atrs.append(atr)
    return dates, atrs


def run_backtest(
    daily: list[list[float]],
    hourly: list[list[float]],
    params: Params,
    *,
    seed_usdt: float,
    atr_period: int = 14,
    slippage: float = 0.0005,
    min_notional: float = 5.0,
    lot: float = 1e-5,
    tick: float = 0.01,
) -> dict:
    ex = BacktestExchange(
        seed_usdt=seed_usdt,
        taker_fee=params.taker_fee,
        slippage=slippage,
        min_notional=min_notional,
        lot=lot,
        tick=tick,
    )
    state = start_cycle(seed_usdt, params, cycle_id=1)
    notifier = NullNotifier()
    dates, atrs = _atr_schedule(daily, atr_period)

    peak = seed_usdt
    max_dd = 0.0
    for bar in hourly:
        ts, _o, _h, low, close, *_ = bar
        now = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        idx = bisect.bisect_left(dates, now.date()) - 1  # 직전 완성 일봉의 ATR 만 사용
        atr14 = atrs[idx] if idx >= 0 else 0.0  # 워밍업 전엔 0 → X 는 하한 3%
        ex.set_bar(price=close, low=low)
        state = run_poll_once(state, ex, params, notifier, now, atr14)
        equity = ex.free_usdt + ex.base_balance * close
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)

    final_equity = ex.free_usdt + ex.base_balance * hourly[-1][4]
    return {
        "seed_usdt": seed_usdt,
        "final_equity": final_equity,
        "return_pct": (final_equity / seed_usdt - 1) * 100,
        "max_drawdown_pct": max_dd * 100,
        "cycles_completed": state.cycle_id - 1,
        "tranches_used_last_cycle": state.tranches_used,
        "bars": len(hourly),
    }


def _fetch_ohlcv(client, symbol, timeframe, since_ms, until_ms, limit=1000):
    """ccxt 페이지네이션 수집."""
    rows = []
    since = since_ms
    while since < until_ms:
        batch = client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + 1
        if nxt <= since:
            break
        since = nxt
        if len(batch) < limit:
            break
    return [r for r in rows if since_ms <= r[0] < until_ms]


def main() -> None:
    import argparse
    import sys

    import ccxt

    try:  # Windows 콘솔(cp1252)에서도 한글 출력이 깨지지 않게
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="June 무한매수법 백테스트")
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD (UTC), 기본 현재")
    ap.add_argument("--seed", type=float, default=1000.0)
    a = ap.parse_args()

    client = ccxt.binance({"enableRateLimit": True})
    client.load_markets()
    m = client.market(a.symbol)
    filters = {f["filterType"]: f for f in m.get("info", {}).get("filters", [])}
    lot = float(filters.get("LOT_SIZE", {}).get("stepSize", 1e-5))
    tick = float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.01))
    min_notional = float(
        filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {})).get("minNotional", 5.0)
    )

    start_ms = int(
        datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc).timestamp() * 1000
    )
    end_ms = (
        int(
            datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc).timestamp()
            * 1000
        )
        if a.end
        else client.milliseconds()
    )
    daily = _fetch_ohlcv(client, a.symbol, "1d", start_ms - 60 * 86_400_000, end_ms)
    hourly = _fetch_ohlcv(client, a.symbol, "1h", start_ms, end_ms)

    params = Params()
    stats = run_backtest(
        daily,
        hourly,
        params,
        seed_usdt=a.seed,
        atr_period=14,
        min_notional=min_notional,
        lot=lot,
        tick=tick,
    )
    print(
        f"\n=== Backtest {a.symbol} {a.start}~{a.end or 'now'} (seed {a.seed:.0f} USDT) ==="
    )
    print(f"기간 시간봉 수      : {stats['bars']}")
    print(f"완료 사이클(익절)   : {stats['cycles_completed']}")
    print(f"최종 자산          : {stats['final_equity']:.2f} USDT")
    print(f"총 수익률          : {stats['return_pct']:+.2f} %")
    print(f"최대 낙폭(MDD)     : {stats['max_drawdown_pct']:.2f} %")
    print(f"마지막 사이클 tranche 사용: {stats['tranches_used_last_cycle']}/{params.n}")


if __name__ == "__main__":
    main()
