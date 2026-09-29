"""xingAPI 접속 전수 점검 — 로그인 · 조회 TR 전부 · 실시간 등록 전부 · 통보 채널 (주문 없음).

운영 PC에서 코어를 띄우지 않고 한 번에 전부 확인한다(사용자 2026-09-29: 옮겨 풀고 켜 보고 첫
오류에서 멈추는 반복이 번거로움). 항목별 OK/FAIL을 표로 내고, 하나라도 FAIL이면 종료 코드 1.

    .venv32\\Scripts\\python -m kp_arb.xing_check [초]   # 개발 PC(모의), 기본 실시간 10초
    xing_check.bat [초]                                  # 배포판(meme-core.exe xingcheck)

자격은 keyring(LS_XING_ID/PW/CERT_PW)·.env(KP_XING_HOST 등)에서 읽고 로그에 남기지 않는다.
조회 본문은 코어(XingGateway)가 쓰는 것과 같다 — 실서버가 거부하는 필드 형식(09604 등)이 여기서
걸린다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("kp_arb.xing_check")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def render_report(results: list[CheckResult]) -> str:
    """항목별 한 줄(OK/FAIL 이름 상세) + 요약. 순수."""
    lines = [f"{'OK  ' if r.ok else 'FAIL'} {r.name:<28} {r.detail}" for r in results]
    fails = sum(1 for r in results if not r.ok)
    lines.append(f"— {len(results)}항목 중 실패 {fails}건" + ("" if fails else " (전부 정상)"))
    return "\n".join(lines)


def realtime_results(registered: list[str], counts: dict[str, int],
                     seconds: float) -> list[CheckResult]:
    """실시간 TR별 결과 — 등록은 됐고 수신 건수만 보고. 0건은 장중이 아니면 정상이라 FAIL이 아니다
    (통보 TR SC*/O01/C01/H01은 주문이 없으면 0건이 당연함). 순수."""
    out: list[CheckResult] = []
    for tr in registered:
        n = counts.get(tr, 0)
        note = f"{n}건/{seconds:.0f}초" + ("" if n else " (0건 — 장중 아니거나 이벤트 없음)")
        out.append(CheckResult(f"실시간 {tr}", True, note))
    return out


async def _timed(name: str, fn: Callable[[], Awaitable[Any]]) -> CheckResult:
    t0 = time.perf_counter()
    try:
        out = await fn()
    except Exception as exc:  # noqa: BLE001 - 항목별로 사유를 표에
        return CheckResult(name, False, str(exc)[:200])
    ms = (time.perf_counter() - t0) * 1000
    if isinstance(out, list | tuple):
        summary = f"{len(out)}건"
    elif isinstance(out, dict):
        summary = f"{len(out)}개"
    else:
        summary = repr(out)[:60]
    return CheckResult(name, True, f"{ms:.0f}ms {summary}")


async def _main(seconds: float) -> int:  # noqa: PLR0915 - 점검 순서를 한 함수에 나열
    from .bootstrap import select_months
    from .config import ConfigError, LSAccounts, XingCredentials
    from .domain.enums import Account, Instrument, Underlying
    from .gateways.xing import XingGateway
    from .gateways.xing_com import Win32ComFactory, XingSession
    from .gateways.xing_ws import XingRealClient
    from .theory import select_usd_futures_months

    results: list[CheckResult] = []
    creds = XingCredentials.load()
    res_dir = Path(creds.path) / "Res"
    n_res = len(list(res_dir.glob("*.res"))) if res_dir.is_dir() else 0
    server = "모의" if creds.server_type else "실서버"
    results.append(CheckResult("환경", n_res > 0,
                               f"{creds.host}:{creds.port} {server} 설치 {creds.path} "
                               f"Res {n_res}개"))
    if n_res == 0:
        print(render_report(results))
        return 1
    session = XingSession(Win32ComFactory(creds.path), res_dir)  # 설치 폴더 → DLL 탐색 경로
    await session.start()
    t0 = time.perf_counter()
    try:
        accts = await session.login(creds.host, creds.port, creds.user_id, creds.password,
                                    creds.cert_password, creds.server_type)
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("로그인", False, str(exc)[:200]))
        print(render_report(results))
        session.close()
        return 1
    results.append(CheckResult("로그인", True,
                               f"{(time.perf_counter() - t0) * 1000:.0f}ms 계좌 {len(accts)}개"))
    try:
        accounts = LSAccounts.load()
    except ConfigError as exc:
        results.append(CheckResult("계좌 설정", False, f"{exc} — 키 등록 창에서 계좌를 넣으세요"))
        print(render_report(results))
        session.close()
        return 1
    results.append(CheckResult("계좌 설정", True, "주식·선물 계좌 키 있음"))

    # --- 조회 TR (코어와 같은 본문) — 마스터 → 월물 → 계좌·시세 ---
    gw = XingGateway.from_session(session, accounts, res_dir=res_dir)
    fut_rows: list[dict[str, Any]] = []
    fx_months: list[tuple[str, int]] = []
    futures_symbols: dict[Underlying, str] = {}
    next_symbols: dict[Underlying, str] = {}

    async def _master() -> list[dict[str, Any]]:
        nonlocal fut_rows
        fut_rows = await gw.fetch_futures_master()
        return fut_rows

    async def _commodity() -> list[tuple[str, int]]:
        nonlocal fx_months
        from datetime import datetime

        rows = await gw.fetch_commodity_master()
        fx_months = select_usd_futures_months(rows, datetime.now(), count=2)
        return fx_months

    results.append(await _timed("t8401 주식선물 마스터", _master))
    if fut_rows:
        months = select_months(fut_rows, count=2)
        for u, picked in months.items():
            if picked:
                futures_symbols[u] = picked[0][0]
            if len(picked) > 1:
                next_symbols[u] = picked[1][0]
        results.append(CheckResult("주식선물 월물", bool(futures_symbols),
                                   ", ".join(f"{u.value}={c}" for u, c in futures_symbols.items())
                                   or "월물 없음"))
    await asyncio.sleep(1.1)
    results.append(await _timed("t8426 상품선물 마스터", _commodity))
    results.append(CheckResult("원달러선물 월물", bool(fx_months),
                               ", ".join(c for c, _ in fx_months) or "월물 없음"))
    gw = XingGateway.from_session(session, accounts, res_dir=res_dir,
                                  futures_symbols=futures_symbols,
                                  next_futures_symbols=next_symbols)
    fx_code = fx_months[0][0] if fx_months else "A756A000"
    sf_code = futures_symbols.get(Underlying.SAMSUNG, "A116A000")
    checks: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        ("t2111 원달러선물 현재가", lambda: gw.get_fx_futures_price(fx_code)),
        ("t1102 주식 현재가", lambda: gw.get_last_price("005930")),
        ("t8402 주식선물 현재가", lambda: gw.get_last_price(sf_code, futures=True)),
    ]
    if accounts is not None:
        checks += [
            ("CSPAQ22200 주식 예수금", lambda: gw.get_balance(Account.KR_STOCK)),
            ("CFOBQ10500 선물 예탁금", lambda: gw.get_balance(Account.KR_DERIV)),
            ("CSPAQ12300 주식 잔고", lambda: gw.get_positions(Account.KR_STOCK)),
            ("t0441 선물 잔고", lambda: gw.get_positions(Account.KR_DERIV)),
            ("CSPAQ13700 주식 미체결", lambda: gw.get_open_orders(Account.KR_STOCK)),
            ("t0434 선물 미체결", lambda: gw.get_open_orders(Account.KR_DERIV)),
            ("CSPAQ12300 신용 대출일", lambda: gw.get_credit_loans(Underlying.SAMSUNG)),
        ]
    for name, fn in checks:
        await asyncio.sleep(1.1)  # 초당 한도 여유
        results.append(await _timed(name, fn))
    per_sec, _base, limit, used = await session.limits("t1102")
    results.append(CheckResult("요청 한도(t1102)", True,
                               f"초당 {per_sec} · 한도 {limit or '무제한'} · 사용 {used}"))

    # --- 실시간 등록 — 코어와 같은 구독(시세·예상체결·VI·장운영·통보·원달러·CUR) ---
    async def _noop() -> None:
        return None

    real = XingRealClient(session, ensure_login=_noop, etf_symbols={})
    for u in Underlying:
        real.subscribe_quotes(u)
        real.subscribe_trades(u)
        real.subscribe_vi(u)
    if futures_symbols:
        real.subscribe_futures_quotes(futures_symbols)
    if next_symbols:
        real.subscribe_futures_quotes(next_symbols, instrument=Instrument.KR_STOCK_FUTURE_NEXT)
    real.subscribe_market_status()
    real.subscribe_stock_fills()
    real.subscribe_futures_fills()
    for code, _ in fx_months:
        real.subscribe_fx(code)
    real.subscribe_fx_spot()
    counts: dict[str, int] = {}

    def on_raw(raw: str) -> None:
        try:
            tr = str(json.loads(raw).get("header", {}).get("tr_cd", "?"))
        except (ValueError, AttributeError):
            tr = "?"
        counts[tr] = counts.get(tr, 0) + 1

    real.on_raw.append(on_raw)
    registered = list(dict.fromkeys(tr for tr, _k, _t in real._subs))
    task = asyncio.create_task(real.run())
    try:
        await asyncio.sleep(0.5)
        results.append(CheckResult("실시간 등록", not task.done() or task.exception() is None,
                                   f"{len(real._subs)}건 등록 — {seconds:.0f}초 수신 대기"))
        await asyncio.sleep(seconds)
    finally:
        real.stop()
        try:
            await asyncio.wait_for(task, 5.0)
        except (TimeoutError, Exception):  # noqa: BLE001 - 종료 중 예외는 표에만
            pass
    results.extend(realtime_results(registered, counts, seconds))
    session.close()
    text = render_report(results)
    print(text)
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(sys.executable).resolve().parent / ".env")  # 배포판: exe 옆
        load_dotenv()                                                   # 개발: cwd
    except ImportError:
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    seconds = float(args[0]) if args and args[0].replace(".", "", 1).isdigit() else 10.0
    return asyncio.run(_main(seconds))


if __name__ == "__main__":
    raise SystemExit(main())
