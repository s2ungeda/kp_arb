# kp-arb

하이퍼리퀴드 HIP-3(Trade.xyz) perp ↔ 국내 종목(LS증권) 차익거래 **인프라 스켈레톤 + 하네스**.

이 저장소는 코드 에이전트(Claude Code)가 안전하게 작업하도록 만든 **하네스 베이스라인**이다.
구체 전략 로직은 아직 미정이며(`DESIGN.md` §6), 인프라/계약/검증 루프가 먼저 깔려 있다.

## 무엇이 들어 있나
- `DESIGN.md` — 설계 계약(단일 진실).
- `CLAUDE.md` — Claude Code 작업 규칙(계약 준수 + 검증 루프).
- `kp_arb/` — 도메인 모델, 순수 로직(라우팅·세션·스프레드·FX), 게이트웨이/전략 계약, 목 구현.
- `tests/` — 계약 단위 테스트(계좌 라우팅·세션맵·스프레드·OrderIntent·FX 노출·목 게이트웨이).
- `.claude/hooks/check.sh` — ruff + mypy + pytest 검증 루프.
- `.claude/settings.json` — 편집 후 검증을 자동 실행하는 PostToolUse 훅 예시.

## 시작
```bash
pip install -e ".[dev]"
./.claude/hooks/check.sh
```
모두 green이면 baseline이 정상이다. 여기서부터 Claude Code로 게이트웨이 실제 구현·전략을 채운다.

## 채워야 할 것 (다음 단계)
- `gateways/`의 LS/HL 실제 구현 (목을 라이브로 교체, 리플레이 픽스처는 녹화본 사용).
- `session.py`를 LS 장운영데이터(JIF + 휴장일) 실데이터에 연결.
- `strategy/`에 실제 전략 구현(인터페이스는 고정).
- DESIGN.md §13의 `[OPEN]` 항목 확정(야간선물 커버리지, #2 노출 발행 인터페이스, 계좌 상품코드 등).
