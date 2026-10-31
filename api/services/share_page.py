"""The HTML page behind a share link (`/s/<id>`).

Link previews in messages and social apps are built from the page's tags by a crawler that does not run
JavaScript, so the page has to be real HTML with the strategy's name and card image already in it. Every value
that comes from a user is escaped here; nothing is put into the page unescaped.
"""
from html import escape

from django.conf import settings

from api.services.card_service import DISCLAIMER, describe_signal

STYLE = """
body{margin:0;background:#0f172a;color:#f1f5f9;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
display:flex;justify-content:center}
main{max-width:640px;width:100%;padding:24px 16px;box-sizing:border-box;text-align:center}
img{width:100%;height:auto;border-radius:12px}
h1{font-size:1.5rem;margin:16px 0 4px}
p{color:#94a3b8;margin:8px 0}
a.button{display:inline-block;margin:12px 6px 4px;padding:12px 22px;border-radius:10px;background:#38bdf8;
color:#0f172a;font-weight:700;text-decoration:none}
a.store{display:inline-block;margin:4px 8px;color:#38bdf8}
small{display:block;margin-top:24px;color:#64748b}
"""


def _web_url(value):
    """A store link from settings, only if it is a plain web address."""
    return value if value.startswith(("https://", "http://")) else ""


def base_url(request):
    return (settings.PUBLIC_BASE_URL or request.build_absolute_uri("/")).rstrip("/")


def _page(title, description, body, image_url="", page_url=""):
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="robots" content="noindex">',
        f"<title>{escape(title)}</title>",
        f'<meta name="description" content="{escape(description, quote=True)}">',
        '<meta property="og:site_name" content="AlgoTracker">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{escape(title, quote=True)}">',
        f'<meta property="og:description" content="{escape(description, quote=True)}">',
    ]
    if page_url:
        tags.append(f'<meta property="og:url" content="{escape(page_url, quote=True)}">')
    if image_url:
        tags += [
            f'<meta property="og:image" content="{escape(image_url, quote=True)}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:image" content="{escape(image_url, quote=True)}">',
            f'<meta name="twitter:title" content="{escape(title, quote=True)}">',
        ]
    tags.append(f"<style>{STYLE}</style>")
    return f"<!doctype html><html lang=\"en\"><head>{''.join(tags)}</head><body><main>{body}</main></body></html>"


def store_links():
    links = []
    for label, url in (("App Store", _web_url(settings.IOS_APP_STORE_URL)),
                       ("Google Play", _web_url(settings.ANDROID_PLAY_STORE_URL))):
        if url:
            links.append(f'<a class="store" href="{escape(url, quote=True)}">{label}</a>')
    return "".join(links)


def shared_strategy_page(request, share_id, strategy):
    root = base_url(request)
    card = f"{root}/api/shared/{share_id}/card.png"
    page = f"{root}/s/{share_id}"
    rules = [f"Buy {describe_signal(s)}" for s in strategy.buy_signals] + \
            [f"Sell {describe_signal(s)}" for s in strategy.sell_signals]
    description = f"{strategy.symbol} ({strategy.interval}): " + "; ".join(rules[:4])
    deep_link = f"{settings.SHARE_APP_SCHEME}://shared/{share_id}"
    body = (
        f'<img src="{escape(card, quote=True)}" width="1200" height="630" alt="{escape(strategy.title, quote=True)}">'
        f"<h1>{escape(strategy.title)}</h1>"
        f"<p>{escape(description)}</p>"
        f'<a class="button" href="{escape(deep_link, quote=True)}">Open in AlgoTracker</a>'
        f"<div>{store_links()}</div>"
        f"<small>{escape(DISCLAIMER)}</small>"
    )
    return _page(f"{strategy.title} on AlgoTracker", description, body, image_url=card, page_url=page)


def not_shared_page():
    body = ("<h1>This strategy is not shared any more</h1>"
            "<p>The owner turned the link off, or the link is wrong.</p>"
            f"<div>{store_links()}</div>")
    return _page("Not shared - AlgoTracker", "This strategy is not shared any more.", body)
