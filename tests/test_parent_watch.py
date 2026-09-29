"""부모(메인) 감시 — 사라지면 안전종료 → 유예 뒤 강제 종료(운영 사고 2026-09-29, 3겹)."""
from kp_arb.parent_watch import ParentWatch


def _run(alive_seq: list[bool], *, stop_after_gone: bool = False,
         misses: int = 2) -> tuple[list[str], list[float]]:
    events: list[str] = []
    sleeps: list[float] = []
    seq = iter(alive_seq)
    watch: ParentWatch

    def is_alive() -> bool:
        return next(seq, False)

    def on_gone() -> None:
        events.append("gone")
        if stop_after_gone:
            watch.stopped = True  # 안전종료가 제때 끝난 상황 흉내

    def sleep(s: float) -> None:
        sleeps.append(s)

    watch = ParentWatch(is_alive, on_gone, lambda: events.append("hard"),
                        interval_s=2.0, grace_s=10.0, sleep=sleep, misses=misses)
    watch.run()
    return events, sleeps


def test_parent_gone_triggers_graceful_then_hard_exit() -> None:
    events, sleeps = _run([True, True, False, False])
    assert events == ["gone", "hard"]
    assert sleeps == [2.0, 2.0, 2.0, 2.0, 10.0]   # 감시 4번 + 유예 10초


def test_single_miss_is_ignored_and_graceful_exit_skips_hard() -> None:
    # 순간 조회 실패 한 번은 무시(연속 2번이어야). 안전종료가 유예 안에 끝나면 강제 종료 없음.
    events, _ = _run([True, False, True, False, False], stop_after_gone=True)
    assert events == ["gone"]


def test_stop_flag_ends_loop_without_firing() -> None:
    watch = ParentWatch(lambda: True, lambda: None, lambda: None, sleep=lambda s: None)
    watch.stopped = True
    watch.run()
    assert not watch.fired
