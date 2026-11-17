"""Draws the picture card for a shared strategy (the link preview in messages and social apps).

Only what the public view may show goes on the card: title, symbol, interval, rules, the two return figures
and a curve built from the strategy's own trades. It is drawn with Pillow at twice the size and scaled down, which
gives smooth edges, and it keeps no state between requests. Text uses Pillow's built-in font, so no font files are
needed.
"""
import io
import math

from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont
from api.exit_rules import describe_rules

WIDTH, HEIGHT = 1200, 630  # the size social apps expect for a link preview
SCALE = 2  # drawn at 2x, then reduced, so lines and text are smooth
DISCLAIMER = "Past results are not a prediction of future results. Not financial advice."

BACKGROUND = "#0f172a"
PANEL = "#1e293b"
TEXT = "#f1f5f9"
MUTED = "#94a3b8"
GREEN = "#22c55e"
RED = "#ef4444"
ACCENT = "#38bdf8"

LEFT = 60  # text column
RULES_WIDTH = 500  # rule lines stop here so they never run into the chart's labels
PLOT = (624, 101, 1140, 454)  # the chart panel: left, top, right, bottom
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def equity_points(trades, strategy_return, now=None):
    """The strategy's value over time as (date, multiple) points, starting at 1.0 on the first buy.

    Value only changes while a position is held, so a closed round trip multiplies it by sell/buy price. An open
    position ends at `strategy_return`, which the live job keeps up to date. Fewer than two points means there is
    no curve to draw yet (no trades, or no open or closed round trip).
    """
    now = now or timezone.now()
    points = []
    value = 1.0
    open_price = None
    for trade in trades:
        price = float(trade.price)
        if trade.is_buy and open_price is None:
            open_price = price
            if not points:
                points.append((trade.date, 1.0))
        elif not trade.is_buy and open_price is not None:
            if open_price > 0:
                value *= price / open_price
            open_price = None
            points.append((trade.date, value))
    if open_price is not None:
        points.append((now, float(strategy_return)))
    return points if len(points) >= 2 else []


def describe_signal(signal):
    """One rule as a short line, e.g. `RSI(14) > 70`."""
    windows = [signal.get(key) for key in ("window", "fast_window", "slow_window", "signal_window")]
    args = ",".join(str(w) for w in windows if w)
    name = f"{signal.get('name', '?')}({args})" if args else str(signal.get("name", "?"))
    return f"{name} {'>' if signal.get('above_target') else '<'} {signal.get('target_value')}"


def _percent(multiple):
    return f"{(float(multiple) - 1) * 100:+.1f}%"


def _font(pixels):
    return ImageFont.load_default(size=pixels * SCALE)


def _fit(draw, text, font, max_width):
    """`text`, shortened with "..." if it would be wider than `max_width` pixels (at card size)."""
    text = str(text)
    limit = max_width * SCALE
    if draw.textlength(text, font=font) <= limit:
        return text
    while text and draw.textlength(text + "...", font=font) > limit:
        text = text[:-1]
    return text.rstrip() + "..."


def _write(draw, x, y, text, size, fill, anchor="ls", bold=False):
    """Text at card coordinates; `y` is the baseline unless `anchor` says otherwise. Bold is a thin outline."""
    font = _font(size)
    stroke = max(1, round(size * SCALE / 40)) if bold else 0
    draw.text((x * SCALE, y * SCALE), text, font=font, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=fill)


def nice_ticks(low, high, count=5):
    """Round-number tick values covering [low, high], about `count` of them."""
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return [low] if math.isfinite(low) else []
    rough = (high - low) / max(1, count - 1)
    magnitude = 10 ** math.floor(math.log10(rough))
    normalized = rough / magnitude
    step = (1 if normalized < 1.5 else 2 if normalized < 3 else 5 if normalized < 7 else 10) * magnitude
    ticks = []
    value = math.ceil(low / step) * step
    while value <= high + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _tick_label(value, step_is_whole):
    return f"{value:.0f}" if step_is_whole else f"{value:.1f}"


def _month_label(moment):
    return f"{MONTHS[moment.month - 1]} {str(moment.year)[2:]}"


def _draw_chart(image, draw, points):
    left, top, right, bottom = PLOT
    draw.rectangle([left * SCALE, top * SCALE, right * SCALE, bottom * SCALE], fill=PANEL)
    if not points:
        _write(draw, (left + right) / 2, (top + bottom) / 2, "No completed trades yet", 22, MUTED, anchor="mm")
        return

    values = [(multiple - 1) * 100 for _, multiple in points]
    low, high = min(values + [0]), max(values + [0])
    if high == low:
        high = low + 1
    room = (high - low) * 0.05
    low, high = low - room, high + room
    first, last = points[0][0], points[-1][0]
    span = (last - first).total_seconds() or 1
    inset = 22  # room inside the panel so the line does not touch the edges

    def x_of(moment):
        return left + inset + ((moment - first).total_seconds() / span) * (right - left - 2 * inset)

    def y_of(value):
        return bottom - ((value - low) / (high - low)) * (bottom - top)

    # Zero line, dashed.
    zero = y_of(0) * SCALE
    x = (left + 1) * SCALE
    while x < (right - 1) * SCALE:
        draw.line([(x, zero), (min(x + 6 * SCALE, (right - 1) * SCALE), zero)], fill=MUTED, width=SCALE)
        x += 10 * SCALE

    # Value ticks on the left, outside the panel.
    ticks = nice_ticks(low, high, 5)
    whole = len(ticks) < 2 or (ticks[1] - ticks[0]) >= 1 - 1e-9
    for tick in ticks:
        _write(draw, left - 8, y_of(tick), _tick_label(tick, whole), 14, MUTED, anchor="rm")

    # Month labels underneath.
    for i in range(4):
        moment = first + (last - first) * (i / 3)
        _write(draw, x_of(moment), bottom + 18, _month_label(moment), 14, MUTED, anchor="mm")

    # "Return (%)", turned to read upwards, centred on the panel.
    label = Image.new("RGBA", (130 * SCALE, 24 * SCALE), (0, 0, 0, 0))
    ImageDraw.Draw(label).text((65 * SCALE, 12 * SCALE), "Return (%)", font=_font(15), fill=MUTED, anchor="mm")
    label = label.rotate(90, expand=True)
    image.paste(label, (int((left - 60) * SCALE - label.width / 2), int((top + bottom) / 2 * SCALE - label.height / 2)), label)

    # The curve holds each value until the next trade.
    path = []
    for index, (moment, multiple) in enumerate(points):
        x_point, y_point = x_of(moment) * SCALE, y_of(values[index]) * SCALE
        if path:
            path.append((x_point, path[-1][1]))
        path.append((x_point, y_point))
    draw.line(path, fill=ACCENT, width=round(3.5 * SCALE), joint="curve")


def render_card(strategy, trades, now=None):
    """The card as PNG bytes."""
    image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title = str(strategy.title)
    title_size = 47 if len(title) <= 28 else 33
    _write(draw, LEFT, 76, _fit(draw, title, _font(title_size), 1080), title_size, TEXT, bold=True)
    _write(draw, LEFT, 126, f"{strategy.symbol}  ·  {strategy.interval}", 28, ACCENT)

    rules = [f"Buy  {describe_signal(s)}" for s in strategy.buy_signals][:3]
    rules += [f"Sell  {describe_signal(s)}" for s in strategy.sell_signals][:3]
    exits = describe_rules(strategy.exit_rules)
    if exits:
        rules = rules[:5] + ["Exit  " + ", ".join(exits)]
    for i, line in enumerate(rules[:6]):
        _write(draw, LEFT, 189 + i * 34.65, _fit(draw, line, _font(21), RULES_WIDTH), 21, MUTED)

    for x, label, value in ((LEFT, "Strategy", strategy.strategy_return), (324, "Holding", strategy.asset_return)):
        _write(draw, x, 447, label, 21, MUTED)
        _write(draw, x, 510, _percent(value), 42, GREEN if float(value) >= 1 else RED, bold=True)

    _draw_chart(image, draw, equity_points(trades, strategy.strategy_return, now))

    _write(draw, LEFT, 592, DISCLAIMER, 17, MUTED)
    _write(draw, WIDTH - LEFT, 592, "AlgoTracker", 19, TEXT, anchor="rs", bold=True)

    buffer = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.LANCZOS).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
