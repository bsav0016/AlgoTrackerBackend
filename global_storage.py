SYMBOLS = [
    "EUR/USD",
    "BTC/USD",
    "AAPL",
    "QQQ"
]

WEBSOCKET_RUNNING = False

latest_prices = {symbol: None for symbol in SYMBOLS}
