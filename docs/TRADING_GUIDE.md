# authTrade 운영 가이드

> 페이퍼 트레이딩 → 백테스트 → 승률 검증 → 실전 전환까지 단계별 가이드.

---

## 목차

1. [환경 설정](#1-환경-설정)
2. [페이퍼 트레이딩 실행](#2-페이퍼-트레이딩-실행)
3. [단위 테스트](#3-단위-테스트)
4. [백테스트](#4-백테스트)
5. [승률 데이터 수집 및 분석](#5-승률-데이터-수집-및-분석)
6. [텔레그램 봇 명령어](#6-텔레그램-봇-명령어)
7. [실전 전환 체크리스트](#7-실전-전환-체크리스트)
8. [파라미터 튜닝 가이드](#8-파라미터-튜닝-가이드)
9. [버킷별 성능 기준](#9-버킷별-성능-기준)
10. [트러블슈팅](#10-트러블슈팅)

---

## 1. 환경 설정

### 1-1. 설치

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 1-2. `.env` 파일 생성

```env
# ── Alpaca ────────────────────────────────────────────────────────────
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here

# 페이퍼: https://paper-api.alpaca.markets
# 실전:   https://api.alpaca.markets
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# 플랜: free(기본) | unlimited($9/월)
# unlimited: 프리마켓 8:00 ET 스캔, API 동시요청 50개
ALPACA_PLAN=free

# ── Gemini AI ─────────────────────────────────────────────────────────
GEMINI_API_KEY=your_gemini_key

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# ── 실행 모드 ────────────────────────────────────────────────────────
MODE=paper               # paper | live
ACCOUNT_TYPE=cash        # cash(T+1 A/B 로테이션) | margin
BROKER=alpaca            # alpaca | toss
```

### 1-3. Alpaca 플랜별 차이

| 항목 | free (기본) | unlimited ($9/월) |
|------|-------------|-------------------|
| 프리마켓 스캔 시각 | 9:40 AM ET | 8:00 AM ET |
| 프리마켓 1분봉 | 불가 | 가능 (4:00~9:30 ET) |
| API 동시 요청 | 세마포어 10 | 세마포어 50 |
| 전환 방법 | 기본값 | `.env`에 `ALPACA_PLAN=unlimited` 추가 |

> **권장**: 페이퍼 테스트는 `free`로 충분. 실전 전환 시 `unlimited`로 업그레이드.

---

## 2. 페이퍼 트레이딩 실행

### 2-1. 기본 실행

```bash
# .env에 MODE=paper 확인 후
python main.py
```

### 2-2. 거래 시간대

| 시간 (ET) | 동작 |
|-----------|------|
| 9:40 AM | 프리마켓 스캔 — B3 후보 종목 선정 |
| 9:30~9:45 AM | B3 Gap&Go 첫 5분봉 진입 윈도우 |
| 9:30~11:00 AM | B3 가장 활성 구간 |
| 11:30~2:00 PM | 데드존 — 신규 진입 차단 |
| 2:00~3:45 PM | 오후 재개 |
| 3:45 PM | 인트라데이 포지션 강제 청산 시작 |

### 2-3. 로그 확인

```bash
# 실시간 로그 (INFO 이상)
python main.py 2>&1 | tee logs/paper_$(date +%Y%m%d).log

# B3 진입/청산만 필터
python main.py 2>&1 | grep "\[B3\]"

# 에러만 확인
python main.py 2>&1 | grep "ERROR\|WARN"
```

### 2-4. 주요 로그 메시지 해석

```
[B3/PAPER] SOUN 매수 50주 @ $8.42          → 진입 성공
  신뢰도 82점 (절반 $3,200 투입)
  근거: Gap&Go — 9:32 첫 5분봉 $8.50 고점 돌파

[B3] SOUN ConfidenceScore 45점 미달 → 진입 차단  → 신뢰도 부족 (정상 차단)
[B3] SOUN gap_pct=2.3% < 4.0% → 진입 차단        → 갭업 기준 미달 (정상)
[EXIT] SOUN trailing_stop 청산 $9.15 PnL +$36.50  → 트레일링 익절
[EXIT][3분룰] SOUN hold=4.2분 pnl=-1.2% → 절반 매도 → 3분룰 발동 (정상)
[B2][DEFENSE] SPY 손절 청산: -8.0%         → DEFENSE_INDEX 손절
```

### 2-5. 현재 상태 확인 (DB 직접 조회)

```bash
# 열린 포지션 확인
sqlite3 storage/trade.db "SELECT symbol, strategy, entry_price, qty FROM positions WHERE status='open';"

# 오늘 거래 내역
sqlite3 storage/trade.db "SELECT symbol, side, price, reason FROM trades WHERE ts LIKE '$(date +%Y-%m-%d)%' ORDER BY ts;"

# 누적 손익
sqlite3 storage/trade.db "SELECT SUM(pnl), COUNT(*) FROM closed_trades;"
```

---

## 3. 단위 테스트

### 3-1. 전체 실행

```bash
pytest tests/ -v
```

### 3-2. 빠른 테스트 (핵심 전략 로직만)

```bash
# 진입/청산 조건
pytest tests/test_entries.py tests/test_exits.py -v

# DB 무결성
pytest tests/test_db.py -v

# 리스크 가드
pytest tests/test_risk_guard.py -v
```

### 3-3. 특정 테스트만 실행

```bash
# 함수명으로 필터
pytest tests/ -k "test_stop_loss or test_trailing"

# 특정 파일
pytest tests/test_signals.py -v
```

### 3-4. 테스트 목록

| 파일 | 검증 내용 |
|------|-----------|
| `test_entries.py` | momentum_entry, value_entry RSI/MACD 조건 |
| `test_exits.py` | stop_loss, take_profit, trailing_stop, EOD exit |
| `test_exit_strategy.py` | 4-레이어 ExitStrategyEngine (blow-off top 포함) |
| `test_signals.py` | compute_indicators 컬럼명, NaN 방어 |
| `test_db.py` | PositionDB CRUD, peak 갱신, 섹터 집계 |
| `test_sizing.py` | ATR 사이징 엣지케이스 (ATR=0, price=0) |
| `test_risk_guard.py` | TradingGuard 일손실 한도, VIX 필터 |
| `test_regime.py` | is_deadzone, is_high_volatility DST 케이스 |
| `test_filters.py` | sector_concentration_ok |
| `test_fundamentals.py` | yfinance mock EPS growth |
| `test_strategy_risk.py` | 여름/겨울 EST/EDT 장 시간 자동 처리 |

---

## 4. 백테스트

> **한계**: 현재 엔진은 `momentum_entry()` 기반. B3 `gap_and_go_squeeze_entry()`, B2 `swing_b2_entry()` 실제 전략과 완전히 일치하지 않음.  
> 방향성 검증에 사용하되, 실제 성과는 페이퍼 트레이딩으로 확인 필요.

### 4-1. 빠른 실행

```bash
# B3 급등주 — 5분봉 60일 (yfinance 최대)
python -m backtest.run --bucket squeeze --days 60 --cash 14800

# B2 ETF 스윙 — 일봉 1년
python -m backtest.run --bucket etf_swing --days 365 --cash 14800

# B1 가치주 — 일봉 1년
python -m backtest.run --bucket value_long --days 365 --cash 14800
```

### 4-2. 종목 직접 지정

```bash
# 오늘 카탈리스트 급등주로 B3 검증
python -m backtest.run --bucket squeeze \
  --symbols SOUN MARA RIOT NVAX PRGO \
  --days 60

# B2 레버리지 ETF 집중 검증
python -m backtest.run --bucket etf_swing \
  --symbols TQQQ SOXL FNGU \
  --days 365
```

### 4-3. 결과 저장

```bash
# CSV 저장
python -m backtest.run --bucket squeeze \
  --days 60 --cash 14800 \
  --csv results/b3_60d.csv

# 결과 폴더
mkdir -p results
```

### 4-4. 출력 예시 해석

```
═══════════════════════════════════════════════════════
  BACKTEST REPORT
═══════════════════════════════════════════════════════
  총 거래 수        : 47          ← 30건 이상이어야 유의미
  총 손익           : $+864.20
  승률              : 53.2%       ← 55%+ 목표
  최대 낙폭         : -8.43%      ← -15% 이내 목표
  샤프 지수(근사)   : 1.24        ← 1.0+ 이면 양호
───────────────────────────────────────────────────────

  [청산 사유 분포]
    stop_loss            : 18건  평균 -4.8%   ← 손절 비중 확인
    trailing_stop        : 21건  평균 +12.3%  ← 주 수익원
    rsi_overbought       :  5건  평균 +6.1%
    eod                  :  3건  평균 +1.2%   ← 마감 청산 (기회비용)
```

### 4-5. 데이터 한계

| 인터벌 | yfinance 최대 기간 | 주의 |
|--------|-------------------|------|
| 1분봉 | 최근 7일 | B3 단기 전략 검증 최소 단위 |
| 5분봉 | 최근 60일 | B3 권장 (한계 있음) |
| 일봉 | 5년+ | B1, B2 충분 |

---

## 5. 승률 데이터 수집 및 분석

### 5-1. 수집 흐름

```
페이퍼 트레이딩 실행
       ↓
매 거래마다 storage/trade.db 자동 기록
(positions, trades, closed_trades 테이블)
       ↓
30건 이상 쌓이면 stats.py로 분석
       ↓
판정 기준 충족 → 실전 전환 검토
```

### 5-2. stats.py 실행

```bash
# 전체 요약
python stats.py

# 버킷별 분석
python stats.py --bucket squeeze        # B3만
python stats.py --bucket etf_swing      # B2만
python stats.py --bucket value_long     # B1만

# 기간 필터
python stats.py --days 30               # 최근 30일
python stats.py --bucket squeeze --days 14

# CSV 내보내기
python stats.py --csv results/stats_$(date +%Y%m%d).csv
```

### 5-3. 출력 예시 및 해석

```
════════════════════════════════════════════════════════
  승률 분석 리포트
════════════════════════════════════════════════════════
  ────────────────────────────────────────────────────
  B3 급등스퀴즈 (47건)   🟡 보통 (최적화 필요)
  ────────────────────────────────────────────────────
  승률          : 51.0%  (목표 55%+)
  평균 수익     : +8.3%   최대 +43.2%
  평균 손실     : -4.1%   최대 -12.0%
  Profit Factor : 1.38   (1.5+ 권장)      ← 총수익/총손실 비율
  거래당 기대값 : $18.4                    ← 이게 양수여야 수익 전략
  누적 손익     : $+864.20
  평균 보유     : 38분

  [일별 승패]
  수익 날 12일  손실 날 8일  일간 승률 60%
  평균 일 손익: $+43.20
  최고 하루   : $+312.50
  최악 하루   : $-187.00

  🔧 파라미터 조정 후 재검증 권장
════════════════════════════════════════════════════════
```

### 5-4. 핵심 지표 이해

| 지표 | 공식 | 의미 |
|------|------|------|
| **승률** | 수익 거래 ÷ 전체 거래 | 55%+ 목표 |
| **Profit Factor** | 총수익 ÷ 총손실 | 1.5+ = 손실 1달러당 수익 1.5달러 |
| **기대값** | (승률×평균수익) - (패률×평균손실) | 양수 = 수익 전략, 음수 = 폐기 |
| **MDD** | 고점 대비 최대 낙폭 | 전략의 실제 리스크 척도 |
| **샤프** | 수익률 ÷ 변동성 × √252 | 1.0+ 양호, 2.0+ 우수 |

---

## 6. 텔레그램 봇 명령어

### 6-1. 상태 확인

| 명령어 | 결과 |
|--------|------|
| `/status` | 현재 모드(B3/B2), 그룹(A/B), 오늘 손익, 보유 종목 |
| `/positions` | 보유 종목별 진입가 · 현재가 · 미실현 PnL |
| `/account` | 총 자산, 현금, 미실현 PnL, 일 손익 |
| `/ping` | 봇 응답 확인 |

### 6-2. 통계 및 분석

| 명령어 | 결과 |
|--------|------|
| `/journal` | 오늘 매매 일지 (AI 분석 포함) |
| `/journal 2025-01-15` | 특정 날짜 일지 |
| `/weekly` | 이번 주 통계 + 버킷 비중 권고 |
| `/stats` | 최근 30일 누계 통계 |
| `/stats 7` | 최근 7일 통계 |

### 6-3. 수동 제어

| 명령어 | 동작 |
|--------|------|
| `/set_mode B3` | B3 급등주 모드로 강제 전환 |
| `/set_mode B2` | B2 ETF 스윙 모드로 강제 전환 |
| `/buy SOUN 100` | SOUN 100주 수동 매수 |
| `/sell SOUN 100` | SOUN 100주 수동 매도 |
| `/scan` | 현재 급등/저평가 후보 종목 스캔 |
| `/ask 오늘 반도체 섹터 전망은?` | Gemini AI 질문 |
| `/stop` | 봇 종료 |

---

## 7. 실전 전환 체크리스트

### 7-1. 데이터 기준 (stats.py 판정)

```
✅ 전환 가능 조건 (모두 충족 시)
   □ 총 거래 수 ≥ 60건 (30건은 최소, 60건이 통계적으로 유의미)
   □ 승률 ≥ 55%
   □ Profit Factor ≥ 1.5
   □ 거래당 기대값 > $0
   □ 최대낙폭(MDD) ≤ 10%
   □ 샤프 지수 ≥ 1.0
   □ 연속 손실 최대 5거래 이내

🟡 재검증 조건 (하나라도 해당 시)
   □ 승률 45~55%
   □ Profit Factor 1.2~1.5
   □ MDD 10~20%

🔴 전환 보류 (하나라도 해당 시)
   □ 거래 수 < 30건
   □ 승률 < 45%
   □ Profit Factor < 1.2
   □ 기대값 음수
   □ MDD > 20%
```

### 7-2. 환경 전환

```bash
# .env 수정
ALPACA_BASE_URL=https://api.alpaca.markets   # paper → live
MODE=live
ALPACA_PLAN=unlimited                         # 프리마켓 스캔 활성화 권장
```

### 7-3. 실전 시작 권장 순서

1. `ALPACA_PLAN=unlimited` 로 업그레이드 ($9/월)
2. 시드머니의 20%만 투입 → 2주 관찰
3. 문제 없으면 50% → 4주 관찰
4. 100% 투입

---

## 8. 파라미터 튜닝 가이드

> 튜닝 순서: B3 → B2 → B1 (수익 기여도 높은 순)

### 8-1. B3 급등주 튜닝 (`config.yaml` → `squeeze:`)

**승률이 낮을 때 (< 50%)**

```yaml
squeeze:
  min_gap_pct: 10.0       # 4.0 → 10.0 (갭업 기준 상향)
  min_rvol: 8.0           # 5.0 → 8.0  (거래량 기준 상향)
  min_rvol_intraday: 15.0 # 10.0 → 15.0
```

`confidence_scanner.py`에서 진입 기준점:
```python
# strategy/confidence_scanner.py
CONFIDENCE_THRESHOLD = 70   # 70 → 80 으로 올리면 필터 강화
```

**평균 수익이 낮을 때 (< 5%)**

```yaml
squeeze:
  atr_multiplier: 4.0       # 3.0 → 4.0 (스탑 멀게 → 더 오래 보유)
  breakeven_trigger_pct: 0.20  # +20% 도달 시 본절 이동
```

**손절이 너무 잦을 때 (MDD > 15%)**

```yaml
risk:
  stop_loss_pct: 0.05       # 7.5% → 5% (손절 타이트하게)
  per_trade_risk_pct: 0.005 # 1% → 0.5% (포지션 크기 줄이기)
```

### 8-2. B2 ETF 스윙 튜닝 (`config.yaml` → `etf_swing:`)

**스윙 승률 개선**

```yaml
etf_swing:
  swing_sl_pct: 0.03    # 4% → 3% (손절 타이트)
  swing_tp_pct: 0.12    # 8% → 12% (목표가 넓게)
```

**데드존 조정** (점심시간 거래 필터)

```yaml
engine:
  deadzone:
    start_hour: 11
    start_min: 0
    end_hour: 14
    end_min: 0
```

### 8-3. 파라미터 변경 후 재검증 절차

```bash
# 1. 백테스트로 방향성 확인
python -m backtest.run --bucket squeeze --days 60

# 2. 페이퍼 2주 운용
python main.py

# 3. 승률 비교
python stats.py --days 14
```

---

## 9. 버킷별 성능 기준

### 9-1. B3 급등스퀴즈 (50%)

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 승률 | < 45% | 45~55% | 55%+ |
| 평균 수익 | < 4% | 4~8% | 8%+ |
| 평균 손실 | > -7% | -4~-7% | < -4% |
| Profit Factor | < 1.2 | 1.2~1.5 | 1.5+ |
| 평균 보유 | > 120분 | 30~120분 | < 60분 |
| 주 거래 수 | < 2건 | 2~5건 | 5~10건 |

> B3는 주 3~7건이 이상적. 10건 초과는 필터가 너무 느슨한 것.

### 9-2. B2 ETF 스윙 (40%)

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 승률 | < 50% | 50~60% | 60%+ |
| 평균 수익 | < 3% | 3~6% | 6%+ |
| 평균 손실 | > -5% | -3~-5% | < -3% |
| 평균 보유 | < 1일 | 2~5일 | 3~7일 |

### 9-3. B1 가치주 (10%)

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 연 수익률 | < 10% | 10~20% | 20%+ |
| 최대 낙폭 | > -20% | -10~-20% | < -10% |
| 평균 보유 | < 30일 | 30~90일 | 60일+ |

---

## 10. 트러블슈팅

### 10-1. B3가 진입을 안 함

**증상**: 로그에 `[B3] gap_pct=0 rvol=0` 또는 `신뢰도 미달` 계속 출력

**원인 및 해결**:

```bash
# 1. 카탈리스트 종목이 없는 날 (정상)
#    - 갭업 20%+ 종목이 없으면 B3는 관망
#    - 주 2~3일은 B3 진입 없을 수 있음

# 2. watchlist 종목이 오늘 움직임 없음
grep "b3_syms" main.py               # watchlist 파일 확인
cat watchlists/symbols.txt

# 3. 9:40 이전에 확인 중 (스캔 전)
#    - 9:40 ET 이후 로그를 확인

# 4. API 키 문제
python -c "
from data.alpaca_bars import fetch_bars
df = fetch_bars('SPY', '5Min', 5)
print(df)
"
```

### 10-2. yfinance 타임아웃으로 B1 멈춤

**증상**: `[fundamentals] XXXX yfinance 타임아웃 (20s) — 스킵`

```bash
# 정상 동작 (20초 타임아웃 후 자동 스킵됨)
# 특정 종목만 계속 타임아웃이면 watchlist에서 제거
vi watchlists/value_symbols.txt
```

### 10-3. 텔레그램 봇 응답 없음

```bash
# 봇 토큰 확인
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"

# Chat ID 확인
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

### 10-4. DB 잠금 오류

```bash
# 다른 프로세스가 DB를 점유 중
lsof storage/trade.db

# WAL 모드 확인 (이미 설정됨, 잠금 최소화)
sqlite3 storage/trade.db "PRAGMA journal_mode;"
```

### 10-5. 승률이 백테스트보다 낮음

실전/페이퍼에서 백테스트 대비 승률이 5~15% 낮은 것은 정상입니다.

| 원인 | 규모 | 대응 |
|------|------|------|
| 슬리피지 | 0.2~0.5% per trade | Paper 모드 자동 반영 |
| 갭앤크랩 (백테스트에서 놓침) | 진짜 큰 요인 | min_gap_pct 상향 |
| 장 시작 5분 스프레드 | 매우 큼 | 9:32 이후 진입 |
| 백테스트 look-ahead bias | 구조적 | 결과 20% 할인 적용 |

### 10-6. 키 명령어 모음

```bash
# 현재 포지션
sqlite3 storage/trade.db "SELECT * FROM positions WHERE status='open';"

# 이번 달 손익
sqlite3 storage/trade.db "
  SELECT date, SUM(pnl) as daily_pnl, COUNT(*) as trades
  FROM closed_trades
  WHERE date >= strftime('%Y-%m-01', 'now')
  GROUP BY date
  ORDER BY date;"

# 버킷별 승률
sqlite3 storage/trade.db "
  SELECT strategy,
         COUNT(*) as total,
         SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
         ROUND(AVG(pnl_pct), 2) as avg_pnl_pct
  FROM closed_trades
  GROUP BY strategy;"

# DB 초기화 (페이퍼 재시작 시)
mv storage/trade.db storage/trade.db.bak
```

---

## 빠른 참조

```bash
# 페이퍼 실행
python main.py

# 단위 테스트
pytest tests/ -v

# B3 백테스트 60일
python -m backtest.run --bucket squeeze --days 60 --cash 14800

# B2 백테스트 1년
python -m backtest.run --bucket etf_swing --days 365 --cash 14800

# 승률 분석 (전체)
python stats.py

# 승률 분석 (B3, 최근 30일)
python stats.py --bucket squeeze --days 30

# 거래 CSV 내보내기
python stats.py --csv results/$(date +%Y%m%d)_stats.csv
```
