# DESIGN.md — Hyperliquid HIP-3 ↔ 국내 종목(LS증권) 차익거래 시스템

> **상태:** 초안 v0.4 (§13 #1·#2·#4 확정 반영)
> **성격:** 대형주 3종 [국내 ↔ 하이퍼리퀴드 perp] 차익거래의 **인프라 설계**. (구체 전략 로직은 추후 결정 — §6)

---

## 0. 문서 목적 / 사용 규칙

- 이 문서는 **확정된 설계 계약(contract)** 이다. 구현(Claude Code)은 이 문서 기준으로 진행하며, 빌드 도중 아키텍처를 임의로 바꾸지 않는다. 변경이 필요하면 **코드보다 이 문서를 먼저 갱신**한다.
- "무엇을·왜"는 이 문서에서 확정. 커넥터 **내부 구현 디테일**(LS TR 포맷, WS 재연결, OAuth2 갱신 등)은 구현 단계에서 채운다.
- **이번 버전 핵심:** 전략 로직은 분리(플러그인)하고, FX 헤지는 외부 프로세스(#2)에 위임한다. 즉 **이 시스템은 "거래 인프라 + 전략 플러그인 슬롯 + 노출 보고"** 까지 책임진다.
- `[OPEN]` 은 리뷰 시 결정할 미결 항목(§13).

---

## 1. 개요

국내 대형주 **3종**(삼성전자 005930, SK하이닉스 000660, 현대차 005380)에 대해, 같은 종목의 **국내 instrument**와 **하이퍼리퀴드 HIP-3 perp** 사이를 거래하는 차익거래 인프라.

- **HL 진영:** Trade.xyz(HIP-3) perp — `SAMSUNG`/`SKHYNIX`/`HYUNDAI`(정확 심볼 구현 시 확인), MM Presto Labs, USDC 증거금, 24/7, 펀딩.
- **국내 진영(LS):** ① 주식(spot) ② 단일종목 ETF ③ 주식선물 ④ 야간선물.
- **포지션:** `[국내 다리 + HL perp 반대 다리]`, 델타 중립 지향. (진입/청산 규칙 = 전략, §6에서 미정.)

---

## 2. 범위 / 비목표

**In scope (v1)**
- underlying 3종 × 국내 instrument 4종 ↔ HL perp 거래 인프라.
- **국내 2계좌(주식 / 선물옵션) 운용** 및 instrument별 주문 라우팅.
- **LS 장운영데이터 기반 instrument별 "주문 가능" 판별**(하드코딩 금지).
- 인스트루먼트 선택, 세션 서비스, 스프레드/시장상태 산출, 리스크 통제, 상태 영속화.
- **전략 플러그인 인터페이스**(전략 자체는 추후).
- **USD 노출을 외부 #2 환헤지 프로세스로 보고**(발행).

**Non-goals**
- 저지연/HFT 아님. 생산성 우선.
- 주식(spot) 공매도 미사용 — 숏은 주식선물·야간선물 매도 / 인버스 ETF.
- **FX 헤지 주문은 본 시스템이 하지 않음** — 외부 #2가 수행.
- 구체 전략 로직은 본 버전 범위 밖(인터페이스만 고정).
- 멀티 거래소 동시 운용은 v2.

---

## 3. 확정 설계 결정 (Locked)

| 항목 | 결정 | 근거 |
|---|---|---|
| 대상 underlying | 삼성·SK하이닉스·현대차 3종 | HL HIP-3 perp 상장 확인(Trade.xyz, Presto MM) |
| 국내 instrument | 주식 · 단일종목 ETF · 주식선물 · 야간선물 | 세션·비용·방향 따라 선택 |
| **국내 계좌** | **주식계좌 1개 + 선물옵션계좌 1개(총 2개)**. 주식·ETF→주식계좌 / 주식선물·야간선물→선물옵션계좌(야간선물도 **동일 계좌**) | 한국 시장 구조(현물/파생 분리) |
| 국내 다리 방향 | spot/ETF 롱 전용(공매도 X). 숏은 선물 매도 / 인버스 ETF | 공매도 미사용 + 양방향 가능 |
| HL 진영 | Trade.xyz HIP-3 perp, USDC, 에이전트 서명, 펀딩 | 기존 3계층 지갑 재사용 |
| 세션 판별 | LS 장운영데이터(JIF 실시간 + 휴장일)로 instrument별 판별 | 휴장·VI·동시호가 대응 |
| **전략 로직** | **미정. ArbEngine은 Strategy 인터페이스 뒤 플러그인** | 인프라를 전략 비종속으로 구축 |
| **FX 헤지** | **외부 #2 프로세스에 위임. 본 시스템은 USD 노출만 보고** | 기존 환헤지 프로그램 재사용 |
| 언어/런타임 | 64비트 Python, 단일 프로세스, asyncio | 생산성 우선 |
| 국내 브로커 | LS 신규 Open API(REST+WS, OAuth2). xingAPI 미사용 | 64비트·언어 무관 |
| 영속화 | SQLite | 단일 프로세스 재시작 복구 |

---

## 4. 아키텍처

단일 64비트 Python 프로세스. 커넥터/서비스는 asyncio 태스크 + 감시·자동 재연결. **FX 헤지와 전략 결정은 이 프로세스 밖**(전략은 플러그인 슬롯, FX는 외부 #2).

```
 [Hyperliquid HIP-3]                 [LS Open API]
   WS mark / REST(agent)        WS 시세·체결·장운영 / REST 주문·잔고
        │                    ┌────────────┴───────────┐
   ┌────┴─────┐              │   LS Gateway            │
   │HL Gateway│              │  ├ 주식계좌  (spot/ETF) │
   └────┬─────┘              │  └ 선물옵션계좌(선물/야간)│
        │                    └───┬─────────────────┬───┘
        │                        ▼                 │
        │                 ┌────────────┐           │
        │                 │SessionService│◄─────────┘ (장운영데이터)
        │                 └──────┬───────┘
        │                 ┌──────┴───────────┐
        │                 │InstrumentSelector│
        │                 └──────┬───────────┘
        └───────────┬───────────┘
                    ▼
          ┌───────────────────────┐
          │  Arb Engine           │
          │  └ Strategy (플러그인, │  ← 전략 로직 미정 (§6)
          │     인터페이스만 고정) │
          └──────┬──────────┬─────┘
                 ▼          ▼
          ┌──────────┐ ┌──────────┐         ┌─────────────────────┐
          │Risk/Pos  │ │State Store│        │ 외부 #2 환헤지 프로세스 │
          │Manager   │ │+ Monitor │         └─────────▲───────────┘
          └────┬─────┘ └──────────┘                   │
               └──────► FXExposureReporter ───(USD 노출 발행)──┘
```

**데이터 흐름:** 장운영데이터 → SessionService(instrument별 거래가능·레퍼런스) → InstrumentSelector(국내 instrument 결정) → ArbEngine이 MarketState 구성 → **Strategy 플러그인**이 주문 의도 산출 → RiskManager 검증 → LSGateway(계좌 라우팅)/HLGateway 주문 → 체결·상태저장. 별도로 **FXExposureReporter가 USD 순노출을 외부 #2로 발행**.

---

## 5. 컴포넌트 계약 (인터페이스 레벨)

> ⚠️ 메서드 이름은 **우리 래퍼 인터페이스**이며 브로커 실제 함수명이 아니다. LS는 TR 코드 기반, HL은 `hyperliquid-python-sdk` 함수에 대응.

### 5.1 LSGateway (2계좌: 주식 1 + 선물옵션 1)
- **책임:** LS Open API(REST `https://openapi.ls-sec.co.kr:8080`, OAuth2 Bearer + `tr_cd`) 연결·인증·시세·**장운영데이터**·주문·잔고. **주식계좌·선물옵션계좌 2개를 보유하고 instrument로 라우팅.**
- **인증은 계좌별(확정 v0.4):** appkey/appsecret이 계좌마다 다르므로 **계좌별 OAuth2 토큰·REST 컨텍스트**를 분리한다. 주문/조회는 대상 계좌의 토큰으로 전송.
- **계좌 라우팅:** 주식·ETF → 주식계좌(`CSPAT00601` 등) / 주식선물·야간선물 → **단일 선물옵션계좌**(선물옵션 주문 TR, 주간·야간 공용). 잔고·증거금·미체결은 **계좌별로 분리 추적**.
- **인터페이스 → LS TR 매핑:**

| 래퍼 메서드 | LS TR (REST/WS) | 계좌 |
|---|---|---|
| `connect()` / 토큰관리 | OAuth2 access_token 발급·자동 갱신 | 공통 |
| `subscribe_market_status()` | WS `JIF`(장운영정보) + 휴장일/운영시간 조회 TR (구현 시 확인) | 공통 |
| `subscribe_quotes()` | WS 주식 `H1_`·`NH1`, 선물 호가 TR | 공통 |
| `subscribe_fills()` | WS `SC0`~`SC4` | 양 계좌 |
| `place_order(stock)` | REST `CSPAT00601` | 주식 |
| `place_order(future)` | REST 선물옵션 주문 TR (구현 시 확인) | 선물옵션 |
| `amend/cancel_order()` | `CSPAT00701`/`CSPAT00801` (선물은 선물 TR) | 해당 계좌 |
| `get_positions()` | `CSPAQ12300`·`t0424`(현물) / 선물 잔고 TR | 계좌별 |
| `get_balance()` | `CSPAQ22200`(예수금)·`CDPCQ04700` / `FOCCQ33600`(증거금) | 계좌별 |
| `get_open_orders()` | `CSPAQ13700`(미체결) | 계좌별 |

- **이벤트:** `on_market_status`, `on_quote`, `on_fill`, `on_order_ack`, `on_disconnect`
- **제약:** 일 5,000 REST + TR별 초당 한도(예 조회 초당 2회). 시세·체결·장운영은 WS, REST는 주문·주기 대사. 참고 래퍼: `LsApiHelper`/`programgarden-finance`(현물·선물·실시간 포함).

### 5.2 HLGateway
- **메서드:** `connect()`, `subscribe_mark(coin)`, `subscribe_fills()`, `place_order(...)`, `cancel_order(...)`, `get_positions()`, `get_margin()`, `get_funding(coin)`
- **주의:** HIP-3는 빌더(Trade.xyz) 오라클·유동성 의존. 국내장 개장 중엔 perp 오라클이 기관 spot 참조, 마감 후엔 자가 가격발견 → 마감 구간 mark는 spot 기준 아님(§8). 펀딩 주기·심볼·`dex:COIN` 형식 실측.

### 5.3 SessionService
- **책임:** LS 장운영데이터 소비 → underlying별 `{instrument → (tradeable?, 동시호가?, is_live_reference?)}` 실시간 산출.

### 5.4 InstrumentSelector
- **책임:** (underlying, 방향, 세션 맵) → 최적 국내 instrument + **대상 계좌** 결정. 기준: 가용성 → 순비용 → 유동성.

### 5.5 ArbEngine (전략 비종속 오케스트레이터)
- **책임:** MarketState 수집·정규화 → `Strategy.evaluate()` 호출 → 반환된 주문 의도를 RiskManager 검증 → 게이트웨이·계좌 라우팅 → 체결 반영. **결정 로직은 갖지 않음**(전략 플러그인이 담당).

### 5.6 RiskManager
- **상태(1급):** 인벤토리·순델타·HL 마진비율·**계좌별 가용자금/증거금**·레퍼런스 가용성.
- 전략 비종속 가드(한도·kill-switch·레퍼런스 가드)는 지금 고정. 전략 의존 임계값은 전략 확정 후.

### 5.7 FXExposureReporter (외부 #2 연동)
- **책임:** 본 시스템의 **국내 다리 KRW 명목 + 환율**을 계산 → 외부 #2 환헤지 프로세스로 **노출 데이터 전송(보고)**. #2가 USD 환산·헤지 수행. 본 시스템은 USD/KRW 선물 주문·계좌를 갖지 않음.
- **채널(확정 v0.4):** 기존 `SignalLink`(TrdBot) 재사용 — **UDP 8888 브로드캐스트**로 피어 발견/하트비트(`HELLO/BYE`, 5초), **TCP(동적 포트)**로 메시지 전송(`<ID>\t<Name>\t<Msg>\n`, UTF-8). Msg 본문 = JSON Signal.
- **메시지 스키마(Signal):** `{id, fx, total_domestic, total_coin, token, datetime}`
  - `total_domestic = 0` (별도 국내 버킷 미보고)
  - `total_coin =` (주식잔고 × 평단) + (주식선물 매수계약수 × 평단 × **10 승수**) + (레버리지 ETF × 평단 × **2**) — KRW 명목
  - `fx` = 환율, `id` = 멱등키(uuid), `token` = 공유 시크릿(env), `datetime` = 전송 시각
  - 국내 다리는 **전략상 매수(롱) 전용** → `total_coin`은 롱 명목만 합산(국내 숏 없음). 중복은 `id` 멱등키로 #2가 필터.

### 5.8 StateStore / Monitor
- SQLite 영속화(재시작 복구), 로깅, 알림(임계·연결끊김·체결실패·데드존·노출보고 실패).

---

## 6. 전략 인터페이스 (전략 로직 미정)

> 구체 전략은 **추후 결정**. 인프라는 전략 비종속으로 구축하고, 전략은 아래 인터페이스 뒤 **플러그인**으로 교체한다.

- **Strategy 계약:** `evaluate(market_state) -> list[OrderIntent]`
- **MarketState (underlying별):** live 레퍼런스 instrument·가격, HL mark, FX, 세션 맵, 현재 포지션·인벤토리(instrument·계좌별), 비용 모델.
- **OrderIntent:** `(venue, account, instrument, side, qty, type, price?)`.
- ArbEngine은 위 계약만 호출. 전략 교체는 config로 선택.
- **후보(미확정, 참고용):** 인벤토리-플렉스 밴드 수렴, 야간 갭 포착 + 펀딩 캐리 등 — 확정 시 §6 상세화.

---

## 7. 세션 모델 (장운영데이터 기반, instrument별)

전역 단일 상태가 아니라 **instrument별 세션 맵**(SessionService가 LS 장운영데이터로 산출).

| 구간(대략, 확정은 장운영데이터) | 거래가능 국내 instrument | 레퍼런스 | 계좌 |
|---|---|---|---|
| 정규장 09:00–15:30 | 주식·ETF·주식선물 | 주식/주식선물 | 주식·선물옵션 |
| 장 전후 시간외·NXT | 주식(시간외)·NXT | 제한적 | 주식 |
| 애프터마켓 ~20:00 (2026-09-14 시행) | 주식·주식선물 | 주식/주식선물 | 주식·선물옵션 |
| 데드존(애프터마켓 종료~익일 개장 전) | 없음 | 없음 → 신규 진입 금지 | — |

- **개별주식선물은 오버나이트(야간) 세션이 없다.** 종전 "파생 야간(~05:00)" 가정은 폐기(§부록). 2026-09-14부터 **애프터마켓**으로 주식·주식선물이 ~20:00까지 연장 거래.
- **구간·시각은 장운영데이터(JIF)로 판정**하며 하드코딩하지 않는다(애프터마켓 포함).
- **[확정 v0.4]** 애프터마켓 선물 = 기존 `KR_STOCK_FUTURE`의 **세션 연장**(같은 계약). 따라서 `KR_NIGHT_FUTURE` instrument와 `SessionPhase.NIGHT_DERIV`는 **제거**하고, 애프터마켓용 `SessionPhase`(예: `AFTER_MARKET`)를 추가한다. 국내 다리는 전략상 **매수(롱) 전용**.
- HL은 항상 24/7. 동시호가·VI·휴장일은 장운영데이터로 감지해 보수적 처리.

---

## 8. 리스크 통제 (전략 비종속 골격)

- **레퍼런스 가용성 가드:** live 국내 레퍼런스 없으면(데드존·HL 자가발견 구간) 신규 진입 금지, 스테일 가격 판단 금지.
- **계좌별 자금/증거금 버퍼:** 주식계좌(예수금)·선물옵션계좌(증거금)·HL(USDC) 각각 독립 버퍼. 정산 비대칭(KRW T+2 vs USDC 즉시) 고려.
- **HL 청산 버퍼:** 마진비율 가드. HIP-3 청산·오라클 거동 보수적.
- **Kill-switch:** 연결끊김·체결실패·한도초과·데드존·노출보고 실패 시 신규 진입 중단 + 알림.
- 인벤토리/델타 등 전략 의존 한도는 전략 확정 후 임계값 주입.

---

## 9. FX 헤지 — 외부 #2 위임

- 본 시스템은 **국내 다리 KRW 명목 + 환율을 계산해 #2로 보고만** 한다. USD 환산·헤지비율·USD/KRW 선물 주문·계좌는 모두 #2 소관.
- 보고 값(§5.7 Signal): `total_domestic=0`, `total_coin=` (주식잔고×평단)+(주식선물 매수계약×평단×10)+(레버ETF×평단×2), `fx`=환율. 변동 시 재계산·재전송.
- **본 시스템 책임 = 정확·적시 노출 데이터 전송(보고).** 보고~헤지 사이 FX 갭 리스크는 #2/집계 레벨에서 관리.
- **전송 프로토콜 확정(v0.4):** 기존 `SignalLink`(UDP 8888 발견 + TCP 메시지) 재사용. 중복은 `id` 멱등키로 처리(§5.7).

---

## 10. 데이터 모델 (SQLite 초안)

| 테이블 | 핵심 컬럼 |
|---|---|
| `positions` | underlying, venue, instrument, account, side, qty, avg_price, updated_at |
| `orders` | order_id, venue, instrument, account, side, qty, price, type, status, ts |
| `fills` | fill_id, order_id, qty, price, fee, ts |
| `inventory` | ts, underlying, signed_units, krw_notional, hl_notional, net_delta |
| `market_state` | ts, underlying, ref_instrument, kr_price, hl_mark, fx |
| `session_log` | ts, underlying, instrument, tradeable, is_reference |
| `fx_exposure_report` | ts, exposure_usd, sent_ok | (→ #2로 발행한 내역) |
| `events` | ts, level, component, message |

재시작 시 `positions`+`inventory`+미체결 `orders`로 복구.

---

## 11. 설정 / 파라미터 (config.yaml)

- underlying·instrument 목록, 세션별 instrument 우선순위.
- **계좌:** 주식계좌·선물옵션계좌 번호/상품코드.
- **외부 #2:** 노출 발행 엔드포인트·프로토콜·주기.
- 전략: 플러그인 선택자(임계값 등 세부는 전략 확정 후).
- 한도: LS 5,000/일 + TR별 초당, HL 마진비율 하한, 일일 손실 한도.
- **실행 모드:** `KP_MODE`(env)로 사용자가 전환 — `paper`(모의, 기본·안전) | `live`(운영). 엔드포인트/안전 게이트 선택용 플래그.
- 비밀값(평문 파일 저장 금지): **저장 = Windows 자격증명관리자(DPAPI, keyring), env는 오버라이드/폴백** (`SecretProvider`). 등록 `python -m kp_arb.secrets_cli set <NAME>`. 이름 — **LS 키는 계좌별**: 주식 `LS_STOCK_APPKEY/APPSECRET/ACCT/ACCT_PW`, 선물옵션 `LS_DERIV_APPKEY/APPSECRET/ACCT/ACCT_PW`. HL `HL_AGENT_KEY`.

---

## 12. 기술 스택

- Python 3.11+ / asyncio
- `hyperliquid-python-sdk`, `eth-account`(EIP-712)
- LS: `aiohttp`(REST) + `websockets`(WS); `LsApiHelper`/`programgarden-finance` 참고
- `aiosqlite`, `pydantic`(설정·검증), `structlog`(로깅), `pandas`(분석)
- 외부 #2 연동: 기존 #1↔#2 채널과 동일 스택

---

## 13. 미결 사항 (Open Questions)

**인프라**
1. ~~야간선물 커버리지~~ **[확정 v0.4]** 개별주식선물 **오버나이트 없음**. **2026-09-14부터 애프터마켓 ~20:00**(주식·주식선물). 애프터마켓 선물 = `KR_STOCK_FUTURE` 세션 연장 → `KR_NIGHT_FUTURE`/`NIGHT_DERIV` **제거**, `AFTER_MARKET` phase 추가. 시각은 장운영데이터(JIF) 판정. 국내 다리 매수 전용. → §7.
2. ~~#2 노출 발행 인터페이스~~ **[확정 v0.4]** 기존 `SignalLink`(UDP 8888 발견 + TCP `<ID>\t<Name>\t<Msg>\n`) 재사용, JSON Signal 스키마. → §5.7·§9.
3. **계좌 상품코드 / 선물 주문 TR** — LS 선물 주문 **구현 완료**: 신규 `CFOAT00100` / 정정 `CFOAT00200` / 취소 `CFOAT00300` (POST `/futureoption/order`, 종목코드 config 주입). **미확정:** 계좌번호 체계, 선물 종목코드값. (참고: `CCENT001/002/003` = KRX야간파생 위탁 주문 — 애프터마켓 적용 여부 별도 확인.)
   - **[라이브 확인 v6.1]** OAuth2 `scope="oob"` 필수. **모의투자 성공 rsp_cd="00136"**(운영 "00000"). 주식 예수금 CSPAQ22200 실필드: `CSPAQ22200OutBlock2.Dps`(예수금)·`MnyOrdAbleAmt`(현금주문가능). 계좌번호는 대시 없이 11자리(`55504974701`). 선물 FOCCQ33600 조회는 실패 → path/파라미터 재확인 필요. 파서 필드 정합은 후속.
4. ~~HL perp 사양~~ **[확정 v0.4]** 심볼 `SAMSUNG`·`SKHYNIX`·`HYUNDAI`(005930/000660/005380), 최대 **10x**. 펀딩 주기·`dex:COIN` 정확 표기는 라이브 시 SDK로 확정.
5. **자본 배분 / 리스크 사이징** — 추후 리스크 관리 로직과 함께 결정(지금 보류).

**전략 확정 후 (지금은 보류)**
6. 진입/청산 규칙, 인벤토리·델타 한도, 밴드 파라미터.
7. 정규장 레퍼런스를 주식 vs 주식선물 중 무엇으로(베이시스 처리).
8. 데드존에서 HL 다리 캐리 vs 축소.

---

## 부록: 폐기/제외·변경 이력 (재논의 방지)

- C++/Boost.Beast → 생산성 우선이라 Python.
- xingAPI(OCX, 32비트) → LS 신규 REST/WS로 회피.
- 멀티프로세스 IPC(시스템 내부) → 단일 프로세스로 단순화.
- 주식 spot 공매도 → 미사용(숏은 선물/인버스).
- 세션 시간 하드코딩 → LS 장운영데이터로 대체.
- **FX 헤지 자체 구현 → 외부 #2 위임(노출 보고만).** (v0.3)
- **전략 로직 문서 내 확정 → 추후 결정, 인터페이스만 고정.** (v0.3)
- **개별주식선물 오버나이트 야간세션 가정 → 폐기. 2026-09-14 애프터마켓(~20:00)으로 대체(장운영데이터 판정).** (v0.4)
- **#2 전송 스키마 `{source_id, exposure_usd, ts}` → 기존 `SignalLink` Signal `{id, fx, total_domestic, total_coin, token, datetime}`로 확정. "발행" 용어 → "노출 데이터 전송/보고".** (v0.4)
- **국내 다리 방향: 숏(선물 매도) 포함 가정 → 전략상 매수(롱) 전용으로 축소(숏은 HL 다리).** (v0.4)
