from django.test import override_settings
from rest_framework import status

from api.models import Strategy
from api.tests.test_shared_strategies import ShareTestCase

XSS = '"><script>alert(1)</script>'


class SharePageTests(ShareTestCase):
    def page(self, link, **extra):
        return self.client.get(f"/s/{link}", **extra)

    def test_the_page_carries_the_preview_tags_for_this_strategy(self):
        link = self.share()
        response = self.page(link)
        html = response.content.decode()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("text/html", response["Content-Type"])
        self.assertIn('<meta property="og:title" content="Momentum on AlgoTracker">', html)
        self.assertIn(f'<meta property="og:image" content="http://testserver/api/shared/{link}/card.png">', html)
        self.assertIn('<meta name="twitter:card" content="summary_large_image">', html)
        self.assertIn(f'<meta property="og:url" content="http://testserver/s/{link}">', html)
        self.assertIn("AAPL (1day)", html)
        self.assertIn("Buy SMA(2) &lt; 1.0", html)

    def test_it_needs_no_sign_in_and_hides_the_page_from_search(self):
        link = self.share()
        self.assertIn('name="robots" content="noindex"', self.page(link).content.decode())

    def test_the_open_button_uses_the_app_scheme(self):
        link = self.share()
        self.assertIn(f'href="algotracker://shared/{link}"', self.page(link).content.decode())
        with override_settings(SHARE_APP_SCHEME="algo"):
            self.assertIn(f'href="algo://shared/{link}"', self.page(link).content.decode())

    def test_the_preview_uses_the_configured_public_address(self):
        link = self.share()
        with override_settings(PUBLIC_BASE_URL="https://api.example.com/"):
            html = self.page(link).content.decode()
        self.assertIn(f'content="https://api.example.com/api/shared/{link}/card.png"', html)
        self.assertNotIn("testserver", html)

    def test_store_links_show_only_when_set_and_only_if_web_addresses(self):
        link = self.share()
        self.assertNotIn("App Store", self.page(link).content.decode())
        with override_settings(IOS_APP_STORE_URL="https://apps.apple.com/app/id1",
                               ANDROID_PLAY_STORE_URL="javascript:alert(1)"):
            html = self.page(link).content.decode()
        self.assertIn('href="https://apps.apple.com/app/id1"', html)
        self.assertNotIn("Google Play", html)
        self.assertNotIn("javascript:", html)

    def test_the_disclaimer_is_on_the_page(self):
        link = self.share()
        self.assertIn("Past results are not a prediction", self.page(link).content.decode())

    def test_a_hostile_title_and_rule_cannot_inject_html(self):
        Strategy.objects.filter(pk=self.strategy.pk).update(
            title=XSS, buy_signals=[{"name": XSS, "above_target": True, "target_value": XSS}])
        link = self.share()
        html = self.page(link).content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn('"><script', html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_page_holds_nothing_about_the_owner(self):
        link = self.share()
        html = self.page(link).content.decode().lower()
        for secret in ("owner@example.com", "olive"):
            self.assertNotIn(secret, html)

    def test_it_sets_a_strict_content_security_policy(self):
        link = self.share()
        policy = self.page(link)["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("script-src", policy)

    def test_a_revoked_or_unknown_link_shows_a_friendly_404_page(self):
        link = self.share()
        self.client.force_authenticate(self.owner)
        self.client.delete(self.share_url())
        self.client.force_authenticate(None)
        for url in (link, "nope"):
            response = self.page(url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            self.assertIn("not shared any more", response.content.decode())
            self.assertNotIn("og:image", response.content.decode())

    def test_it_is_limited_per_address(self):
        link = self.share()
        with override_settings(AUTH_THROTTLE_RATES={"shared_view_address": "2/min"}):
            statuses = [self.page(link).status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 429, 429])
