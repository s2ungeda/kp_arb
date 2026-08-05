# DESIGN — 수동 주문 화면 (일반 주문창)

> [DESIGN.md](DESIGN.md) §6.3의 상세. 계약의 일부다(DESIGN.md와 함께 읽는다).
> 원본 참조: 델파이 `C:\project\Dalin_2026\Src\wins\UNormalOrderEx.pas/.dfm`.

## 6.3 수동 주문 화면 (일반 주문창, v1=MVP — 확정 2026-08-03)

사람이 직접 한 건씩 주문을 내는 창. 자동 전략(§6.2)과 **독립**. 화면은 코어에 명령만 보내고 주문·검증·기록은 코어(§6.2-0 구조 동일: 코어 클라이언트, 화면 스레드 네트워크 금지).

**HL/LS는 UI가 크게 달라 화면을 분리한다**(확정 2026-08-05) — **HL 전용 `order_hl`**(먼저 구현, main.bat 메뉴 "HL 일반주문"), **LS 전용 `order_ls`**(후속). **코어 명령(`manual_*`)·스냅샷(`/manual_state`)은 두 화면 공용.** 아래 §1은 HL 창 기준(LS 창은 공매도·계좌 표시 등이 달라 별도 확정).

**경계 재설정**: 델파이 원본은 화면이 직접 엔진(TradeCore/ApiManager/QuoteBroker/TradeBroker)에 붙어 주문했다. 우리는 그 능력을 **코어**가 갖고(이미 있음: `place_order`/`cancel_order`, `get_open_orders`/`get_positions`/`get_balance`, 호가 사다리 `quote.bids/asks`), 화면은 입력·표시만 한다.

### 1) HL 창(`order_hl`) 구조 (확정 2026-08-05)

델파이 원본(`UNormalOrderEx`) 레이아웃 — **좌(입력+잔고) / 우(호가창) 2분할**. HL perp 전용이라 거래소 콤보 없음.

- **좌 상단**: 종목 콤보(취급 3종목의 HL perp), 매수/매도 라디오 + **'적' 버튼**(같은 줄, 세로 안 늘어나게) — '적'은 **누를 때만** 그 종목을 활성화하고 잔고/포지션 재조회(`manual_refresh`→`refresh_snapshot`, OrderBook 재동기). 콤보만 바꿔선 안 바뀜(델파이 SetSymbol). 호가단위 머지 콤보는 **오더북 위**(우측, `manual_hl_merge`→`set_hl_aggregation`).
- **좌 중단**: 수량, 단가. **지정가만**(시장가 제외). **호가/가격 모드 + 틱 오프셋** — **호가 모드**: 단가 = (매수=매수1호가 / 매도=매도1호가) + N틱, 시세 따라 자동 갱신. **가격 모드**: 호가 클릭 시 그 가격 그대로(틱 미적용). **가격은 틱에서 뽑은 소수 자리수로 통일**. **Reduce/Post 체크박스**(reduce_only / post_only=Alo 메이커).
- **좌 버튼**: 큰 색버튼 "종목 매수/매도"(매수 빨강/매도 파랑).
- **좌 잔고표**: 보유수량·평가금액·주문가능 / 평가손익·평균단가·청산가(Liq.Prc). (HL은 공매도 제약 없어 매도가능 없음 — LS 창에서만.)
- **우 호가창**: 헤더 없이 `가격 | 잔량` 2열(색으로 구분), 매도호가 파랑(위)·매수호가 빨강(아래). **현재가와 같은 호가만 노랑 바탕**(별도 현재가 행 없음). 가격 모드에서 클릭 시 그 가격을 단가에.
- **하단**: 상태바(주문 접수/거부 결과)만. **미체결 목록·정정·취소는 별도 화면 `order_list`**(구현됨, 메인 메뉴 "주문 리스트") — /manual_state의 open_orders를 폴링해 표시, `manual_amend`/`manual_cancel` 사용. HL·LS 공통.

### 2) 도메인 불변식·가드 (§3 강제)

- **공매도 금지 = 보유 초과 매도 금지**(전량 차단 아님 — 보유분 매도는 청산이라 정상): 코어가 **OrderBook(§5.9)** 으로 잔고를 실시간 유지(최초 REST 스냅샷 + 실시간 체결통보 SC* 증분, 조회 폴링 없음). LS 주식 매도 주문 시 코어가 **매도가능수량 = OrderBook 보유수량 − 미체결 매도수량**을 즉시(재조회 없이) 계산해, 주문수량이 이를 넘으면 거부(사유: 공매도 금지). 화면은 이 값을 표시하고 초과 입력 시 경고(이중 안전 — 화면 우회해도 코어가 막음). **주의**: OrderBook은 우리 시스템으로 낸 주문의 체결만 반영 — HTS 등 외부 거래는 안 잡히니, **창 열 때 + '잔고 새로고침' 버튼**으로 `get_positions` 재조회해 재동기(주문마다가 아니라 필요할 때만). LS 주식선물·HL perp는 양방향 허용(숏 가능).
- **계좌 라우팅**: `routing.account_for`로 결정(주식→KR_STOCK, 주식선물→KR_DERIV, HL→HL). 화면은 관여 안 함.
- **세션 데드존**: 수동 주문은 사람이 판단하므로 데드존/세션 가드로 **막지 않는다**(닫혀 있으면 LS/HL가 거부). 자동 판정 루프(§6.2)와 무관.
- **WS 건강 경고(Phase 8-6, 확정 2026-08-05)**: WS 끊김/시세 무데이터(N초, env `KP_WS_MAX_IDLE_S` 기본 10초)여도 **수동 주문은 막지 않고 발주하되, 응답 `warnings`에 사유를 실어 화면이 빨간 글씨로 경고**한다(사용자 확정: "차단 없이 경고만"). 판정은 순수 함수 `ws_status.order_block_reason`. **자동 발주(§6.2, 추후)는 같은 게이트로 하드 차단**(신규 진입 금지 = 데드존 불변식).

### 3) 수량 단위

수동은 **다리 하나씩**이라 그 거래소 **네이티브 단위**로 입력(자동의 1:10 페어 환산과 다름): LS 주식=주, LS 주식선물=계약, HL=계약. ('세트'·페어 환산 없음 — 페어 수량 규칙은 §6.2-3 자동 전용.)

### 4) 코어 신규 명령 (화면 → 코어, `POST /command`)

- `manual_order` {instrument, underlying, side, order_type(=limit), qty, price, reduce_only?, post_only?} → 공매도·라우팅 검증 후 `place`, `order_id`. reduce_only/post_only는 **HL 전용**(LS는 무시).
- `manual_amend` {order_id, price} → `amend_price`(잔량 기준 정정 = 취소+신규).
- `manual_cancel` {order_id} → `cancel`.
- `manual_hl_merge` {underlying, n_sig_figs, mantissa} → `set_hl_aggregation`(HL 호가단위 머지, WS 재구독).
- `manual_refresh` {} → `refresh_snapshot`(잔고/포지션 재조회 → OrderBook 재동기, '적' 버튼·HTS 외부거래 반영).
- `GET /manual_state` (별도 엔드포인트 — /state를 무겁게 안 하려 분리): 취급 종목별 **호가 사다리·포지션·평균단가·평가손익·평가금액·청산가·매도가능·잔고·틱** + **전체 미체결**. OrderBook·`quotes` 메모리 읽기(조회 폴링 없음). 화면 뒷단 스레드가 폴링해 표시.

### 5) v2 [OPEN] (이후)

레버리지·마크가 기반 최대수량·증거금 자동계산, 예상체결가(est-pr) 표시, 시장가.
