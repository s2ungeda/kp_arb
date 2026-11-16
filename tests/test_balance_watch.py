"""잔고 감시 — DESIGN-balance-watch.md(2026-11-11, 개정 2026-11-16: HL 합 판정·새 글 형식)."""
from __future__ import annotations

import asyncio

from kp_arb.balance_watch import (
    BalanceWatcher,
    balance_rows,
    balance_watch_loop,
    format_report,
    hl_total,
    total_balance,
)
from kp_arb.domain.enums import Account, Instrument, Side, Underlying
from kp_arb.domain.models import Position

S, H, Y = Underlying.SAMSUNG, Underlying.SK_HYNIX, Underlying.HYUNDAI


def _pos(u: Underlying, inst: Instrument, qty: float, account: Account | None = None) -> Position:
    return Position(venue=inst.venue, instrument=inst, underlying=u,
                    side=Side.BUY if qty > 0 else Side.SELL, qty=abs(qty), avg_price=1.0,
                    account=account)


def test_rows_near_next_summed_fx_excluded_and_hl_total_is_signed_sum_abs() -> None:
    # §0: 종목별 주식·선물(근+차근)·HL 부호 잔고, 원달러선물·설정 밖 종목 제외.
    # 판정값(2026-11-16) = |Σ HL| — 부호대로 더한 뒤 절대값(삼성 +50, 하이닉스 −30 → 20)
    positions = [_pos(S, Instrument.KR_STOCK, 200, Account.KR_STOCK),
                 _pos(S, Instrument.KR_STOCK_FUTURE, -15, Account.KR_DERIV),
                 _pos(S, Instrument.KR_STOCK_FUTURE_NEXT, -5, Account.KR_DERIV),
                 _pos(S, Instrument.HL_PERP, 50),
                 _pos(H, Instrument.KR_ETF, -30, Account.KR_STOCK),
                 _pos(H, Instrument.KR_STOCK_FUTURE, 2, Account.KR_DERIV),  # 역방향 롱
                 _pos(H, Instrument.HL_PERP, -30),
                 _pos(H, Instrument.KR_FX_FUTURE, 7, Account.KR_FX),         # 제외
                 _pos(Y, Instrument.HL_PERP, 999)]                            # 설정 밖
    rows = balance_rows(positions, [S, H])
    assert [(r.underlying, r.stock, r.sf, r.hl) for r in rows] == [
        (S, 200, -20, 50), (H, -30, 2, -30)]
    assert hl_total(rows) == 20
    assert total_balance(rows) == (200 + 200 + 50) + (30 + 20 + 30)  # 옛 토탈잔고 정의는 남김


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


def test_format_report_follows_user_example() -> None:
    # 사용자 2026-11-16 예(배수 100): 종목 이름 줄 + 잔고 줄, 잔고는 음수만 부호. 변화량(증감)은
    # 같은 날 "없어도 된다"로 뺌 — 증가·감소 때도 그 시점 잔고만.
    first = balance_rows([_pos(H, Instrument.KR_STOCK, 200, Account.KR_STOCK),
                          _pos(H, Instrument.KR_STOCK_FUTURE, 20, Account.KR_DERIV),
                          _pos(H, Instrument.HL_PERP, -400)], [H, S])
    assert format_report(first) == "SK하이닉스\nLS 주식(S) 200 LS선물(SF) 20*10 HP -400"
    down = balance_rows([_pos(H, Instrument.KR_STOCK, 160, Account.KR_STOCK),
                         _pos(H, Instrument.KR_STOCK_FUTURE, -24, Account.KR_DERIV),
                         _pos(H, Instrument.HL_PERP, -400)], [H, S])
    assert format_report(down) == "SK하이닉스\nLS 주식(S) 160 LS선물(SF) -24*10 HP -400"
    # 전부 0인 종목은 뺀다
    assert format_report(balance_rows([], [H, S])) == ""
    # 두 종목이면 종목마다 두 줄
    both = balance_rows([_pos(H, Instrument.HL_PERP, -100), _pos(S, Instrument.HL_PERP, 70)],
                        [H, S])
    assert format_report(both) == (
        "SK하이닉스\nLS 주식(S) 0 LS선물(SF) 0*10 HP -100\n"
        "삼성전자\nLS 주식(S) 0 LS선물(SF) 0*10 HP 70")


def test_loop_sends_startup_report_then_on_hl_sum_crossing_only_when_enabled() -> None:
    positions = [_pos(S, Instrument.KR_STOCK, 150, Account.KR_STOCK),
                 _pos(S, Instrument.HL_PERP, -150)]
    enabled = {"on": False}
    sent: list[str] = []
    w = BalanceWatcher()

    def notify(text: str) -> bool:
        sent.append(text)
        return True

    async def run() -> None:
        task = asyncio.create_task(balance_watch_loop(
            lambda: positions, [S], lambda: (enabled["on"], 100.0), w, notify,
            interval_s=0.01))
        await asyncio.sleep(0.05)
        assert (w.baseline, w.total) == (150, 150) and sent == []   # 꺼진 채 시동 → 시동 글 없음
        positions.append(_pos(S, Instrument.HL_PERP, -200))          # |HL| 350 — 꺼져서 안 보냄
        await asyncio.sleep(0.05)
        assert w.baseline == 150 and sent == []
        enabled["on"] = True                                         # 켜면 150→350: 기준 350
        await asyncio.sleep(0.05)
        assert w.baseline == 350 and len(sent) == 1
        assert sent[0] == "삼성전자\nLS 주식(S) 150 LS선물(SF) 0*10 HP -350"
        positions.append(_pos(S, Instrument.KR_STOCK, 50, Account.KR_STOCK))  # 주식만 → 안 보냄
        await asyncio.sleep(0.05)
        assert len(sent) == 1
        positions.append(_pos(S, Instrument.HL_PERP, 120))           # |HL| 230 ≤ 350−100 → 보냄
        await asyncio.sleep(0.05)
        assert w.baseline == 250 and len(sent) == 2
        assert sent[1] == "삼성전자\nLS 주식(S) 200 LS선물(SF) 0*10 HP -230"  # 잔고만, 변화량 없음
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_loop_sends_startup_report_when_enabled_at_start() -> None:
    positions = [_pos(H, Instrument.HL_PERP, -400),
                 _pos(H, Instrument.KR_STOCK, 200, Account.KR_STOCK)]
    sent: list[str] = []
    w = BalanceWatcher()

    def notify(text: str) -> bool:
        sent.append(text)
        return True

    async def run() -> None:
        task = asyncio.create_task(balance_watch_loop(
            lambda: positions, [H], lambda: (True, 100.0), w, notify, interval_s=0.01))
        await asyncio.sleep(0.05)
        assert w.baseline == 400
        assert sent == ["SK하이닉스\nLS 주식(S) 200 LS선물(SF) 0*10 HP -400"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
