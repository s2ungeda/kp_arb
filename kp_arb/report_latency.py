"""자동M 한 판의 지연·슬리피지 리포트 — 선주문 체결 수신 → 후주문 발주 → 응답 → 체결, 기준est 대비.

로그 파일만 읽는다(라이브 접속 없음). 결과는 화면(stdout)과 `<로그폴더>/report_<날짜>.md` 둘 다.

    운영 PC(배포판):  meme-core.exe report [YYYYMMDD]     (report.bat 더블클릭 = 오늘)
    개발 PC:          python -m kp_arb.report_latency 20260914 real_log
                      python tools/round_latency.py 20260914   (real_log/ 기본)
날짜를 안 주면 오늘, 폴더를 안 주면 exe 옆(개발은 프로젝트) logs/.

시각의 출처
  ① 선체결 수신  autom_<종목>_<날짜>.log "체결 … 선주문 #N …"
                 (LS 체결 통보를 세트 장부에 반영한 시각)
  ② 후주문 발주  hl_order_<날짜>.log "발주요청 [자동M] …"
                 (HL로 보내기 직전; 요청패킷의 cloid로 주문번호와 짝)
  ③ 후주문 응답  ② + "HL 발주 왕복 N ms #oid"
  ④ 후주문 체결  autom 로그 "체결 … 후주문 #oid … HL대기 0" (마지막 조각.
                 여러 조각이면 첫 조각 시각도 괄호로 표시)
③→④가 음수면 발주 응답보다 체결 통보가 먼저 와 cloid로 세트에 연결된 경우.
기준est = 선주문 발주 시점 HL est(후주문 방향), 체결평균 = 그 판 후주문 조각들의 수량가중 평균
체결가, 차이 = 유리하면 +(매도는 체결−est, 매수는 est−체결). 기준est는 체결 줄(09-14 저녁
빌드~)에서, 없으면 발주 '통과' 줄(09-14 오후 빌드~)에서 가져온다. 현est@선체결·현est@후체결은
그 순간 호가창으로 다시 계산한 est(09-15 빌드~; 사용자 09-15 요청).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_PRE = re.compile(r"INFO 체결 (정방향|역방향) (\d)세트 (진입|청산): 선주문 #(\d+) "
                  r".*?(?:현est ([\d.]+|-) )?→ 누적.*HL 대기 ([\d.e+-]+)")
_POST = re.compile(r"INFO 체결 (정방향|역방향) (\d)세트 (진입|청산): 후주문 #(\d+) "
                   r"HL ([\d.]+) @ ([\d.]+)(?: 기준est ([\d.]+|-))?(?: 차이 \S+)?"
                   r"(?: 현est ([\d.]+|-))?.*HL대기 ([\d.e+-]+)")
# 옛 로그(체결 줄에 기준est가 없던 빌드)용 대체: 선주문 '통과' 줄의 est(발주 시점 값)
_PASS = re.compile(r"INFO 판정 (정방향|역방향) (\d)세트 (진입|청산): 통과 → 선주문 .* est ([\d.]+)")
_PACKET = re.compile(r'HL 요청패킷 .*"c": "(0x[0-9a-f]+)"')
_RTT = re.compile(r"HL 발주 왕복 (\d+) ms #(\d+) cloid=(0x[0-9a-f]+)")

_HEAD = ("| # | 종목 | 세트 | 블록 | 선주문# | ① 선체결 수신 | ② 후주문 발주 | ③ 후주문 응답 "
         "| ④ 후주문 체결 | ①→② | ②→③ | ③→④ | ①→④ | 기준est | 현est@선체결 | 현est@후체결 "
         "| 체결평균 | 차이(유리+) |")
_SEP = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"


def _fnum(text: str | None) -> float | None:
    return float(text) if text and text != "-" else None


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
        pieces: dict[tuple[str, str], list[tuple[float, float]]] = {}  # 조각 (수량, 가격)
        est_of: dict[tuple[str, str], float | None] = {}
        now_pre: dict[tuple[str, str], float | None] = {}   # 선체결 순간 현est
        now_post: dict[tuple[str, str], float | None] = {}  # 후체결(마지막 조각) 순간 현est
        pass_est: dict[tuple[str, str], float] = {}  # 마지막 '통과'(발주) 줄의 est
        for line in _lines(f):
            m = _PASS.search(line)
            if m:
                pass_est[(m.group(1) + m.group(2), m.group(3))] = float(m.group(4))
                continue
            m = _PRE.search(line)
            if m:
                key = (m.group(1) + m.group(2), m.group(3))
                if key not in pre_open:
                    pre_open[key] = (_ts(line), m.group(4))
                    now_pre[key] = _fnum(m.group(5))  # 선체결 순간 현est(09-15 빌드~)
                continue
            m = _POST.search(line)
            if not m:
                continue
            key = (m.group(1) + m.group(2), m.group(3))
            if key not in pre_open:
                continue
            first_fill.setdefault(key, _ts(line))
            pieces.setdefault(key, []).append((float(m.group(5)), float(m.group(6))))
            if m.group(7) and m.group(7) != "-":
                est_of[key] = float(m.group(7))
            now_post[key] = _fnum(m.group(8))
            if float(m.group(9)) > 1e-6:
                continue  # 부분 체결 — 마지막 조각(HL대기 0)에서 집계
            t_pre, pre_id = pre_open.pop(key)
            oid = m.group(4)
            qp = pieces.pop(key)
            avg_px = sum(q * px for q, px in qp) / sum(q for q, _ in qp)
            est = est_of.pop(key, None)
            if est is None:
                est = pass_est.get(key)  # 체결 줄에 없으면 발주 '통과' 줄의 est로
            sell = key[1] == "진입"  # 정방향 진입 = HL 매도(역방향은 반대)
            if key[0].startswith("역방향"):
                sell = not sell
            diff = None if est is None else (avg_px - est if sell else est - avg_px)
            t_send = send_at.get(oid)
            t_resp = None
            if t_send is not None and oid in rtt:
                t_resp = t_send + timedelta(milliseconds=rtt[oid])
            rows.append({"name": name, "set": key[0], "blk": key[1], "pre": pre_id, "oid": oid,
                         "t_pre": t_pre, "t_send": t_send, "t_resp": t_resp,
                         "t_first": first_fill.pop(key), "t_fill": _ts(line),
                         "avg_px": avg_px, "est": est, "diff": diff,
                         "now_pre": now_pre.pop(key, None), "now_post": now_post.pop(key, None)})
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
                 _ms(r["t_resp"], r["t_fill"]), _ms(r["t_pre"], r["t_fill"]),
                 f"{r['est']:g}" if r["est"] is not None else "-",
                 f"{r['now_pre']:g}" if r["now_pre"] is not None else "-",
                 f"{r['now_post']:g}" if r["now_post"] is not None else "-",
                 f"{r['avg_px']:g}",
                 (f"{r['diff']:+g}({r['diff'] / r['est'] * 100:+.3f}%)"
                  if r["diff"] is not None else "-")]
        out.append("| " + " | ".join(cells) + " |")
    tot = sorted((r["t_fill"] - r["t_pre"]).total_seconds() * 1000 for r in rows)
    if tot:
        out.append("")
        out.append(f"판 수 {len(tot)} / ①→④ 최소 {tot[0]:.0f} 중앙 {tot[len(tot) // 2]:.0f} "
                   f"평균 {sum(tot) / len(tot):.0f} 최대 {tot[-1]:.0f} ms")
        diffs = [r["diff"] / r["est"] * 100 for r in rows if r["diff"] is not None]
        if diffs:
            d = sorted(diffs)
            out.append(f"기준est 대비 체결(유리+): 최소 {d[0]:+.3f}% 중앙 {d[len(d) // 2]:+.3f}% "
                       f"평균 {sum(d) / len(d):+.3f}% 최대 {d[-1]:+.3f}% ({len(d)}판)")
    return "\n".join(out)


def default_log_dir() -> Path:
    """배포판은 exe 옆 logs/, 개발은 프로젝트 logs/ (core_server._base_dir와 같은 규칙)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 윈도우 콘솔(CP949) 깨짐 방지
    day = argv[1] if len(argv) > 1 and argv[1].isdigit() else datetime.now().strftime("%Y%m%d")
    log_dir = Path(argv[2]) if len(argv) > 2 else default_log_dir()
    rows = rounds(log_dir, day)
    if not rows:
        print(f"{log_dir / f'autom_*_{day}.log'} 에 후주문까지 끝난 판이 없음")
        return 1
    text = render(rows)
    print(text)
    out = log_dir / f"report_{day}.md"
    try:
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {out}")
    except OSError as exc:
        print(f"\n저장 실패: {out} — {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
