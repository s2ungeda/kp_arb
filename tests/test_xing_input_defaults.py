"""xing 요청 본문 기본값 — Res InBlock엔 있는데 본문이 안 준 필드(RecCnt·구분코드) 채우기.

운영 PC 실측 2026-09-29 08:42: CSPAQ22200을 AcntNo·Pwd만 보내자 실서버가 09604 "입력 데이터 포맷이
맞지않습니다"로 거부(REST 서버는 비운 필드를 채워 줬지만 xing은 고정길이 레코드를 그대로 보낸다)."""
from pathlib import Path

from kp_arb.gateways.xing import input_defaults, to_blocks

_RES_22200 = """BEGIN_FUNCTION_MAP
\t.Func,현물계좌예수금,CSPAQ22200,attr,block,headtype=B;
\tBEGIN_DATA_MAP
\tCSPAQ22200InBlock1,입력,input;
\tbegin
\t\t레코드갯수,RecCnt,RecCnt,long,5;
\t\t관리지점번호,MgmtBrnNo,MgmtBrnNo,char,3;
\t\t계좌번호,AcntNo,AcntNo,char,20;
\t\t비밀번호,Pwd,Pwd,char,8;
\t\t잔고생성구분,BalCreTp,BalCreTp,char,1;
\tend
\tCSPAQ22200OutBlock2,출력,output;
\tbegin
\t\t현금주문가능금액,MnyOrdAbleAmt,MnyOrdAbleAmt,long,16;
\tend
\tEND_DATA_MAP
END_FUNCTION_MAP
"""
_RES_T0441 = """BEGIN_FUNCTION_MAP
\t.Func,선물옵션잔고,t0441,attr,block,headtype=A;
\tBEGIN_DATA_MAP
\tt0441InBlock,입력,input;
\tbegin
\t\t계좌번호,accno,accno,char,11;
\t\t비밀번호,passwd,passwd,char,8;
\t\t연속코드,cts_expcode,cts_expcode,char,8;
\tend
\tEND_DATA_MAP
END_FUNCTION_MAP
"""


def test_input_defaults_fill_only_missing_res_fields() -> None:
    fields = ("RecCnt", "MgmtBrnNo", "AcntNo", "Pwd", "BalCreTp")
    out = input_defaults("CSPAQ22200", fields, {"AcntNo": "1", "Pwd": "p"})
    assert out == {"AcntNo": "1", "Pwd": "p", "RecCnt": "1", "MgmtBrnNo": "", "BalCreTp": "0"}
    # 본문이 준 값이 우선, Res에 없는 필드는 안 채움
    out2 = input_defaults("CSPAQ22200", ("AcntNo", "BalCreTp"), {"AcntNo": "1", "BalCreTp": "1"})
    assert out2 == {"AcntNo": "1", "BalCreTp": "1"}
    # 공통 기본값(RecCnt)만 있는 TR
    assert input_defaults("CFOBQ10500", ("RecCnt", "AcntNo"), {"AcntNo": "1"}) == {
        "AcntNo": "1", "RecCnt": "1"}
    # 기본값이 없는 필드(t0441 cts_expcode)는 비워 둔다(연속조회 키 — Space가 맞음)
    assert input_defaults("t0441", ("accno", "cts_expcode"), {"accno": "1"}) == {"accno": "1"}


def test_to_blocks_applies_defaults_from_res(tmp_path: Path) -> None:
    (tmp_path / "CSPAQ22200.res").write_bytes(_RES_22200.encode("cp949"))
    (tmp_path / "t0441.res").write_bytes(_RES_T0441.encode("cp949"))
    blocks = to_blocks("CSPAQ22200", {"AcntNo": "20142871001", "Pwd": "1234"}, tmp_path)
    assert blocks == {"CSPAQ22200InBlock1": {
        "AcntNo": "20142871001", "Pwd": "1234", "RecCnt": "1", "MgmtBrnNo": "", "BalCreTp": "0"}}
    # 별칭(InptPwd → Pwd) 뒤에 기본값이 붙는다
    blocks2 = to_blocks("CSPAQ22200", {"AcntNo": "1", "InptPwd": "p"}, tmp_path)
    assert blocks2["CSPAQ22200InBlock1"]["Pwd"] == "p"
    assert blocks2["CSPAQ22200InBlock1"]["RecCnt"] == "1"
    assert to_blocks("t0441", {"accno": "1", "passwd": "p"}, tmp_path) == {
        "t0441InBlock": {"accno": "1", "passwd": "p"}}
    # Res가 없으면(폴더 미지정) 예전처럼 그대로
    assert to_blocks("CSPAQ22200", {"AcntNo": "1", "Pwd": "p"}) == {
        "CSPAQ22200InBlock1": {"AcntNo": "1", "Pwd": "p"}}
