# DESIGN-market-hours.md — 시장 시간표(config.yaml)와 특별 개장일

사용자 지시 2026-11-10: "하드코딩 시각 → config 파일 시각으로 변경", "평소 시간표에 야간선물 시장도 포함",
"사용자 입력 시간 변경은 사용자가 직접", "설정파일값으로 — 설정값은 사용자가 입력할 것". 배경: 수능일(2026-11-19)
개장 1시간 연기 점검(docs/ops/특별개장일-체크리스트.md).

## 0. 원칙
- **거래 가능 판정은 여전히 LS 장운영정보(JIF) 기반**(DESIGN §세션 주도). 이 시간표는 그것을 대신하지 않는다.
- 코드 안에 박혀 있던 시각 상수를 `config.yaml`로 옮기고, 특별일(개장 연기 등)엔 날짜별로 덮어쓴다.
- **시각의 출처는 config.yaml 하나.** 코드에는 기본 시각(폴백)을 두지 않는다 — `market_hours` 일곱 시장과
  `fx_calc_start`는 필수 항목이고 빠지면 시동 때 `ConfigError`. 값 자체는 사용자가 적는다(Claude가 확인을 구할
  대상이 아님).
- **사용자가 화면에 넣은 시각은 코드가 건드리지 않는다**(체결쏴 구간·동시호가 창·동시호가 주문시각·외환선물
  우선시간·NXT 개장 시각·자동종료 시각). 특별일엔 사용자가 직접 바꾸고 다음 날 되돌린다. 코어는 알려 주기만 한다(§4).

## 1. config.yaml
```yaml
market_hours:                 # 평소 시간표 "HH:MM" [개장, 마감]. 마감 ≤ 개장이면 익일 마감(야간)
  krx:           ["09:00", "15:30"]   # KRX 정규장
  nxt_pre:       ["08:00", "08:50"]   # NXT 프리마켓
  nxt_main:      ["09:00", "15:20"]   # NXT 메인마켓
  nxt_after:     ["15:30", "20:00"]   # NXT 애프터마켓
  stock_futures: ["08:45", "15:45"]   # 주식선물(주식·금리 파생)
  fx_futures:    ["08:45", "15:45"]   # 원달러선물 주간(통화상품)
  fx_night:      ["18:00", "06:00"]   # 야간 원달러선물(익일 06:00)
fx_calc_start: "07:30"        # 환율 계산 시작(exec 결정 47) — 이 전엔 판정·정산용 환율 없음
special_days:                 # 바뀌는 날만. 적은 시장만 덮고, null = 그날 열지 않음
  "2026-11-19":
    name: 수능일
    krx:           ["10:00", "16:30"]
    nxt_pre:       null
    nxt_main:      ["10:00", "16:20"]
    nxt_after:     ["17:00", "20:00"]   # [OPEN] NXT 공지로 확인(16:40 가능성)
    stock_futures: ["09:45", "16:45"]
    fx_futures:    ["09:45", "15:45"]   # 마감 그대로(KRX 2026-11-05 발표)
    fx_night:      ["19:00", "06:00"]
```
- 형식이 틀리거나 `market_hours`·`fx_calc_start`가 빠지면 시동 때 `ConfigError`(다른 config 오류와 같음).
- 값은 사용자가 적는다. 위 예시는 2026-11-10 시점에 Claude가 KRX·NXT 운영시간과 수능일 기사로 채운 초기값이다
  (체결쏴 구간 기본값 15:40은 사용자 입력칸 초기값이라 이 표와 무관).

## 2. 순수 모듈 `kp_arb/market_hours.py`
- `Window(open, close)` — `contains(t)`: 마감 ≤ 개장이면 자정 넘김(야간)으로 본다.
- `MarketCalendar.from_config(AppConfig)` / `MarketCalendar.load(path)`(config.yaml을 직접 읽음 — 단독 도구·
  LiveSystem에 달력을 안 넘겼을 때). 코드 기본값 생성자(`default()`)는 없다.
  - `hours(day) -> {시장: Window|None}` — 특별일이면 적힌 시장만 덮고 null은 None(그날 없음).
  - `special_name(day)`, `window(market, day)`, `is_open(market, moment)`, `close_time(market, day)`,
    `snapshot(day)`(/state용 dict), `describe(day)`(사람용 한 줄).
- I/O 없음, 단위 테스트.

## 3. 코드가 이 시간표를 읽는 자리 (옛 하드코딩 → config)
| 자리 | 옛 값 | 지금 |
|---|---|---|
| `bootstrap` 환율 계산 시작 | `FX_CALC_START = time(7, 30)` | `calendar.fx_calc_start`(config `fx_calc_start`) |
| `bootstrap` 원달러선물 예비 갱신 루프 | `8 <= hour < 16` | `calendar.is_open("fx_futures", now)` |
| `theory.is_rolled` 월물 롤오버 | 최종거래일 15:45 고정 | `close_time` 필수 인자(기본값 없음) — 주식선물은 `stock_futures` 마감, 원달러선물은 `fx_futures` 마감(시동 날 기준). 그날 그 시장이 null이면 날짜가 지나야 롤. 원달러선물 최종거래일 거래종료가 평소와 다르면 그날을 `special_days`에 적는다(사용자) |
| `strategy_core.OPERATING_WINDOWS` | 화면 운영시간 기본값 | **미사용 코드**(호출처 없음, 테스트만) — 그대로 둠. [OPEN] 삭제 여부 |
- 사용자 입력 초기값(체결쏴 구간·동시호가 주문시각·NXT 개장 08:00 등)은 "코드 상수"가 아니라 입력칸 초기값이라 옮기지 않는다.

## 4. 특별일 알림 (코어 → 메인)
- `/state`에 `market_day` {date, special(이름|null), hours{시장: [개장, 마감]|null}, text}.
- 메인은 시동 뒤 처음 코어가 붙을 때 `special`이 있으면 팝업 한 번(부모 가운데): "오늘은 수능일입니다. KRX 10:00~16:30 ·
  주식선물 09:45~16:45 · … / 체결쏴 구간·동시호가 주문시각·외환선물 우선시간·NXT 개장매매는 자동으로 바뀌지 않습니다 —
  직접 확인하세요." + 메인 로그. 코어 로그에도 시동 때 한 줄.

## 5. 구현 순서
1. config 모델 + `market_hours.py` + 테스트.
2. 하드코딩 3곳 교체(§3) + 기존 테스트 유지.
3. `/state market_day` + 메인 팝업.
