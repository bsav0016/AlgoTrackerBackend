"""Standard indicator definitions written as plain loops, with no pandas, so they can be read against the books.

RSI and ADX follow J. Welles Wilder Jr., "New Concepts in Technical Trading Systems" (1978): the first average is a
plain average of the first `n` values and every later one is smoothed as (previous * (n - 1) + new) / n.
Used by the reference tests and by the `indicator_report` command.
"""


def wilder_rsi(close: list[float], n: int) -> list[float | None]:
    """RSI per candle. The first value is at index n (n price changes are needed), earlier ones are None."""
    out: list[float | None] = [None] * len(close)
    if len(close) <= n:
        return out
    gains = [max(close[i] - close[i - 1], 0.0) for i in range(1, len(close))]
    losses = [max(close[i - 1] - close[i], 0.0) for i in range(1, len(close))]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(close)):
        if i > n:
            avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
            avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def wilder_adx(high: list[float], low: list[float], close: list[float], n: int) -> list[float | None]:
    """ADX per candle. The first value is at index 2n - 1, earlier ones are None."""
    size = len(close)
    out: list[float | None] = [None] * size
    if size < 2 * n:
        return out
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, size):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    smooth_tr, smooth_plus, smooth_minus = sum(tr[:n]), sum(plus_dm[:n]), sum(minus_dm[:n])
    dx: list[float] = []
    for i in range(n - 1, len(tr)):
        if i >= n:
            smooth_tr += tr[i] - smooth_tr / n
            smooth_plus += plus_dm[i] - smooth_plus / n
            smooth_minus += minus_dm[i] - smooth_minus / n
        plus_di = 100 * smooth_plus / smooth_tr if smooth_tr else 0.0
        minus_di = 100 * smooth_minus / smooth_tr if smooth_tr else 0.0
        total = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / total if total else 0.0)
    adx = sum(dx[:n]) / n
    out[2 * n - 1] = adx
    for k in range(n, len(dx)):
        adx = (adx * (n - 1) + dx[k]) / n
        out[n + k] = adx
    return out


def ema(values: list[float], span: int) -> list[float]:
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def macd_lines(close: list[float], fast: int, slow: int, signal: int) -> tuple[list[float], list[float]]:
    """The MACD line and its signal line."""
    line = [f - s for f, s in zip(ema(close, fast), ema(close, slow))]
    return line, ema(line, signal)
