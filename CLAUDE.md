# CLAUDE.md — kp-arb 작업 규칙

이 파일은 Claude Code가 매 세션 읽는 **작업 규칙**이다. 코드를 짜기 전에 먼저 읽고 따른다.

## 1. 계약: DESIGN.md가 최우선
- `DESIGN.md`(및 그 안에서 링크한 `DESIGN-*.md`)가 이 프로젝트의 **단일 진실(spec/계약)** 이다. DESIGN.md는 제목·요약·링크만, 상세는 분할 파일에 둔다(예: 수동 주문 = `DESIGN-manual-order.md`). 작업 전 관련 분할 파일도 읽는다.
- 구현은 DESIGN.md를 따른다. **임의로 아키텍처를 바꾸지 않는다.**
- 설계를 바꿔야 한다고 판단되면, **코드보다 DESIGN.md를 먼저 고치고** 사람의 확인을 받는다.
- DESIGN.md의 `[OPEN]` 항목은 아직 미정이다. 그 부분을 추측으로 채우지 말고, 필요하면 질문한다.
- **매 세션 함께 확인할 파일:**
  - `DESIGN.md`(+ 링크된 `DESIGN-*.md`) — 계약/spec
  - `korean-terms.md`(자동 메모리: `~/.claude/projects/c--project-VibeCode-kp-arb/memory/korean-terms.md`) — **직역 금지 용어 관리대장.** 설명·문서·주석을 쓰기 전에 이 대조표를 확인하고 따른다. 직역 회피는 기계적 치환이 아니라 문맥에 맞는 말로(§7 참고).

## 2. 검증 루프 (가장 중요)
- 작업을 끝내기 전에 **반드시** `./.claude/hooks/check.sh` 를 돌린다 (ruff + mypy + pytest). 편집 직후엔 훅이 고친 .py만
  ruff로 보고, 끝낼 때 Stop 훅이 전체를 한 번 더 확인한다(마지막 green 뒤 코드가 바뀐 경우만, 2026-11-12). 훅이 막은 뒤
  이어진 턴에서는 다시 막지 않으므로 고친 뒤 수동 check.sh green은 그대로 필요하다.
- **모두 green이 되기 전에는 작업을 "완료"로 선언하지 않는다.**
- 통과시키려고 **테스트를 약화시키거나 ruff/mypy 설정을 느슨하게 바꾸지 않는다.** 테스트가 틀렸다고 판단되면 그 근거를 적고 사람에게 확인받는다.
- 새 순수 로직 함수에는 **반드시 단위 테스트**를 추가한다.
- 게이트웨이는 **mock/리플레이로만 테스트**한다. 테스트에서 **라이브 LS/HL API를 절대 호출하지 않는다.**

## 3. 작업 범위
- 한 번에 다 만들지 말 것. 작은 단위로, **"완료" 조건을 명확히** 하고 진행한다.
  - 나쁜 예: "시스템 전체 구현"
  - 좋은 예: "LSGateway 토큰 매니저 구현, tests/test_ls_auth.py 전부 통과"
- 여러 파일을 건드리는 변경은 먼저 plan을 세우고 시작한다.

## 4. 아키텍처 맵 (2026-11-12, kp_arb/ 실제 파일 기준)
- **프로세스 둘.** 메인(`main_window.py`, 배포판 meme.exe)은 관리 화면이고, 코어(`core_server.py`, meme-core.exe)가
  LS·HL 접속·판정·발주를 혼자 맡는다. 메인이 코어를 띄우고 HTTP(127.0.0.1:8787 `/state`·명령)로 보며, 읽은 상태를
  공유메모리(`state_share`)에 써서 다른 화면들이 읽는다. 화면마다 별도 프로세스, 전부 코어의 얇은 클라이언트. `app.py`가
  exe 인자로 무엇을 띄울지 가른다.
- **32비트 venv(.venv32)가 기준.** LS 접속이 xingAPI COM(32비트 DLL)이라 코어가 32비트여야 하고, 배포판도 32비트로
  얼리므로 검증(check.sh)도 같은 비트로 돈다.
```
kp_arb/
  domain/enums.py, models.py          # Underlying·Account·Instrument… / Quote·Position·OrderIntent(pydantic)
  config.py, market_hours.py          # config.yaml·keyring 비밀 / 시장 시간표(시간 하드코딩 금지)
  routing.py, session.py, ticks.py    # 계좌 라우팅 / 장운영→상태 / 호가단위
  theory.py, disparity.py, spread.py  # 이론가·괴리·스프레드(순수)
  gateways/
    base.py, mock_ls.py, mock_hl.py   # LSGateway·HLGateway 계약(라이브·목이 전부 상속) / 테스트용 목(라이브 금지)
    xing_com.py, xing.py, xing_ws.py  # LS = xingAPI COM 한 스레드(세션·TR·실시간) — ls.py/ls_ws.py의 전송 단
    ls.py, ls_ws.py, ls_tr.py         # LS 주문·조회 게이트웨이 / 실시간 파서(UH1·JH0…) / TR 공통
    hl_live.py, hl_ws.py, hl_ws_post.py # HL SDK 주문·조회 / 실시간 WS / 주문 WS 전송
  bootstrap.py (LiveSystem)           # 코어 시동 절차·시세/포지션 저장소·장부 재동기
  core_server.py, core_engine.py      # 코어 HTTP API·엔진 생성·주기 태스크 / 리허설 판정
  order_book.py, strategy_core.py     # 주문·포지션 장부(순수) / 설정·검증·한도(순수)
  auto_m.py + auto_m_engine.py        # 체결쏴(자동M): 순수 상태변화 + 코어 결합(주식선물·주식·HL선)
  auction_order.py + _engine.py       # 동시호가 주문: 순수 + 엔진
  nxt_open.py + _engine.py, _screen.py # NXT 개장 매매(11-04 원복, 코드만 남음)
  balance_watch.py, pair_check.py, alert.py, telegram_rx.py # 잔고 감시 / 쌍 대조 / 텔레그램 송·수신
  fx.py, fx_service.py, signallink.py, fx_auction*.py # USD 노출 계산(§5)·외부 #2 보고 / 원달러선물 동시호가 대응주문
  main_window.py, settings_window.py, order_autom*.py, order_hl.py, order_list.py, monitor.py,
    auction_order_screen.py           # 화면(tk) — 전부 코어 클라이언트
  core_client.py, ui_*.py, win_state.py # 화면 공통: 코어 호출 / 팝업(부모 중앙)·스타일·상태줄 / 창 상태 저장
  quote_plan.py, report_*.py, logs.py # LS 구독 계획 / 지연·Sprd 리포트 / 파일 로그
  engine.py, runner.py, risk.py, strategy/, peg_order.py+pegging.py # 6월 골격·옛 도구(ArbEngine·전략 플러그인·호가추적 창) — 운영 판정 아님
tools/                                # 측정·분석 스크립트. 실주문(hl_*_order_probe)·실접속(probe_nxt_open·probe_auction·
                                      # hl_region_probe)은 Claude가 실행하지 않는다(deny) — 나머지는 로그 분석·공개 조회
```

## 5. 도메인 불변식 (깨면 안 됨)
- **계좌 라우팅:** 주식·ETF → `KR_STOCK` 계좌 / 주식선물·야간선물 → `KR_DERIV` 계좌. (`routing.account_for`)
- **공매도 기본 금지(자동T 대주 예외):** 국내 주식(spot) 숏은 기본적으로 만들지 않는다(숏은 선물 매도 / 인버스 ETF). **단 자동T 역방향은 대차 공매도(대주)를 허용** — 설정창 주문유형 옵션으로만(2026-08-24, DESIGN §3 / DESIGN-auto-t.md).
- **FX 노출 헤지는 외부 위임:** 주 노출은 `fx.usd_exposure`로 계산해 **외부 #2로 보고만** 한다(주문 안 냄). **단, 별도 용도의 원달러선물 헤지 계좌 `Account.KR_FX`(DESIGN §9.1)는 예외** — 이 계좌로는 헤지 주문을 낼 수 있다(동시호가 대응주문으로 구현·운영 발주 확인 2026-09-09, 계약 = `DESIGN-fx-auction.md`; 이 경로는 OrderBook 밖).
- **세션 주도:** 거래 가능 여부는 `session`(장운영데이터 기반)으로 판단한다. 시간을 하드코딩하지 않는다.
- **데드존:** live 레퍼런스가 없으면 신규 진입 금지.
- **비밀값:** 키/시크릿은 환경변수로만. 코드·테스트·로그에 평문 금지.

## 6. 코딩 컨벤션
- Python 3.11+, asyncio, pydantic v2. **타입 힌트 필수**(mypy strict 통과).
- ruff(E,F,I,UP,B) 클린. import 정렬은 ruff(I)에 맡긴다.
- 외부 I/O는 게이트웨이 뒤로 격리. 순수 로직(routing/session/spread/fx)은 I/O 없이 유지.

## 7. 반복 실수 — 규칙 (경위는 docs/lessons.md)
- 규칙만 여기에 둔다. 각 항목의 사건 경위(날짜·무슨 일·사용자 지적)는 `docs/lessons.md`의 같은 번호(L1…)에 있다. 같은 실수를
  반복하면 규칙은 여기 한 줄, 경위는 lessons.md에 적는다.
- **L1 설명에 영어 직역투·기술용어 남발 금지** (결선→연결, 시딩→초기값 넣기, 정합→실제 응답에 맞게 고치기, 폴링→반복 조회 등). 쉬운 우리말 우선, 꼭 필요한 용어는 첫 등장에 뜻을 한 줄 붙인다.
- **L2 한글이 든 파일을 PowerShell 텍스트 치환으로 재작성 금지** — 인코딩(CP949 오독)으로 파일이 깨진다. 편집은 Edit 도구로만.
- **L3 [.bat 작업 때만] .bat 파일에 한글 금지(주석 포함)** — cmd가 CP949로 읽어 한글 줄이 명령으로 오인된다('李?...' 오류). 배치 파일은 영문 전용.
- **L4 [.bat 작업 때만] .bat는 CRLF 줄바꿈 + 순수 ASCII로 저장** — Write 도구는 LF로 쓰므로, .bat 편집 후엔 PowerShell `Set-Content -Encoding ascii`(CRLF)로 재작성한다. LF나 비ASCII(예 em대시 '—')가 섞이면 cmd가 오파싱한다('m'은(는) 명령이 아닙니다 류).
- **L5 [화면(tk)] tkinter 화면 스레드(화면이 부르는 함수·after() 콜백 포함)에서 네트워크 호출 금지** — 창 끌기·메뉴까지 통째로 얼어 버벅인다. 폴링은 뒷단 스레드가 하고, 화면은 저장된 결과만 after()로 읽는다. (→ lessons L5)
- **L6 스펙이 원본(목업·이미지·델파이·엑셀·외부문서)을 가리키면 작업 전 그 원본을 먼저 연다. 화면(UI)은 목업을 반드시 열고, 바꾼 뒤엔 띄워서 목업과 대조한다** — "글이 우선"류 규칙은 그림을 본 뒤 충돌을 판정하는 규칙이지 안 봐도 된다는 뜻이 아니고(안 보면 따질 대상조차 없다), 화면은 단위 테스트로 못 잡는다. (→ lessons L6)
- **L7 UI·스펙 작업은 시작할 때 "근거"를 한 줄로 먼저 밝힌다** — 예: `근거: DESIGN §1 + order_hl_layout.png(열어봄) / 안 연 것: 델파이 원본`. 건너뛴 원본이 글로 보여야 사용자가 앞단에서 잡는다(침묵한 가정이 실수의 뿌리).
- **L8 빌드한 배포판 exe(meme.exe·meme-core.exe)를 이 PC에서 "동작 확인"으로 실행 금지. 빌드 확인은 파일 목록·zip 내용으로만** — `--help`/`--version` 같은 인자도 인식하지 않고 그냥 메인·코어가 떠 키링의 실키로 LS·HL에 접속한다. (→ lessons L8; settings.json deny로도 막음 2026-11-12)
- **L9 빈 결과·임시값을 돌려주는 코드("TR 확인 후 구현" 류)는 DESIGN의 `[OPEN]`에 올리고, 그 기능이 실사용에 들어가는 시점에 사용자에게 다시 말한다** — 주석·문서 한 줄에 묻어 두면 잊힌다. (→ lessons L9)
- **L10 외부 시스템의 한도·규칙 값은 공식 문서 출처를 적지 못하면 코드에 넣지 않는다. 넣어야 한다면 사용자에게 먼저 말하고, 막는 가드가 아니라 경고로 둔다** — DESIGN.md에 적혀 있어도 출처가 아니다(근거 없이 적힌 "LS 일 5,000회" 가드가 운영 중 정상 거래까지 막았다). (→ lessons L10)
- **L11 주문을 만들거나 바꾸는 요청(발주·정정·취소)은 전송 실패·시간 초과여도 자동 재전송 금지. 재시도·시간 초과는 반드시 로그에 남긴다** — 첫 요청이 이미 접수됐을 수 있다. 응답 없음은 거부가 아니라 "결과 모름"이다(조회용 재시도를 발주에 공통으로 쓰지 말 것). (→ lessons L11)
- **L12 외부 시스템이 매기는 번호는 채번 범위까지 키에 넣는다** — LS 주문번호는 계좌마다 따로 매겨져 주식·선물이 겹친다. 번호만으로 찾으면 다른 계좌의 통보가 섞인다(실증 2026-10-14: 주식 HTS 체결 8주가 같은 번호의 선물 선주문에 들어가 HL 80계약 초과 헤지). 주문은 (계좌, 번호)로 찾고 종목은 대조한다(exec 결정 73). **키를 넓힐 때는 그 번호를 쓰는 모든 자리를 전수로 찾아 바꾼다** — 10-14에 장부 키만 바꾸고 엔진의 등록 전 체결 보관함·발주 뒤 보관 통보 되돌리기는 번호만 남겨 "고쳤다"고 함 → 10-19 같은 사고 재발(주식 체결이 선물 세트에 들어가 HL 200계약 초과 헤지, 결정 84·85). 사용자: "멀티 계좌일 때 주문 찾는 법은 기본". (→ lessons L12)
- **L13 [화면(tk)] 알림·확인창은 항상 부모 창 가운데** — `tk.messagebox`는 모니터 가운데에 뜨므로 쓰지 않고 `ui_dialog.show_message`/`ask_yes_no`(center_on_parent)를 쓴다. (→ lessons L13)
- **L14 시세·데이터 구독을 뺄 때는 쓰는 곳을 시간대(주간·야간·설정 2구간)까지 전수 확인하고, 그 데이터를 읽는 변수(`trades`·`quotes` 키)까지 전수 grep하고, 뺄 목록은 사용자에게 보여 준 뒤 뺀다** — "쓰는 곳 없음"은 한 시간대·한 변수만 보고 내린 판단이기 쉽다(알려진 예: 야간 원달러선물 DC0는 결정 47의 우선시간 2구간이 쓴다). (→ lessons L14)
- **L15 같은 종목의 실시간 TR 한 벌(호가·체결·예상체결·VI)을 통합/다른 거래소로 옮길 때는 전부 같이 옮기고, "검토"로 남긴 항목은 그 기능이 운영에 들어가기 전에 사용자에게 다시 말한다. 누락은 구조 탓처럼 말하지 말고 누락이라고 말한다** — 하나만 옛 거래소 키에 남으면 그 항목이 빠진 채 운영된다(VI_는 KRX 전용, 통합은 UVI — 결정 89). (→ lessons L15)
- **L16 전략 이해·판단이 확실하지 않으면 추측으로 코드를 쓰지 말고 질문한다** — 미확정·모호한 요구를 임의로 메우지 말고 멈춰 묻는다(`[OPEN]`뿐 아니라 전략 로직·의도 전반). 잘못된 추측 위에 쌓은 코드가 가장 비싸다 — 지우고 다시 하는 값 + 사용자 신뢰.

## 8. 실행
```bash
pip install -e ".[dev]"      # 최초 1회
./.claude/hooks/check.sh     # 검증 루프
```
