# authTrade — 사계절 퀀트 엔진 구조 및 매매 원칙 요약
> 교차 검증용 문서. 최종 업데이트: 2026-06-16 (Paper Trading 준비 반영)

---

## 1. 전체 아키텍처

```
[main.py — asyncio.gather 비동기 다중 태스크]
  ├─ run_exit_loop()         30초 — 전 버킷 포지션 청산 체크
  ├─ run_monitor_loop()      60초 — 킬스위치 + VIX RoC + 리밸런싱
  ├─ run_bucket1_loop()      60분 — 가치주 장기
  ├─ run_bucket2_loop()      15분 — ETF 스윙 or B2 동적 배분
  ├─ run_bucket3_stream()    이벤트 — WebSocket(Alpaca) / 1초 폴링(Toss)
  └─ strategy_mgr.run_premarket_loop()  30초 체크 — 매일 9:20 ET 스캔

[3대 관심사 분리 모듈]
  ├─ MarketRegimeAnalyzer  시장 상태 진단 (RegimeEngine + ConfidenceScanner + NewsAnalyzer)
  ├─ StrategyManager       전략 전환 엔진 (B3/B2 모드 스위칭 + B2 리밸런싱)
  └─ AccountManager        계좌 자금 관리 (A/B 로테이션 + Settled Cash 재확인)

[사계절 레짐 이중 구조]
  외부 레짐 (RegimeEngine — 매일 9:20 ET)
    신뢰도 ≥70점 후보 ≥5개 → B3_AGGRESSIVE (급등주 단타)
    신뢰도 ≥70점 후보  <5개 → B2_SWING     (지수 ETF 스윙)
  내부 레짐 (B2AllocationEngine — 15분마다)
    QQQ+SPY 모두 > MA20  → BULL_LEVERAGE (TQQQ/SOXL/FNGU/LABU Top2 × 50%)
    하나 이하 > MA20     → DEFENSE_INDEX (QQQ/SPY 모멘텀 강한 1개 × 100%)
    모두 ≤ MA20          → CASH          (전량 청산)

[뉴스 심리 보정 파이프라인]
  yfinance/Alpaca 뉴스 → 긴급 키워드 차단 → Gemini Flash 심리 점수(-1~+1)
  최종 신뢰도 = 차트 점수 × 0.7 + 뉴스 점수(0~100환산) × 0.3

[DB 이중 구조]
  storage/db.py          → PositionDB (포지션/거래/손익 — 기존)
  storage/db_manager.py  → trades/market_log/system_state (사계절 엔진 전용)

[브로커]
  BROKER=alpaca (기본): Alpaca Markets API + WebSocket
  BROKER=toss:          토스증권 Open API + 1초 폴링
```

---

## 2. B3 급등주 모드 (B3_AGGRESSIVE)

**진입 조건 (AND)**
- 신뢰도 스코어 ≥ 70점 (RVOL 30pt + Alpha vs QQQ 40pt + VWAP 30pt)
- 뉴스 보정 후 최종 점수: 차트×0.7 + 뉴스×0.3
- 긴급 키워드(유상증자/소송/실적악화 등) 미감지
- A/B 그룹 오늘 순번일 것 (홀수 날 A, 짝수 날 B — T+1 프리라이딩 방지)
- 스푸핑 블랙리스트 미등록
- 레짐 B3_AGGRESSIVE & 동기화 대기(60s) 완료

**자금 배분**
| 점수 | 배분 |
|------|------|
| ≥ 90점 | 버킷 전액 |
| 70~89점 | 버킷 절반 |
| < 70점 | 진입 금지 |
- 켈리 스케일: 최근 5회 승률 ≥60% → 예산 ×1.2

**B3 청산 전략 (4-레이어 ExitStrategyEngine)**
| 우선순위 | 트리거 | 동작 |
|---------|--------|------|
| L1 | 본절가 트랩: 고점 +10% 후 진입가 이탈 | 즉시 SELL |
| L2 | 오더플로우 매도압력 ≥1.5x (100봉) | 즉시 SELL |
| L3 | ATR 가변 트레일링 스탑 | 수익 <50%: ATR×3, ≥50%: ATR×1.5 |
| 보조 | 개미털기 방어: 저거래량(avg×0.5) ATR 이탈 | 60초 대기 후 판단 |

**3분 룰**
- 진입 3~8분 내 PnL ≤ 0% → 보유 수량 50% 즉시 매도

---

## 3. B2 지수/ETF 스윙 모드 (B2_SWING)

**B2 유니버스**
- 레버리지: TQQQ, SOXL, FNGU, LABU
- 방어: QQQ, SPY

**내부 레짐별 전략**

| 내부 레짐 | 조건 | 포트폴리오 |
|----------|------|-----------|
| BULL_LEVERAGE | QQQ + SPY 모두 > MA20 | ATR조정수익률 Top2 × 50% |
| DEFENSE_INDEX | 하나라도 ≤ MA20 | 5일 모멘텀 강한 QQQ 또는 SPY × 100% |
| CASH | QQQ + SPY 모두 ≤ MA20 | 전량 청산, 현금 대기 |

- ATR 조정 수익률 = 5일 수익률 / ATR14 (리스크 1단위당 수익 기준 순위)
- 방어 모드 청산: 주봉 MA20(20주) 이탈 시

---

## 4. Cash Account A/B 로테이션 (ACCOUNT_TYPE=cash)

```
홀수 날 → Group A 활성, Group B 결제 대기
짝수 날 → Group B 활성, Group A 결제 대기

진입 가능 자금 = min(총자산/2, Settled Cash)
레짐 전환 시 → AccountManager.on_mode_switch() → broker.get_settled_cash() 재확인
```

**T+1 안전 장치**
- Settled Cash = `non_marginable_buying_power` (미결제 자금 제외)
- 모드 전환 시마다 재조회하여 미결제 현금 진입 원천 차단

---

## 5. 뉴스 심리 분석 (NewsAnalyzer)

**수집 소스** (우선순위)
1. Alpaca News API (최근 1시간)
2. yfinance Ticker.news (fallback)

**긴급 차단 키워드**
`유상증자 / 소송 / 실적 악화 / 상장폐지 / 회계 부정 / 횡령 / secondary offering / class action / SEC investigation / bankruptcy` 등

**Gemini Flash 심리 분석**
- 입력: 뉴스 제목 최대 10개
- 출력: -1.0(매우 부정) ~ 1.0(매우 긍정)
- 실패 시: 내장 Bullish/Bearish 키워드 사전으로 폴백

**신뢰도 통합**
```
최종 = int(차트점수 × 0.7 + 뉴스점수(0~100환산) × 0.3)
```

**DB 기록**: 모든 뉴스 점수는 `market_log` 테이블에 `regime="NEWS:NVDA:+0.73:5:"` 형식으로 저장

---

## 6. DB 구조

### storage/db.py — PositionDB (포지션 저널)
```sql
positions  (symbol PK, strategy, entry_price, peak_price, qty, sector, status)
trades     (id, symbol, side, qty, price, strategy, reason, ts)
daily_pnl  (date PK, realized, unrealized)
```

### storage/db_manager.py — 사계절 엔진 전용
```sql
trades       (id, symbol, buy_price, sell_price, quantity, mode, result, timestamp)
market_log   (id, date, nasdaq_ma20, regime, scanner_score, timestamp)
system_state (key PK, value)
```

**system_state 주요 키**
| 키 | 예시 값 |
|----|---------|
| CURRENT_MODE | B3_AGGRESSIVE |
| ACTIVE_GROUP | A |
| B2_ALLOC_MODE | BULL_LEVERAGE |

---

## 7. 리스크 관리

### 계좌 레벨
| 규칙 | 수치 | 동작 |
|------|------|------|
| 일 손실 한도 | -2% | 신규 진입 차단 |
| VIX 절대값 | ≥ 30 | 신규 진입 차단 |
| VIX 변화율 | 전일 대비 +20% | 선제 진입 차단 + 텔레그램 경고 |
| 데드존 | 11:30~13:00 ET | 신규 진입 차단 |
| 장 마감 전 | 15분 전 | 인트라데이 포지션 강제 청산 |
| Panic 레짐 | VIX > 30 | B3 즉시 청산 + B2 인버스 ETF 헤지(SQQQ/SDS) |

### 포지션 레벨
- ATR 사이징: 거래당 계좌 1% 리스크, 손절거리 = ATR×2
- 섹터 집중도: 동일 섹터 최대 3개
- 청산 로직이 진입 로직보다 항상 먼저 실행

---

## 8. Paper Trading 모드

### 슬리피지 시뮬레이션

| 구분 | Paper | Live |
|------|-------|------|
| 매수가 | `last × 1.001` (+0.1%) | `ask × 1.002` (+0.2%) |
| 매도가 (일반) | `price × 0.999` (-0.1%) | `price × 0.997` (-0.3%) |
| 매도가 (손절/긴급) | `price × 0.999` (-0.1%) | `price × 0.995` (-0.5%) |

판별: `os.getenv("MODE", "paper") == "paper"` → `_is_paper()` 메서드

### 예수금 실시간 확인

```python
# on_bar() — B3 진입 직전 (async)
await account_mgr.refresh_settled_cash(broker)
budget = account_mgr.capital_for("squeeze", conf.total)

# _b2_alloc_cycle() — B2 사이클 상단 (sync)
settled = broker.get_settled_cash()
bucket_capital.update_settled_cash(settled)
if settled <= 0:
    return  # 매매 차단
```

### 실시간 DB 기록 (`storage/db_manager.py`)

```python
# 매수 진입
dbm.save_trade(symbol, buy_price=last, sell_price=None, quantity=qty, mode="B3|88pt", result=None)

# 청산
dbm.save_trade(symbol, buy_price=entry, sell_price=price, quantity=qty,
               mode="squeeze|trailing_stop", result=round(pnl_pct, 2))
```

### 시작 검증 (`main.py`)

```python
dbm.init_db()   # storage/db/trading_data.db 자동 생성
# MODE=paper + ALPACA_BASE_URL=실전 엔드포인트 → AssertionError 조기 차단
```

### Telegram 알림 형식

```
📈 [B3/PAPER] NVDA 매수 10주 @ $875.50
📉 [SQUEEZE/PAPER] NVDA 청산  매도 사유: trailing_stop  청산가: $891.20  PnL: +$157.00 (+1.8%)
```

---

## 10. 텔레그램 명령어

| 명령어 | 기능 |
|--------|------|
| `/status` | 현재 모드·그룹·오늘 성적·보유 종목 요약 |
| `/set_mode [B3/B2]` | 레짐 강제 전환 (다음 프리마켓 스캔 전까지 유지) |
| `/positions` | 포지션 + 진입가 + 고점 |
| `/account` | 계좌 잔고 |
| `/journal` | 일일 매매 일지 |
| `/weekly` | 주간 분석 |
| `/stats [N]` | 최근 N일 누계 통계 |
| `/scan` | 급등/저평가 종목 스캔 |
| `/ask` | Gemini AI에게 질문 |
| `/buy / /sell` | 수동 주문 |

---

## 11. 데이터 흐름

```
Alpaca Markets API   → 실시간 가격/거래량/뉴스
yfinance             → 펀더멘털(PER/PBR/ROE/DCF), 일봉/주봉 OHLCV
Alpaca News API      → 뉴스 수집 (yfinance fallback)
Gemini Flash         → 뉴스 심리 분석, 종목 빠른 분석, 텔레그램 메시지 생성
Gemini Pro           → 전략 수정, 포트폴리오 리밸런싱, 큰손실 분석, 레짐전환 심층분석
SQLite (PositionDB)  → 포지션/거래 저널
SQLite (db_manager)  → 사계절 엔진 trades/market_log/system_state
Telegram Bot         → 실시간 알림 + 수동 제어
```

---

## 12. 핵심 파일 목록

| 경로 | 역할 |
|------|------|
| `core/MarketRegimeAnalyzer.py` | 시장 상태 진단 (프리마켓 스캔 + 뉴스 보정) |
| `core/StrategyManager.py` | 전략 전환 엔진 (B3/B2 + sync lock) |
| `core/AccountManager.py` | 계좌 자금 관리 (A/B + Settled Cash) |
| `core/orchestrator.py` | 비동기 태스크 조율 |
| `core/regime_engine.py` | B3/B2 모드 감지 + 전환 |
| `strategy/news_analyzer.py` | 뉴스 수집 + Gemini 심리 분석 + 차단 |
| `strategy/confidence_scanner.py` | RVOL/Alpha/VWAP 100점 스코어러 |
| `strategy/exit_strategy.py` | 4-레이어 청산 엔진 (개미털기 방어) |
| `strategy/b2_allocation.py` | B2 내부 동적 배분 (BULL/DEFENSE/CASH) |
| `strategy/strategy_engine.py` | 통합 인터페이스 (진입/청산 DB 자동 저장) |
| `storage/db_manager.py` | trades/market_log/system_state CRUD |
| `storage/db.py` | PositionDB (포지션 저널) |
| `main.py` | 진입점 + asyncio.gather |
