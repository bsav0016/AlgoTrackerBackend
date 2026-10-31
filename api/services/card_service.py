"""Draws the picture card for a shared strategy (the link preview in messages and social apps).

Only what the public view may show goes on the card: title, symbol, interval, rules, the two return figures
and a curve built from the strategy's own trades. Drawing uses matplotlib's `Figure` directly, not `pyplot`,
so nothing is kept in global state between requests.
"""
import io

from django.utils import timezone
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure

WIDTH, HEIGHT, DPI = 1200, 630, 100  # the size social apps expect for a link preview
DISCLAIMER = "Past results are not a prediction of future results. Not financial advice."

BACKGROUND = "#0f172a"
PANEL = "#1e293b"
TEXT = "#f1f5f9"
MUTED = "#94a3b8"
GREEN = "#22c55e"
RED = "#ef4444"
ACCENT = "#38bdf8"


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


def _safe(text):
    """matplotlib reads a pair of dollar signs as maths, which would break on a title like `$$ strategy`."""
    return str(text).replace("$", r"\$")


def render_card(strategy, trades, now=None):
    """The card as PNG bytes."""
    figure = Figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor=BACKGROUND)
    FigureCanvasAgg(figure)

    title = strategy.title
    figure.text(0.05, 0.88, _safe(title), color=TEXT, fontsize=34 if len(title) <= 28 else 24, fontweight="bold")
    figure.text(0.05, 0.80, _safe(f"{strategy.symbol}  ·  {strategy.interval}"), color=ACCENT, fontsize=20)

    rules = [f"Buy  {describe_signal(s)}" for s in strategy.buy_signals][:3]
    rules += [f"Sell  {describe_signal(s)}" for s in strategy.sell_signals][:3]
    for i, line in enumerate(rules[:6]):
        figure.text(0.05, 0.70 - i * 0.055, _safe(line), color=MUTED, fontsize=15)

    for x, label, value in ((0.05, "Strategy", strategy.strategy_return), (0.27, "Holding", strategy.asset_return)):
        figure.text(x, 0.29, label, color=MUTED, fontsize=15)
        figure.text(x, 0.19, _percent(value), color=GREEN if float(value) >= 1 else RED, fontsize=30,
                    fontweight="bold")

    axes = figure.add_axes([0.52, 0.28, 0.43, 0.56], facecolor=PANEL)
    points = equity_points(trades, strategy.strategy_return, now)
    for spine in axes.spines.values():
        spine.set_color(PANEL)
    if points:
        dates = [p[0] for p in points]
        values = [(p[1] - 1) * 100 for p in points]
        axes.step(dates, values, where="post", color=ACCENT, linewidth=2.5)
        axes.axhline(0, color=MUTED, linewidth=0.8, linestyle="--")
        locator = AutoDateLocator(maxticks=5)
        axes.xaxis.set_major_locator(locator)
        axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        axes.tick_params(colors=MUTED, labelsize=10)
        axes.set_ylabel("Return (%)", color=MUTED, fontsize=11)
    else:
        axes.set_xticks([])
        axes.set_yticks([])
        axes.text(0.5, 0.5, "No completed trades yet", color=MUTED, fontsize=16, ha="center", va="center",
                  transform=axes.transAxes)

    figure.text(0.05, 0.06, DISCLAIMER, color=MUTED, fontsize=12)
    figure.text(0.95, 0.06, "AlgoTracker", color=TEXT, fontsize=14, fontweight="bold", ha="right")

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=BACKGROUND)
    return buffer.getvalue()
