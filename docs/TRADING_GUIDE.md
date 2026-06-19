# authTrade 운영 가이드

> 환경 설정 · 실행 · 모니터링 · 실전 전환 · 트러블슈팅
> 최종 업데이트: 2026-06-18

---

## 목차

1. [환경 설정](#1-환경-설정)
2. [실행 및 모니터링](#2-실행-및-모니터링)
3. [텔레그램 봇 명령어](#3-텔레그램-봇-명령어)
4. [실전 전환 체크리스트](#4-실전-전환-체크리스트)
5. [트러블슈팅](#5-트러블슈팅)
6. [빠른 참조](#6-빠른-참조)

---

## 1. 환경 설정

### 1-1. 설치

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **패키지 변경 (2026-06-18)**: Gemini SDK가 `google-generativeai` → `google-genai`로 변경됨.
> 이전 버전 사용 중이라면 재설치 필요:
> ```bash
> pip uninstall google-generativeai -y
> pip install google-genai
> ```

### 1-2. `.env` 파일 구성

환경 파일은 3단계로 로드됩니다: `.env` → `.env.paper` 또는 `.env.live`

```bash
cp .env.example .env
cp .env.example .env.paper   # 페이퍼 전용 설정
cp .env.example .env.live    # 실전 전용 설정
```

**`.env` (공통 기본값)**

```env
# ── Alpaca ────────────────────────────────────────────────────────────
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PLAN=free              # free | unlimited

# ── Gemini AI ─────────────────────────────────────────────────────────
GEMINI_API_KEY=your_gemini_key
# GEMINI_FLASH_MODEL=gemini-2.0-flash   # 기본값
# GEMINI_PRO_MODEL=gemini-2.5-pro       # 기본값

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

**`.env.paper` (페이퍼 오버레이)**

```env
MODE=paper
ACCOUNT_TYPE=cash
BROKER=alpaca
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

**`.env.live` (실전 오버레이)**

```env
MODE=live
ACCOUNT_TYPE=cash
BROKER=alpaca
ALPACA_BASE_URL=https://api.alpaca.markets
ALPACA_PLAN=unlimited
```

### 1-3. Alpaca 플랜별 차이

| 항목 | free (기본) | unlimited ($9/월) |
|------|-------------|-------------------|
| 프리마켓 스캔 시각 | 9:40 AM ET | 8:00 AM ET |
| 프리마켓 1분봉 | 불가 | 가능 (4:00~9:30 ET) |
| API 동시 요청 | 세마포어 10 | 세마포어 50 |

> **권장**: 페이퍼 테스트는 `free`로 충분. 실전 전환 시 `unlimited`.

---

## 2. 실행 및 모니터링

### 2-1. 기본 실행

```bash
# MODE는 .env.paper 또는 .env.live 에서 자동 로드
python main.py
```

### 2-2. 거래 시간대 (ET 기준)

| 시간 (ET) | KST | 동작 |
|-----------|-----|------|
| 9:30 AM | 22:30 | 장 개장 — B3 WebSocket 바 수신 시작 |
| 9:40 AM | 22:40 | 프리마켓 스캔 — B3/B2 모드 결정 |
| 9:30~9:45 AM | 22:30~22:45 | B3 Gap&Go 첫 5분봉 진입 윈도우 |
| **10:00 AM~** | **23:00~** | **B4 스나이퍼 모드 진입 가능** |
| 11:30~2:00 PM | 00:30~03:00 | 데드존 — 신규 진입 차단 |
| 3:30 PM | 04:30 | B4 타임 스탑 — 잔여 계약 강제 청산 |
| 4:00 PM | 05:00 | 장 마감 |

> **장 외 시간**: 봇은 자동으로 신규 진입을 차단하고 60초마다 현재 상태를 출력합니다.

### 2-3. 로그 파일

로그는 자동으로 날짜별 파일로 저장됩니다:

```
logs/
└── YYMM/
    └── YYYY-MM-DD.log    # 예: logs/2606/2026-06-18.log
```

```bash
# 오늘 로그 실시간 확인
tail -f logs/$(date +%y%m)/$(date +%Y-%m-%d).log

# B3 진입/청산만 필터
tail -f logs/$(date +%y%m)/$(date +%Y-%m-%d).log | grep "\[B3\]"

# 에러만 확인
tail -f logs/$(date +%y%m)/$(date +%Y-%m-%d).log | grep '"lvl":"ERROR\|WARNING"'

# 실시간 stdout + 파일 동시 출력 (구 방식도 가능)
python main.py 2>&1 | tee /dev/stderr
```

### 2-4. 주요 로그 메시지 해석

**장 상태 heartbeat (60초마다)**

```json
{"ts":"2026-06-18T01:00:00Z","lvl":"INFO","msg":"[MONITOR] 장 개장까지 8시간 30분 (09:30 ET)"},
{"ts":"2026-06-18T13:30:00Z","lvl":"INFO","msg":"[MONITOR] 장중 (ET 09:30) | 킬스위치=False"},
{"ts":"2026-06-18T20:05:00Z","lvl":"INFO","msg":"[MONITOR] 장 마감 — 다음 개장 대기"}
```

**프리마켓 스캔 결과**

```json
{"msg":"[MarketRegimeAnalyzer] 스캔 완료 — [프리마켓 스캔] 2026-06-18 09:40 ET\n모드: B3_AGGRESSIVE — 신뢰도 ≥70점 후보 7종목\n  NVDA: 87점 ..."}
```

**B1 가치주 스캔**

```
[B1] 스캔 시작 — 20종목 검색 (레짐: bull, VIX: 18.3, B1보유: 2/8)
[B1] 후보 5종목: ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META']
[B1] AAPL 진입 조건 미충족: RSI overbought
```

**B2 ETF 리밸런싱**

```
[B2] 리밸런싱 → 모드: BULL_LEVERAGE | 목표 종목: ['TQQQ', 'SOXL'] | 예산: $12,400
[B2-Alloc] TQQQ 진입 조건 미충족: MA20 하향 돌파
```

**B3 급등주**

```
[B3] NVDA 바 수신 — gap2.3% RVOL3.1x | 진입 판단 중
[B3] NVDA 진입 조건 미충족 — Gap 크기 부족 (2.3% < 3.0%)
[B3/PAPER] NVDA 매수 50주 @ $142.30   ← 진입 성공
[EXIT] NVDA trailing_stop 청산 $155.10 PnL +$634.00
```

**B4 스나이퍼**

```
[B4] 평균 거래량 갱신 시작 — ['QQQ', 'SPY']
[B4] QQQ 평균5분봉거래량=1250000
[B4] VIX 전일22.50 → 현재21.30 → 롱허용
[B4] QQQ RVOL 3.1x < 250% — 스킵
[B4] QQQ240621C00490000 진입 @$1.25 × 3계약
```

### 2-5. 현재 상태 확인 (DB 직접 조회)

```bash
# 열린 포지션 (B1/B2/B3)
sqlite3 storage/trade.db "SELECT symbol, strategy, entry_price, qty FROM positions WHERE status='open';"

# 오늘 거래 내역 (B1/B2/B3)
sqlite3 storage/trade.db "SELECT symbol, side, price, reason FROM trades WHERE ts LIKE '$(date +%Y-%m-%d)%' ORDER BY ts;"

# 누적 손익 (B1/B2/B3)
sqlite3 storage/trade.db "SELECT SUM(pnl), COUNT(*) FROM closed_trades;"

# B4 거래 내역
sqlite3 "storage/db/trading_data.db" "SELECT symbol, result_pct, exit_reason, trade_date FROM b4_trades ORDER BY trade_date DESC LIMIT 20;"

# B4 쿨다운 상태
sqlite3 "storage/db/trading_data.db" "SELECT * FROM b4_cooldown WHERE end_date >= DATE('now');"

# B4 일별 손익 요약
sqlite3 "storage/db/trading_data.db" "SELECT trade_date, COUNT(*) trades, ROUND(SUM(result_pct)*100,2) total_pct FROM b4_trades GROUP BY trade_date ORDER BY trade_date DESC;"
```

---

## 3. 텔레그램 봇 명령어

### 3-1. 상태 확인

| 명령어 | 결과 |
|--------|------|
| `/status` | 현재 모드(B3/B2), 그룹(A/B), 오늘 손익, 보유 종목 |
| `/positions` | 보유 종목별 진입가 · 현재가 · 미실현 PnL |
| `/account` | 총 자산, 현금, 미실현 PnL, 일 손익 |
| `/ping` | 봇 응답 확인 |

### 3-2. 통계 및 분석

| 명령어 | 결과 |
|--------|------|
| `/journal` | 오늘 매매 일지 (AI 분석 포함) |
| `/journal 2025-01-15` | 특정 날짜 일지 |
| `/weekly` | 이번 주 통계 + 버킷 비중 권고 |
| `/stats` | 최근 30일 누계 통계 |
| `/stats 7` | 최근 7일 통계 |

### 3-3. 수동 제어

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

## 4. 실전 전환 체크리스트

### 4-1. 데이터 기준 (stats.py 판정)

```
✅ 전환 가능 조건 (모두 충족 시)
   □ 총 거래 수 ≥ 60건
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

### 4-2. 환경 전환

`.env.live` 수정 후 실행:

```env
MODE=live
ALPACA_BASE_URL=https://api.alpaca.markets
ALPACA_PLAN=unlimited
```

```bash
python main.py   # .env.live 자동 로드
```

### 4-3. 실전 시작 권장 순서

1. `ALPACA_PLAN=unlimited` 업그레이드 ($9/월)
2. 시드머니의 20%만 투입 → 2주 관찰
3. 문제 없으면 50% → 4주 관찰
4. 100% 투입

---

## 5. 트러블슈팅

### 5-1. WebSocket 구독 후 아무 일도 없음

**원인**: 장 외 시간 (Alpaca는 정규장에서만 bar를 전송)

```bash
# 현재 ET 시각 및 장 상태 확인
python -c "
from datetime import datetime
import zoneinfo
now = datetime.now(zoneinfo.ZoneInfo('America/New_York'))
print(f'현재 ET: {now.strftime(\"%Y-%m-%d %H:%M\")}')
"
```

장중이라면 60초마다 `[MONITOR] 장중 (ET HH:MM)` 로그가 출력됩니다.
장 외라면 `[MONITOR] 장 개장까지 N시간 N분` 로그가 출력되며 정상 대기 상태입니다.

### 5-2. B4가 진입을 안 함

```bash
# 쿨다운 여부 확인
sqlite3 "storage/db/trading_data.db" "SELECT * FROM b4_cooldown WHERE end_date >= DATE('now');"

# 평균 거래량 초기화 실패 시 로그 확인
grep "평균 거래량" logs/$(date +%y%m)/$(date +%Y-%m-%d).log
# "[B4] QQQ 거래량 데이터 없음 (장 외 또는 API 미응답)" → 장중 재시작 필요

# B4 DB 테이블 존재 확인
sqlite3 "storage/db/trading_data.db" ".tables"
# 없으면:
python -c "from storage import db_manager; db_manager.init_db()"
```

### 5-3. B3가 진입을 안 함

```bash
# 1. 카탈리스트 종목이 없는 날 (정상) — 주 2~3일은 B3 진입 없을 수 있음
# 2. 9:40 ET 이전 확인 중 — 스캔 전이므로 대기
# 3. Alpaca API 연결 확인
python -c "
from data.alpaca_bars import fetch_bars
df = fetch_bars('SPY', '5Min', 5)
print(df)
"
```

### 5-4. Gemini API 응답 없음

```bash
# 1. 환경변수 확인
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv('GEMINI_API_KEY', '없음')[:10])"

# 2. 패키지 확인 (google-genai 필요)
python -c "from google import genai; print('OK')"
# ModuleNotFoundError → pip install google-genai

# 3. 직접 테스트
python -c "
from dotenv import load_dotenv; load_dotenv()
from ai.gemini_helper import call_gemini, GeminiTask
print(call_gemini('테스트', task=GeminiTask.STOCK_ANALYSIS, max_tokens=10))
"
```

> Gemini 실패 시 뉴스 감성 분석은 키워드 기반으로 자동 폴백되어 봇 동작에는 영향 없음.

### 5-5. yfinance 타임아웃으로 B1/B4 멈춤

**증상**: `[B4] QQQ 거래량 갱신 타임아웃(20s)` 또는 `[B1] XXXX yfinance 타임아웃`

20초 타임아웃 후 자동 스킵됩니다. 특정 종목이 계속 타임아웃이면 watchlist에서 제거하세요.

### 5-6. 텔레그램 봇 응답 없음

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

### 5-7. DB 잠금 오류

```bash
lsof storage/trade.db
sqlite3 storage/trade.db "PRAGMA journal_mode;"   # wal 이어야 정상
```

### 5-8. 승률이 백테스트보다 낮음

페이퍼/실전에서 백테스트 대비 5~15% 낮은 것은 정상입니다.

| 원인 | 대응 |
|------|------|
| 슬리피지 | Paper 모드 자동 반영 (+0.1%) |
| 갭앤크랩 (백테스트에서 놓침) | `min_gap_pct` 상향 |
| 장 시작 5분 스프레드 | 9:32 이후 진입 |
| 백테스트 look-ahead bias | 결과 20% 할인 적용 |

### 5-9. 키 명령어 모음

```bash
# 현재 포지션
sqlite3 storage/trade.db "SELECT * FROM positions WHERE status='open';"

# 이번 달 손익
sqlite3 storage/trade.db "
  SELECT date, SUM(pnl) daily_pnl, COUNT(*) trades
  FROM closed_trades
  WHERE date >= strftime('%Y-%m-01','now')
  GROUP BY date ORDER BY date;"

# 버킷별 승률
sqlite3 storage/trade.db "
  SELECT strategy, COUNT(*) total,
         SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
         ROUND(AVG(pnl_pct),2) avg_pnl_pct
  FROM closed_trades GROUP BY strategy;"

# DB 초기화 (페이퍼 재시작 시)
mv storage/trade.db storage/trade.db.bak
```

---

## 6. 빠른 참조

```bash
# 페이퍼 실행
python main.py

# 오늘 로그 실시간
tail -f logs/$(date +%y%m)/$(date +%Y-%m-%d).log

# 현재 포지션
sqlite3 storage/trade.db "SELECT symbol,strategy,entry_price,qty FROM positions WHERE status='open';"

# B4 쿨다운 상태
sqlite3 "storage/db/trading_data.db" "SELECT * FROM b4_cooldown WHERE end_date >= DATE('now');"

# B4 거래 내역 (최근 20건)
sqlite3 "storage/db/trading_data.db" "SELECT symbol,result_pct,exit_reason,trade_date FROM b4_trades ORDER BY trade_date DESC LIMIT 20;"

# Gemini 연결 테스트
python -c "from dotenv import load_dotenv; load_dotenv('.env'); from ai.gemini_helper import ask_gpt; print(ask_gpt('안녕'))"
```
