"""오전 NXT 개장 매매 — 순수 로직(I/O 없음). 계약: DESIGN-nxt-open.md.

08:00 NXT 첫 체결 뒤 0.5~5초 창 안에서 `(환 × HL − S) / S` 괴리로 진입(HL 매도)·청산(HL 매수) 단을
판정하고, 조건이 맞는 단을 한꺼번에 **딱 한 번** 낸다. 수량 단위는 HL 1:1(주식 1주 = HL 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .domain.enums import Side, Underlying
from .hl_price import hl_round_price

TIERS = 3                      # 진입·청산 각 3단(엑셀 A90~A96)
DEFAULT_START_S = 0.5          # 창 시작 = 첫 체결 + 0.5초(엑셀 C87)
DEFAULT_END_S = 5.0            # 창 끝 = 첫 체결 + 5초(엑셀 F87)
DEFAULT_OPEN_HMS = "08:00:00"  # NXT 프리마켓 개장(첫 체결을 받아들이는 기준 시각, 화면 입력)
OPEN_GRACE_S = 10.0            # 개장 시각 뒤 이 안에 온 첫 체결만 T0로 — 코어를 늦게 켠 날의
                               # 첫 수신(예 08:41)을 개장으로 오인해 창을 여는 일을 막는다
SOURCE = "NXT개장"             # OrderIntent.source(주문 리스트 출처·쌍 대조 로그 구분)


@dataclass
class Tier:
    """한 단 — 조건 스프레드(진입: 이상, 청산: 이하)·주문 수량·주문가 역산용 스프레드(소수)."""

    spread: float | None = None
    qty: int = 0
    price_spread: float | None = None

    def valid(self) -> bool:
        return self.spread is not None and self.qty > 0 and self.price_spread is not None


def _tiers() -> list[Tier]:
    return [Tier() for _ in range(TIERS)]


@dataclass
class NxtOpenSettings:
    """종목 하나의 설정(core_state.json 저장). 실행(run_entry·run_exit)은 복원하지 않는다
    (자동M과 같은 원칙). HL 호가단위(틱 머지)는 여기 없다 — 일반주문창·체결쏴와 같은 코어 공용
    상태(hl_merge)."""

    start_s: float = DEFAULT_START_S
    end_s: float = DEFAULT_END_S
    open_hms: str = DEFAULT_OPEN_HMS
    # 모니터 수치용 기준수량 — 이 수량만큼 HL 호가창을 쓸어담은 est 가격(체결쏴 모니터와 같음)
    ref_qty: int = 1
    entries: list[Tier] = field(default_factory=_tiers)
    exits: list[Tier] = field(default_factory=_tiers)
    run_entry: bool = False   # 진입(HL매도) 실행 — 화면 표의 병합 버튼(사용자 2026-10-20 목업)
    run_exit: bool = False    # 청산(HL매수) 실행


@dataclass
class NxtOpenScreen:
    """전 종목 설정 — CoreState.nxt_open."""

    books: dict[str, NxtOpenSettings] = field(
        default_factory=lambda: {u.value: NxtOpenSettings() for u in Underlying})

    def of(self, u: Underlying) -> NxtOpenSettings:
        return self.books.setdefault(u.value, NxtOpenSettings())


@dataclass(frozen=True)
class Plan:
    """판정 결과 한 건 — 어느 단을 어느 방향으로 몇 개, 역산용 스프레드."""

    block: str        # "entry" | "exit"
    index: int        # 단 번호(0~2)
    side: Side        # entry → SELL, exit → BUY
    qty: int
    price_spread: float


# --- 계산 ---

def disparity(fx: float | None, hl_px: float | None, s: float | None) -> float | None:
    """괴리 = (환 × HL − S) / S. 하나라도 없거나 S가 0 이하면 None."""
    if fx is None or hl_px is None or s is None or s <= 0:
        return None
    return (fx * hl_px - s) / s


def level_price(levels: list[tuple[float, float]] | None, n: int = 1) -> float | None:
    """호가창(최우선부터)에서 n호가(기본 1 = 선택한 호가단위를 적용한 1호가) 가격. 없으면 None."""
    if not levels or n < 1 or n > len(levels):
        return None
    return float(levels[n - 1][0])


def order_price(s: float, price_spread: float, fx: float, side: Side,
                sz_decimals: int | None) -> float | None:
    """주문가 = 스프레드 t를 식에 넣어 푼 HL 가격 `S × (1 + t) / 환`을 HL 격자에 맞춘 값.
    매도(진입)는 바로 **위** 호가(올림), 매수(청산)는 바로 **아래** 호가(내림) — 엑셀 K94·K95.
    (hl_round_price의 maker=True가 정확히 그 방향: 매도 올림 / 매수 내림.)"""
    if s <= 0 or fx <= 0:
        return None
    raw = s * (1.0 + price_spread) / fx
    if raw <= 0:
        return None
    return hl_round_price(raw, side, sz_decimals, maker=True)


# --- 창 ---

def window_state(now: float, t0: float | None, start_s: float, end_s: float) -> str:
    """'none'(첫 체결 전) / 'before'(T0 뒤 start 전) / 'open' / 'closed'."""
    if t0 is None:
        return "none"
    dt = now - t0
    if dt < start_s:
        return "before"
    if dt <= end_s:
        return "open"
    return "closed"


def accept_t0(first_wall: float, open_hms: str, grace_s: float = OPEN_GRACE_S) -> bool:
    """그날 첫 NXT 체결의 PC 시각이 개장 시각 ~ +grace 안이면 T0로 받아들인다. 밖이면(코어를
    늦게 켠 날 등) 오늘 창은 없다 — 뒤늦은 첫 수신을 개장으로 오인하지 않기 위한 안전장치."""
    import time as _t

    lt = _t.localtime(first_wall)
    h, m, s = (int(x) for x in open_hms.split(":"))
    open_sec = h * 3600 + m * 60 + s
    now_sec = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (first_wall % 1.0)
    return open_sec <= now_sec <= open_sec + grace_s


# --- 판정 ---

def judge(entry_disp: float | None, exit_disp: float | None,
          settings: NxtOpenSettings) -> list[Plan]:
    """조건이 맞는 단을 전부(동시주문). 진입이 하나라도 맞으면 진입만, 아니면 청산.
    (둘 다 맞는 일은 식 구조상 없지만, 있어도 한 방향만 낸다.) 실행이 꺼진 쪽은 보지 않는다."""
    plans: list[Plan] = []
    if entry_disp is not None and settings.run_entry:
        for i, t in enumerate(settings.entries):
            if t.valid() and t.spread is not None and entry_disp >= t.spread:
                plans.append(Plan("entry", i, Side.SELL, t.qty, float(t.price_spread or 0.0)))
    if plans:
        return plans
    if exit_disp is not None and settings.run_exit:
        for i, t in enumerate(settings.exits):
            if t.valid() and t.spread is not None and exit_disp <= t.spread:
                plans.append(Plan("exit", i, Side.BUY, t.qty, float(t.price_spread or 0.0)))
    return plans


# --- 저장·복원(core_state.json) ---

def _tier_from(raw: object) -> Tier:
    if not isinstance(raw, dict):
        return Tier()
    out = Tier()
    for key in ("spread", "price_spread"):
        v = raw.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool):
            setattr(out, key, float(v))
    q = raw.get("qty")
    if isinstance(q, int) and not isinstance(q, bool) and q >= 0:
        out.qty = q
    return out


def settings_from_dict(raw: object) -> NxtOpenSettings:
    """dict → 설정. 값 오류는 그 항목만 기본값. 실행(run_entry·run_exit)은 복원하지 않는다."""
    st = NxtOpenSettings()
    if not isinstance(raw, dict):
        return st
    for key in ("start_s", "end_s"):
        v = raw.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool) and v >= 0:
            setattr(st, key, float(v))
    if st.end_s < st.start_s:
        st.start_s, st.end_s = DEFAULT_START_S, DEFAULT_END_S
    rq = raw.get("ref_qty")
    if isinstance(rq, int) and not isinstance(rq, bool) and rq >= 0:
        st.ref_qty = rq
    hms = raw.get("open_hms")
    if isinstance(hms, str) and len(hms) == 8 and hms[2] == ":" and hms[5] == ":":
        st.open_hms = hms
    for key in ("entries", "exits"):
        rows = raw.get(key)
        if isinstance(rows, list):
            tiers = [_tier_from(r) for r in rows[:TIERS]]
            while len(tiers) < TIERS:
                tiers.append(Tier())
            setattr(st, key, tiers)
    return st


def screen_from_dict(screen: NxtOpenScreen, raw: object) -> None:
    if not isinstance(raw, dict):
        return
    books = raw.get("books")
    if not isinstance(books, dict):
        return
    valid = {u.value for u in Underlying}
    for key, val in books.items():
        if key in valid:
            screen.books[str(key)] = settings_from_dict(val)


def apply_settings(st: NxtOpenSettings, body: dict[str, Any]) -> list[str]:
    """화면 명령(nxt_open_settings) → 설정 반영. 틀린 값은 사유 목록으로 거부(아무 것도 안 바꿈)."""
    errors: list[str] = []
    new = settings_from_dict({**_as_dict(st), **body})
    if "start_s" in body or "end_s" in body:
        start, end = body.get("start_s", st.start_s), body.get("end_s", st.end_s)
        if not (isinstance(start, int | float) and isinstance(end, int | float)
                and 0 <= start <= end):
            errors.append("매매시간은 0 ≤ 시작 ≤ 끝")
    if "open_hms" in body and new.open_hms != body["open_hms"]:
        errors.append("개장 시각은 HH:MM:SS")
    if errors:
        return errors
    st.start_s, st.end_s = new.start_s, new.end_s
    st.open_hms, st.entries, st.exits = new.open_hms, new.entries, new.exits
    st.ref_qty = new.ref_qty
    return []


def _as_dict(st: NxtOpenSettings) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(st)
