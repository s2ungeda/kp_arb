# DESIGN-hl-priority-fee.md — HL 우선수수료(IOC) 시간구간

사용자 지시 2026-11-17: "시간별로 하기로 했음. 4개의 시간구간 만들고 선택한 시각에만 IOC와 수수료 반영, 단 일반주문은 제외.
체크박스, 시작시각, 종료시각, 수수료. / 잔고조회 화면에 탭으로 분류해서 추가". 과금용 HYPE 잔고는 있음("잔고좋").
공식 근거: HL 문서 Priority fees(docs/hl-trading-rules.md §1.3), 실측 11-04(kp_arb/hl_block_fees.py). 이 문서가 계약이고
코드는 이 문서를 따른다. 설계가 미정인 [OPEN]이 있는 동안은 그 부분을 구현하지 않는다(CLAUDE.md L16); 실측으로만 알 수 있는
[OPEN]은 구현 뒤 실측(§5)으로 닫는다.

## 0. 한 줄 요약
공통 시간구간 4개(체크·시작·종료·수수료 bp)를 두고, **지금 시각이 체크된 구간 안이면** 전략이 내는 HL 주문을 **IOC + 우선수수료
`{"p": bp × 10,000}`** 로 보낸다(bp 0이면 IOC만). 구간 밖이면 지금처럼 GTC(수수료 없음). **적용 안 되는 경우는 셋뿐(사용자
2026-11-18): ① 일반주문(일반주문창) ② NXT 개장매매 청산주문(자동청산) ③ 동시호가 HL 주문.** 적용 대상 = 체결쏴 HL 후주문·
NXT 개장매매 전략주문(진입·청산 세트).

## 1. 적용 범위
- 적용: 전략이 코어 안에서 내는 HL 주문 중 **체결쏴 HL 후주문(헤지)과 NXT 개장매매 전략주문(진입·청산 세트)**. 출처 `source`로
  일반주문창을 빼고, 아래 주문은 `priority_exempt`로 뺀다.
- **제외(사용자 2026-11-18 "NXT 개장매매 청산주문, 동시호가 HL 주문은 GTC로 한다")**:
  - 일반주문창(수동). (2026-11-18 시험 때 환경변수 `KP_PRIORITY_MANUAL=1`로 잠시 포함시켜 IOC+p 발주 확인 → 사용자
    "테스트 잘 되었음, 원복" → 스위치 제거. 일반주문창 HL 주문에 IOC·p를 붙이는 길은 코드에 없다. → **2026-11-19 사용자 "테스트용으로 빌드
    해줘, 운영 PC에서 해볼게"로 스위치 되살림**(수수료 0bp/8bp 구간을 번갈아 두고 안 닿는 IOC로 왕복 비교) → 계정 방식·bp별
    실측을 마치고 **같은 날 사용자 "시험 스위치 빼고 커밋해줘"로 다시 제거**. 지금 코드에 그 스위치는 없다.)
  - **동시호가 HL 주문**(프로세스 3 묶음·'정정' 버튼 신규 모두) — GTC 그대로(`priority_exempt=True`).
  - **메이커 전용 주문(`post_only`, ALO)** — 지금 전략 중엔 없다(HL선 선주문이 유일했는데 2026-11-18 제거). 일반주문창 Post
    체크는 어차피 제외. 규칙은 안전장치로 남긴다: 구간 안에서도 ALO 그대로(p도 안 붙임).
  - **호가창에 걸어 두는 전략 주문(`priority_exempt`)** — NXT 개장매매 **자동청산(익절)**. 평균가 × (1 ∓ 목표)라 맞붙지 않는 가격이라
    IOC면 즉시 거부돼 청산 주문이 걸리지 않는다. GTC 그대로.
- HL 정정(amend)은 IOC와 무관(IOC는 남지 않아 정정할 것이 없다). 취소도 그대로. LS 주문은 무관.
- 따라가기(peg_order)는 별도 프로세스라 이 설정이 주입되지 않는다(사실상 제외).

## 2. 설정 — 시간구간 4개 (공통, 종목 무관)
- 구간마다 `enabled`(체크) · `start`(HH:MM:SS, 두 자리씩) · `end`(HH:MM:SS) · `bp`(우선수수료, bp 단위 소수 가능 — 예 2 = 2bp,
  0.5 = 0.5bp).
- 판정(순수 `strategy_core.priority_window_for(now, windows)`): 체크된 구간 중 `start ≤ 지금 < end`인 **첫 구간**. 없으면 None
  (= GTC). 겹치면 위 구간이 이긴다. 자정 넘김(start ≥ end)은 거부(저장 때).
- `p = round(bp × 10,000)`(`priority_p`; 문서: 수수료율 = p ÷ 100,000,000, 1bp = p 10,000). **bp 0인 체크 구간은 IOC만**(p 안
  붙임 — 사용자 답 1 "체크된 구간은 전부 IOC"). **bp 상한 10**(사용자 2026-11-18 "수수료 최대 10까지 입력 가능하게" —
  공식 상한이 아니라 사용자가 정한 입력 한도. `PRIORITY_BP_MAX`): 저장 때 0~10 밖이면 거부, 화면 칸도 10 넘는 값은 안 받는다.
- 저장: 코어 설정 `GlobalSettings.hl_priority_fee` {windows: [{enabled, start, end, bp} × 4]} — `settings_global` 명령의 새 키
  (키가 없으면 기존 값 유지). 검사(`apply_hl_priority_fee`): 체크된 구간은 시각 둘 다 HH:MM:SS, start < end; bp는 유한수 ≥ 0
  (NaN·inf 거부). 틀리면 거부(아무 것도 안 바꿈). 저장본이 깨졌으면 복원 때 전부 빈 구간(= GTC).
- 과금 잔고는 **위임하지 않은 스테이킹 HYPE**(문서). 모자랄 때 동작은 문서에 없음 — [OPEN] 3(실측).
- **최소 잔고 10 HYPE**(공식 문서 for-developers/api/optimizing-latency §"Optimizing write latency" 6-4, 2026-11-13경 추가:
  "IOC and ALO priority fees require a minimum delegatable balance of 10 HYPE"). 스테이킹 계정에 위임 안 한 HYPE가 10개
  미만이면 p가 붙은 요청 전체가 거부된다(실측 11-18: 0.2 HYPE로 거부). 코드 가드는 두지 않고(L10 — 값은 문서에 있으나 "delegatable"
  의 정확한 뜻이 문서에 없음) 운영 전 사용자가 잔고를 맞춘다. 같은 절 6-1: 우선 처리는 Standard abstraction 계정만(Unified
  Account·Portfolio Margin 제외).
- **계정 방식 실측(2026-11-19, 개발 계정, 삼성 1계약·안 닿는 가격, 발주 왕복 중앙 ms, 대부분 10건씩)** — 수수료는 Manual(Standard)
  계정에서만 통한다. 운영 계정은 Portfolio Margin이라 지금은 효과 없음(운영 11-19 81건: 0bp·8bp 같음). 계정 방식 변경은 사용자 결정.
  | 계정 방식 | ALO | IOC 0bp | 2bp | 4bp | 6bp | 8bp |
  |---|---|---|---|---|---|---|
  | Manual(Standard) | ~445 | 737 | 663 | ~600 | 468 | 452 |
  | Unified | ~730 | 732 | 742 | 702 | 749 | 699 |
  한가한 낮·미체결 주문 기준. 체결 주문·개장 순간·취소(두 방식 모두 700~800)는 미확인. 경위 docs/worklog/2026-11-19.md.

## 3. 화면 — 잔고 조회 창의 둘째 탭
- 잔고 조회 창(DESIGN-balance-query)을 `ttk.Notebook` **탭 둘**로: `잔고`(지금 화면 그대로) / `우선수수료`.
- `우선수수료` 탭: 설명 한 줄("체크된 구간 안에서만 전략 HL 주문을 IOC + 우선수수료로(일반주문, 개장매매 청산주문, 동시호가
  HL주문 제외)" — 사용자 2026-11-18 문구) + 4행 `[ ] 구간 n  시작 [HH:MM:SS] ~ 종료 [HH:MM:SS]  수수료 [    ] bp` + 아래 상태줄·[저장].
  **저장 버튼 때만** 코어로(Enter·칸 이동 저장 없음 — 체결쏴 결정 74 방식). 열 때 뒷단 스레드가 `/state`의 settings를 한 번 읽어
  채우고(after()로 그림, L5), **읽기에 성공하기 전엔 저장 버튼이 잠긴다**(못 읽은 채 저장하면 빈 4행이 코어 구간을 지움 — 검토 M5).
  저장 결과는 상태줄에 "HH:MM:SS 저장 완료" 또는 "저장 거부: 사유". 순수 도우미 `balance_query.priority_rows_from_settings`(설정 →
  4행 글, bp 0은 "0") · `priority_rows_payload`(4행 → 명령 본문, bp 빈칸·오타는 0).
- "지금 적용 중" 표시는 두지 않는다(사용자 2026-11-17 — 저장 화면만).

## 4. 코어·게이트웨이
- 발주 공통 길(`LiveSystem.place`/`place_many`)에서 리스크·일일 한도 검사 **뒤**, 보내기 직전에 `_priority_intent`: 구간 안이면
  `OrderIntent.tif="Ioc"` + `priority_p`(§1 제외 규칙 적용). 바뀐 intent로 장부 등록·거부내역을 쓴다.
- 게이트웨이(`hl_live`): 묶음 키 = (post_only, tif, p) — 한 요청(action)은 같은 tif·같은 p(문서: p는 전부 IOC거나 전부 ALO일 때만,
  grouping은 요청 단위). p가 있으면 **SDK `bulk_orders(reqs, grouping={"p": p})`**(SDK가 grouping 인자를 받아 서명에 넣는다 —
  별도 wire 조립 없음). p 있는 한 건 발주(`place_order`)는 한 건짜리 묶음으로 보내고 결과를 단건 규약(oid 또는 예외)으로 바꾼다
  — **통신 오류·응답 없음은 `ConnectionError`(결과 모름)**, 거래소 거부만 `HLError`(검토 H1 — 자동M이 거부로 보고 재발주하지 않게,
  L11). GTC에 p가 붙어 오면 보내지 않고 거부 결과(코어 버그 방어). WS 전송(DESIGN-hl-ws-post)도 같은 action을 보낸다. 요청패킷
  로그에 grouping, 사람 읽는 주문 로그에 ` ioc p=…`, 지연 리포트용 "HL 발주 왕복" 줄도 남긴다.
- IOC 응답: 체결분은 `filled`(totalSz·avgPx)로 바로 오고 **남은 수량은 거래소가 즉시 취소**한다. 코어는 응답 체결분을 반영한 뒤
  **남은 수량을 장부에서 바로 닫는다**(`on_cancel`, 취소 통보가 안 와도 열린 주문으로 남지 않게 — 검토 M1). 뒤늦은 취소 통보는
  닫힌 주문이라 무해. 남은 수량의 뒤처리는 사용자(§6-2).
- IOC가 하나도 안 맞으면 거래소가 오류로 돌려준다(주문번호 없음) → 거부내역. 응답 유실 뒤 cloid 조회가 "canceled"를 미접수로 보는
  것(`_recover_by_cloid`)은 IOC 부분체결 뒤 취소와 겹칠 수 있다 — 그때는 결과 모름(ConnectionError)으로 올라가고 체결 통보는
  보관된다(검토 M2, 운영 로그로 확인 대상).
- 리스크 한도(DESIGN-settings §1a)·일일 한도는 그대로 건다.

## 5. 선행 실측 (운영 전, 사용자가 실행 — 실주문)
- `tools/hl_priority_fee_probe.py`: 메인넷에서 **IOC 매수 1건을 시장 아래 3%에** `grouping {"p": p}`로 보낸다(SDK 같은 호출). IOC라
  거래소가 바로 취소하고 체결이 없으니 수수료도 0. 확인: ① 서버가 dict grouping 서명을 받는지 ② 응답 모양 ③ 오류 문구(HYPE 잔고
  등). 키는 env `HL_AGENT_KEY`·`HL_ACCOUNT_ADDRESS`(코어는 keyring — 같은 값을 env로 넣는다).
- 이 실측이 통과하기 전엔 운영 배포하지 않는다. 부분체결 뒤 잔량 취소 통보 모양은 운영 첫날 로그로 확인(§4).

## 6. [OPEN] — 사용자 답 2026-11-17 / 검토 뒤 확인 대상
1. ~~bp 0~~ → **체크된 구간은 전부 IOC**(bp 0이면 p 없이 IOC만), 일반주문창만 제외. 확정.
2. ~~IOC 남은 수량~~ → **사용자가 알아서 처리**(코어는 체결차로 두고 지금 알람 규칙 그대로, 재발주 없음). 확정.
3. ~~HYPE 잔고가 모자랄 때 거래소 동작~~ → **실측 2026-11-18 09:38(개발 PC, 체결쏴 삼성 후주문 IOC p=1000)**: 요청 전체 거부
   `HL rejected: Insufficient delegatable balance for priority order`, 왕복 74~79ms(체결 엔진 전 잔고 검사), 주문 안 들어감.
   서명·`grouping {"p": n}` 형식은 받아들여졌다(사유가 잔고). 원인 = 스테이킹된 HYPE 없음(사용자 확인). **과금 잔고(위임 안
   한 스테이킹 HYPE)가 없으면 구간 안의 모든 p 주문이 거부돼 헤지가 빠진다** — 잔고를 만들기 전엔 bp 0(IOC만) 또는 구간 끔.
   거부 시 p 없이 재발주하는 안전장치는 두지 않았다(사용자 결정 전). 닫음.
   **11-18 09:58·10:09 추가 실측**: 스테이킹 계정에 위임 안 한 HYPE 0.2(이체 81초 뒤, 주문 계정과 같은 주소)로도 같은 거부
   (삼성·하이닉스 둘 다). 원인 = §2의 **최소 10 HYPE** 규칙(문서 11-13경 추가). 빌더 dex(xyz) 미지원은 아님 — 문서 "non-outcome
   asset이면 됨", SDK 예제도 빌더 dex 종목.
4. ~~"지금 적용" 표시~~ → 저장 화면만(사용자 "이해가 잘 안됨" — 필요해지면 그때). 확정.
5. ~~메이커 전용(ALO) 주문~~ → 사용자 2026-11-18 "HL선 전략은 제거, 앞으로 안 쓸거다": 전략이 내는 ALO 주문은 없어짐. §1의 ALO
   제외 규칙은 안전장치로만 남김. 닫음.
6. ~~NXT 개장매매 자동청산(익절)~~ → 사용자 2026-11-18 "NXT 개장매매 청산주문 … GTC로 한다": GTC 확정(ALO 절충안도 안 함). 닫음.
7. ~~동시호가 '정정' 버튼~~ → 사용자 2026-11-18 "동시호가 HL 주문은 GTC로 한다": 동시호가 HL 주문 전부(묶음·정정) `priority_exempt`
   로 제외. 닫음. 남은 적용 대상은 체결쏴 HL 후주문·NXT 개장매매 전략주문뿐.

## 7. 구현 순서 (각 단계 check.sh green 후 다음) — **2026-11-17 1~4 구현 완료, §5 실측 전**
1. ~~순수~~ `strategy_core.PriorityWindow`·`HlPriorityFee`·`apply_hl_priority_fee`·`priority_window_for`·`priority_p` + 테스트.
2. ~~코어~~ `GlobalSettings.hl_priority_fee`(저장·복원·asdict 스냅샷)·`settings_global` 키·`LiveSystem.set_hl_priority_fee`·
   발주 길 `_priority_intent`(place·place_many, 리스크·한도 검사 뒤, 제외 규칙 §1)·IOC 잔량 닫기 + `OrderIntent.tif`/`priority_p`/
   `priority_exempt` + 테스트.
3. ~~게이트웨이~~ `hl_live`: 묶음 키 (post_only, tif, p)·`bulk_orders(grouping={"p": p})`·tif Ioc·패킷 로그 grouping·한 건 발주에
   p가 있으면 한 건짜리 묶음(통신 오류는 ConnectionError)·GTC+p 방어 + 테스트(가짜 거래소가 grouping 기록). **§5 실측 스크립트
   `tools/hl_priority_fee_probe.py`(사용자 실행).**
4. ~~화면~~ 잔고 조회 창 `ttk.Notebook` 두 탭(`잔고` / `우선수수료`), 우선수수료 탭 4행 + 저장(읽은 뒤 활성) + 상태줄,
   `balance_query.priority_rows_from_settings`/`priority_rows_payload`(순수) + 테스트, 미리보기 `docs/preview/balance_query_fee_tab.png`.
5. 독립 검토(2026-11-17) 지적 중 남긴 것: 지연 리포트 정규식은 "HL 발주 왕복" 줄을 p 경로에도 남겨 맞춤; `_recover_by_cloid`의
   canceled 처리(M2)·부분체결 통보 모양은 운영 로그로; 주문리스트에 IOC 표시 없음(L1 일부); 시각 "24:00:00" 불가(23:59:59까지).
