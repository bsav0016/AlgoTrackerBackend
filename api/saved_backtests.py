"""Saved backtests: every successful backtest is kept for its owner so it can be reopened for free, rerun with a
tweak, and compared.

What is kept is the request (symbol, interval, period, signals, exit rules, costs), the summary numbers, and a thinned
series of rows: enough for the app to draw the same chart and mark the same trades, at a few tens of kilobytes instead
of every candle. The thinning keeps every candle where the position changed or a rule sold, so the trades read back
exactly, and it keeps each stretch's highest and lowest points so a spike is never averaged away.
"""
import dataclasses

from django.db import transaction

MAX_SAVED = 50  # per user; the oldest are dropped when a new one is saved
MAX_ROWS = 800  # about this many rows are kept per saved run (a little over when there are many trades)
MAX_TRADE_ROWS = 2000  # a strategy that flips position every few candles would keep a row per flip; past this it is thinned like any other
ROW_KEYS = ("datetime", "close", "owned", "return", "exit_reason")
SUMMARY_KEYS = (
    "percent_return", "percent_return_adj", "period_return", "period_return_adj", "period_return_before_costs",
    "annualized_is_extrapolated", "trade_cost_percent", "trades", "sharpe_ratio", "sharpe_ratio_adj",
    "sortino_ratio", "sortino_ratio_adj", "data_window",
)


def thin_rows(rows, limit=MAX_ROWS):
    """About `limit` rows that still draw the same chart and give the same trades. A short series is unchanged."""
    if len(rows) <= limit:
        return list(rows)
    keep = {0, len(rows) - 1}
    trades = set()
    for i in range(1, len(rows)):
        if rows[i]["owned"] != rows[i - 1]["owned"] or rows[i]["exit_reason"]:
            trades.update((i - 1, i))  # both sides of a trade, so the change is still seen between neighbours
    if len(trades) <= MAX_TRADE_ROWS:
        keep |= trades
    buckets = max(1, (limit - len(keep)) // 6)
    size = len(rows) / buckets
    for b in range(buckets):
        start, end = int(b * size), min(len(rows), int((b + 1) * size))
        if start >= end:
            continue
        window = range(start, end)
        keep.update((
            start, end - 1,
            min(window, key=lambda i: rows[i]["close"]), max(window, key=lambda i: rows[i]["close"]),
            min(window, key=lambda i: rows[i]["return"]), max(window, key=lambda i: rows[i]["return"]),
        ))
    return [rows[i] for i in sorted(keep)]


def _short(number):
    """8 significant digits: far more than a chart needs, and about a seventh smaller to store. Significant digits,
    not decimal places, so a very low price keeps its precision."""
    return float(f"{float(number):.8g}")


def _row(row):
    moment = row["datetime"]
    return {
        "datetime": moment.isoformat() if hasattr(moment, "isoformat") else str(moment),
        "close": _short(row["close"]),
        "owned": int(row["owned"]),
        "return": _short(row["return"]),
        "exit_reason": row.get("exit_reason") or "",
    }


def save(user, *, symbol, interval, start_date, end_date, signals, exit_rules, delay, cost_percent, response):
    """Keep one finished backtest for `user` and drop their oldest beyond MAX_SAVED. `response` is the backtest's
    response data. Returns the saved row."""
    from api.models import SavedBacktest

    rows = [_row(row) for row in response["result"]]
    window = response["data_window"]
    buy = [dataclasses.asdict(s) for s in signals if s.buy_signal]
    sell = [dataclasses.asdict(s) for s in signals if not s.buy_signal]
    with transaction.atomic():
        saved = SavedBacktest.objects.create(
            user=user, symbol=symbol, interval=interval, requested_start=start_date, requested_end=end_date,
            covered_start=window["covered_start"], covered_end=window["covered_end"],
            buy_signals=buy, sell_signals=sell, exit_rules=exit_rules or {}, next_candle_execution=delay,
            cost_percent=cost_percent, summary={key: response[key] for key in SUMMARY_KEYS},
            series=thin_rows(rows))
        stale = list(SavedBacktest.objects.filter(user=user).order_by("-id").values_list("id", flat=True)[MAX_SAVED:])
        if stale:
            SavedBacktest.objects.filter(id__in=stale).delete()
    return saved


def list_item(saved):
    """The short form for the list: no series."""
    return {
        "id": saved.pk, "created_at": saved.created_at, "label": saved.label, "symbol": saved.symbol,
        "interval": saved.interval, "covered_start": saved.covered_start, "covered_end": saved.covered_end,
        "period_return": saved.summary.get("period_return"), "trades": saved.summary.get("trades"),
        "has_exit_rules": bool(saved.exit_rules),
    }


def detail(saved):
    """The whole run in the shape of a backtest response (so the app reads it the same way), plus what produced it.
    No chart images and no credit balances: opening a saved run is free."""
    return {
        **list_item(saved),
        "requested_start": saved.requested_start, "requested_end": saved.requested_end,
        "buy_signals": saved.buy_signals, "sell_signals": saved.sell_signals, "exit_rules": saved.exit_rules,
        "next_candle_execution": saved.next_candle_execution,
        **saved.summary,
        "result": saved.series,
    }
