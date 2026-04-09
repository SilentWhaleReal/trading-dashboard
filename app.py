from flask import Flask, render_template, request
import requests
from datetime import datetime

app = Flask(__name__)

# ========================
# GLOBALS
# ========================
active_trade = None
wins = 0
losses = 0
signals = []

trades_history = []
wins = 0
losses = 0

# ========================
# TELEGRAM
# ========================
def send_telegram(message):
    BOT_TOKEN = "8575145338:AAFDbJ5HjWtW4R9_V2aK5bWeAw8GqkXaHzI"
    CHAT_ID = "982556834"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message
        })
    except Exception as e:
        print("Telegram error:", e)


# ========================
# MARKET DATA
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


def get_volatility():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=20"
        data = requests.get(url).json()

        closes = [float(c[4]) for c in data]
        avg = sum(closes) / len(closes)

        return (max(closes) - min(closes)) / avg
    except:
        return 0


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
        return "SIDEWAYS"
    except:
        return "UNKNOWN"


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
    if not active_trade["be"]:
        if active_trade["type"] == "BUY" and current >= entry * 1.0015:
            active_trade["sl"] = entry
            active_trade["be"] = True
            send_telegram("⚡ Breakeven activated")

        elif active_trade["type"] == "SELL" and current <= entry * 0.9985:
            active_trade["sl"] = entry
            active_trade["be"] = True
            send_telegram("⚡ Breakeven activated")

    # TRAILING
    if active_trade["be"]:
        if active_trade["type"] == "BUY":
            new_sl = current * 0.998
            if new_sl > active_trade["sl"]:
                active_trade["sl"] = new_sl
                send_telegram(f"📈 Trailing moved → {new_sl:.2f}")
        else:
            new_sl = current * 1.002
            if new_sl < active_trade["sl"]:
                active_trade["sl"] = new_sl
                send_telegram(f"📉 Trailing moved → {new_sl:.2f}")

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
    else:
        if current <= active_trade["tp"]:
            wins += 1
            send_telegram("✅ TP HIT")
            active_trade = None
        elif current >= active_trade["sl"]:
            losses += 1
            send_telegram("❌ SL HIT")
            active_trade = None

def get_market_bias():
    last_4h = next((s for s in signals if s.get("tf") == "4h"), None)
    last_1h = next((s for s in signals if s.get("tf") == "1h"), None)

    if last_4h and last_1h:
        if last_4h["type"] == "BUY" and last_1h["type"] == "BUY":
            return "UP"
        elif last_4h["type"] == "SELL" and last_1h["type"] == "SELL":
            return "DOWN"

    return "NEUTRAL"

def get_regime(volatility, trend):
    if volatility > 0.08:
        if trend == "UP":
            return "STRONG BULL"
        elif trend == "DOWN":
            return "STRONG BEAR"
    elif volatility > 0.04:
        return "TRENDING"
    else:
        return "RANGE"

def get_quality(score):
    if score >= 5:
        return "VERY STRONG"
    elif score >= 3:
        return "MODERATE"
    else:
        return "WEAK"

        quality = get_quality(score)

    if score < 3:
        return {"status": "filtered"}

def update_trades(current_price):
    global wins, losses

    for trade in trades_history:
        if trade["status"] != "open":
            continue

        if trade["type"] == "BUY":
            if current_price >= trade["tp"]:
                trade["status"] = "win"
                wins += 1
            elif current_price <= trade["sl"]:
                trade["status"] = "loss"
                losses += 1

        elif trade["type"] == "SELL":
            if current_price <= trade["tp"]:
                trade["status"] = "win"
                wins += 1
            elif current_price >= trade["sl"]:
                trade["status"] = "loss"
                losses += 1

def get_winrate():
    total = wins + losses
    if total == 0:
        return 0
    return round((wins / total) * 100, 2)                

# ========================
# WEBHOOK (TEST MODE)
# ========================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("📩 WEBHOOK DATA:", data)

    if not data:
        return {"status": "no data"}

    signal_type = data.get("type")
    price = float(data.get("price"))
    tf = data.get("tf")

    update_trades(price)

    tp_pct = 0.003
    sl_pct = 0.002

    # Save signal
    signal = {
        "type": signal_type,
        "price": price,
        "tf": tf
    }
    signals.insert(0, signal)

    # ===== PROBABILITY ENGINE =====

    score = 0

    bias = get_market_bias()
    regime = get_regime(volatility, trend)

    # 1. Bias alignment (heavy weight)
    if (signal_type == "BUY" and bias == "UP") or (signal_type == "SELL" and bias == "DOWN"):
        score += 2

    # 2. Multi-timeframe alignment
        alignment = 0

    if last_15m and last_15m["type"] == signal_type:
        alignment += 1

    if last_1h and last_1h["type"] == signal_type:
        alignment += 1

    if last_4h and last_4h["type"] == signal_type:
        alignment += 1

    if alignment == 3:
        score += 2
    elif alignment == 2:
        score += 1

    # 3. Volatility (market participation)
    if volatility > 0.05:
        score += 1

    # 4. Regime boost
    if "STRONG" in regime:
        score += 1

        print(f"📊 SCORE: {score} | BIAS: {bias} | REGIME: {regime} | ALIGN: {alignment}")

    # ❌ FILTER BAD TRADES
    if score < 3:
        return {"status": "filtered (sniper low quality)"}

    # ===== TP / SL =====
    tp_pct = 0.003
    sl_pct = 0.002

    if signal_type == "BUY":
        active_trade = {
            "type": "BUY",
            "entry": price,
            "tp": price * (1 + tp_pct),
            "sl": price * (1 - sl_pct),
            "be": False
        }
    else:
        active_trade = {
            "type": "SELL",
            "entry": price,
            "tp": price * (1 - tp_pct),
            "sl": price * (1 + sl_pct),
            "be": False
        }

    print("📊 TRADE:", active_trade)

    trade = {
    "type": signal_type,
    "entry": price,
    "tp": active_trade["tp"],
    "sl": active_trade["sl"],
    "status": "open"
}

    trades_history.insert(0, trade)

    # ===== TELEGRAM ALERT =====
    send_telegram(
    f"🚀 BTC SNIPER DASHBOARD\n"
    f"━━━━━━━━━━━━━━━━━━\n"
    
    f"📊 Signal: {signal_type}\n"
    f"💰 Entry: {price}\n"
    f"⏱ TF: {tf}\n\n"
    
    f"📈 Bias: {bias}\n"
    f"🌍 Regime: {regime}\n"
    f"📊 Alignment: {alignment}/3\n\n"
    
    f"⭐ Quality: {quality}\n"
    f"📊 Score: {score}/6\n\n"
    
    f"🎯 TP: {round(active_trade['tp'], 2)}\n"
    f"🛑 SL: {round(active_trade['sl'], 2)}"
)

    if score >= 5:
        send_telegram("🔥 ULTRA STRONG SNIPER SETUP")

    return {"status": "signal sent"}


# ========================
# DASHBOARD
# ========================
@app.route("/")
def home():
    price = get_btc_price()

    last_signals = signals[:10]
    last_trades = trades_history[:10]

    total = wins + losses
    win_rate = get_winrate()

    return render_template(
        "index.html",
        price=price,
        last_signals=last_signals,
        trades=last_trades,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=win_rate
    )


# ========================
# TEST ROUTE
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


@app.route("/price")
def price():
    return {"price": get_btc_price()}


# ========================
# RUN
# ========================
if __name__ == "__main__":
    app.run(debug=True)