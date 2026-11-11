"""HL 블록 우선순위 수수료 분포 — 순수 로직(kp_arb/hl_block_fees.py)."""
from __future__ import annotations

from kp_arb.hl_block_fees import (
    asset_id,
    bp_of,
    fee_bucket,
    find_block_at,
    format_block_line,
    format_fill,
    format_summary,
    format_users,
    merge_summaries,
    merge_users,
    priority_of,
    summarize_block,
    summarize_fills,
    tif_of,
    top_paid_assets,
)


def test_bp_conversion_and_buckets() -> None:
    # 공식 문서: p ÷ 10,000 = bp(p 80,000 = 8bp). 11-04 실측 최대 115,600 = 11.56bp
    assert bp_of(80_000) == 8.0 and bp_of(115_600) == 11.56
    assert fee_bucket(None) == "없음(na)"
    assert fee_bucket(0) == "p=0"
    assert fee_bucket(7) == "p 1~99(≈0, 줄서기)"
    assert fee_bucket(9_999) == "<1bp"
    assert fee_bucket(10_000) == "1~8bp" and fee_bucket(79_999) == "1~8bp"
    assert fee_bucket(80_000) == "≥8bp"


def test_priority_and_tif_parsing() -> None:
    assert priority_of({"grouping": {"p": 80000}}) == 80000
    assert priority_of({"grouping": "na"}) is None
    assert priority_of({}) is None
    assert tif_of({"t": {"limit": {"tif": "Ioc"}}}) == "Ioc"
    assert tif_of({"t": {"trigger": {"isMarket": True}}}) == "trigger"


def _tx(user: str, kind: str, orders: list[dict[str, object]] | None = None,
        grouping: object = "na") -> dict[str, object]:
    action: dict[str, object] = {"type": kind}
    if kind == "order":
        action["orders"] = orders or []
        action["grouping"] = grouping
    return {"user": user, "action": action}


def test_summarize_block_counts_by_tif_and_finds_mine() -> None:
    me = "0xABCDEF"
    block = {"blockTime": 1_793_746_801_216, "txs": [
        _tx("0x1", "cancel"),
        _tx("0x2", "order", [{"t": {"limit": {"tif": "Alo"}}}], {"p": 7}),
        _tx("0x3", "order", [{"t": {"limit": {"tif": "Ioc"}}}, {"t": {"limit": {"tif": "Ioc"}}}],
            {"p": 80000}),
        _tx(me.lower(), "order", [{"t": {"limit": {"tif": "Gtc"}}}]),
        _tx("0x4", "cancelByCloid"),
        _tx("0x5", "order", [{"t": {"limit": {"tif": "Ioc"}}}]),
    ]}
    s = summarize_block(1208130639, block, me)
    assert (s.n_tx, s.n_orders, s.n_cancels) == (6, 4, 2)
    assert s.by_tif["Ioc"] == {"≥8bp": 2, "없음(na)": 1}
    assert s.by_tif["Alo"] == {"p 1~99(≈0, 줄서기)": 1}
    assert s.by_tif["Gtc"] == {"없음(na)": 1}
    assert s.max_p == {"Alo": 7, "Ioc": 80000}
    assert [i for i, _a, _h in s.mine] == [3]
    line = format_block_line(s)
    assert "블록 1208130639" in line and "내 tx 위치 [3]" in line
    assert "주문     4(수수료 있음     3)" in line
    by_tif, max_p = merge_summaries([s, s])
    assert by_tif["Ioc"]["≥8bp"] == 4 and max_p["Ioc"] == 80000
    lines = format_summary(by_tif, max_p)
    assert lines[0].startswith("Ioc") and "≥8bp 4(66%)" in lines[0] and "최대 8.00bp" in lines[0]


def test_find_block_at_brackets_then_bisects() -> None:
    # 블록 시각 = 높이×70ms. 기준점이 멀리 떨어져 있어도 괄호를 넓힌 뒤 이진 탐색으로 맞춘다
    calls: list[int] = []

    def get_time(h: int) -> int:
        calls.append(h)
        return h * 70

    target = 1_000_000 * 70 + 5          # 1,000,000번 블록 직후 → 1,000,001이 첫 블록
    h = find_block_at(target, get_time, ref_height=900_000, ref_time_ms=900_000 * 70,
                      ms_per_block=60.0)  # 간격 추정이 틀려도(60 vs 70) 찾는다
    assert h == 1_000_001
    assert len(calls) < 60
    # 추정이 목표보다 뒤에 있는 경우(뒤로 괄호)
    calls.clear()
    h = find_block_at(target, get_time, ref_height=900_000, ref_time_ms=900_000 * 70,
                      ms_per_block=80.0)
    assert h == 1_000_001


def test_asset_filter_and_paid_asset_tally() -> None:
    # 자산 번호: 빌더 dex xyz(1번) 순번 22 = 110022(하이닉스 퍼프, 발주 패킷 실측), 본 dex는 순번
    assert asset_id(1, 22) == 110022 and asset_id(0, 211) == 211
    block = {"blockTime": 1, "txs": [
        _tx("0x1", "order", [{"a": 110034, "t": {"limit": {"tif": "Ioc"}}}], {"p": 115600}),
        _tx("0x2", "order", [{"a": 110034, "t": {"limit": {"tif": "Ioc"}}}], {"p": 100000}),
        _tx("0x3", "order", [{"a": 110022, "t": {"limit": {"tif": "Ioc"}}}], {"p": 82200}),
        _tx("0x4", "order", [{"a": 211, "t": {"limit": {"tif": "Ioc"}}}], {"p": 80000}),
        _tx("0x5", "order", [{"a": 110022, "t": {"limit": {"tif": "Alo"}}}], {"p": 7}),
        _tx("0x6", "cancel"),
    ]}
    # 필터 없음 — 전부
    s_all = summarize_block(1, block, None)
    assert s_all.n_orders == 5 and s_all.by_tif["Ioc"]["≥8bp"] == 4
    assert top_paid_assets([s_all], {110034: "xyz:SMSN", 110022: "xyz:SKHX"}) == [
        "110034(xyz:SMSN) 2건", "110022(xyz:SKHX) 1건", "211 1건"]
    # 우리 종목만 — 취소는 자산을 몰라 그대로 센다
    s_ours = summarize_block(1, block, None, assets={110034, 110022, 110045})
    assert s_ours.n_orders == 4 and s_ours.n_cancels == 1 and s_ours.n_tx == 6
    assert s_ours.by_tif["Ioc"]["≥8bp"] == 3 and s_ours.max_p["Ioc"] == 115600
    assert s_ours.by_tif["Alo"]["p 1~99(≈0, 줄서기)"] == 1
    # 하이닉스만
    s_one = summarize_block(1, block, None, assets={110022})
    assert s_one.by_tif["Ioc"] == {"≥8bp": 1} and s_one.max_p["Ioc"] == 82200


def test_user_behaviour_classification() -> None:
    # --who: 주소별로 묶어 메이커형(ALO 80%↑)·테이커형(IOC 80%↑)을 가른다. 신원은 모른다.
    alo = {"a": 110022, "t": {"limit": {"tif": "Alo"}}}
    ioc = {"a": 110022, "t": {"limit": {"tif": "Ioc"}}}
    gtc = {"a": 110022, "t": {"limit": {"tif": "Gtc"}}}
    block = {"blockTime": 1, "txs": [
        _tx("0xLP", "order", [alo], {"p": 5}), _tx("0xLP", "order", [alo], {"p": 5}),
        _tx("0xLP", "order", [alo, alo]), _tx("0xLP", "cancel"), _tx("0xLP", "cancel"),
        _tx("0xTAKER", "order", [ioc], {"p": 80000}), _tx("0xTAKER", "order", [ioc], {"p": 115600}),
        _tx("0xME", "order", [gtc]),
    ]}
    s = summarize_block(1, block, "0xME")
    users = merge_users([s, s])
    assert users["0xlp"].kind() == "메이커형(LP)" and users["0xlp"].n_orders == 8
    assert users["0xlp"].cancels == 4 and users["0xlp"].paid == 0   # p 5는 수수료로 안 침
    assert users["0xtaker"].kind() == "테이커형" and users["0xtaker"].paid == 4
    assert users["0xtaker"].max_p == 115600
    assert users["0xme"].kind() == "섞임"  # GTC만 — 메이커도 테이커도 아님
    lines = format_users(users, "0xME")
    assert lines[0].startswith("0xlp") and "메이커형(LP)" in lines[0] and "취소     4" in lines[0]
    assert any(line.endswith("← 나") for line in lines)
    assert "수수료 4건(최대 11.56bp)" in lines[1]
    # 종목 필터 땐 취소를 주소별로 안 센다(자산을 몰라서)
    s2 = summarize_block(1, block, None, assets={110022})
    assert s2.by_user["0xlp"].cancels == 0 and s2.n_cancels == 2


def test_summarize_fills_groups_pieces_by_tx_hash() -> None:
    # 체인 체결(userFillsByTime)은 조각마다 원인 주문의 tx 해시가 붙는다 — 해시별로 수량 가중
    # 평균가·조각 수·시각·수수료를 합친다(11-04 실측: 매도 100이 11조각 1083.727).
    fills = [
        {"hash": "0xaa", "oid": 1, "coin": "xyz:SKHX", "side": "A", "sz": "60", "px": "1084",
         "time": 1000, "crossed": True, "fee": "5", "feeToken": "USDC"},
        {"hash": "0xaa", "oid": 1, "coin": "xyz:SKHX", "side": "A", "sz": "40", "px": "1083",
         "time": 1000, "crossed": True, "fee": "3.4", "feeToken": "USDC"},
        {"hash": "0xbb", "oid": 2, "coin": "xyz:SKHX", "side": "B", "sz": "1", "px": "1082.6",
         "time": 2000, "crossed": False, "fee": "0.08", "feeToken": "USDC"},
        {"hash": "", "oid": 3, "sz": "1", "px": "1"},  # 해시 없는 조각은 뺀다
    ]
    out = summarize_fills(fills)
    assert set(out) == {"0xaa", "0xbb"}
    a = out["0xaa"]
    assert (a.qty, a.pieces, a.crossed, a.fee) == (100.0, 2, True, 8.4)
    assert round(a.avg_px, 3) == 1083.6
    line = format_fill(a)
    assert line.startswith("체결 매도 100 @평균 1,083.600 (2조각, ")
    assert "테이커) 수수료 8.4000 USDC #1" in line
    assert "메이커" in format_fill(out["0xbb"])
