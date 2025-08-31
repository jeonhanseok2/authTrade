# notify/telegram_bot.py
# -*- coding: utf-8 -*-
"""
텔레그램 명령으로 트레이딩 상태를 조회/제어하는 간단 봇 (Long Polling)
실행: MODE=paper python -m notify.telegram_bot --minutes 600
"""

import os, sys, time, json, signal, random, logging, argparse, threading, requests, yaml, hashlib
from typing import Optional, Dict, List
from dotenv import load_dotenv
from collections import deque
from data.assets import ensure_asset_cache, search_assets

ASK_COOLDOWN_SEC = int(os.getenv("ASK_COOLDOWN_SEC", "15"))
MAX_GPT_QPS = float(os.getenv("MAX_GPT_QPS", "0.2"))  # 0.2 = 5초/건
MAX_GPT_CONCURRENCY = int(os.getenv("MAX_GPT_CONCURRENCY", "1"))
PROMPT_CACHE_TTL = int(os.getenv("PROMPT_CACHE_TTL", "120"))

CHAT_COOLDOWN = {}
PROMPT_CACHE = {}  # key -> (ts, answer)
REQ_QUEUE = deque()
WORKERS_STARTED = False
LAST_CALL_TS = 0.0
CONC_SEM = threading.BoundedSemaphore(MAX_GPT_CONCURRENCY)

def _cache_key(chat_id: str, prompt: str) -> str:
    h = hashlib.sha256((chat_id + "|" + prompt).encode("utf-8")).hexdigest()
    return h

def _get_cached(chat_id: str, prompt: str):
    k = _cache_key(chat_id, prompt)
    v = PROMPT_CACHE.get(k)
    if not v: return None
    ts, ans = v
    if time.time() - ts > PROMPT_CACHE_TTL:
        PROMPT_CACHE.pop(k, None)
        return None
    return ans

def _set_cache(chat_id: str, prompt: str, answer: str):
    PROMPT_CACHE[_cache_key(chat_id, prompt)] = (time.time(), answer)

def _allow_chat(chat_id: str, cooldown_sec: int = ASK_COOLDOWN_SEC) -> bool:
    now = time.time()
    last = CHAT_COOLDOWN.get(chat_id, 0)
    if now - last < cooldown_sec:
        return False
    CHAT_COOLDOWN[chat_id] = now
    return True

def _worker_loop(bot_token: str):
    global LAST_CALL_TS
    while True:
        try:
            chat_id, prompt = REQ_QUEUE.popleft()
        except IndexError:
            time.sleep(0.05)
            continue

        # QPS 제한: 마지막 호출 후 최소 간격 확보
        min_gap = 1.0 / max(0.0001, MAX_GPT_QPS)
        sleep_need = LAST_CALL_TS + min_gap - time.time()
        if sleep_need > 0:
            time.sleep(sleep_need)

        with CONC_SEM:
            ans = ask_gpt(prompt)  # 내부에서 백오프 재시도
            LAST_CALL_TS = time.time()
        # 전송 및 캐시
        tg_send(bot_token, chat_id, f"🤖 GPT 응답:\n{ans}")
        _set_cache(chat_id, prompt, ans)

# ===== 로깅 =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ===== 내부 모듈 경로 보정 =====
try:
    from config import load_mode_env
    from data.fetch import fetch_recent_bars
    from data.fundamentals import fetch_quick_fundamentals
    from strategy.entries import momentum_entry, value_entry
except ModuleNotFoundError:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import load_mode_env
    from data.fetch import fetch_recent_bars
    from data.fundamentals import fetch_quick_fundamentals
    from strategy.entries import momentum_entry, value_entry

# ===== 브로커 =====
USE_PAPER_SIM = False
try:
    from trader.execution import AlpacaBroker
except Exception:
    USE_PAPER_SIM = True
from trader.paper import PaperSimBroker
from alpaca.data.historical import StockHistoricalDataClient

# ===== OpenAI SDK (v1) =====
from openai import OpenAI, RateLimitError, APIError, APIStatusError
_GPT_CLIENT = None

def _get_gpt_client():
    global _GPT_CLIENT
    if _GPT_CLIENT is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        _GPT_CLIENT = OpenAI(api_key=api_key, timeout=20.0, max_retries=0)
    return _GPT_CLIENT

def ask_gpt(prompt: str) -> str:
    client = _get_gpt_client()
    if client is None:
        return "GPT 호출 실패: OPENAI_API_KEY 미설정"
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if len(prompt) > 2000:
        prompt = prompt[:2000] + " ... (trimmed)"
    base, max_attempts = 0.8, 5
    for attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=130,
            )
            return resp.choices[0].message.content.strip()
        except RateLimitError as e:
            body = ""
            try:
                body = e.response.text[:200]
            except Exception:
                pass
            logging.warning(f"[429] headers-missing. body={body}")
        except APIStatusError as e:
            if 500 <= e.status_code < 600:
                logging.warning(f"[{e.status_code}] OpenAI 서버 오류. 재시도...")
                time.sleep(base * (2 ** attempt) + random.uniform(0, 0.7))
                continue
            return f"GPT 호출 실패: {e}"
        except (APIError, Exception) as e:
            time.sleep(base * (2 ** attempt) + random.uniform(0, 0.5))
    return "GPT 호출 실패: 서버 혼잡/레이트 한도. 잠시 후 다시 시도해주세요."

# ===== 텔레그램 유틸 =====
def tg_send(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=8)
        if r.status_code != 200:
            logging.warning("sendMessage failed: %s", r.text[:200])
    except Exception as e:
        logging.warning("sendMessage error: %s", e)

def tg_get_updates(bot_token: str, offset: Optional[int]):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
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
        logging.warning("getUpdates connect timeout — backing off"); time.sleep(1.0)
        return {"ok": False, "result": []}
    except requests.exceptions.ConnectionError as e:
        logging.warning(f"getUpdates connection error: {e}"); time.sleep(1.0)
        return {"ok": False, "result": []}
    except Exception as e:
        logging.warning(f"getUpdates error: {e}")
        return {"ok": False, "result": []}

# ===== 트레이딩 헬퍼 =====
def build_clients():
    mode = load_mode_env()
    api_key = os.getenv("ALPACA_API_KEY"); secret = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    if USE_PAPER_SIM or not api_key or not secret:
        logging.info("[WARN] Using PaperSimBroker (no live orders).")
        return PaperSimBroker(), None
    return AlpacaBroker(api_key, secret, paper=paper), StockHistoricalDataClient(api_key, secret)

def get_watchlist(cfg: Dict) -> List[str]:
    path = cfg.get("universe", {}).get("watchlist_file", "watchlists/symbols.txt")
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
            logging.info("fetch %s failed: %s", s, e); dfs[s] = None
        time.sleep(0.05)
    mom_cfg = cfg.get("momentum_rules", {})
    momentum = [s for s, df in dfs.items() if df is not None and not df.empty and momentum_entry(df, mom_cfg)]
    fundamentals = fetch_quick_fundamentals(symbols[:40])
    val_cfg = cfg.get("value_rules", {})
    value = [it["symbol"] for it in fundamentals if value_entry(it, val_cfg)]
    return momentum[:5], value[:10]

def fmt_positions(positions: Dict[str, float]) -> str:
    if not positions: return "포지션 없음"
    return "\n".join([f"- {sym}: {qty}" for sym, qty in positions.items()])

# ===== 제어/보안 =====
RUN = True
CHAT_COOLDOWN: Dict[str, float] = {}
LAST_PROMPT_BY_CHAT: Dict[str, str] = {}
GPT_SEM = threading.BoundedSemaphore(1)
SEEN_UPDATES = set(); MAX_SEEN = 2000

def allow_chat(chat_id: str, cooldown_sec: int = 10) -> bool:
    now = time.time(); last = CHAT_COOLDOWN.get(chat_id, 0)
    if now - last < cooldown_sec: return False
    CHAT_COOLDOWN[chat_id] = now; return True

def _sigint_handler(signum, frame):
    global RUN; RUN = False; logging.info("SIGINT received. Stopping after current poll...")
signal.signal(signal.SIGINT, _sigint_handler)

# ===== 메인 =====
def main():
    global RUN
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=600, help="스캔 시 사용할 분봉 길이")
    args = parser.parse_args()

    mode = load_mode_env(); load_dotenv(f".env.{mode}")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    allow_env = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
    allow_ids = [x.strip() for x in allow_env.split(",") if x.strip()]

    # 디버그
    logging.info(f"mode={mode} | TG_TOKEN={'set' if bot_token else 'missing'} | allow_ids={allow_ids}")
    logging.info(f"OPENAI_KEY={'set' if os.getenv('OPENAI_API_KEY') else 'missing'} model={os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")

    if not bot_token:
        logging.error("TELEGRAM_BOT_TOKEN 미설정"); return

    cfg = {}
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r") as f:
            cfg = yaml.safe_load(f) or {}

    broker, data_client = build_clients()
    logging.info("Telegram bot started. Send /start or /ping to your bot.")

    try:
        trading_client = getattr(broker, "trading_client", None)
    except Exception:
        trading_client = None

    ensure_asset_cache(trading_client, csv_fallback="data/assets_us_equities.csv")

    offset = None
    while RUN:
        updates = tg_get_updates(bot_token, offset)
        if not updates.get("ok"):
            time.sleep(0.8); continue

        result = updates.get("result", [])
        if result:
            logging.info(f"updates={len(result)} first_id={result[0]['update_id']}")

        for item in result:
            uid = item["update_id"]; offset = uid + 1
            if uid in SEEN_UPDATES: continue
            SEEN_UPDATES.add(uid)
            if len(SEEN_UPDATES) > MAX_SEEN: SEEN_UPDATES.clear()

            msg = item.get("message") or item.get("edited_message")
            if not msg: continue

            chat_id = str(msg["chat"]["id"])
            text = (msg.get("text") or "").strip()
            user = msg.get("from", {}).get("username", "unknown")

            if allow_ids and chat_id not in allow_ids:
                tg_send(bot_token, chat_id, "권한이 없습니다.")
                logging.info("reject chat_id=%s user=%s text=%s", chat_id, user, text)
                continue
            if not text: continue

            logging.info("cmd from %s(%s): %s", user, chat_id, text)
            cmd, *rest = text.split(); cmd = cmd.lower()

            if cmd in ("/start", "/help"):
                tg_send(bot_token, chat_id,
                        "명령:\n/ping\n/ask 질문내용\n/gptstatus\n/account\n/search 키워드 - 종목/이름/거래소 검색\n/positions\n/scan\n/buy TICKER QTY\n/sell TICKER QTY\n/stop")
                continue

            if cmd == "/ping":
                tg_send(bot_token, chat_id, "pong ✅"); continue

            if cmd == "/gptstatus":
                ok = bool(os.getenv("OPENAI_API_KEY"))
                tg_send(bot_token, chat_id, f"OPENAI_KEY: {'OK' if ok else 'MISSING'}\nMODEL: {os.getenv('OPENAI_MODEL','gpt-4o-mini')}")
                continue

            if cmd == "/stop":
                tg_send(bot_token, chat_id, "봇을 종료합니다."); RUN = False; continue

            if cmd == "/ask":
                if not rest:
                    tg_send(bot_token, chat_id, "형식: /ask 질문내용")
                    continue

                q = " ".join(rest).strip()

                # 캐시 히트면 즉시 응답 (같은 질문 반복 방지)
                cached = _get_cached(chat_id, q)
                if cached:
                    tg_send(bot_token, chat_id, f"🤖 (캐시) GPT 응답:\n{cached}")
                    continue

                # 채팅별 쿨다운
                if not _allow_chat(chat_id):
                    tg_send(bot_token, chat_id, f"요청이 너무 빠릅니다. {ASK_COOLDOWN_SEC}초 뒤 다시 시도해주세요.")
                    continue

                # 큐에 적재하고 즉시 안내(서버 과부하 시 체감 개선)
                REQ_QUEUE.append((chat_id, q))
                tg_send(bot_token, chat_id, "질문을 접수했어요. 잠시만 기다려주세요…")
                continue

            if cmd == "/account":
                try:
                    info = broker.get_account()
                    tg_send(bot_token, chat_id, f"📊 Account\ncash: {info.get('cash')}\nportfolio_value: {info.get('portfolio_value')}")
                except Exception as e:
                    tg_send(bot_token, chat_id, f"계정 조회 오류: {e}")
                continue

            if cmd == "/search":
                if not rest:
                    tg_send(bot_token, chat_id, "형식: /search 키워드  (예: /search apple 또는 /search AAPL)")
                    continue

                keyword = " ".join(rest).strip()
                matches = search_assets(keyword, limit=20)

                if not matches:
                    tg_send(bot_token, chat_id, f"검색 결과 없음: {keyword}")
                    continue

                # 보기 좋게 포맷
                lines = []
                for a in matches[:20]:
                    trad = "✅" if a.get("tradable") else "⛔"
                    frac = "•" if a.get("fractionable") else ""
                    lines.append(f"{a['symbol']:<6} {a.get('name', '')[:40]}  ({a.get('exchange', '')}) {trad}{frac}")
                msg = "<b>검색 결과</b>\n" + "\n".join(lines)
                tg_send(bot_token, chat_id, msg)
                continue

            if cmd == "/positions":
                try:
                    pos = broker.list_positions() if hasattr(broker, "list_positions") else {}
                    tg_send(bot_token, chat_id, fmt_positions(pos))
                except Exception as e:
                    tg_send(bot_token, chat_id, f"포지션 조회 오류: {e}")
                continue

            if cmd == "/scan":
                try:
                    symbols = get_watchlist(cfg)
                    mom, val = quick_scan(data_client, symbols, args.minutes, cfg)
                    txt = "<b>스캔 결과</b>\n" \
                          "• 급등(모멘텀): " + (", ".join(mom) if mom else "없음") + "\n" \
                                                                              "• 저평가(Value): " + (", ".join(val) if val else "없음")
                    tg_send(bot_token, chat_id, txt)
                except Exception as e:
                    tg_send(bot_token, chat_id, f"스캔 오류: {e}")
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

                    # dict/obj 모두 safe stringify
                    if isinstance(resp, dict):
                        body = json.dumps(resp, ensure_ascii=False, default=str)
                        order_id = resp.get("id") or resp.get("client_order_id")
                    else:
                        # pydantic/obj → 가능한 dict로
                        if hasattr(resp, "model_dump"):
                            data = resp.model_dump()
                        elif hasattr(resp, "dict"):
                            data = resp.dict()
                        elif hasattr(resp, "__dict__"):
                            data = dict(resp.__dict__)
                        else:
                            data = {"raw": str(resp)}
                        body = json.dumps(data, ensure_ascii=False, default=str)
                        order_id = data.get("id") or data.get("client_order_id")

                    tg_send(
                        bot_token,
                        chat_id,
                        f"{'🟢 BUY' if side=='buy' else '🔴 SELL'} {sym} x{qty}\n"
                        f"id={order_id}\n{body}"
                    )
                except Exception as e:
                    tg_send(bot_token, chat_id, f"주문 실패: {e}")
                continue

            tg_send(bot_token, chat_id, "알 수 없는 명령입니다. /help 를 확인하세요.")

        time.sleep(0.3)

        global WORKERS_STARTED
        if not WORKERS_STARTED:
            threading.Thread(target=_worker_loop, args=(bot_token,), daemon=True).start()
            WORKERS_STARTED = True

    logging.info("Bot stopped. Bye 👋")

if __name__ == "__main__":
    main()
