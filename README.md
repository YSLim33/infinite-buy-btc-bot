# June 무한매수법 — BTC 현물 자동매매 봇

BTC/USDT **현물** 무한매수 봇. 분할 N=40, 추격매수(−X% 지정가 OR 24h 시장가), 평단 +10%(순수익) 익절을 기계적으로 반복한다. 전략 사양의 단일 출처(SSOT)는 [`CLAUDE.md`](CLAUDE.md).

> ⚠️ 실제 자금 위험. 본 봇은 **손절·서킷브레이커가 없다**(설계상). 라이브 전 **dry-run/testnet 으로 최소 1~3개월** 검증을 권장한다. 레버리지 없음(청산 위험 0)이나 현물 자체의 큰 평가손실은 가능.

## 구조
```
src/strategy.py   순수 전략 코어(decide + 리듀서, I/O 없음)
src/atr.py        Wilder ATR14 / X 계산
src/exchange.py   CCXT(Binance) 래퍼 + 드라이런 페이퍼 시뮬레이터
src/state.py      SQLite 영속화
src/executor.py   폴링 처리·액션 실행·부팅 reconcile
src/backtest.py   과거 데이터 재생 백테스트
src/main.py       엔트리포인트(모드 분기·시그널·일일 ATR·킬스위치)
tests/            단위테스트(pytest)
```

## 설치
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # 런타임
.venv/bin/pip install -r requirements-dev.txt     # 개발/테스트
cp .env.example .env                              # 키 입력(절대 커밋 금지)
```

## 실행 모드 (`config.yaml` 의 `mode`)
- **dry-run (기본):** 실시간 시세로 모의 매매. 실제 주문 없음. `seed_usdt` 가상 시드.
- **testnet:** Binance spot testnet 실주문. `.env` 에 testnet 키 필요.
- **live:** 실거래. **이중 가드** — `config.yaml` 의 `i_understand_live: true` **그리고** 환경변수 `JUNE_BOT_ALLOW_LIVE=YES_I_UNDERSTAND` 가 **둘 다** 있어야 동작. 하나라도 없으면 거부.

```bash
.venv/bin/python -m src.main        # config.yaml 의 mode 로 실행
```

## testnet → live 전환 절차
1. dry-run 으로 동작·로그 확인.
2. testnet 키로 `mode: testnet` 최소 수주~수개월 무중단 검증.
3. 전용 서브계정 + **거래 권한만/출금 OFF/IP 화이트리스트** 키 발급.
4. 소액 입금 → `mode: live` + `i_understand_live: true` + `JUNE_BOT_ALLOW_LIVE=YES_I_UNDERSTAND`.
5. 점진적 증액.

## 배포 (GCP e2-micro / Debian 12)
- **Docker(권장):** `docker compose up -d --build`. `restart: always` 로 크래시·재부팅 자동 재시작. 상태/로그는 `./data` 볼륨에 영속.
- **systemd(대안):** `deploy/june-bot.service` 참고(`Restart=always`, `SIGTERM` graceful).

## 운영
- **킬스위치:** 프로젝트 루트(또는 `data/` 작업경로)에 `KILL` 파일 생성 → 다음 폴에서 안전 정지. `SIGTERM`/`Ctrl-C` 도 graceful(상태 저장 후 종료).
- **상태:** `data/june_bot.db`(SQLite). 재시작 시 **거래소를 진실의 원천으로 reconcile** — 오프라인 체결은 자동 복원, 설명 불가 불일치면 HALT+알림.
- **알림:** `telegram.enabled: true` + `.env` 토큰/챗ID.
- **로그:** `data/june_bot.log`(+stdout). 키/시크릿은 기록하지 않음.

## 백테스트
`src.backtest.run_backtest(daily_ohlcv, hourly_ohlcv, params, seed_usdt=...)` — ATR=일봉, 체결판정=시간봉, 수수료·슬리피지 반영, look-ahead 금지. 반환: 최종자산·수익률·최대낙폭·사이클수.

## 테스트
```bash
.venv/bin/python -m pytest -q
```

## 보안
- API 키: **거래 권한만, 출금 OFF, IP 화이트리스트**. `.env` 로만 주입, 하드코딩/로그/커밋 금지.
- 출금 관련 API 는 호출하지도 구현하지도 않는다.
- 전용 (가상)서브계정으로 메인/수동 자금과 분리.
