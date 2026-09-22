# Trade.xyz(HIP-3) 오라클·마크 가격 산식 — 조사 기록 (2026-09-21)

출처: Trade.xyz 공식 문서(아래 링크). **요약 도구를 거쳐 읽은 값이므로 코드에 쓰기 전 원문 대조 필수.**
DESIGN.md:131의 "개장 중 spot 참조, 마감 후 자가 가격발견"을 구체화하는 참고 자료(계약 아님).

## 1. 오라클 가격

### 외부 시장이 열려 있을 때
외부에서 산출한 공정가를 그대로 오라클로 전송. 한국 종목:

    오라클(USD) = KRW 주가 ÷ USD/KRW 환율      예) 886,000 ÷ 1,444.10 = $613.53

**환율 출처 — Pyth `FX.USD/KRW`로 추정(미확정).** 원문 직접 확인(curl, 2026-09-21) 기준 근거 등급:
- 확실: 환율은 "real-time USD/KRW FX rates"(고시환율·선물 아님). 한국 페이지 FX Conversion 절 원문 —
  "The relayer aggregates executable quotes for KRW-denominated instruments alongside real-time
  [USD/KRW](https://insights.pyth.network/price-feeds/FX.USD%2FKRW) FX rates."
- 확실: Trade.xyz는 Pyth를 실제 데이터 출처로 쓰는 회사 — XYZ100은 "Primary Datasource: …pyth…NMH6/USD"로 명시.
- **추정**: 그 USD/KRW가 Pyth 피드 값이라는 것 — 근거는 "USD/KRW" 단어에 걸린 링크 하나뿐이고,
  "Pyth를 쓴다"는 문장은 없음. 한국 주식 호가 출처는 "institutional data providers"로만 적혀 있어
  환율도 같은 기관 경로일 가능성 배제 못 함. **확정은 실측 대조로만 가능**(아래).
- 외부 주식시장이 닫힌 동안 환율을 계속 반영하는지는 문서에 없음.

프로젝트 환율(§6.1: 주간 LS CUR 현물/하나고시, 야간 환율이론가)과 출처가 달라 괴리에 환율 차이가 섞인다.
실측 방법: 괴리 CSV에 oraclePx 칸을 추가해 `내재 환율 = base_last ÷ oraclePx`(외부 가격 시간대)를
`usdkrw_used`와 비교. Pyth는 Hermes 공개 API로 같은 피드를 받을 수 있음(도입은 §6.1 변경이라 사용자 결정 사안).

### 외부 가격이 없을 때 (외부 데이터 간격 > 15초면 전환) — 내부 가격 방식

    IPD_t = max(P_impactBid − S, 0) − max(S − P_impactAsk, 0)
    x_t   = S_(t−) + IPD_t
    S_t   = β_t · S_(t−) + (1 − β_t) · x_t
    β_t   = exp(−Δt* / τ),   τ = 30분,   Δt* = min(Δt, 0.1·τ)

- S = 직전 오라클. P_impactBid/Ask = 정해진 금액(impact notional)을 호가창에서 체결시킬 때의 평균가. 깊이 부족 시 그쪽 기여 0.
- 1회 갱신의 최대 반영 비중 = 1 − e^(−0.1) ≈ 9.5%.
- 전환 시 초기값 = 마지막 외부 가격. 외부 가격 복귀 시 다음 틱에 즉시 외부 가격으로.

## 2. 한국 종목 외부 가격 시간 (GMT+9)

| 구간 | 시간 | 오라클 |
|---|---|---|
| 프리마켓 | 08:10 ~ 08:50 | 외부 |
| 정규장 | 09:01 ~ 15:30 | 외부 |
| 애프터 | 15:40 ~ 20:00 | 외부 |
| 사이 구간 | 08:50~09:00, 15:30~15:40 | 내부 |
| 야간 | 20:00 ~ 익일 08:10 (문서 표기는 08:00) | 내부 |
| 주말 | 금 20:00 ~ 월 08:00 | 내부 |

상장: SMSN(삼성전자) · SKHX(SK하이닉스) · HYUNDAI(현대차) · EWY(한국 ETF). 휴장일은 별도 문서(holiday-closures).

## 3. 마크 가격

    마크 = median( 오라클,
                   오라클 + EMA_150초(중간가 − 오라클),
                   median(최우선매수, 최우선매도, 마지막 체결가) )

- 앞 두 개는 Trade.xyz 중계기(relayer)가 전송, 세 번째는 HL 프로토콜이 계산.
- **중계기 갱신은 오라클·마크 모두 1회당 ±50bp로 제한.** 갱신 주기는 문서에 없음(실측 필요).
- 용도: 증거금, 청산, 스탑/지정가 발동, 미실현 손익.

## 4. 가격 발견 범위 (Discovery Bounds)

- 외부 시장이 닫힌 동안 마크는 `기준가 × (1 ± 1/최대레버리지)` 안. 한국 종목 10배 → ±10%.
- 발동선: `기준가 × (1 ± 범위 × oracle_threshold)` (문서 예시 threshold 90%).
- 오라클이 발동선에 닿으면 기준가가 그 경계로 옮겨가고 새 범위 설정. 상·하 방향 횟수는 독립, 종목별 상한. 소진되면 외부 가격 복귀까지 고정 상한.
- 범위 폭은 고정(옮겨갈 뿐 넓어지지 않음). 외부 가격 복귀 시 기준가=실시간, 횟수 0으로.
- 범위가 청산가를 막는 동안은 청산 불가.
- 한국 종목의 threshold·재설정 횟수는 해당 페이지에 없음.

## 5. 프로젝트 함의

1. **갭 추종 지연** — ±50bp 제한으로, 08:10·09:01에 주가가 크게 뛰면 오라클이 여러 번 갱신해야 따라감. 그 구간 HL 괴리는 차익이 아니라 추종 지연일 수 있음.
2. **환율 기준 차이** — 괴리 계산(현물환/환율이론가 환산)에는 Trade.xyz 환율(실시간 USD/KRW — Pyth로 추정, 미확정)과의 차이가 섞임. 주간은 작을 것으로 예상되나 하나고시 대체 구간·야간(선물 이론가 vs 현물)은 벌어질 수 있음 — 실측 필요.
3. **야간 오라클은 호가창의 함수** — 데드존 신규 진입 금지의 근거.
4. **사이 구간 10분**(08:50~09:00, 15:30~15:40)은 내부 방식 — 동시호가 시간대와 겹침.
5. 외부 가격이 NXT 프리·애프터까지 덮음 — 자동T 운영시간과 거의 일치.

## 출처

- https://docs.trade.xyz/perpetuals/mechanics/oracle-price.md
- https://docs.trade.xyz/perpetuals/mechanics/mark-price.md
- https://docs.trade.xyz/perpetuals/mechanics/discovery-bounds.md
- https://docs.trade.xyz/perpetuals/mechanics/external-price.md
- https://docs.trade.xyz/perpetuals/markets/stocks/korea.md
- 전체 문서 묶음: https://docs.trade.xyz/llms-full.txt (경로 개편됨 — 옛 `perp-mechanics/…` 주소는 404)
- HIP-3 본문: https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals
