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
        