"""엔트리포인트 — 설정/로깅 초기화, 모드 분기, 부팅 reconcile, 폴링 루프, graceful shutdown.

실행 모드: dry-run(기본) / live. **live 는 환경변수 + config 이중 가드**(§4).
거래소는 config `exchange`(기본 kraken). 출금 API 는 사용하지 않는다.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv

from src.atr import compute_atr
from src.exchange import CcxtExchange, PaperExchange
from src.executor import reconcile, run_poll_once
from src.notifier import build_notifier
from src.state import StateStore
from src.strategy import Params, Status

log = logging.getLogger("june_bot")
_STOP = False


def _handle_signal(signum, _frame):
    global _STOP
    _STOP = True
    log.info("signal %s received → graceful shutdown requested", signum)


def setup_logging(log_file: str) -> None:
    parent = os.path.dirname(log_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:  # Windows 콘솔(cp1252)에서도 유니코드 로그가 깨지지 않게 UTF-8 고정
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def build_params(cfg: dict, min_notional: float) -> Params:
    return Params(
        n=int(cfg["n"]),
        tp=float(cfg["tp"]),
        taker_fee=float(cfg["taker_fee"]),
        x_mult=float(cfg["x_mult"]),
        x_floor=float(cfg["x_floor"]),
        fallback_hours=float(cfg["fallback_hours"]),
        min_notional=float(min_notional),
    )


def build_exchange(cfg: dict):
    mode = cfg["mode"]
    symbol = cfg["symbol"]
    exchange_id = cfg.get("exchange", "kraken")
    if mode == "dry-run":
        return PaperExchange(
            symbol,
            exchange_id=exchange_id,
            seed_usdt=float(cfg["seed_usdt"]),
            taker_fee=float(cfg["taker_fee"]),
            slippage=float(cfg["slippage"]),
        )
    key = os.environ.get("KRAKEN_API_KEY", "")
    secret = os.environ.get("KRAKEN_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("KRAKEN_API_KEY/SECRET 가 .env 에 필요합니다 (live).")
    if mode == "live":
        # 이중 가드: config 플래그 + 환경변수 둘 다 명시돼야 실거래.
        if (
            not cfg.get("i_understand_live", False)
            or os.environ.get("JUNE_BOT_ALLOW_LIVE") != "YES_I_UNDERSTAND"
        ):
            raise SystemExit(
                "LIVE 거부: config 의 i_understand_live: true 와 환경변수 JUNE_BOT_ALLOW_LIVE=YES_I_UNDERSTAND 가 모두 필요합니다."
            )
        return CcxtExchange(exchange_id, key, secret, symbol)
    raise SystemExit(f"알 수 없는 mode: {mode} (dry-run|live)")


def _refresh_atr(exchange, cfg: dict) -> float:
    ohlcv = exchange.fetch_daily_ohlcv(
        limit=int(cfg["daily_ohlcv_limit"])
    )  # 메모 3: 넉넉히
    return compute_atr(ohlcv, period=int(cfg["atr_period"]))


def main(config_path: str = "config.yaml") -> None:
    load_dotenv()
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg.get("log_file", "june_bot.log"))
    log.info("starting in mode=%s symbol=%s", cfg["mode"], cfg["symbol"])

    exchange = build_exchange(cfg)
    params = build_params(cfg, exchange.min_notional())
    store = StateStore(cfg["state_db"])
    notifier = build_notifier(
        cfg.get("telegram", {}).get("enabled", False),
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT_ID"),
    )

    stored = store.load()
    if cfg["mode"] == "dry-run" and stored is not None:
        exchange.resume_from(stored)  # 드라이런 가상잔고를 저장상태로 복원

    now = datetime.now(timezone.utc)
    atr14 = _refresh_atr(exchange, cfg)
    atr_date = now.date()

    state = reconcile(stored, exchange, params, notifier, now)
    store.save(state)
    if state.status == Status.HALTED:
        log.error("reconcile resulted in HALTED — manual check required. exiting.")
        return

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    poll = int(cfg["poll_seconds"])
    kill_file = cfg.get("kill_switch_file", "KILL")
    notifier.notify(f"[UP] june-bot running ({cfg['mode']})")

    while not _STOP:
        if os.path.exists(kill_file):
            notifier.notify("[KILL] kill-switch file present → halting")
            break
        now = datetime.now(timezone.utc)
        if now.date() != atr_date:  # 일일 ATR 갱신(§3)
            try:
                atr14 = _refresh_atr(exchange, cfg)
                atr_date = now.date()
            except Exception as e:
                log.warning("daily ATR refresh failed: %s", e)
        try:
            state = run_poll_once(state, exchange, params, notifier, now, atr14)
            store.save(state)
            if state.status == Status.HALTED:
                notifier.notify("[HALT] entering halted state — stopping loop")
                break
        except Exception as e:  # fail-safe: 불확실하면 멈추고 알림(§3)
            log.exception("poll error")
            notifier.notify(f"[ERROR] poll failed: {e}")
        # 중단에 반응하도록 잘게 나눠 대기
        for _ in range(poll):
            if _STOP:
                break
            time.sleep(1)

    store.save(state)
    store.close()
    notifier.notify("[DOWN] june-bot stopped (state saved)")
    log.info("shutdown complete")


if __name__ == "__main__":
    main()
