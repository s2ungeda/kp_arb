# 하이퍼리퀴드(Hyperliquid) DEX 기술 조사 보고서 (2026년 9월 기준)

하이퍼리퀴드는 "거래소 하나를 위해 체인을 통째로 만든" 구조입니다. HotStuff 계열 합의(HyperBFT) 위에서 주문장·증거금·청산을 체인 상태 머신(HyperCore)이 직접 처리합니다. 핵심 차별점은 속도보다 **L1이 블록 안의 주문 순서를 강제한다**는 점입니다(취소·ALO가 GTC·IOC보다 먼저).\[1\] 차익거래·페어 트레이딩 시스템은 이 순서 규칙, 2026년 4월 도입된 우선순위 수수료(priority fee), 주소 기반 레이트 리밋을 전제로 설계해야 합니다.

## TL;DR
- **구조**: 자체 L1 하나에 합의 1개(HyperBFT)와 실행 엔진 2개(HyperCore = 온체인 현물·무기한 주문장, HyperEVM = EVM 스마트컨트랙트)가 있습니다. 공식 문서 기준 처리량은 초당 약 20만 주문이고, 병목은 합의가 아니라 실행입니다. 커밋은 보통 2블록(파이프라인), 공동 위치(co-located) 클라이언트 기준 종단 지연은 중앙값 0.2초, p99 0.9초입니다.
- **주문 처리**: 주문장은 체인 상태이고 가격-시간 우선으로 매칭됩니다. 다만 각 합의 배치 안에서 "GTC/IOC가 없는 액션 → 취소 → GTC/IOC 포함 액션" 순으로 재정렬됩니다. 그래서 같은 시점에 보낸 메이커의 취소·ALO가 테이커보다 거의 항상 먼저 실행됩니다. 2026년 4월 13일부터는 HYPE로 쓰기(IOC·ALO)와 읽기(gossip) 우선순위를 살 수 있습니다.
- **리스크**: 검증인은 24명(27명으로 확대 발표)이고, 재단 운영 노드의 스테이크 비중은 약 49%입니다. 노드는 비공개 바이너리입니다. JELLY 사건(2025년 3월)에서는 검증인 투표로 시장이 강제 정산됐고, 2025년 10월 10일에는 하이퍼리퀴드에서만 100억 달러 이상이 청산되며 2년여 만에 처음으로 ADL이 발동했습니다(insights4vc). "탈중앙 거래소"라기보다 "투명한 상태 머신을 가진 준(準)중앙화 거래소"로 보고 리스크를 관리하는 편이 정확합니다.

## Key Findings

### 1. 블록체인 기본 구조

**왜 자체 L1인가**
- 공식 문서는 하이퍼리퀴드를 "first principles로 작성·최적화된 L1"로 설명합니다. 합의 알고리즘과 네트워킹 스택을 모두 새로 만들었습니다.\[2\]
- 설계 이유의 핵심은 **주문 순서 규칙을 프로토콜 수준에서 강제하는 것**입니다. 하이퍼리퀴드 공식 블로그는 "블록을 올바르게 실행하는 유일한 방법은 취소와 post-only를 먼저 정렬하는 것"이라고 밝힙니다.\[1\] 이더리움·솔라나 같은 범용 체인이나 Arbitrum 같은 단일 시퀀서 롤업에서는 이런 거래소 전용 정렬 규칙을 체인 규칙으로 걸 수 없습니다. 블록 생산자나 시퀀서의 정렬 정책에 의존해야 합니다.
- 결과적으로 주문·취소에 가스가 없고, 외부 체인에 정산하지 않습니다. 블록 생산·실행 속도·정렬을 모두 직접 통제합니다.\[3\]

**HyperCore와 HyperEVM**
- 둘은 별도 체인이 아닙니다. 같은 HyperBFT 합의가 확정하는 하나의 L1 블록 시퀀스 안에 있는 두 실행 환경입니다.\[4\]\[5\] 공식 문서도 EVM 블록이 "Hyperliquid 실행의 일부로 생성되며 HyperBFT의 보안을 그대로 상속한다"고 적고 있습니다.\[6\]\[7\]
- HyperCore: 무기한·현물 주문장, 증거금, 펀딩, 청산, 스테이킹, 볼트.\[4\]
- HyperEVM: Cancun 기반 EVM(블롭 제외).\[4\] 2025년 2월 18일 메인넷에 출시됐습니다.\[8\]
- **읽기 경로(EVM → Core)**: 프리컴파일로 HyperCore의 포지션, 현물 잔고, 오라클 가격 등을 조회합니다.\[5\]\[9\]
- **쓰기 경로(EVM → Core)**: CoreWriter 시스템 컨트랙트(`0x3333…3333`)가 약 25,000 가스를 소모한 뒤 로그를 내보내고, HyperCore가 이 로그를 액션으로 처리합니다.\[10\] 일부 CoreWriter 액션은 올린 Core 블록에서 즉시 실행되지 않습니다. EVM이 L1 멤풀을 우회해 지연 이점을 얻지 못하게 하려는 설계입니다(Ambit Labs 분석).\[10\]\[11\]
- **자산 이동**:
  - 토큰마다 Core 쪽 시스템 주소가 있습니다. 첫 바이트가 `0x20`이고 나머지는 토큰 인덱스(빅엔디언)입니다. 예를 들어 인덱스 200이면 `0x20…00c8`입니다.\[12\]
  - HYPE만 예외로 `0x2222…2222`를 씁니다.\[12\]
  - EVM → Core: 연결된 ERC-20을 시스템 주소로 전송합니다. HYPE는 네이티브 value 전송입니다.\[11\]\[12\]\[13\]
  - Core → EVM: 다음 EVM 블록 기본 가스가 기준으로 200k 가스가 듭니다.\[12\]
  - EVM 블록이 끝나면 전송 이벤트가 먼저 반영되고, 그다음 CoreWriter 액션이 실행됩니다.\[4\]\[11\]

**HYPE 토큰의 역할**
- HyperEVM의 가스 토큰입니다. EIP-1559 구조이며 기본 수수료와 우선 수수료가 모두 소각됩니다.\[5\]\[8\]\[14\]
- 위임형 지분증명(DPoS) 스테이킹에 쓰입니다.\[15\]\[16\] 검증인은 자기 위임 10,000 HYPE를 1년간 락업해야 합니다.\[17\]
- 스테이킹량에 따라 거래 수수료가 할인됩니다(최대 40%).\[15\]\[18\]
- HIP-1/3 배포 경매 대금, 우선순위 수수료 결제에도 쓰입니다.\[19\]\[20\]
- 거래 수수료는 HLP, Assistance Fund, 배포자에게만 갑니다. Assistance Fund(`0xfefe…fefe`)는 수수료를 L1 실행 안에서 자동으로 HYPE로 바꾸고, 이 HYPE는 소각됩니다(공식 fees 문서).\[15\]\[18\]\[21\]
- 총 공급량은 10억 개입니다. 스테이킹 수익률은 약 4억 HYPE가 스테이킹된 상태 기준 연 약 2.4%로 문서화돼 있고, 재원은 신규 발행이 아니라 사전 배정된 에미션 준비금입니다(Hyperliquid Guide). Staking Rewards의 2026년 9월 실시간 수치는 2.23% APY입니다.

**브리지(Arbitrum USDC)**
- Arbitrum의 Bridge2 컨트랙트가 USDC를 보관합니다.\[22\]\[23\]\[24\]
- **출금 흐름**:
  1. 출금 요청 즉시 L1 잔고가 차감됩니다.\[25\]
  2. 검증인이 별도 트랜잭션으로 서명합니다.\[25\]
  3. 스테이크 가중 2/3 이상이 서명하면 브리지에 출금 요청이 올라갑니다.\[25\]
  4. 분쟁 기간이 시작됩니다. 이 기간에 악의적 출금이 보이면 브리지를 잠글 수 있고, 다시 풀려면 스테이크 가중 2/3이 필요합니다.\[22\]\[23\]\[25\]
  5. 분쟁 기간이 끝나면 finalizer가 USDC를 지급합니다.\[22\]\[25\]
- 출금 수수료는 1 USDC입니다.\[25\]\[26\]
- 브리지 컨트랙트는 서명자 집합을 hot validator set, cold validator set, finalizer, locker로 나눕니다. PANews 분석 당시 분쟁 기간은 200초였습니다(사용자 가이드에는 약 5분으로 표기된 곳도 있음).\[22\]\[26\]
- **보안 모델의 본질**: 브리지 보안은 곧 검증인 집합의 보안입니다.\[27\] 스테이크 2/3을 쥔 주체가 담합하면 브리지 자금 전체가 위험해집니다. 따라서 아래 검증인 집중도 문제는 브리지 리스크와 같은 문제입니다.

### 2. 블록 생성 주기와 성능

| 항목 | 수치 | 출처·성격 |
|---|---|---|
| HyperCore 처리량 | 약 20만 주문/초, 병목은 실행 | 공식 문서(HyperCore overview) |
| 종단 지연(co-located) | 중앙값 0.2초, p99 0.9초 | 공식 문서, 측정 방법·시점 비공개 |
| 취소·ALO 종단 지연 | 약 380ms | 공식 Optimizing latency 문서 |
| 커밋 | 보통 2블록(파이프라인 HyperBFT) | 공식 문서 |
| L1 블록 시간 | 약 70ms | Figment(2025년 4월), Messari. 공식 수치 아님 |
| 블록 내 합의 배치 | 보통 1~2개 | 공식 order book 문서 |
| 실측 예시 | 1,000블록(약 73초)에 주문 상태 142,955건, 북 diff 89,241건 | Bitquery 측정 |

- Bitquery 측정은 1,000블록이 약 73초로, 블록당 약 73ms입니다.\[28\] 70ms 블록 시간과 맞습니다.
- 커뮤니티 위키에는 "중앙값 0.1초, p99 0.5초"라는 수치도 돕니다.\[29\] 공식 문서의 0.2초/0.9초와 다르므로 공식 수치를 기준으로 삼으십시오.

**HyperEVM 이중 블록**
- 공식 문서의 현재값은 small(fast) 블록 1초·3M 가스, big(slow) 블록 1분·30M 가스입니다.\[30\] 초기 자료에는 2M 가스로 적힌 곳이 많습니다. 지금은 3M으로 올라갔습니다.
- 두 블록은 하나의 증가하는 EVM 블록 번호 시퀀스에 교차 배치됩니다.\[31\] 멤풀도 두 개로 나뉩니다.\[30\]
- 온체인 멤풀은 주소당 다음 8개 nonce만 받고, 1일이 지난 트랜잭션은 제거합니다.\[30\]
- big block을 쓰려면 HyperCore 사용자 액션 `{"type":"evmUserModify","usingBigBlocks":true}`로 플래그를 켭니다. 이 플래그는 계정 단위라서 다시 꺼야 small block으로 돌아갑니다.\[30\]\[31\]
- 설계 이유: 블록 "속도"와 "크기"를 분리해 빠른 확인과 큰 배포를 동시에 개선하려는 것입니다.\[30\]

### 3. 합의 알고리즘 (HyperBFT)

- **동작 원리**: HotStuff와 그 후속 연구에서 영감을 받은 리더 기반 BFT입니다. 공식 문서 표현은 "HotStuff의 변형"입니다.\[2\]\[32\]\[33\] 스테이크의 1/3 미만이 비잔틴이어도 안전성을 유지합니다. 파이프라이닝으로 앞 블록 확정을 기다리는 동안 다음 블록을 진행하고, 커밋은 보통 2블록 뒤에 일어납니다. 확정된 블록은 재구성(reorg)되지 않습니다.\[34\] 블록 생산 기회는 스테이크에 비례합니다.\[33\]
- **문서화 수준**: 공식 사양서나 논문은 없습니다. 커뮤니티 위키조차 "HotStuff에 크게 영감을 받았다는 것 외에 공식 문서가 없다"고 적습니다.\[29\] 외부 검증이 불가능한 블랙박스라는 점을 인지해야 합니다.
- **검증인 수와 선정**:
  - 등록은 무허가입니다. 스테이크 상위 24개가 활성 집합이 됩니다(공식 문서).\[35\]
  - 2026년 5월 19일 24개에서 27개로 확대가 발표됐습니다.\[36\]
  - 변천: 재단 5개 → 제네시스 16개 → 2025년 4월 21개(무허가 등록 전환) → 24개 → 27개.\[37\]\[38\]
  - 활성 집합에 들어가는 실질 문턱은 100만 HYPE 이상이었습니다(2025년 8월 기준 24위가 약 1,018,436 HYPE).\[37\]
  - 자기 위임이 10k HYPE 아래로 떨어지면 해당 검증인은 undelegate-only 모드가 됩니다.\[17\] 언스테이킹 대기열은 7일입니다.
- **탈중앙화 논란**:
  - JELLY 사건(2025년 3월) 당시 재단이 16개 중 5개 검증인, 전체 스테이크의 약 78.5%를 통제했다는 분석이 있습니다(커뮤니티 X 분석, 2차 출처).\[39\]
  - 2026년 6월 재위임 후 재단 운영 노드 비중은 약 49.3%, 나머지 약 50.7%는 22개 독립 운영자입니다(crypto.news).\[36\]
  - `hyperliquid-dex/node` 저장소에는 노드 소스가 없습니다. GPG 서명된 바이너리(hl-visor)를 받는 Dockerfile뿐입니다.\[39\]
  - 합의 오작동에 대한 스테이크 슬래싱은 없고, 대신 jail(배제)이 있습니다. 2026년 6월에는 "검증인이 한 건물에 몰려 있고, 코드가 닫혀 있고, 재단이 jail과 강제 업그레이드를 할 수 있다"는 공개 비판도 나왔습니다.\[36\]\[40\]
  - 해석: 재단 스테이크가 1/3을 넘으면 사실상 거부권, 2/3에 가까우면 사실상 통제권입니다. 49%는 거부권 수준입니다.

### 4. DEX 주문 처리 방식 (핵심)

**4-1. 주문장이 체인 상태로 유지되는 방식**
- HyperCore 상태에는 자산마다 주문장이 하나씩 있습니다. 가격은 tick size의 정수배, 수량은 lot size의 정수배여야 하고, **가격-시간 우선**으로 매칭합니다.\[41\]\[42\]
- 오프체인 주문장은 없습니다.\[33\]\[43\] 모든 검증인과 비검증 노드가 같은 결정론적 상태 머신을 실행해 동일한 주문장을 재현합니다.
- 무기한 주문장 연산은 클리어링하우스를 참조합니다. 증거금 검사는 **신규 주문 시점**에 한 번, **매칭 시 resting 쪽**에 다시 한 번 합니다. 주문을 걸어 둔 뒤 오라클 가격이 변해도 증거금 일관성이 유지되도록 하기 위해서입니다.\[41\]
- 노드는 L4 수준의 개별 주문 diff(`raw_book_diff`)를 파일로 출력합니다. 이를 이용해 로컬에서 주문장을 재구성할 수 있습니다(`order_book_server` 예제, 현재 유지보수 중단).\[44\]\[45\]

**4-2. 주문 흐름 (단계별)**
1. **서명**: 클라이언트가 액션(주문·취소 배치)을 msgpack으로 직렬화합니다. 여기에 nonce(8바이트), vault 주소 플래그, 선택적 `expiresAfter`를 붙여 keccak 해시(connectionId)를 만듭니다. 이 해시로 phantom agent `{source: "a"(메인넷)/"b"(테스트넷), connectionId}`를 구성하고, EIP-712 도메인(`name: "Exchange"`, `chainId: 1337`, `version: "1"`, verifyingContract 0 주소)으로 서명합니다.\[46\]
2. **전송**: `POST https://api.hyperliquid.xyz/exchange`로 보냅니다.\[47\]
3. **API 서버 → 노드**: 공식 문서상 API 서버는 노드의 업데이트를 받아 상태를 로컬에 유지하고, 사용자 트랜잭션을 연결된 노드로 전달합니다. 노드는 이를 HyperBFT의 일부로 gossip합니다.\[48\]
4. **멤풀·블록 포함**: 멤풀과 합의 로직은 주문장 트랜잭션을 **의미적으로 인식**합니다.\[41\] 제안자가 트랜잭션을 모아 블록을 제안합니다.
5. **블록 내 정렬**: 각 합의 배치 안에서 아래 순서로 정렬합니다(4-3 참조).
6. **실행·매칭**: 정렬된 순서로 주문장에 적용하고 가격-시간 우선으로 체결합니다. ALO가 즉시 체결될 상황이면 거절합니다. 체결 시 수수료(볼륨 등급, 스테이킹, 추천, 빌더 코드)가 계산되고 클리어링하우스 포지션이 갱신됩니다.\[49\]
7. **커밋·응답**: 블록이 커밋되면(보통 2블록) API 서버가 L1 실행 결과를 원래 요청에 응답합니다.\[44\]\[48\] 증거금·잔고 변동은 그 블록 상태로 즉시 결제됩니다. 별도 청산(clearing) 단계는 없습니다.

**4-3. 블록 안 정렬 규칙 — 설계 이유와 수치**
- 공식 order book 문서가 정한 배치 내 순서는 다음과 같습니다.
  1. GTC·IOC 주문을 보내지 않는 액션(ALO 전용 배치 등)\[41\]
  2. 취소
  3. GTC 또는 IOC를 하나라도 포함한 액션\[41\]
- 같은 범주 안에서는 제안자가 제안한 순서를 따릅니다. modify는 새로 거는 주문 유형으로 분류합니다.\[41\]
- 공식 Optimizing latency 문서는 "시각 t에 보낸 취소와 ALO는 같은 시각 t에 보낸 IOC·GTC보다 거의 항상 먼저 실행되며, 이 우선순위는 여러 블록에 걸친다"고 설명합니다. 또 "전송 시각 차이가 10ms 미만인 실험에서도 L1 순서를 예측할 수 있다"고 적습니다.\[44\]
- **설계 이유**: 일반 주문장에서는 메이커가 취소를 보냈는데 조금 더 빠른 테이커가 먼저 체결해 버리는 "pick-off"가 흔합니다. 하이퍼리퀴드는 이를 구조적으로 막아 메이커의 역선택 비용을 낮추고, 스프레드를 좁히고, 변동성 구간에서도 유동성을 유지하려 합니다. 공식 블로그는 "HFT 메이커와 HFT 테이커 간 거래 비중이 타 플랫폼 대비 최소 10배 낮다"고 주장합니다(자체 주장).\[1\]
- **실무 함의**: 페어 트레이딩이나 차익거래의 테이커 레그는 같은 순간 메이커 취소보다 뒤에 처리된다고 가정해야 합니다. "보이는 호가를 IOC로 친다"의 체결 확률이 CEX보다 낮고, 특히 급변 구간에서 더 그렇습니다.

**4-4. 우선순위 수수료 (2026년 4월 13일 메인넷, 알파)**
- **쓰기 우선순위 (IOC)**:
  - 주문에 `grouping: {"p": N}`를 붙이면 요율 N/1e8이 적용됩니다. 체결 명목가 기준으로, 미위임 스테이킹 잔고의 HYPE에서 빠져나가 소각됩니다.\[45\]
  - 0~8bp 구간에서는 지연 감소가 선형입니다. 실측 효과는 **1bp당 약 45ms 감소**입니다.\[45\]
  - 8bp~100% 구간은 시간 선호가 같고, 70ms 유닉스 시간 버킷 안에서만 수수료 내림차순으로 동점을 깹니다.\[45\]
  - 내부 모델은 `effective_time = arrival_time + f(action, fee)`이고, 취소에는 f=0이 적용됩니다. 즉 **어떤 우선순위 수수료를 내도 취소는 모든 즉시 체결성 주문보다 앞섭니다**.\[45\]
  - 출시 당시 상한은 20bp에서 8bp로 낮아졌고, HIP-3 자산의 IOC에만 적용됐습니다. 현재 문서는 비(非)outcome 자산의 IOC 전용 또는 non-reduce-only ALO 전용 주문 액션을 지원합니다.\[45\]\[50\]
- **ALO 우선순위**:
  - T=400ms 창 안에서 같은 가격 레벨 큐의 꼬리를 우선순위 요율 내림차순으로 정렬합니다.\[45\]
  - 수수료는 체결 여부와 관계없이 주문 시점에 걸린 명목가 기준으로 부과됩니다.\[45\]
  - 멤풀 처리 순서는 바꾸지 않고, 큐 위치만 바꿉니다.\[45\]
  - 테스트넷에서 먼저 도입됐다는 보고가 있으므로 메인넷 적용 범위는 문서로 확인하십시오.\[51\]
- **읽기 우선순위 (gossip)**:
  - 3분 주기 더치 경매가 2개 있습니다. 경매는 직전 낙찰가의 10배에서 시작하고 최소가는 0.1 HYPE입니다. 현물 잔고에서 소각됩니다.\[45\]
  - 효과는 슬롯당 약 25ms입니다.\[45\]
  - 온체인에 등록한 IP가 피어가 보는 IP와 정확히 같아야 효과가 있습니다.\[45\]
  - 재단 비검증 노드가 이 순서를 존중합니다.\[45\]
- **규모**: 출시부터 2026년 7월 23일까지 우선순위 수수료 수입은 507만 달러였습니다(쓰기 283만, 읽기 224만). 일평균 참여자는 쓰기 약 194명, 읽기 약 3.5명입니다(Messari). HIP-3 시장이 최근 30일 쓰기 수입의 61%를 차지했습니다.\[52\]\[53\]
- **해석**: 하이퍼리퀴드는 "지연 경쟁"을 인프라 경쟁에서 **가격이 매겨진 경매**로 바꾸고 있습니다. 소수 참여자가 이미 순서를 사고 있으므로, 우선순위 수수료 없이 경쟁하는 테이커 전략은 구조적으로 불리해졌다고 봐야 합니다.

**4-5. 주문 유형, 서명, nonce**
- **TIF**: GTC, IOC, ALO(post-only. 즉시 체결될 상황이면 취소).\[54\]
- **시장가**: IOC 공격적 지정가로 구현하고 슬리피지 한도를 둡니다.\[55\]
- **트리거**: TP/SL은 마크 가격 기준으로 발동하며, 트리거되면 시장가로 나갑니다.\[55\] stop-limit, 트레일링 스톱도 있습니다.\[55\]\[56\]
- **그 외**: reduce-only, Scale 주문이 있습니다. TWAP는 30초 간격 하위 주문으로 나가며, 하위 주문당 최대 슬리피지는 3%입니다.\[55\] Chase는 최우선 호가를 따라가는 ALO입니다.\[43\]\[56\]
- **cloid**: 128비트 hex 클라이언트 주문 ID를 붙일 수 있습니다.\[57\]
- **최소 주문가·modify**: 최소 주문가는 $10입니다.\[58\] modify는 새 주문 유형 기준으로 정렬됩니다.
- **서명 방식 두 가지**:
  - L1 액션(주문·취소 등): 위의 phantom agent 방식, chainId 1337.\[46\]
  - 사용자 서명 액션(출금, USDC 전송, ApproveAgent 등): 도메인 `HyperliquidSignTransaction`. `signatureChainId`는 지갑이 서명에 쓰는 체인(예: Arbitrum `0xa4b1`)이고, `hyperliquidChain`("Mainnet"/"Testnet")이 재전송을 막습니다.\[46\]
  - 공식 문서가 꼽는 흔한 실수: msgpack 필드 순서, 숫자 뒤의 0, 주소 대문자, 로컬 recover가 맞으니 서명도 맞다고 믿는 것.\[59\]
- **nonce**:
  - 서명자별로 **가장 큰 nonce 100개**를 저장합니다. 새 nonce는 그 집합의 최소값보다 커야 하고 재사용할 수 없으며, (T−2일, T+1일) 범위 안이어야 합니다.\[60\]
  - 순차 nonce가 아니라서 순서가 뒤바뀐 도착을 허용합니다. 대신 동시성 관리 책임은 클라이언트에 있습니다.\[61\]
- **API(에이전트) 지갑**:
  - 서명만 하고 자금을 보관하지 않습니다.\[62\]
  - 계정당 이름 없는 지갑 1개와 이름 있는 지갑 최대 3개를 쓸 수 있고, 서브계정마다 2개가 추가됩니다. 만료는 최대 180일입니다.\[57\]
  - 등록 해제된 지갑 주소는 nonce 상태가 정리(pruning)될 수 있으므로 재사용하지 마십시오.\[60\]
  - 조회할 때는 에이전트 주소가 아니라 실제 계정 주소를 넣어야 합니다.\[60\]
- **공식 권장 구조**:
  - 트레이딩 프로세스마다 API 지갑을 하나씩 둡니다.\[60\]
  - 0.1초마다 주문과 취소를 배치로 묶어 보냅니다. **IOC·GTC 배치와 ALO 배치는 분리합니다**(ALO 전용 배치가 우선 처리되기 때문).\[60\]
  - 배치마다 원자 카운터로 고유 nonce를 발급합니다.\[60\]

**4-6. 마크·오라클 가격, 펀딩**
- **오라클 가격**:
  - 각 검증인이 CEX 현물 가격의 가중 중앙값을 계산하고, 최종값은 검증인 제출값의 스테이크 가중 중앙값입니다.\[63\]
  - 약 3초마다 갱신되며, 하이퍼리퀴드 자체 시장 데이터는 쓰지 않습니다.\[64\]
  - 용도는 펀딩 계산입니다.\[64\]
- **마크 가격**은 다음 세 값의 중앙값입니다.
  1. 오라클 + (HL 중간가 − 오라클)의 150초 EMA
  2. HL 최우선 매수·최우선 매도·최근 체결가의 중앙값
  3. Binance·OKX·Bybit·Gate·MEXC 무기한 중간가의 가중 중앙값(가중치 3:2:2:1:1)
  - 셋 중 두 개만 있으면 (2)의 30초 EMA를 추가합니다.
  - 용도: 증거금, 청산, TP/SL 트리거, 미실현 손익.\[55\]\[65\]\[66\]
- **펀딩**:
  - F = 평균 프리미엄 P + clamp(이자율 − P, −0.05%, +0.05%)로 계산한 8시간 요율을 **매시간 1/8씩** 지급합니다.\[67\]
  - 이자율은 8시간당 0.01%(시간당 0.00125%, 숏이 받는 쪽, 연 약 11.6%)입니다.\[67\]
  - 프리미엄은 5초마다 샘플링해 1시간 평균을 냅니다.\[67\]
  - 상한은 시간당 4%로, CEX보다 훨씬 느슨합니다.\[68\]
  - 명목가 환산에는 마크가 아니라 오라클 가격을 씁니다.\[69\]
  - HIP-3는 impact bid/ask 중간값 기반의 더 민감한 프리미엄 공식을 씁니다.\[67\]
- **CEX와 펀딩 차익 시 주의**: 하이퍼리퀴드는 1시간, 대부분의 CEX는 8시간 주기입니다. 비교하려면 HL 시간당 값에 8을 곱해야 합니다.\[70\] 정산 시점이 다르니 포지션 진입·청산 타이밍도 조정하십시오.

**4-7. 증거금, 청산, HLP**
- **교차·격리**: 교차 마진은 계정 전체 담보를 공유하고, 격리 마진은 포지션별로 분리합니다.\[71\] 교차 포지션의 청산가는 설정 레버리지와 무관합니다. 레버리지가 낮으면 담보를 더 쓸 뿐입니다. 유지증거금은 최대 레버리지에서의 초기증거금의 절반입니다.\[72\]\[73\]
- **청산 절차**:
  1. 계정 자산(미실현 포함)이 유지증거금 아래로 떨어지면, 먼저 주문장에 시장가 주문을 보내 포지션을 닫습니다.\[74\]
  2. 커뮤니티 위키에 따르면 $100k 초과 포지션은 20%씩 부분 청산하고 30초 쿨다운을 둡니다.\[75\]
  3. 시장 청산에 성공하면 남은 담보는 사용자에게 돌아갑니다. 청산 수수료는 없습니다.\[76\]
  4. 자산이 유지증거금의 2/3 아래로 떨어지면 **백스톱 청산**이 일어납니다. HLP의 하위 전략인 liquidator vault가 포지션과 증거금을 인수하고, 유지증거금은 돌려주지 않습니다.\[65\] 교차 계정이면 모든 교차 포지션이 넘어갑니다.\[77\]
  5. 그래도 손실을 흡수하지 못하면 ADL(자동 디레버리징)로 수익 포지션을 강제 축소합니다.\[65\]\[75\]\[78\]
- **HLP**: 커뮤니티 볼트이며, 마켓메이킹과 청산 인수 전략을 돌립니다.\[78\] 청산 손익이 거래소 운영자가 아니라 예치자에게 갑니다. 대신 방향성 손실도 예치자가 떠안습니다(JELLY 사례).\[72\]\[79\]

**4-8. MEV·프론트러닝, 지연, API와 노드의 관계**
- **MEV 대응**: 공개 멤풀의 샌드위치 공격 구조가 아닙니다. L1 정렬 규칙(취소 우선)과 스테이크 비례 리더 순환이 기본 방어선입니다. 다만 **같은 범주 안의 순서는 제안자가 정합니다**.\[41\] 이론적으로 제안자 재량이 남아 있습니다. 2026년부터는 우선순위 수수료로 순서를 "공식적으로" 파는 모델로 옮겨 갔습니다. 무질서한 MEV를 가격 매겨진 경매로 흡수하는 방식입니다.
- **API는 누가 운영하나**:
  - 공개 엔드포인트 `api.hyperliquid.xyz`의 운영 주체를 문서가 명시하지는 않습니다. 사실상 팀·재단 측 인프라로 보이지만 확인되지 않았습니다.
  - 구조는 명확합니다. API 서버는 노드를 따라가며 상태를 복제하는 **읽기 캐시 겸 트랜잭션 중계기**이고, 검증인이 아닙니다.\[48\]
  - 재단은 지연이 낮은 비검증 노드를 운영합니다. 우선 접근 조건은 10,000 HYPE 스테이킹, 메이커 리베이트 Tier 1 이상, 98% 가동률입니다.\[80\]
- **지연 최적화(공식 권장)**:
  - 재단 비검증 노드를 피어로 삼아 자체 비검증 노드를 운영합니다. 사양은 32 논리 코어, 128GB RAM, 500MB/s 디스크 이상입니다.\[44\]
  - `--disable-output-file-buffering` 옵션을 켭니다.\[44\]
  - 주문장을 로컬에서 재구성합니다.\[44\]
  - `split_client_blocks: true`를 쓰면 입력 읽기가 70~150ms 빨라집니다. 대신 실행 결과는 포함되지 않습니다.\[44\]
  - 취소에는 fast 플래그를 붙입니다. 단, 트리거 주문은 fast 취소로 취소할 수 없습니다.\[44\]
- **레이트 리밋**:
  - IP당 REST 가중치는 분당 1,200입니다. exchange 요청 가중치는 1 + floor(배치 길이/40)입니다.\[81\]
  - 주소 기반 한도는 누적 거래 1 USDC당 요청 1건이고, 초기 버퍼는 10,000건입니다. 한도에 걸리면 10초에 1건만 허용됩니다. 취소 한도는 min(limit+100000, limit×2)입니다.\[81\]
  - 배치로 n건을 보내면 IP 기준으로는 1건, 주소 기준으로는 n건으로 셉니다.\[81\]
  - 미체결 주문 한도는 기본 1,000건이고, 거래량 500만 USDC마다 1건씩 늘어 최대 5,000건입니다.\[81\]
  - 혼잡 시에는 전일 메이커 점유율의 2배까지만 블록 공간을 쓸 수 있습니다.\[81\]
  - 요청 1건당 0.0005 USDC를 내고 여유분을 예약할 수 있습니다.\[57\]
  - 공식 문서상 **DMM 프로그램, 특별 리베이트, 지연 우대는 없습니다**.\[82\]

**4-9. 수수료 (공식 fees 문서, 14일 가중 거래량 = 무기한 + 2×현물, 매일 UTC 기준 산정)**

| 등급 | 14일 가중 거래량 | 무기한 테이커/메이커 | 현물 테이커/메이커 |
|---|---|---|---|
| 0 | — | 0.045% / 0.015% | 0.070% / 0.040% |
| 1 | >$5M | 0.040% / 0.012% | 0.060% / 0.030% |
| 2 | >$25M | 0.035% / 0.008% | 0.050% / 0.020% |
| 3 | >$100M | 0.030% / 0.004% | 0.040% / 0.010% |
| 4 | >$500M | 0.028% / 0% | 0.035% / 0% |
| 5 | >$2B | 0.026% / 0% | 0.030% / 0% |
| 6 | >$7B | 0.024% / 0% | 0.025% / 0% |

- **메이커 리베이트**: 14일 메이커 점유율 >0.5%이면 −0.001%, >1.5%이면 −0.002%, >3.0%이면 −0.003%입니다.\[18\]
- **스테이킹 할인**: >10 HYPE 5%, >100 10%, >1k 15%, >10k 20%, >100k 30%, >500k 40%. 등급별 기본 수수료에 곱해지며, Diamond이면 Tier 0 테이커가 0.027%가 됩니다.\[18\]
- **계정 합산**: 서브계정 거래량은 마스터 계정에 합산되고 등급을 공유합니다. 볼트 거래량은 따로 셉니다.\[18\]
- **특수 할인**:
  - 스테이블-스테이블 현물 쌍은 테이커 수수료가 80% 낮습니다.\[18\]
  - Aligned quote asset은 테이커 20% 인하, 메이커 리베이트 50% 우대입니다.\[18\]
  - HIP-3 growth mode는 수수료가 90% 이상 인하됩니다(테이커 0.0045~0.009%).\[18\]
- **스테이킹 링크 경고**: 스테이킹 계정과 트레이딩 계정을 링크하면 스테이킹 계정이 트레이딩 계정 자금을 일방적으로 가져갈 수 있고, 링크는 해제할 수 없습니다. 반드시 같은 소유자일 때만 쓰십시오.\[18\]

### 5. 상장 방식과 HIP

- **HIP-1 (네이티브 토큰 표준)**:
  - 스마트컨트랙트가 아니라 L1 네이티브 자산입니다.\[83\]
  - 31시간 더치 경매로 배포 권한을 얻습니다. 가격은 직전 낙찰가의 2배에서 시작해 하한 500 HYPE까지 선형으로 내려갑니다. 2025년 5월 22일부터 HYPE로 지불합니다.\[83\]\[84\]
  - 배포하면 USDC 현물 주문장이 자동으로 생깁니다. ERC-20과 연결해 EVM에서도 쓸 수 있습니다.\[84\]\[85\]
  - 2026년 8월 12일 기준 485개 토큰, 324개 현물 쌍이 있습니다(Hyperliquid Guide 집계).\[83\]
- **HIP-2 (Hyperliquidity)**: 프로토콜 내장 자동 호가 전략입니다. 주문장에 3초마다 갱신되는 0.3% 간격의 사다리 호가를 겁니다. AMM 대신 CLOB 위에서 초기 유동성을 제공하는 방식입니다.\[84\]\[86\]
- **무기한 상장**:
  - 검증인 운영 무기한은 검증인 투표로 상장·상장폐지합니다.
  - Hyperps는 외부 현물 오라클 없이 자체 마크의 이동평균을 기준으로 펀딩하는 사전 상장 무기한입니다.\[71\]\[87\]
- **HIP-3 (빌더 배포 무기한)**:
  - 2025년 10월 13일 메인넷에 출시됐습니다.\[88\]
  - 500k HYPE를 스테이킹하면 독자적인 perp DEX를 배포할 수 있습니다. 오라클, 레버리지 한도, 정산을 배포자가 운영합니다.\[88\]\[89\]\[90\]
  - 첫 3개 자산은 경매가 면제되고, 추가 자산은 공유 31시간 경매로 받습니다.\[90\]
  - 악의적으로 운영하면 검증인이 스테이크 가중 투표로 슬래싱합니다(7일 언스테이킹 대기 중에도 가능).\[90\] 슬래싱된 HYPE는 소각됩니다.\[90\]
  - 배포자는 수수료의 최대 50%를 가질 수 있습니다.\[18\]\[91\]
  - 주식·원자재 등 RWA가 주류이고, trade.xyz가 HIP-3 미결제약정의 대부분을 차지합니다.\[92\]\[93\]
  - HIP-3 미결제약정은 2026년 6월 초 30억 달러를 넘었습니다(OAK Research).\[94\]
- **HIP-4 (Outcome 시장)**:
  - 2026년 2월 2일 발표, 5월 2일 메인넷에 출시됐습니다. 첫 시장은 BTC 일간 바이너리입니다.\[88\]\[95\]
  - 완전 담보, 0/1 정산 구조이고,\[95\] 오픈 시에는 수수료를 받지 않고 청산·정산 시에만 받습니다.\[18\]
  - 무허가 배포 요건 스테이크는 초기 보도에서 100만 HYPE였습니다. 2026년 7월 예비 사양은 500k HYPE와 6개월 락업입니다.\[91\]\[96\] 출처마다 다르니 최신 문서로 확인하십시오.

### 6. 주요 사고와 리스크

- **2025년 3월 12일 ETH 고래 청산**: Lookonchain 기준 160,234 ETH(약 3억 685만 달러) 규모 포지션을 청산하는 과정에서 HLP가 손실을 봤고, 하이퍼리퀴드 공식 성명은 "HLP lost ~$4M over the past 24h"라고 밝혔습니다(CoinDesk, 2025-03-12). 이후 최대 레버리지가 BTC 40배, ETH 25배로 낮아졌습니다. CoinDesk는 포지션을 113,000 ETH, 2억 달러 이상으로 보도해 출처마다 수치가 다릅니다.
- **2025년 3월 26일 JELLY 사건**:
  - 공격자는 저유동성 밈코인 JELLY로 대형 숏을 연 뒤, 외부 현물을 사들이며 가격을 올려 숏을 백스톱 청산으로 HLP에 떠넘겼습니다.\[97\]
  - HLP 미실현 손실은 최대 1,350만 달러에 달했습니다(CoinDesk).\[97\]
  - 검증인들이 약 2분 만에 만장일치로 상장폐지를 의결했고, $0.0095에 정산했습니다. 공격자 측이 오라클에 반영시키던 가격은 약 $0.50이었습니다.\[97\]\[98\]
  - 결과적으로 HLP는 392M JELLY를 $0.0095(약 372만 달러)에 정산해 손실 대신 70.3만 달러 이익으로 마감했습니다(Lookonchain).
  - 후속 조치: 재단이 롱 보유자를 보상했고, liquidator vault 규모를 HLP의 일부로 제한했으며, ADL 트리거를 개선했습니다.\[65\]\[98\]
  - **교훈**: 극단 상황에서는 "코드가 아니라 검증인 투표"가 결과를 정합니다. 롱·숏 어느 쪽이든 오라클 오버라이드로 정산가가 바뀔 수 있습니다.\[99\]
- **2025년 10월 10일 플래시 크래시**:
  - CoinGlass 집계 기준 시장 전체에서 24시간 동안 160만 명 넘는 트레이더의 레버리지 포지션 193.7억 달러가 청산됐고(CNBC, 2025-10-22), 하이퍼리퀴드에서만 100억 달러 이상이 청산됐습니다(투명하게 공개된 탓도 있음). CoinDesk(2025-10-11)는 하이퍼리퀴드 트레이더 자본이 12.3억 달러 넘게 사라졌다고 보도했습니다.
  - 2년여 만에 처음으로 교차 ADL이 발동했습니다.\[78\]\[100\]
  - Tarun Chitra의 arXiv 논문(2512.01112)은 "12분 동안 ADL로 21억 달러 포지션이 청산됐다"고 분석합니다. 같은 논문은 하이퍼리퀴드의 실제 ADL이 최적 정책 대비 약 28배 과다 사용됐고, 승자에게 약 6.53억 달러의 불필요한 헤어컷을 부과했다고 추정합니다.\[101\] 후속 논문(2602.15182)은 과잉분을 4,500만~5,170만 달러로 다시 추정합니다.\[102\] 두 수치는 모형 가정에 따라 크게 다릅니다.
  - **페어 트레이딩 함의**: 헤지 레그(숏)가 ADL로 강제 청산되면 다른 거래소의 롱이 헤지 없이 노출됩니다.\[100\]
- **구조적 리스크 요약**:
  1. 노드 코드 비공개와 공식 합의 사양 부재
  2. 재단 스테이크 약 49%(거부권 수준)
  3. 브리지 보안 = 검증인 2/3
  4. HLP 방향성 손실
  5. 저유동성 자산·HIP-3 오라클 조작 가능성\[91\]\[92\]
  6. 네트워크 업그레이드 중 post-only 구간(이 구간에는 TWAP 하위 주문도 체결되지 않음)\[56\]

## Recommendations (차익거래·페어 트레이딩 개발자용)

1. **인프라**: 레이턴시가 중요하면 공개 API 대신 자체 비검증 노드와 로컬 주문장 재구성을 기본으로 삼으십시오. 도쿄 등 검증인 근처 리전에 두는 것이 유리합니다. `split_client_blocks`로 입력 스트림을 70~150ms 먼저 받고, 계정 상태를 추정해 실행 결과를 예측하는 방식이 공식 권장입니다.\[44\]
2. **테이커 레그 설계**: 취소가 우선 처리되므로 "보이는 호가"의 체결 확률을 낮게 잡으십시오. 급변 구간에서는 IOC 체결률과 슬리피지를 백테스트가 아니라 실측으로 추정해야 합니다. 우선순위 수수료의 손익분기는 "1bp당 약 45ms 단축"과 기대 엣지를 비교해 판단하십시오. 수수료는 미위임 스테이킹 잔고에서 빠지므로 HYPE를 따로 확보해 둬야 합니다.\[45\]
3. **메이커 레그 설계**: HL 쪽 레그를 ALO로 두고 CEX 쪽을 테이커로 치는 구성이 구조적으로 유리합니다. ALO와 취소는 우선 처리되고, 메이커 수수료가 낮으며 리베이트도 있습니다. ALO 배치는 IOC·GTC 배치와 분리하십시오.
4. **주문 엔진 구현(C++ 관점)**:
   - 트레이딩 프로세스·서브계정마다 API 지갑을 분리합니다.
   - 0.1초 배치 태스크와 원자 카운터로 nonce를 발급합니다(ms 타임스탬프 기반).
   - msgpack 필드 순서와 숫자 정규화(뒤쪽 0 제거)는 공식 Python SDK와 바이트 단위로 대조 테스트하십시오.
   - cloid로 멱등성을 확보합니다.
   - 주소 기반 레이트 리밋(누적 1 USDC당 1건)을 토큰 버킷으로 모델링하고, 한도가 부족하면 예약 구매를 검토하십시오.
5. **리스크 관리**:
   - 트리거와 청산은 마크 가격 기준입니다. CEX의 last price와 다르니 헤지 비율과 스톱을 마크 기준으로 계산하십시오.
   - 펀딩 주기(1시간 대 8시간)와 시간당 4% 상한을 반영하십시오.
   - ADL과 검증인 투표 정산(JELLY형) 시나리오에서 한쪽 레그가 사라지는 경우를 대비해 자동 헤지 해제 로직을 두십시오.
   - 저유동성 알트나 HIP-3 자산은 오라클 조작 리스크를 감안해 포지션 한도를 낮게 잡으십시오.
   - 교차 마진 계정 하나에 모든 페어를 몰지 말고, 격리 마진이나 서브계정으로 백스톱 청산 전파를 차단하십시오.
6. **수수료 최적화**: 현물 거래량은 2배로 인정되고, 서브계정 거래량은 합산됩니다. 스테이킹 링크는 동일 소유 계정끼리만 쓰십시오.

## Caveats
- 블록 시간 약 70ms는 공식 수치가 아닙니다(Figment, Messari 등 3자 자료). 공식 문서의 지연 수치(0.2초/0.9초, 380ms)는 측정 방법과 시점이 공개되지 않은 자체 주장입니다.
- HyperBFT에는 공개 사양이 없고 노드는 비공개 바이너리라서, 합의 동작에 대한 설명 상당 부분은 HotStuff에서 유추한 것입니다.
- HyperEVM small block 가스 한도(2M에서 3M으로), HIP-4 스테이크(100만에서 50만 HYPE로), 브리지 분쟁 기간(200초 대 약 5분) 등은 시점과 출처에 따라 값이 다릅니다. 여기서는 최신 공식 문서값을 우선했습니다.
- 재단 스테이크 비중(49.3%), JELLY 당시 78.5% 같은 수치는 언론·커뮤니티 분석이라 온체인에서 직접 검증하기를 권합니다.
- 우선순위 수수료는 "알파" 기능이라 파라미터(8bp 상한, 400ms 창, 적용 자산 범위)가 계속 바뀌고 있습니다. 실전 투입 전에 공식 priority fees 문서를 다시 확인하십시오.
- 우선순위 수수료로 연 5,400만~1억 4,800만 달러 수입이 가능하다는 Messari 전망은 추정치이며 실현된 수치가 아닙니다.\[52\]

## 출처

1. [Latency and transaction ordering on Hyperliquid | by Hyperliquid | Medium](https://hyperliquid.medium.com/latency-and-transaction-ordering-on-hyperliquid-cf28df3648eb)
2. [About Hyperliquid | Hyperliquid Docs](https://hyperliquid.gitbook.io/hyperliquid-docs)
3. [Hyperliquid Architecture Explained: HyperBFT, HyperCore, ...](https://www.zealynx.io/research/protocol-deep-dives/Understanding-Hyperliquid-Architecture-HyperBFT-HyperCore-HyperEVM-Part1)
4. [HyperEVM - Hyperliquid Wiki - GitBook](https://hyperliquid-co.gitbook.io/wiki/architecture/hyperevm)
5. [Hyperliquid HyperEVM Explained: Ethereum Compatible Blockchain](https://www.coingabbar.com/en/hyperliquid-hyperevm-explained-ethereum-compatible-blockchain)
6. [HyperEVM - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperevm)
7. [Hyperliquid Architecture Explained: Security & Decentralization - OneKey Blog](https://onekey.so/blog/ecosystem/hyperliquid-architecture-explained-security-decentralization-19137c/)
8. [Stackedmarkets](https://stackedmarkets.com/blog/what-is-hyperevm-on-chain-traders)
9. [Hyperliquid: HyperEVM precompiles, CoreWriter, and HyperCore bridge | Chainstack Blog](https://chainstack.com/hyperliquid-hyperevm-precompiles-corewriter-bridge/)
10. [Interacting with HyperCore | Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/interacting-with-hypercore)
11. [Demystifying the Hyperliquid Precompiles and CoreWriter | by Ambit Labs | Medium](https://medium.com/@ambitlabs/demystifying-the-hyperliquid-precompiles-and-corewriter-ef4507eb17ef)
12. [HyperCore \<\> HyperEVM transfers - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/hypercore-less-than-greater-than-hyperevm-transfers)
13. [How to Build a HyperEVM Indexer on Hyperliquid | Envio](https://docs.envio.dev/blog/index-hyperevm-data)
14. [How Hyperliquid Works: Architecture, Order Book, HyperEVM](https://rocknblock.io/blog/how-does-hyperliquid-work-a-technical-deep-dive)
15. [Hyperliquid HYPE ETF: Buyback, Staking Yield and Institutional Access (2026) - AMINA Bank](https://aminagroup.com/research/hyperliquid-hype-etf-buyback-staking-yield-institutional-access-2026/)
16. [How Hyperliquid's tokenomics work and why HYPE holders care](https://nexo.com/blog/hyperliquid-tokenomics-explained)
17. [Staking - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/staking)
18. [Fees | Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
19. [HIP-1: Native token standard - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-1-native-token-standard)
20. [What are Hyperliquid Improvement Proposals (HIPs)? - HypeRPC](https://hyperpc.app/blog/hyperliquid-improvement-proposals)
21. [What Is Hyperliquid? HYPE Fees And Buybacks Guide 2026](https://icoannouncement.io/what-is-hyperliquid-hype-fees-and-buybacks-guide-2026)
22. [As the hype recedes, a technical analysis of Hyperliquid’s bridge contract, HyperEVM, and its potential problems | PANews](https://www.panewslab.com/en/articledetails/l3o76fqd.html)
23. [Hyperliquid Technology Interpretation: Bridge Contract, HyperEVM and Potential Issues - LianPR](https://www.lianpr.com/en/news/detail/40064)
24. [Hyperliquid Arbitrum Bridge | Support - Eco](https://eco.com/support/en/articles/15082533-hyperliquid-arbitrum-bridge)
25. [Hyperliquid Bridge: How to Bridge USDC to Hyperliquid](https://cryptopotato.com/hyperliquid-bridge-how-to-bridge-usdc-to-hyperliquid/)
26. [How to Withdraw From Hyperliquid: USDC Off-Ramp Guide 2026 | Support](https://eco.com/support/en/articles/15191996-how-to-withdraw-from-hyperliquid-usdc-off-ramp-guide-2026)
27. [Hyperliquid's Native Bridge: Why Validator-Secured Bridging Eliminates Third-Party Risk](https://www.chainupad.com/blog/hyperliquid-native-bridge-validator-security-gold-standard/)
28. [Hyperliquid Order Book – Historical Data | Bitquery](https://bitquery.io/datastore/datasets/hyperliquid-order-book)
29. [HyperBFT | Hyperliquid Wiki - GitBook](https://hyperliquid-co.gitbook.io/wiki/architecture/hyperbft)
30. [Dual-block architecture - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/dual-block-architecture)
31. [A Guide to HyperEVM's Dual Block Architecture - HypeRPC](https://hyperpc.app/blog/hyperevm-dual-block-architecture)
32. [What is Hyperliquid? | Cube Exchange](https://www.cube.exchange/what-is/hyperliquid)
33. [Overview | Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/overview)
34. [HyperBFT | Blockchain Security Glossary | Zealynx](https://www.zealynx.io/glossary/hyperbft)
35. [Running a validator | Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/validators/running-a-validator)
36. [Who actually runs Hyperliquid? The governance audit](https://crypto.news/who-actually-runs-hyperliquid-the-governance-audit/)
37. [HL HUB (Community) on X: "The current Hyperliquid validator set consists of 24 validators, as shown in the image. To enter the validator set, around 1,018,436 HYPE tokens are required (calculated based on the lowest stake of the 24th validator, Meria). At present, the validator run by Team Enigma has https://t.co/EXHqYIn5PY" / X](https://x.com/Hyperliquid_Hub/status/1962086650923905282)
38. [Hyper Foundation on X: "The validator set has been updated to 21 permissionless nodes. Anyone can register a validator, and the 21 largest by stake form the active set. Delegations from the Delegation Program are expected to go live in the coming days. Technical details for validators: Validators" / X](https://x.com/HyperFND/status/1914520069897670826)
39. [Vadim (AI, ⋈) on X: "Always wondered how Hyperliquid validators actually work. Did a small research dive. Here's what I learned. The repo at \`hyperliquid-dex/node\` contains no node source code. It's a Dockerfile that downloads a GPG-signed binary (hl-visor). Community reverse engineering exists, b… / X](https://x.com/zacodil/status/2056373750925242590)
40. [Hyperliquid to Increase Validators Amid Transparency and Security Discussions | Bitget News](https://www.bitget.com/news/detail/12560605417512)
41. <https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/order-book>
42. [Hyperliquid Order Book Model Explained for Crypto Traders](https://www.coingabbar.com/en/hyperliquid-order-book-model-explained-for-crypto-traders-guide)
43. [Hyperliquid On-Chain Order Book. Hyperliquid implements a fully on-chain… | by Jung-Hua Liu | Medium](https://medium.com/@gwrx2005/hyperliquid-on-chain-order-book-6df27cbce416)
44. <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/optimizing-latency>
45. <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/priority-fees>
46. <https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/utils/signing.py>
47. [hyperliquid — Trading | CryptoSkills](https://cryptoskills.dev/skills/hyperliquid)
48. <https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/api-servers>
49. [Yaugourt.hl on X: "Before you can understand priority fees, you need to understand how a transaction flows through Hyperliquid. Most people skip this. Don't. The full path from "click Buy" to "position open" thanks to @androolloyd reverse work: 1. Sign You sign an order via the API. EIP-712 signa… / X](https://x.com/Yaugourt/status/2045371807666954422)
50. [The Hyperliquid mainnet has launched a "priority fee mechanism," allowing users to pay HYPE to prioritize data reading or order writing | WEEX Crypto News](https://www.weex.com/news/detail/the-hyperliquid-mainnet-has-launched-a-priority-fee-mechanism-allowing-users-to-pay-hype-to-prioritize-data-reading-or-order-writing-648157)
51. [shaunda devens on X: "Hyperliquid has updated its documentation: priority fees, previously limited to aggressive IOC orders, now extend to ALO orders on testnet, allowing makers to bid for queue position. Expect both write and read priority fees to continue rising. With both sides of the book now… / X](https://x.com/shaundadevens/status/2074929360226910647)
52. [Hyperliquid Priority Fees: Internalizing High Frequency Trading Revenue | Messari by Blockworks](https://messari.io/report/hyper-liquid-priority-fees-internalizing-high-frequency-trading-revenue)
53. [Hyperliquid Priority Fees: Internalizing HFT... | 0xArchive](https://0xarchive.io/resources/research/hyperliquid-priority-fees-internalizing-hft-revenue)
54. [Exchange endpoint | Hyperliquid Docs](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/exchange-endpoint)
55. [Hyperliquid Order Types 2026: Market, Limit, Stop, TWAP Explained | Support](https://eco.com/support/en/articles/15247718-hyperliquid-order-types-2026-market-limit-stop-twap-explained)
56. [Order types | Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types)
57. [Exchange endpoint | Hyperliquid Docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
58. [Error responses | Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses)
59. <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing>
60. <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets>
61. [Hyperliquid Agent Wallets and Nonce State Machine | Chainstack Blog](https://chainstack.com/hyperliquid-agent-wallets-nonce-state-machine/)
62. [Agent wallets - Privy Docs](https://docs.privy.io/recipes/hyperliquid/agents-and-subaccounts)
63. [Unit Protocol: The Asset Tokenization Layer on Hyperliquid](https://newsletter.asxn.xyz/p/unit-protocol)
64. [Robust price indices - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices)
65. [Hyperliquid Liquidations Explained: Margin Calls and Insurance Fund | Support](https://eco.com/support/en/articles/15247705-hyperliquid-liquidations-explained-margin-calls-and-insurance-fund)
66. [Mark and oracle price - HyperOdd Documentation](https://docs.hyperodd.com/trading/price)
67. [Funding - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
68. [Hyperliquid Funding Rates: Hourly Settlement, Mechanics & How to Farm Them (2026) | perp.wiki](https://perp.wiki/learn/hyperliquid-funding-rates-guide)
69. [Understanding Hyperliquid Funding Rates: A Trader's Guide - OneKey Blog](https://onekey.so/blog/ecosystem/understanding-hyperliquid-funding-rates-a-traders-guide-49a5c9/)
70. [Hyperliquid Funding Rate: How It Works, Track, Profit | Support](https://eco.com/support/en/articles/15082536-hyperliquid-funding-rate-how-it-works-track-profit)
71. [Order Book | Hyperliquid Wiki](https://hyperliquid-co.gitbook.io/wiki/architecture/hypercore/dex/order-book)
72. [Liquidations - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)
73. [Hyperliquid Liquidation | Udit Samani](https://uditsamani.com/hype-liquidation/)
74. [Hyperliquid Liquidations Data Explained | SonarX](https://sonarx.com/blog/hyperliquid-liquidations-data)
75. [Liquidations - Hyperliquid Wiki - GitBook](https://hyperliquid-co.gitbook.io/wiki/architecture/hypercore/dex/clearinghouse/liquidations)
76. [Hyperliquid Liquidations Explained: How to Read the Heatmap and Avoid Cascades](https://coinmarketman.com/blog/hyperliquid-liquidations-explained--en/)
77. [Hyperliquid Liquidation Calculator (Cross & Isolated) - Otomato](https://otomato.xyz/tools/hyperliquid-liquidation-calculator)
78. [After October's 'Liquidation Day' Collapse, ADL Are The 3 Most Important Letters In Crypto\<!-- --\> | ZeroHedge](https://www.zerohedge.com/crypto/after-octobers-liquidation-day-collapse-adl-are-3-most-important-letters-crypto)
79. [Hyperliquid](https://www.bit.com/insights/knowledge-hub/hyperliquid)
80. [Foundation non-validating node - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/foundation-non-validating-node)
81. <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits>
82. [Market making | Hyperliquid Docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/market-making)
83. [HIP-1 Explained: Hyperliquid's Native Token Standard | Hyperliquid Guide](https://hyperliquidguide.com/ecosystem/hip-1-native-token-standard)
84. [Spot Deployments (HIP-1/HIP-2) | Hyperliquid Wiki](https://hyperliquid-co.gitbook.io/wiki/architecture/hypercore/hips/spot-deployments-hip-1-hip-2)
85. [Spot Deployments (HIP-1/HIP-2) - Community Docs - GitBook](https://hyperliquid-co.gitbook.io/community-docs/technology-breakdown/hypercore/hips/spot-deployments-hip-1-hip-2)
86. [xulian.hl on X: "Understanding HIP-1: Hyperliquid spot deployments HIP-1 is the native token standard on the Hyperliquid L1; it is similar to the ERC-20 standard for EVMs. Once the HyperEVM is live, the spot deployer can link their native spot asset to a corresponding ERC-20 contract deployed to https://t.co/GYs8udszGZ" / X](https://x.com/xulian_hl/status/1813397274237546499)
87. [Hyperps - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/hyperps)
88. [Hyperliquid HIP-3 and HIP-4 Explained: Perps to Predictions](https://crypto.news/hyperliquid-hip-3-and-hip-4-explained/)
89. [Hyperliquid Diligence Report](https://messari.io/research/deep-research-reports/hyperliquid-diligence-report-fdf9486f-d978-4a6f-980e-ccadc697b120)
90. [HIP-3: Builder-deployed perpetuals - Hyperliquid Docs - GitBook](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals)
91. [HIP-3: Builder-Deployed Perpetuals Explained - Permissionless Perp DEXs on Hyperliquid | Hyperliquid Guide](https://hyperliquidguide.com/ecosystem/hip-3-builder-codes)
92. [Hyperliquid’s HIP-3 & HIP-4: Tokenized Stocks and Prediction Markets | CoinGecko](https://www.coingecko.com/learn/hyperliquid-hip3-hip4-tokenized-stocks-and-prediction-markets)
93. [What Is HIP-3? Hyperliquid's Permissionless Markets ...](https://nansen.ai/post/what-is-hip-3-hyperliquid)
94. [What is Hyperliquid's HIP-3? How it works and use cases | OAK Research](https://oakresearch.io/en/analyses/innovations/what-is-hyperliquid-hip-3-how-it-works-and-use-cases)
95. [Hyperliquid Launches HIP-4 and Targets Polymarket With Zero-Fee Outcome Markets – Bitcoin News](https://news.bitcoin.com/hyperliquid-launches-hip-4-and-targets-polymarket-with-zero-fee-outcome-markets/)
96. [Hyperliquid's New HIP-4 Rule Could Lock \$30M Worth of HYPE Per Market](https://www.cryptotimes.io/2026/07/20/hyperliquids-new-hip-4-rule-could-lock-30m-worth-of-hype-per-market/)
97. [HyperLiquid Delists JELLY After Vault Squeezed in \$13M Tussle](https://www.coindesk.com/markets/2025/03/26/hyperliquid-delists-jellyjelly-after-vault-squeezed-in-usd13m-tussle)
98. [2025-26-03 | Hyperliquid Wiki](https://hyperliquid-co.gitbook.io/community-docs/introduction/roadmap/2025-26-03_incident)
99. [Hyperliquid and the JELLY attack: Context, vulnerability and team solution | OAK Research](https://oakresearch.io/en/analyses/investigations/hyperliquid-jelly-attack-context-vulnerability-team-solution)
100. [Inside the \$19B Flash Crash - insights4vc](https://insights4vc.substack.com/p/inside-the-19b-flash-crash)
101. [Autodeleveraging: Impossibilities and Optimization](https://arxiv.org/html/2512.01112v2)
102. [Autodeleveraging as Online Learning](https://arxiv.org/html/2602.15182v1)
