"""xingAPI Res 파일 파서 — TR별 InBlock/OutBlock 필드 정의 (순수 로직, DESIGN-ls-xing.md §2).

Res 파일(xingAPI 패키지 ``Res\\<TR>.res``, CP949 텍스트)은 TR의 입력·출력 블록과 필드를 정의한다::

    BEGIN_FUNCTION_MAP
        .Func,주식현재가호가조회,t1102,attr,block,headtype=A;
        BEGIN_DATA_MAP
        t1102InBlock,기본입력,input;
        begin
            단축코드,shcode,shcode,char,6;
        end
        t1102OutBlock,출력,output;
        begin
            한글명,hname,hname,char,20;
        end
        END_DATA_MAP
    END_FUNCTION_MAP

블록 줄 = ``이름,설명,input|output[,occurs];``, 필드 줄 = ``설명,이름,이름,형,길이;``.
우리가 쓰는 것은 블록 이름·입력/출력·반복(occurs) 여부·필드 이름 순서뿐이다 — XAQuery의
SetFieldData/GetFieldData가 필드 **이름**으로 동작하고, occurs 블록만 GetBlockCount로 행 수를
센다. 형식이 낯설면(줄 모양이 다름) 그 줄은 건너뛰고, 블록이 하나도 없으면 ``ResError``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ResError(ValueError):
    """Res 파일을 읽을 수 없음(없음·블록 없음)."""


@dataclass(frozen=True)
class ResBlock:
    name: str
    is_input: bool
    occurs: bool
    fields: tuple[str, ...]
    lengths: tuple[int, ...] = ()  # 필드 길이(Res 5번째 칸, 모르면 0) — 실시간 키 자르기용

    def length_of(self, field_name: str) -> int:
        """필드 길이(없거나 모르면 0)."""
        try:
            return self.lengths[self.fields.index(field_name)]
        except (ValueError, IndexError):
            return 0


@dataclass(frozen=True)
class ResSpec:
    tr: str
    blocks: dict[str, ResBlock] = field(default_factory=dict)

    @property
    def in_blocks(self) -> list[ResBlock]:
        return [b for b in self.blocks.values() if b.is_input]

    @property
    def out_blocks(self) -> list[ResBlock]:
        return [b for b in self.blocks.values() if not b.is_input]

    def key_field(self) -> str | None:
        """실시간 TR의 구독 키 필드 — 첫 InBlock의 첫 필드(H1_ shcode, JIF jangubun 등).
        없으면 None."""
        for block in self.in_blocks:
            if block.fields:
                return block.fields[0]
        return None


def parse_res(text: str, tr: str = "") -> ResSpec:
    """Res 본문 → ResSpec. 순수."""
    blocks: dict[str, ResBlock] = {}
    current: tuple[str, bool, bool] | None = None
    fields: list[str] = []
    lengths: list[int] = []
    in_fields = False
    for raw in text.splitlines():
        line = raw.strip().rstrip(";").strip()
        if not line or line.startswith(("BEGIN_", "END_", ".Func")):
            if line.startswith(".Func") and not tr:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    tr = parts[2]
            continue
        if line == "begin":
            in_fields = True
            fields, lengths = [], []
            continue
        if line == "end":
            if current is not None:
                name, is_input, occurs = current
                blocks[name] = ResBlock(name, is_input, occurs, tuple(fields), tuple(lengths))
            current = None
            in_fields = False
            continue
        parts = [p.strip() for p in line.split(",")]
        if in_fields:
            if len(parts) >= 4 and parts[1]:
                fields.append(parts[1])
                try:
                    lengths.append(int(float(parts[4])) if len(parts) > 4 else 0)
                except ValueError:
                    lengths.append(0)
            continue
        if len(parts) >= 3 and parts[2] in ("input", "output"):
            occurs = any(p == "occurs" for p in parts[3:])
            current = (parts[0], parts[2] == "input", occurs)
    if not blocks:
        raise ResError(f"Res 블록 없음: {tr or '?'}")
    return ResSpec(tr=tr, blocks=blocks)


def load_res(res_dir: Path | str, tr: str) -> ResSpec:
    """``<res_dir>/<tr>.res``를 CP949로 읽어 파싱. 없으면 ResError."""
    path = Path(res_dir) / f"{tr}.res"
    if not path.is_file():
        raise ResError(f"Res 파일 없음: {path}")
    return parse_res(path.read_text(encoding="cp949", errors="replace"), tr)
