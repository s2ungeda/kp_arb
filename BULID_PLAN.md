# BUILD_PLAN.md — 단계별 작업 명령 (Claude Code)

skeleton(green baseline)에서 시작해 위로 쌓는 작업 사다리. **한 번에 한 블록**만 클로드 코드에 복붙해 진행한다.

## 사용법
- 한 블록 = 한 작업. 끝나면 `check.sh`가 green인지 보고받고, **green이면 git 커밋**(체크포인트) 후 다음 블록.
- 여러 파일 건드리는 블록은 먼저 **Plan Mode**(Shift+Tab 2번)로 계획부터.
- mock/오프라인으로 만들 수 있는 것(Phase 1~5)을 먼저. **라이브 전환(Phase 6)은 OPEN 항목 확정 + 키 준비 후.**
- 막히면 멈추고 사람에게. 추측으로 `[OPEN]`을 채우지 말 것.

## 공통 규칙 (모든 블록에 적용 — 각 명령 끝의 `[규칙]`이 이걸 가리킴)
> mock/리플레이로만 테스트(라이브 API 호출 금지) · 비밀키는 env로만(평문 금지) · tests/는 계약(약화·수정 금지, 코드가 테스트에 맞춘다) · DESIGN.md/CLAUDE.md 계약 벗어나지 말 것 · 끝나면 `./.claude/hooks/check.sh` 전부 green.

---

## Phase 0 — 오리엔테이션 (코드 변경 없음)

```
이 레포의 CLAUDE.md와 DESIGN.md를 먼저 읽어. 아직 코드는 절대 고치지 마.
1) DESIGN.md 기준으로 이 시스템이 뭘 하는지와 불변식(계좌 라우팅 / 주식 공매도 금지 /
   FX 외부 #2 위임 / 세션 주도 / 데드존)을 네 말로 5줄 이내 요약.
2) pip install -e ".[dev]" 후 ./.claude/hooks/check.sh 를 돌려 ruff·mypy·pytest가
   전부 green인지 확인하고 결과 보고.
3) DESIGN.md §13과 README '다음 단계'에서 다음 작업 후보 3개를 '제안만' 해. 구현 금지.
```

---

## Phase 1 — LS 게이트웨이 (전부 mock/오프라인, 키 불필요)

### 1-1. OAuth2 토큰 매니저
```
LS 게이트웨이의 OAuth2 토큰 매니저부터 구현해.
완료 조건: tests/test_ls_token.py에 토큰 발급 / 만료 전 자동 갱신(가짜 시계 사용) /
발급 실패 시 재시도 테스트를 추가하고 통과. HTTP는 mock transport로만.
[규칙]
```

### 1-2. LS REST 클라이언트 코어
```
LS REST 호출 공통 계층을 구현해: base_url, Bearer 토큰 주입, tr_cd 헤더,
레이트리밋 가드(일 5,000회 + TR별 초당 한도, 예: 조회 초당 2회), 지수 백오프 재시도.
완료 조건: tests/test_ls_rest.py에 헤더 구성·레이트리밋 차단·재시도 테스트 추가하고 통과.
실제 네트워크 대신 mock transport + 녹화 응답 픽스처 사용.
[규칙]
```

### 1-3. LS 주문 (현물)
```
LSGateway.place_order(현물)를 REST(CSPAT00601 계열)로 구현해. OrderIntent를 받아
routing.account_for로 계좌를 정하고 주문 후 order_id 반환. 정정/취소도 추가(원주문 컨텍스트 보존).
완료 조건: tests/test_ls_order.py에 '주식/ETF→주식계좌, 주식선물/야간선물→선물옵션계좌' 라우팅과
주문 응답 파싱 테스트 추가하고 통과. mock transport + 녹화 픽스처. 계좌 라우팅 계약 깨지 마.
[규칙]
```

### 1-4. LS WebSocket (시세·체결·장운영)
```
LS WS 클라이언트를 구현해: 실시간 호가(H1_/NH1) + 체결(SC0~SC4) + 장운영(JIF) 구독,
연결 끊김 시 자동 재연결·재구독. on_quote / on_fill / on_market_status 이벤트로 노출.
완료 조건: tests/test_ls_ws.py에 녹화 프레임 재생 → 이벤트 발생 검증과 재연결 복구 테스트 추가하고 통과.
가짜 WS 서버/녹화 프레임으로만.
[규칙]
```

### 1-5. LS 잔고·포지션 (계좌별)
```
LSGateway.get_balance / get_positions를 계좌별로 구현해
(예수금 CSPAQ22200·투자가능 CDPCQ04700, 잔고 CSPAQ12300·t0424, 선물 증거금 FOCCQ33600).
완료 조건: tests/test_ls_account.py에 주식계좌/선물옵션계좌 잔고·포지션을 분리 조회하는 테스트 추가하고 통과.
녹화 픽스처 사용.
[규칙]
```

---

## Phase 2 — SessionService 실데이터 연결

### 2-1. 장운영데이터 → 세션 맵
```
SessionService를 구현해: LS on_market_status(JIF + 휴장일)를 받아 SessionPhase로 매핑하고,
기존 session.py의 순수 로직(build_session 등)으로 underlying별 instrument 상태 맵을 산출.
완료 조건: tests/test_session_service.py에 '녹화 JIF 프레임/휴장일 입력 → 올바른 tradeable·reference 맵'
테스트 추가하고 통과. session.py 순수 함수는 재사용하되 수정 금지(필요하면 제안만).
[규칙]
```

---

## Phase 3 — HL 게이트웨이 (키는 env, 테스트는 mock)

### 3-1. HL 연결 + 에이전트 서명
```
HLGateway.connect와 EIP-712 에이전트 지갑 서명을 구현해. 키/시크릿은 env에서만 읽어.
완료 조건: tests/test_hl_auth.py에 서명 페이로드 구성·헤더 테스트 추가하고 통과.
실제 키 없이 동작하도록 서명 부분은 주입 가능한 서명자(mock)로 테스트. 평문 키 금지.
[규칙]
```

### 3-2. HL 마크·펀딩·포지션·주문
```
HLGateway의 subscribe_mark / get_funding / get_positions / place_order / cancel_order를 구현해
(Trade.xyz HIP-3 perp, 심볼은 config로). HL 주문은 KR 계좌를 갖지 않음.
완료 조건: tests/test_hl_market.py와 test_hl_order.py에 마크 수신·펀딩 조회·주문 응답 파싱 테스트 추가하고 통과.
녹화 픽스처 + mock transport.
[규칙]
```

---

## Phase 4 — 엔진 · 리스크 · FX보고 · 상태저장

### 4-1. ArbEngine 오케스트레이션
```
ArbEngine을 구현해: 게이트웨이 시세 + SessionService + InstrumentSelector + spread + fx로
underlying별 MarketState를 조립 → Strategy.evaluate() 호출(지금은 NoopStrategy) →
RiskManager 검증 → 게이트웨이·계좌 라우팅. 엔진은 결정 로직을 갖지 않는다.
완료 조건: tests/test_engine.py에 'mock 게이트웨이 입력 → MarketState 정확 조립' 및
'Noop 전략이면 주문 0건' 테스트 추가하고 통과.
[규칙]
```

### 4-2. InstrumentSelector
```
InstrumentSelector를 구현해: (underlying, 방향, 세션 맵) → 최적 국내 instrument + 대상 계좌.
기준: 가용성 → 순비용 → 유동성. 숏 방향에서 주식 spot은 선택 불가(공매도 금지).
완료 조건: tests/test_instrument_selector.py에 정규장 롱/숏, 야간 각각의 선택 테스트와
'숏인데 spot 선택 안 됨' 테스트 추가하고 통과.
[규칙]
```

### 4-3. RiskManager 가드
```
RiskManager를 구현해(전략 비종속 골격): 레퍼런스 가용성 가드(데드존 신규 진입 금지),
계좌별 자금/증거금 버퍼, HL 마진비율 가드, kill-switch. 전략 의존 임계값은 config로 주입만.
완료 조건: tests/test_risk.py에 '데드존이면 진입 거부', '마진비율 하한 위반 시 거부',
'버퍼 부족 시 거부' 테스트 추가하고 통과.
[규칙]
```

### 4-4. FXExposureReporter
```
FXExposureReporter를 구현해: fx.usd_exposure로 USD 순노출을 계산하고 외부 #2로 발행.
발행 채널은 인터페이스(Protocol) 뒤로 추상화하고, 실제 프로토콜은 [OPEN §13 #2]이므로 mock sink로 둔다.
이 시스템은 USD/KRW 선물 주문을 내지 않는다.
완료 조건: tests/test_fx_reporter.py에 노출 계산 정확성과 'mock sink로 발행됨' 테스트 추가하고 통과.
[규칙]
```

### 4-5. StateStore (SQLite)
```
StateStore를 구현해: DESIGN.md §10 스키마(positions/orders/fills/inventory/market_state/
session_log/fx_exposure_report/events)로 SQLite 영속화 + 재시작 복구.
완료 조건: tests/test_state_store.py에 임시 DB로 '저장→재로딩 복구'와 '미체결 주문 복구' 테스트 추가하고 통과.
[규칙]
```

---

## Phase 5 — 통합 드라이런 (mock end-to-end, 라이브 없음)

### 5-1. 단일 프로세스 결선
```
지금까지 컴포넌트를 단일 asyncio 프로세스로 결선해 드라이런 엔트리포인트를 만들어.
mock 게이트웨이 + NoopStrategy로 시세 수신→MarketState→리스크→상태저장→FX 노출 발행까지 한 바퀴 돌게.
완료 조건: tests/test_integration.py에 녹화 픽스처 기반 e2e 스모크 테스트(주문 0건, 노출 보고 1회 이상,
상태 저장됨) 추가하고 통과. 라이브 호출 없음.
[규칙]
```

---

## Phase 6 — 라이브 전환 (게이트: 먼저 OPEN 해결 + 키 준비)

> **여기부터는 Phase 1~5가 전부 green이고, 아래 OPEN이 확정된 뒤에만.**
> - §13 #3 LS 주문 TR·계좌 상품코드 확정
> - §13 #2 #2 노출 발행 프로토콜 확정
> - §13 #4 HL perp 정확 심볼·펀딩 주기 확정
> - §13 #1 삼성·하이닉스·현대차 야간선물 거래 여부·시간 확인

```
모의투자(페이퍼) 환경에서 LS 게이트웨이를 '읽기 전용'으로 먼저 라이브 연결해
(시세·장운영·잔고만, 주문 금지) 픽스처가 아닌 실데이터로 SessionService와 MarketState가
정상 동작하는지 검증해. 주문 경로는 아직 막아둬. 키는 env에서만.
완료 조건: 페이퍼 환경 실데이터로 세션 맵·스프레드가 정상 산출됨을 로그로 확인. check.sh green 유지.
[규칙]
```
(이후: 페이퍼 주문 → 소액 라이브 → 정상화 순으로 단계적 개방.)

---

## Phase 7 — 전략 (추후, DESIGN.md §6)

> 인프라가 안정되고 §13 #4·#6·#7(밴드 파라미터·레퍼런스 정의·데드존 정책)이 확정된 뒤.

```
프리미엄 분포 분석(일중 vs 야간 분리)을 먼저 하고, 그 결과로 밴드 파라미터를 정한 뒤
Strategy 인터페이스(evaluate)를 구현해. 인터페이스는 고정, 인프라/테스트는 깨지 마.
완료 조건: tests/에 전략 시그널 단위 테스트(밴드 진입/청산, 데드존 무진입) 추가하고 통과.
[규칙]
```

---

## 진행 규율 메모
- **체크포인트:** 블록 green마다 `git commit`. 엉뚱해지면 `git`으로 되돌림.
- **반복 실수:** 클로드가 같은 실수 반복하면 그 교훈을 CLAUDE.md에 한 줄 추가.
- **순서 건너뛰기 금지:** Phase 6(라이브)을 1~5 green 전에 시작하지 말 것.
- **OPEN 가드:** `[OPEN]` 항목은 추측 금지 — 멈추고 확인.
