# DESIGN-ui.md — 화면 공통 스타일 계약 (tkinter 클라이언트)

이 문서는 모든 창(order_hl, order_list, monitor, fx_monitor, main_window …)이 따르는 **UI 일관성 규칙**이다. 기준은 **개발 완료된 일반주문창(order_hl)** 이며, 그 값을 `kp_arb/ui_theme.py`에 토큰으로 굳혔다.

## 0. 원칙
- 화면은 **raw 폰트 튜플·색 문자열을 직접 쓰지 않는다.** `ui_theme`의 **역할 토큰만** 참조한다.
- 크기·색을 바꾸려면 **`ui_theme.py` 한 곳만** 고친다(전 화면 일괄 반영).
- 새 창은 **처음부터** `ui_theme`를 쓴다. 기존 창은 점진 이전.

## 1. 폰트 (family = `Malgun Gothic`)

| 토큰 | 값 | 용도 |
|---|---|---|
| `FONT_BASE` | 11 | 창 기본(라벨·일반 텍스트) — `apply_base`가 `*Font`로 지정 |
| `FONT_NUM` | 11 **bold** | **숫자·입력값**(수량·단가·잔고·가격) — *숫자는 볼드* 규칙 |
| `FONT_GRID` | 10 **bold** | 표(Treeview: 호가창·주문목록), 행높이 `ROWHEIGHT=22` |
| `FONT_LABEL` | 9 | 표 제목·상태바·콤보 |
| `FONT_SMALL` | 8 | 보조 캡션(Cross·Unified 등) |
| `FONT_STRONG` | 14 **bold** | 강조 버튼(주문 버튼) |

## 2. 색 (의미별)

| 토큰 | 값 | 의미 |
|---|---|---|
| `C_BUY` | `#c00000` | 매수·이익(빨강) |
| `C_SELL` | `#0000c0` | 매도·손실(파랑) |
| `C_ZERO` | `black` | 0·중립 |
| `C_BUY_ACTIVE` / `C_SELL_ACTIVE` | `#a00000` / `#000090` | 매수/매도 버튼 눌림 |
| `C_HILITE_BG` | `#ffe680` | 강조 바탕(잔고·PNL) |
| `C_QUERY_LBL` | `gray45` | 조회(REST) 항목 라벨 — 회색 구분 |
| `C_ERR` | `#8b0000` | 거부·실패(상태바) |
| `C_MUTED` | `gray30` | 보조·안내(상태바 일반) |
| `C_BORDER` | `gray50` | 셀 테두리 |

`sign_color(v)`: 양수→`C_BUY` / 음수→`C_SELL` / 0·비수치→`C_ZERO`. (잔고·PNL 등 부호 색)

## 3. 규칙 요약
- **숫자·입력값은 `FONT_NUM`(볼드).** 라벨은 `FONT_BASE`/`FONT_LABEL`.
- **매수=빨강, 매도=파랑**(주문 버튼·부호 색 일관). 이익/손실도 같은 색 규약(`sign_color`).
- **조회(REST) 값 라벨은 회색**(`C_QUERY_LBL`)으로 즉시계산 값과 구분.
- 창 생성 직후 **`T.apply_base(root)`** 한 줄로 기본 폰트·표 스타일 적용.

## 4. 셀 단위 색이 필요한 표
- **`ttk.Treeview`는 행 단위 색(tags)만** 되고 셀 단위는 안 된다. 셀마다 색이 필요하면
  표를 **`tk.Label` 그리드**(스크롤 캔버스)로 짠다 — 각 Label이 자기 fg/bg를 가짐.
- 대신 Treeview가 공짜로 주던 **행 선택·스크롤·가상화**를 직접 구현해야 하고, 행이 많으면
  **위젯 재사용(행 풀)** 으로 갱신 비용을 낮춘다. (예: `order_list` — 매매 칸만 색)
- 작은 고정 표(잔고칸 ~10행)는 Label 그리드가 자연스럽고, 매우 긴 목록은 Treeview가 유리.

## 5. 적용 상태 (추적)

- ✅ 토큰 정의: `kp_arb/ui_theme.py` (+ `tests/test_ui_theme.py`)
- ✅ `order_list` — ui_theme 적용 + Label 그리드 표(매매 칸 매수=빨강/매도=파랑)
- ⬜ order_hl 이전(값 동일 → 무변화) · monitor · fx_monitor · main_window
- ⬜ [OPEN] 숫자 표시 포맷(`_fmt`/`_fmt_qty`/`_fmt_px`) 공용화 — 현재 창별 중복
