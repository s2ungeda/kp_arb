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


def test_stop_cancels_even_while_replace_cancel_pending() -> None:
    # 실측 2026-09-08: 역산가 변경 취소가 LS 한도에 걸려 실패 → '취소 대기' 표시만 남은 채 종료
    # → 정지 루틴이 건너뛰어 선주문이 LS에 남음. 끔·정지·종료는 표시와 무관하게 취소를 보낸다.
    from kp_arb.auto_m import on_pre_cancel_failed

    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 1
    on_pre_ack(s, Block.ENTRY, "9865")
    s.entry.replace_pending = True                 # 역산가 변경 취소 요청 중(확인 안 옴)
    s.entry.cancel_sent = True
    acts = set_running(s, Block.ENTRY, False)
    assert [a.kind for a in acts] == ["cancel_pre"] and acts[0].order_id == "9865"
    # 취소 요청 자체가 실패하면 표시를 되돌려 다음 판정이 다시 보낸다
    s2 = _set()
    set_running(s2, Block.ENTRY, True)
    s2.entry.pre_qty = 1
    on_pre_ack(s2, Block.ENTRY, "9866")
    s2.entry.replace_pending = s2.entry.cancel_sent = True
    on_pre_cancel_failed(s2, Block.ENTRY)
    assert not s2.entry.replace_pending and not s2.entry.cancel_sent
    assert s2.entry.pre_order_id == "9866"         # 주문은 그대로 걸려 있음


def test_trade_result_commits_only_after_full_post_fill() -> None:
    # 사용자 확정 2026-09-08: 선주문 체결은 판 버퍼에 보관, 후주문 전량 체결 확인 뒤 매매결과에.
    # 부분값 표시 없음. 중지로 판이 끝나면 짝이 맞은(적은 쪽) 몫만.
    from kp_arb.auto_m import AutoMSettings, on_post_fill, on_post_reject

    st = AutoMSettings()
    s = _set()
    set_running(s, Block.ENTRY, True)
    s.entry.pre_qty = 1
    on_pre_ack(s, Block.ENTRY, "1")
    on_pre_fill(s, Block.ENTRY, 1, 1_836_000.0, mono=10.0)   # SF 1계약 → HL 10 대기
    assert s.entry.acc.sf_qty == 0 and s.entry.pending.sf_qty == 1  # 아직 매매결과엔 없음
    on_post_fill(s, Block.ENTRY, 4.0, 1313.0, 1340.0, 11.0, st)  # HL 일부 4
    assert s.entry.acc.hl_qty == 0 and s.entry.pending.hl_qty == 4  # 부분값 표시 없음
    on_post_fill(s, Block.ENTRY, 6.0, 1314.0, 1341.0, 12.0, st)  # 전량 10 확인 → 그 판 합침
    assert s.entry.pending.hl_qty == 0 and s.entry.pending.sf_qty == 0
    assert s.entry.acc.sf_qty == 1 and s.entry.acc.hl_qty == 10
    assert abs(s.entry.acc.hl_avg() - 1313.6) < 1e-9  # (4×1313 + 6×1314)/10
    # 다음 판: SF 1 체결 후 HL 0.588만 잡히고 중지 → 짝 맞은 몫(HL 0.588, SF 0.0588)만 추가
    s.entry.pre_qty, s.entry.pre_filled = 1, 0
    on_pre_ack(s, Block.ENTRY, "2")
    on_pre_fill(s, Block.ENTRY, 1, 1_840_000.0, mono=20.0)
    on_post_fill(s, Block.ENTRY, 0.588, 1315.0, 1342.0, 21.0, st)
    assert s.entry.acc.hl_qty == 10  # 전량 아님 → 아직 그대로
    on_post_reject(s, Block.ENTRY, "HL order not accepted")
    assert abs(s.entry.acc.hl_qty - 10.588) < 1e-9 and abs(s.entry.acc.sf_qty - 1.0588) < 1e-9
    assert s.entry.pending.hl_qty == 0 and s.entry.pending.sf_qty == 0
