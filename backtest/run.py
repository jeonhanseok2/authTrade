# backtest/run.py
"""
버킷별 백테스트 CLI 러너.

사용법:
  # B2 ETF 스윙 — 일봉 1년
  python -m backtest.run --bucket etf_swing --days 365

  # B3 급등주 — 1분봉 60일 (yfinance 최대)
  python -m backtest.run --bucket squeeze --days 60

  # B1 가치주 — 일봉 1년
  python -m backtest.run --bucket value_long --days 365

  # 특정 종목 직접 지정
  python -m backtest.run --bucket squeeze --symbols TSLA AMD NVDA MARA --days 60

  # 초기 자본 변경
  python -m backtest.run --bucket etf_swing --cash 14800

  # CSV 저장
  python -m backtest.run --bucket etf_swing --csv results/etf_swing.csv

데이터 한계:
  yfinance 1분봉 → 최근 60일만 제공
  yfinance 일봉  → 5년치 가능
  갭업 필터(B3)  → open/prev_close 비율로 근사
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import yaml

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import run_backtest
from backtest.report import print_report, to_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ─────────────────────────────────────────────────────────────────────
# 기본 종목 목록
# ─────────────────────────────────────────────────────────────────────

DEFAULT_SYMBOLS = {
    "value_long": ["AAPL", "MSFT", "JNJ", "KO", "PG", "BRK-B", "JPM", "V", "WMT", "HD"],
    "etf_swing":  ["SPY", "QQQ", "TQQQ", "SOXL", "IWM", "XLK", "XLF", "XLE", "ARKK", "GLD"],
    "squeeze":    ["TSLA", "NVDA", "AMD", "MARA", "RIOT", "SOUN", "MSTR", "GME", "AMC", "PLTR"],
}


# ─────────────────────────────────────────────────────────────────────
# 데이터 페치
# ─────────────────────────────────────────────────────────────────────

def fetch_data(symbols: list[str], days: int, interval: str) -> dict[str, pd.DataFrame]:
    """Alpaca Historical Data API로 OHLCV 데이터 페치."""
    from data.alpaca_bars import fetch_bars

    _tf_map = {
        "1d": ("1Day",  days),
        "1m": ("1Min",  days * 390),
        "5m": ("5Min",  days * 78),
    }
    tf, limit = _tf_map.get(interval, ("1Day", days))

    dfs = {}
    logging.info("데이터 다운로드: %d종목 / %s / %dd", len(symbols), interval, days)

    for sym in symbols:
        try:
            df = fetch_bars(sym, tf, limit)
            if df is None or df.empty:
                logging.warning("%s: 데이터 없음", sym)
                continue
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) < 20:
                logging.warning("%s: 데이터 부족 (%d봉)", sym, len(df))
                continue
            dfs[sym] = df
            logging.info("  %s: %d봉", sym, len(df))
        except Exception as e:
            logging.warning("%s: 페치 실패 — %s", sym, e)

    return dfs


# ─────────────────────────────────────────────────────────────────────
# 버킷별 설정 로더
# ─────────────────────────────────────────────────────────────────────

def load_bucket_cfg(bucket: str, config_path: str = "config.yaml") -> tuple[dict, dict]:
    """config.yaml에서 버킷별 + risk 설정 로드."""
    try:
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}

    risk_cfg = cfg.get("risk", {
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.10,
        "trailing_stop_pct": 0.02,
        "trail_after_profit_pct": 0.10,
        "rsi_overbought_exit": 80.0,
        "eod_exit_minutes_before_close": 15,
        "atr_multiplier": 2.0,
        "per_trade_risk_pct": 0.01,
    })

    if bucket == "squeeze":
        mom_cfg = cfg.get("momentum_rules", {
            "lookback_minutes": 120,
            "min_intraday_change_pct": 5.0,
            "vol_spike_ratio": 2.0,
            "min_price_usd": 3.0,
            "rsi_entry_max": 75.0,
            "require_macd_positive": True,
        })
        # B3: 더 공격적인 손익 설정
        risk_cfg = dict(risk_cfg)
        risk_cfg.setdefault("stop_loss_pct",      cfg.get("squeeze", {}).get("scalp_sl_pct", 0.05))
        risk_cfg.setdefault("take_profit_pct",    cfg.get("squeeze", {}).get("scalp_tp_pct", 0.20))
        risk_cfg.setdefault("atr_multiplier",     cfg.get("squeeze", {}).get("atr_multiplier", 3.0))

    elif bucket == "etf_swing":
        mom_cfg = {
            "lookback_minutes": 30,        # 30일봉 기준 모멘텀 윈도우
            "min_intraday_change_pct": 3.0, # 30일 3% 이상 상승 시 진입
            "vol_spike_ratio": 1.0,         # ETF는 거래량 스파이크 없음 → 비활성화
            "min_price_usd": 1.0,
            "rsi_entry_max": 68.0,
            "require_macd_positive": True,
        }
        risk_cfg = dict(risk_cfg)
        risk_cfg["stop_loss_pct"]   = cfg.get("etf_swing", {}).get("swing_sl_pct", 0.04)
        risk_cfg["take_profit_pct"] = cfg.get("etf_swing", {}).get("swing_tp_pct", 0.08)

    else:  # value_long
        mom_cfg = {
            "lookback_minutes": 20 * 390,
            "min_intraday_change_pct": 1.0,
            "vol_spike_ratio": 1.2,
            "min_price_usd": 5.0,
            "rsi_entry_max": 30.0,  # 과매도 구간만
            "require_macd_positive": False,
        }
        risk_cfg = dict(risk_cfg)
        risk_cfg["stop_loss_pct"]   = cfg.get("value_long", {}).get("stop_loss_pct", 0.08)
        risk_cfg["take_profit_pct"] = cfg.get("value_long", {}).get("take_profit_pct", 0.25)

    return mom_cfg, risk_cfg


# ─────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="버킷별 백테스트 러너")
    parser.add_argument("--bucket",  default="etf_swing",
                        choices=["value_long", "etf_swing", "squeeze"],
                        help="백테스트할 버킷 (기본: etf_swing)")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="테스트 종목 (미지정 시 버킷 기본 종목 사용)")
    parser.add_argument("--days",    type=int, default=None,
                        help="백테스트 기간 (일). 미지정 시 버킷별 기본값 사용")
    parser.add_argument("--cash",    type=float, default=14_800.0,
                        help="초기 자본 USD (기본: $14,800)")
    parser.add_argument("--csv",     default=None,
                        help="결과 CSV 저장 경로 (예: results/etf_swing.csv)")
    parser.add_argument("--config",  default="config.yaml",
                        help="설정 파일 경로")
    args = parser.parse_args()

    bucket  = args.bucket
    symbols = args.symbols or DEFAULT_SYMBOLS[bucket]

    # 버킷별 기본 기간/인터벌
    if args.days:
        days = args.days
    else:
        days = 60 if bucket == "squeeze" else 365

    # yfinance 인터벌 제한:
    #   1m  → 최근 7일만
    #   5m  → 최근 60일
    #   1d  → 5년+
    if bucket == "squeeze":
        interval = "5m"
        if days > 60:
            logging.warning("yfinance 5분봉은 최대 60일. days=60으로 조정합니다.")
            days = 60
    else:
        interval = "1d"

    print(f"\n{'='*55}")
    print(f"  백테스트: {bucket.upper()}")
    print(f"  종목: {', '.join(symbols)}")
    print(f"  기간: {days}일  |  인터벌: {interval}  |  자본: ${args.cash:,.0f}")
    print(f"{'='*55}\n")

    # 데이터 페치
    dfs = fetch_data(symbols, days=days, interval=interval)
    if not dfs:
        print("ERROR: 데이터를 가져올 수 없습니다.")
        sys.exit(1)

    # 버킷 설정 로드
    mom_cfg, risk_cfg = load_bucket_cfg(bucket, config_path=args.config)

    # 백테스트 실행
    max_pos = {"value_long": 2, "etf_swing": 3, "squeeze": 3}[bucket]
    result  = run_backtest(
        symbol_dfs          = dfs,
        mom_cfg             = mom_cfg,
        risk_cfg            = risk_cfg,
        initial_cash        = args.cash,
        max_positions       = max_pos,
        risk_per_trade_pct  = float(risk_cfg.get("per_trade_risk_pct", 0.01)),
        atr_multiplier      = float(risk_cfg.get("atr_multiplier", 2.0)),
    )

    # 결과 출력
    print_report(result)

    # 버킷 배분 기준 수익 환산
    bucket_pct = {"value_long": 0.10, "etf_swing": 0.40, "squeeze": 0.50}[bucket]
    bucket_cap = args.cash * bucket_pct
    if result["trades"]:
        actual_pnl_pct = result["total_pnl"] / args.cash * 100
        print(f"  버킷 비중 기준 ({bucket_pct*100:.0f}%): 할당자본 ${bucket_cap:,.0f}")
        print(f"  전체 계좌 환산 수익: {actual_pnl_pct:+.2f}%\n")

    # CSV 저장
    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        to_csv(result, args.csv)

    # 청산 사유 분포
    df = result.get("summary_df")
    if df is not None and not df.empty:
        print("  [청산 사유 분포]")
        for reason, cnt in df["exit_reason"].value_counts().items():
            pnl_avg = df[df["exit_reason"] == reason]["pnl_pct"].mean()
            print(f"    {reason:<20}: {cnt}건  평균 {pnl_avg:+.1f}%")
        print()


if __name__ == "__main__":
    main()
