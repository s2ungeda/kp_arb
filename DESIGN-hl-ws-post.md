# DESIGN-hl-ws-post.md — HL 주문을 웹소켓으로 보내기(WS post)

사용자 지시 2026-11-11: "우선 WS로 주문 구현해봐. 신경 써야 할 것은 끊기지 않게 핑/퐁, 접속 끊겼을 때 재접속, 로그."
배경: 도쿄 리전 실측(11-11)으로 HL 요청→블록 ~500ms는 서버 몫으로 확정. 남는 클라이언트 몫은 **식은 HTTP 연결을
새로 맺는 ~300ms**(7분 유휴 뒤 첫 요청 821ms 실측)이고, 개장 첫 주문이 정확히 그 경우다. WS는 핑으로 늘 살아 있어
이 비용이 없다. 평상시(연결이 살아 있을 때) 이득은 수십 ms 안쪽으로 본다 — 운영 로그로 확인한다(§5).

근거 문서: HL 공식 API → WebSocket → Post requests(2026-11-11 확인). HTTP로 보내던 info·signed action을 WS로 보낼 수
있고, 응답은 HTTP와 같은 본문. 지연 차이·건수 한도는 문서에 없음. explorer 요청은 WS 불가.

## 0. 원칙
- **서명·nonce·본문은 그대로.** SDK가 만든 `{action, nonce, signature, vaultAddress, expiresAfter}`를 통로만 바꿔
  보낸다. 발주·정정·취소·레버리지 등 `Exchange._post_action`을 지나는 액션 전부가 대상.
- **WS가 못 보내면 HTTP로.** 미접속·재접속 중이면 그 요청은 바로 HTTP(옛 길)로 간다. 전송 뒤 응답이 없는 것은
  HTTP 시간 초과와 같은 "결과 모름"이고 재전송하지 않는다(CLAUDE.md §7 발주 재전송 금지, 발주는 cloid 유예 조회).
- **별도 연결.** 시세·체결 구독(hl_ws)과 다른 WS 연결을 하나 더 둔다 — 구독 스트림의 재연결·스냅샷 처리와 섞이지
  않고, 발주 응답 대조(id)만 한다.
- 끄는 스위치: config.yaml `hl_ws_post: true|false`(기본 true). false면 옛 HTTP만.

## 1. 프로토콜(공식)
- 보냄: `{"method":"post","id":N,"request":{"type":"action","payload":{HTTP /exchange 본문}}}` — id는 요청마다 고유.
- 받음: `{"channel":"post","data":{"id":N,"response":{"type":"action","payload":{"status":"ok","response":{…}}}}}`.
  오류: `response.type == "error"`, payload는 HTTP 상태·설명을 담은 **문자열**.
- 유지: 20초마다 `{"method":"ping"}` → 서버 `{"channel":"pong"}`(hl_ws와 같은 간격, 60초 무통신이면 서버가 끊음).

## 2. 모듈 `kp_arb/gateways/hl_ws_post.py` — `HLWsPostClient`
- 주입형 `WSConnector`(ls_ws Protocol 재사용, 테스트는 가짜 연결). `run()` = 연결 → 읽기 루프 → 끊기면 재연결.
  - 연결되면 `ready`. 끊기면(예외·정상 종료 모두) **대기 중인 요청 전부를 "연결 끊김"으로 실패**시키고 2초 뒤 다시
    연결(횟수 무제한 — 시세 WS와 같음). 핑 태스크는 연결마다 새로.
  - `WsStatus(venue="HL", name="HL 주문 WS", kind="주문", expects_stream=False)` — 메인 창 WS 현황표에 한 줄 더.
- `post_action(payload, timeout_s) -> dict`: id 발급 → Future 등록 → 전송 → 응답 대기.
  - 미접속: `HLWsPostUnavailable` 즉시(게이트웨이가 HTTP로).
  - 응답 `type == "error"`: `HLWsPostError(문자열)`(HTTP 4xx/5xx 예외와 같은 자리 — 게이트웨이는 "결과 모름" 취급).
  - 시간 초과(HL_SDK_TIMEOUT_S 10초, HTTP와 같음): `TimeoutError` → "결과 모름".
  - 응답 순서가 바뀌어도 id로 맞춘다. 모르는 id·pong·그 밖의 채널은 무시(로그 debug).
- 스레드: SDK 호출은 `asyncio.to_thread` 안이라 `_post_action` 대체 함수는 **다른 스레드**에서 불린다 →
  `asyncio.run_coroutine_threadsafe(post_action(...), loop).result()`. loop는 `bind_loop()`로 코어 루프를 받아 둔다.

## 3. 게이트웨이 연결 — `hl_live.install_ws_post(exchange, client)`
- SDK `Exchange._post_action(action, signature, nonce)`를 바꿔 끼운다(nonce 발급기 `install_nonce_clock`과 같은 수법).
  본문은 SDK 원본과 똑같이 만든다(vaultAddress는 이체류 액션이면 None, expiresAfter 포함).
- 흐름: WS `post_action` → `HLWsPostUnavailable`이면 **원래 `_post_action`(HTTP)** 호출. 그 밖의 예외는 그대로 올린다.
- SDK에 `_post_action`이 없으면(버전 변경) 설치하지 않고 경고 로그, HTTP 유지.

## 4. 로그(사용자 요구) — logger `kp_arb.hl_post`
- 연결: "HL 주문 WS 연결(n회째)" / 끊김: "HL 주문 WS 끊김 — 대기 요청 k건 실패 처리, 2초 뒤 재접속" / 재접속 실패는
  경고 한 줄씩(백오프 2초).
- 요청마다 INFO 한 줄: "HL WS post #id order 왕복 523 ms"(액션 type·왕복·결과 status). 오류·시간 초과는 WARNING.
- HTTP로 돌아간 요청: "HL 주문 WS 미접속 → HTTP로 전송(order)" WARNING(요청마다 — 운영 중 얼마나 자주 WS가 비는지
  보려고 줄이지 않는다).
- 기존 hl_live의 "HL 묶음 발주 왕복"·"HL 취소 왕복" 로그는 그대로 — 둘을 나란히 보면 WS/HTTP 왕복 비교가 된다.

## 5. 확인 방법·[OPEN]
- 실연결 확인 2026-11-11 11:14(개발 PC, 존재하지 않는 주문번호 취소만): HTTP 1,107ms(식은 첫 요청)·753ms → WS 687·685·681ms.
  평상시 왕복도 ~70ms 짧고, 식은 연결 비용(~350ms)은 WS엔 없다. 운영 로그로 개장 첫 주문을 다시 본다.
- 도쿄 실측 2026-11-11 13:25(`tools/hl_ws_order_probe.py`, 3바퀴·유휴 7분): 연결이 살아 있으면 HTTP와 WS 왕복이 같다(둘 다 690~790ms, 요청→블록 ~500~600ms). 식은 HTTP는 첫 요청 1,090ms·7분 유휴 뒤 2,036ms, WS는 747·752ms —
  WS의 이득은 **식은 연결 비용 제거**이고 평상시 이득은 없다고 본다. 도쿄 WS도 한국 WS보다 빠르지 않다(리전 무관).
- 운영 로그에서 같은 날 "HL WS post … 왕복"과 옛 HTTP 왕복(11-10 낮 ~800ms, 7분 유휴 뒤 첫 요청 821ms)을 견준다.
  특히 **개장 첫 주문**의 왕복이 줄었는지.
- [OPEN] WS post의 건수 한도가 HTTP와 같은지 문서에 없음 — 운영에서 429류 오류 문자열이 보이면 사용자에게 보고.
- [OPEN] HL 서버의 주기적(~3시간) 정상 종료 때 발주가 겹치면 그 요청은 "결과 모름"으로 처리된다(HTTP와 같음). 빈도는
  로그로 본다.

## 6. 테스트(가짜 연결, 라이브 없음)
- 응답 id 대조(순서 역전 포함), error 타입 → 예외, 미접속 → Unavailable, 끊김 → 대기 요청 실패 + 재접속 + 핑 재시작,
  시간 초과, `install_ws_post` 본문 모양·HTTP 폴백.
