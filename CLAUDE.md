# CLAUDE.md — kp-arb 작업 규칙

이 파일은 Claude Code가 매 세션 읽는 **작업 규칙**이다. 코드를 짜기 전에 먼저 읽고 따른다.

## 1. 계약: DESIGN.md가 최우선
- `DESIGN.md`가 이 프로젝트의 **단일 진실(spec/계약)** 이다.
- 구현은 DESIGN.md를 따른다. **임의로 아키텍처를 바꾸지 않는다.**
- 설계를 바꿔야 한다고 판단되면, **코드보다 DESIGN.md를 먼저 고치고** 사람의 확인을 받는다.
- DESIGN.md의 `[OPEN]` 항목은 아직 미정이다. 그 부분을 추측으로 채우지 말고, 필요하면 질문한다.

## 2. 검증 루프 (가장 중요)
- 코드를 고치면 **반드시** `./.claude/hooks/check.sh` 를 돌린다 (ruff + mypy + pytest).
- **모두 green이 되기 전에는 작업을 "완료"로 선언하지 않는다.**
- 통과시키려고 **테스트를 약화시키거나 ruff/mypy 설정을 느슨하게 바꾸지 않는다.** 테스트가 틀렸다고 판단되면 그 근거를 적고 사람에게 확인받는다.
- 새 순수 로직 함수에는 **반드시 단위 테스트**를 추가한다.
- 게이트웨이는 **mock/리플레이로만 테스트**한다. 테스트에서 **라이브 LS/HL API를 절대 호출하지 않는다.**

## 3. 작업 범위
- 한 번에 다 만들지 말 것. 작은 단위로, **"완료" 조건을 명확히** 하고 진행한다.
  - 나쁜 예: "시스템 전체 구현"
  - 좋은 예: "LSGateway 토큰 매니저 구현, tests/test_ls_auth.py 전부 통과"
- 여러 파일을 건드리는 변경은 먼저 plan을 세우고 시작한다.

## 4. 아키텍처 맵 (DESIGN.md §4-5)
```
kp_arb/
  domain/enums.py     # Underlying, Venue, Account, Instrument, Side, OrderType, SessionPhase
  domain/models.py    # OrderIntent, Position, Quote, InstrumentStatus, MarketState (pydantic)
  routing.py          # instrument -> LS 계좌
  session.py          # 장운영 단계 -> instrument별 상태 맵
  spread.py           # 프리미엄 계산
  fx.py               # USD 노출 계산 (외부 #2로 보고할 값)
  gateways/base.py    # LSGateway / HLGateway 추상 계약  <- 여기를 구현
  gateways/mock_*.py  # 테스트용 목 (라이브 금지)
  strategy/base.py    # Strategy 인터페이스 (전략 미정)
  strategy/noop.py    # 플레이스홀더
```

## 5. 도메인 불변식 (깨면 안 됨)
- **계좌 라우팅:** 주식·ETF → `KR_STOCK` 계좌 / 주식선물·야간선물 → `KR_DERIV` 계좌. (`routing.account_for`)
- **공매도 금지:** 국내 주식(spot) 숏 주문을 만들지 않는다. 숏은 선물 매도 / 인버스 ETF로만.
- **FX는 외부 위임:** 이 시스템은 USD/KRW 선물 주문을 내지 않는다. `fx.usd_exposure`로 노출을 계산해 **외부 #2로 보고만** 한다.
- **세션 주도:** 거래 가능 여부는 `session`(장운영데이터 기반)으로 판단한다. 시간을 하드코딩하지 않는다.
- **데드존:** live 레퍼런스가 없으면 신규 진입 금지.
- **비밀값:** 키/시크릿은 환경변수로만. 코드·테스트·로그에 평문 금지.

## 6. 코딩 컨벤션
- Python 3.11+, asyncio, pydantic v2. **타입 힌트 필수**(mypy strict 통과).
- ruff(E,F,I,UP,B) 클린. import 정렬은 ruff(I)에 맡긴다.
- 외부 I/O는 게이트웨이 뒤로 격리. 순수 로직(routing/session/spread/fx)은 I/O 없이 유지.

## 7. 반복 실수는 여기에 기록
- Claude가 같은 실수를 반복하면, 그 교훈을 이 파일(또는 스킬)에 한 줄로 적어 다음 세션까지 남긴다.
- **설명에 영어 직역투·기술용어 남발 금지** (결선→연결, 시딩→초기값 넣기, 정합→실제 응답에 맞게 고치기, 폴링→반복 조회 등). 쉬운 우리말 우선, 꼭 필요한 용어는 첫 등장에 뜻을 한 줄 붙인다.
- **한글이 든 파일을 PowerShell 텍스트 치환으로 재작성 금지** — 인코딩(CP949 오독)으로 파일이 깨진다. 편집은 Edit 도구로만.
- **.bat 파일에 한글 금지(주석 포함)** — cmd가 CP949로 읽어 한글 줄이 명령으로 오인된다('李?...' 오류). 배치 파일은 영문 전용.
- **tkinter 화면 스레드에서 네트워크 호출 금지** — 창 끌기·메뉴까지 통째로 얼어 버벅인다(메인 화면에서 실증). 폴링은 뒷단 스레드가 하고, 화면은 저장된 결과만 after()로 읽는다.

## 8. 실행
```bash
pip install -e ".[dev]"      # 최초 1회
./.claude/hooks/check.sh     # 검증 루프
```
