"""xingAPI Res 파서 — 블록·필드·occurs·키 필드 (순수 로직, DESIGN-ls-xing.md §2)."""
from pathlib import Path

import pytest

from kp_arb.gateways.xing_res import ResError, load_res, parse_res

T1102 = """BEGIN_FUNCTION_MAP
\t.Func,주식현재가호가조회,t1102,attr,block,headtype=A;
\tBEGIN_DATA_MAP
\tt1102InBlock,기본입력,input;
\tbegin
\t\t단축코드,shcode,shcode,char,6;
\tend
\tt1102OutBlock,출력,output;
\tbegin
\t\t한글명,hname,hname,char,20;
\t\t현재가,price,price,long,8;
\tend
\tEND_DATA_MAP
END_FUNCTION_MAP
"""

T0441 = """BEGIN_FUNCTION_MAP
\t.Func,선물옵션 잔고,t0441,attr,block,headtype=A;
\tBEGIN_DATA_MAP
\tt0441InBlock,입력,input;
\tbegin
\t\t계좌번호,accno,accno,char,11;
\t\t비밀번호,passwd,passwd,char,8;
\tend
\tt0441OutBlock,출력1,output;
\tbegin
\t\t총평가손익,tdtsunik,tdtsunik,long,18;
\tend
\tt0441OutBlock1,출력2,output,occurs;
\tbegin
\t\t종목번호,expcode,expcode,char,32;
\t\t매매구분,medocd,medocd,char,1;
\t\t잔고수량,jqty,jqty,long,18;
\tend
\tEND_DATA_MAP
END_FUNCTION_MAP
"""


def test_parse_query_res_blocks_and_fields() -> None:
    spec = parse_res(T1102)
    assert spec.tr == "t1102"  # .Func 줄에서
    assert [b.name for b in spec.in_blocks] == ["t1102InBlock"]
    assert spec.blocks["t1102InBlock"].fields == ("shcode",)
    out = spec.blocks["t1102OutBlock"]
    assert not out.is_input and not out.occurs and out.fields == ("hname", "price")


def test_parse_occurs_block() -> None:
    spec = parse_res(T0441, "t0441")
    assert [b.name for b in spec.out_blocks] == ["t0441OutBlock", "t0441OutBlock1"]
    assert not spec.blocks["t0441OutBlock"].occurs
    assert spec.blocks["t0441OutBlock1"].occurs
    assert spec.blocks["t0441OutBlock1"].fields == ("expcode", "medocd", "jqty")
    assert spec.key_field() == "accno"


def test_realtime_key_field_and_errors(tmp_path: Path) -> None:
    h1 = ("BEGIN_FUNCTION_MAP\n.Func,KRX호가,H1_,attr,key=8,group=1;\nBEGIN_DATA_MAP\n"
          "H1_InBlock,입력,input;\nbegin\n단축코드,shcode,shcode,char,6;\nend\n"
          "H1_OutBlock,출력,output;\nbegin\n호가시간,hotime,hotime,char,8;\n"
          "매도호가1,offerho1,offerho1,long,8;\nend\nEND_DATA_MAP\nEND_FUNCTION_MAP\n")
    spec = parse_res(h1)
    assert spec.tr == "H1_" and spec.key_field() == "shcode"
    assert spec.blocks["H1_OutBlock"].fields == ("hotime", "offerho1")
    # 필드 길이(5번째 칸) — 실시간 키를 Res 길이에 맞춰 자르는 데 쓴다(CUR base_id 6)
    assert spec.blocks["H1_InBlock"].length_of("shcode") == 6
    assert spec.blocks["H1_OutBlock"].lengths == (8, 8)
    assert spec.blocks["H1_OutBlock"].length_of("없는필드") == 0
    with pytest.raises(ResError):
        parse_res("BEGIN_FUNCTION_MAP\nEND_FUNCTION_MAP\n")
    # 파일 로드(CP949) — 없으면 ResError
    (tmp_path / "t1102.res").write_bytes(T1102.encode("cp949"))
    assert load_res(tmp_path, "t1102").blocks["t1102OutBlock"].fields == ("hname", "price")
    with pytest.raises(ResError):
        load_res(tmp_path, "t9999")
