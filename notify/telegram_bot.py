# notify/telegram_bot.py
# -*- coding: utf-8 -*-
"""
텔레그램 명령으로 트레이딩 상태를 조회/제어하는 봇 (Long Polling).
실행: MODE=paper python -m notify.telegram_bot --minutes 600
"""

import hashlib
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from typing import Dict, List, Optional

import requests
import yaml
from dotenv import load_dotenv

from data.assets import ensure_asset_cache, search_assets

# ── 환경 상수 ─────────────────────────────────────────────────────────

ASK_COOLDOWN_SEC    = int(os.getenv("ASK_COOLDOWN_SEC",    "15"))
MAX_GPT_QPS         = float(os.getenv("MAX_GPT_QPS",       "0.2"))   # 5초/건
MAX_GPT_CONCURRENCY = int(os.getenv("MAX_GPT_CONCURRENCY", "1"))
PROMPT_CACHE_TTL    = int(os.getenv("PROMPT_CACHE_TTL",    "120"))

# ── 스레드 안전 글로벌 상태 ────────────────────────────────────────────

_STATE_LOCK    = threading.Lock()
_CHAT_COOLDOWN: Dict[str, float] = {}
_PROMPT_CACHE:  Dict[str, tuple] = {}   # key -> (ts, answer)

REQ_QUEUE   = queue.Queue()             # thread-safe
LAST_CALL_TS = 0.0
CONC_SEM    = threading.BoundedSemaphore(MAX_GPT_CONCURRENCY)


# ── 캐시 / 쿨다운 헬퍼 ───────────────────────────────────────────────

def _cache_key(chat_id: str, prompt: str) -> str:
    return hashlib.sha256((chat_id + "|" + prompt).encode()).hexdigest()


def _get_cached(chat_id: str, prompt: str) -> Optional[str]:
    k = _cache_key(chat_id, prompt)
    with _STATE_LOCK:
        v = _PROMPT_CACHE.get(k)
        if not v:
            return None
        ts, ans = v
        if time.time() - ts > PROMPT_CACHE_TTL:
            _PROMPT_CACHE.pop(k, None)
            return None
        return ans


def _set_cache(chat_id: str, prompt: str, answer: str) -> None:
    with _STATE_LOCK:
        _PROMPT_CACHE[_cache_key(chat_id, prompt)] = (time.time(), answer)


def _allow_chat(chat_id: str, cooldown_sec: int = ASK_COOLDOWN_SEC) -> bool:
    now = time.time()
    with _STATE_LOCK:
        last = _CHAT_COOLDOWN.get(chat_id, 0)
        if now - last < cooldown_sec:
            return False
        _CHAT_COOLDOWN[chat_id] = now
        return True


# ── GPT 워커 (백그라운드 스레드) ──────────────────────────────────────

def _worker_loop(bot_token: str) -> None:
    global LAST_CALL_TS
    while True:
        try:
            chat_id, prompt = REQ_QUEUE.get(timeout=0.05)
        except queue.Empty:
            continue

        min_gap    = 1.0 / max(0.0001, MAX_GPT_QPS)
        sleep_need = LAST_CALL_TS + min_gap - time.time()
        if sleep_need > 0:
            time.sleep(sleep_need)

        with CONC_SEM:
            ans = ask_gpt(prompt)
            LAST_CALL_TS = time.time()

        tg_send(bot_token, chat_id, f"🤖 Gemini 응답:\n{ans}")
        _set_cache(chat_id, prompt, ans)
        REQ_QUEUE.task_done()


# ── 로깅 ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── 내부 모듈 경로 보정 ───────────────────────────────────────────────

try:
    from config import load_mode_env
    from data.fetch import fetch_recent_bars
    from data.fundamentals import fetch_quick_fundamentals
    from storage.db import PositionDB
    from storage import db_manager as dbm
    from strategy.entries import momentum_entry, value_entry
except ModuleNotFoundError:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import load_mode_env
    from data.fetch import fetch_recent_bars
    from data.fundamentals import fetch_quick_fundamentals
    from storage.db import PositionDB
    from storage import db_manager as dbm
    from strategy.entries import momentum_entry, value_entry

# ── 브로커 ───────────────────────────────────────────────────────────

USE_PAPER_SIM = False
try:
    from trader.execution import AlpacaBroker
except Exception:
    USE_PAPER_SIM = True

from trader.paper import PaperSimBroker
from alpaca.data.historical import StockHistoricalDataClient

# ── Gemini ───────────────────────────────────────────────────────────

try:
    from ai.gemini_helper import ask_gpt
except Exception as _e:
    logging.warning("[telegram_bot] ai.gemini_helper import 실패: %s", _e)

    def ask_gpt(prompt: str, max_tokens: int = 256) -> str:  # type: ignore[misc]
        return f"Gemini 모듈 로드 실패 — ai/gemini_helper.py 확인 필요"


# ── 텔레그램 유틸 ─────────────────────────────────────────────────────

BOT_COMMANDS = [
    {"command": "ping",       "description": "봇 상태 확인"},
    {"command": "status",     "description": "현재 모드·그룹·오늘 성적·보유 종목 요약"},
    {"command": "set_mode",   "description": "모드 강제 전환 (예: /set_mode B3 또는 B2)"},
    {"command": "set_b4",     "description": "B4 스나이퍼 설정 (예: /set_b4 on|off|30|50)"},
    {"command": "account",    "description": "계좌 잔고 조회"},
    {"command": "positions",  "description": "보유 포지션 + 진입가 + 고점"},
    {"command": "journal",    "description": "일일 매매 일지 (날짜 미입력 시 오늘)"},
    {"command": "weekly",     "description": "주간 분석 (버킷 비중 조정 권고)"},
    {"command": "stats",      "description": "누계 통계 (기본 30일, 예: /stats 7)"},
    {"command": "scan",       "description": "급등/저평가 종목 스캔"},
    {"command": "ask",        "description": "AI에게 질문 (예: /ask TSLA 지금 매수할까?)"},
    {"command": "buy",        "description": "수동 매수 (예: /buy TSLA 5)"},
    {"command": "sell",       "description": "수동 매도 (예: /sell TSLA 5)"},
    {"command": "search",     "description": "종목 검색 (예: /search nvidia)"},
    {"command": "gptstatus",  "description": "Gemini API 연결 상태 확인"},
    {"command": "help",       "description": "명령 목록 보기"},
    {"command": "stop",       "description": "봇 종료"},
]


def tg_register_commands(bot_token: str) -> bool:
    """봇 시작 시 Telegram에 명령어 목록 등록 (앱에서 / 입력 시 자동완성)."""
    url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
    try:
        r = requests.post(url, json={"commands": BOT_COMMANDS}, timeout=8)
        if r.status_code == 200 and r.json().get("result"):
            logging.info("Telegram 명령어 자동완성 등록 완료 (%d개)", len(BOT_COMMANDS))
            return True
        logging.warning("setMyCommands 실패: %s", r.text[:200])
        return False
    except Exception as e:
        logging.warning("setMyCommands 오류: %s", e)
        return False


def tg_send(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=8,
        )
        if r.status_code != 200:
            logging.warning("sendMessage failed: %s", r.text[:200])
    except Exception as e:
        logging.warning("sendMessage error: %s", e)


def tg_get_updates(bot_token: str, offset: Optional[int]) -> dict:
    url    = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=(5, 30))
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ReadTimeout:
        logging.info("getUpdates read timeout — continuing")
        return {"ok": True, "result": []}
    except requests.exceptions.ConnectTimeout:
        logging.warning("getUpdates connect timeout — backing off")
        time.sleep(1.0)
        return {"ok": False, "result": []}
    except requests.exceptions.ConnectionError as e:
        logging.warning("getUpdates connection error: %s", e)
        time.sleep(1.0)
        return {"ok": False, "result": []}
    except Exception as e:
        logging.warning("getUpdates error: %s", e)
        return {"ok": False, "result": []}


# ── 트레이딩 헬퍼 ─────────────────────────────────────────────────────

def build_clients():
    load_mode_env()
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    paper   = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    if USE_PAPER_SIM or not api_key or not secret:
        logging.info("[WARN] Using PaperSimBroker (no live orders).")
        return PaperSimBroker(), None
    return AlpacaBroker(api_key, secret, paper=paper), StockHistoricalDataClient(api_key, secret)


def get_watchlist(cfg: Dict) -> List[str]:
    path     = cfg.get("universe", {}).get("watchlist_file", "watchlists/symbols.txt")
    fallback = cfg.get("universe", {}).get("fallback_symbols", ["AAPL", "MSFT", "SPY"])
    try:
        with open(path, "r") as f:
            ls = [x.strip().upper() for x in f if x.strip() and not x.startswith("#")]
            return ls or fallback
    except Exception:
        return fallback


def quick_scan(data_client, symbols: List[str], minutes: int, cfg: Dict):
    dfs = {}
    for s in symbols[:30]:
        try:
            dfs[s] = fetch_recent_bars(data_client, s, minutes=minutes) if data_client else None
        except Exception as e:
            logging.info("fetch %s failed: %s", s, e)
            dfs[s] = None
        time.sleep(0.05)
    mom_cfg    = cfg.get("momentum_rules", {})
    momentum   = [s for s, df in dfs.items() if df is not None and not df.empty and momentum_entry(df, mom_cfg)]
    fundamentals = fetch_quick_fundamentals(symbols[:40])
    val_cfg    = cfg.get("value_rules", {})
    value_list = [it["symbol"] for it in fundamentals if value_entry(it, val_cfg)]
    return momentum[:5], value_list[:10]


_STRATEGY_KR = {
    "B1_VALUE":      "B1 가치주",
    "B2_SWING":      "B2 스윙",
    "B3_AGGRESSIVE": "B3 급등주",
    "B4":            "B4 변동성",
    "momentum":      "모멘텀",
    "value":         "가치주",
}

_SEP = "─" * 22


def _strategy_label(strategy: str) -> str:
    return _STRATEGY_KR.get(strategy, strategy)


def fmt_positions(db: Optional["PositionDB"], broker_detail: list) -> str:
    """포지션을 한글로 보기좋게 포맷. broker_detail은 list_positions_detail() 결과."""
    # DB 포지션 → strategy 이름 맵
    db_map: Dict[str, dict] = {}
    if db:
        for r in (db.list_open_positions() or []):
            db_map[r["symbol"]] = r

    if not broker_detail and not db_map:
        return "💼 보유 포지션 없음"

    # broker_detail 없으면 DB 전용 표시
    items = broker_detail if broker_detail else [
        {"symbol": r["symbol"], "qty": r["qty"]} for r in db_map.values()
    ]

    lines = [f"💼 <b>보유 포지션 ({len(items)}종목)</b>"]
    for i, p in enumerate(items, 1):
        sym = p["symbol"]
        qty = int(p.get("qty", 0))
        db_row = db_map.get(sym, {})
        strategy = db_row.get("strategy", "")
        label    = _strategy_label(strategy)

        header = f"<b>{'①②③④⑤⑥⑦⑧⑨⑩'[i-1] if i <= 10 else str(i)} {sym}</b>"
        if label:
            header += f"  [{label}]"
        lines.append(_SEP)
        lines.append(header)

        entry = p.get("avg_entry") or db_row.get("entry_price")
        cur   = p.get("current_price")
        upl   = p.get("unrealized_pl")
        upct  = p.get("unrealized_pct")

        lines.append(f"  수량:    {qty:,}주")
        if entry:
            lines.append(f"  진입가:  ${entry:,.2f}")
        if cur:
            if entry and entry > 0:
                arrow = "▲" if cur >= entry else "▼"
                diff_pct = (cur - entry) / entry * 100
                lines.append(f"  현재가:  ${cur:,.2f}  {arrow}{diff_pct:+.1f}%")
            else:
                lines.append(f"  현재가:  ${cur:,.2f}")
        if upl is not None:
            sign = "+" if upl >= 0 else ""
            pct_str = f" ({upct:+.1f}%)" if upct is not None else ""
            lines.append(f"  미실현:  {sign}${upl:,.0f}{pct_str}")
        if not cur and db_row.get("peak_price"):
            lines.append(f"  고점가:  ${db_row['peak_price']:,.2f}")

    lines.append(_SEP)
    return "\n".join(lines)


# ── 제어 / 보안 ───────────────────────────────────────────────────────

RUN         = True
SEEN_UPDATES: set = set()
MAX_SEEN    = 2000


def _sigint_handler(signum, frame):
    global RUN
    RUN = False
    logging.info("SIGINT received. Stopping after current poll...")


signal.signal(signal.SIGINT, _sigint_handler)


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    import argparse
    global RUN

    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=600)
    args = parser.parse_args()

    mode = load_mode_env()
    load_dotenv(f".env.{mode}")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    allow_env = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
    allow_ids = [x.strip() for x in allow_env.split(",") if x.strip()]

    logging.info("mode=%s | TG_TOKEN=%s | allow_ids=%s", mode, "set" if bot_token else "missing", allow_ids)
    logging.info("GEMINI_KEY=%s flash=%s", "set" if os.getenv("GEMINI_API_KEY") else "missing",
                 os.getenv("GEMINI_FLASH_MODEL", "gemini-2.0-flash"))

    if not bot_token:
        logging.error("TELEGRAM_BOT_TOKEN 미설정")
        return

    cfg = {}
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

    broker, data_client = build_clients()

    # DB 초기화
    db_path = cfg.get("storage", {}).get("db_path", "storage/trade.db")
    try:
        db: Optional[PositionDB] = PositionDB(db_path)
    except Exception as e:
        logging.warning("PositionDB init failed: %s", e)
        db = None

    try:
        trading_client = getattr(broker, "trading_client", None)
    except Exception:
        trading_client = None

    ensure_asset_cache(trading_client, csv_fallback="data/assets_us_equities.csv")

    # 명령어 자동완성 등록 (앱에서 / 입력 시 목록 표시)
    tg_register_commands(bot_token)

    # 워커 스레드 시작 (루프 밖)
    threading.Thread(target=_worker_loop, args=(bot_token,), daemon=True).start()
    logging.info("Telegram bot started. Send /start or /ping to your bot.")

    offset = None
    while RUN:
        updates = tg_get_updates(bot_token, offset)
        if not updates.get("ok"):
            time.sleep(0.8)
            continue

        result = updates.get("result", [])
        if result:
            logging.info("updates=%d first_id=%d", len(result), result[0]["update_id"])

        for item in result:
            uid    = item["update_id"]
            offset = uid + 1
            if uid in SEEN_UPDATES:
                continue
            SEEN_UPDATES.add(uid)
            if len(SEEN_UPDATES) > MAX_SEEN:
                SEEN_UPDATES.clear()

            msg = item.get("message") or item.get("edited_message")
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text    = (msg.get("text") or "").strip()
            user    = msg.get("from", {}).get("username", "unknown")

            if allow_ids and chat_id not in allow_ids:
                tg_send(bot_token, chat_id, "권한이 없습니다.")
                logging.info("reject chat_id=%s user=%s", chat_id, user)
                continue
            if not text:
                continue

            logging.info("cmd from %s(%s): %s", user, chat_id, text)
            cmd, *rest = text.split()
            cmd = cmd.lower()

            # ── 명령 처리 ─────────────────────────────────────────

            if cmd == "/status":
                try:
                    # db_manager에서 상태 조회
                    cur_mode  = dbm.get_system_state("CURRENT_MODE", "알 수 없음")
                    cur_group = dbm.get_system_state("ACTIVE_GROUP",  "알 수 없음")
                    b2_alloc  = dbm.get_system_state("B2_ALLOC_MODE", "-")
                    b4_en     = dbm.get_system_state("B4_ENABLED",    "on")
                    b4_cap    = float(dbm.get_system_state("B4_CAPITAL", "0.30"))

                    # 오늘 매매 성적
                    trades_today = dbm.get_trades_today()
                    closed   = [t for t in trades_today if t["result"] is not None]
                    wins     = sum(1 for t in closed if t["result"] > 0)
                    losses   = len(closed) - wins
                    total_pnl = sum(t["result"] for t in closed) if closed else 0.0
                    pnl_str  = f"{total_pnl*100:+.2f}%" if closed else "없음"

                    # 보유 종목
                    holding_str = "없음"
                    if db:
                        positions = db.list_open_positions()
                        if positions:
                            holding_str = ", ".join(p["symbol"] for p in positions[:10])

                    mode_line = f"<b>{cur_mode}</b>"
                    if cur_mode == "B2_SWING":
                        mode_line += f"  (내부: {b2_alloc})"

                    b4_line = f"{'✅ ON' if b4_en == 'on' else '🔴 OFF'}  ({b4_cap*100:.0f}%)"
                    msg = (
                        f"📊 <b>봇 상태 요약</b>\n"
                        f"{_SEP}\n"
                        f"모드:      {mode_line}\n"
                        f"그룹:      <b>{cur_group}</b>\n"
                        f"B4 스나이퍼: {b4_line}\n"
                        f"{_SEP}\n"
                        f"📈 오늘 매매\n"
                        f"  체결: {len(closed)}건  (수익 {wins} / 손실 {losses})\n"
                        f"  누계: {pnl_str}\n"
                        f"{_SEP}\n"
                        f"💼 보유: {holding_str}"
                    )
                    tg_send(bot_token, chat_id, msg)
                except Exception as e:
                    tg_send(bot_token, chat_id, f"상태 조회 오류: {e}")
                continue

            if cmd == "/set_mode":
                _mode_map = {
                    "B3": "B3_AGGRESSIVE", "B3_AGGRESSIVE": "B3_AGGRESSIVE",
                    "B2": "B2_SWING",      "B2_SWING":      "B2_SWING",
                }
                _mode_labels = {"B3_AGGRESSIVE": "B3 급등주", "B2_SWING": "B2 스윙"}

                # ── 인수 없음: 현재 상태 + 예산 표시 ─────────────────
                if not rest:
                    try:
                        cur = dbm.get_system_state("CURRENT_MODE", "B3_AGGRESSIVE")
                        locked = dbm.get_system_state("MODE_LOCKED", "off") == "on"
                        lock_str = "🔒 고정됨 (프리마켓 스캔 무시)" if locked else "🔓 자동 (프리마켓 스캔이 결정)"

                        # 계좌 + 예산 조회
                        try:
                            acct    = broker.get_account()
                            equity  = acct.get("equity", acct.get("portfolio_value", 0))
                            cash    = acct.get("cash", 0)
                            b3_budget = equity * 0.50
                            b2_budget = equity * 0.40
                            b1_budget = equity * 0.10
                            budget_lines = (
                                f"  B3 급등주:  ${b3_budget:>10,.0f}  (50%)\n"
                                f"  B2 스윙:    ${b2_budget:>10,.0f}  (40%)\n"
                                f"  B1 가치주:  ${b1_budget:>10,.0f}  (10%)\n"
                                f"  현금:       ${cash:>10,.0f}"
                            )
                        except Exception:
                            budget_lines = "  (계좌 조회 실패)"

                        tg_send(bot_token, chat_id,
                            f"⚙️ <b>모드 설정</b>\n"
                            f"{_SEP}\n"
                            f"현재 모드: <b>{_mode_labels.get(cur, cur)}</b>\n"
                            f"잠금:      {lock_str}\n"
                            f"{_SEP}\n"
                            f"<b>예산 현황</b> (총 ${equity:,.0f})\n"
                            f"{budget_lines}\n"
                            f"{_SEP}\n"
                            f"변경 명령:\n"
                            f"  /set_mode B3       — B3 전환 (자동복원 가능)\n"
                            f"  /set_mode B2       — B2 전환 (자동복원 가능)\n"
                            f"  /set_mode B3 lock  — B3 전환 + 고정\n"
                            f"  /set_mode B2 lock  — B2 전환 + 고정\n"
                            f"  /set_mode auto     — 고정 해제 (프리마켓 자동)"
                        )
                    except Exception as e:
                        tg_send(bot_token, chat_id, f"모드 조회 오류: {e}")
                    continue

                new_mode_str = rest[0].upper()

                # ── auto / unlock: 고정 해제 ──────────────────────────
                if new_mode_str in ("AUTO", "UNLOCK"):
                    try:
                        dbm.update_system_state("MODE_LOCKED", "off")
                        cur = dbm.get_system_state("CURRENT_MODE", "B3_AGGRESSIVE")
                        tg_send(bot_token, chat_id,
                                f"🔓 모드 고정 해제\n"
                                f"현재 모드: <b>{_mode_labels.get(cur, cur)}</b>\n"
                                f"다음 프리마켓 스캔(9:40 ET)에서 자동 결정됩니다.")
                    except Exception as e:
                        tg_send(bot_token, chat_id, f"해제 오류: {e}")
                    continue

                # ── B3 / B2 [lock] ────────────────────────────────────
                if new_mode_str not in _mode_map:
                    tg_send(bot_token, chat_id,
                            "사용법:\n"
                            "  /set_mode B3 [lock]\n"
                            "  /set_mode B2 [lock]\n"
                            "  /set_mode auto")
                    continue

                canonical = _mode_map[new_mode_str]
                do_lock   = len(rest) >= 2 and rest[1].lower() == "lock"

                # 가용 자금 체크 (전환 가능 여부)
                feasible = True
                try:
                    acct   = broker.get_account()
                    equity = acct.get("equity", acct.get("portfolio_value", 0))
                    cash   = acct.get("cash", 0)
                    min_required = equity * (0.50 if canonical == "B3_AGGRESSIVE" else 0.40)
                    if cash < min_required * 0.3:
                        feasible = False
                except Exception:
                    equity = cash = 0

                try:
                    prev_mode = dbm.get_system_state("CURRENT_MODE", "?")
                    dbm.update_system_state("CURRENT_MODE", canonical)
                    dbm.update_system_state("MODE_LOCKED", "on" if do_lock else "off")
                    logging.warning("[TG] /set_mode: %s → %s lock=%s (by %s)",
                                    prev_mode, canonical, do_lock, user)

                    lock_note = "🔒 프리마켓 스캔 고정" if do_lock else "🔓 다음 프리마켓 스캔에 자동 복원 가능"
                    warn = ""
                    if not feasible:
                        warn = f"\n⚠️ 현금(${cash:,.0f})이 권장 예산(${min_required:,.0f})보다 낮습니다."

                    tg_send(bot_token, chat_id,
                            f"✅ 모드 전환: <b>{_mode_labels.get(canonical, canonical)}</b>\n"
                            f"{lock_note}\n"
                            f"총 자산: ${equity:,.0f} | 현금: ${cash:,.0f}"
                            f"{warn}")
                except Exception as e:
                    tg_send(bot_token, chat_id, f"모드 전환 오류: {e}")
                continue

            if cmd == "/set_b4":
                arg = rest[0].lower() if rest else ""
                try:
                    if arg == "on":
                        dbm.update_system_state("B4_ENABLED", "on")
                        cap = float(dbm.get_system_state("B4_CAPITAL", "0.30"))
                        tg_send(bot_token, chat_id,
                                f"✅ B4 스나이퍼 <b>활성화</b> (자금 {cap*100:.0f}%)\n"
                                f"다음 스캔 주기부터 적용됩니다.")
                    elif arg == "off":
                        dbm.update_system_state("B4_ENABLED", "off")
                        tg_send(bot_token, chat_id,
                                "🔴 B4 스나이퍼 <b>비활성화</b>\n"
                                "현재 포지션은 청산 로직이 계속 동작합니다.")
                    elif arg.isdigit() or (arg.replace(".", "", 1).isdigit()):
                        pct = float(arg)
                        if not 5 <= pct <= 80:
                            tg_send(bot_token, chat_id, "자금 비율은 5~80 사이로 입력하세요 (예: /set_b4 30)")
                        else:
                            dbm.update_system_state("B4_CAPITAL", str(pct / 100))
                            enabled = dbm.get_system_state("B4_ENABLED", "on")
                            tg_send(bot_token, chat_id,
                                    f"✅ B4 자금 비율 → <b>{pct:.0f}%</b>  (상태: {'ON' if enabled == 'on' else 'OFF'})\n"
                                    f"다음 진입 시 적용됩니다.")
                    else:
                        cur_en  = dbm.get_system_state("B4_ENABLED", "on")
                        cur_cap = float(dbm.get_system_state("B4_CAPITAL", "0.30"))
                        tg_send(bot_token, chat_id,
                                f"🎯 B4 스나이퍼 현재 설정\n"
                                f"{_SEP}\n"
                                f"상태:      {'✅ ON' if cur_en == 'on' else '🔴 OFF'}\n"
                                f"자금 비율: {cur_cap*100:.0f}%\n"
                                f"{_SEP}\n"
                                f"변경 명령:\n"
                                f"  /set_b4 on   — 활성화\n"
                                f"  /set_b4 off  — 비활성화\n"
                                f"  /set_b4 30   — 자금 30%\n"
                                f"  /set_b4 50   — 자금 50%")
                except Exception as e:
                    tg_send(bot_token, chat_id, f"B4 설정 오류: {e}")
                continue

            if cmd in ("/start", "/help"):
                tg_send(bot_token, chat_id,
                        "📋 <b>명령 목록</b>\n"
                        "\n<b>모니터링</b>\n"
                        "/ping — 봇 상태 확인\n"
                        "/status — 모드·그룹·오늘 성적·보유 종목\n"
                        "/set_mode [B3/B2] — 모드 강제 전환\n"
                        "/set_b4 [on/off/30] — B4 스나이퍼 설정\n"
                        "/account — 계좌 잔고\n"
                        "/positions — 포지션 + 진입가 + 고점\n"
                        "\n<b>분석 · 일지</b>\n"
                        "/journal [YYYY-MM-DD] — 일일 매매 일지 생성\n"
                        "/weekly [YYYY-MM-DD] — 주간 분석 (해당 주 월요일)\n"
                        "/stats [30] — 누계 통계 (기본 30일)\n"
                        "/scan — 급등/저평가 종목 스캔\n"
                        "/ask 질문내용 — AI에게 질문\n"
                        "\n<b>수동 주문</b>\n"
                        "/buy TICKER QTY\n"
                        "/sell TICKER QTY\n"
                        "/search 키워드\n"
                        "\n<b>기타</b>\n"
                        "/gptstatus — Gemini 연결 상태\n"
                        "/stop — 봇 종료")
                continue

            if cmd == "/ping":
                tg_send(bot_token, chat_id, "pong ✅")
                continue

            if cmd == "/gptstatus":
                ok = bool(os.getenv("GEMINI_API_KEY"))
                flash = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.0-flash")
                pro   = os.getenv("GEMINI_PRO_MODEL",   "gemini-2.5-pro")
                tg_send(bot_token, chat_id,
                        f"GEMINI_KEY: {'OK' if ok else 'MISSING'}\n"
                        f"Flash: {flash}\n"
                        f"Pro:   {pro}")
                continue

            if cmd == "/stop":
                tg_send(bot_token, chat_id, "봇을 종료합니다.")
                RUN = False
                continue

            if cmd == "/ask":
                if not rest:
                    tg_send(bot_token, chat_id, "형식: /ask 질문내용")
                    continue
                q = " ".join(rest).strip()
                cached = _get_cached(chat_id, q)
                if cached:
                    tg_send(bot_token, chat_id, f"🤖 (캐시) Gemini 응답:\n{cached}")
                    continue
                if not _allow_chat(chat_id):
                    tg_send(bot_token, chat_id, f"요청이 너무 빠릅니다. {ASK_COOLDOWN_SEC}초 뒤 다시 시도해주세요.")
                    continue
                REQ_QUEUE.put((chat_id, q))
                tg_send(bot_token, chat_id, "질문을 접수했어요. 잠시만 기다려주세요…")
                continue

            if cmd == "/account":
                try:
                    info  = broker.get_account()
                    cash  = info.get("cash", 0.0)
                    pv    = info.get("portfolio_value", 0.0)
                    eq    = info.get("equity", pv)
                    dpl   = info.get("day_pl")
                    dpct  = info.get("day_pl_pct")

                    acct_mode = "PAPER" if mode == "paper" else "LIVE"
                    lines = [
                        f"💰 <b>계좌 현황</b>  ({acct_mode})",
                        _SEP,
                        f"총 자산:     ${eq:>12,.2f}",
                        f"현금:        ${cash:>12,.2f}",
                        f"포트폴리오:  ${pv:>12,.2f}",
                    ]
                    if dpl is not None:
                        sign = "+" if dpl >= 0 else ""
                        pct_str = f" ({dpct:+.2f}%)" if dpct is not None else ""
                        lines.append(f"전일 대비:   {sign}${dpl:>11,.2f}{pct_str}")
                    lines.append(_SEP)
                    tg_send(bot_token, chat_id, "\n".join(lines))
                except Exception as e:
                    tg_send(bot_token, chat_id, f"계정 조회 오류: {e}")
                continue

            if cmd == "/search":
                if not rest:
                    tg_send(bot_token, chat_id, "형식: /search 키워드")
                    continue
                keyword = " ".join(rest).strip()
                matches = search_assets(keyword, limit=20)
                if not matches:
                    tg_send(bot_token, chat_id, f"검색 결과 없음: {keyword}")
                    continue
                lines = []
                for a in matches[:20]:
                    trad = "✅" if a.get("tradable") else "⛔"
                    frac = "•" if a.get("fractionable") else ""
                    lines.append(f"{a['symbol']:<6} {a.get('name','')[:40]}  ({a.get('exchange','')}) {trad}{frac}")
                tg_send(bot_token, chat_id, "<b>검색 결과</b>\n" + "\n".join(lines))
                continue

            if cmd == "/positions":
                try:
                    detail = broker.list_positions_detail() if hasattr(broker, "list_positions_detail") else []
                    tg_send(bot_token, chat_id, fmt_positions(db, detail))
                except Exception as e:
                    tg_send(bot_token, chat_id, f"포지션 조회 오류: {e}")
                continue

            if cmd == "/scan":
                try:
                    symbols = get_watchlist(cfg)
                    mom, val = quick_scan(data_client, symbols, args.minutes, cfg)
                    txt = (
                        "<b>스캔 결과</b>\n"
                        "• 급등(모멘텀): " + (", ".join(mom) if mom else "없음") + "\n"
                        "• 저평가(Value): " + (", ".join(val) if val else "없음")
                    )
                    tg_send(bot_token, chat_id, txt)
                except Exception as e:
                    tg_send(bot_token, chat_id, f"스캔 오류: {e}")
                continue

            if cmd == "/journal":
                target_date = rest[0] if rest else None
                tg_send(bot_token, chat_id, "📝 일지 생성 중...")
                try:
                    from storage.journal import generate_and_save
                    msg = generate_and_save(db, date=target_date, send_telegram=False)
                    tg_send(bot_token, chat_id, msg or "일지 생성 실패")
                except Exception as e:
                    tg_send(bot_token, chat_id, f"일지 생성 오류: {e}")
                continue

            if cmd == "/weekly":
                week_start = rest[0] if rest else None
                tg_send(bot_token, chat_id, "📊 주간 분석 생성 중...")
                try:
                    from storage.journal import generate_weekly
                    msg = generate_weekly(db, week_start=week_start, send_telegram=False)
                    tg_send(bot_token, chat_id, msg or "주간 분석 실패")
                except Exception as e:
                    tg_send(bot_token, chat_id, f"주간 분석 오류: {e}")
                continue

            if cmd == "/stats":
                days = int(rest[0]) if rest and rest[0].isdigit() else 30
                try:
                    from storage.journal import format_stats_message
                    msg = format_stats_message(db, days=days)
                    tg_send(bot_token, chat_id, msg)
                except Exception as e:
                    tg_send(bot_token, chat_id, f"통계 조회 오류: {e}")
                continue

            if cmd in ("/buy", "/sell"):
                side = "buy" if cmd == "/buy" else "sell"
                if len(rest) < 2:
                    tg_send(bot_token, chat_id, "형식: /buy TICKER QTY  또는  /sell TICKER QTY")
                    continue
                sym = rest[0].upper()
                try:
                    qty = int(rest[1])
                    if qty <= 0:
                        raise ValueError
                except Exception:
                    tg_send(bot_token, chat_id, "QTY는 양의 정수여야 합니다.")
                    continue
                try:
                    resp = broker.submit_market_order(sym, qty, side)
                    if isinstance(resp, dict):
                        body     = json.dumps(resp, ensure_ascii=False, default=str)
                        order_id = resp.get("id") or resp.get("client_order_id")
                    else:
                        data_d   = resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__
                        body     = json.dumps(data_d, ensure_ascii=False, default=str)
                        order_id = data_d.get("id")
                    tg_send(bot_token, chat_id,
                            f"{'🟢 BUY' if side=='buy' else '🔴 SELL'} {sym} x{qty}\n"
                            f"id={order_id}\n{body}")
                except Exception as e:
                    tg_send(bot_token, chat_id, f"주문 실패: {e}")
                continue

            tg_send(bot_token, chat_id, "알 수 없는 명령입니다. /help 를 확인하세요.")

        time.sleep(0.3)

    logging.info("Bot stopped. Bye 👋")


if __name__ == "__main__":
    main()
