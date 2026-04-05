from flask import Flask, render_template, request
import requests

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
        return float(data["price"])
    except:
        return None


# 🔹 WEBHOOK (TradingView)
@app.route("/webhook", methods=["POST"])
def webhook():
    print("🔥 WEBHOOK HIT")

    data = request.get_json(force=True)
    print("📩 DATA:", data)

    if data:
        signal = {
            "type": data.get("type"),
            "price": data.get("price"),
            "tf": data.get("tf")
        }
        
    if type == "BUY":
    send_telegram(
        f"🟢 BTC BUY SIGNAL\n"
        f"💰 Price: {price}\n"
        f"⏱ TF: {tf}\n"
        f"🚀 Momentum detected"
    )

    if type == "SELL":
    send_telegram(
        f"🔴 BTC SELL SIGNAL\n"
        f"💰 Price: {price}\n"
        f"⏱ TF: {tf}\n"
        f"⚠️ Bearish pressure"
    )

        signals.insert(0, signal)

        signal_type = signal["type"]
        price = signal["price"]
        tf = signal["tf"]

    if signal_type == "BUY":
        send_telegram(
            f"🟢 BTC BUY SIGNAL\n"
            f"💰 Price: {price}\n"
            f"⏱ TF: {tf}"
        )

    elif signal_type == "SELL":
        send_telegram(
            f"🔴 BTC SELL SIGNAL\n"
            f"💰 Price: {price}\n"
            f"⏱ TF: {tf}"
        )

        from datetime import datetime
        now = datetime.now().strftime("%H:%M:%S")
        f"🕒 Time: {now}\n"

    return {"status": "received"}

@app.route("/test_telegram")
def test_telegram():
    send_telegram("🔥 TEST MESSAGE — Telegram working!")
    return "Sent!"

# 🔹 DASHBOARD
@app.route("/")
def home():
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


if __name__ == "__main__":
    app.run(debug=True)