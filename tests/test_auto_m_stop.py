"""자동M 실행 끔 — 전부 체결된 선주문은 취소를 보내지 않는다(실측 2026-09-07 LS 01433 거부)."""
from __future__ import annotations

from kp_arb.auto_m import AutoMSet, on_pre_ack, on_pre_fill, set_running
from kp_arb.domain.enums import Block


def _set() -> AutoMSet:
    return AutoMSet(target_qty=10, per_qty=2, switch_delay_s=1,
                    en_sf=0.0001, en_s=0.0002, ex_sf=-0.002)


def test_stop_after_full_fill_sends_no_cancel() -> None:
    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 2
    on_pre_ack(s, Block.ENTRY, "16725")
    on_pre_fill(s, Block.ENTRY, 2, 1_767_000.0, mono=10.0)  # 2/2 전부 체결 → 후주문 대기
    acts = set_running(s, Block.ENTRY, False)
    assert acts == []  # 취소할 잔량 없음


def test_stop_after_partial_fill_cancels_remaining() -> None:
    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 2
    on_pre_ack(s, Block.ENTRY, "16726")
    on_pre_fill(s, Block.ENTRY, 1, 1_767_000.0, mono=10.0)  # 1/2 → 1계약 남음
    acts = set_running(s, Block.ENTRY, False)
    assert [a.kind for a in acts] == ["cancel_pre"]
    assert acts[0].order_id == "16726"


def test_halt_turns_running_off_and_release_allows_rerun() -> None:
    # 중지 = 실행 꺼짐(버튼 원색). 해제 없이 켜면 그대로 중지, 해제 뒤 켜면 감시(armed).
    from kp_arb.auto_m import LegStatus, on_post_reject, release_halt

    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 2
    on_pre_ack(s, Block.ENTRY, "1")
    on_pre_fill(s, Block.ENTRY, 2, 1_767_000.0, mono=10.0)
    acts = on_post_reject(s, Block.ENTRY, "HL order not accepted")
    assert [a.kind for a in acts] == ["halt", "notify"]
    assert s.entry.status is LegStatus.HALTED and not s.entry.running
    set_running(s, Block.ENTRY, True)          # 해제 없이 켬 → 중지 그대로
    assert s.entry.status is LegStatus.HALTED
    set_running(s, Block.ENTRY, False)
    release_halt(s, Block.ENTRY)
    set_running(s, Block.ENTRY, True)          # 해제 뒤 켬 → 감시
    assert s.entry.status is LegStatus.ARMED and s.entry.running


def test_halt_is_set_wide_and_cancels_other_leg_resting_order() -> None:
    # 실측 2026-09-07: 청산 체결차로 청산만 검정, 진입은 빨강(감시 계속) → 세트 단위여야 함
    # (결정 로그 7·9 "헤지 깨진 세트는 멈춤"). 다른 다리의 걸린 선주문은 취소, 해제도 세트 단위.
    from kp_arb.auto_m import LegStatus, on_post_reject, release_halt

    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 2
    on_pre_ack(s, Block.ENTRY, "E1")               # 진입 선주문이 걸려 있음
    set_running(s, Block.EXIT, True)
    s.exit.pre_qty = 1
    on_pre_ack(s, Block.EXIT, "X1")
    on_pre_fill(s, Block.EXIT, 1, 1_780_000.0, mono=10.0)
    acts = on_post_reject(s, Block.EXIT, "HL order not accepted")
    assert [a.kind for a in acts] == ["halt", "notify", "cancel_pre"]
    assert acts[2].order_id == "E1"                # 진입 쪽 걸린 선주문 취소
    assert s.exit.status is LegStatus.HALTED and not s.exit.running
    assert s.entry.status is LegStatus.HALTED and not s.entry.running
    assert "청산 체결차로 세트 중지" in s.entry.halt_reason
    release_halt(s, Block.ENTRY)                   # 어느 다리에서 풀든 세트 전체
    assert s.entry.status is LegStatus.IDLE and s.exit.status is LegStatus.IDLE
