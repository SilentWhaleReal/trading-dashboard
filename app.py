from flask import Flask, render_template, request
import requests

active_trade = None
wins = 0
losses = 0

last_signal_type = None
last_signal_time = None
COOLDOWN_SECONDS = 120  # 2 min cooldown

def check_trade(price):
    global active_trade, wins, losses

    if not active_trade:
        return

    current = float(price)
    entry = active_trade["entry"]

    # BREAKEVEN
    if not active_trade["be_activated"]:
        if active_trade["type"] == "BUY" and current >= entry * 1.0015:
            active_trade["sl"] = entry
            active_trade["be_activated"] = True

            send_telegram("⚡ BREAKEVEN ACTIVATED\n🔒 Risk = 0")

        elif active_trade["type"] == "SELL" and current <= entry * 0.9985:
            active_trade["sl"] = entry
            active_trade["be_activated"] = True

            send_telegram("⚡ BREAKEVEN ACTIVATED\n🔒 Risk = 0")

    # TRAILING
    if active_trade["be_activated"]:
        if active_trade["type"] == "BUY":
            new_trail = current * 0.998

            if new_trail > active_trade["trail_level"]:
                active_trade["trail_level"] = new_trail
                active_trade["sl"] = new_trail

                send_telegram(f"📈 TRAILING STOP MOVED\n🔒 SL: {new_trail:.2f}")

        elif active_trade["type"] == "SELL":
            new_trail = current * 1.002

            if new_trail < active_trade["trail_level"]:
                active_trade["trail_level"] = new_trail
                active_trade["sl"] = new_trail

                send_telegram(f"📉 TRAILING STOP MOVED\n🔒 SL: {new_trail:.2f}")

    # TP / SL
    if active_trade["type"] == "BUY":
        if current >= active_trade["tp"]:
            wins += 1
            send_telegram("✅ TP HIT")
            active_trade = None

        elif current <= active_trade["sl"]:
            losses += 1
            send_telegram("❌ SL HIT")
            active_trade = None

    elif active_trade["type"] == "SELL":
        if current <= active_trade["tp"]:
            wins += 1
            send_telegram("✅ TP HIT")
            active_trade = None

        elif current >= active_trade["sl"]:
            losses += 1
            send_telegram("❌ SL HIT")
            active_trade = None

def send_telegram(message):
    BOT_TOKEN = "8575145338:AAFDbJ5HjWtW4R9_V2aK5bWeAw8GqkXaHzI"
    CHAT_ID = "982556834"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        requests.post(url, json=payload)
    except Exception as e:
        print("Telegram error:", e)

app = Flask(__name__)

signals = []

# 🔹 BTC PRICE
def get_btc_price():
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        data = requests.get(url).json()

        price = float(data["price"])

        # ✅ THIS MUST BE BEFORE RETURN
        check_trade(price)

        return price

    except:
        return None

        from datetime import datetime, timedelta

def smart_filter(signal_type, tf, last_15m, last_1h, last_4h):
    global last_signal_type, last_signal_time

    now = datetime.now()

    # ❌ 1. Multi-timeframe alignment
    if not (last_15m and last_1h and last_4h):
        return False

    if not (
        last_15m["type"] == signal_type and
        last_1h["type"] == signal_type and
        last_4h["type"] == signal_type
    ):

        return False

    # ❌ 2. Avoid duplicate signals
    if signal_type == last_signal_type:
        return False

    # ❌ 3. Cooldown
    if last_signal_time:
        if (now - last_signal_time).total_seconds() < COOLDOWN_SECONDS:
            return False

    # ✅ PASS → update memory
    last_signal_type = signal_type
    last_signal_time = now

    return True

def get_trend():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=50"
        data = requests.get(url).json()

        closes = [float(candle[4]) for candle in data]

        short_ma = sum(closes[-10:]) / 10   # fast MA
        long_ma = sum(closes[-30:]) / 30    # slow MA

        if short_ma > long_ma:
            return "UP"
        elif short_ma < long_ma:
            return "DOWN"
        else:
            return "SIDEWAYS"

    except:
        return "UNKNOWN"

def get_dynamic_tp_sl(price):
    vol = get_volatility()

    # scale based on volatility
    if vol < 0.15:
        tp_pct = 0.002
        sl_pct = 0.0015
    elif vol < 0.30:
        tp_pct = 0.003
        sl_pct = 0.002
    else:
        tp_pct = 0.005
        sl_pct = 0.003

    return tp_pct, sl_pct

# 🔹 WEBHOOK (TradingView)
@app.route("/webhook", methods=["POST"])
def webhook():
    global active_trade

    print("🔥 WEBHOOK HIT")

    data = request.get_json(force=True)
    print("📩 DATA:", data)

    signal = {
        "type": data.get("type"),
        "price": data.get("price"),
        "tf": data.get("tf")
    }

    signals.insert(0, signal)

    signal_type = signal["type"]
    price = float(signal["price"])
    tf = signal["tf"]

    # TIMEFRAMES
    last_15m = next((s for s in signals if s.get("tf") == "15m"), None)
    last_1h = next((s for s in signals if s.get("tf") == "1h"), None)
    last_4h = next((s for s in signals if s.get("tf") == "4h"), None)

    # VOLATILITY
    volatility = get_volatility()
    if volatility < 0.15:
        return {"status": "low volatility"}

    # TREND
    trend = get_trend()

    if signal_type == "BUY" and trend != "UP":
        return {"status": "trend blocked"}

    if signal_type == "SELL" and trend != "DOWN":
        return {"status": "trend blocked"}

    # SMART FILTER
    if not smart_filter(signal_type, tf, last_15m, last_1h, last_4h):
        return {"status": "filtered"}

    # SCORE
    alignment_ok = last_15m and last_1h and last_4h
    score = trade_score(volatility, trend, alignment_ok)

    if score < 4:
        return {"status": "low score"}

    # TP / SL
    tp_pct, sl_pct = get_dynamic_tp_sl(price)

    if signal_type == "BUY":
        active_trade = {
        "type": "BUY",
        "entry": price,
        "tp": price * (1 + tp_pct),
        "sl": price * (1 - sl_pct),
        "be_activated": False,
        "trail_level": price
    }

    elif signal_type == "SELL":
        active_trade = {
        "type": "SELL",
        "entry": price,
        "tp": price * (1 - tp_pct),
        "sl": price * (1 + sl_pct),
        "be_activated": False,
        "trail_level": price
    }

    # TELEGRAM
    send_telegram(
        f"🚀 BTC {signal_type} SIGNAL\n\n"
        f"💰 Entry: {price}\n"
        f"🎯 TP: {active_trade['tp']:.2f}\n"
        f"🛑 SL: {active_trade['sl']:.2f}\n\n"
        f"📊 Volatility: {volatility:.2f}%\n"
        f"📈 Trend: {trend}\n"
        f"⭐ Score: {score}/5"
    )

    return {"status": "ok"} 

# 🔹 DASHBOARD
@app.route("/")
def home():

    total = wins + losses
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0
    
    price = get_btc_price()

    # LAST SIGNALS
    last_signals = signals[:10]

    # LAST PER TF
    last_15m = next((s for s in signals if s.get("tf") == "15m"), None)
    last_1h = next((s for s in signals if s.get("tf") == "1h"), None)
    last_4h = next((s for s in signals if s.get("tf") == "4h"), None)

    # ALIGNMENT
    alignment = "NONE"

    if last_15m and last_1h and last_4h:
        if (
            last_15m["type"] == "BUY" and
            last_1h["type"] == "BUY" and
            last_4h["type"] == "BUY"
        ):
            alignment = "STRONG BUY"

        elif (
            last_15m["type"] == "SELL" and
            last_1h["type"] == "SELL" and
            last_4h["type"] == "SELL"
        ):
            alignment = "STRONG SELL"

    # STATS
    total_signals = len(signals)
    buy_count = len([s for s in signals if s["type"] == "BUY"])
    sell_count = len([s for s in signals if s["type"] == "SELL"])

    if total_signals > 0:
        win_rate = round((buy_count / total_signals) * 100, 2)
    else:
        win_rate = 0

    # ✅ RETURN MUST BE INSIDE FUNCTION
    return render_template(
        "index.html",
        price=price,
        signals=signals,
        last_signals=last_signals,
        last_15m=last_15m,
        last_1h=last_1h,
        last_4h=last_4h,
        alignment=alignment,
        total_signals=total_signals,
        buy_count=buy_count,
        sell_count=sell_count,
        win_rate=win_rate
    )

# 🔹 TEST ROUTES
@app.route("/test_buy")
def test_buy():
    signals.insert(0, {"type": "BUY", "price": get_btc_price(), "tf": "test"})
    return {"status": "buy added"}

@app.route("/test_sell")
def test_sell():
    signals.insert(0, {"type": "SELL", "price": get_btc_price(), "tf": "test"})
    return {"status": "sell added"}

@app.route("/price")
def price():
    return {"price": get_btc_price()}

@app.route("/test_signal")
def test_signal():
        test_data = {
        "type": "BUY",
        "price": "67000",
        "tf": "15m"
    }

    with app.test_request_context(
        "/webhook",
        method="POST",
        json=test_data
    ):
        return webhook()


if __name__ == "__main__":
    app.run(debug=True)