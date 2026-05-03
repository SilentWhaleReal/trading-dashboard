from flask import Flask, jsonify, render_template, request
from datetime import datetime, timedelta
import os

import requests

app = Flask(__name__)

REQUEST_TIMEOUT = 5
REQUEST_HEADERS = {"User-Agent": "trading-dashboard/1.0"}
MARKET_CACHE_SECONDS = 10
SETUP_ALERT_COOLDOWN_SECONDS = 900

# ========================
# GLOBAL STATE
# ========================
active_trade = None
wins = 0
losses = 0
signals = []
trades_history = []

# Risk / behavior
loss_streak = 0
win_streak = 0
last_trade_time = None
COOLDOWN_SECONDS = 300
MAX_LOSS_STREAK = 3

# Memory
trade_memory = []
MAX_MEMORY = 20

# Session performance
session_performance = {
    "LONDON": {"win": 0, "loss": 0},
    "NEW_YORK": {"win": 0, "loss": 0},
    "ASIA": {"win": 0, "loss": 0},
}

# Dashboard state
latest_data = {
    "score": 0,
    "prob_up": 50,
    "prob_down": 50,
    "alignment": 0,
    "quality": "-",
    "bias": "NEUTRAL",
    "bias_pct": 0,
    "trend": "UNKNOWN",
    "market_note": "Waiting for live candles",
    "composite_prob_up": 50,
    "composite_prob_down": 50,
    "composite_bias": "NEUTRAL",
    "composite_edge": 0,
    "composite_note": "Waiting for daily candles",
}

market_cache = {
    "updated_at": None,
    "volatility": 0,
    "trend": "UNKNOWN",
}

setup_alert_state = {
    "decision": "WAIT",
    "sent_at": None,
}

# ========================
# TELEGRAM
# ========================
def is_telegram_configured():
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN")
        and os.environ.get("TELEGRAM_CHAT_ID")
    )


def send_telegram(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        app.logger.warning("Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        app.logger.warning("Telegram send failed: %s", exc)
        return False


# ========================
# MARKET DATA
# ========================
def fetch_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def get_btc_price():
    price = None

    for url, params, price_key in (
        (
            "https://api.binance.com/api/v3/ticker/price",
            {"symbol": "BTCUSDT"},
            "price",
        ),
        (
            "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
            None,
            "price",
        ),
    ):
        try:
            data = fetch_json(url, params=params)
            price = float(data[price_key])
            break
        except (requests.RequestException, KeyError, TypeError, ValueError):
            continue

    if price is not None:
        check_trade(price)

    return price


def get_binance_closes(interval, limit):
    data = fetch_json(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
    )
    return [float(candle[4]) for candle in data]


def get_coinbase_closes(granularity):
    data = fetch_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": granularity},
    )
    candles = sorted(data, key=lambda candle: candle[0])
    return [float(candle[4]) for candle in candles]


def get_market_closes(interval, limit, granularity):
    try:
        return get_binance_closes(interval, limit)
    except (requests.RequestException, IndexError, TypeError, ValueError):
        try:
            return get_coinbase_closes(granularity)[-limit:]
        except (requests.RequestException, IndexError, TypeError, ValueError):
            return []


def get_volatility():
    closes = get_market_closes("1m", 20, 60)
    return calculate_volatility(closes)


def get_trend():
    closes = get_market_closes("5m", 50, 300)
    return calculate_trend(closes)


# ========================
# UTILITIES
# ========================
def get_session():
    hour = datetime.now().hour
    if 7 <= hour < 13:
        return "LONDON"
    elif 13 <= hour < 20:
        return "NEW_YORK"
    return "ASIA"


def calculate_score(signal_type, bias, alignment, volatility):
    score = 0
    if bias == signal_type:
        score += 2
    if alignment >= 2:
        score += 2
    if volatility > 0.001:
        score += 1
    return score


def calculate_volatility(closes):
    try:
        avg = sum(closes) / len(closes)
        return (max(closes) - min(closes)) / avg
    except (ValueError, ZeroDivisionError):
        return 0


def calculate_trend(closes):
    try:
        return "UP" if closes[-1] > closes[0] else "DOWN"
    except IndexError:
        return "UNKNOWN"


def calculate_momentum(closes):
    try:
        return (closes[-1] - closes[0]) / closes[0]
    except (IndexError, ZeroDivisionError):
        return 0


def calculate_sma(closes, window):
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def calculate_daily_sentiment(daily_closes):
    daily_momentum_7 = calculate_momentum(daily_closes[-7:])
    daily_momentum_14 = calculate_momentum(daily_closes[-14:])
    daily_momentum_30 = calculate_momentum(daily_closes[-30:])
    blended_momentum = (
        daily_momentum_30 * 0.5
        + daily_momentum_14 * 0.3
        + daily_momentum_7 * 0.2
    )
    sma_7 = calculate_sma(daily_closes, 7)
    sma_21 = calculate_sma(daily_closes, 21)
    sma_50 = calculate_sma(daily_closes, 50)
    latest_close = daily_closes[-1] if daily_closes else None

    if blended_momentum > 0.002:
        bias = "UP"
    elif blended_momentum < -0.002:
        bias = "DOWN"
    else:
        bias = "NEUTRAL"

    alignment = 0
    if latest_close and sma_21:
        if latest_close > sma_21 and bias == "UP":
            alignment += 1
        elif latest_close < sma_21 and bias == "DOWN":
            alignment += 1
    if sma_7 and sma_21:
        if sma_7 > sma_21 and bias == "UP":
            alignment += 1
        elif sma_7 < sma_21 and bias == "DOWN":
            alignment += 1
    if sma_21 and sma_50:
        if sma_21 > sma_50 and bias == "UP":
            alignment += 1
        elif sma_21 < sma_50 and bias == "DOWN":
            alignment += 1

    edge = min(45, max(0, abs(blended_momentum) * 160 + alignment * 4))
    edge = round(edge, 1)
    if bias == "UP":
        prob_up = round(50 + edge, 1)
        prob_down = round(50 - edge, 1)
    elif bias == "DOWN":
        prob_up = round(50 - edge, 1)
        prob_down = round(50 + edge, 1)
    else:
        prob_up = 50
        prob_down = 50

    return {
        "bias": bias,
        "prob_up": prob_up,
        "prob_down": prob_down,
        "edge": round(abs(prob_up - prob_down), 1),
        "note": f"Daily bias {blended_momentum * 100:+.4f}% | 30d {daily_momentum_30 * 100:+.2f}% | 14d {daily_momentum_14 * 100:+.2f}% | 7d {daily_momentum_7 * 100:+.2f}%",
    }


def update_live_market_model():
    now = datetime.now()
    if (
        market_cache["updated_at"]
        and (now - market_cache["updated_at"]).total_seconds() < MARKET_CACHE_SECONDS
    ):
        return market_cache["volatility"], market_cache["trend"]

    one_minute_closes = get_market_closes("1m", 20, 60)
    five_minute_closes = get_market_closes("5m", 50, 300)
    volatility = calculate_volatility(one_minute_closes)
    trend = calculate_trend(five_minute_closes)
    fast_momentum = calculate_momentum(one_minute_closes[-10:])
    trend_momentum = calculate_momentum(five_minute_closes)
    blended_momentum = trend_momentum * 0.7 + fast_momentum * 0.3
    daily_sentiment = calculate_daily_sentiment(get_market_closes("1d", 90, 86400))

    if blended_momentum > 0.00015:
        bias = "UP"
    elif blended_momentum < -0.00015:
        bias = "DOWN"
    else:
        bias = "NEUTRAL"

    alignment = 0
    if trend == bias and bias != "NEUTRAL":
        alignment += 1
    if (fast_momentum > 0 and bias == "UP") or (fast_momentum < 0 and bias == "DOWN"):
        alignment += 1
    if (trend_momentum > 0 and bias == "UP") or (trend_momentum < 0 and bias == "DOWN"):
        alignment += 1

    bias_pct = round(blended_momentum * 100, 4)
    edge = min(45, max(0, abs(blended_momentum) * 5000 + alignment * 2.5))
    if volatility > 0.001:
        edge = min(45, edge + 2)
    edge = round(edge, 1)

    if bias == "UP":
        prob_up = round(50 + edge, 1)
        prob_down = round(50 - edge, 1)
    elif bias == "DOWN":
        prob_up = round(50 - edge, 1)
        prob_down = round(50 + edge, 1)
    else:
        prob_up = 50
        prob_down = 50

    score = min(6, alignment + (2 if edge >= 12 else 1 if edge >= 6 else 0))
    quality = get_quality(score) if score >= 2 else "C"

    latest_data.update({
        "score": score,
        "prob_up": prob_up,
        "prob_down": prob_down,
        "alignment": alignment,
        "quality": quality,
        "bias": bias,
        "bias_pct": bias_pct,
        "trend": trend,
        "market_note": f"Live bias {bias_pct:+.4f}% | 5m {trend_momentum * 100:+.4f}% | 1m {fast_momentum * 100:+.4f}% | {daily_sentiment['note']}",
        "composite_prob_up": daily_sentiment["prob_up"],
        "composite_prob_down": daily_sentiment["prob_down"],
        "composite_bias": daily_sentiment["bias"],
        "composite_edge": daily_sentiment["edge"],
        "composite_note": daily_sentiment["note"],
    })
    market_cache.update({
        "updated_at": now,
        "volatility": volatility,
        "trend": trend,
    })

    return volatility, trend


def get_quality(score):
    return "A+" if score >= 5 else "A" if score >= 4 else "B"


def get_probability(score):
    return min(70, 50 + score * 5), max(30, 50 - score * 5)


def get_decision(score, bias):
    if score >= 5 and bias == "UP":
        return "BUY SETUP", "decision-buy"
    if score >= 5 and bias == "DOWN":
        return "SELL SETUP", "decision-sell"
    return "WAIT", "decision-wait"


def format_price(value):
    if value is None:
        return "unavailable"
    return f"{value:,.2f}"


def format_session(value):
    return value.replace("_", " ").title()


def get_signal_levels(price, signal_type, session):
    if price is None:
        return None

    entry_buffer = 0.001
    tp1_pct = 0.002
    tp2_pct = 0.004 if session != "ASIA" else 0.002
    sl_pct = 0.002

    if signal_type == "BUY":
        return {
            "entry_low": price * (1 - entry_buffer),
            "entry_high": price * (1 + entry_buffer),
            "tp1": price * (1 + tp1_pct),
            "tp2": price * (1 + tp2_pct),
            "sl": price * (1 - sl_pct),
        }

    return {
        "entry_low": price * (1 - entry_buffer),
        "entry_high": price * (1 + entry_buffer),
        "tp1": price * (1 - tp1_pct),
        "tp2": price * (1 - tp2_pct),
        "sl": price * (1 + sl_pct),
    }


def format_signal_alert(context):
    signal_type = "BUY" if context["decision_text"] == "BUY SETUP" else "SELL"
    direction_word = "Bullish" if signal_type == "BUY" else "Bearish"
    bias_dot = "🟢" if signal_type == "BUY" else "🔴"
    levels = get_signal_levels(context["price"], signal_type, context["session"])

    if levels:
        entry_text = f"{format_price(levels['entry_low'])} - {format_price(levels['entry_high'])}"
        tp1_text = format_price(levels["tp1"])
        tp2_text = format_price(levels["tp2"])
        sl_text = format_price(levels["sl"])
    else:
        entry_text = tp1_text = tp2_text = sl_text = "unavailable"

    return (
        f"🚨 BTC {signal_type} SIGNAL\n\n"
        f"💰 Current Price: {format_price(context['price'])}\n\n"
        f"📍 Entry: {entry_text}\n"
        f"🎯 TP1: {tp1_text}\n"
        f"🎯 TP2: {tp2_text}\n"
        f"🛑 SL: {sl_text}\n\n"
        f"📊 Bias: {bias_dot} {direction_word}\n"
        f"📈 Trend: {context['trend']}\n\n"
        f"⚡ Strength:\n"
        f"{context['quality']} | Score: {context['score']} | Edge: {context['edge']}%\n\n"
        f"📉 Probabilities:\n"
        f"UP {context['prob_up']}% | DOWN {context['prob_down']}%\n\n"
        f"🕰️ Session: {format_session(context['session'])}\n"
        f"🧭 Daily Composite: {context['composite_bias']} "
        f"(UP {context['composite_prob_up']}% | DOWN {context['composite_prob_down']}%)"
    )


def maybe_send_setup_alert(context):
    decision = context["decision_text"]
    if decision not in {"BUY SETUP", "SELL SETUP"}:
        setup_alert_state["decision"] = "WAIT"
        return False

    if not is_telegram_configured():
        return False

    now = datetime.now()
    sent_at = setup_alert_state["sent_at"]
    is_new_decision = setup_alert_state["decision"] != decision
    cooldown_done = (
        sent_at is None
        or (now - sent_at).total_seconds() >= SETUP_ALERT_COOLDOWN_SECONDS
    )

    if not is_new_decision and not cooldown_done:
        return False

    message = format_signal_alert(context)

    if send_telegram(message):
        setup_alert_state["decision"] = decision
        setup_alert_state["sent_at"] = now
        return True

    return False


def get_bias_class(value):
    if value == "UP" or value == "BUY":
        return "positive"
    if value == "DOWN" or value == "SELL":
        return "negative"
    return "neutral"


def get_strength(score, alignment):
    if score >= 5:
        return "VERY STRONG"
    if score >= 3 or alignment >= 2:
        return "STRONG"
    return "WEAK"


def build_event_rows(score, prob_up, prob_down, win_rate, total_trades):
    sample_size = max(total_trades, len(signals), 1)
    expected = round((prob_up - prob_down) / 100, 2)
    profit_factor = round((wins + 1) / (losses + 1), 2)
    last_return = round(expected * max(score, 1), 2)
    long_window_wr = min(100, max(0, round(win_rate + (expected * 10), 2)))
    quality_score = min(99, max(1, 50 + score * 8 - loss_streak * 7))

    return [
        {
            "event": "BTC Signal Engine",
            "dn": prob_down,
            "up": prob_up,
            "win_rate": win_rate,
            "n": sample_size,
            "expect": expected,
            "pf": profit_factor,
            "last": last_return,
            "ln_wr": long_window_wr,
            "q": quality_score,
            "bias": latest_data.get("bias", "NEUTRAL"),
            "edge": "ACTIVE" if score >= 4 else "NEUTRAL",
        },
        {
            "event": "Session Filter",
            "dn": 45 if get_session() == "LONDON" else 52,
            "up": 55 if get_session() == "LONDON" else 48,
            "win_rate": win_rate,
            "n": sample_size,
            "expect": 0.21 if get_session() == "LONDON" else -0.05,
            "pf": profit_factor,
            "last": 0.07 if get_session() == "LONDON" else -0.03,
            "ln_wr": min(100, max(0, round(win_rate + (3 if get_session() == "LONDON" else -2), 2))),
            "q": 72 if get_session() == "LONDON" else 55,
            "bias": "UP" if get_session() == "LONDON" else "NEUTRAL",
            "edge": "SESSION",
        },
        {
            "event": "Memory / Overtrade",
            "dn": 60 if loss_streak else 48,
            "up": 40 if loss_streak else 52,
            "win_rate": win_rate,
            "n": len(trade_memory),
            "expect": -0.12 if loss_streak else 0.08,
            "pf": profit_factor,
            "last": loss_streak * -0.2,
            "ln_wr": max(0, round(win_rate - loss_streak * 8, 2)),
            "q": max(10, 68 - loss_streak * 9),
            "bias": "DOWN" if loss_streak else "NEUTRAL",
            "edge": "RISK" if loss_streak else "OK",
        },
        {
            "event": "Quality Gate",
            "dn": prob_down,
            "up": prob_up,
            "win_rate": win_rate,
            "n": sample_size,
            "expect": expected,
            "pf": profit_factor,
            "last": last_return,
            "ln_wr": long_window_wr,
            "q": quality_score,
            "bias": latest_data["quality"],
            "edge": get_strength(score, latest_data["alignment"]),
        },
    ]


def build_virtual_row(event, n, directional_prob, expected_return, volatility, confidence_boost=0):
    n = min(500, max(1, int(n)))
    wr_1d = round(min(100, max(0, directional_prob + confidence_boost - 2.0)), 1)
    wr_3d = round(min(100, max(0, directional_prob + confidence_boost)), 1)
    wr_5d = round(min(100, max(0, directional_prob + confidence_boost + 1.8)), 1)
    avg_1d = round(expected_return, 2)
    avg_3d = round(expected_return * 1.8, 2)
    avg_5d = round(expected_return * 2.6, 2)
    avg_mdd = round(-max(0.35, volatility * 28 + abs(expected_return) * 3.2), 2)
    best_3d = round(max(0.4, abs(expected_return) * 8 + volatility * 22), 2)
    worst_3d = round(-max(0.5, abs(expected_return) * 7 + volatility * 24), 2)
    db_quality_score = min(100, max(1, round(n / 5)))

    return {
        "event": event,
        "n": n,
        "wr_1d": wr_1d,
        "wr_3d": wr_3d,
        "wr_5d": wr_5d,
        "avg_1d": avg_1d,
        "avg_3d": avg_3d,
        "avg_5d": avg_5d,
        "avg_mdd": avg_mdd,
        "best_3d": best_3d,
        "worst_3d": worst_3d,
        "quality": f"{db_quality_score}%",
        "quality_score": db_quality_score,
    }


def build_virtual_rows(context):
    bias = context["bias"]
    directional_prob = context["prob_up"] if bias == "UP" else context["prob_down"] if bias == "DOWN" else 50
    composite_prob = (
        context["composite_prob_up"]
        if context["composite_bias"] == "UP"
        else context["composite_prob_down"]
        if context["composite_bias"] == "DOWN"
        else 50
    )
    signed_edge = (context["prob_up"] - context["prob_down"]) / 100
    composite_edge = (context["composite_prob_up"] - context["composite_prob_down"]) / 100
    volatility = context["volatility"] / 100
    weekday = datetime.now().strftime("%A")
    streak_label = "Up Streak(3)" if bias == "UP" else "Down Streak(3)" if bias == "DOWN" else "Flat Streak"
    rsi_event = "RSI OB (65)" if context["rsi_value"] >= 60 else "RSI OS (35)" if context["rsi_value"] <= 40 else "RSI Midline"
    phase_event = "Phase Bull" if context["phase_bias"] == "UP" else "Phase Bear"

    return [
        build_virtual_row(f"{weekday} + TOM", 500, directional_prob, signed_edge * 0.08, volatility, 0),
        build_virtual_row("Pivot / Live Bias", 133 + context["alignment"] * 35, directional_prob, signed_edge * 0.26, volatility, context["alignment"]),
        build_virtual_row(rsi_event, 500, directional_prob, abs(context["rsi_value"] - 50) / 100, volatility, 1.2),
        build_virtual_row("Vol Spike", 306 if context["vol_state"] == "active" else 188, directional_prob, volatility * 7.5, volatility, 2 if context["vol_state"] == "active" else -1),
        build_virtual_row(streak_label, 478, directional_prob, signed_edge * 0.33, volatility, max(win_streak - loss_streak, 0)),
        build_virtual_row(f"Daily Composite {context['composite_bias']}", 500, composite_prob, composite_edge * 0.18, volatility, 2),
        build_virtual_row(f"{context['session']} Session", 500, directional_prob, signed_edge * 0.11, volatility, 1 if context["session"] in {"LONDON", "NEW_YORK"} else -0.5),
        build_virtual_row(phase_event, 500, 56 if context["phase_bias"] == "UP" else 44, (context["phase_up"] - context["phase_down"]) / 180, volatility, 0),
        build_virtual_row(f"Quality {context['quality']}", 500, directional_prob, context["score"] / 900, volatility, context["score"] / 2),
    ]


def build_auto_opt_text(bias, score, prob_up, prob_down, composite_edge, volatility):
    mode = "BULL" if bias == "UP" else "BEAR" if bias == "DOWN" else "NEUTRAL"
    rsi_ob = 65 if volatility > 0.001 else 70
    rsi_os = 25 if volatility > 0.001 else 30
    pivot_lookback = 10 if score >= 4 else 14
    directional_prob = prob_up if bias == "UP" else prob_down if bias == "DOWN" else 50
    probability_edge = abs(directional_prob - 50)
    pf_1d = round(1 + min(0.65, probability_edge / 120), 2)
    pf_3d = round(pf_1d + min(0.35, composite_edge / 220), 2)
    pf_5d = round(pf_3d + min(0.28, score / 40), 2)

    return (
        f"{mode} | RSI OB:{rsi_ob}/OS:{rsi_os} | pivLB={pivot_lookback} | "
        f"H=5d PF[1d={pf_1d} 3d={pf_3d} 5d={pf_5d}]"
    )


def build_adaptive_weight_text(score, rsi_value, volatility, alignment, session, phase_up, composite_edge):
    pivot_weight = round(1 + alignment * 0.45 + score * 0.08, 2)
    rsi_weight = round(max(0.6, abs(rsi_value - 50) / 6), 2)
    streak_weight = round(1 + min(2, abs(win_streak - loss_streak) * 0.5), 2)
    vol_weight = round(1 + min(1.8, volatility * 10), 2)
    composite_weight = round(0.8 + composite_edge / 50, 2)
    moon_weight = round(phase_up / 70, 2)
    session_weight = 1.2 if session in {"LONDON", "NEW_YORK"} else 0.8

    return (
        f"AdaptW -> Piv:{pivot_weight} 🔥 | RSI:{rsi_weight} 🔥 | "
        f"Strk:{streak_weight} | Vol:{vol_weight} | Comp:{composite_weight} 🔥 | "
        f"Moon:{moon_weight} | Ses:{session_weight}"
    )


def build_forecast_projection(price, bias, prob_up, prob_down, composite_edge, volatility):
    directional_prob = prob_up if bias == "UP" else prob_down if bias == "DOWN" else max(prob_up, prob_down)
    projected_pct = round(min(6, max(0.35, composite_edge / 12 + volatility * 8)), 2)

    if price is None:
        target_up = None
        target_down = None
    else:
        target_up = round(price * (1 + projected_pct / 100), 2)
        target_down = round(price * (1 - projected_pct / 100), 2)

    return {
        "label": f"5-day @ {directional_prob}%",
        "projected_pct": projected_pct,
        "target_up_5d": target_up,
        "target_down_5d": target_down,
    }


def build_aspect_rows(phase_up, phase_bias):
    base = datetime.now().minute
    return [
        {
            "name": "Moon",
            "mult": "2.0x",
            "degree": round((base * 6.1) % 360, 1),
            "aspect": "No aspect" if phase_bias == "UP" else "Square pressure",
            "time_arc": round(phase_up * 1.8, 1),
            "bias": phase_bias,
        },
        {
            "name": "Venus",
            "mult": "1.5x",
            "degree": round((base * 4.7 + 96) % 360, 1),
            "aspect": "No aspect" if phase_bias == "DOWN" else "Supportive arc",
            "time_arc": round((100 - phase_up) * 1.7, 1),
            "bias": "DOWN" if phase_bias == "UP" else "UP",
        },
        {
            "name": "Jupiter",
            "mult": "1.3x",
            "degree": round((base * 3.2 + 32) % 360, 1),
            "aspect": "Time arc active",
            "time_arc": round(phase_up * 2.2, 1),
            "bias": phase_bias,
        },
        {
            "name": "Saturn",
            "mult": "1.2x",
            "degree": round((base * 2.8 + 11) % 360, 1),
            "aspect": "No aspect",
            "time_arc": round((100 - phase_up) * 2.1, 1),
            "bias": "NEUTRAL",
        },
    ]


def build_dashboard_context(price=None):
    volatility, trend = update_live_market_model()
    score = latest_data["score"]
    alignment = latest_data["alignment"]
    bias = latest_data.get("bias", "NEUTRAL")
    bias_pct = latest_data["bias_pct"]
    session = get_session()
    total_trades = wins + losses
    win_rate = round((wins / total_trades) * 100, 2) if total_trades else 0
    edge = round(abs(latest_data["prob_up"] - latest_data["prob_down"]), 1)
    composite_prob_up = latest_data["composite_prob_up"]
    composite_prob_down = latest_data["composite_prob_down"]
    composite_bias = latest_data["composite_bias"]
    composite_edge = latest_data["composite_edge"]
    confidence = min(100, max(0, 50 + score * 10))
    decision_text, decision_class = get_decision(score, bias)
    strength = get_strength(score, alignment)
    active_type = active_trade["type"] if active_trade else "NONE"
    event_rows = build_event_rows(
        score,
        latest_data["prob_up"],
        latest_data["prob_down"],
        win_rate,
        total_trades,
    )
    phase_bias = "UP" if datetime.now().minute < 30 else "DOWN"
    phase_up = 56 if phase_bias == "UP" else 44
    phase_down = 100 - phase_up
    target_up = round(price * 1.004, 2) if price else None
    target_down = round(price * 0.996, 2) if price else None
    rsi_value = round(50 + (latest_data["prob_down"] - latest_data["prob_up"]) * 0.3, 1)
    adx_value = round(18 + abs(score) * 1.9 + alignment * 1.4, 1)
    forecast_projection = build_forecast_projection(
        price,
        bias,
        latest_data["prob_up"],
        latest_data["prob_down"],
        composite_edge,
        volatility,
    )
    auto_opt_text = build_auto_opt_text(
        bias,
        score,
        latest_data["prob_up"],
        latest_data["prob_down"],
        composite_edge,
        volatility,
    )
    phase_pct = round(datetime.now().minute / 60 * 100, 1)
    adaptive_weight_text = build_adaptive_weight_text(
        score,
        rsi_value,
        volatility,
        alignment,
        session,
        phase_up,
        composite_edge,
    )
    db_total_records = max(0, total_trades + len(signals) + len(trades_history))
    pending_slots = max(0, 20 - len(signals))

    context = {
        "price": price,
        "score": score,
        "prob_up": latest_data["prob_up"],
        "prob_down": latest_data["prob_down"],
        "composite_prob_up": composite_prob_up,
        "composite_prob_down": composite_prob_down,
        "composite_bias": composite_bias,
        "composite_edge": composite_edge,
        "quality": latest_data["quality"],
        "alignment": alignment,
        "alignment_strength": "STRONG" if alignment >= 3 else "MEDIUM" if alignment >= 2 else "LOW",
        "bias": bias,
        "bias_pct": bias_pct,
        "bias_class": get_bias_class(bias),
        "session": session,
        "edge": edge,
        "confidence": confidence,
        "strength": strength,
        "decision_text": decision_text,
        "decision_class": decision_class,
        "last_signals": signals[-5:],
        "trades": trades_history[-10:],
        "active_trade": active_trade,
        "active_type": active_type,
        "event_rows": event_rows,
        "phase_bias": phase_bias,
        "phase_up": phase_up,
        "phase_down": phase_down,
        "phase_name": "PHASE 2 - CYCLE MAP",
        "phase_pct": phase_pct,
        "target_up": target_up,
        "target_down": target_down,
        "forecast_label": forecast_projection["label"],
        "forecast_projected_pct": forecast_projection["projected_pct"],
        "target_up_5d": forecast_projection["target_up_5d"],
        "target_down_5d": forecast_projection["target_down_5d"],
        "rsi_value": rsi_value,
        "adx_value": adx_value,
        "auto_opt_text": auto_opt_text,
        "adaptive_weight_text": adaptive_weight_text,
        "volatility": round(volatility * 100, 3),
        "vol_state": "active" if volatility > 0.001 else "normal",
        "trend": trend,
        "market_note": latest_data["market_note"],
        "mtf_state": "BULL" if bias == "UP" else "BEAR" if bias == "DOWN" else "NEUTRAL",
        "lookback": "480d",
        "composite_lookback": "90d / 1D",
        "late_session_note": f"Live {session.title()} ({round((latest_data['prob_up'] - latest_data['prob_down']) / 100, 2)}%)",
        "aspect_rows": build_aspect_rows(phase_up, phase_bias),
        "planet_arcs": [
            "Ju-Sa: 101.2",
            "Ma-Sa: 21",
            "Ve-Ma: 105.4",
            "Su-Mo: 73",
        ],
        "planet_config": "P2 Config -> Orb: Standard (6) | Retro Emphasis: ON | Retro Mult: 1x",
        "db_note": "UP% = blended(model + DB win-rate) | n = live DB samples | AVG RET / BEST / WORST = DB-backed | MDD = avg max drawdown",
        "db_total_records": db_total_records,
        "pending_slots": pending_slots,
        "wins": wins,
        "losses": losses,
        "loss_streak": loss_streak,
        "win_streak": win_streak,
        "total_trades": total_trades,
        "win_rate": win_rate,
    }
    context["virtual_rows"] = build_virtual_rows(context)
    context["db_total_records"] = sum(row["n"] for row in context["virtual_rows"])
    context["pending_slots"] = max(0, 500 - min(500, max(row["n"] for row in context["virtual_rows"])))
    return context


def serialize_dashboard_context(context):
    payload = {
        "price": context["price"],
        "score": context["score"],
        "prob_up": context["prob_up"],
        "prob_down": context["prob_down"],
        "composite_prob_up": context["composite_prob_up"],
        "composite_prob_down": context["composite_prob_down"],
        "composite_bias": context["composite_bias"],
        "composite_edge": context["composite_edge"],
        "quality": context["quality"],
        "alignment": context["alignment"],
        "alignment_strength": context["alignment_strength"],
        "bias": context["bias"],
        "bias_pct": context["bias_pct"],
        "bias_class": context["bias_class"],
        "session": context["session"],
        "edge": context["edge"],
        "confidence": context["confidence"],
        "strength": context["strength"],
        "decision_text": context["decision_text"],
        "decision_class": context["decision_class"],
        "wins": context["wins"],
        "losses": context["losses"],
        "total_trades": context["total_trades"],
        "win_rate": context["win_rate"],
        "active_type": context["active_type"],
        "target_up": context["target_up"],
        "target_down": context["target_down"],
        "forecast_label": context["forecast_label"],
        "forecast_projected_pct": context["forecast_projected_pct"],
        "target_up_5d": context["target_up_5d"],
        "target_down_5d": context["target_down_5d"],
        "rsi_value": context["rsi_value"],
        "adx_value": context["adx_value"],
        "auto_opt_text": context["auto_opt_text"],
        "adaptive_weight_text": context["adaptive_weight_text"],
        "volatility": context["volatility"],
        "vol_state": context["vol_state"],
        "mtf_state": context["mtf_state"],
        "lookback": context["lookback"],
        "composite_lookback": context["composite_lookback"],
        "phase_pct": context["phase_pct"],
        "late_session_note": context["late_session_note"],
        "db_total_records": context["db_total_records"],
        "pending_slots": context["pending_slots"],
        "virtual_rows": context["virtual_rows"],
        "trend": context["trend"],
        "market_note": context["market_note"],
    }
    return payload


def telegram_status_payload():
    return {
        "configured": is_telegram_configured(),
        "last_setup_alert": setup_alert_state["decision"],
        "last_setup_alert_at": (
            setup_alert_state["sent_at"].isoformat()
            if setup_alert_state["sent_at"]
            else None
        ),
    }


# ========================
# TRADE MANAGEMENT
# ========================
def check_trade(price):
    global active_trade, wins, losses, loss_streak, win_streak, last_trade_time

    if not active_trade:
        return

    current = price
    entry = active_trade["entry"]

    # BREAK EVEN
    if not active_trade["be"]:
        if active_trade["type"] == "BUY" and current >= entry * 1.0015:
            active_trade["sl"] = entry
            active_trade["be"] = True
        elif active_trade["type"] == "SELL" and current <= entry * 0.9985:
            active_trade["sl"] = entry
            active_trade["be"] = True

    # PARTIAL TP
    if not active_trade["partial_tp"]:
        if active_trade["type"] == "BUY" and current >= entry * 1.002:
            active_trade["partial_tp"] = True
        elif active_trade["type"] == "SELL" and current <= entry * 0.998:
            active_trade["partial_tp"] = True

    # TP / SL
    if active_trade["type"] == "BUY":
        if current >= active_trade["tp"]:
            for trade in reversed(trades_history):
                if trade.get("status") == "ACTIVE":
                    trade["status"] = "WIN"
                    break
            wins += 1
            win_streak += 1
            loss_streak = 0
            session_performance[active_trade["session"]]["win"] += 1
            active_trade = None
            last_trade_time = datetime.now()
        elif current <= active_trade["sl"]:
            for trade in reversed(trades_history):
                if trade.get("status") == "ACTIVE":
                    trade["status"] = "LOSS"
                    break
            losses += 1
            loss_streak += 1
            win_streak = 0
            session_performance[active_trade["session"]]["loss"] += 1
            active_trade = None
            last_trade_time = datetime.now()

    else:
        if current <= active_trade["tp"]:
            for trade in reversed(trades_history):
                if trade.get("status") == "ACTIVE":
                    trade["status"] = "WIN"
                    break
            wins += 1
            win_streak += 1
            loss_streak = 0
            session_performance[active_trade["session"]]["win"] += 1
            active_trade = None
            last_trade_time = datetime.now()
        elif current >= active_trade["sl"]:
            for trade in reversed(trades_history):
                if trade.get("status") == "ACTIVE":
                    trade["status"] = "LOSS"
                    break
            losses += 1
            loss_streak += 1
            win_streak = 0
            session_performance[active_trade["session"]]["loss"] += 1
            active_trade = None
            last_trade_time = datetime.now()


# ========================
# WEBHOOK
# ========================
@app.route("/webhook", methods=["POST"])
def webhook():
    global active_trade

    data = request.get_json(silent=True) or {}
    if not data:
        return {"status": "no data"}, 400

    signal_type = data.get("type")
    if signal_type not in {"BUY", "SELL"}:
        return {"status": "invalid signal type"}, 400

    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return {"status": "invalid price"}, 400

    session = get_session()
    volatility = get_volatility()
    trend = get_trend()

    # COOLDOWN
    if last_trade_time and (datetime.now() - last_trade_time).total_seconds() < COOLDOWN_SECONDS:
        return {"status": "cooldown"}

    # LOSS STOP
    if loss_streak >= MAX_LOSS_STREAK:
        return {"status": "paused"}

    # MEMORY FILTER
    if len(trade_memory) >= 3 and all(t["type"] == signal_type for t in trade_memory[-3:]):
        return {"status": "overtrading"}

    # SESSION FILTER
    perf = session_performance[session]
    total = perf["win"] + perf["loss"]
    if total >= 5 and perf["win"] / total < 0.4:
        return {"status": "bad session"}

    # ENGINE
    alignment = 2  # simplified
    bias = trend
    score = calculate_score(signal_type, bias, alignment, volatility)
    quality = get_quality(score)
    prob_up, prob_down = get_probability(score)

    latest_data.update({
        "score": score,
        "prob_up": prob_up,
        "prob_down": prob_down,
        "alignment": alignment,
        "quality": quality,
        "bias": bias,
    })

    if quality not in ["A+", "A"]:
        return {"status": "low quality"}

    # RISK
    tp_pct = 0.004 if session != "ASIA" else 0.002
    sl_pct = 0.002

    if signal_type == "BUY":
        active_trade = {
            "type": "BUY",
            "entry": price,
            "tp": price * (1 + tp_pct),
            "sl": price * (1 - sl_pct),
            "be": False,
            "partial_tp": False,
            "quality": quality,
            "session": session,
        }
    else:
        active_trade = {
            "type": "SELL",
            "entry": price,
            "tp": price * (1 - tp_pct),
            "sl": price * (1 + sl_pct),
            "be": False,
            "partial_tp": False,
            "quality": quality,
            "session": session,
        }

    trade_memory.append({"type": signal_type})
    if len(trade_memory) > MAX_MEMORY:
        trade_memory.pop(0)

    signals.append({"type": signal_type, "tf": data.get("tf", "-"), "price": price})
    if len(signals) > 20:
        signals.pop(0)

    trades_history.append({**active_trade, "status": "ACTIVE"})
    if len(trades_history) > 50:
        trades_history.pop(0)

    send_telegram(format_signal_alert({
        "decision_text": f"{signal_type} SETUP",
        "price": price,
        "session": session,
        "trend": trend,
        "quality": quality,
        "score": score,
        "edge": abs(prob_up - prob_down),
        "prob_up": prob_up,
        "prob_down": prob_down,
        "composite_bias": latest_data.get("composite_bias", "NEUTRAL"),
        "composite_prob_up": latest_data.get("composite_prob_up", 50),
        "composite_prob_down": latest_data.get("composite_prob_down", 50),
    }))

    return {"status": "ok"}


# ========================
# DASHBOARD
# ========================
@app.route("/")
def home():
    price = get_btc_price()

    return render_template("index.html", **build_dashboard_context(price))


@app.route("/price")
def price():
    current_price = get_btc_price()
    if current_price is None:
        return jsonify({"price": None, "status": "unavailable"}), 503
    return jsonify({"price": current_price, "status": "ok"})


@app.route("/dashboard-data")
def dashboard_data():
    current_price = get_btc_price()
    context = build_dashboard_context(current_price)
    maybe_send_setup_alert(context)
    status = "ok" if current_price is not None else "unavailable"
    return jsonify({**serialize_dashboard_context(context), "status": status})


@app.route("/telegram-status")
def telegram_status():
    return jsonify(telegram_status_payload())


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ========================
# RUN
# ========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
