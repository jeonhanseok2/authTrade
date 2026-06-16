# main.py
"""
authTrade — 비동기 이벤트 기반 자동매매 시스템 진입점.

브로커 선택 (BROKER 환경변수):
  BROKER=alpaca  (기본) — Alpaca Markets API + WebSocket
  BROKER=toss           — 토스증권 Open API + 1초 폴링

실행 모드 (MODE 환경변수):
  MODE=paper (기본) — PaperSimBroker (실제 주문 없음, 토스 포함)
  MODE=live         — 실전 거래

태스크 구조:
  exit_task    : 전 버킷 청산 체크      — 30초 주기
  monitor_task : 킬스위치 + VIX + 리밸  — 60초 주기
  bucket1      : 가치주 장기투자         — 60분 주기
  bucket2      : ETF 스윙               — 15분 주기
  bucket3      : 급등주 초단타           — WebSocket(Alpaca) / 1초 폴링(Toss)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import yaml
from dotenv import load_dotenv

from core.kill_switch    import KillSwitch
from core.bucket_capital import BucketCapitalManager
from core.orchestrator   import Orchestrator
from storage.db          import PositionDB
from utils.logging       import setup_logging


def load_cfg(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_watchlist(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        logging.warning("워치리스트 없음: %s", path)
        return []


def _init_alpaca(mode: str):
    """Alpaca 브로커 + 데이터 클라이언트 초기화."""
    from alpaca.trading.client        import TradingClient
    from alpaca.data.historical       import StockHistoricalDataClient
    from trader.execution             import AlpacaBroker

    api_key  = os.getenv("ALPACA_API_KEY", "")
    secret   = os.getenv("ALPACA_SECRET_KEY", "")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    if not api_key or not secret:
        logging.error("ALPACA_API_KEY / ALPACA_SECRET_KEY 미설정")
        sys.exit(1)

    is_paper    = (mode == "paper") or (base_url != "https://api.alpaca.markets")
    broker      = AlpacaBroker(api_key, secret, paper=is_paper)
    data_client = StockHistoricalDataClient(api_key, secret)

    acct   = broker.get_account()
    equity = float(acct.get("portfolio_value") or 0)
    return broker, data_client, equity, "PAPER" if is_paper else "LIVE"


def _init_toss(mode: str):
    """토스증권 브로커 초기화 (MODE=paper → PaperSimBroker)."""
    if mode == "paper":
        from trader.paper import PaperSimBroker
        broker = PaperSimBroker()
        equity = float(os.getenv("TOSS_PAPER_EQUITY", "14800"))
        logging.warning("[Toss] MODE=paper → PaperSimBroker (실제 주문 없음, 샌드박스 미지원)")
        return broker, None, equity, "PAPER(Toss)"

    client_id     = os.getenv("TOSS_CLIENT_ID", "")
    client_secret = os.getenv("TOSS_CLIENT_SECRET", "")
    account_seq   = os.getenv("TOSS_ACCOUNT_SEQ", "")

    if not client_id or not client_secret or not account_seq:
        logging.error("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET / TOSS_ACCOUNT_SEQ 미설정")
        sys.exit(1)

    from trader.toss import TossInvestBroker
    broker = TossInvestBroker(client_id, client_secret, int(account_seq))

    try:
        balance = broker.get_balance()
        equity  = float(balance.get("portfolio_value") or 0)
    except Exception as exc:
        logging.error("토스 계좌 조회 실패: %s", exc)
        sys.exit(1)

    return broker, None, equity, "LIVE(Toss)"


async def main() -> None:
    load_dotenv()
    setup_logging()

    cfg    = load_cfg()
    broker_type = os.getenv("BROKER", "alpaca").lower()  # alpaca | toss
    mode        = os.getenv("MODE",   "paper").lower()   # paper  | live

    # ── 브로커 초기화 ─────────────────────────────────────────────────
    if broker_type == "toss":
        broker, data_client, equity, label = _init_toss(mode)
    else:
        broker, data_client, equity, label = _init_alpaca(mode)

    # ── 인프라 초기화 ─────────────────────────────────────────────────
    db          = PositionDB(cfg["storage"]["db_path"])
    kill_switch = KillSwitch(
        daily_loss_limit_pct=float(cfg.get("risk", {}).get("daily_loss_limit_pct", 0.02))
    )
    bucket_capital = BucketCapitalManager(total_equity=max(equity, 1.0))

    # ── 오케스트레이터 ────────────────────────────────────────────────
    orch = Orchestrator(
        broker=broker,
        data_client=data_client,
        db=db,
        cfg=cfg,
        kill_switch=kill_switch,
        bucket_capital=bucket_capital,
    )

    # 토스 브로커는 WebSocket 없음 → 항상 PollingStream 사용
    # (paper 모드도 마찬가지: PaperSimBroker가 주문 실행, PollingStream이 시세 조회)
    if broker_type == "toss":
        from core.polling_stream import PollingStream
        # paper 모드에서는 Toss 실제 API로 시세는 조회할 수 없으므로 broker=None으로 빈 스트림
        stream_broker = broker if mode == "live" else None
        if stream_broker is not None:
            orch._stream = PollingStream(
                broker   = stream_broker,
                on_bar   = orch.on_bar,
                on_quote = orch.on_quote,
            )
            logging.info("[Toss] B3 스트림 → 1초 폴링 (live)")
        else:
            logging.info("[Toss] MODE=paper: 폴링 스트림 비활성 (실제 시세 조회 없음)")

    # ── 워치리스트 로드 ───────────────────────────────────────────────
    b1_syms = load_watchlist(cfg["value_long"].get("watchlist_file", "watchlists/value_symbols.txt"))
    b3_syms = load_watchlist(cfg["squeeze"].get("watchlist_file",    "watchlists/symbols.txt"))

    # ── 시작 로그 ─────────────────────────────────────────────────────
    logging.info("=" * 60)
    logging.info("authTrade 시작 [%s / %s] — 계좌잔고 $%.0f", broker_type.upper(), label, equity)
    logging.info(
        "버킷 초기비중: B1=%.0f%% B2=%.0f%% B3=%.0f%%",
        bucket_capital.weights["value_long"] * 100,
        bucket_capital.weights["etf_swing"]  * 100,
        bucket_capital.weights["squeeze"]    * 100,
    )
    logging.info(
        "할당금액: B1=$%.0f  B2=$%.0f  B3=$%.0f",
        bucket_capital.allocated("value_long"),
        bucket_capital.allocated("etf_swing"),
        bucket_capital.allocated("squeeze"),
    )
    logging.info("=" * 60)

    # ── 비동기 태스크 동시 실행 ───────────────────────────────────────
    tasks = [
        orch.run_exit_loop(),              # 30초 — 전 버킷 청산 체크
        orch.run_monitor_loop(),           # 60초 — 킬스위치 + VIX RoC
        orch.run_bucket1_loop(b1_syms),    # 60분 — 가치주 장기
        orch.run_bucket2_loop(),           # 15분 — ETF 스윙
        orch.run_bucket3_stream(b3_syms),  # B3 — PollingStream(Toss) / WebSocket(Alpaca)
    ]
    # Toss 브로커: 세션 감시 워치독 추가 (세션 끊기면 킬스위치 + 텔레그램)
    if broker_type == "toss" and mode == "live":
        from core.session_watchdog import run_session_watchdog
        notifier = getattr(orch, "notifier", None)
        tasks.append(run_session_watchdog(broker, kill_switch, notifier))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("사용자 종료 (Ctrl+C)")
