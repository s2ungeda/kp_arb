"""자동M 한 판의 지연 표 — 선주문 체결 수신 → 후주문 발주 → 후주문 응답 → 후주문 체결.

운영 로그 복사본(real_log/)을 읽어 마크다운 표로 찍는다. 라이브 접속 없음.

    python tools/round_latency.py 20260914            # real_log/ 의 그 날짜
    python tools/round_latency.py 20260914 logs       # 다른 폴더(개발 PC logs/)

시각의 출처
  ① 선체결 수신  autom_<종목>_<날짜>.log "체결 … 선주문 #N …"
                 (LS 체결 통보를 세트 장부에 반영한 시각)
  ② 후주문 발주  hl_order_<날짜>.log "발주요청 [자동M] …"
                 (HL로 보내기 직전; 요청패킷의 cloid로 주문번호와 짝)
  ③ 후주문 응답  ② + "HL 발주 왕복 N ms #oid"
  ④ 후주문 체결  autom 로그 "체결 … 후주문 #oid … HL대기 0" (마지막 조각.
                 여러 조각이면 첫 조각 시각도 괄호로 표시)
③→④가 음수면 발주 응답보다 체결 통보가 먼저 와 cloid로 세트에 연결된 경우.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_PRE = re.compile(r"INFO 체결 (정방향|역방향) (\d)세트 (진입|청산): 선주문 #(\d+) "
                  r".*HL 대기 ([\d.e+-]+)")
_POST = re.compile(r"INFO 체결 (정방향|역방향) (\d)세트 (진입|청산): 후주문 #(\d+) "
                   r"HL ([\d.]+) @ ([\d.]+).*HL대기 ([\d.e+-]+)")
_PACKET = re.compile(r'HL 요청패킷 .*"c": "(0x[0-9a-f]+)"')
_RTT = re.compile(r"HL 발주 왕복 (\d+) ms #(\d+) cloid=(0x[0-9a-f]+)")

_HEAD = ("| # | 종목 | 세트 | 블록 | 선주문# | ① 선체결 수신 | ② 후주문 발주 | ③ 후주문 응답 "
         "| ④ 후주문 체결 | ①→② | ②→③ | ③→④ | ①→④ |")
_SEP = "|---|---|---|---|---|---|---|---|---|---|---|---|---|"


def _ts(line: str) -> datetime:
    return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def hl_send_times(hl_order_log: Path) -> tuple[dict[str, datetime], dict[str, int]]:
    """HL 주문 로그 → (주문번호→발주요청 시각, 주문번호→왕복 ms). 요청과 응답은 cloid로 짝."""
    send_at_cloid: dict[str, datetime] = {}
    send_at: dict[str, datetime] = {}
    rtt: dict[str, int] = {}
    last_req: datetime | None = None
    for line in _lines(hl_order_log):
        if "발주요청 [자동M]" in line:
            last_req = _ts(line)
        m = _PACKET.search(line)
        if m and last_req is not None:
            send_at_cloid[m.group(1)] = last_req
        m = _RTT.search(line)
        if m:
            rtt[m.group(2)] = int(m.group(1))
            if m.group(3) in send_at_cloid:
                send_at[m.group(2)] = send_at_cloid[m.group(3)]
    return send_at, rtt


def rounds(log_dir: Path, day: str) -> list[dict[str, Any]]:
    """자동M 로그에서 후주문까지 끝난 판을 모은다(세트·블록별 선체결 → 후주문 마지막 조각)."""
    send_at, rtt = hl_send_times(log_dir / f"hl_order_{day}.log")
    rows: list[dict[str, Any]] = []
    for f in sorted(log_dir.glob(f"autom_*_{day}.log")):
        name = f.stem[len("autom_"):-len(f"_{day}")]
        pre_open: dict[tuple[str, str], tuple[datetime, str]] = {}
        first_fill: dict[tuple[str, str], datetime] = {}
        for line in _lines(f):
            m = _PRE.search(line)
            if m:
                key = (m.group(1) + m.group(2), m.group(3))
                pre_open.setdefault(key, (_ts(line), m.group(4)))
                continue
            m = _POST.search(line)
            if not m:
                continue
            key = (m.group(1) + m.group(2), m.group(3))
            if key not in pre_open:
                continue
            first_fill.setdefault(key, _ts(line))
            if float(m.group(7)) > 1e-6:
                continue  # 부분 체결 — 마지막 조각(HL대기 0)에서 집계
            t_pre, pre_id = pre_open.pop(key)
            oid = m.group(4)
            t_send = send_at.get(oid)
            t_resp = None
            if t_send is not None and oid in rtt:
                t_resp = t_send + timedelta(milliseconds=rtt[oid])
            rows.append({"name": name, "set": key[0], "blk": key[1], "pre": pre_id, "oid": oid,
                         "t_pre": t_pre, "t_send": t_send, "t_resp": t_resp,
                         "t_first": first_fill.pop(key), "t_fill": _ts(line)})
        for key, (_, pre_id) in pre_open.items():
            print(f"(후주문 미완료 — 집계 제외) {name} {key[0]} {key[1]} 선주문 #{pre_id}",
                  file=sys.stderr)
    return rows


def _hms(t: datetime | None) -> str:
    return t.strftime("%H:%M:%S.%f")[:-3] if t else "-"


def _ms(a: datetime | None, b: datetime | None) -> str:
    return f"{(b - a).total_seconds() * 1000:+.0f}" if (a and b) else "-"


def render(rows: list[dict[str, Any]]) -> str:
    out = [_HEAD, _SEP]
    for i, r in enumerate(rows, 1):
        fill = _hms(r["t_fill"])
        if r["t_first"] != r["t_fill"]:
            fill += f" (첫 {_hms(r['t_first'])})"
        cells = [str(i), r["name"], r["set"], r["blk"], f"#{r['pre']}",
                 _hms(r["t_pre"]), _hms(r["t_send"]), _hms(r["t_resp"]), fill,
                 _ms(r["t_pre"], r["t_send"]), _ms(r["t_send"], r["t_resp"]),
                 _ms(r["t_resp"], r["t_fill"]), _ms(r["t_pre"], r["t_fill"])]
        out.append("| " + " | ".join(cells) + " |")
    tot = sorted((r["t_fill"] - r["t_pre"]).total_seconds() * 1000 for r in rows)
    if tot:
        out.append("")
        out.append(f"판 수 {len(tot)} / ①→④ 최소 {tot[0]:.0f} 중앙 {tot[len(tot) // 2]:.0f} "
                   f"평균 {sum(tot) / len(tot):.0f} 최대 {tot[-1]:.0f} ms")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔(CP949)에서 한글·기호 깨짐 방지
    day = argv[1] if len(argv) > 1 else datetime.now().strftime("%Y%m%d")
    default_dir = Path(__file__).resolve().parent.parent / "real_log"
    log_dir = Path(argv[2]) if len(argv) > 2 else default_dir
    rows = rounds(log_dir, day)
    if not rows:
        print(f"{log_dir}/autom_*_{day}.log 에 후주문까지 끝난 판이 없음")
        return 1
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
