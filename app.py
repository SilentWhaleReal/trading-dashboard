from flask import Flask, render_template, request, jsonify
import requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ========================
# GLOBAL STATE
# ========================
signals = []
active_trade = None
wins = 0
losses = 0

last_signal_type = None
last_signal_time = None
COOLDOWN_SECONDS = 120


# ========================
# TELEGRAM
# ========================
def send_telegram(message):
    BOT_TOKEN = "YOUR_BOT_TOKEN"
    CHAT_ID = "YOUR_CHAT_ID"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message
        })
    except Exception as e:
        print("Telegram error:", e)


# ========================
# VOLATILITY
# ========================
def get_volatility():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=20"
        data = requests.get(url).json()

        closes = [float(c[4]) for c in data]
        return (max(closes) - min(closes)) / min(closes) * 100
    except:
        return 0


# ========================
# TREND
# ========================
def get_trend():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=50"
        data = requests.get(url).json()

        closes = [float(c[4]) for c in data]

        short_ma = sum(closes[-10:]) / 10
        long_ma = sum(closes[-30:]) / 30

        if short_ma > long_ma:
            return "UP"
        elif short_ma < long_ma:
            return "DOWN"
        else:
            return "SIDEWAYS"
    except:
        return "UNKNOWN"


# ========================
# SCORE
# ========================
def trade_score(volatility, trend, alignment_ok):
    score = 0

    if volatility > 0.2:
        score += 2
    if trend in ["UP", "DOWN"]:
        score += 2
    if alignment_ok:
        score += 1

    return score


# ========================
# TP / SL
# ========================
def get_dynamic_tp_sl(price):
    vol = get_volatility()

    if vol < 0.15:
        return 0.002, 0.0015
    elif vol < 0.30:
        return 0.003, 0.002
    else:
        return 0.005, 0.003


# ========================
# TRADE MANAGEMENT
# ========================
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
            send_telegram("⚡ BREAKEVEN ACTIVATED")

        elif active_trade["type"] == "SELL" and current <= entry * 0.9985:
            active_trade["sl"] = entry
            active_trade["be_activated"] = True
            send_telegram("⚡ BREAKEVEN ACTIVATED")

    # TRAILING
    if active_trade["be_activated"]:
        if active_trade["type"] == "BUY":
            new_trail = current * 0.998
            if new_trail > active_trade["trail_level"]:
                active_trade["trail_level"] = new_trail
                active_trade["sl"] = new_trail
                send_telegram(f"📈 TRAILING SL: {new_trail:.2f}")

        elif active_trade["type"] == "SELL":
            new_trail = current * 1.002
            if new_trail < active_trade["trail_level"]:
                active_trade["trail_level"] = new_trail
                active_trade["sl"] = new_trail
                send_telegram(f"📉 TRAILING SL: {new_trail:.2f}")

    # TP / SL HIT
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


# ========================
# PRICE
# ========================
def get_btc_price():
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        data = requests.get(url).json()
        price = float(data["price"])

        check_trade(price)

        return price
    except:
        return None


# ========================
# SMART FILTER
# ========================
def smart_filter(signal_type, last_15m, last_1h, last_4h):
    global last_signal_type, last_signal_time

    now = datetime.now()

    if not (last_15m and last_1h and last_4h):
        return False

    if not (
        last_15m["type"] == signal_type and
        last_1h["type"] == signal_type and
        last_4h["type"] == signal_type
    ):
        return False

    if signal_type == last_signal_type:
        return False

    if last_signal_time:
        if (now - last_signal_time).total_seconds() < COOLDOWN_SECONDS:
            return False

    last_signal_type = signal_type
    last_signal_time = now

    return True


# ========================
# WEBHOOK
# ========================
@app.route("/webhook", methods=["POST"])
def webhook():
    global active_trade

    data = request.get_json(force=True)

    signal = {
        "type": data.get("type"),
        "price": float(data.get("price")),
        "tf": data.get("tf"),
        "time": datetime.now()
    }

    signals.insert(0, signal)

    signal_type = signal["type"]
    price = signal["price"]

    # TIMEFRAMES
    last_15m = next((s for s in signals if s["tf"] == "15m"), None)
    last_1h = next((s for s in signals if s["tf"] == "1h"), None)
    last_4h = next((s for s in signals if s["tf"] == "4h"), None)

    volatility = get_volatility()
    trend = get_trend()

    if volatility < 0.15:
        return {"status": "low volatility"}

    if signal_type == "BUY" and trend != "UP":
        return {"status": "trend blocked"}

    if signal_type == "SELL" and trend != "DOWN":
        return {"status": "trend blocked"}

    if not smart_filter(signal_type, last_15m, last_1h, last_4h):
        return {"status": "filtered"}

    alignment_ok = last_15m and last_1h and last_4h
    score = trade_score(volatility, trend, alignment_ok)

    if score < 4:
        return {"status": "low score"}

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
    else:
        active_trade = {
            "type": "SELL",
            "entry": price,
            "tp": price * (1 - tp_pct),
            "sl": price * (1 + sl_pct),
            "be_activated": False,
            "trail_level": price
        }

    send_telegram(
        f"🚀 BTC {signal_type}\n"
        f"Entry: {price}\nTP: {active_trade['tp']:.2f}\nSL: {active_trade['sl']:.2f}\n"
        f"Volatility: {volatility:.2f}% | Trend: {trend} | Score: {score}"
    )

    return {"status": "ok"}


# ========================
# DASHBOARD
# ========================
@app.route("/")
def home():
    price = get_btc_price()

    last_signals = signals[:10]

    total = wins + losses
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0

    return render_template(
        "index.html",
        price=price,
        last_signals=last_signals,
        total_signals=len(signals),
        buy_count=len([s for s in signals if s["type"] == "BUY"]),
        sell_count=len([s for s in signals if s["type"] == "SELL"]),
        win_rate=win_rate
    )


# ========================
# TEST
# ========================
@app.route("/test_signal")
def test_signal():
    test_data = {
        "type": "BUY",
        "price": "67000",
        "tf": "15m"
    }

    with app.test_request_context("/webhook", method="POST", json=test_data):
        return webhook()


# ========================
# RUN
# ========================
if __name__ == "__main__":
    app.run(debug=True)