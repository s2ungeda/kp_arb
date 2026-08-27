# 파이썬 학습 노트 (kp-arb 소스 기반)

델파이·C++ 경험자를 위한 정리. 실제 프로젝트 코드에서 만난 문법만 담는다.

---

## 1. 파일 구조 기본

### docstring (문서 문자열)
```python
"""이 파일이 하는 일 설명."""
```
- 파일·함수 맨 위 따옴표 3개 문자열. 주석(`#`)과 달리 **언어 공식 기능** — 편집기 툴팁, `help()`로 조회 가능.
- 델파이 유닛 상단 주석과 역할은 같지만 프로그램이 읽을 수 있다는 점이 다름.

### import (다른 파일 가져오기) = 델파이 `uses` / C++ `#include`
```python
import math                              # 모듈 통째로 → math.floor(...) 로 사용
from .domain.enums import Account        # 특정 이름만 가져오기
from kp_arb.routing import account_for   # 전체 경로 (패키지 밖에서)
```
- 앞의 점(`.`) = "같은 패키지 안에서". 테스트 파일은 패키지 밖이라 전체 경로를 씀.
- **함수 안에서도 import 가능** — 무거운 모듈(tkinter)을 필요할 때만 로드하거나, 순환 참조를 피할 때 사용. `main_window.py`가 그 예.

### `from __future__ import annotations`
- 타입 표기를 **즉시 평가하지 않고 문자열로 남겨두는** 지시.
- 파이썬 3.10+ 에서는 `float | None` 같은 최신 문법이 이미 지원되므로 **대부분 불필요** — 이 프로젝트에선 관례로 붙어 있음.
- 여전히 유효한 용도: 클래스가 자기 자신을 타입으로 참조할 때(전방 참조).

---

## 2. 값과 타입

### None = 델파이 `nil` / C++ `nullptr`
```python
x = None
if x is None:        # 비교는 반드시 is (== 아님)
if x is not None:
```
- **C++과 결정적 차이**: None을 잘못 건드려도 크래시가 아니라 평범한 예외(`AttributeError`)가 남. 행 번호까지 나옴.
- 함수에 `return`이 없으면 자동으로 None 반환.
- 타입 표기에 `float | None` = "float이거나 None" (C++ `std::optional<double>`).

### `-> None` = 반환값 없음
| 파이썬 | 델파이 | C++ |
|---|---|---|
| `def f() -> None:` | `procedure f;` | `void f()` |
| `def f() -> Account:` | `function f: TAccount;` | `Account f()` |

### 파이썬 변수는 전부 참조
- `a = b`는 값 복사가 아니라 **같은 객체를 가리키는 이름이 하나 더 생기는 것**. C++로 치면 모든 변수가 `shared_ptr`.
- 메모리는 참조 카운트로 자동 관리(수동 해제 없음).

### `is` vs `==`
```
is  → 메모리상 같은 객체인가 (C++의 &a == &b)
==  → 값이 같은가 (C++의 operator== 오버로드)
```
- `is`를 쓰는 곳: **None 비교**, enum 멤버 비교(둘 다 가능하나 관례).
- `==`를 쓰는 곳: 숫자·문자열·리스트 등 값 비교, 직접 만든 클래스(pydantic 모델) 비교.
- **문자열에 `is`를 쓰면 위험** — 우연히 True가 나올 수 있지만 보장된 동작이 아님.

---

## 3. 자료구조

### dict (사전) = 델파이 `TDictionary` / C++ `std::unordered_map`
```python
_KR_ROUTING: dict[Instrument, Account] = {
    Instrument.KR_STOCK: Account.KR_STOCK,
    Instrument.KR_ETF: Account.KR_STOCK,
}
value = _KR_ROUTING[key]     # 키가 없으면 KeyError 예외
```
- `{키: 값, ...}` 리터럴 문법이 언어 기본.
- **설계 습관**: if/elif 사슬 대신 표(테이블)로 규칙을 표현 → 규칙이 늘어도 표에 한 줄만 추가.

### 튜플 = C++ `std::pair` / `std::tuple`
```python
_STOCK_BANDS = ((2_000, 1), (5_000, 5), (20_000, 10))   # 튜플의 튜플
```
- 숫자 중간 밑줄(`2_000`)은 자릿수 구분용. C++14의 `2'000`과 같음. 값은 2000.
- 구간 검색처럼 **순서가 중요한** 규칙은 dict 대신 튜플 나열을 씀.

### for 루프 + 자동 분해
```python
for limit, tick in _STOCK_BANDS:      # C++ 구조화 바인딩과 동일
    if price < limit:
        return tick
return _STOCK_TOP_TICK                 # 루프가 끝까지 돌면 여기로
```
- C++: `for (auto& [limit, tick] : bands)`

---

## 4. 예외 처리

```python
try:
    return _KR_ROUTING[instrument]
except KeyError as exc:
    raise ValueError(f"{instrument}는 국내 상품이 아님") from exc
```

| 조각 | 의미 |
|---|---|
| `except` / `as` | 예약어 |
| `KeyError` | **예외 클래스 이름** (존재해야 함). C++ `catch (std::out_of_range&)`의 타입 자리 |
| `exc` | 아무 이름이나 가능 (exception 줄임). 관례상 `e` 또는 `exc` |
| `from exc` | 원인이 된 예외를 **사슬로 매달기**. C++ `throw_with_nested`, 델파이 `RaiseOuterException` |

### 실전 규칙
- **예외 클래스를 다 외울 필요 없음.** 에러가 나면 traceback 맨 아랫줄이 이름을 알려줌.
- 델파이처럼 `except Exception as e:` 로 넓게 잡아도 됨.
- 로직 안쪽 = 구체적으로(`except KeyError`), 프로그램 경계(스레드 루프, 알림) = 넓게(`except Exception`).
- **`except BaseException`이나 맨몸 `except:`는 금지** — Ctrl-C까지 삼켜서 프로그램이 안 꺼짐.

### traceback 읽는 법
- **아래가 최신** (C++ 디버거 콜스택과 반대). 에러는 맨 아랫줄부터 읽는다.
- `from exc`가 있으면 원인 예외까지 함께 출력됨.

---

## 5. 문자열

### f-string = 델파이 `Format` / C++ `std::format`
```python
f"{instrument} is not a domestic instrument"
# → "hl_perp is not a domestic instrument"
```
- 문자열 앞에 `f`를 붙이면 `{변수}` 자리에 값이 끼워짐.

---

## 6. 조건식·연산자

### 삼항 조건식 — 어순이 C와 반대
```c
result = cond ? A : B;        // C/C++
```
```python
result = A if cond else B     # 파이썬 — 값이 먼저, 조건이 가운데
```
읽는 법: "A다, 만약 cond라면. 아니면 B다."

### 실제 사용 예 (main_window.py)
```python
_BASE_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parent.parent)
```
- 괄호는 문법이 아니라 **줄바꿈 허용용**.
- `getattr(sys, "frozen", False)` = 속성이 있으면 그 값, 없으면 False (exe로 빌드됐는지 판정).

---

## 7. 함수·데코레이터

### 함수 정의
```python
def account_for(instrument: Instrument) -> Account:
```
- 델파이 `function account_for(instrument: TInstrument): TAccount;`
- `begin`/`end` 대신 **들여쓰기**로 블록 구분.
- 인자가 많으면 한 줄에 하나씩 세로로 나열(가독성).

### self / cls — 첫 인자를 명시적으로 쓴다
| 종류 | 첫 인자 | 받는 것 | 델파이 | C++ |
|---|---|---|---|---|
| 일반 메서드 | `self` | 그 인스턴스 | `Self` | `this` |
| `@classmethod` | `cls` | 클래스 자체 | `class function` | `static` 멤버 |
| `@staticmethod` | 없음 | 아무것도 | — | `static` (this 불필요) |

- **파이썬은 `self`/`cls`를 인자 목록에 직접 써야 함** (다른 언어는 숨겨져 있음).
- 편집기 색이 다르게 보이면 `@classmethod` 유무 차이 — 클래스 메서드 vs 인스턴스 메서드.

### @property = 델파이 property
```python
@property
def krx_code(self) -> str:
    return {...}[self]

code = Underlying.SAMSUNG.krx_code    # 괄호 없이 변수처럼 접근
```
- 델파이 `property KrxCode: string read GetKrxCode;`
- C++엔 대응 문법이 없어 보통 `getKrxCode()` 메서드로 만듦.
- 저장되는 필드가 아니라 **호출될 때 계산**되는 값.

### 데코레이터 = 델파이 어트리뷰트 `[Test]`
```python
@pytest.mark.parametrize("a,b", [(1,2), (3,4)])
def test_something(a, b): ...
```
- 함수 **위에 붙이는 꼬리표**. 함수를 실행하는 게 아니라 "이렇게 다뤄라"는 정보를 매다는 것.
- 점 사슬 분해: `pytest`(모듈) → `.mark`(객체) → `.parametrize`(메서드) → `(...)`(호출).
- 원리: `@데코레이터` 는 `f = 데코레이터(f)` 와 같음 — 함수를 받아 함수를 돌려주는 함수.
- 괄호 없으면 이름 자체가 데코레이터, 괄호 있으면 호출 결과가 데코레이터.

---

## 8. 테스트 (pytest) = DUnitX / GoogleTest

```python
@pytest.mark.parametrize(
    "instrument,expected",
    [
        (Instrument.KR_STOCK, Account.KR_STOCK),
        (Instrument.KR_ETF, Account.KR_STOCK),
    ],
)
def test_account_routing(instrument, expected) -> None:
    assert account_for(instrument) == expected


def test_hl_perp_has_no_ls_account() -> None:
    with pytest.raises(ValueError):
        account_for(Instrument.HL_PERP)
```

| 요소 | 의미 | 대응 |
|---|---|---|
| 파일 `test_*.py`, 함수 `test_*` | **이름 규칙만으로 자동 수집** | DUnitX는 `[Test]` 필수 |
| `parametrize` | 표의 행 수만큼 반복 실행 (위 예는 테스트 2개) | DUnitX `[TestCase]`, GoogleTest `TEST_P` |
| `assert 조건` | 참이면 통과. 실패 시 양쪽 실제 값 자동 출력 | `CheckEquals` / `EXPECT_EQ` |
| `with pytest.raises(X):` | 블록 안에서 X 예외가 **나야** 통과 | `CheckException` / `EXPECT_THROW` |

- 데코레이터 없는 함수는 그냥 1번 실행되는 평범한 테스트.
- **주의**: 함수 이름이 `test_`로 시작하지 않으면 조용히 안 돌아감.
- 실행: `.venv/Scripts/python -m pytest tests/test_routing.py -v`

---

## 9. 읽은 소스 정리

### routing.py (18줄) — 계좌 라우팅
- dict 테이블 하나 + 함수 하나. "주식·ETF→주식계좌, 선물→선물계좌"라는 **도메인 불변식의 유일한 정의처**.
- 규칙을 한 곳에만 두면 어디서 주문을 만들든 계좌가 틀릴 수 없음.

### ticks.py (69줄) — 호가단위 계산
- **구간표 순회**: `(미만 상한, 틱)` 튜플 나열을 for로 돌며 첫 매칭에서 return.
- **부동소수점 완충치**: `math.floor(price / tick + 1e-9) * tick` — `1e-9`가 없으면 5000/5가 999.99…로 계산돼 틱 하나가 사라짐. C++/델파이의 epsilon 비교와 같은 습관.
- **반올림 방향이 곧 비즈니스 규칙**: 매수는 내림(비싸게 사면 손해), 매도는 올림(싸게 팔면 손해).
- **maker_cap**: 내 지정가가 반대편 1호가를 침범하면 즉시체결(taker)이 되므로 1틱 물러남. `best_ask is not None` 검사가 먼저 오는 이유는 시세 미수신 상태 대비.

---

## 10. 다음 학습 순서

1. `domain/enums.py` — Enum, `@property`
2. `domain/models.py` — pydantic 모델, 검증기
3. `pegging.py` / `risk.py` — dataclass, "판단만 하고 실행은 위층" 설계
4. 화면: `key_setup.py` → `fx_monitor.py` → `main_window.py` (tkinter, 델파이 VCL과 유사)
5. 마지막: `gateways/`, `bootstrap.py` — asyncio (가장 어려움)

---

## 11. Enum 심화 (enums.py)

### StrEnum — 값이 문자열인 열거형
```python
from enum import StrEnum

class Underlying(StrEnum):
    SAMSUNG = "samsung"

Underlying.SAMSUNG == "samsung"   # True — 문자열 그 자체
```
- 델파이/C++ enum은 **정수 이름표**라 부가 정보를 못 가짐. 파이썬 enum은 **진짜 클래스**이고 멤버는 그 클래스의 인스턴스 → 메서드·property를 가질 수 있음.
- 종류: `Enum`(값 자유) / `IntEnum`(정수) / `StrEnum`(문자열, 3.11+) / `Flag`(비트).
- 이 프로젝트가 StrEnum을 고른 이유: JSON 직렬화 자동, yaml 설정과 직접 비교, 로그가 깔끔.
- `for x in cls:` 로 **모든 멤버 순회** 가능 (델파이 `Low()..High()` 불필요).

### @property = 델파이 property
```python
@property
def krx_code(self) -> str: ...
code = Underlying.SAMSUNG.krx_code    # 괄호 없이 변수처럼
```

### self / cls
| 종류 | 첫 인자 | 델파이 | C++ |
|---|---|---|---|
| 일반 메서드 | `self` | `Self` | `this` |
| `@classmethod` | `cls` (클래스 자체) | `class function` | `static` |

- 파이썬은 `self`/`cls`를 **인자 목록에 직접 써야 함**.
- 편집기 색이 다르면 `@classmethod` 유무 차이.

---

## 12. pydantic 모델 (models.py)

### BaseModel의 위치
```
object          ← 파이썬의 TObject (모든 클래스의 뿌리, 자동 상속)
  └─ BaseModel  ← pydantic 제공 (델파이 TPersistent 같은 선택적 부모)
       └─ Quote
```
- 파이썬에도 생성자(`__init__`)는 있음. pydantic은 **생성자에 들어갈 반복 코드(타입검사·대입·==·JSON)를 대신 써주는 도구**.

### 필드 선언
```python
class Quote(BaseModel):
    bid: float                      # 기본값 없음 → 필수
    bid_qty: float | None = None    # 기본값 있음 → 선택
    market: str = "krx"
```

### 검증기 두 종류
```python
@field_validator("qty")        # ← "qty" = 검사할 필드 이름(문자열로 지목)
@classmethod
def _qty_positive(cls, v):     # v = 그 필드에 들어온 값
    if v <= 0: raise ValueError(...)
    return v

@model_validator(mode="after") # 모든 필드가 채워진 뒤
def _consistency(self):        # self = 완성된 객체 전체
    if self.order_type is LIMIT and self.price is None: raise ...
    return self
```

| | 언제 쓰나 | 받는 것 |
|---|---|---|
| `field_validator` | 그 필드만 보면 판단 가능 (qty > 0) | `cls, v` |
| `model_validator(after)` | **다른 필드를 함께 봐야** 함 (지정가면 price 필수) | `self` |

- **호출 시점 = 객체 생성 순간**. `Quote(...)` 괄호 안에서 자동 실행. 만든 뒤 값 읽을 땐 안 돎.
- 실행 순서: 타입검사 → field_validator들 → model_validator(after) → 완성.
- `field_validator`/`model_validator` = pydantic이 정한 이름(못 바꿈). 함수 이름(`_qty_positive`)은 자유.
- 필드명을 바꾸면 `@field_validator("...")` 문자열도 함께 바꿔야 함 — 안 바꾸면 **클래스 정의 시점에 즉시 에러**(조용히 넘어가지 않음).
- 검증기는 값을 **고칠 수도** 있음 → 계좌 자동 배정(`self.account = expected`)이 그 활용.

### 속도 (실측, 20만 번)
| | 생성 | 읽기 |
|---|---|---|
| 일반 클래스 / dataclass | 0.025초 | 0.005초 |
| pydantic | **0.154초** (6배) | 0.005초 (동일) |

- 느린 건 **생성할 때뿐**. 객체 하나당 약 0.8마이크로초 → 이 프로젝트(초당 수십 건)엔 무의미.
- 진짜 병목은 네트워크: LS REST 50~300ms, HL 액션 0.7~1.0초 (백만 배 차이).

### 언제 BaseModel, 언제 dataclass
| 상황 | 선택 |
|---|---|
| 외부에서 들어오는 데이터 (거래소 JSON, 설정 파일) | **BaseModel** — 검증 필요 |
| 내부 계산 결과만 담는 그릇 | **dataclass** — 가볍게 |

- models.py가 전부 BaseModel인 이유: **모듈 간 경계를 넘나드는 "계약 타입"**이라서.

---

## 13. 읽은 소스 추가

### domain/enums.py (72줄) — 도메인 열거형
- 프로젝트의 모든 "종류"(종목·계좌·상품·방향·주문유형·장운영단계)를 정의. 델파이 `Types.pas` 역할.
- `Underlying.krx_code` (property) — 종목코드 매핑을 enum 안에 붙여둠.
- `Underlying.from_krx_code()` (classmethod) — 역매핑, 없으면 `None`.
- `Instrument.venue` (property) — **"어느 거래소 상품인가"의 유일한 정의처**. models.py의 검증이 이걸 사용.

### domain/models.py (114줄) — 계약 타입
- `Quote`(호가) / `Position`(포지션) / `OrderIntent`(주문 의도) / `MarketState`(전략 입력) 등.
- `Position.signed_qty` — 롱 +, 숏 − 부호 규약의 **유일한 정의처**. fx.py 노출 계산이 전부 이것에 의존.
- `OrderIntent._consistency`가 강제하는 3가지:
  1. instrument와 venue 일치 (HL 상품을 LS로 주문 불가)
  2. 지정가면 price 필수
  3. **계좌 자동 배정** — 생략하면 routing이 채우고, 틀리게 적으면 거부. HL 주문은 계좌가 `None`이어야 정상(HL은 계좌 개념 없음)
- `from ..routing import account_for`가 **함수 안**에 있는 이유 = 순환 참조 회피 (models ↔ routing).
- 핵심 가치: **"잘못된 주문 객체는 애초에 존재할 수 없다"** → 받는 쪽에서 방어 코드 반복 불필요.

### 세 파일의 연결
```
enums.py    "종류" 정의 + instrument.venue 규칙
    ↓
routing.py  계좌 라우팅 규칙
    ↓
models.py   둘을 불러다 주문 생성 시점에 강제
```
