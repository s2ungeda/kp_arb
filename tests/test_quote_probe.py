"""시세 원문 기록(quote_probe) — 임시 진단(DESIGN-ls-xing §8). 파일 쓰기는 tmp_path, 라이브 없음."""
import asyncio
import json
from pathlib import Path
from typing import Any

from kp_arb.quote_probe import QuoteProbe


def test_probe_records_only_target_trs_with_selected_fields(tmp_path: Path) -> None:
    p = QuoteProbe(tmp_path / "probe")
    assert p.record("H1_", {"shcode": "005930", "hotime": "093000", "bidho1": "70000",
                            "offerho1": "70100", "bidrem1": "10", "offerrem1": "20",
                            "volume": "123", "offerho9": "99"})
    assert p.record("S3_", {"shcode": "005930", "chetime": "093000", "price": "70100",
                            "cgubun": "+", "cvolume": "5", "volume": "128", "offerho": "70100",
                            "bidho": "70000"})
    assert not p.record("JIF", {"jangubun": "1", "jstatus": "21"})  # 대상 아님
    assert not p.record("CUR", {"price": "1380"})
    assert p.pending == 2 and p.total == 2
    assert p.flush(now=1_790_000_000.0) is not None  # 2026-09-21 — 파일명은 쓰는 날짜
    files = list((tmp_path / "probe").glob("quote_trade_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert [r["tr"] for r in rows] == ["H1_", "S3_"] and [r["n"] for r in rows] == [0, 1]
    assert rows[0]["volume"] == "123" and rows[0]["bidrem1"] == "10" and "offerho9" not in rows[0]
    assert rows[1]["cgubun"] == "+" and rows[1]["offerho"] == "70100"
    assert rows[0]["ms"] <= rows[1]["ms"] and "wall" in rows[0]
    assert p.pending == 0 and p.flush() is None  # 쌓인 게 없으면 안 씀
    p.record("JC0", {"futcode": "A116A000", "price": "275000", "cgubun": "-", "cvolume": "1"})
    p.flush(now=1_790_000_000.0)
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 3  # 같은 날 파일에 덧붙임


def test_probe_purges_files_older_than_keep_days(tmp_path: Path) -> None:
    d = tmp_path / "probe"
    d.mkdir()
    old, new = d / "quote_trade_20260901.jsonl", d / "quote_trade_20261002.jsonl"
    other = d / "note.txt"
    for f in (old, new, other):
        f.write_text("x", encoding="utf-8")
    import os

    now = 1_790_900_000.0
    os.utime(old, (now - 6 * 86400, now - 6 * 86400))
    os.utime(new, (now - 1 * 86400, now - 1 * 86400))
    os.utime(other, (now - 30 * 86400, now - 30 * 86400))
    p = QuoteProbe(d, keep_days=5)
    assert p.purge_old(now=now) == [old]
    assert new.exists() and other.exists() and not old.exists()
    assert QuoteProbe(tmp_path / "none").purge_old() == []  # 폴더 없음


async def test_xing_client_records_frames_and_flushes_on_stop(tmp_path: Path) -> None:
    # 수신 콜백은 쌓기만 하고, 기록 루프가 쓴다. 정지 때 남은 것을 쓴다.
    from kp_arb.gateways.xing_ws import XingRealClient
    from tests.test_xing_ws import FakeSession

    fake = FakeSession()

    async def ensure_login() -> None:
        return None

    probe = QuoteProbe(tmp_path / "probe")
    client = XingRealClient(fake, ensure_login=ensure_login,  # type: ignore[arg-type]
                            reconnect_backoff_s=0.0, stats_every_s=0.0, probe=probe)
    task: Any = asyncio.ensure_future(client.run())
    for _ in range(50):
        if client.status.connected:
            break
        await asyncio.sleep(0.01)
    fake.push("H1_", {"shcode": "005930", "hotime": "093000", "bidho1": "70000",
                      "offerho1": "70100", "bidrem1": "10", "offerrem1": "20"})
    fake.push("JIF", {"jangubun": "1", "jstatus": "21"})
    await asyncio.sleep(0.02)
    assert probe.total == 1 and probe.pending == 1  # 아직 파일엔 안 씀(5초 루프)
    assert not list((tmp_path / "probe").glob("*.jsonl"))
    client.stop()
    await asyncio.wait_for(task, 2.0)
    files = list((tmp_path / "probe").glob("quote_trade_*.jsonl"))
    assert len(files) == 1 and probe.pending == 0
    assert json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])["bidho1"] == "70000"
