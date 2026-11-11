"""토탈잔고 감시 — DESIGN-balance-watch.md(사용자 2026-11-11) 규칙 그대로."""
from __future__ import annotations

import asyncio

from kp_arb.balance_watch import (
    BalanceWatcher,
    balance_rows,
    balance_watch_loop,
    format_main_line,
    format_telegram,
    total_balance,
)
from kp_arb.domain.enums import Account, Instrument, Side, Underlying
from kp_arb.domain.models import Position

S, H, Y = Underlying.SAMSUNG, Underlying.SK_HYNIX, Underlying.HYUNDAI


def _pos(u: Underlying, inst: Instrument, qty: float, account: Account | None = None) -> Position:
    return Position(venue=inst.venue, instrument=inst, underlying=u,
                    side=Side.BUY if qty > 0 else Side.SELL, qty=abs(qty), avg_price=1.0,
                    account=account)


def test_total_abs_per_symbol_x10_futures_near_next_summed_fx_excluded() -> None:
    # §0: |주식| + |선물 근+차근|×10 + |HL|, 종목마다 절대값, 원달러선물·설정 밖 종목 제외
    positions = [_pos(S, Instrument.KR_STOCK, 200, Account.KR_STOCK),
                 _pos(S, Instrument.KR_STOCK_FUTURE, -15, Account.KR_DERIV),
                 _pos(S, Instrument.KR_STOCK_FUTURE_NEXT, -5, Account.KR_DERIV),
                 _pos(S, Instrument.HL_PERP, 50),
                 _pos(H, Instrument.KR_ETF, -30, Account.KR_STOCK),
                 _pos(H, Instrument.KR_STOCK_FUTURE, 2, Account.KR_DERIV),  # 역방향 롱
                 _pos(H, Instrument.KR_FX_FUTURE, 7, Account.KR_FX),         # 제외
                 _pos(Y, Instrument.HL_PERP, 999)]                            # 설정 밖
    rows = balance_rows(positions, [S, H])
    assert [(r.underlying, r.stock, r.sf, r.hl) for r in rows] == [
        (S, 200, -20, 50), (H, -30, 2, 0)]
    assert rows[0].total == 200 + 200 + 50 and rows[1].total == 30 + 20
    assert total_balance(rows) == 500
    # 선물 숏(삼성 −20)과 롱(하이닉스 +2)이 서로 지워지지 않는다
    assert total_balance(balance_rows(positions, [S, H])) > abs(-20 + 2) * 10


def test_watcher_examples_from_spec() -> None:
    # §1 예: B=150,S=150 — 150→300 보냄(B'=300), 300→450 보냄(B'=450), 450→300 보냄(B'=300)
    w = BalanceWatcher(150)
    assert w.check(200, 150) is None and w.baseline == 150
    c = w.check(300, 150)
    assert c is not None and (c.old_baseline, c.new_baseline, c.change) == (150, 300, 150)
    c = w.check(450, 150)
    assert c is not None and (c.old_baseline, c.new_baseline) == (300, 450)
    c = w.check(300, 150)
    assert c is not None and (c.old_baseline, c.new_baseline, c.change) == (450, 300, -150)
    # 300→380→290: 150 이하여야 보냄
    assert w.check(380, 150) is None and w.check(290, 150) is None and w.baseline == 300


def test_watcher_multi_step_jump_baseline_is_last_line_crossed() -> None:
    # §1: 150→480 한 번 보내고 B'=450(사용자 "기준값은 450"); 480→450 안 보냄, 300 이하면 보냄
    w = BalanceWatcher(150)
    c = w.check(480, 150)
    assert c is not None and (c.new_baseline, c.total, c.change) == (450, 480, 330)
    assert w.check(450, 150) is None and w.check(310, 150) is None
    c = w.check(300, 150)
    assert c is not None and (c.old_baseline, c.new_baseline) == (450, 300)
    # 아래로 여러 칸: 300→0 → 기준 0
    c = w.check(0, 150)
    assert c is not None and c.new_baseline == 0


def test_watcher_step_change_keeps_baseline_and_zero_step_disables() -> None:
    w = BalanceWatcher(150)
    assert w.check(1000, 0) is None and w.baseline == 150   # 꺼짐(배수 0) — 기준 유지
    assert w.check(1000, -5) is None and w.baseline == 150
    c = w.check(1000, 500)                                    # 배수 바꿔도 기준은 150에서
    assert c is not None and (c.old_baseline, c.new_baseline) == (150, 650)
    assert BalanceWatcher(None).check(999, 10) is None        # 시동 값 없음 → 판정 안 함


def test_format_telegram_and_main_line() -> None:
    rows = balance_rows([_pos(S, Instrument.KR_STOCK, 200, Account.KR_STOCK),
                         _pos(S, Instrument.KR_STOCK_FUTURE, -20, Account.KR_DERIV),
                         _pos(S, Instrument.HL_PERP, 50)], [S, H])
    w = BalanceWatcher(300)
    c = w.check(450, 150)
    assert c is not None
    assert format_telegram(c, rows, "2026-11-11 14:05:12") == (
        "토탈잔고 450 (+150)  2026-11-11 14:05:12\n"
        "기준 300 → 450, 배수 150, 다음 알림 600↑ / 300↓\n"
        "삼성 주식 +200 / 선물 -20×10 / HL +50")           # 전부 0인 하이닉스는 뺀다
    assert format_main_line(300, 500) == "기준 300  잔고 : 500"
    assert format_main_line(None, None) == "기준 -  잔고 : -"
    assert format_main_line(1500, 12345) == "기준 1,500  잔고 : 12,345"


def test_loop_sets_startup_baseline_then_notifies_on_crossing_only_when_enabled() -> None:
    positions = [_pos(S, Instrument.KR_STOCK, 150, Account.KR_STOCK)]
    enabled = {"on": False}
    sent: list[str] = []
    w = BalanceWatcher()

    def notify(text: str) -> bool:
        sent.append(text)
        return True

    async def run() -> None:
        task = asyncio.create_task(balance_watch_loop(
            lambda: positions, [S], lambda: (enabled["on"], 150.0), w, notify,
            interval_s=0.01, clock=lambda: "T"))
        await asyncio.sleep(0.05)
        assert (w.baseline, w.total) == (150, 150)           # 첫 틱 = 시동 값
        positions.append(_pos(S, Instrument.HL_PERP, 200))   # 350 — 꺼져 있으니 안 보냄
        await asyncio.sleep(0.05)
        assert w.baseline == 150 and sent == []
        enabled["on"] = True                                 # 켜면 150→350: 기준 300
        await asyncio.sleep(0.05)
        assert w.baseline == 300 and len(sent) == 1
        assert sent[0].startswith("토탈잔고 350 (+200)  T\n기준 150 → 300")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
