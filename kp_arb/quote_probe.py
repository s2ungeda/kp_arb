"""시세 원문 기록 — 호가·체결 TR의 도착 순서 분석용 임시 진단 (DESIGN-ls-xing.md §8, 2026-10-02).

모의 서버 실측(10-01·10-02)에서 체결 TR이 호가 TR보다 늦게, 그 호가에 이미 반영된 체결을 몰아서
왔다.
실서버도 같은지 보려고 운영 코어가 받는 네 TR(주식 호가 H1_·체결 S3_, 주식선물 호가 JH0·체결 JC0)의
원문 일부를 날짜별 jsonl로 남긴다. 판정·주문 경로와 무관한 기록만이고, 결론이 나면 뗀다.

쓰기는 호출자(asyncio 루프)가 record()로 쌓고 flush()를 몇 초마다 불러 한 번에 쓴다 — COM 스레드·
수신 콜백 안에서 파일을 열지 않는다. 켜고 끄기는 공통설정(기본 끔) → `enabled`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# TR별로 남길 필드 — 1·2호가 가격·잔량, 누적거래량(주식 호가 TR에만 있음), 체결가·방향·체결량
PROBE_FIELDS: dict[str, tuple[str, ...]] = {
    "H1_": ("hotime", "shcode", "offerho1", "bidho1", "offerrem1", "bidrem1", "offerho2", "bidho2",
            "offerrem2", "bidrem2", "totofferrem", "totbidrem", "volume"),
    "JH0": ("hotime", "futcode", "offerho1", "bidho1", "offerrem1", "bidrem1", "offerho2", "bidho2",
            "offerrem2", "bidrem2", "offercnt1", "bidcnt1", "totofferrem", "totbidrem"),
    "S3_": ("chetime", "shcode", "price", "cgubun", "cvolume", "volume", "offerho", "bidho",
            "mdvolume", "msvolume"),
    "JC0": ("chetime", "futcode", "price", "cgubun", "cvolume", "volume", "offerho1", "bidho1",
            "mdvolume", "msvolume"),
}
KEEP_DAYS = 5  # 이보다 오래된 기록 파일은 시동 때 지운다(하루 수백 MB)


class QuoteProbe:
    """record()로 모아 flush()로 쓴다. 파일은 <dir>/quote_trade_YYYYMMDD.jsonl(날짜는 쓰는 시점)."""

    def __init__(self, directory: Path, fields: dict[str, tuple[str, ...]] | None = None,
                 keep_days: int = KEEP_DAYS, enabled: bool = False) -> None:
        self.directory = directory
        # 공통설정 "시세 원문 기록"(DESIGN-settings §3, 사용자 2026-10-02) — 기본 끔, 바꾸면 즉시
        self.enabled = enabled
        self.fields = fields if fields is not None else PROBE_FIELDS
        self.keep_days = keep_days
        self._buf: list[str] = []
        self._n = 0
        self._t0 = time.perf_counter()

    @property
    def pending(self) -> int:
        return len(self._buf)

    @property
    def total(self) -> int:
        return self._n

    def record(self, tr: str, fields: dict[str, str], key: str = "") -> bool:
        """대상 TR이면 한 줄 쌓고 True. ms = 프로세스 기준 단조 시계(도착 순서·간격용), wall =
        벽시계, pc = 받은 PC 시각(HH:MM:SS.mmm), key = 등록할 때 쓴 종목코드. 꺼져 있으면 False."""
        keep = self.fields.get(tr)
        if keep is None or not self.enabled:
            return False
        now = time.time()
        row: dict[str, object] = {
            "n": self._n, "ms": round((time.perf_counter() - self._t0) * 1000, 2), "tr": tr,
            "key": key.strip(), "wall": round(now, 3),
            # 받은 PC의 시각(사람이 읽는 형식, 사용자 2026-10-02) — 서버 시간 필드(초)와 대조용
            "pc": time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}",
        }
        for k in keep:
            row[k] = fields.get(k, "")
        self._buf.append(json.dumps(row, ensure_ascii=False))
        self._n += 1
        return True

    def flush(self, now: float | None = None) -> Path | None:
        """쌓인 줄을 오늘 파일에 덧붙인다. 쓴 게 없으면 None."""
        if not self._buf:
            return None
        lines, self._buf = self._buf, []
        self.directory.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y%m%d", time.localtime(now if now is not None else time.time()))
        path = self.directory / f"quote_trade_{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def purge_old(self, now: float | None = None) -> list[Path]:
        """keep_days보다 오래된 기록 파일 삭제(파일 수정 시각 기준). 지운 경로 목록."""
        if not self.directory.is_dir():
            return []
        cutoff = (now if now is not None else time.time()) - self.keep_days * 86400
        gone: list[Path] = []
        for p in self.directory.glob("quote_trade_*.jsonl"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                gone.append(p)
        return gone
