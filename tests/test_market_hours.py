"""시장 시간표(config market_hours/special_days) — DESIGN-market-hours.md. 순수 로직 테스트."""
from datetime import date, datetime, time

import pytest

from kp_arb.config import AppConfig, MarketHours, SpecialDay
from kp_arb.market_hours import MarketCalendar, Window, parse_hm, parse_window

_SYMBOLS = {"samsung": {"stock": "005930", "hl": "xyz:SMSN"}}
# 평소 시간표는 필수 항목 — 코드에 기본 시각 없음(사용자 2026-11-10 "설정파일값으로")
_HOURS = {"krx": ["09:00", "15:30"], "nxt_pre": ["08:00", "08:50"],
          "nxt_main": ["09:00", "15:20"], "nxt_after": ["15:30", "20:00"],
          "stock_futures": ["08:45", "15:45"], "fx_futures": ["08:45", "15:45"],
          "fx_night": ["18:00", "06:00"]}
_HOURS_YAML = (
    "market_hours:\n" + "".join(f"  {k}: ['{o}', '{c}']\n" for k, (o, c) in _HOURS.items())
    + "fx_calc_start: '07:30'\n")


def test_parse_hm_and_window() -> None:
    assert parse_hm("09:00") == time(9, 0)
    assert parse_hm("15:44:55") == time(15, 44, 55)
    for bad in ("9", "25:00", "ab:cd", ""):
        with pytest.raises(ValueError):
            parse_hm(bad)
    assert parse_window(None) is None
    w = parse_window(("08:45", "15:45"))
    assert w == Window(time(8, 45), time(15, 45)) and not w.overnight
    assert w.contains(time(8, 45)) and w.contains(time(15, 44, 59))
    assert not w.contains(time(15, 45)) and not w.contains(time(8, 44))
    with pytest.raises(ValueError):
        parse_window(("08:45",))  # type: ignore[arg-type]


def test_overnight_window_crosses_midnight() -> None:
    # 야간 원달러선물 18:00~익일 06:00
    w = Window(time(18, 0), time(6, 0))
    assert w.overnight and w.text() == "18:00~06:00(익일)"
    assert w.contains(time(18, 0)) and w.contains(time(23, 59)) and w.contains(time(1, 0))
    assert not w.contains(time(6, 0)) and not w.contains(time(12, 0))


def test_calendar_from_config_replaces_old_hardcoded_values() -> None:
    cfg = AppConfig.model_validate(
        {"symbols": _SYMBOLS, "market_hours": _HOURS, "fx_calc_start": "07:30"})
    cal = MarketCalendar.from_config(cfg)
    day = date(2026, 11, 10)
    assert cal.special_name(day) is None
    assert cal.fx_calc_start == time(7, 30)                       # 옛 FX_CALC_START
    assert cal.close_time("stock_futures", day) == time(15, 45)    # 옛 롤오버 15:45
    assert cal.close_time("fx_futures", day) == time(15, 45)
    # 옛 예비 갱신 루프 8 <= hour < 16 자리 → 원달러선물 주간장 08:45~15:45
    assert cal.is_open("fx_futures", datetime(2026, 11, 10, 9, 0))
    assert not cal.is_open("fx_futures", datetime(2026, 11, 10, 16, 0))
    assert cal.is_open("fx_night", datetime(2026, 11, 11, 1, 0))
    assert "KRX 정규장 09:00~15:30" in cal.describe(day)


def test_special_day_overrides_only_listed_markets() -> None:
    cfg = AppConfig.model_validate({
        "symbols": _SYMBOLS,
        "market_hours": _HOURS,
        "fx_calc_start": "07:00",
        "special_days": {
            "2026-11-19": {"name": "수능일", "krx": ["10:00", "16:30"], "nxt_pre": None,
                           "stock_futures": ["09:45", "16:45"], "fx_night": ["19:00", "06:00"]},
            date(2027, 1, 4): {"krx": ["10:00", "15:30"]},  # YAML이 날짜로 읽어도(따옴표 없음)
        },
    })
    cal = MarketCalendar.from_config(cfg)
    assert cal.fx_calc_start == time(7, 0)
    csat = date(2026, 11, 19)
    assert cal.special_name(csat) == "수능일"
    h = cal.hours(csat)
    assert h["krx"] == Window(time(10, 0), time(16, 30))
    assert h["nxt_pre"] is None                                   # null = 그날 열지 않음
    assert h["fx_futures"] == Window(time(8, 45), time(15, 45))   # 안 적은 시장은 평소
    assert cal.close_time("stock_futures", csat) == time(16, 45)
    assert cal.is_open("fx_night", datetime(2026, 11, 19, 18, 30)) is False
    assert cal.is_open("fx_night", datetime(2026, 11, 19, 19, 0)) is True
    assert cal.special_name(date(2027, 1, 4)) == "2027-01-04"    # 이름 없으면 날짜
    assert cal.special_name(date(2026, 11, 18)) is None
    snap = cal.snapshot(csat)
    assert snap["special"] == "수능일" and snap["hours"]["nxt_pre"] is None
    assert snap["hours"]["krx"] == ["10:00", "16:30"] and "NXT 프리 없음" in snap["text"]


def test_bad_time_text_is_config_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from kp_arb.config import ConfigError, load_config

    p = tmp_path / "config.yaml"
    p.write_text(
        "symbols:\n  samsung:\n    stock: \"005930\"\n    hl: \"xyz:SMSN\"\n" + _HOURS_YAML
        + "special_days:\n  \"2026-11-19\":\n    krx: [\"25:00\", \"16:30\"]\n",
        encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(p))
    assert "krx" not in SpecialDay(name="x").model_fields_set


def test_market_hours_required_and_load_reads_real_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # 일곱 시장 중 하나라도 빠지면 ConfigError(코드 기본값으로 메우지 않는다)
    from kp_arb.config import ConfigError
    from kp_arb.market_hours import MarketCalendar as MC

    with pytest.raises(ValueError):
        MarketHours.model_validate({k: v for k, v in _HOURS.items() if k != "fx_night"})
    p = tmp_path / "config.yaml"
    p.write_text("symbols:\n  samsung: {stock: '005930', hl: 'xyz:SMSN'}\n"
                 "market_hours: {krx: ['09:00', '15:30']}\nfx_calc_start: '07:30'\n",
                 encoding="utf-8")
    with pytest.raises(ConfigError, match="market_hours"):
        MC.load(str(p))
    # 저장소 config.yaml은 그대로 읽힌다(값은 사용자 몫 — 여기서 특정 시각을 단정하지 않음)
    cal = MC.load()
    assert all(k in cal.hours(date(2026, 11, 10)) for k in _HOURS)
