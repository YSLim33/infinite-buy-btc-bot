# 라오어의 무한 매수법 수정 버젼 — BTC 현물 자동매매 봇

BTC/USDT **현물** 무한매수 봇 (거래소 **Kraken**). 분할 N=40, 추격매수(−X% 지정가 OR 24h 시장가), 평단 +10%(순수익) 익절을 기계적으로 반복한다. 전략 사양의 단일 출처(SSOT)는 [`CLAUDE.md`](CLAUDE.md).

> ⚠️ 실제 자금 위험. 본 봇은 **손절·서킷브레이커가 없다**(설계상). Kraken 은 현물 testnet 이 없으므로 라이브 전 **dry-run 으로 최소 1~3개월** 검증 후 **소액 live** 로 전환한다. 레버리지 없음(청산 위험 0)이나 현물 자체의 큰 평가손실은 가능.

## 구조
```
src/strategy.py   순수 전략 코어(decide + 리듀서, I/O 없음)
src/atr.py        Wilder ATR14 / X 계산
src/exchange.py   CCXT(Kraken) 래퍼 + 드라이런 페이퍼 시뮬레이터
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
- **dry-run (기본):** Kraken 실시간 시세로 모의 매매. 실제 주문 없음. `seed_usdt` 가상 시드.
- **live:** 실거래. **이중 가드** — `config.yaml` 의 `i_understand_live: true` **그리고** 환경변수 `JUNE_BOT_ALLOW_LIVE=YES_I_UNDERSTAND` 가 **둘 다** 있어야 동작. 하나라도 없으면 거부.
- 거래소는 `config.yaml` 의 `exchange`(기본 `kraken`). Kraken 은 현물 sandbox/testnet 이 없다.

```bash
.venv/bin/python -m src.main        # config.yaml 의 mode 로 실행
```

## dry-run → live 전환 절차
1. dry-run 으로 동작·로그 확인(Kraken 은 testnet 없음 → 최소 1~3개월 dry-run 권장).
2. Kraken Pro 계정 + KYC, **query+trade 권한만 / 출금·펀딩 OFF**(가능하면 VM IP 제한) 키 발급.
3. 자금을 Kraken 으로 입금/전송(KRW 불가 → 암호화폐/USDT 전송).
4. 소액으로 `mode: live` + `i_understand_live: true` + `JUNE_BOT_ALLOW_LIVE=YES_I_UNDERSTAND` → **첫 사이클 집중 관찰**.
5. 점진적 증액.

## 배포 (무료 US 리전 GCP e2-micro / Debian 12)
- Kraken 은 미국 IP 를 허용하므로 **무료 US 리전**(us-west1 / us-central1 / us-east1) e2-micro 에서 ~$0 운용.
- 배포 전 preflight: `curl -sI https://api.kraken.com/0/public/Time` → **200**(451 아님) 확인.
- **Docker(권장):** `docker compose up -d --build`. `restart: always` 로 크래시·재부팅 자동 재시작. 상태/로그는 `./data` 볼륨에 영속.
- **systemd(대안):** `deploy/june-bot.service` 참고(`Restart=always`, `SIGTERM` graceful).
- **단일 인스턴스/키:** Kraken 은 API 키 nonce 가 단조 증가해야 한다 → **같은 키로 봇을 동시에 2개 이상 실행 금지**(nonce 충돌).

## 운영
- **킬스위치:** 프로젝트 루트(또는 `data/` 작업경로)에 `KILL` 파일 생성 → 다음 폴에서 안전 정지. `SIGTERM`/`Ctrl-C` 도 graceful(상태 저장 후 종료).
- **상태:** `data/june_bot.db`(SQLite). 재시작 시 **거래소를 진실의 원천으로 reconcile** — 오프라인 체결은 자동 복원, 설명 불가 불일치면 HALT+알림.
- **알림:** `telegram.enabled: true` + `.env` 토큰/챗ID.
- **로그:** `data/june_bot.log`(+stdout). 키/시크릿은 기록하지 않음.

## 백테스트
`src.backtest.run_backtest(daily_ohlcv, hourly_ohlcv, params, seed_usdt=...)` — ATR=일봉, 체결판정=시간봉, 수수료·슬리피지 반영, look-ahead 금지. 반환: 최종자산·수익률·최대낙폭·사이클수.

```bash
# 수수료 영향 비교 (Kraken 0.4% vs Binance 0.1%)
.venv/bin/python -m src.backtest --start 2024-01-01 --fee 0.004
.venv/bin/python -m src.backtest --start 2024-01-01 --fee 0.001
```
과거 OHLCV 는 Binance 에서 수집한다(Kraken 공개 OHLC 는 timeframe 당 ~720개만 제공 → 다개월 시간봉 백테스트 불가). BTC 가격은 거래소 무관 ≈동일하므로 `--fee` 만 Kraken 값으로 바꿔 평가한다.

## 테스트
```bash
.venv/bin/python -m pytest -q
```

## 보안
- Kraken API 키: **query + trade 권한만, 출금/펀딩 OFF**. Kraken 이 키 IP 제한을 제공하면 GCP VM IP 로 제한. `.env` 로만 주입, 하드코딩/로그/커밋 금지.
- 출금 관련 API 는 호출하지도 구현하지도 않는다(유출돼도 거래만, 자금 탈취 불가).
- 전용 계정/자금으로 메인/수동 자금과 분리. 키당 단일 인스턴스(nonce 충돌 방지).
- 구글 클라우드 VM IP를 크라켄 API에 등록 필요.