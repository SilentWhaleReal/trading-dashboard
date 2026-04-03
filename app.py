from flask import Flask, render_template, request
import requests

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

        signals.insert(0, signal)

    return {"status": "received"}


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


if __name__ == "__main__":
    app.run(debug=True)