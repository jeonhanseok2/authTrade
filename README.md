# authTrade — Alpaca 자동매매 봇

미국 주식 3-버킷 비동기 알고리즘 트레이딩 시스템.  
Alpaca Markets API + Google Gemini AI + Telegram 양방향 제어.

---

## 아키텍처

```
main.py
└── asyncio.gather()
    ├── Orchestrator.run_exit_loop()      # 30초 — 청산 감시
    ├── Orchestrator.run_monitor_loop()   # 60초 — 계좌/VIX/레짐/일지
    ├── Orchestrator.run_bucket1_loop()   # 60분 — 가치주 스캔
    ├── Orchestrator.run_bucket2_loop()   # 15분 — ETF 스윙
    └── Orchestrator.run_bucket3_ws()     # 실시간 WebSocket — 급등주
```

---

## 버킷 구조 (B1 : B2 : B3 = 1 : 4 : 5)

| 버킷 | 비중 | 전략 | 주기 | 데이터 |
|------|------|------|------|--------|
| B1 가치주 | 10% | 펀더멘털 + DCF 안전마진 + RSI < 30 | 60분 스캔 | 일봉 |
| B2 ETF 스윙 | 40% | MACD + RSI + 거래량 추세 | 15분 스캔 | 분봉 |
| B3 급등주 | 50% | 프리마켓 갭업 20%+ / RVOL 5x+ / Gap&Go | WebSocket 실시간 | 1분봉 |

> 계좌 $25,000 초과 시 B1 비중 30%로 상향, Margin 계좌 전환 권장

---

## 진입 조건

### B1 — 가치주
- 펀더멘털 점수 >= 55 (PER/PBR/ROE/EPS/부채 종합)
- DCF 안전마진 >= 10%
- RSI < 30 (과매도 구간만 진입)
- 레짐 panic 제외, 뉴스 bearish 강도 -0.3 이하 차단

### B2 — ETF 스윙
- MACD 히스토그램 양수
- RSI < 70
- 거래량 20일 평균 대비 0.5x 이상

### B3 — 급등주 (Gap&Go / Short Squeeze)
- 프리마켓 갭업 20% 이상
- RVOL 5x 이상 (장중 진입 10x)
- Float 5,000만주 이하
- 첫 5분봉 거래량 >= 프리마켓 평균 × 5배 (Gap&Go 볼륨 팩터)
- Short비율 15% + Days to Cover 3일 이상 (Short Squeeze 후보)

---

## 청산 조건 (우선순위 순)

1. **Hard Stop (갭다운)** — 시초가가 진입가 대비 -stop_pct 이상 갭다운 시 즉시 시장가 청산
2. **Effective Stop** — `max(고정손절가, ATR×배수)` — 더 높은(타이트한) 쪽 적용
3. **익절 목표가** — 버킷별 take_profit_pct 도달
4. **트레일링 스탑** — +10% 도달 후 고점 대비 -2% 이탈
5. **RSI 과매수** — RSI >= 80 모멘텀 소진
6. **Bid-Ask 스프레드** — >= 1.5% 확산 시 B3 즉시 청산 (WebSocket)
7. **EOD** — 장 마감 15분 전 인트라데이 포지션 청산

---

## 리스크 관리

| 항목 | 내용 |
|------|------|
| 일손실 한도 | 계좌 대비 -2% (미실현 기준) → 킬스위치 ON |
| VIX 절대값 | >= 30 → 신규 진입 차단 |
| VIX 변화율 | 전일 대비 +20% 이상 급등 → 선제 차단 |
| 자금 격리 | 버킷별 독립 예산, 손실이 타 버킷 예산 침범 불가 |
| Panic 레짐 | B3 전량청산 + B2 인버스 ETF(SQQQ/SDS) 헤지 |
| 섹터 집중도 | 동일 섹터 최대 3포지션 |

---

## AI 연동 (Google Gemini)

**Flash 모델** — 빠른 일상 분석
- 뉴스 요약, 시장 브리핑, 종목 분석, 텔레그램 메시지 생성

**Pro 모델** — 고도 추론 (비용↑, 꼭 필요할 때만)
- 매매 전략 수정 권고
- 포트폴리오 리밸런싱 판단
- 큰 손실 발생 시 원인 분석
- 시장 레짐 전환 심층 분석 (Bull→Bear 등)
- 지표 충돌 시 최종 판단

---

## 매매 일지 & 통계

장 마감(16:05 ET) 자동 실행:
- 당일 청산 거래 집계 (PnL, 보유시간, 청산사유)
- Gemini Pro 분석 — 패턴 분석 + 내일 전략 방향 + 파라미터 조정 권고
- `daily_journal` DB 저장 + Telegram 자동 전송

매주 금요일 주간 분석:
- 전략별 누계 승률/손익
- 버킷 비중 조정 권고
- `weekly_analysis` DB 저장

저장 테이블: `positions` / `trades` / `closed_trades` / `daily_journal` / `weekly_analysis`

---

## Telegram 봇

봇 시작 시 `setMyCommands`로 명령어 자동완성 등록됨 (앱에서 `/` 입력 시 목록 표시).

| 명령어 | 기능 |
|--------|------|
| `/ping` | 봇 상태 확인 |
| `/account` | 계좌 잔고 |
| `/positions` | 보유 포지션 + 진입가 + 고점 |
| `/journal [날짜]` | 일일 매매 일지 생성 (AI 분석 포함) |
| `/weekly [날짜]` | 주간 분석 + 버킷 비중 조정 권고 |
| `/stats [30]` | 전략별/청산사유별 누계 통계 |
| `/scan` | 급등/저평가 종목 스캔 |
| `/ask 질문` | Gemini AI에게 질문 |
| `/buy TICKER QTY` | 수동 매수 |
| `/sell TICKER QTY` | 수동 매도 |
| `/search 키워드` | 종목 검색 |
| `/gptstatus` | Gemini API 연결 상태 |
| `/stop` | 봇 종료 |

---

## 백테스트

```bash
# B2 ETF 스윙 — 일봉 1년 (권장 시작점)
python -m backtest.run --bucket etf_swing --days 365

# B3 급등주 — 5분봉 60일
python -m backtest.run --bucket squeeze --symbols TSLA AMD NVDA MARA PLTR --days 60

# B1 가치주 — 일봉 1년
python -m backtest.run --bucket value_long --days 365

# 초기 자본 / CSV 저장
python -m backtest.run --bucket etf_swing --cash 14800 --csv results/etf.csv
```

**yfinance 데이터 한계:**

| 인터벌 | 최대 기간 | 사용 버킷 |
|--------|-----------|-----------|
| 5분봉 | 60일 | B3 급등주 |
| 일봉 | 5년+ | B1, B2 |

**백테스트 결과 예시 (참고용, 미래 수익 보장 아님):**

| 버킷 | 기간 | 승률 | 손익 | 최대낙폭 |
|------|------|------|------|---------|
| B2 ETF 스윙 | 1년 일봉 | 55% | +$1,230 | -7.6% |
| B3 급등주 | 60일 5분봉 | 65% | +$2,338 | -6.2% |

> 슬리피지 미반영 — 실전 수익은 5~10% 낮게 산정 권장

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

# 4. 텔레그램 봇 별도 실행
python -m notify.telegram_bot
```

---

## 환경변수 (.env)

```env
# Alpaca
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # 실전: https://api.alpaca.markets

# Google Gemini
GEMINI_API_KEY=...
# GEMINI_FLASH_MODEL=gemini-1.5-flash  (기본값)
# GEMINI_PRO_MODEL=gemini-1.5-pro      (기본값)

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# 실행 모드
MODE=paper  # paper | live
```

---

## 프로젝트 구조

```
authTrade/
├── main.py                   # 진입점 — asyncio 5태스크 실행
├── config.yaml               # 전략 파라미터 (손절/익절/버킷 비중 등)
├── core/
│   ├── orchestrator.py       # 비동기 루프 조율자
│   ├── bucket_capital.py     # 버킷별 자금 격리 + 성과 기반 리밸런싱
│   ├── kill_switch.py        # 일손실 한도 킬스위치
│   └── websocket_stream.py   # Alpaca WebSocket (B3 실시간)
├── strategy/
│   ├── entries.py            # 진입 조건 함수
│   ├── exits.py              # 청산 조건 함수
│   ├── value_long.py         # B1 가치주 전략
│   ├── squeeze.py            # B3 급등주/스퀴즈 전략
│   ├── signals.py            # RSI/MACD/ATR/BB 지표
│   └── sizing.py             # ATR 기반 포지션 사이징
├── ai/
│   └── gemini_helper.py      # Gemini Flash/Pro 듀얼 모델 라우터
├── storage/
│   ├── db.py                 # SQLite (포지션/거래/일지)
│   └── journal.py            # 일일 일지 생성 + AI 분석
├── notify/
│   ├── telegram_bot.py       # 양방향 Telegram 봇
│   └── telegram_notifier.py  # 단방향 알림 push
├── backtest/
│   ├── engine.py             # 바-단위 이벤트 드리븐 엔진
│   ├── run.py                # CLI 러너
│   └── report.py             # 결과 출력/CSV
├── risk/
│   └── guard.py              # 서킷브레이커 + VIX 가드
└── tests/                    # pytest 단위 테스트
```

---

## 단위 테스트

```bash
pytest tests/ -v
```
