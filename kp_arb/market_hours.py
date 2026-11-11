"""시장 시간표 — config.yaml `market_hours`(평소) + `special_days`(특별 개장일 덮어쓰기).

계약: DESIGN-market-hours.md(사용자 2026-11-10 "하드코딩 시각 → config"). 순수 로직, I/O 없음.
거래 가능 판정은 여전히 LS 장운영정보(JIF)이고, 여기는 코드에 박혀 있던 시각 상수를 대신한다.
사용자가 화면에 넣은 시각은 건드리지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

from .config import MARKET_KEYS

if TYPE_CHECKING:
    from .config import AppConfig

MARKET_NAMES: dict[str, str] = {
    "krx": "KRX 정규장", "nxt_pre": "NXT 프리", "nxt_main": "NXT 메인", "nxt_after": "NXT 애프터",
    "stock_futures": "주식선물", "fx_futures": "원달러선물", "fx_night": "야간 원달러선물",
}


def parse_hm(text: str) -> time:
    """"HH:MM" 또는 "HH:MM:SS" → time. 형식이 틀리면 ValueError."""
    parts = str(text).strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"시각 형식 오류: {text!r} (HH:MM)")
    nums = [int(p) for p in parts] + [0]
    return time(nums[0], nums[1], nums[2])


@dataclass(frozen=True)
class Window:
    """개장~마감. 마감 ≤ 개장이면 자정을 넘기는 장(야간)."""

    open: time
    close: time

    @property
    def overnight(self) -> bool:
        return self.close <= self.open

    def contains(self, t: time) -> bool:
        if not self.overnight:
            return self.open <= t < self.close
        return t >= self.open or t < self.close

    def text(self) -> str:
        return (f"{self.open:%H:%M}~{self.close:%H:%M}"
                + ("(익일)" if self.overnight else ""))


def parse_window(pair: tuple[str, str] | None) -> Window | None:
    """[개장, 마감] → Window. None은 "열지 않음"."""
    if pair is None:
        return None
    if len(pair) != 2:
        raise ValueError(f"시장 시간은 [개장, 마감] 두 값: {pair!r}")
    return Window(parse_hm(pair[0]), parse_hm(pair[1]))


Hours = dict[str, Window | None]


class MarketCalendar:
    """평소 시간표 + 날짜별 덮어쓰기. `hours(day)`가 그날 시장별 창을 준다."""

    def __init__(self, normal: Hours, special: dict[date, tuple[str, Hours]],
                 fx_calc_start: time) -> None:
        self._normal = dict(normal)
        self._special = dict(special)
        self.fx_calc_start = fx_calc_start  # 환율 계산 시작(exec 결정 47) — 시장과 무관한 값

    @classmethod
    def load(cls, path: str | None = None) -> MarketCalendar:
        """config.yaml을 읽어 달력을 만든다(코어 시동·단독 도구·LiveSystem 기본). 코드 안에 기본
        시각은 없다 — 사용자 "설정파일값으로"(2026-11-10). 파일·형식 오류는 ConfigError."""
        from .config import DEFAULT_CONFIG_PATH, load_config

        return cls.from_config(load_config(path or DEFAULT_CONFIG_PATH))

    @classmethod
    def from_config(cls, cfg: AppConfig) -> MarketCalendar:
        """config.yaml → 달력. 형식 오류는 ValueError(load_config가 ConfigError로 바꾼다)."""
        normal: Hours = {k: parse_window(getattr(cfg.market_hours, k)) for k in MARKET_KEYS}
        special: dict[date, tuple[str, Hours]] = {}
        for key, sd in cfg.special_days.items():
            day = key if isinstance(key, date) else date.fromisoformat(str(key).strip())
            # 적은 시장만 덮는다(model_fields_set) — null은 "그날 열지 않음"
            over: Hours = {k: parse_window(getattr(sd, k))
                           for k in MARKET_KEYS if k in sd.model_fields_set}
            special[day] = (sd.name or day.isoformat(), over)
        return cls(normal, special, parse_hm(cfg.fx_calc_start))

    def special_name(self, day: date) -> str | None:
        found = self._special.get(day)
        return found[0] if found else None

    def hours(self, day: date) -> Hours:
        base = dict(self._normal)
        found = self._special.get(day)
        if found:
            base.update(found[1])
        return base

    def window(self, market: str, day: date) -> Window | None:
        return self.hours(day).get(market)

    def is_open(self, market: str, moment: datetime) -> bool:
        w = self.window(market, moment.date())
        return w is not None and w.contains(moment.time())

    def close_time(self, market: str, day: date) -> time | None:
        w = self.window(market, day)
        return w.close if w else None

    def describe(self, day: date) -> str:
        return " · ".join(f"{MARKET_NAMES.get(k, k)} {w.text() if w else '없음'}"
                          for k, w in self.hours(day).items())

    def snapshot(self, day: date) -> dict[str, Any]:
        """/state `market_day` — 메인 팝업·로그용."""
        return {
            "date": day.isoformat(), "special": self.special_name(day),
            "hours": {k: ([f"{w.open:%H:%M}", f"{w.close:%H:%M}"] if w else None)
                      for k, w in self.hours(day).items()},
            "text": self.describe(day),
        }
