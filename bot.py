#!/usr/bin/env python3
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ============================= CONFIG =============================
TELEGRAM_TOKEN = "8589870096:AAHahTpg6LNXbUwUMdt3q2EqVa2McIo14h8"
TELEGRAM_CHAT_IDS = ["5054484162", "497819952"]

SCAN_THRESHOLD = 0.25       # Min % to track
ALERT_THRESHOLD = 5.0       # Instant alert at ±5%
ALERT_COOLDOWN = 60         # Don't spam same symbol
SUMMARY_INTERVAL = 300      # Summary every 5 minutes (300 sec)
MAX_WORKERS = 15
# ==================================================================

BINANCE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_BOOK_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
KUCOIN_ACTIVE_URL = "https://api-futures.kucoin.com/api/v1/contracts/active"
KUCOIN_TICKER_URL = "https://api-futures.kucoin.com/api/v1/ticker?symbol={symbol}"

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def send_telegram(message):
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.get(url, params={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
        except:
            pass

# ==================== SYMBOL & PRICE FETCHING ====================
def get_binance_symbols():
    try:
        data = requests.get(BINANCE_INFO_URL, timeout=10).json()
        return [s["symbol"] for s in data["symbols"] if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"]
    except:
        return []

def get_kucoin_symbols():
    try:
        data = requests.get(KUCOIN_ACTIVE_URL, timeout=10).json()
        return [s["symbol"] for s in data.get("data", []) if s.get("status") == "Open"]
    except:
        return []

def normalize(sym): 
    return sym.upper().rstrip("M")

def get_common_symbols():
    bin_syms = get_binance_symbols()
    ku_syms = get_kucoin_symbols()
    bin_set = {normalize(s) for s in bin_syms}
    ku_set = {normalize(s) for s in ku_syms}
    common = bin_set.intersection(ku_set)
    ku_map = {normalize(s): s for s in ku_syms}
    return common, ku_map

def get_binance_book():
    try:
        data = requests.get(BINANCE_BOOK_URL, timeout=10).json()
        return {d["symbol"]: {"bid": float(d["bidPrice"]), "ask": float(d["askPrice"])} for d in data}
    except:
        return {}

def get_kucoin_price(symbol, session):
    try:
        url = KUCOIN_TICKER_URL.format(symbol=symbol)
        resp = session.get(url, timeout=8)
        data = resp.json().get("data", {})
        bid = float(data.get("bestBidPrice", 0))
        ask = float(data.get("bestAskPrice", 0))
        return symbol, bid if bid > 0 else None, ask if ask > 0 else None
    except:
        return symbol, None, None

def threaded_kucoin_prices(symbols):
    prices = {}
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(get_kucoin_price, sym, session) for sym in symbols]
            for f in as_completed(futures):
                sym, bid, ask = f.result()
                if bid and ask:
                    prices[sym] = {"bid": bid, "ask": ask}
    return prices

# ====================== SPREAD CALCULATION ======================
def calculate_spread(bin_bid, bin_ask, ku_bid, ku_ask):
    if not all([bin_bid, bin_ask, ku_bid, ku_ask]) or bin_ask <= 0:
        return None
    pos = ((ku_bid - bin_ask) / bin_ask) * 100
    neg = ((ku_ask - bin_bid) / bin_bid) * 100
    if pos > 0.01:
        return pos
    if neg < -0.01:
        return neg
    return None

# =========================== MAIN LOOP ===========================
def main():
    print(f"Binance ↔ KuCoin Monitor STARTED - {timestamp()}")
    send_telegram("Bot started — watching for ±5%+ spreads\nSummaries every 5 min | Instant alerts on big moves")

    last_alert = {}
    last_summary_time = 0
    heartbeat_counter = 0

    while True:
        try:
            common_symbols, ku_map = get_common_symbols()
            if not common_symbols:
                print("No common symbols — retrying...")
                time.sleep(10)
                continue

            bin_prices = get_binance_book()
            ku_symbols = [ku_map.get(sym, sym + "M") for sym in common_symbols]
            ku_prices = threaded_kucoin_prices(ku_symbols)

            candidates = {}
            max_pos = max_pos_sym = max_neg = max_neg_sym = None

            for sym in common_symbols:
                bin_tick = bin_prices.get(sym)
                ku_sym = ku_map.get(sym, sym + "M")
                ku_tick = ku_prices.get(ku_sym)
                if not bin_tick or not ku_tick:
                    continue

                spread = calculate_spread(bin_tick["bid"], bin_tick["ask"], ku_tick["bid"], ku_tick["ask"])
                if spread is not None and abs(spread) >= SCAN_THRESHOLD:
                    candidates[sym] = spread

                    if max_pos is None or spread > max_pos:
                        max_pos, max_pos_sym = spread, sym
                    if max_neg is None or spread < max_neg:
                        max_neg, max_neg_sym = spread, sym

                    # INSTANT ALERT ON ±5%+
                    if abs(spread) >= ALERT_THRESHOLD:
                        now = time.time()
                        if sym not in last_alert or now - last_alert[sym] > ALERT_COOLDOWN:
                            direction = "Long Binance / Short KuCoin" if spread > 0 else "Long KuCoin / Short Binance"
                            msg = (
                                f"*BIG SPREAD ALERT*\n"
                                f"`{sym}` → *{spread:+.4f}%*\n"
                                f"Direction → {direction}\n"
                                f"Binance: `{bin_tick['bid']:.6f}` ↔ `{bin_tick['ask']:.6f}`\n"
                                f"KuCoin : `{ku_tick['bid']:.6f}` ↔ `{ku_tick['ask']:.6f}`\n"
                                f"{timestamp()}"
                            )
                            send_telegram(msg)
                            last_alert[sym] = now
                            print(f"ALERT → {sym} {spread:+.4f}%")

            # SUMMARY ONLY EVERY 5 MINUTES
            now = time.time()
            if now - last_summary_time >= SUMMARY_INTERVAL:
                summary = f"*Scan Summary* — {timestamp()}\n"
                summary += f"Tracked: {len(candidates)} symbols\n"
                if max_pos_sym:
                    summary += f"Max +ve → `{max_pos_sym}`: *+{max_pos:.4f}%*\n"
                else:
                    summary += "No +ve spreads\n"
                if max_neg_sym:
                    summary += f"Max -ve → `{max_neg_sym}`: *{max_neg:.4f}%*\n"
                else:
                    summary += "No -ve spreads\n"
                send_telegram(summary)
                last_summary_time = now
                print("Summary sent")

            # Keep Railway alive
            heartbeat_counter += 1
            if heartbeat_counter % 400 == 0:  # ~20 min
                print(f"Bot alive — {timestamp()}")

            time.sleep(3)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
