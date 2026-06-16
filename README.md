# authTrade — 사계절 퀀트 엔진

미국 주식 자동매매 시스템.  
Alpaca Markets API · Google Gemini AI · Telegram 양방향 제어.  
시장 상황에 따라 **B3 급등주 단타 ↔ B2 지수/ETF 스윙**을 자동 전환하는 레짐 스위칭 아키텍처.

---

## 아키텍처

```
main.py — asyncio.gather() 비동기 다중 태스크
  ├─ run_exit_loop()                   30초  — 전 버킷 청산 감시
  ├─ run_monitor_loop()                60초  — 킬스위치 + VIX + 리밸런싱
  ├─ run_bucket1_loop()                60분  — B1 가치주
  ├─ run_bucket2_loop()                15분  — B2 ETF 스윙 / 동적 배분
  ├─ run_bucket3_stream()              실시간 — B3 WebSocket(Alpaca) / 1초폴링(Toss)
  └─ strategy_mgr.run_premarket_loop() 30초체크 — 매일 9:20 ET 프리마켓 스캔

[3대 관심사 분리]
  MarketRegimeAnalyzer  시장 상태 진단 (RegimeEngine + ConfidenceScanner + NewsAnalyzer)
  StrategyManager       전략 전환 엔진 (B3/B2 모드 스위칭 + 60초 sync lock)
  AccountManager        계좌 자금 관리 (A/B 로테이션 + Settled Cash 재확인)

[사계절 레짐 이중 구조]
  외부 레짐 (매일 9:20 ET 스캔)
    신뢰도 ≥70점 후보 ≥5개 → B3_AGGRESSIVE
    신뢰도 ≥70점 후보  <5개 → B2_SWING
  내부 레짐 (B2, 15분마다)
    QQQ+SPY 모두 > MA20  → BULL_LEVERAGE (TQQQ/SOXL/FNGU/LABU Top2 × 50%)
    하나 이하 > MA20     → DEFENSE_INDEX (QQQ/SPY 모멘텀 강한 1개 × 100%)
    모두 ≤ MA20          → CASH (전량 청산)
```

---

## 버킷 구조 (B1 : B2 : B3 = 1 : 4 : 5)

| 버킷 | 기본 비중 | 전략 | 주기 |
|------|----------|------|------|
| B1 가치주 | 10% | 펀더멘털 + DCF 안전마진 | 60분 |
| B2 ETF 스윙 | 40% | MA20 기반 레버리지/방어 자동 전환 | 15분 |
| B3 급등주 | 50% | 신뢰도 스코어 + A/B 로테이션 + 개미털기 방어 | 실시간 |

---

## B3 급등주 모드

### 신뢰도 스코어 (100점 만점)

| 항목 | 배점 | 기준 |
|------|------|------|
| RVOL | 30점 | 5x→20pt, 10x→25pt, 20x→30pt |
| Alpha vs QQQ | 40점 | +5%→20pt, +7%→30pt, +10%→40pt |
| VWAP 돌파 | 30점 | 상단+0.5%→30pt, 위→15pt, 아래→0pt |

- **뉴스 보정**: 최종 신뢰도 = 차트 점수 × 0.7 + 뉴스 심리 점수 × 0.3
- **자금 배분**: ≥90점→전액, 70~89점→절반, <70점→진입 금지
- **켈리 스케일**: 최근 5회 승률 ≥60% → 예산 ×1.2

### A/B 그룹 로테이션 (Cash Account)
- 홀수 날 Group A / 짝수 날 Group B 활성
- 진입 가능 자금 = `min(총자산/2, Settled Cash)` — T+1 프리라이딩 원천 차단

### 청산 — 4-레이어 ExitStrategyEngine

| 우선순위 | 트리거 | 동작 |
|---------|--------|------|
| L1 | 본절가 트랩: 고점 +10% 후 진입가 이탈 | 즉시 SELL |
| L2 | 오더플로우 매도압력 ≥1.5× (100봉) | 즉시 SELL |
| L3 | ATR 가변 트레일링 (<50%: ATR×3, ≥50%: ATR×1.5) | SELL or HOLD |
| 보조 | 개미털기 방어: 저거래량 ATR 이탈 | 60초 대기 후 재판단 |
| 3분룰 | 진입 3~8분 내 PnL ≤ 0% | 보유량 50% 매도 |

---

## B2 ETF 스윙 모드

### 유니버스

| 구분 | 종목 |
|------|------|
| 레버리지 | TQQQ, SOXL, FNGU, LABU |
| 방어 | QQQ, SPY |

### 내부 레짐 전환

| 레짐 | 조건 | 전략 |
|------|------|------|
| BULL_LEVERAGE | QQQ+SPY 모두 > MA20 | ATR조정수익률 상위 2개 × 50%씩 |
| DEFENSE_INDEX | 하나라도 ≤ MA20 | 5일 모멘텀 강한 1개 × 100% |
| CASH | 모두 ≤ MA20 | 전량 청산 |

- ATR 조정 수익률 = 5일 수익률 / ATR14 (리스크 1단위당 수익 기준 순위)
- 방어 모드 청산: 주봉 MA20(20주) 이탈 시

---

## 뉴스 심리 분석 (NewsAnalyzer)

1. **수집**: Alpaca News API (최근 1시간) → yfinance fallback
2. **긴급 차단**: `유상증자 / 소송 / 실적악화 / 상장폐지 / secondary offering / class action` 등 감지 시 즉시 블랙리스트 + 텔레그램 경고
3. **Gemini Flash 분석**: 뉴스 제목 최대 10개 → 심리 점수 (-1.0 ~ +1.0)
4. **신뢰도 통합**: `최종 = 차트×0.7 + 뉴스(0~100환산)×0.3`
5. **DB 기록**: 모든 뉴스 점수 `market_log` 저장 (수익률-뉴스 상관 분석용)

---

## 리스크 관리

| 항목 | 기준 | 동작 |
|------|------|------|
| 일 손실 한도 | 계좌 -2% | 킬스위치 ON — 신규 진입 차단 |
| VIX 절대값 | ≥ 30 | 신규 진입 차단 |
| VIX 변화율 | 전일 대비 +20% | 선제 차단 + 텔레그램 경고 |
| 데드존 | 11:30~13:00 ET | 신규 진입 차단 |
| 장 마감 전 | 15분 | 인트라데이 포지션 강제 청산 |
| Panic 레짐 | VIX > 30 | B3 전량청산 + B2 인버스 ETF 헤지(SQQQ/SDS) |
| 섹터 집중도 | 동일 섹터 최대 3개 | 초과 시 진입 차단 |
| ATR 사이징 | 거래당 계좌 1% 리스크 | 손절거리 = ATR×2 |

---

## DB 구조

### storage/db.py — PositionDB (기존 포지션 저널)
```
positions   symbol, strategy, entry_price, peak_price, qty, sector
trades      id, symbol, side, qty, price, strategy, reason, ts
daily_pnl   date, realized, unrealized
```

### storage/db_manager.py — 사계절 엔진 전용
```
trades        id, symbol, buy_price, sell_price, quantity, mode, result, timestamp
market_log    id, date, nasdaq_ma20, regime, scanner_score, timestamp
system_state  key('CURRENT_MODE'|'ACTIVE_GROUP'|'B2_ALLOC_MODE'), value
```

---

## Telegram 봇

봇 시작 시 `setMyCommands`로 자동완성 등록 (`/` 입력 시 목록 표시).

| 명령어 | 기능 |
|--------|------|
| `/status` | 현재 모드·그룹·오늘 성적·보유 종목 요약 |
| `/set_mode [B3/B2]` | 레짐 강제 전환 |
| `/ping` | 봇 상태 확인 |
| `/account` | 계좌 잔고 |
| `/positions` | 보유 포지션 + 진입가 + 고점 |
| `/journal [날짜]` | 일일 매매 일지 (AI 분석 포함) |
| `/weekly [날짜]` | 주간 분석 + 버킷 비중 권고 |
| `/stats [N]` | 최근 N일 누계 통계 (기본 30일) |
| `/scan` | 급등/저평가 종목 스캔 |
| `/ask 질문` | Gemini AI에게 질문 |
| `/buy TICKER QTY` | 수동 매수 |
| `/sell TICKER QTY` | 수동 매도 |
| `/search 키워드` | 종목 검색 |
| `/gptstatus` | Gemini API 상태 |
| `/stop` | 봇 종료 |

---

## 설치 & 실행

```bash
# 1. 환경 구성
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에서 API 키 입력

# 3. 페이퍼 트레이딩 (실제 주문 없음)
MODE=paper python main.py

# Cash Account A/B 로테이션 활성화
ACCOUNT_TYPE=cash MODE=paper python main.py

# 4. 텔레그램 봇 별도 실행
python -m notify.telegram_bot

# 5. 단위 테스트
pytest tests/ -v
```

---

## 환경변수 (.env)

```env
# Alpaca
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # 실전: https://api.alpaca.markets

# Google Gemini
GEMINI_API_KEY=...
# GEMINI_FLASH_MODEL=gemini-1.5-flash  (기본값)
# GEMINI_PRO_MODEL=gemini-1.5-pro      (기본값)

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# 실행 모드
MODE=paper          # paper | live
ACCOUNT_TYPE=cash   # cash | margin (기본: margin)
BROKER=alpaca       # alpaca | toss
```

---

## 프로젝트 구조

```
authTrade/
├── main.py                          # 진입점 — asyncio 다중 태스크
├── config.yaml                      # 전략 파라미터
├── core/
│   ├── MarketRegimeAnalyzer.py      # 시장 상태 진단 (신규)
│   ├── StrategyManager.py           # 전략 전환 엔진 (신규)
│   ├── AccountManager.py            # 계좌 자금 관리 (신규)
│   ├── orchestrator.py              # 비동기 태스크 조율
│   ├── regime_engine.py             # B3/B2 모드 감지 + 전환
│   ├── bucket_capital.py            # 버킷 자금 격리 + 성과 리밸런싱
│   ├── kill_switch.py               # 일손실 킬스위치
│   ├── websocket_stream.py          # Alpaca WebSocket
│   └── polling_stream.py            # 토스증권 1초 폴링
├── strategy/
│   ├── strategy_engine.py           # 통합 인터페이스 (신규)
│   ├── news_analyzer.py             # 뉴스 심리 분석 + 긴급 차단 (신규)
│   ├── confidence_scanner.py        # RVOL/Alpha/VWAP 100점 스코어러
│   ├── exit_strategy.py             # 4-레이어 청산 엔진
│   ├── b2_allocation.py             # B2 내부 동적 배분
│   ├── etf_swing.py                 # B2 ETF 진입 조건
│   ├── squeeze.py                   # B3 급등주/스퀴즈
│   ├── value_long.py                # B1 가치주
│   ├── signals.py                   # RSI/MACD/ATR/BB/VWAP
│   ├── sizing.py                    # ATR 포지션 사이징 + 켈리
│   ├── entries.py                   # 진입 조건 함수
│   └── exits.py                     # 청산 조건 함수
├── storage/
│   ├── db_manager.py                # 사계절 엔진 DB (신규)
│   ├── db.py                        # PositionDB (포지션 저널)
│   └── journal.py                   # 일일/주간 일지 생성
├── ai/
│   └── gemini_helper.py             # Gemini Flash/Pro 듀얼 라우터
├── notify/
│   ├── telegram_bot.py              # 양방향 Telegram 봇
│   └── telegram_notifier.py         # 단방향 알림 push
├── analysis/
│   ├── market.py                    # SPX/VIX 시장 레짐 분석
│   ├── news.py                      # Alpaca 뉴스 + 키워드 감성
│   └── fundamental.py               # PER/PBR/ROE/DCF
├── trader/
│   ├── execution.py                 # AlpacaBroker (limit/IOC + settled_cash)
│   ├── toss.py                      # TossInvestBroker
│   └── paper.py                     # PaperSimBroker
├── backtest/
│   ├── engine.py                    # 바-단위 이벤트 드리븐 엔진
│   ├── run.py                       # CLI 러너
│   └── report.py                    # 결과 출력/CSV
├── risk/
│   └── guard.py                     # VIX 가드 + 서킷브레이커
└── tests/                           # pytest 단위 테스트 (80개)
```

---

## 백테스트

```bash
# B2 ETF 스윙 — 일봉 1년
python -m backtest.run --bucket etf_swing --days 365

# B3 급등주 — 5분봉 60일
python -m backtest.run --bucket squeeze --symbols TSLA AMD NVDA --days 60

# 초기 자본 / CSV 저장
python -m backtest.run --bucket etf_swing --cash 50000 --csv results/etf.csv
```

| 인터벌 | 최대 기간 | 사용 버킷 |
|--------|-----------|-----------|
| 5분봉 | 60일 | B3 급등주 |
| 일봉 | 5년+ | B1, B2 |

> 슬리피지 미반영 — 실전 수익은 5~10% 낮게 산정 권장
