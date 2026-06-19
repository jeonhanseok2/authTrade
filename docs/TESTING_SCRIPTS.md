# authTrade 테스트 & 스크립트 가이드

> 단위 테스트 · 백테스트 · 승률 분석 · 파라미터 최적화 · 튜닝 · 성능 기준
> 최종 업데이트: 2026-06-18

---

## 목차

1. [단위 테스트](#1-단위-테스트)
2. [백테스트](#2-백테스트)
3. [승률 데이터 수집 및 분석](#3-승률-데이터-수집-및-분석)
4. [파라미터 최적화 — analyzer.py](#4-파라미터-최적화--analyzerpy)
5. [파라미터 튜닝 가이드](#5-파라미터-튜닝-가이드)
6. [버킷별 성능 기준](#6-버킷별-성능-기준)
7. [빠른 스크립트 참조](#7-빠른-스크립트-참조)

---

## 1. 단위 테스트

### 1-1. 전체 실행

```bash
pytest tests/ -v
```

### 1-2. 빠른 테스트 (핵심 전략 로직만)

```bash
# 진입/청산 조건
pytest tests/test_entries.py tests/test_exits.py -v

# DB 무결성
pytest tests/test_db.py -v

# 리스크 가드
pytest tests/test_risk_guard.py -v
```

### 1-3. 특정 테스트만 실행

```bash
# 함수명으로 필터
pytest tests/ -k "test_stop_loss or test_trailing"

# 특정 파일
pytest tests/test_signals.py -v
```

### 1-4. 테스트 목록

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

## 2. 백테스트

> **한계**: 현재 엔진은 `momentum_entry()` 기반으로 B3 `gap_and_go_squeeze_entry()`, B2 `swing_b2_entry()` 실제 전략과 완전히 일치하지 않음.
> 방향성 검증에 사용하되, 실제 성과는 페이퍼 트레이딩으로 확인 필요.

### 2-1. 빠른 실행

```bash
# B3 급등주 — 5분봉 60일 (yfinance 최대)
python -m backtest.run --bucket squeeze --days 60 --cash 14800

# B2 ETF 스윙 — 일봉 1년
python -m backtest.run --bucket etf_swing --days 365 --cash 14800

# B1 가치주 — 일봉 1년
python -m backtest.run --bucket value_long --days 365 --cash 14800
```

### 2-2. 종목 직접 지정

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

### 2-3. 결과 저장

```bash
mkdir -p results

python -m backtest.run --bucket squeeze \
  --days 60 --cash 14800 \
  --csv results/b3_60d.csv
```

### 2-4. 출력 예시 해석

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
    stop_loss            : 18건  평균 -4.8%
    trailing_stop        : 21건  평균 +12.3%   ← 주 수익원
    rsi_overbought       :  5건  평균 +6.1%
    eod                  :  3건  평균 +1.2%
```

### 2-5. 데이터 한계

| 인터벌 | yfinance 최대 기간 | 주의 |
|--------|-------------------|------|
| 1분봉 | 최근 7일 | B3 단기 전략 검증 최소 단위 |
| 5분봉 | 최근 60일 | B3 권장 |
| 일봉 | 5년+ | B1, B2 충분 |

---

## 3. 승률 데이터 수집 및 분석

### 3-1. 수집 흐름

```
페이퍼 트레이딩 실행
       ↓
매 거래마다 storage/trade.db 자동 기록
       ↓
30건 이상 쌓이면 stats.py로 분석
       ↓
판정 기준 충족 → 실전 전환 검토
```

### 3-2. stats.py 실행

```bash
# 전체 요약
python stats.py

# 버킷별 분석
python stats.py --bucket squeeze        # B3만
python stats.py --bucket etf_swing      # B2만
python stats.py --bucket value_long     # B1만

# 기간 필터
python stats.py --days 30
python stats.py --bucket squeeze --days 14

# CSV 내보내기
python stats.py --csv results/stats_$(date +%Y%m%d).csv
```

### 3-3. 출력 예시

```
════════════════════════════════════════════════════════
  승률 분석 리포트
════════════════════════════════════════════════════════
  B3 급등스퀴즈 (47건)   🟡 보통 (최적화 필요)
  ────────────────────────────────────────────────────
  승률          : 51.0%  (목표 55%+)
  평균 수익     : +8.3%   최대 +43.2%
  평균 손실     : -4.1%   최대 -12.0%
  Profit Factor : 1.38   (1.5+ 권장)
  거래당 기대값 : $18.4                    ← 양수여야 수익 전략
  누적 손익     : $+864.20
  평균 보유     : 38분
════════════════════════════════════════════════════════
```

### 3-4. 핵심 지표 이해

| 지표 | 공식 | 목표 |
|------|------|------|
| **승률** | 수익 거래 ÷ 전체 거래 | 55%+ |
| **Profit Factor** | 총수익 ÷ 총손실 | 1.5+ |
| **기대값** | (승률×평균수익) - (패률×평균손실) | 양수 |
| **MDD** | 고점 대비 최대 낙폭 | -10% 이내 |
| **샤프** | 수익률 ÷ 변동성 × √252 | 1.0+ |

---

## 4. 파라미터 최적화 — analyzer.py

> 페이퍼 트레이딩 데이터를 분석해 현재 파라미터와 최적값을 비교하고, 구체적 튜닝 제안을 생성합니다.

### 4-1. 기본 실행

```bash
# 전체 분석 (최근 30일)
python analyzer.py

# 최근 2주만 분석
python analyzer.py --days 14

# B4 집중 분석 + 텔레그램 알림
python analyzer.py --bucket b4 --notify

# 분석 결과만 (알림 없음)
python analyzer.py --days 30
```

### 4-2. 출력 예시

```
════════════════════════════════════════════════════════════════════════
  전략 파라미터 최적화 분석기
  기간: 최근 30일  |  생성: 2026-06-18 10:30:22

  ■ 버킷별 성과 요약
  버킷          N   승률       PF      기대값    상태
  squeeze      47  51.0%     1.38    $+18.40   🔴 개선필요
  B4           15  60.0%     2.10    $+45.20   ✅ 최적

  ■ B4 파라미터 매트릭스 (RVOL 필터 × 트레일링 스탑)
  rvol_%  trail_%   n  win_rate  avg_ret_%    pf  현재설정  note
  200%     -10%    18      55.6       8.40  2.10                근사(RVOL미저장)
  250%     -10%    15      60.0      10.20  2.40  ★
  250%     -15%    15      60.0       7.10  2.10  ★
  300%     -10%    12      66.7      11.50  2.80                근사(RVOL미저장)

  ■ 파라미터 튜닝 제안
  [1] 🔴 B3 — min_rvol_intraday
       현재값: 10.0x → 제안값: 12.0x
       근거: 승률 51.0% — 진입 RVOL 기준 강화로 신호 품질 향상
```

### 4-3. recommendations.log 활용

```bash
# 최신 추천 확인
tail -100 recommendations.log

# 날짜별 추천 이력
grep "^\[202" recommendations.log

# 고우선순위 항목만 필터
grep "\[HIGH\]" recommendations.log
```

### 4-4. 파라미터 적용 절차

```
1. analyzer.py 실행 → recommendations.log 확인
2. config.yaml 수정 (자동 적용 없음)
   예: squeeze.min_rvol_intraday: 10.0 → 12.0
3. B4 상수는 strategy/strategy_engine.py 수정
   예: _B4O_TRAIL_DIST = 0.15 → 0.10
4. 백테스트 또는 2주 페이퍼 재검증
5. 결과 비교 후 최종 채택
```

> **주의**: B4 RVOL 매트릭스는 DB에 RVOL이 저장되지 않으므로 근사치입니다.

---

## 5. 파라미터 튜닝 가이드

> 튜닝 순서: B4 → B3 → B2 → B1 (변동성/수익 기여도 높은 순)
> **권장**: `python analyzer.py` 실행 후 `recommendations.log` 확인 → 수동 적용

### 5-1. B4 스나이퍼 모드 (`strategy/strategy_engine.py` 모듈 상수)

```python
# strategy/strategy_engine.py 상단
_B4O_RVOL_MIN   = 2.50   # 진입 RVOL 기준 (250%)
_B4O_TRAIL_DIST = 0.15   # 트레일링 스탑 거리 (-15%)
_B4O_INIT_SL    = 0.20   # 초기 손절 (-20%)
_B4O_CAPITAL    = 0.50   # 예수금 투입 비율 (50%)
_B4O_ENTRY_H    = 10     # 진입 시작 시각 (10:00 ET)
```

| 증상 | 조정 방향 |
|------|-----------|
| 승률 < 50% | `_B4O_RVOL_MIN` 올리기 (2.50 → 3.00) |
| 평균 수익 낮음 (트레일링 너무 타이트) | `_B4O_TRAIL_DIST` 올리기 (-15% → -20%) |
| 큰 수익에서 되돌릴 때 | `_B4O_TRAIL_DIST` 내리기 (-15% → -10%) |
| 초기 손절 너무 잦음 | `_B4O_INIT_SL` 내리기 (-20% → -15%) |

**검증 절차**

```bash
python analyzer.py --bucket b4      # 1. 현행 시뮬레이션
# 상수 수정 후
python main.py                      # 2. 페이퍼 2주 운용
python analyzer.py --bucket b4 --days 14  # 3. 변경 효과 비교
```

### 5-2. B3 급등주 (`config.yaml` → `squeeze:`)

```yaml
squeeze:
  min_gap_pct: 4.0          # 승률 낮으면 올리기 (→ 10.0)
  min_rvol: 5.0             # 승률 낮으면 올리기 (→ 8.0)
  min_rvol_intraday: 10.0   # 장중 진입 기준
  atr_multiplier: 3.0       # 평균 수익 낮으면 올리기 (→ 4.0)
  breakeven_trigger_pct: 0.20
```

신뢰도 점수 기준 강화:
```python
# strategy/confidence_scanner.py
CONFIDENCE_THRESHOLD = 70   # → 80 으로 올리면 필터 강화
```

### 5-3. B2 ETF 스윙 (`config.yaml` → `etf_swing:`)

```yaml
etf_swing:
  swing_sl_pct: 0.04    # 손절 (낮추면 타이트)
  swing_tp_pct: 0.08    # 목표가 (높이면 더 오래 보유)

engine:
  deadzone:
    start_hour: 11
    start_min: 30
    end_hour: 13
    end_min: 0
```

### 5-4. 파라미터 변경 후 재검증 절차

```bash
# 1. 백테스트로 방향성 확인
python -m backtest.run --bucket squeeze --days 60

# 2. B4 시뮬레이션 매트릭스
python analyzer.py --bucket b4

# 3. 페이퍼 2주 운용
python main.py

# 4. 승률 비교
python analyzer.py --days 14 --notify
```

---

## 6. 버킷별 성능 기준

### 6-1. B4 스나이퍼 옵션

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 승률 | < 40% | 40~55% | 55%+ |
| 평균 수익 | < 20% | 20~50% | 50%+ |
| 평균 손실 | > -20% | -15~-20% | < -15% |
| Profit Factor | < 1.5 | 1.5~2.5 | 2.5+ |
| 월 거래 수 | < 5건 | 5~15건 | 10~20건 |

> 진입 조건이 까다로우므로 월 5~15건이 정상 범위.

### 6-2. B3 급등스퀴즈

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 승률 | < 45% | 45~55% | 55%+ |
| 평균 수익 | < 4% | 4~8% | 8%+ |
| 평균 손실 | > -7% | -4~-7% | < -4% |
| Profit Factor | < 1.2 | 1.2~1.5 | 1.5+ |
| 평균 보유 | > 120분 | 30~120분 | < 60분 |
| 주 거래 수 | < 2건 | 2~5건 | 5~10건 |

> 주 3~7건이 이상적. 10건 초과는 필터가 너무 느슨한 것.

### 6-3. B2 ETF 스윙

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 승률 | < 50% | 50~60% | 60%+ |
| 평균 수익 | < 3% | 3~6% | 6%+ |
| 평균 손실 | > -5% | -3~-5% | < -3% |
| 평균 보유 | < 1일 | 2~5일 | 3~7일 |

### 6-4. B1 가치주

| 지표 | 나쁨 | 보통 | 좋음 |
|------|------|------|------|
| 연 수익률 | < 10% | 10~20% | 20%+ |
| 최대 낙폭 | > -20% | -10~-20% | < -10% |
| 평균 보유 | < 30일 | 30~90일 | 60일+ |

---

## 7. 빠른 스크립트 참조

```bash
# ── 테스트 ──────────────────────────────────────────────────────────
pytest tests/ -v                                   # 전체 테스트
pytest tests/test_exits.py -v                      # 청산 로직만
pytest tests/ -k "test_stop_loss"                  # 특정 함수만

# ── 백테스트 ────────────────────────────────────────────────────────
python -m backtest.run --bucket squeeze --days 60 --cash 14800
python -m backtest.run --bucket etf_swing --days 365 --cash 14800
python -m backtest.run --bucket value_long --days 365 --cash 14800

# ── 승률 분석 ────────────────────────────────────────────────────────
python stats.py                                    # 전체
python stats.py --bucket squeeze --days 30         # B3, 최근 30일
python stats.py --csv results/stats_$(date +%Y%m%d).csv

# ── 파라미터 최적화 ──────────────────────────────────────────────────
python analyzer.py                                 # 전체 분석
python analyzer.py --bucket b4 --notify            # B4 + 텔레그램
python analyzer.py --days 14                       # 최근 2주
tail -100 recommendations.log                      # 추천 확인
grep "\[HIGH\]" recommendations.log                # 고우선순위만
```
