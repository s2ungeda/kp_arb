# DESIGN-ls-xing.md — LS 접근을 OpenAPI(REST/WS)에서 xingAPI(COM)로 전부 교체

사용자 결정 2026-09-23: "LS는 전부 xing으로". 이유 = HL선(§7D)처럼 **LS를 테이커로 쓰려면 REST 발주가
너무 느림**(운영 실측 09-17: 발주 왕복 중앙 286ms·p90 1.4초, 시간 초과 하루 몇 건). xingAPI는 전용
소켓이라 빠르다고 알려져 있으나 **우리 실측값은 아직 없다** — 1단계에서 먼저 잰다.

## 0. 원칙
- 상위(OrderBook·자동M·세션·화면)는 **손대지 않는다.** `LSGateway` 계약(`gateways/base.py`)과 WS 콜백
  (`on_quote/on_trade/on_fill/on_order_event/on_market_status/on_fx_*/on_vi/on_expected/on_reconnect`)을
  xing 구현이 그대로 채운다. 이벤트 자료형(`Fill`·`TradeTick`·`OrderEvent`·`MarketStatus`·`Quote`)도 그대로.
- 외부 규칙 값(초당 한도)은 **xingAPI가 알려 주는 값**(`GetTRCountPerSec/BaseSec/Limit`)을 쓴다 — 코드에
  숫자를 박지 않는다(CLAUDE.md §7).
- 주문 요청(발주·정정·취소)은 **자동 재전송 금지** 그대로(응답 없음 = 결과 모름).
- 라이브 xing 호출은 테스트에서 금지 — COM은 가짜로, 브리지 프로토콜은 재생으로.
- 전환은 **설정 하나로**(`KP_LS_API=openapi|xing`, 기본 openapi) — 검증 전엔 언제든 되돌린다.
  **OpenAPI 전송 코드는 실운영을 xing으로 돌려 본 뒤 삭제**(사용자 결정 2026-09-28): 지울 것 = `ls_rest.py`·
  `ls_http.py`·`ls_auth.py`·`ls_ws_live.py`·`paper_check.py`·`ws_check.py`·시동 분기·스위치·REST 테스트.
  `ls.py`(본문·파싱)와 `ls_ws.py`(파서·콜백)는 xing이 상속하므로 남는다(참고는 git 이력).

## 1. 코어를 32비트로 — COM을 코어 안에 (확정 2026-09-23, 사용자 문답 "HL은 SDK 필수인가?")
- xingAPI는 32비트 COM(XA_Session/XA_DataSet)만 있다.
- 처음엔 HL SDK → `eth-account` → **`ckzg`**(이더리움 블롭 증명용 C 확장, HL 주문 서명에는 안 쓰임)가
  32비트 휠이 없어 코어를 64비트에 묶고 브리지 프로세스를 두려 했다. **실측(09-23):** `eth-account 0.10`
  (ckzg 의존 없음, SDK 허용 범위 `>=0.10`)으로 고정하면 32비트에서 SDK 서명이 되고 **같은 키·입력에 64비트와
  서명이 동일**, 전체 테스트 783건이 32비트 venv(`.venv32`, Python 3.12.10 x86)에서 통과.
- 따라서 **코어 전체를 32비트로** 빌드·실행하고, xingAPI COM 객체를 **코어 프로세스 안**에 둔다. 브리지·소켓·
  공유메모리 없음(사용자 우려 "소켓은 느리다" 해소). HL 쪽은 그대로.
- 대가: 32비트 프로세스 메모리 한도(2~4GB) — 코어 실측 사용량 수백 MB라 여유. 개발 venv도 `.venv32`로
  옮긴다(`check.sh`·`build_exe.bat` 경로). `pyproject`에 `eth-account<0.11` 고정.

## 2. 구성 (한 프로세스) — 정정 2026-09-23: 파일 분리 대신 **전송 단만 갈아 끼움**
```
meme-core.exe (32bit, asyncio 메인 스레드)
  gateways/xing_com.py  XingSession     — COM 전용 STA 스레드: XASession 로그인, XAQuery/XAReal 객체 보관, 메시지 펌프.
                                          asyncio와는 Future/큐로만 만난다. COM 객체 생성은 주입(가짜 COM으로 테스트)
  gateways/xing_res.py  Res 파일 파서   — TR별 InBlock/OutBlock 필드명·occurs (순수)
  gateways/xing.py      XingQueryClient — LSRestClient와 같은 `request(tr_cd, body, path=…) → RestResponse` 계약을
                                          XAQuery로 채움 (body {"…InBlock": {…}} → SetFieldData, OutBlock → dict)
                        XingGateway(LSApiGateway) — 계좌별 클라이언트만 XingQueryClient로. 본문 만들기·응답 파싱·
                                          주문 문맥·계좌 라우팅 **전부 상속 그대로**
  gateways/xing_ws.py   XingRealClient(LSWebSocketClient) — run()만 바꿔 XAReal에 advise하고, OnReceiveRealData를
                                          `{"header":{"tr_cd","tr_key"},"body":{OutBlock 필드}}` 프레임으로 만들어
                                          부모의 `_dispatch`에 넣는다. 파서·콜백·WsStatus·on_reconnect 그대로
```
- 이렇게 하면 기존 테스트(`test_ls_order/account/ws`)가 그대로 xing 경로의 본문·파서를 보증하고, xing 쪽 새 테스트는
  "전송 단"(Res 파싱, 필드 채우기, 프레임 만들기, COM 스레드 경계·타임아웃)만 다룬다.
- **한 로그인이 모든 계좌를 본다.** xing 로그인은 사용자 ID 단위 — 주식·선물·FX 계좌번호는 `GetAccountList`로
  받고, 요청 때 계좌번호·비밀번호(기존 `LS_*_ACCT_PW`)만 넣는다. 토큰 관리(`TokenManager`)는 사라진다.

### 2.1 COM 스레드 모델
- **STA 스레드 하나**(`XingSession`)가 `XA_Session.XASession`·`XA_DataSet.XAQuery`(조회용 1 + 주문용 1)·
  `XA_DataSet.XAReal`(TR별 1)을 `win32com.client.DispatchWithEvents`로 만들고 소유한다. COM 객체는 이 스레드
  밖에서 만지지 않는다.
- 펌프: `win32event.MsgWaitForMultipleObjects`(요청 큐 이벤트 + 윈도 메시지)로 **메시지가 오는 즉시 깨어**
  `pythoncom.PumpWaitingMessages()` — 잠자기 간격 없음(예제의 10ms sleep 방식은 안 씀).
- asyncio → COM: `loop.run_in_executor` 대신 요청 큐 + `asyncio.Future`(응답은 `loop.call_soon_threadsafe`로
  완료). COM → asyncio: 이벤트(`OnReceiveRealData`/`OnReceiveData`/`OnReceiveMessage`/`OnLogin`…)에서 필드를
  dict로 뽑아 `call_soon_threadsafe`로 넘긴다. **COM 스레드에서 네트워크·파일 I/O 금지**(화면 규칙과 같음).
- `tr` 요청은 XAQuery 객체 하나당 한 번에 하나(직렬). **주문 전용 XAQuery**를 따로 두어 조회 뒤에 줄 서지
  않게 한다. 시간 초과(조회 10초·주문 30초, 기존 값)면 주문 TR은 `RestTimeoutError`와 같은 뜻(결과 모름).
- 응답 코드: `OnReceiveMessage(bIsSystemError, nMessageCode, szMessage)` — 성공 판정은 기존과 같이 `00`으로
  시작. `rsp_cd/rsp_msg`로 옮겨 기존 파서·거부내역이 그대로 쓴다.
- 실시간(XAReal)은 TR마다 객체 하나, `AdviseRealData` 전에 `SetFieldData(InBlock, 키필드, 0, key)`. 키 필드명은
  **Res 파일 InBlock에서 읽는다**(추측 금지, [OPEN]).
- 초당 한도: 요청 직전 `GetTRCountPerSec/GetTRCountBaseSec/GetTRCountLimit/GetTRCountRequest`로 xing이 주는
  값으로 판단(기존 `RateLimiter` 표는 REST 전용으로 남김).

### 2.2 코어 쪽 연결
- `XingGateway(LSApiGateway)`: 공개 메서드는 상속. `from_session(session, accounts, …)`가 계좌별
  `XingQueryClient`를 만들어 부모 생성자에 넣는다. 다른 점은 전송(REST POST ↔ XAQuery)뿐. `RestResponse.body`에
  `rsp_cd/rsp_msg`(OnReceiveMessage)와 `{tr}OutBlockN`(occurs면 list, 아니면 dict — REST와 같은 모양)을 채운다.
  `RateLimitError`는 xing 카운터(`GetTRCountRequest ≥ GetTRCountLimit`)로 낸다.
- `XingRealClient(LSWebSocketClient)`: 구독 등록(`_subs`)·파서·콜백 상속. `run()`은 세션 로그인 대기 → `_subs`를
  XAReal advise → 세션 끊김을 기다렸다가 재로그인 뒤 재advise + `on_reconnect`(재동기). `WsStatus`는 부모 것.
  키 필드명은 Res InBlock의 첫 필드.
- `bootstrap_live`: `KP_LS_API=xing`이면 토큰·REST·WS 대신 `XingSession`을 띄우고 위 둘을 조립. 계좌별 WS 두 개
  대신 **XingRealClient 하나**(계좌통보 SC/O01/C01/H01·시세·JIF·CUR·FC9/DC0·VI_ 전부) — `stock_ws`·`deriv_ws`
  자리에 같은 객체를 넣어 `_wire`를 안 고친다.
- 시동 실패(xingAPI 미설치·로그인 실패)는 기존 `boot_errors` → 메인창 팝업.

## 3. 자격·설정
- keyring/env: `LS_XING_ID`, `LS_XING_PW`, `LS_XING_CERT_PW`(공인인증서 비밀번호) — 키 등록 창·keys.bat에
  추가. 브리지만 읽는다(코어·로그에 평문 금지).
- `KP_XING_PATH`(xingAPI 설치 폴더, Res 하위) — 기본값은 설치 확인 뒤 정함([OPEN]).
- `KP_XING_SERVER`: `real|demo` — 호스트·포트는 [OPEN](기존 eBEST `hts.ebestsec.co.kr:20001` /
  `demo.ebestsec.co.kr:20001`이 LS로 바뀌었을 것 — 문서·패키지로 확인).
- 계좌 매핑: `GetAccountList` 결과와 `LS_*_ACCT`(기존)를 대조해 없는 계좌면 시동 실패 사유로 팝업.

## 4. TR 대응표 (REST → xing) — 필드명 동일 전제, Res로 검증
| 용도 | TR | 비고 |
|---|---|---|
| 주식 발주/정정/취소 | CSPAT00601/00701/00801 | `MbrNo`(NXT)·`MgntrnCode`·`LoanDt` 포함 — Res에 있는지 확인 |
| 선물 발주/정정/취소, FX 발주 | CFOAT00100/00200/00300 | |
| 잔고·포지션·미체결 | CSPAQ22200, CFOBQ10500, CSPAQ12300, t0441, CSPAQ13700, t0434 | t0434 연속조회 `cts_ordno`는 `Request(True)` |
| 시세·마스터 | t1102, t8402, t2111, t1901, t8401, t8426 | 마스터는 xing `t8401/t8426` Res 블록 수 확인 |
| 실시간 시세 | H1_/UH1/NH1, S3_/US3/NS3, YS3/UYS/YJC, JH0/JC0, FC9/DC0, CUR, JIF, VI_ | 키 형식은 Res InBlock 기준 |
| 계좌 통보 | SC0~SC4, O01/C01/H01 | xing에선 로그인 계정 전체에 대해 온다(계좌별 구독 없음) |

## 5. 단계 (각 단계 검증 green + 완료 조건) — 진행 2026-09-23
코드는 0~4단계 전부 작성(가짜 COM·가짜 세션 테스트 green, 기존 LS 주문·계좌 테스트가 xing 경로 본문·
파싱 보증). 남은 것은 **라이브 실측** 순서: `python -m kp_arb.xing_check`(로그인·t1102 왕복·실시간 10초,
주문 없음) → `KP_LS_API=xing`으로 코어 시동해 잔고·포지션·미체결·마스터 대조 → 소량 주문.
0. **32비트 전환** — `.venv32`를 개발 venv로(`check.sh`), `pyproject` `eth-account<0.11`, `build_exe.bat`
   32비트 빌드. 완료 = check green + 32비트 배포판 zip. (HL 접속은 그대로이므로 운영 영향 없음.)
1. **COM 세션 + 로그인 + 왕복 실측** — `xing_com.py`(STA 스레드·펌프·요청 큐). 완료 = 개발/운영 PC에서 로그인
   성공, `t1102` 조회 왕복 ms 로그, 끊김→재로그인. 테스트: 가짜 COM 객체로 큐·타임아웃·직렬화·스레드 경계.
2. **조회·마스터** — `XingGateway`의 조회 계열 + 공용 본문/파서 분리(`ls_tr.py`). 완료 = `KP_LS_API=xing`으로
   코어 시동 시 잔고·포지션·미체결·마스터가 REST와 같은 값(운영 대조).
3. **실시간** — `XingRealClient` + 공용 파서(`ls_real.py`) + 계좌 통보. 완료 = 시세·체결 통보가 OrderBook에
   같은 모양으로 들어옴, 재접속 → 재동기.
4. **주문** — 발주·정정·취소·FX 발주, 시간 초과·거부 규칙. 완료 = 운영 소량 주문으로 접수·체결·취소 확인 +
   **발주 왕복 실측**(REST 286ms와 비교).
5. **운영** — 키 등록 창 항목(`LS_XING_*`), 메인 상태줄에 xing 로그인 상태(기존 WS 상태 자리). 그 뒤 HL선
   시험을 xing으로 재개.

## 6. 실측으로 확정된 것 (2026-09-23, C:\meme 설치·DevCenter Res 내려받기)
- 설치 폴더 `C:\meme`(사용자), Res = `C:\meme\Res`. COM 등록은 `reg.bat`(regsvr32 두 DLL) — 개발 PC는
  Claude가 등록, 운영 PC는 관리자 권한으로 `reg.bat`. 32비트 파이썬에서 `XA_Session.XASession` 생성·
  `IsLoadAPI=True` 확인.
- **주식 주문 TR 이름이 다르다:** xing Res는 `CSPAT00600/00700/00800`(REST는 …601/701/801). InBlock1
  필드는 REST와 같고 **`MbrNo`(NXT)·`MgntrnCode`·`LoanDt` 있음** → `XingQueryClient`가 요청·응답 블록
  접두를 바꿔 게이트웨이는 REST 이름 그대로.
- **선물 주문 TR(CFOAT00100/200/300) 비밀번호 필드는 `Pwd`**(REST는 `InptPwd`) → 필드 별칭표로 바꿈.
  Res에 없는 필드는 빼고 경고.
- 조회·마스터 TR(t1102/t8402/t2111/t8401/t8426/t0441/t0434/CSPAQ12300/13700/22200/CFOBQ10500)의
  InBlock·OutBlock 이름·필드는 REST와 같다(occurs 블록 = REST의 list와 일치).
- **실시간 TR 블록 이름은 `InBlock`/`OutBlock`**(TR 접두 없음). 키 필드: H1_/S3_/YS3/VI_ `shcode`(6),
  통합·NXT(UH1/NH1/US3/NS3/UYS) `ex_shcode`(10 — WS 키 "U005930   "과 같은 형식), 선물·원달러
  (JH0/JC0/YJC/FC9/DC0) `futcode`(8), CUR `base_id`(6 — "USD   "), JIF `jangubun`, 계좌 통보 키 없음.
  선물·원달러 OutBlock엔 `shcode`가 없고 `futcode`만 → 프레임 body에 `shcode`를 같이 넣는다.
- 계좌 통보(SC0~SC4, O01/C01/H01) OutBlock에 파서·동시호가 대응이 읽는 필드(ordno·execno·execqty·execprc·
  exectime·orgordno / fnoIsuno·bnstp·ordqty·ordprc·trcode1 / chevol·cheprice·chetime·yakseq·ordordno) 있음.
- 32비트 전환: `.venv32`(3.12.10 x86) 783 테스트 통과, 배포판 `meme-core.exe` 기계 종류 0x14C 확인.

## 6.1 1단계 실측 (2026-09-23 15:32, 개발 PC, 모의서버 `demo.ls-sec.co.kr:20001`, 주문 없음)
- 로그인 **0.50초**(계좌 4개). 모의는 공인인증서 비번 없이 로그인됨.
- t1102 조회 왕복: **첫 건 113ms, 이후 6~8ms**(REST 조회 수백 ms·주문 중앙 286ms와 비교).
- xing이 알려 준 t1102 한도: 초당 5·기준 1초·한도 0(=무제한)·현재 1 — 코드는 한도 0을 "없음"으로 본다.
- 실시간: UH1(통합 호가) 10초 50건 수신, 첫 수신은 등록 뒤 25ms. H1_/S3_는 KRX 마감(15:30) 뒤라 무데이터가
  정상, JIF는 상태 변화 때만. **CUR 미확인**(§7).
- 모의 서버 주소는 DevCenter가 남긴 `C:\meme\User\Login.ini`(SERVER_IP)에서 확인.

## 6.2 2·3단계 실측 (2026-09-23 15:4x, `KP_LS_API=xing python -m kp_arb.bootstrap 5`, 주문 없음)
- xing으로 코어 조립·시동 전부 통과: 마스터(t8401/t8426) → 잔고(주식 99,475,784 / 선물 495,489,052) →
  포지션 2건(삼성 주식 2주 @254,450 · 삼성 SF 4계약 @272,000) → 미체결 0 → 실시간 5초 254건 + HL 마크.
  같은 날 REST 코어 시동 로그(11:18)와 건수 일치(잔고 2계좌·포지션 2건·미체결 0). xing 경고(Res에 없는
  필드·등록 실패) 없음.
- **4단계 주문(16:05, 사용자 승인):** 모의 서버 장 종료라 CFOAT00100·CSPAT00600 모두 `01458 모의투자 장종료`
  거부 — 거부 경로는 끝까지 정상(요청→OnReceiveMessage→rsp_cd→RestError→거부 로그·거부내역, **선물 20ms /
  주식 61ms**). 본문은 REST와 동일(계좌·종목·MbrNo NXT·MgntrnCode·LoanDt). 접수→취소 왕복·통보(O01/H01·
  SC0/SC3)는 **장중 재실측**(스크립트: 스크래치 `xing_order_probe.py` — 체결 안 될 가격 1건 발주→취소).
- 시동 때 실시간 등록 60건 전부 접수(주식 3종 × KRX/통합/NXT 호가·체결·예상·VI, 선물 근·차근 6코드, JIF,
  계좌 통보 8종, FC9/DC0 2월물, CUR 2키).

## 6.3 4단계 실측 — 주문 (2026-09-28 09:02~09:07, 모의, 체결 안 될 가격 1건 발주→취소)
| | 발주 왕복 | 접수 통보 도착 | 취소 왕복 | 취소 통보 도착 |
|---|---|---|---|---|
| 주식선물 CFOAT00100/00300 (#1781·#2293) | 18~20ms | O01 56~77ms | 11~13ms | 48ms |
| 주식 CSPAT00600/00800 (#711·#851·#1159) | 33~44ms | SC0 52~118ms | 30~40ms | SC3 77~90ms |
- REST 실측(09-17 운영: 발주 중앙 286ms·p90 1.4초)과 비교해 10배 이상 빠르다. 왕복은 우리 요청→xing 응답,
  통보 도착은 요청 시각 기준 장부 반영까지.
- **xing 선물 취소 통보의 특성(실측):** 취소하면 O01(취소주문 번호, `orgordno`=원주문, `trcode1`=**FO03**) +
  H01(`ordordno` **빈 값**)이 온다. REST WS는 H01에 원주문이 있었기에 그걸 썼는데 xing에선 못 찾는다 →
  파서가 O01의 `trcode1`(FO01 신규·FO02 정정·FO03 취소)로 사건 종류를 정해 O01/FO03을 취소 확인으로
  반영한다(`ls_ws._O01_KIND_BY_TRCODE`). 첫 실측 때 이 때문에 취소가 장부에 3초 넘게 안 반영됐고(서버는
  취소됨 — t0434 미체결 0건), 수정 뒤 48ms.
- 모의 선물은 주문 가격범위가 좁다(01427 — 5% 아래 거부, 매수1호가 −10틱은 접수).
- CUR(현물환율) 실시간: 서울외환시장 개장 뒤 정상 수신(09:04, 20초 21건) — 키는 등록한 두 값 중 하나가
  맞음(둘 다 등록해 둠).

## 7. [OPEN] — 남은 것
- xingAPI 서버 호스트·포트(real/demo) — DevCenter 로그인 창 값(`KP_XING_HOST`, 기본 포트 20001).
- OpenAPI(REST/WS)와 xing을 **같이 로그인**해도 되는지(전환 기간에 둘 다 뜰 수 있음).
- 통보 TR의 `cheprice` 단위(REST WS는 원화의 1/100)가 xing에서도 같은지 — 첫 체결 로그로 확인.
- CUR 키: Res `base_id`는 6자 — WS의 8자 키는 advise 때 6자로 잘린다(같은 값). 실측 뒤 하나로.
- 선물 통보(O01)에 `ordordno` 없음(`orgordno`만) — 파서는 둘 다 보므로 무관, 기록만.
- 주문 TR을 조회와 다른 XAQuery로 분리했을 때 xing 쪽 제약이 있는지.
- `eth-account 0.10` 고정이 HL SDK의 다른 기능(정보 조회·WS)에 영향 없는지 — 운영 첫 시동에서 확인(서명은 동일
  실측).
